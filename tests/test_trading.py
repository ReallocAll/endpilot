import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from endpilot.containers import Containers
from endpilot.protocol import Protocol, RawPacket
from endpilot.trading import TradeItem, TradeOffers, Trading


def tag(value):
    return SimpleNamespace(value=value)


class TradingTests(unittest.IsolatedAsyncioTestCase):
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
        inventory = SimpleNamespace(
            slots={3: source}, _allocate_request_id=Mock(return_value=-1),
            item_or_empty=Mock(return_value=source), _set_slot=Mock(),
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
        self.assertEqual(action.destination.slot, 0)
        corrected = inventory._set_slot.call_args.args[1]
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
        inventory = SimpleNamespace(
            slots={0: source}, _allocate_request_id=Mock(return_value=-1),
            item_or_empty=Mock(return_value=source), _set_slot=Mock(),
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
        inventory._set_slot.assert_not_called()

    async def test_update_trade_opens_and_closes_container_window(self):
        b = Protocol(2193).binding
        observers = []
        replies = {
            b"trade": SimpleNamespace(
                container_id=5, type=b.ContainerType.TRADE, entity_unique_id=42,
            ),
            b"close": SimpleNamespace(container_id=5),
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
