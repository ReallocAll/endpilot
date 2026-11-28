import asyncio
from dataclasses import FrozenInstanceError
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from item_response_stream import item_fixture

from endpilot import Bot
from endpilot.compression import PacketConnection
from endpilot.containers import Containers
from endpilot.inventory import Inventory
from endpilot.protocol import Protocol, RawPacket, write_batch
from endpilot.trading import TradeItem, TradeOffer, TradeOffers, Trading
from test_scheduled_input import Wire


def tag(value):
    return SimpleNamespace(value=value)


class CurrentTradeOffersTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.protocol = Protocol(2225)
        self.b = self.protocol.binding
        self.wire = Wire(self.protocol)
        settings = self.b.NetworkSettingsPacket(compression_algorithm=self.b.PacketCompressionAlgorithm.NONE)
        self.packets = PacketConnection(self.wire, self.protocol, settings)
        self.inventory = Inventory(self.packets, 1)
        self.containers = Containers(self.packets, None, self.inventory)
        self.trading = Trading(self.packets, None, None, self.inventory, self.containers)
        self.reader_error = None
        self.reader = self.packets.start_receiving()
        self.addAsyncCleanup(self.close_packets)
        self.addCleanup(self.trading.close)
        self.addCleanup(self.containers.close)
        self.addCleanup(self.inventory.close)

    async def close_packets(self):
        try:
            await self.packets.close()
        except OSError as error:
            if error is not self.reader_error:
                raise

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket(time=41)
        handled = asyncio.create_task(self.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(self.protocol.encode(packet) for packet in (*packets, marker)))
        await handled

    def quote(self, count=9, uses=0, *, invalid=False, tier=None):
        from bedrock_protocol import nbt

        amount = nbt.StringTag("unknown") if invalid else nbt.ByteTag(count)
        recipe = nbt.CompoundTag({
            "netId": nbt.IntTag(123), "uses": nbt.IntTag(uses), "maxUses": nbt.IntTag(12),
            "buyA": nbt.CompoundTag({"Name": nbt.StringTag("minecraft:emerald"), "Count": amount}),
            "sell": nbt.CompoundTag({"Name": nbt.StringTag("minecraft:book"), "Count": nbt.ByteTag(1)}),
        })
        if tier is not None:
            recipe["tier"] = tier
        data = nbt.CompoundTag({"Recipes": nbt.ListTag([recipe], int(nbt.Type.COMPOUND))})
        return self.b.UpdateTradePacket(
            container_id=7, type=self.b.ContainerType.TRADE, size=1, trader_tier=0,
            entity_unique_id=42, last_trading_player=1, display_name="Librarian",
            use_new_trade_screen=True, using_economy_trade=True, data=data,
        )

    def open_packet(self, kind):
        return self.b.ContainerOpenPacket(container_id=7, type=self.b.ContainerType[kind],
                                          pos=self.b.BlockPos(x=0, y=0, z=0), entity_unique_id=42)

    async def test_latest_server_quote_replaces_same_window_and_keeps_old_values_frozen(self):
        from bedrock_protocol import nbt

        self.assertIsNone(self.trading.current_offers)
        await self.deliver(self.quote(tier=nbt.IntTag(0)))
        original = self.trading.current_offers
        window = self.containers.window
        await self.deliver(self.quote(18, uses=3, tier=nbt.IntTag(2)))
        latest = self.trading.current_offers
        self.assertIs(self.containers.window, window)
        self.assertEqual((latest.offers[0].buy_a.count, latest.offers[0].uses), (18, 3))
        self.assertEqual((original.offers[0].buy_a.count, original.offers[0].uses), (9, 0))
        self.assertEqual((original.offers[0].tier, latest.offers[0].tier), (0, 2))
        with self.assertRaises(FrozenInstanceError):
            original.offers[0].buy_a.count = 1
        with self.assertRaises(FrozenInstanceError):
            original.offers[0].tier = 1

        await self.deliver(self.quote(invalid=True, uses=4, tier=nbt.StringTag("2")))
        invalid = self.trading.current_offers
        self.assertIs(self.containers.window, window)
        self.assertIsNone(invalid.offers[0].buy_a)
        self.assertIsNone(invalid.offers[0].tier)
        self.assertEqual(invalid.offers[0].uses, 4)
        self.assertEqual(latest.offers[0].buy_a.count, 18)
        self.assertEqual(self.wire.sent, [])

    async def test_close_and_same_number_reopen_require_a_quote_for_the_new_window(self):
        from bedrock_protocol import nbt

        await self.deliver(self.quote(tier=nbt.IntTag(1)))
        old = self.trading.current_offers
        window = self.containers.window
        await self.deliver(self.b.ContainerClosePacket(container_id=7, container_type=self.b.ContainerType.TRADE))
        self.assertIsNone(self.trading.current_offers)
        await self.deliver(self.open_packet("TRADE"))
        self.assertIsNot(self.containers.window, window)
        self.assertEqual(self.containers.window.container_id, old.window_id)
        self.assertIsNone(self.trading.current_offers)
        await self.deliver(self.quote(10))
        self.assertEqual(self.trading.current_offers.offers[0].buy_a.count, 10)
        self.assertIsNone(self.trading.current_offers.offers[0].tier)
        self.assertEqual(old.offers[0].tier, 1)
        await self.deliver(self.open_packet("CONTAINER"))
        self.assertIsNone(self.trading.current_offers)

    async def test_quotes_become_unavailable_when_the_reader_fails(self):
        await self.deliver(self.quote())
        original = self.trading.current_offers
        self.reader_error = OSError("trade reader failed")
        self.wire.incoming.put_nowait(self.reader_error)
        with self.assertRaises(OSError):
            await self.reader
        self.assertIsNone(self.trading.current_offers)
        self.assertEqual(original.offers[0].buy_a.count, 9)

    async def test_server_disconnect_clears_the_public_view(self):
        await self.deliver(self.quote())
        self.assertIsNotNone(self.trading.current_offers)
        await self.deliver(self.b.DisconnectPacket())
        self.assertIsNone(self.trading.current_offers)

    async def test_detached_trading_component_does_not_publish_its_previous_quote(self):
        await self.deliver(self.quote())
        self.assertIsNotNone(self.trading.current_offers)
        self.trading.close()
        self.assertIsNotNone(self.containers.window)
        self.assertIsNone(self.trading.current_offers)


class TradingTests(unittest.IsolatedAsyncioTestCase):
    def test_quote_constructor_keeps_tier_optional(self):
        self.assertIsNone(TradeOffer(17, TradeItem("minecraft:wheat", 20), None,
                                     TradeItem("minecraft:emerald", 1), 2, 16).tier)

    async def test_quotes_match_unique_actor_and_preserve_server_recipe(self):
        b = Protocol(2193).binding
        wrong = RawPacket(b.UpdateTradePacket.ID, b"wrong")
        correct = RawPacket(b.UpdateTradePacket.ID, b"right")
        recipes = [{
            "netId": tag(17), "buyA": {"Name": tag("minecraft:wheat"), "Count": tag(20)},
            "sell": {"Name": tag("minecraft:emerald"), "Count": tag(1)},
            "uses": tag(2), "maxUses": tag(16),
        }]
        trade = SimpleNamespace(
            container_id=5, entity_unique_id=42, display_name="Farmer",
            trader_tier=1, use_new_trade_screen=True, data={"Recipes": recipes},
        )
        mismatch = SimpleNamespace(entity_unique_id=99)
        decoded = {b"wrong": mismatch, b"right": trade}

        async def wait(packet_id, *, match, timeout):
            self.assertEqual(packet_id, b.UpdateTradePacket.ID)
            self.assertEqual(timeout, 3)
            self.assertFalse(match(wrong))
            self.assertTrue(match(correct))
            return correct

        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: decoded[raw.body]),
            wait_for_packet=wait,
        )
        entities = SimpleNamespace(
            get=lambda runtime_id: SimpleNamespace(entity_id=42) if runtime_id == 77 else None,
            interact=AsyncMock(),
        )
        movement, inventory = object(), object()
        trading = Trading(packets, entities, movement, inventory, None)
        observed = await trading.open(77, timeout=3)
        entities.interact.assert_awaited_once_with(movement, inventory, 77)
        self.assertEqual((observed.window_id, observed.trader_unique_id), (5, 42))
        self.assertEqual((observed.display_name, observed.trader_tier), ("Farmer", 1))
        self.assertTrue(observed.use_new_trade_screen)
        self.assertEqual(len(observed.offers), 1)
        quote = observed.offers[0]
        self.assertEqual(quote.recipe_net_id, 17)
        self.assertEqual(quote.buy_a, TradeItem("minecraft:wheat", 20))
        self.assertIsNone(quote.buy_b)
        self.assertEqual(quote.sell, TradeItem("minecraft:emerald", 1))
        self.assertEqual((quote.uses, quote.max_uses), (2, 16))
        with self.assertRaisesRegex(ValueError, "observed actor"):
            await trading.open(78)
        self.assertEqual(entities.interact.await_count, 1)

    async def test_empty_quotes_are_valid_and_timeout_does_not_open_a_window(self):
        b = Protocol(2193).binding
        entities = SimpleNamespace(
            get=lambda _: SimpleNamespace(entity_id=42), interact=AsyncMock(),
        )
        raw = RawPacket(b.UpdateTradePacket.ID, b"empty")
        quote = SimpleNamespace(
            container_id=5, entity_unique_id=42, display_name="Trader",
            trader_tier=0, use_new_trade_screen=False, data={},
        )
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda _: quote),
            wait_for_packet=AsyncMock(return_value=raw),
        )
        self.assertEqual((await Trading(packets, entities, None, None, None).open(7)).offers, ())
        packets.wait_for_packet = AsyncMock(side_effect=TimeoutError("no response"))
        with self.assertRaises(TimeoutError):
            await Trading(packets, entities, None, None, None).open(7)

    async def test_native_nbt_recipe_uses_upstream_map_and_list_bindings(self):
        from bedrock_protocol import nbt
        from endpilot.trading import _read_offers

        def item(name, count):
            tag = nbt.CompoundTag()
            tag["Name"] = nbt.StringTag(name)
            tag["Count"] = nbt.ByteTag(count)
            return tag

        recipe = nbt.CompoundTag()
        recipe["netId"] = nbt.IntTag(17)
        recipe["buyA"] = item("minecraft:wheat", 20)
        recipe["sell"] = item("minecraft:emerald", 1)
        recipe["uses"] = nbt.IntTag(2)
        recipe["maxUses"] = nbt.IntTag(16)
        data = nbt.CompoundTag()
        data["Recipes"] = nbt.ListTag([recipe], int(nbt.Type.COMPOUND))
        packet = SimpleNamespace(
            container_id=5, entity_unique_id=42, display_name="Farmer",
            trader_tier=1, use_new_trade_screen=True, data=data,
        )
        observed = _read_offers(packet)
        self.assertEqual(len(observed.offers), 1)
        self.assertEqual(observed.offers[0].buy_a, TradeItem("minecraft:wheat", 20))
        self.assertEqual(observed.offers[0].sell, TradeItem("minecraft:emerald", 1))

    async def test_stage_ingredient_waits_for_matching_server_reply_and_updates_source(self):
        b = Protocol(2193).binding
        source = b.SerializedNetworkItemStackDescriptor(
            id=1, stack_size=5, aux_value=0, net_id_variant=23,
            block_runtime_id=0, user_data_buffer=b"",
        )
        reply = SimpleNamespace(
            client_request_id=SimpleNamespace(raw_id=-1),
            result=SimpleNamespace(name="SUCCESS"),
            containers=[SimpleNamespace(
                full_container_name=SimpleNamespace(
                    name=b.ContainerEnumName.COMBINED_HOTBAR_AND_INVENTORY_CONTAINER,
                ),
                slots=[SimpleNamespace(slot=3, amount=2,
                                       item_stack_net_id=SimpleNamespace(raw_id=55))],
            )],
        )
        other = SimpleNamespace(client_request_id=SimpleNamespace(raw_id=-3))
        raw = RawPacket(b.ItemStackResponsePacket.ID, b"accepted")
        raw_other = RawPacket(b.ItemStackResponsePacket.ID, b"unrelated")
        responses = {
            b"accepted": SimpleNamespace(responses=[other, reply]),
            b"unrelated": SimpleNamespace(responses=[other]),
        }

        async def wait(packet_id, *, timeout, match):
            self.assertEqual(packet_id, b.ItemStackResponsePacket.ID)
            self.assertFalse(match(raw_other))
            self.assertTrue(match(raw))
            return raw

        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: responses[raw.body]),
            wait_for_packet=wait, send=AsyncMock(),
        )
        containers = SimpleNamespace(window=SimpleNamespace(
            container_type="TRADE", container_id=2, entity_unique_id=42,
        ))
        inventory = item_fixture(packets, {3: source})
        self.addCleanup(inventory.close)
        trading = Trading(packets, None, None, inventory, containers)
        trading.offers = TradeOffers(2, 42, "Farmer", 1, True, ())
        observed = await trading.stage_ingredient(3, 3)
        self.assertIs(observed, reply)
        sent = packets.send.await_args.args[0].requests[0]
        self.assertEqual(int(sent.client_request_id.raw_id), -1)
        action = sent.actions[0]
        self.assertEqual(int(action.amount), 3)
        self.assertEqual(action.source.full_container_name.name.name,
                         "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER")
        self.assertEqual(action.source.net_id_variant, 23)
        self.assertEqual(action.destination.full_container_name.name.name,
                         "TRADE2_INGREDIENT_1_CONTAINER")
        self.assertEqual(action.destination.slot, 4)
        corrected = inventory.slots[3]
        self.assertEqual((int(corrected.stack_size), int(corrected.net_id_variant)),
                         (2, 55))

        # A stale/closed window must not submit the old trade's input.
        containers.window = None
        with self.assertRaisesRegex(RuntimeError, "trade window"):
            await trading.stage_ingredient(3, 1)
        self.assertEqual(packets.send.await_count, 1)

    async def test_stage_old_trade_uses_legacy_input_container(self):
        b = Protocol(2193).binding
        source = b.SerializedNetworkItemStackDescriptor(
            id=1, stack_size=2, aux_value=0, net_id_variant=3,
            block_runtime_id=0, user_data_buffer=b"",
        )
        reply = SimpleNamespace(
            client_request_id=SimpleNamespace(raw_id=-1),
            result=SimpleNamespace(name="ERROR"), containers=[],
        )
        packets = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=b, decode=lambda raw: SimpleNamespace(responses=[reply]),
            ),
            wait_for_packet=AsyncMock(return_value=RawPacket(b.ItemStackResponsePacket.ID, b"")),
            send=AsyncMock(),
        )
        inventory = item_fixture(packets, {0: source})
        self.addCleanup(inventory.close)
        trading = Trading(packets, None, None, inventory, SimpleNamespace(
            window=SimpleNamespace(
                container_type="TRADE", container_id=4, entity_unique_id=9,
            ),
        ))
        trading.offers = TradeOffers(4, 9, "Trader", 1, False, ())
        self.assertIs(await trading.stage_ingredient(0, 2, ingredient=2), reply)
        action = packets.send.await_args.args[0].requests[0].actions[0]
        self.assertEqual(action.destination.full_container_name.name.name,
                         "TRADE_INGREDIENT_2_CONTAINER")
        self.assertIs(inventory.slots[0], source)

    async def test_unstage_returns_confirmed_trade_input_before_window_close(self):
        b = Protocol(2193).binding
        source = b.SerializedNetworkItemStackDescriptor(
            id=1, stack_size=3, aux_value=7, net_id_variant=23,
            block_runtime_id=0, user_data_buffer=b"original",
        )
        empty = b.SerializedNetworkItemStackDescriptor(
            id=0, stack_size=0, aux_value=0, net_id_variant=None,
            block_runtime_id=0, user_data_buffer=b"",
        )
        slots = {3: empty}
        reply = SimpleNamespace(
            client_request_id=SimpleNamespace(raw_id=-3),
            result=SimpleNamespace(name="SUCCESS"),
            containers=[
                SimpleNamespace(full_container_name=SimpleNamespace(
                    name=b.ContainerEnumName.TRADE2_INGREDIENT_1_CONTAINER,
                ), slots=[SimpleNamespace(slot=4, amount=0, item_stack_net_id=None)]),
                SimpleNamespace(full_container_name=SimpleNamespace(
                    name=b.ContainerEnumName.COMBINED_HOTBAR_AND_INVENTORY_CONTAINER,
                ), slots=[SimpleNamespace(
                    slot=3, amount=3, item_stack_net_id=SimpleNamespace(raw_id=55),
                )]),
            ],
        )
        raw = RawPacket(b.ItemStackResponsePacket.ID, b"unstage")

        async def wait(packet_id, *, timeout, match):
            self.assertEqual(packet_id, b.ItemStackResponsePacket.ID)
            self.assertTrue(match(raw))
            return raw

        packets = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=b, decode=lambda _: SimpleNamespace(responses=[reply]),
            ), wait_for_packet=wait, send=AsyncMock(),
        )
        containers = SimpleNamespace(window=SimpleNamespace(
            container_type="TRADE", container_id=2, entity_unique_id=42,
        ))
        inventory = item_fixture(packets, slots, request_id=-3)
        self.addCleanup(inventory.close)
        trading = Trading(packets, None, None, inventory, containers)
        trading.offers = TradeOffers(2, 42, "Farmer", 1, True, ())
        trading._inputs[1] = ("minecraft:stone", 3, 301, source)

        self.assertIs(await trading.unstage_ingredient(3), reply)
        action = packets.send.await_args.args[0].requests[0].actions[0]
        self.assertEqual((action.amount, action.source.slot,
                          action.source.net_id_variant), (3, 4, 301))
        self.assertEqual(action.source.full_container_name.name.name,
                         "TRADE2_INGREDIENT_1_CONTAINER")
        self.assertEqual((action.destination.slot, action.destination.net_id_variant),
                         (3, 0))
        self.assertEqual(action.destination.full_container_name.name.name,
                         "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER")
        self.assertNotIn(1, trading._inputs)
        self.assertEqual((int(slots[3].id), int(slots[3].stack_size),
                          int(slots[3].net_id_variant)), (1, 3, 55))
        self.assertEqual((int(slots[3].aux_value), slots[3].user_data_buffer),
                         (7, b"original"))
        containers.window = None
        with self.assertRaisesRegex(RuntimeError, "trade window"):
            await trading.unstage_ingredient(3)
        self.assertEqual(packets.send.await_count, 1)

    async def test_trade_exchange_matches_reply_and_uses_confirmed_input_id(self):
        b = Protocol(2193).binding
        empty = b.SerializedNetworkItemStackDescriptor(
            id=0, stack_size=0, aux_value=0, net_id_variant=None,
            block_runtime_id=0, user_data_buffer=b"",
        )
        reply = SimpleNamespace(
            client_request_id=SimpleNamespace(raw_id=-3),
            result=SimpleNamespace(name="SUCCESS"),
            containers=[
                SimpleNamespace(full_container_name=SimpleNamespace(
                    name=b.ContainerEnumName.TRADE2_INGREDIENT_1_CONTAINER,
                ), slots=[SimpleNamespace(slot=4, amount=0, item_stack_net_id=None)]),
                SimpleNamespace(full_container_name=SimpleNamespace(
                    name=b.ContainerEnumName.COMBINED_HOTBAR_AND_INVENTORY_CONTAINER,
                ), slots=[SimpleNamespace(
                    slot=9, amount=1,
                    item_stack_net_id=SimpleNamespace(raw_id=501),
                )]),
            ],
        )
        raw = RawPacket(b.ItemStackResponsePacket.ID, b"exchange")
        packets = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=b, decode=lambda _: SimpleNamespace(responses=[reply]),
            ),
            wait_for_packet=AsyncMock(return_value=raw), send=AsyncMock(),
        )
        inventory = item_fixture(packets, {9: empty}, request_id=-3)
        self.addCleanup(inventory.close)
        trading = Trading(
            packets, None, None, inventory,
            SimpleNamespace(window=SimpleNamespace(
                container_type="TRADE", container_id=2, entity_unique_id=42,
            )), item_names={42: "minecraft:emerald", 43: "minecraft:wheat"},
        )
        trading.offers = TradeOffers(2, 42, "Farmer", 1, True, (
            TradeOffer(123, TradeItem("minecraft:wheat", 3), None,
                       TradeItem("minecraft:emerald", 1), 0, 16),
        ))
        with self.assertRaisesRegex(RuntimeError, "confirmed staged stack"):
            await trading.exchange(123, 9)
        observed_source = b.SerializedNetworkItemStackDescriptor(
            id=43, stack_size=3, aux_value=0, net_id_variant=301,
            block_runtime_id=0, user_data_buffer=b"",
        )
        trading._inputs[1] = ("minecraft:wheat", 3, 301, observed_source)
        self.assertIs(await trading.exchange(123, 9), reply)
        actions = packets.send.await_args.args[0].requests[0].actions
        self.assertEqual(len(actions), 4)
        self.assertEqual((actions[0].recipe_net_id, actions[0].num_crafts), (123, 1))
        self.assertEqual(actions[1].craft_results[0].item_descriptor.full_name,
                         "minecraft:emerald")
        self.assertEqual((actions[2].amount, actions[2].source.slot,
                          actions[2].source.net_id_variant), (3, 4, 301))
        self.assertEqual((actions[3].source.slot, actions[3].source.net_id_variant,
                          actions[3].destination.slot), (50, -3, 9))
        self.assertNotIn(1, trading._inputs)
        self.assertEqual((int(inventory.slots[9].id), int(inventory.slots[9].stack_size),
                          int(inventory.slots[9].net_id_variant)), (42, 1, 501))

        # A full server InventorySlot may have supplied richer item data first.
        observed = b.SerializedNetworkItemStackDescriptor(
            id=42, stack_size=2, aux_value=7, net_id_variant=700,
            block_runtime_id=99, user_data_buffer=b"observed",
        )
        inventory._set_slot(9, observed)
        reply.client_request_id.raw_id = -5
        reply.containers[1].slots[0].amount = 3
        reply.containers[1].slots[0].item_stack_net_id = SimpleNamespace(raw_id=777)
        trading._inputs[1] = ("minecraft:wheat", 3, 301, observed_source)
        await trading.exchange(123, 9)
        corrected = inventory.slots[9]
        self.assertEqual((int(corrected.id), int(corrected.stack_size),
                          int(corrected.net_id_variant)), (42, 3, 777))
        self.assertEqual((int(corrected.aux_value), int(corrected.block_runtime_id),
                          corrected.user_data_buffer), (7, 99, b"observed"))

    async def test_two_input_exchange_consumes_confirmed_ids_from_both_slots(self):
        b = Protocol(2193).binding
        source = b.SerializedNetworkItemStackDescriptor(
            id=43, stack_size=3, aux_value=0, net_id_variant=301,
            block_runtime_id=0, user_data_buffer=b"",
        )
        second = b.SerializedNetworkItemStackDescriptor(
            id=44, stack_size=1, aux_value=0, net_id_variant=302,
            block_runtime_id=0, user_data_buffer=b"",
        )
        empty = b.SerializedNetworkItemStackDescriptor(
            id=0, stack_size=0, aux_value=0, net_id_variant=None,
            block_runtime_id=0, user_data_buffer=b"",
        )
        def container(name, index, amount, net_id=None):
            return SimpleNamespace(
                full_container_name=SimpleNamespace(name=b.ContainerEnumName[name]),
                slots=[SimpleNamespace(
                    slot=index, amount=amount,
                    item_stack_net_id=None if net_id is None else
                    SimpleNamespace(raw_id=net_id),
                )],
            )
        reply = SimpleNamespace(
            client_request_id=SimpleNamespace(raw_id=-5),
            result=SimpleNamespace(name="SUCCESS"),
            containers=[
                container("TRADE2_INGREDIENT_1_CONTAINER", 4, 0),
                container("TRADE2_INGREDIENT_2_CONTAINER", 5, 0),
                container("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 9, 1, 777),
            ],
        )
        packets = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=b, decode=lambda _: SimpleNamespace(responses=[reply]),
            ),
            wait_for_packet=AsyncMock(return_value=RawPacket(b.ItemStackResponsePacket.ID, b"reply")),
            send=AsyncMock(),
        )
        inventory = item_fixture(packets, {9: empty}, request_id=-5)
        self.addCleanup(inventory.close)
        trading = Trading(
            packets, None, None, inventory,
            SimpleNamespace(window=SimpleNamespace(
                container_type="TRADE", container_id=2, entity_unique_id=42,
            )),
            item_names={42: "minecraft:emerald"},
        )
        trading.offers = TradeOffers(2, 42, "Librarian", 1, True, (
            TradeOffer(
                123, TradeItem("minecraft:wheat", 3),
                TradeItem("minecraft:book", 1),
                TradeItem("minecraft:emerald", 1), 0, 16,
            ),
        ))
        trading._inputs[1] = ("minecraft:wheat", 3, 301, source)
        with self.assertRaisesRegex(RuntimeError, "ingredient 2"):
            await trading.exchange(123, 9)
        packets.send.assert_not_awaited()

        trading._inputs[2] = ("minecraft:book", 1, 302, second)
        self.assertIs(await trading.exchange(123, 9), reply)
        request = packets.send.await_args.args[0].requests[0]
        self.assertEqual(int(request.client_request_id.raw_id), -5)
        self.assertEqual(len(request.actions), 5)
        consumes = request.actions[2:4]
        self.assertEqual(
            [(int(a.amount), a.source.full_container_name.name.name,
              int(a.source.slot), int(a.source.net_id_variant)) for a in consumes],
            [(3, "TRADE2_INGREDIENT_1_CONTAINER", 4, 301),
             (1, "TRADE2_INGREDIENT_2_CONTAINER", 5, 302)],
        )
        self.assertEqual((request.actions[4].source.slot,
                          request.actions[4].source.net_id_variant), (50, -5))
        self.assertEqual(trading._inputs, {})
        self.assertEqual((int(inventory.slots[9].id),
                          int(inventory.slots[9].stack_size),
                          int(inventory.slots[9].net_id_variant)), (42, 1, 777))

    async def test_update_trade_opens_and_closes_container_window(self):
        b = Protocol(2193).binding
        observers = []
        replies = {
            b"trade": SimpleNamespace(
                container_id=5, type=b.ContainerType.TRADE, entity_unique_id=42,
            ),
            b"close": SimpleNamespace(container_id=5, server_initiated_close=False),
        }
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: replies[raw.body]),
            subscribe_raw=lambda cb: observers.append(cb) or (lambda: observers.remove(cb)),
        )
        containers = Containers(packets, None, None)
        try:
            observers[0](RawPacket(b.UpdateTradePacket.ID, b"trade"))
            self.assertEqual(containers.window.container_type, "TRADE")
            self.assertEqual(containers.window.entity_unique_id, 42)
            self.assertEqual(containers.window.container_id, 5)
            self.assertIsNone(containers.window.position)
            containers._slots = {0: object()}
            observers[0](RawPacket(b.UpdateTradePacket.ID, b"trade"))
            self.assertEqual(len(containers.slots), 1)
            # ContainerOpen carries an actual position that UpdateTrade lacks.
            known = containers.window
            containers.window = type(known)(5, "TRADE", (2, 100, 0), 42)
            observers[0](RawPacket(b.UpdateTradePacket.ID, b"trade"))
            self.assertEqual(containers.window.position, (2, 100, 0))
            self.assertEqual(len(containers.slots), 1)
            observers[0](RawPacket(b.ContainerClosePacket.ID, b"close"))
            self.assertIsNone(containers.window)
        finally:
            containers.close()
        self.assertEqual(observers, [])

class TradeManyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.codec = Protocol(2225)
        self.b = self.codec.binding
        self.observers = []
        self.decoded = {}
        self.sent = asyncio.Queue()

        async def send(packet):
            await self.sent.put(packet.requests[0])

        def subscribe(observer):
            self.observers.append(observer)
            return lambda: self.observers.remove(observer)

        packets = SimpleNamespace(
            protocol=SimpleNamespace(number=2225, binding=self.b, decode=self.decode),
            send=send, subscribe_raw=subscribe,
        )
        self.inventory = Inventory(packets, 1)
        self.containers = Containers(packets, None, self.inventory)
        self.trading = Trading(packets, None, None, self.inventory, self.containers, {
            1: "minecraft:emerald", 2: "minecraft:book", 3: "minecraft:enchanted_book",
        })
        self.bot = Bot("Trader", protocol=2225)
        self.bot._trading = self.trading
        self.bot._containers = self.containers
        self.addCleanup(self.inventory.close)
        self.addCleanup(self.containers.close)
        self.addCleanup(self.trading.close)
        self.inventory._slots = {index: self.stack(0, 0, None) for index in range(36)}
        self.inventory._set_slot(0, self.stack(1, 36, 100))
        self.inventory._set_slot(1, self.stack(2, 3, 200))
        self.quote()

    def stack(self, item, count, net_id, *, aux=0, block_runtime_id=0, user_data=b""):
        return self.b.SerializedNetworkItemStackDescriptor(
            id=item, stack_size=count, aux_value=aux, net_id_variant=net_id,
            block_runtime_id=block_runtime_id, user_data_buffer=user_data,
        )

    def emit(self, binding_packet, value):
        key = str(len(self.decoded)).encode()
        self.decoded[key] = value
        self.dispatch(RawPacket(binding_packet.ID, key))

    def decode(self, raw):
        return self.decoded[raw.body] if raw.body in self.decoded else self.codec.decode(raw)

    def dispatch(self, raw):
        for observer in tuple(self.observers):
            observer(raw)

    def native_quote(self, *recipes, economy=True, new_screen=True):
        from bedrock_protocol import nbt

        data = nbt.CompoundTag({"Recipes": nbt.ListTag(list(recipes), int(nbt.Type.COMPOUND))})
        packet = self.b.UpdateTradePacket(
            container_id=7, type=self.b.ContainerType.TRADE, size=len(recipes), trader_tier=0,
            entity_unique_id=42, last_trading_player=1, display_name="Librarian",
            use_new_trade_screen=new_screen, using_economy_trade=economy, data=data,
        )
        self.dispatch(self.codec.encode(packet))

    @staticmethod
    def native_recipe(recipe_id):
        from bedrock_protocol import nbt

        def item(name, count):
            return nbt.CompoundTag({"Name": nbt.StringTag(name), "Count": nbt.ByteTag(count)})

        return nbt.CompoundTag({
            "netId": nbt.IntTag(recipe_id), "uses": nbt.IntTag(0), "maxUses": nbt.IntTag(12),
            "buyA": item("minecraft:emerald", 9), "buyB": item("minecraft:book", 1),
            "buyCountA": nbt.IntTag(9), "buyCountB": nbt.IntTag(1),
            "sell": item("minecraft:enchanted_book", 1),
        })

    def hero_price_recipe(self, payable=16):
        from bedrock_protocol import nbt

        recipe = self.native_recipe(123)
        recipe["buyA"] = nbt.CompoundTag({
            "Name": nbt.StringTag("minecraft:carrot"), "Count": nbt.ByteTag(payable),
            "Damage": nbt.ShortTag(32767), "WasPickedUp": nbt.ByteTag(0),
        })
        del recipe["buyB"]
        recipe["buyCountA"] = nbt.IntTag(22)
        recipe["buyCountB"] = nbt.IntTag(0)
        recipe["demand"] = nbt.IntTag(0)
        recipe["priceMultiplierA"] = nbt.FloatTag(0.05)
        recipe["sell"] = nbt.CompoundTag({
            "Name": nbt.StringTag("minecraft:emerald"), "Count": nbt.ByteTag(1),
        })
        return recipe

    def observe_discount_materials(self):
        self.trading.item_names[4] = "minecraft:carrot"
        self.observe_player_slot(0, 4, 16, 100)
        self.observe_player_slot(1, 0, 0, None)

    def pumpkin_quote(self, *, block=None, mode="hashed", name="minecraft:pumpkin", two_inputs=False):
        from bedrock_protocol import nbt

        self.trading.item_names[4] = name
        self.trading._block_id_mode = lambda: mode
        if block is None:
            block = nbt.CompoundTag({
                "name": nbt.StringTag("minecraft:pumpkin"), "version": nbt.IntTag(18168865),
                "states": nbt.CompoundTag({"minecraft:cardinal_direction": nbt.StringTag("south")}),
            })
        recipe = self.native_recipe(123)
        recipe["buyA"] = nbt.CompoundTag({"Name": nbt.StringTag(name), "Count": nbt.ByteTag(6),
                                          "Damage": nbt.ShortTag(32767), "WasPickedUp": nbt.ByteTag(0), "Block": block})
        recipe["buyCountA"] = nbt.IntTag(6)
        if not two_inputs:
            recipe["buyCountB"] = nbt.IntTag(0)
            del recipe["buyB"]
        self.native_quote(recipe)

    def quote(self, *, emeralds=9, uses=0, maximum=12, book_tag=None, base=None, demand=12,
              economy=True, book_base=1, multiplier=0.2):
        from bedrock_protocol import nbt

        buy_b = {"Name": tag("minecraft:book"), "Count": tag(1)}
        if book_tag is not None:
            buy_b["tag"] = book_tag
        recipe = {"netId": tag(123), "uses": tag(uses), "maxUses": tag(maximum),
                  "buyA": {"Name": tag("minecraft:emerald"), "Count": tag(emeralds)}, "buyB": buy_b,
                  "sell": {"Name": tag("minecraft:enchanted_book"), "Count": tag(1)}}
        if base is not None:
            native_buy = {"Name": nbt.StringTag("minecraft:emerald"), "Damage": nbt.ShortTag(32767),
                          "WasPickedUp": nbt.ByteTag(0)}
            if emeralds is not None:
                native_buy["Count"] = nbt.ByteTag(emeralds)
            recipe.update(buyA=nbt.CompoundTag(native_buy),
                          buyCountA=nbt.IntTag(base), buyCountB=nbt.IntTag(book_base),
                          demand=nbt.IntTag(demand), priceMultiplierA=nbt.FloatTag(multiplier))
        self.emit(self.b.UpdateTradePacket, SimpleNamespace(
            container_id=7, entity_unique_id=42, display_name="Librarian",
            trader_tier=0, use_new_trade_screen=True, using_economy_trade=economy, data={"Recipes": [recipe]},
        ))

    def close_window(self):
        self.emit(self.b.ContainerClosePacket, SimpleNamespace(container_id=7, server_initiated_close=False))

    def observe_player_slot(self, index, item, count, net_id, **contents):
        self.emit(self.b.InventorySlotPacket, SimpleNamespace(
            inventory_id=self.b.ContainerID.INVENTORY, slot=index, item=self.stack(item, count, net_id, **contents),
        ))

    def receipt(self, request, rows, verdict="SUCCESS", *, request_id=None):
        reply = SimpleNamespace(
            client_request_id=SimpleNamespace(raw_id=int(request.client_request_id.raw_id)
                                             if request_id is None else request_id),
            result=SimpleNamespace(name=verdict), containers=[
                SimpleNamespace(full_container_name=SimpleNamespace(name=self.b.ContainerEnumName[container]),
                                slots=[SimpleNamespace(slot=slot, amount=count,
                                                       item_stack_net_id=None if net_id is None else
                                                       SimpleNamespace(raw_id=net_id))])
                for container, slot, count, net_id in rows
            ],
        )
        self.emit(self.b.ItemStackResponsePacket, SimpleNamespace(responses=[reply]))

    @staticmethod
    def player_slot(index, amount, net_id=None):
        return "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", index, amount, net_id

    @staticmethod
    def input_slot(ingredient, amount, net_id=None):
        return f"TRADE2_INGREDIENT_{ingredient}_CONTAINER", 3 + ingredient, amount, net_id

    async def next_request(self):
        return await asyncio.wait_for(self.sent.get(), 1)

    async def answer_stage(self, ingredient, *, expected_slot=None):
        request = await self.next_request()
        action, = request.actions
        index = action.source.slot
        stack = self.inventory.slots[index]
        self.assertEqual(index, ingredient - 1 if expected_slot is None else expected_slot)
        remaining = int(stack.stack_size) - int(action.amount)
        self.receipt(request, [
            self.player_slot(index, remaining, int(stack.net_id_variant) if remaining else None),
            self.input_slot(ingredient, int(action.amount), 500 + ingredient),
        ])
        return request

    async def answer_exchange(self):
        request = await self.next_request()
        output = request.actions[-1].destination.slot
        self.receipt(request, [self.input_slot(1, 0), self.input_slot(2, 0), self.player_slot(output, 1, 800 + output)])
        return request

    async def exchange(self, *, missing=None, refresh=None):
        await self.answer_stage(1)
        await self.answer_stage(2)
        request = await self.next_request()
        output = request.actions[-1].destination.slot
        self.assertEqual([int(action.source.net_id_variant) for action in request.actions[2:4]], [501, 502])
        if refresh is not None:
            self.quote(uses=refresh, maximum=2)
        endpoints = [self.input_slot(1, 0), self.input_slot(2, 0), self.player_slot(output, 1, 800 + output)]
        if missing is not None:
            endpoints.pop(missing)
        self.receipt(request, endpoints)
        return request, output

    async def test_repeat_preflights_both_ingredients_and_output(self):
        batch = asyncio.create_task(self.trading.trade_many(123, 4))
        for _ in range(3):
            await self.exchange()
        result = await batch
        self.assertEqual((result.confirmed_count, result.stop_reason), (3, "insufficient_materials"))
        self.assertEqual([step.request_id for step in result.requests], list(range(-1, -19, -2)))
        self.assertTrue(all(step.status == "confirmed" for step in result.requests))
        self.assertTrue(self.sent.empty())
        self.assertEqual(self.inventory.slots[0].stack_size, 9)
        self.assertEqual(self.inventory.slots[1].id, 0)
        self.assertEqual(sum(stack.id == 3 for stack in self.inventory.slots.values()), 3)

    async def test_state_bearing_cost_selects_south_instead_of_identically_named_north(self):
        for mode, north, south in (("hashed", 0x2B072127, 0xB10F1845), ("sequential", 11068, 11066)):
            with self.subTest(mode=mode):
                self.close_window()
                self.pumpkin_quote(mode=mode)
                self.observe_player_slot(0, 4, 6, 310, block_runtime_id=north, user_data=bytes(10))
                self.observe_player_slot(2, 4, 6, 312, block_runtime_id=0, user_data=bytes(10))
                self.observe_player_slot(3, 4, 6, 311, block_runtime_id=south, user_data=bytes(10))
                batch = asyncio.create_task(self.bot.trade_many(123, 1))
                stage = await self.split_stage(1, 3, 6, 311, 0)
                self.receipt(stage, [self.player_slot(3, 0), self.input_slot(1, 6, 501)])
                exchange = await self.next_request()
                (cost,) = exchange.actions[2:-1]
                self.assertEqual((cost.amount, cost.source.net_id_variant), (6, 501))
                output = exchange.actions[-1].destination.slot
                self.receipt(exchange, [self.input_slot(1, 0), self.player_slot(output, 1, 930)])
                self.assertEqual((await batch).confirmed_count, 1)
                self.assertEqual(self.inventory.slots[0].stack_size, 6)

    async def test_north_and_unknown_inventory_states_do_not_pay_a_south_quote(self):
        self.pumpkin_quote()
        self.observe_player_slot(0, 4, 12, 310, block_runtime_id=0x2B072127, user_data=bytes(10))
        self.observe_player_slot(4, 4, 6, 312, aux=1, block_runtime_id=0xB10F1845, user_data=bytes(10))
        self.observe_player_slot(5, 4, 6, 313, block_runtime_id=0xB10F1845, user_data=b"unknown")
        result = await self.bot.trade_many(123, 1)
        self.assertEqual((result.stop_reason, result.requests), ("insufficient_materials", ()))
        self.observe_player_slot(3, 4, 5, 311, block_runtime_id=0, user_data=bytes(10))
        result = await self.bot.trade_many(123, 1)
        self.assertEqual((result.stop_reason, result.requests), ("insufficient_materials", ()))
        for runtime in (0, 0xFFFFFFFE):
            with self.subTest(runtime=runtime):
                self.observe_player_slot(3, 4, 6, 311, block_runtime_id=runtime, user_data=bytes(10))
                result = await self.bot.trade_many(123, 1)
                self.assertEqual((result.stop_reason, result.requests), ("inventory_unobserved", ()))
        self.observe_player_slot(6, 4, 4, 316, block_runtime_id=0xB10F1845, user_data=bytes(10))
        self.observe_player_slot(3, 4, 2, 311, block_runtime_id=0, user_data=bytes(10))
        result = await self.bot.trade_many(123, 1)
        self.assertEqual((result.stop_reason, result.requests), ("inventory_unobserved", ()))
        self.assertTrue(self.sent.empty())

    async def test_sequential_zero_is_valid_for_its_own_block_and_unknown_for_a_different_item(self):
        from bedrock_protocol import nbt

        self.pumpkin_quote(mode="sequential")
        self.observe_player_slot(0, 4, 6, 310, block_runtime_id=0, user_data=bytes(10))
        unknown = await self.bot.trade_many(123, 1)
        self.assertEqual((unknown.stop_reason, unknown.requests), ("inventory_unobserved", ()))
        self.assertTrue(self.sent.empty())

        terracotta = "minecraft:cyan_terracotta"
        self.pumpkin_quote(mode="sequential", name=terracotta, block=nbt.CompoundTag({
            "name": nbt.StringTag(terracotta), "version": nbt.IntTag(18168865), "states": nbt.CompoundTag(),
        }))
        batch = asyncio.create_task(self.bot.trade_many(123, 1))
        stage = await self.split_stage(1, 0, 6, 310, 0)
        self.receipt(stage, [self.player_slot(0, 0), self.input_slot(1, 6, 501)])
        exchange = await self.next_request()
        destination = exchange.actions[-1].destination.slot
        self.receipt(exchange, [self.input_slot(1, 0), self.player_slot(destination, 1, 932)])
        self.assertEqual((await batch).confirmed_count, 1)

    async def test_block_mode_loss_stops_the_whole_quote_before_the_other_ingredient(self):
        self.pumpkin_quote(two_inputs=True)
        self.observe_player_slot(0, 4, 6, 310, block_runtime_id=0xB10F1845, user_data=bytes(10))
        self.trading._block_id_mode = lambda: None
        unavailable = await self.bot.trade_many(123, 1)
        self.assertEqual((unavailable.stop_reason, unavailable.requests), ("unsupported_quote", ()))
        self.assertTrue(self.sent.empty())

        self.trading._block_id_mode = lambda: "hashed"
        batch = asyncio.create_task(self.bot.trade_many(123, 1, timeout=0.15))
        stage = await self.split_stage(1, 0, 6, 310, 0)
        self.trading._block_id_mode = lambda: None
        self.receipt(stage, [self.player_slot(0, 0), self.input_slot(1, 6, 501)])
        result = await batch
        self.assertEqual((result.stop_reason, result.confirmed_count), ("quote_changed", 0))
        self.assertEqual([(step.ingredient, step.status, step.verdict) for step in result.requests],
                         [(1, "confirmed", "SUCCESS")])
        self.assertEqual(self.trading._inputs[1][1:3], (6, 501))
        self.assertTrue(self.sent.empty())

    async def test_verified_block_cost_appends_only_matching_sources(self):
        self.pumpkin_quote()
        self.observe_player_slot(0, 4, 4, 310, block_runtime_id=0xB10F1845, user_data=bytes(10))
        self.observe_player_slot(3, 4, 4, 311, block_runtime_id=0xB10F1845, user_data=bytes(10))
        self.observe_player_slot(4, 4, 6, 312, block_runtime_id=0x2B072127, user_data=bytes(10))
        batch = asyncio.create_task(self.bot.trade_many(123, 1))
        first = await self.split_stage(1, 0, 4, 310, 0)
        self.receipt(first, [self.player_slot(0, 0), self.input_slot(1, 4, 501)])
        append = await self.split_stage(1, 3, 2, 311, 501)
        self.receipt(append, [self.player_slot(3, 2, 311), self.input_slot(1, 6, 502)])
        exchange = await self.next_request()
        (cost,) = exchange.actions[2:-1]
        self.assertEqual((cost.amount, cost.source.net_id_variant), (6, 502))
        output = exchange.actions[-1].destination.slot
        self.receipt(exchange, [self.input_slot(1, 0), self.player_slot(output, 1, 930)])
        self.assertEqual((await batch).confirmed_count, 1)
        self.assertEqual(self.inventory.slots[4].stack_size, 6)

    async def test_unknown_mode_and_unmodeled_block_values_do_not_create_a_trade_plan(self):
        from bedrock_protocol import nbt

        self.pumpkin_quote(mode=None)
        self.observe_player_slot(0, 4, 6, 310, block_runtime_id=11066, user_data=bytes(10))
        result = await self.bot.trade_many(123, 1)
        self.assertEqual((result.stop_reason, result.requests), ("unsupported_quote", ()))
        invalid_blocks = (
            nbt.StringTag("minecraft:pumpkin"),
            nbt.CompoundTag({"name": nbt.StringTag("minecraft:pumpkin"), "version": nbt.IntTag(18168865),
                             "states": nbt.CompoundTag()}),
            nbt.CompoundTag({"name": nbt.StringTag("minecraft:pumpkin"), "version": nbt.StringTag("18168865"),
                             "states": nbt.CompoundTag({"minecraft:cardinal_direction": nbt.StringTag("south")})}),
            nbt.CompoundTag({"name": nbt.StringTag("minecraft:pumpkin"), "version": nbt.IntTag(18168865),
                             "states": nbt.CompoundTag({"minecraft:cardinal_direction": nbt.IntTag(0)})}),
            nbt.CompoundTag({"name": nbt.StringTag("custom:pumpkin"), "version": nbt.IntTag(18168865),
                             "states": nbt.CompoundTag({"minecraft:cardinal_direction": nbt.StringTag("south")})}),
        )
        self.observe_player_slot(0, 4, 6, 310, block_runtime_id=0xB10F1845, user_data=bytes(10))
        for block in invalid_blocks:
            with self.subTest(block=block):
                self.pumpkin_quote(block=block)
                result = await self.bot.trade_many(123, 1)
                self.assertEqual((result.stop_reason, result.requests), ("unsupported_quote", ()))
        self.assertTrue(self.sent.empty())

    async def test_replaced_block_state_is_rechecked_before_staging(self):
        self.pumpkin_quote()
        self.observe_player_slot(0, 4, 6, 310, block_runtime_id=0xB10F1845, user_data=bytes(10))
        plan = self.trading._plan

        def plan_then_replace(*args):
            result = plan(*args)
            self.observe_player_slot(0, 4, 6, 310, block_runtime_id=0x2B072127, user_data=bytes(10))
            return result

        with patch.object(self.trading, "_plan", side_effect=plan_then_replace):
            result = await self.bot.trade_many(123, 1)
        self.assertEqual((result.stop_reason, result.requests), ("inventory_unobserved", ()))
        self.assertTrue(self.sent.empty())

    async def test_full_inventory_stops_before_staging_either_ingredient(self):
        for index in range(2, 36):
            self.inventory._set_slot(index, self.stack(4, 64, 1000 + index))
        self.inventory._set_slot(0, self.stack(1, 9, 100))
        self.inventory._set_slot(1, self.stack(2, 1, 200))
        result = await self.trading.trade_many(123, 1)
        self.assertEqual((result.stop_reason, result.requests), ("no_output_slot", ()))
        self.assertTrue(self.sent.empty())

    def output_quote(self, *, full=False, tag_output=False, output_count=1):
        from bedrock_protocol import nbt

        self.trading.item_names[4] = "minecraft:wheat"
        recipe = self.native_recipe(123)
        recipe["buyA"] = nbt.CompoundTag({"Name": nbt.StringTag("minecraft:wheat"), "Count": nbt.ByteTag(20)})
        recipe["buyCountA"] = nbt.IntTag(20)
        output = {"Name": nbt.StringTag("minecraft:emerald"), "Count": nbt.ByteTag(output_count),
                  "Damage": nbt.ShortTag(0), "WasPickedUp": nbt.ByteTag(0)}
        if tag_output:
            output["tag"] = nbt.CompoundTag({"Custom": nbt.StringTag("different")})
        recipe["sell"] = nbt.CompoundTag(output)
        if full:
            for slot in range(2, 36):
                self.observe_player_slot(slot, 5, 64, 2000 + slot)
        self.observe_player_slot(0, 4, 64, 300)
        self.observe_player_slot(9, 1, 62, 901, user_data=bytes(10))
        self.native_quote(recipe)
        return recipe

    def split_quote(self, second_cost=0):
        from bedrock_protocol import nbt

        self.trading.item_names[4] = "minecraft:carrot"

        def carrot(count):
            return nbt.CompoundTag({"Name": nbt.StringTag("minecraft:carrot"), "Count": nbt.ByteTag(count)})

        recipe = self.native_recipe(123)
        recipe["buyA"], recipe["buyCountA"] = carrot(22), nbt.IntTag(22)
        if second_cost:
            recipe["buyB"] = carrot(second_cost)
        else:
            del recipe["buyB"]
        recipe["buyCountB"] = nbt.IntTag(second_cost)
        self.observe_player_slot(0, 4, 12, 301, user_data=bytes(10))
        self.observe_player_slot(1, 4, 14, 302, user_data=bytes(10))
        self.native_quote(recipe)
        return recipe

    async def split_stage(self, ingredient, index, amount, source_id, target_id):
        request = await self.next_request()
        action, = request.actions
        self.assertEqual((action.source.slot, action.amount, action.source.net_id_variant), (index, amount, source_id))
        self.assertEqual((action.destination.slot, action.destination.net_id_variant), (3 + ingredient, target_id))
        return request

    async def test_split_inputs_reserve_shared_sources_and_consume_the_final_stack_ids(self):
        self.split_quote(second_cost=8)
        self.observe_player_slot(2, 4, 4, 303, user_data=bytes(10))
        batch = asyncio.create_task(self.bot.trade_many(123, 1))
        for ingredient, slot, amount, prior_source, prior_target, remainder, total, next_id in (
                (1, 1, 14, 302, 0, 0, 14, 501), (1, 2, 4, 303, 501, 0, 18, 502),
                (1, 0, 4, 301, 502, 8, 22, 503), (2, 0, 8, 312, 0, 0, 8, 504)):
            req = await self.split_stage(ingredient, slot, amount, prior_source, prior_target)
            self.receipt(req, [self.player_slot(slot, remainder, 312 if remainder else None),
                               self.input_slot(ingredient, total, next_id)])
        exchange = await self.next_request()
        consumes = exchange.actions[2:-1]
        self.assertEqual([(step.amount, step.source.net_id_variant) for step in consumes], [(22, 503), (8, 504)])
        output = exchange.actions[-1].destination.slot
        self.receipt(exchange, [self.input_slot(1, 0), self.input_slot(2, 0), self.player_slot(output, 1, 801)])
        result = await batch
        self.assertEqual((result.confirmed_count, result.stop_reason), (1, "complete"))
        self.assertEqual([step.ingredient for step in result.requests], [1, 1, 1, 2, None])

    async def test_joint_input_shortage_is_distinct_from_incompatible_named_stacks(self):
        from endpilot.item_user_data import read_item_user_data

        self.split_quote(second_cost=8)

        def named(name):
            encoded = name.encode("utf-8")
            data = (b"\xff\xff\x01\x0a\x00\x00\x0a\x07\x00display\x08\x04\x00Name"
                    + len(encoded).to_bytes(2, "little") + encoded + bytes(10))
            self.assertEqual(read_item_user_data(data), {"display": {"Name": name}})
            return data

        def inventory(first, second, count):
            slots = [self.stack(4, 22, 301, user_data=bytes(10)),
                     self.stack(4, 4, 302, user_data=first), self.stack(4, count, 303, user_data=second)]
            slots.extend(self.stack(0, 0, None) for _ in range(33))
            self.dispatch(self.codec.encode(self.b.InventoryContentPacket(inventory_id=self.b.ContainerID.INVENTORY,
                                                                           slots=slots)))
            self.assertEqual(len(self.inventory.slots), 36)

        first, second = named("First carrots"), named("Other carrots")
        inventory(first, second, 4)
        result = await self.bot.trade_many(123, 1)
        self.assertEqual((result.stop_reason, result.requests), ("fragmented_materials", ()))
        self.assertTrue(self.sent.empty())

        inventory(first, second, 3)
        result = await self.bot.trade_many(123, 1)
        self.assertEqual((result.stop_reason, result.requests), ("insufficient_materials", ()))
        self.assertTrue(self.sent.empty())

        inventory(bytes(10), bytes(10), 4)
        batch = asyncio.create_task(self.bot.trade_many(123, 1))
        for ingredient, source, amount, source_id, target_id, total, result_id in (
                (1, 0, 22, 301, 0, 22, 501), (2, 1, 4, 302, 0, 4, 502), (2, 2, 4, 303, 502, 8, 503)):
            stage = await self.split_stage(ingredient, source, amount, source_id, target_id)
            self.receipt(stage, [self.player_slot(source, 0), self.input_slot(ingredient, total, result_id)])
        exchange = await self.next_request()
        self.assertEqual([(action.amount, action.source.net_id_variant) for action in exchange.actions[2:-1]],
                         [(22, 501), (8, 503)])
        output = exchange.actions[-1].destination.slot
        self.receipt(exchange, [self.input_slot(1, 0), self.input_slot(2, 0), self.player_slot(output, 1, 801)])
        done = await batch
        self.assertEqual((done.confirmed_count, done.stop_reason), (1, "complete"))

    async def test_split_quote_prefers_single_source_and_preflights_capacity(self):
        self.split_quote()
        for slot in range(2, 36):
            self.observe_player_slot(slot, 5, 64, 2000 + slot)
        blocked = await self.bot.trade_many(123, 1)
        self.assertEqual((blocked.stop_reason, blocked.requests), ("no_output_slot", ()))
        self.assertTrue(self.sent.empty())
        self.observe_player_slot(3, 4, 22, 304, user_data=bytes(10))
        self.observe_player_slot(4, 0, 0, None)
        batch = asyncio.create_task(self.bot.trade_many(123, 1))
        stage = await self.split_stage(1, 3, 22, 304, 0)
        self.receipt(stage, [self.player_slot(3, 0), self.input_slot(1, 22, 501)])
        exchange = await self.next_request()
        self.receipt(exchange, [self.input_slot(1, 0), self.player_slot(4, 1, 801)])
        self.assertEqual((await batch).confirmed_count, 1)

    async def test_split_append_rejection_preserves_the_confirmed_input_for_explicit_return(self):
        self.split_quote()
        batch = asyncio.create_task(self.bot.trade_many(123, 1))
        first = await self.split_stage(1, 0, 12, 301, 0)
        self.receipt(first, [self.player_slot(0, 0), self.input_slot(1, 12, 501)])
        append = await self.split_stage(1, 1, 10, 302, 501)
        self.receipt(append, [], "ERROR")
        result = await batch
        self.assertEqual((result.stop_reason, result.confirmed_count), ("rejected", 0))
        self.assertEqual([step.status for step in result.requests], ["confirmed", "rejected"])
        self.assertTrue(self.sent.empty())
        refund = asyncio.create_task(self.bot.unstage_trade_ingredient(8))
        request = await self.next_request()
        transfer, = request.actions
        self.assertEqual((transfer.amount, transfer.source.net_id_variant), (12, 501))
        self.receipt(request, [self.input_slot(1, 0), self.player_slot(8, 12, 803)])
        self.assertEqual((await refund).result.name, "SUCCESS")
        self.assertEqual(self.inventory.slots[8].stack_size, 12)

    async def test_split_append_cancellation_retains_pending_result_and_accepts_real_late_amount(self):
        self.split_quote()
        batch = asyncio.create_task(self.bot.trade_many(123, 1))
        first = await self.split_stage(1, 0, 12, 301, 0)
        self.receipt(first, [self.player_slot(0, 0), self.input_slot(1, 12, 501)])
        append = await self.split_stage(1, 1, 10, 302, 501)
        batch.cancel()
        with self.assertRaises(asyncio.CancelledError) as error:
            await batch
        progress = error.exception.trade_progress
        self.assertEqual((progress.stop_reason, progress.confirmed_count), ("cancelled", 0))
        self.assertEqual([step.status for step in progress.requests], ["confirmed", "unconfirmed"])
        self.receipt(append, [self.player_slot(1, 4, 302), self.input_slot(1, 22, 502)])
        self.assertEqual(self.trading._inputs[1][1:3], (22, 502))
        self.assertEqual(progress.requests[-1].status, "unconfirmed")
        self.assertTrue(self.sent.empty())

    async def test_split_does_not_take_over_unexpected_input_or_mix_source_variants(self):
        self.split_quote()
        self.observe_player_slot(1, 4, 14, 302, user_data=b"different")
        blocked = await self.bot.trade_many(123, 1)
        self.assertEqual((blocked.stop_reason, blocked.requests), ("fragmented_materials", ()))
        self.assertTrue(self.sent.empty())
        self.split_quote()
        batch = asyncio.create_task(self.bot.trade_many(123, 1))
        stage = await self.split_stage(1, 0, 12, 301, 0)
        self.receipt(stage, [self.player_slot(0, 0), self.input_slot(1, 13, 501)])
        result = await batch
        self.assertEqual((result.stop_reason, result.confirmed_count, result.requests[-1].verdict),
                         ("unconfirmed", 0, "SUCCESS"))
        self.assertFalse(self.trading._inputs)
        self.assertTrue(self.sent.empty())

    async def test_quote_change_during_split_stage_prevents_later_stages(self):
        recipe = self.split_quote()
        batch = asyncio.create_task(self.bot.trade_many(123, 1))
        stage = await self.split_stage(1, 0, 12, 301, 0)
        from bedrock_protocol import nbt
        recipe["buyA"] = nbt.CompoundTag({"Name": nbt.StringTag("minecraft:carrot"), "Count": nbt.ByteTag(23)})
        recipe["buyCountA"] = nbt.IntTag(23)
        self.native_quote(recipe)
        self.receipt(stage, [self.player_slot(0, 0), self.input_slot(1, 12, 501)])
        result = await batch
        self.assertEqual((result.stop_reason, result.confirmed_count, result.requests[-1].status),
                         ("quote_changed", 0, "confirmed"))
        self.assertTrue(self.sent.empty())

    async def test_replaced_split_source_cannot_be_appended_to_the_staged_input(self):
        self.split_quote()
        batch = asyncio.create_task(self.bot.trade_many(123, 1))
        first = await self.split_stage(1, 0, 12, 301, 0)
        self.observe_player_slot(1, 4, 14, 392, user_data=b"changed")
        self.receipt(first, [self.player_slot(0, 0), self.input_slot(1, 12, 501)])
        result = await batch
        self.assertEqual((result.stop_reason, result.confirmed_count), ("inventory_unobserved", 0))
        self.assertEqual([step.status for step in result.requests], ["confirmed"])
        self.assertEqual(self.trading._inputs[1][1:3], (12, 501))
        self.assertTrue(self.sent.empty())

    async def test_late_split_append_cannot_install_inputs_in_a_reopened_window(self):
        recipe = self.split_quote()
        original = self.containers.window
        batch = asyncio.create_task(self.bot.trade_many(123, 1))
        first = await self.split_stage(1, 0, 12, 301, 0)
        self.receipt(first, [self.player_slot(0, 0), self.input_slot(1, 12, 501)])
        append = await self.split_stage(1, 1, 10, 302, 501)
        self.close_window()
        self.native_quote(recipe)
        self.assertEqual(self.containers.window.container_id, original.container_id)
        self.assertIsNot(self.containers.window, original)
        self.receipt(append, [self.player_slot(1, 4, 302), self.input_slot(1, 22, 502)])
        result = await batch
        self.assertEqual((result.stop_reason, result.confirmed_count), ("window_changed", 0))
        self.assertEqual([step.status for step in result.requests], ["confirmed", "confirmed"])
        self.assertFalse(self.trading._inputs)
        self.assertTrue(self.sent.empty())

    async def test_occupied_output_uses_latest_network_id_and_stops_before_third_stage(self):
        self.output_quote(full=True)
        batch = asyncio.create_task(self.trading.trade_many(123, 3))
        for index in (0, 1):
            await self.answer_stage(1)
            if index == 0:
                self.observe_player_slot(9, 1, 62, 910, user_data=bytes(10))
            await self.answer_stage(2)
            exchange = await self.next_request()
            target = exchange.actions[-1].destination
            self.assertEqual((target.slot, target.net_id_variant), (9, 910 + index))
            self.assertEqual([action.amount for action in exchange.actions[2:-1]], [20, 1])
            self.receipt(exchange, [self.input_slot(1, 0), self.input_slot(2, 0),
                                    self.player_slot(9, 63 + index, 911 + index)])
        result = await batch
        self.assertEqual((result.stop_reason, result.confirmed_count), ("no_output_slot", 2))
        self.assertEqual([(step.trade, step.ingredient) for step in result.requests],
                         [(1, 1), (1, 2), (1, None), (2, 1), (2, 2), (2, None)])
        self.assertEqual((self.inventory.slots[0].stack_size, self.inventory.slots[1].stack_size), (24, 1))
        self.assertEqual((self.inventory.slots[9].stack_size, self.inventory.slots[9].net_id_variant), (64, 912))
        self.assertTrue(self.sent.empty())

    async def test_merge_prefers_compatible_stack_over_empty_and_requires_the_whole_output(self):
        self.output_quote(output_count=2)
        self.observe_player_slot(3, 1, 63, 903, user_data=bytes(10))
        self.observe_player_slot(4, 1, 62, 904, user_data=b"unknown")
        self.observe_player_slot(5, 1, 62, 905, aux=1, user_data=bytes(10))
        self.observe_player_slot(6, 1, 62, 906, block_runtime_id=1, user_data=bytes(10))
        self.observe_player_slot(7, 1, 62, 0, user_data=bytes(10))
        batch = asyncio.create_task(self.trading.trade_many(123, 1))
        await self.answer_stage(1)
        await self.answer_stage(2)
        exchange = await self.next_request()
        self.assertEqual((exchange.actions[-1].destination.slot, exchange.actions[-1].amount), (9, 2))
        self.receipt(exchange, [self.input_slot(1, 0), self.input_slot(2, 0), self.player_slot(9, 64, 950)])
        self.assertEqual((await batch).confirmed_count, 1)

    async def test_unmodeled_output_uses_empty_slot_without_making_it_mergeable(self):
        from bedrock_protocol import nbt

        recipe = self.output_quote(tag_output=True)
        self.observe_player_slot(9, 1, 63, 901, user_data=bytes(10))
        batch = asyncio.create_task(self.trading.trade_many(123, 1))
        await self.answer_stage(1)
        await self.answer_stage(2)
        exchange = await self.next_request()
        self.assertEqual(exchange.actions[-1].destination.slot, 2)
        self.receipt(exchange, [self.input_slot(1, 0), self.input_slot(2, 0), self.player_slot(2, 1, 600)])
        self.assertEqual((await batch).confirmed_count, 1)
        self.assertEqual(self.inventory.slots[9].stack_size, 63)

        # A verdict supplying amount and ID alone did not describe the tagged output's metadata.
        recipe["sell"] = nbt.CompoundTag({"Name": nbt.StringTag("minecraft:emerald"), "Count": nbt.ByteTag(1),
                                          "Damage": nbt.ShortTag(0)})
        for index in range(3, 36):
            self.observe_player_slot(index, 5, 64, 2000 + index)
        self.native_quote(recipe)
        next_trade = await self.trading.trade_many(123, 1)
        self.assertEqual((next_trade.stop_reason, next_trade.requests), ("no_output_slot", ()))
        self.assertEqual(self.inventory.slots[2].stack_size, 1)
        self.assertTrue(self.sent.empty())

    async def test_destination_change_before_next_material_stops_without_sending_it(self):
        self.output_quote(full=True)
        batch = asyncio.create_task(self.trading.trade_many(123, 1))
        stage = await self.next_request()
        self.observe_player_slot(9, 1, 62, 901, user_data=b"unknown")
        self.receipt(stage, [self.player_slot(0, 44, 300), self.input_slot(1, 20, 501)])
        stopped = await batch
        self.assertEqual((stopped.stop_reason, stopped.confirmed_count), ("inventory_unobserved", 0))
        self.assertEqual([(step.ingredient, step.status) for step in stopped.requests],
                         [(1, "confirmed"), (2, "not_sent")])
        self.assertTrue(self.sent.empty())

    async def test_occupied_destination_receipt_requires_exact_net_gain(self):
        self.output_quote(full=True)
        batch = asyncio.create_task(self.trading.trade_many(123, 2))
        await self.answer_stage(1)
        await self.answer_stage(2)
        exchange = await self.next_request()
        self.assertEqual(exchange.actions[-1].destination.slot, 9)
        self.receipt(exchange, [self.input_slot(1, 0), self.input_slot(2, 0), self.player_slot(9, 62, 951)])
        result = await batch
        self.assertEqual((result.stop_reason, result.confirmed_count), ("unconfirmed", 0))
        self.assertEqual(result.requests[-1].verdict, "SUCCESS")
        self.assertTrue(self.sent.empty())

    async def test_ordinary_chest_missing_receipt_blocks_use_of_stale_occupied_output(self):
        recipe = self.output_quote(full=True)
        self.close_window()
        self.emit(self.b.ContainerOpenPacket, SimpleNamespace(
            container_id=2, type=self.b.ContainerType.CONTAINER,
            pos=self.b.BlockPos(x=1, y=2, z=3), entity_unique_id=-1,
        ))
        self.emit(self.b.InventoryContentPacket, SimpleNamespace(
            inventory_id=2, slots=[self.stack(1, 1, 800, user_data=bytes(10))],
        ))
        take = asyncio.create_task(self.bot.take_from_container(0, 9, 1))
        request = await self.next_request()
        self.receipt(request, [("LEVEL_ENTITY_CONTAINER", 0, 0, None)])
        self.assertEqual((await take).result.name, "SUCCESS")
        self.emit(self.b.ContainerClosePacket, SimpleNamespace(container_id=2, server_initiated_close=False))
        self.native_quote(recipe)
        self.assertEqual(self.inventory.slots[9].stack_size, 62)
        blocked = await self.bot.trade_many(123, 1, timeout=0.05)
        self.assertEqual((blocked.stop_reason, blocked.requests), ("inventory_unobserved", ()))
        self.assertTrue(self.sent.empty())

        self.observe_player_slot(9, 1, 63, 902, user_data=bytes(10))
        pending = asyncio.create_task(self.bot.trade_many(123, 1))
        await self.answer_stage(1)
        await self.answer_stage(2)
        exchange = await self.next_request()
        self.assertEqual(exchange.actions[-1].destination.net_id_variant, 902)
        self.receipt(exchange, [self.input_slot(1, 0), self.input_slot(2, 0), self.player_slot(9, 64, 902)])
        self.assertEqual((await pending).confirmed_count, 1)

    async def test_unconfirmed_destination_after_planning_blocks_first_stage_send(self):
        self.output_quote(full=True)
        original_plan = self.trading._plan

        def plan_then_invalidate(*args):
            plan = original_plan(*args)
            self.inventory._unconfirmed_transfer_slots.add(9)
            return plan

        with patch.object(self.trading, "_plan", side_effect=plan_then_invalidate):
            result = await self.bot.trade_many(123, 1)
        self.assertEqual((result.stop_reason, result.confirmed_count), ("inventory_unobserved", 0))
        self.assertEqual([(step.ingredient, step.status) for step in result.requests], [(1, "not_sent")])
        self.assertTrue(self.sent.empty())

    async def test_occupied_destination_preserves_conflicting_observations_after_send(self):
        for item, count, net_id in ((2, 7, 655), (1, 60, 901)):
            with self.subTest(item=item, count=count):
                self.close_window()
                self.output_quote(full=True)
                self.observe_player_slot(1, 2, 3, 200)
                batch = asyncio.create_task(self.bot.trade_many(123, 1))
                await self.answer_stage(1)
                await self.answer_stage(2)
                exchange = await self.next_request()
                self.observe_player_slot(9, item, count, net_id, user_data=bytes(10))
                self.receipt(exchange, [self.input_slot(1, 0), self.input_slot(2, 0), self.player_slot(9, 63, 901)])
                result = await batch
                self.assertEqual((result.stop_reason, result.confirmed_count), ("unconfirmed", 0))
                report = result.requests[-1]
                self.assertEqual((report.request_id, report.status, report.verdict),
                                 (exchange.client_request_id.raw_id, "unconfirmed", "SUCCESS"))
                observed = self.inventory.slots[9]
                self.assertEqual((observed.id, observed.stack_size, observed.net_id_variant), (item, count, net_id))
                self.assertFalse(self.trading._inputs)
                self.assertTrue(self.sent.empty())

    async def test_occupied_destination_accepts_baseline_reobservation_and_matching_early_result(self):
        for observations in (((62, 901),), ((62, 901), (62, 901), (63, 905), (63, 905))):
            with self.subTest(observations=observations):
                self.close_window()
                self.output_quote(full=True)
                self.observe_player_slot(1, 2, 3, 200)
                batch = asyncio.create_task(self.bot.trade_many(123, 1))
                await self.answer_stage(1)
                await self.answer_stage(2)
                exchange = await self.next_request()
                for count, net_id in observations:
                    self.observe_player_slot(9, 1, count, net_id, user_data=bytes(10))
                before = self.inventory.slots[9]
                self.receipt(exchange, [self.input_slot(1, 0), self.input_slot(2, 0), self.player_slot(9, 63, net_id)])
                self.assertEqual((await batch).confirmed_count, 1)
                observed = self.inventory.slots[9]
                self.assertEqual((observed.stack_size, observed.net_id_variant), (63, net_id))
                if count == 63:
                    self.assertIs(observed, before)

    async def test_occupied_destination_return_to_baseline_preserves_latest_observation(self):
        self.output_quote(full=True)
        batch = asyncio.create_task(self.bot.trade_many(123, 1))
        await self.answer_stage(1)
        await self.answer_stage(2)
        exchange = await self.next_request()
        self.observe_player_slot(9, 1, 63, 901, user_data=bytes(10))
        self.observe_player_slot(9, 1, 62, 901, user_data=bytes(10))
        latest = self.inventory.slots[9]
        self.receipt(exchange, [self.input_slot(1, 0), self.input_slot(2, 0), self.player_slot(9, 63, 901)])
        result = await batch
        self.assertEqual((result.stop_reason, result.confirmed_count), ("unconfirmed", 0))
        settled = result.requests[-1]
        self.assertEqual((settled.request_id, settled.status, settled.verdict),
                         (exchange.client_request_id.raw_id, "unconfirmed", "SUCCESS"))
        self.assertIs(self.inventory.slots[9], latest)
        self.assertEqual(latest.stack_size, 62)
        self.assertTrue(self.sent.empty())

    async def test_second_stage_rejection_preserves_confirmed_first_input_and_correction(self):
        batch = asyncio.create_task(self.trading.trade_many(123, 3))
        await self.answer_stage(1)
        request = await self.next_request()
        self.receipt(request, [self.player_slot(1, 2, 220)], "ERROR")
        result = await batch
        self.assertEqual((result.confirmed_count, result.stop_reason), (0, "rejected"))
        self.assertEqual([(step.ingredient, step.status, step.verdict) for step in result.requests],
                         [(1, "confirmed", "SUCCESS"), (2, "rejected", "ERROR")])
        self.assertEqual((self.inventory.slots[1].stack_size, self.inventory.slots[1].net_id_variant), (2, 220))
        self.assertEqual(self.trading._inputs[1][1:3], (9, 501))
        self.assertEqual((await self.trading.trade_many(123, 3)).stop_reason, "existing_inputs")
        self.assertTrue(self.sent.empty())

    async def test_same_window_quote_change_stops_after_current_response(self):
        batch = asyncio.create_task(self.trading.trade_many(123, 2))
        pending = await self.next_request()
        window = self.containers.window
        self.quote(emeralds=10)
        self.assertIs(self.containers.window, window)
        self.receipt(pending, [self.player_slot(0, 27, 100), self.input_slot(1, 9, 501)])
        result = await batch
        self.assertEqual((result.stop_reason, result.confirmed_count), ("quote_changed", 0))
        self.assertEqual([(step.ingredient, step.status) for step in result.requests], [(1, "confirmed")])
        self.assertTrue(self.sent.empty())

    async def test_native_economy_trade_pays_nested_count_and_preserves_old_price_stack(self):
        self.quote(emeralds=61, base=18)
        self.observe_player_slot(0, 1, 18, 101)
        blocked = await self.trading.trade_many(123, 1)
        self.assertEqual((blocked.stop_reason, blocked.requests), ("insufficient_materials", ()))
        self.assertTrue(self.sent.empty())

        self.observe_player_slot(9, 1, 61, 110)
        batch = asyncio.create_task(self.trading.trade_many(123, 1))
        await self.verify_adjusted_exchange()
        self.assertEqual((await batch).confirmed_count, 1)
        self.assertEqual(self.inventory.slots[0].stack_size, 18)
        self.assertEqual(self.inventory.slots[9].stack_size, 0)

    async def verify_adjusted_exchange(self):
        first = await self.answer_stage(1, expected_slot=9)
        second = await self.answer_stage(2)
        self.assertEqual((first.actions[0].amount, second.actions[0].amount), (61, 1))
        reply = await self.answer_exchange()
        self.assertEqual([int(action.amount) for action in reply.actions[2:4]], [61, 1])

    async def answer_discounted_exchange(self, *, reprice=False):
        stage = await self.answer_stage(1)
        self.assertEqual((stage.actions[0].amount, self.inventory.slots[0].stack_size), (16, 0))
        exchange = await self.next_request()
        consume, = (action for action in exchange.actions if isinstance(
            action, self.b.ItemStackRequestCereal.ConsumeActionData
        ))
        self.assertEqual((consume.amount, consume.source.slot, consume.source.net_id_variant), (16, 4, 501))
        if reprice:
            window = self.containers.window
            self.native_quote(self.hero_price_recipe(22))
            self.assertIs(self.containers.window, window)
        output = exchange.actions[-1].destination.slot
        self.receipt(exchange, [self.input_slot(1, 0), self.player_slot(output, 1, 800 + output)])
        return output

    async def test_native_hero_quote_trades_for_payable_amount_when_base_is_unaffordable(self):
        self.observe_discount_materials()
        self.native_quote(self.hero_price_recipe())
        trade = asyncio.create_task(self.bot.trade_many(123, 1))
        output = await self.answer_discounted_exchange()
        result = await trade
        self.assertEqual((result.stop_reason, result.confirmed_count), ("complete", 1))
        self.assertEqual([(step.ingredient, step.status) for step in result.requests], [(1, "confirmed"), (None, "confirmed")])
        self.assertEqual((self.inventory.slots[output].id, self.inventory.slots[output].stack_size), (1, 1))
        self.assertTrue(self.sent.empty())

    async def test_native_hero_quote_reprice_keeps_confirmation_of_already_sent_exchange(self):
        self.observe_discount_materials()
        self.native_quote(self.hero_price_recipe())
        trade = asyncio.create_task(self.bot.trade_many(123, 2))
        await self.answer_discounted_exchange(reprice=True)
        result = await trade
        self.assertEqual((result.stop_reason, result.confirmed_count), ("quote_changed", 1))
        self.assertEqual(len(result.requests), 2)
        self.assertTrue(self.sent.empty())

    async def test_discount_requires_native_economy_fields_and_keeps_other_predicates_strict(self):
        from bedrock_protocol import nbt

        known_second = self.native_recipe(123)["buyB"]
        self.observe_discount_materials()
        for label, changes, flags in (
            ("ordinary screen", {}, {"new_screen": False}),
            ("non-economy", {}, {"economy": False}),
            ("missing demand", {"demand": None}, {}),
            ("short demand", {"demand": nbt.ShortTag(0)}, {}),
            ("negative demand", {"demand": nbt.IntTag(-1)}, {}),
            ("non-native base", {"buyCountA": nbt.ShortTag(22)}, {}),
            ("missing multiplier", {"priceMultiplierA": None}, {}),
            ("invalid multiplier", {"priceMultiplierA": nbt.FloatTag(float("nan"))}, {}),
            ("unknown predicate", {"extraPredicate": nbt.IntTag(1)}, {}),
            ("second price mismatch", {"buyB": known_second, "buyCountB": nbt.IntTag(2)}, {}),
        ):
            with self.subTest(label=label):
                recipe = self.hero_price_recipe()
                for name, replacement in changes.items():
                    if replacement is None:
                        del recipe[name]
                    else:
                        recipe[name] = replacement
                self.native_quote(recipe, **flags)
                result = await self.bot.trade_many(123, 1)
                self.assertEqual((result.stop_reason, result.confirmed_count, result.requests), ("unsupported_quote", 0, ()))
                self.assertTrue(self.sent.empty())

    async def test_price_update_during_sent_exchange_confirms_old_trade_then_replans_only_on_new_call(self):
        self.quote(emeralds=18)
        window = self.containers.window
        old = asyncio.create_task(self.trading.trade_many(123, 2))
        await self.answer_stage(1)
        await self.answer_stage(2)
        current = await self.next_request()
        self.assertEqual([int(action.amount) for action in current.actions[2:4]], [18, 1])
        self.quote(emeralds=61, base=18)
        self.assertIs(self.containers.window, window)
        output = current.actions[-1].destination.slot
        self.receipt(current, [self.input_slot(1, 0), self.input_slot(2, 0), self.player_slot(output, 1, 800 + output)])
        prior = await old
        self.assertEqual((prior.stop_reason, prior.confirmed_count), ("quote_changed", 1))
        self.assertEqual(len(prior.requests), 3)
        self.assertTrue(self.sent.empty())

        self.observe_player_slot(9, 1, 61, 110)
        new = asyncio.create_task(self.trading.trade_many(123, 1))
        await self.verify_adjusted_exchange()
        self.assertEqual((await new).confirmed_count, 1)
        self.assertEqual(self.inventory.slots[0].stack_size, 18)
        self.assertTrue(self.sent.empty())

    async def test_changed_price_after_first_stage_never_mixes_old_and_fresh_ingredients(self):
        self.quote(emeralds=18)
        window = self.containers.window
        old = asyncio.create_task(self.trading.trade_many(123, 2))
        first = await self.next_request()
        self.assertEqual(first.actions[0].amount, 18)
        self.quote(emeralds=61, base=18)
        self.assertIs(self.containers.window, window)
        self.receipt(first, [self.player_slot(0, 18, 100), self.input_slot(1, 18, 501)])
        result = await old
        self.assertEqual((result.stop_reason, result.confirmed_count), ("quote_changed", 0))
        self.assertEqual([(step.ingredient, step.status) for step in result.requests], [(1, "confirmed")])
        self.assertEqual((await self.trading.trade_many(123, 1)).stop_reason, "existing_inputs")
        self.assertTrue(self.sent.empty())
        self.assertEqual(self.trading._inputs[1][1:3], (18, 501))

        refund = asyncio.create_task(self.trading.unstage_ingredient(0))
        request = await self.next_request()
        action, = request.actions
        self.assertEqual((action.amount, action.source.net_id_variant), (18, 501))
        self.receipt(request, [self.input_slot(1, 0), self.player_slot(0, 36, 100)])
        self.assertEqual((await refund).result.name, "SUCCESS")
        self.observe_player_slot(9, 1, 61, 110)
        latest = asyncio.create_task(self.trading.trade_many(123, 1))
        await self.verify_adjusted_exchange()
        self.assertEqual((await latest).confirmed_count, 1)
        self.assertIs(self.containers.window, window)
        self.assertEqual(self.inventory.slots[0].stack_size, 36)

    async def test_unproven_dynamic_shapes_and_special_input_predicates_remain_unsupported(self):
        for fields in ({"economy": False}, {"demand": 0}, {"book_base": 2}, {"multiplier": float("nan")},
                       {"book_tag": {"custom": tag("required")}}, {"emeralds": 0}, {"emeralds": None}):
            with self.subTest(fields=fields):
                self.quote(**({"emeralds": 61, "base": 18} | fields))
                blocked = await self.trading.trade_many(123, 1)
                self.assertEqual((blocked.stop_reason, blocked.requests), ("unsupported_quote", ()))
        self.assertTrue(self.sent.empty())

    async def test_declared_second_price_requires_a_complete_second_descriptor(self):
        from bedrock_protocol import nbt

        for second in (None, nbt.CompoundTag({"Name": nbt.StringTag("minecraft:book")}), nbt.StringTag("book")):
            with self.subTest(second=second):
                recipe = self.native_recipe(123)
                if second is None:
                    del recipe["buyB"]
                else:
                    recipe["buyB"] = second
                self.native_quote(recipe)
                result = await self.trading.trade_many(123, 1, timeout=0.05)
                self.assertEqual((result.stop_reason, result.requests), ("unsupported_quote", ()))
                with self.assertRaisesRegex(ValueError, "no complete server quote"):
                    await self.trading.exchange(123, 3)
                self.assertTrue(self.sent.empty())

    async def test_malformed_cost_invalidates_only_its_quote_and_does_not_erase_inflight_exchange(self):
        from bedrock_protocol import nbt

        single = self.native_recipe(124)
        del single["buyB"]
        single["buyCountB"] = nbt.IntTag(0)
        self.native_quote(self.native_recipe(123), single)
        window = self.containers.window
        pending = asyncio.create_task(self.trading.trade_many(123, 2))
        await self.answer_stage(1)
        await self.answer_stage(2)
        exchange = await self.next_request()
        self.assertEqual(int(exchange.actions[0].recipe_net_id), 123)
        invalid = self.native_recipe(123)
        invalid["buyA"] = nbt.CompoundTag({"Name": nbt.StringTag("minecraft:emerald"),
                                          "Count": nbt.StringTag("unreadable")})
        self.native_quote(invalid, single)
        self.assertIs(self.containers.window, window)
        output = exchange.actions[-1].destination.slot
        self.receipt(exchange, [self.input_slot(1, 0), self.input_slot(2, 0), self.player_slot(output, 1, 801)])
        stopped = await pending
        self.assertEqual((stopped.stop_reason, stopped.confirmed_count), ("quote_changed", 1))
        self.assertEqual(len(stopped.requests), 3)
        fresh = await self.trading.trade_many(123, 1)
        self.assertEqual((fresh.stop_reason, fresh.requests), ("unsupported_quote", ()))
        self.assertTrue(self.sent.empty())

        independent = asyncio.create_task(self.trading.trade_many(124, 1))
        await self.answer_stage(1)
        delivered = await self.next_request()
        self.assertEqual(int(delivered.actions[0].recipe_net_id), 124)
        self.assertEqual([int(action.amount) for action in delivered.actions[2:-1]], [9])
        output = delivered.actions[-1].destination.slot
        self.receipt(delivered, [self.input_slot(1, 0), self.player_slot(output, 1, 802)])
        self.assertEqual((await independent).confirmed_count, 1)

    async def test_quota_updates_do_not_count_the_same_exchange_twice(self):
        self.quote(maximum=2)
        batch = asyncio.create_task(self.trading.trade_many(123, 4))
        await self.exchange(refresh=1)
        await self.exchange()
        result = await batch
        self.assertEqual((result.stop_reason, result.confirmed_count), ("quoted_limit", 2))
        self.assertEqual((await self.trading.trade_many(123, 4)).stop_reason, "quoted_limit")
        self.assertTrue(self.sent.empty())

    async def test_explicit_fresh_quote_allows_another_exchange_after_the_previous_quota(self):
        self.quote(maximum=1)
        original = self.containers.window
        batch = asyncio.create_task(self.trading.trade_many(123, 1))
        await self.exchange()
        self.assertEqual((await batch).confirmed_count, 1)
        self.quote(maximum=1)
        self.assertEqual((await self.trading.trade_many(123, 1)).stop_reason, "quoted_limit")
        self.quote(uses=1, maximum=1)
        self.assertEqual((await self.trading.trade_many(123, 1)).stop_reason, "quoted_limit")
        self.quote(uses=0, maximum=1)
        self.assertIs(self.containers.window, original)
        fresh = asyncio.create_task(self.trading.trade_many(123, 1))
        await self.exchange()
        self.assertEqual((await fresh).confirmed_count, 1)
        self.quote(maximum=1)
        self.assertEqual((await self.trading.trade_many(123, 1)).stop_reason, "quoted_limit")
        self.assertTrue(self.sent.empty())

    async def test_exchange_receipt_arriving_after_quote_reset_does_not_charge_new_quota(self):
        self.quote(maximum=1)
        batch = asyncio.create_task(self.trading.trade_many(123, 1))
        await self.answer_stage(1)
        await self.answer_stage(2)
        request = await self.next_request()
        output = request.actions[-1].destination.slot
        self.quote(uses=1, maximum=1)
        self.quote(maximum=1)
        self.receipt(request, [self.input_slot(1, 0), self.input_slot(2, 0), self.player_slot(output, 1, 701)])
        self.assertEqual((await batch).confirmed_count, 1)
        fresh = asyncio.create_task(self.trading.trade_many(123, 1))
        await self.exchange()
        self.assertEqual((await fresh).confirmed_count, 1)

    async def test_cancelled_exchange_keeps_only_its_own_frozen_progress(self):
        batch = asyncio.create_task(self.trading.trade_many(123, 2))
        await self.answer_stage(1)
        await self.answer_stage(2)
        exchange = await self.next_request()
        batch.cancel()
        with self.assertRaises(asyncio.CancelledError) as cancelled:
            await batch
        result = cancelled.exception.trade_progress
        self.assertEqual((result.stop_reason, result.confirmed_count), ("cancelled", 0))
        self.assertEqual(result.requests[-1].status, "unconfirmed")
        self.assertEqual((await self.trading.trade_many(123, 1)).stop_reason, "trade_busy")
        with self.assertRaisesRegex(RuntimeError, "owns this trade window"):
            await self.trading.exchange(123, 2)
        output = exchange.actions[-1].destination.slot
        self.receipt(exchange, [self.input_slot(1, 0), self.input_slot(2, 0), self.player_slot(output, 1, 700)])
        self.assertEqual(result.requests[-1].status, "unconfirmed")
        self.assertEqual((result.confirmed_count, self.inventory.slots[output].id), (0, 3))
        self.assertEqual(self.trading._inputs, {})

    async def test_reused_window_id_does_not_own_old_pending_ingredient(self):
        batch = asyncio.create_task(self.trading.trade_many(123, 1))
        pending = await self.next_request()
        original = self.containers.window
        self.close_window()
        self.quote()
        self.assertIsNot(self.containers.window, original)
        self.receipt(pending, [self.player_slot(0, 27, 100), self.input_slot(1, 9, 501)])
        result = await batch
        self.assertEqual((result.stop_reason, result.confirmed_count), ("window_changed", 0))
        self.assertEqual(self.inventory.slots[0].stack_size, 27)
        self.assertEqual(self.trading._inputs, {})
        next_batch = asyncio.create_task(self.trading.trade_many(123, 1))
        await self.exchange()
        self.assertEqual((await next_batch).confirmed_count, 1)

    async def test_missing_exchange_endpoint_leaves_corrections_without_counting_trade(self):
        batch = asyncio.create_task(self.trading.trade_many(123, 2))
        _, output = await self.exchange(missing=1)
        result = await batch
        self.assertEqual((result.stop_reason, result.confirmed_count), ("unconfirmed", 0))
        self.assertEqual(result.requests[-1].verdict, "SUCCESS")
        self.assertEqual(self.inventory.slots[output].id, 3)
        self.assertEqual(self.trading._inputs, {})
        self.assertTrue(self.sent.empty())

    async def test_missing_exchange_input_cannot_be_consumed_but_confirmed_input_can_be_returned(self):
        batch = asyncio.create_task(self.trading.trade_many(123, 1))
        await self.answer_stage(1)
        await self.answer_stage(2)
        exchange = await self.next_request()
        output = exchange.actions[-1].destination.slot
        self.receipt(exchange, [self.input_slot(1, 9, 601), self.player_slot(output, 1, 701)])
        result = await batch
        self.assertEqual((result.confirmed_count, result.stop_reason), (0, "unconfirmed"))
        with self.assertRaisesRegex(RuntimeError, "ingredient 2"):
            await self.trading.exchange(123, 3, timeout=0.08)
        with self.assertRaisesRegex(RuntimeError, "confirmed staged stack"):
            await self.trading.unstage_ingredient(4, ingredient=2, timeout=0.08)
        self.assertTrue(self.sent.empty())
        pending = asyncio.create_task(self.trading.unstage_ingredient(4, ingredient=1))
        request = await self.next_request()
        self.assertEqual(request.actions[0].source.net_id_variant, 601)
        self.receipt(request, [self.input_slot(1, 0), self.player_slot(4, 9, 702)])
        self.assertEqual((await pending).result.name, "SUCCESS")
        self.assertEqual((self.inventory.slots[4].id, self.inventory.slots[4].stack_size), (1, 9))

    async def test_late_stage_zero_correction_keeps_a_newly_observed_player_stack(self):
        self.observe_player_slot(0, 1, 9, 100)
        batch = asyncio.create_task(self.trading.trade_many(123, 1, timeout=0.08))
        request = await self.next_request()
        with self.assertRaises(TimeoutError):
            await batch
        self.close_window()
        self.quote()
        self.observe_player_slot(0, 2, 6, 655)
        self.receipt(request, [self.player_slot(0, 0), self.input_slot(1, 9, 501)])
        observed = self.inventory.slots[0]
        self.assertEqual((observed.id, observed.stack_size, observed.net_id_variant), (2, 6, 655))
        self.assertEqual(self.trading._inputs, {})

    async def test_stage_correction_updates_an_equivalent_inventory_reobservation(self):
        batch = asyncio.create_task(self.trading.trade_many(123, 1))
        request = await self.next_request()
        self.observe_player_slot(0, 1, 36, 100)
        self.receipt(request, [self.player_slot(0, 27, 120), self.input_slot(1, 9, 501)])
        second = await self.next_request()
        self.receipt(second, [], "ERROR")
        self.assertEqual((await batch).requests[0].status, "confirmed")
        observed = self.inventory.slots[0]
        self.assertEqual((observed.id, observed.stack_size, observed.net_id_variant), (1, 27, 120))

    async def test_exchange_correction_does_not_remove_unrelated_new_destination_stack(self):
        batch = asyncio.create_task(self.trading.trade_many(123, 1))
        await self.answer_stage(1)
        await self.answer_stage(2)
        request = await self.next_request()
        output = request.actions[-1].destination.slot
        self.observe_player_slot(output, 2, 6, 655)
        self.receipt(request, [self.player_slot(output, 1, 701)], "ERROR")
        self.assertEqual((await batch).stop_reason, "rejected")
        observed = self.inventory.slots[output]
        self.assertEqual((observed.id, observed.stack_size, observed.net_id_variant), (2, 6, 655))

    async def test_unstage_correction_updates_new_observation_only_when_its_id_matches(self):
        staging = asyncio.create_task(self.trading.stage_ingredient(0, 9))
        await self.answer_stage(1)
        await staging
        pending = asyncio.create_task(self.trading.unstage_ingredient(4))
        request = await self.next_request()
        self.observe_player_slot(4, 1, 7, 601)
        self.receipt(request, [self.input_slot(1, 0), self.player_slot(4, 9, 601)])
        self.assertEqual((await pending).result.name, "SUCCESS")
        observed = self.inventory.slots[4]
        self.assertEqual((observed.id, observed.stack_size, observed.net_id_variant), (1, 9, 601))

    async def test_timeout_stops_further_requests_but_leaves_late_stage_dispatch(self):
        batch = asyncio.create_task(self.trading.trade_many(123, 1, timeout=0.08))
        request = await self.next_request()
        self.receipt(request, [], request_id=123456)
        with self.assertRaises(TimeoutError) as timeout:
            await batch
        result = timeout.exception.trade_progress
        self.assertEqual((result.stop_reason, result.confirmed_count), ("timeout", 0))
        self.assertEqual(result.requests[-1].status, "unconfirmed")
        self.receipt(request, [self.player_slot(0, 27, 100), self.input_slot(1, 9, 501)])
        self.assertEqual(self.trading._inputs[1][2], 501)
        self.assertEqual(result.requests[-1].status, "unconfirmed")
        self.assertTrue(self.sent.empty())

    async def test_unmodeled_ingredient_constraint_never_matches_by_name_only(self):
        self.quote(book_tag={"unmodeled": tag("custom")})
        result = await self.trading.trade_many(123, 1)
        self.assertEqual((result.stop_reason, result.requests), ("unsupported_quote", ()))
        self.assertTrue(self.sent.empty())
