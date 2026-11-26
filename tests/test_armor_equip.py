import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from item_response_stream import feed_item_replies
from test_scheduled_input import Wire

from endpilot import Bot
from endpilot.compression import PacketConnection
from endpilot.inventory import Inventory
from endpilot.protocol import Protocol, RawPacket


def item(b, item_id, count, net_id=None):
    return b.SerializedNetworkItemStackDescriptor(
        id=item_id, stack_size=count, aux_value=0,
        net_id_variant=net_id, block_runtime_id=0, user_data_buffer=b"",
    )


def corrected_slot(slot, amount, net_id=None):
    return SimpleNamespace(
        slot=slot, amount=amount,
        item_stack_net_id=None if net_id is None else SimpleNamespace(raw_id=net_id),
    )


def response_container(name, *slots):
    return SimpleNamespace(
        full_container_name=SimpleNamespace(name=SimpleNamespace(name=name)),
        slots=slots,
    )


class ArmorEquipTests(unittest.IsolatedAsyncioTestCase):
    async def test_swap_tracks_matching_server_response_and_slots(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        reply = SimpleNamespace(
            client_request_id=SimpleNamespace(raw_id=-1),
            result=SimpleNamespace(name="SUCCESS"),
            containers=[
                response_container("ARMOR_CONTAINER", corrected_slot(0, 1, 102)),
                response_container(
                    "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                    corrected_slot(2, 0),
                ),
            ],
        )
        raw = RawPacket(b.ItemStackResponsePacket.ID, b"reply")
        stream = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=b, decode=lambda packet:
                SimpleNamespace(responses=[reply]) if packet.body == b"reply"
                else codec.decode(packet),
            ),
            subscribe_raw=lambda cb: callbacks.append(cb)
            or (lambda: callbacks.remove(cb)),
            send=AsyncMock(),
        )

        async def wait(packet_id, *, timeout, match):
            self.assertEqual(packet_id, b.ItemStackResponsePacket.ID)
            self.assertEqual(timeout, 4)
            self.assertTrue(match(raw))
            return raw

        stream.wait_for_packet = wait
        inventory = Inventory(stream, 7)
        feed_item_replies(inventory, timeout=4)
        try:
            inventory._set_slot(2, item(b, 42, 1, 101))
            result = await inventory.equip_armor(2, 0, timeout=4)
            self.assertIs(result, reply)
            sent = codec.decode(codec.encode(stream.send.await_args.args[0]))
            self.assertIsInstance(sent, b.ItemStackRequestPacket)
            request = sent.requests[0]
            self.assertEqual(int(request.client_request_id.raw_id), -1)
            action = request.actions[0]
            self.assertIsInstance(action, b.ItemStackRequestCereal.PlaceActionData)
            self.assertEqual(int(action.amount), 1)
            self.assertEqual(action.source.full_container_name.name,
                             b.ContainerEnumName.COMBINED_HOTBAR_AND_INVENTORY_CONTAINER)
            self.assertEqual(action.destination.full_container_name.name,
                             b.ContainerEnumName.ARMOR_CONTAINER)
            self.assertEqual(int(action.source.slot), 2)
            self.assertEqual(int(action.destination.slot), 0)
            self.assertEqual(int(action.source.net_id_variant), 101)
            self.assertEqual(int(action.destination.net_id_variant), 0)
            self.assertEqual(int(inventory.armor_slots[0].id), 42)
            self.assertEqual(int(inventory.armor_slots[0].net_id_variant), 102)
            self.assertEqual(int(inventory.item_or_empty(2).id), 0)
            self.assertEqual(inventory._allocate_request_id(), -3)
        finally:
            inventory.close()
        self.assertEqual(callbacks, [])

    async def test_rejection_preserves_item_identity_and_applies_corrections(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        reply = SimpleNamespace(
            client_request_id=SimpleNamespace(raw_id=-1),
            result=SimpleNamespace(name="ERROR"),
            containers=[
                response_container("ARMOR_CONTAINER", corrected_slot(0, 1, 55)),
                response_container("INVENTORY_CONTAINER", corrected_slot(2, 1, 44)),
            ],
        )
        raw = RawPacket(b.ItemStackResponsePacket.ID, b"reply")
        stream = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=b, decode=lambda packet:
                SimpleNamespace(responses=[reply]) if packet.body == b"reply"
                else codec.decode(packet),
            ),
            subscribe_raw=lambda cb: callbacks.append(cb)
            or (lambda: callbacks.remove(cb)),
            send=AsyncMock(),
            wait_for_packet=AsyncMock(return_value=raw),
        )
        inventory = Inventory(stream, 7)
        feed_item_replies(inventory)
        try:
            inventory._set_slot(2, item(b, 42, 1, 44))
            inventory._armor_slots[0] = item(b, 50, 1, 55)
            result = await inventory.equip_armor(2, 0)
            self.assertIs(result, reply)
            self.assertEqual(int(inventory.armor_slots[0].id), 50)
            self.assertEqual(int(inventory.item_or_empty(2).id), 42)
            self.assertEqual(int(inventory.armor_slots[0].net_id_variant), 55)
            sent = codec.decode(codec.encode(stream.send.await_args.args[0]))
            self.assertIsInstance(
                sent.requests[0].actions[0], b.ItemStackRequestCereal.SwapActionData,
            )
        finally:
            inventory.close()

    async def test_rejected_unknown_armor_correction_does_not_invent_item(self):
        codec = Protocol(2193)
        b = codec.binding
        reply = SimpleNamespace(
            client_request_id=SimpleNamespace(raw_id=-1),
            result=SimpleNamespace(name="ERROR"),
            containers=[response_container(
                "ARMOR_CONTAINER", corrected_slot(0, 1, 55),
            )],
        )
        raw = RawPacket(b.ItemStackResponsePacket.ID, b"reply")
        callbacks = []
        stream = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=b, decode=lambda packet:
                SimpleNamespace(responses=[reply]) if packet.body == b"reply"
                else codec.decode(packet),
            ),
            subscribe_raw=lambda cb: callbacks.append(cb)
            or (lambda: callbacks.remove(cb)),
            send=AsyncMock(),
            wait_for_packet=AsyncMock(return_value=raw),
        )
        inventory = Inventory(stream, 7)
        feed_item_replies(inventory)
        try:
            inventory._set_slot(2, item(b, 42, 1, 44))
            result = await inventory.equip_armor(2, 0)
            self.assertEqual(result.result.name, "ERROR")
            self.assertNotIn(0, inventory.armor_slots)
            self.assertEqual(int(inventory.item_or_empty(2).id), 42)
            self.assertEqual(int(inventory.item_or_empty(2).stack_size), 1)
        finally:
            inventory.close()
        self.assertEqual(callbacks, [])

    async def test_unequip_into_empty_inventory_uses_take_and_server_slots(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        reply = SimpleNamespace(
            client_request_id=SimpleNamespace(raw_id=-1),
            result=SimpleNamespace(name="SUCCESS"),
            containers=[
                response_container("ARMOR_CONTAINER", corrected_slot(0, 0)),
                response_container(
                    "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                    corrected_slot(2, 1, 103),
                ),
            ],
        )
        raw = RawPacket(b.ItemStackResponsePacket.ID, b"reply")
        stream = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=b, decode=lambda packet:
                SimpleNamespace(responses=[reply]) if packet.body == b"reply"
                else codec.decode(packet),
            ),
            subscribe_raw=lambda cb: callbacks.append(cb)
            or (lambda: callbacks.remove(cb)),
            send=AsyncMock(),
            wait_for_packet=AsyncMock(return_value=raw),
        )
        inventory = Inventory(stream, 7)
        feed_item_replies(inventory)
        try:
            inventory._armor_slots[0] = item(b, 42, 1, 102)
            response = await inventory.unequip_armor(0, 2)
            self.assertIs(response, reply)
            sent = codec.decode(codec.encode(stream.send.await_args.args[0]))
            request = sent.requests[0]
            action = request.actions[0]
            self.assertEqual(int(request.client_request_id.raw_id), -1)
            self.assertIsInstance(action, b.ItemStackRequestCereal.TakeActionData)
            self.assertEqual(int(action.amount), 1)
            self.assertEqual(action.source.full_container_name.name, b.ContainerEnumName.ARMOR_CONTAINER)
            self.assertEqual(action.destination.full_container_name.name,
                             b.ContainerEnumName.COMBINED_HOTBAR_AND_INVENTORY_CONTAINER)
            self.assertEqual(int(action.source.slot), 0)
            self.assertEqual(int(action.destination.slot), 2)
            self.assertEqual(int(action.source.net_id_variant), 102)
            self.assertEqual(int(action.destination.net_id_variant), 0)
            self.assertEqual(int(inventory.armor_slots[0].id), 0)
            self.assertEqual(int(inventory.item_or_empty(2).id), 42)
            self.assertEqual(int(inventory.item_or_empty(2).net_id_variant), 103)
            self.assertEqual(inventory._allocate_request_id(), -3)
        finally:
            inventory.close()
        self.assertEqual(callbacks, [])

    async def test_unequip_occupied_destination_swaps_and_rejection_preserves_items(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        replies = [
            SimpleNamespace(
                client_request_id=SimpleNamespace(raw_id=-1),
                result=SimpleNamespace(name="SUCCESS"),
                containers=[
                    response_container("ARMOR_CONTAINER", corrected_slot(0, 1, 201)),
                    response_container("INVENTORY_CONTAINER", corrected_slot(2, 1, 202)),
                ],
            ),
            SimpleNamespace(
                client_request_id=SimpleNamespace(raw_id=-1),
                result=SimpleNamespace(name="ERROR"),
                containers=[
                    response_container("ARMOR_CONTAINER", corrected_slot(0, 1, 102)),
                    response_container("INVENTORY_CONTAINER", corrected_slot(2, 1, 200)),
                ],
            ),
        ]
        for reply in replies:
            with self.subTest(result=reply.result.name):
                raw = RawPacket(b.ItemStackResponsePacket.ID, b"reply")
                stream = SimpleNamespace(
                    protocol=SimpleNamespace(
                        binding=b, decode=lambda packet:
                        SimpleNamespace(responses=[reply]) if packet.body == b"reply"
                        else codec.decode(packet),
                    ),
                    subscribe_raw=lambda cb: callbacks.append(cb)
                    or (lambda: callbacks.remove(cb)),
                    send=AsyncMock(),
                    wait_for_packet=AsyncMock(return_value=raw),
                )
                inventory = Inventory(stream, 7)
                bot = Bot("ArmorSwapReplay", protocol=2193)
                bot._inventory = inventory
                feed_item_replies(inventory)
                try:
                    inventory._armor_slots[0] = item(b, 42, 1, 102)
                    inventory._set_slot(2, item(b, 50, 1, 200))
                    self.assertIs(await bot.unequip_armor("head", destination_slot=2), reply)
                    sent = codec.decode(codec.encode(stream.send.await_args.args[0]))
                    action = sent.requests[0].actions[0]
                    self.assertIsInstance(action, b.ItemStackRequestCereal.SwapActionData)
                    self.assertEqual(int(action.source.net_id_variant), 102)
                    self.assertEqual(int(action.destination.net_id_variant), 200)
                    if reply.result.name == "SUCCESS":
                        self.assertEqual(int(inventory.armor_slots[0].id), 50)
                        self.assertEqual(int(inventory.armor_slots[0].net_id_variant), 201)
                        self.assertEqual(int(inventory.item_or_empty(2).id), 42)
                        self.assertEqual(int(inventory.item_or_empty(2).net_id_variant), 202)
                    else:
                        self.assertEqual(int(inventory.armor_slots[0].id), 42)
                        self.assertEqual(int(inventory.item_or_empty(2).id), 50)
                finally:
                    inventory.close()
        self.assertEqual(callbacks, [])

    async def test_server_armor_updates_and_minimal_validations(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        messages = {
            b"content": SimpleNamespace(
                inventory_id=b.ContainerID.ARMOR,
                slots=[item(b, 99, 1), item(b, 0, 0)],
            ),
            b"slot": SimpleNamespace(
                inventory_id=b.ContainerID.ARMOR, slot=0, item=item(b, 100, 1),
            ),
            b"transaction": SimpleNamespace(variant_transaction=SimpleNamespace(
                transaction=SimpleNamespace(actions=[SimpleNamespace(
                    source=SimpleNamespace(
                        type=SimpleNamespace(name="CONTAINER_INVENTORY"),
                        container_id=b.ContainerID.ARMOR,
                    ),
                    slot=0, to_item_descriptor=item(b, 101, 1),
                )]),
            )),
        }
        stream = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: messages[raw.body]),
            subscribe_raw=lambda cb: callbacks.append(cb)
            or (lambda: callbacks.remove(cb)),
            send=AsyncMock(),
        )
        inventory = Inventory(stream, 7)
        try:
            callback = callbacks[0]
            callback(RawPacket(b.InventoryContentPacket.ID, b"content"))
            self.assertEqual(int(inventory.armor_slots[0].id), 99)
            callback(RawPacket(b.InventorySlotPacket.ID, b"slot"))
            self.assertEqual(int(inventory.armor_slots[0].id), 100)
            callback(RawPacket(b.InventoryTransactionPacket.ID, b"transaction"))
            self.assertEqual(int(inventory.armor_slots[0].id), 101)
            snapshot = inventory.armor_slots
            snapshot.clear()
            self.assertEqual(len(inventory.armor_slots), 2)
            inventory_snapshot = inventory.slots
            inventory_snapshot[3] = item(b, 42, 1)
            self.assertEqual(inventory.slots, {})
            for src, dst in ((-1, 0), (256, 0), (0, -1), (0, 4)):
                with self.assertRaises(ValueError):
                    await inventory.equip_armor(src, dst)
            with self.assertRaises(ValueError):
                await inventory.equip_armor(0, 0)
            for armor, dst in ((-1, 2), (4, 2), (0, -1), (0, 256), (1, 2)):
                with self.assertRaises(ValueError):
                    await inventory.unequip_armor(armor, dst)
            stream.send.assert_not_awaited()
        finally:
            inventory.close()


class AutomaticArmorUnequipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.codec = Protocol(2225)
        self.b = self.codec.binding
        self.wire = Wire(self.codec)
        settings = self.b.NetworkSettingsPacket(compression_algorithm=self.b.PacketCompressionAlgorithm.NONE)
        self.packets = PacketConnection(self.wire, self.codec, settings)
        self.inventory = Inventory(self.packets, 7)
        self.bot = Bot("ArmorUnequipReplay", protocol=2225)
        self.bot._inventory = self.inventory
        self.addAsyncCleanup(self.packets.close)
        self.addCleanup(self.inventory.close)

    def observe(self, *packets):
        for packet in packets:
            self.inventory._on_packet(self.codec.encode(packet))

    def player_slot(self, index, item_id=0, count=0, net_id=None):
        return self.b.InventorySlotPacket(
            inventory_id=self.b.ContainerID.INVENTORY, slot=index, item=item(self.b, item_id, count, net_id),
        )

    def armor_slot(self, index, item_id=42, count=1, net_id=101):
        return self.b.InventorySlotPacket(
            inventory_id=self.b.ContainerID.ARMOR, slot=index, item=item(self.b, item_id, count, net_id),
        )

    def player_content(self, size=36, *, empty=None):
        return self.b.InventoryContentPacket(
            inventory_id=self.b.ContainerID.INVENTORY,
            slots=[item(self.b, 0, 0) if index == empty else item(self.b, 42, 1, 200 + index)
                   for index in range(size)],
        )

    async def sent_request(self, packet_index=0):
        async with asyncio.timeout(1):
            while len(self.wire.sent) <= packet_index:
                await asyncio.sleep(0)
        request, = self.wire.sent[packet_index].requests
        return request

    def success(self, request, armor_slot, destination_slot):
        b = self.b
        self.observe(b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request.client_request_id, result=b.ItemStackNetResult.SUCCESS,
            containers=[
                b.ItemStackResponseContainerInfo(
                    full_container_name=b.FullContainerName(name=b.ContainerEnumName[name]),
                    slots=[b.ItemStackResponseSlotInfo(
                        requested_slot=index, slot=index, amount=amount,
                        item_stack_net_id=None if net_id is None else b.ItemStackNetId(raw_id=net_id),
                    )],
                )
                for name, index, amount, net_id in (
                    ("ARMOR_CONTAINER", armor_slot, 0, None),
                    ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", destination_slot, 1, 401),
                )
            ],
        )]))

    async def test_one_take_into_the_known_empty_non_hotbar_slot(self):
        for index, slot in enumerate(("head", "chest", "legs", "feet")):
            with self.subTest(slot=slot):
                self.wire.sent.clear()
                # The head case has 35 occupied slots. Other cases have only one observed slot.
                self.observe(self.player_content(36 if index == 0 else 0, empty=22),
                             self.player_slot(22), self.armor_slot(index))
                task = asyncio.create_task(self.bot.unequip_armor(slot))
                request = await self.sent_request()
                action, = request.actions
                self.assertIsInstance(action, self.b.ItemStackRequestCereal.TakeActionData)
                self.assertEqual((action.amount, action.source.slot, action.source.net_id_variant), (1, index, 101))
                self.assertEqual(action.source.full_container_name.name, self.b.ContainerEnumName.ARMOR_CONTAINER)
                self.assertEqual((action.destination.slot, action.destination.net_id_variant), (22, 0))
                self.assertEqual(action.destination.full_container_name.name,
                                 self.b.ContainerEnumName.COMBINED_HOTBAR_AND_INVENTORY_CONTAINER)
                self.assertEqual(self.bot.armor_slots[index].id, 42)
                self.assertEqual(self.bot.inventory_slots[22].id, 0)
                self.observe(self.player_slot(22), self.armor_slot(index))
                self.success(request, index, 22)
                response = await task
                self.assertEqual(response.result.name, "SUCCESS")
                self.assertEqual(response.client_request_id.raw_id, request.client_request_id.raw_id)
                armor = self.bot.armor_slots[index]
                self.assertEqual((armor.id, armor.stack_size), (0, 0))
                stowed = self.bot.inventory_slots[22]
                self.assertEqual((stowed.id, stowed.stack_size, stowed.net_id_variant), (42, 1, 401))
                self.assertEqual(len(self.wire.sent), 1)

    async def test_reports_full_and_unconfirmed_inventory_without_sending(self):
        self.observe(self.armor_slot(0), self.player_content())
        with self.assertRaisesRegex(RuntimeError, "main inventory is full"):
            await self.bot.unequip_armor(0)
        self.observe(self.player_content(35), self.player_slot(36))
        with self.assertRaisesRegex(RuntimeError, "no empty main inventory slot is confirmed"):
            await self.bot.unequip_armor(0)
        self.assertEqual(self.bot.armor_slots[0].net_id_variant, 101)
        self.assertEqual(self.wire.sent, [])

    async def test_reports_unobserved_empty_or_incomplete_armor_without_sending(self):
        self.observe(self.player_slot(22))
        with self.assertRaisesRegex(RuntimeError, "armor slot is unobserved"):
            await self.bot.unequip_armor("head")
        self.observe(self.armor_slot(0, 0, 0, None))
        with self.assertRaisesRegex(ValueError, "armor slot has no observed item"):
            await self.bot.unequip_armor("head")
        for count, net_id in ((1, None), (2, 101)):
            self.observe(self.armor_slot(0, 42, count, net_id))
            with self.assertRaisesRegex(RuntimeError, "no confirmed identity or single-item amount"):
                await self.bot.unequip_armor("head")
        self.assertEqual(self.wire.sent, [])

    async def test_rechecks_armor_identity_and_destination_at_wire_submission(self):
        send = self.packets.send
        for update in (self.armor_slot(0, net_id=117), self.player_slot(22, 42, 1, 209)):
            with self.subTest(container=update.inventory_id):
                self.observe(self.armor_slot(0), self.player_slot(22), self.player_slot(23))

                async def send_with_update(packet, *, _before_send=None):
                    self.observe(update)
                    await send(packet, _before_send=_before_send)

                with patch.object(self.packets, "send", side_effect=send_with_update):
                    with self.assertRaisesRegex(RuntimeError, "changed before submission"):
                        await self.bot.unequip_armor("head")
                self.assertEqual(self.bot.armor_slots[0].id, 42)
                self.assertEqual(self.wire.sent, [])

    async def test_avoids_armor_and_inventory_slots_with_requests_in_flight(self):
        self.observe(self.armor_slot(0), self.armor_slot(1, net_id=102), self.player_slot(22), self.player_slot(23))
        explicit = asyncio.create_task(self.bot.unequip_armor(0, 22))
        first = await self.sent_request()
        with self.assertRaisesRegex(RuntimeError, "item-stack request in flight"):
            await self.bot.unequip_armor("head")
        automatic = asyncio.create_task(self.bot.unequip_armor("chest"))
        second = await self.sent_request(1)
        self.assertEqual(second.actions[0].destination.slot, 23)
        self.success(first, 0, 22)
        self.success(second, 1, 23)
        self.assertEqual((await explicit).result.name, "SUCCESS")
        self.assertEqual((await automatic).result.name, "SUCCESS")
        self.assertEqual(len(self.wire.sent), 2)

    async def test_matching_response_preserves_newer_observations(self):
        self.observe(self.armor_slot(0), self.player_slot(22))
        task = asyncio.create_task(self.bot.unequip_armor("head"))
        request = await self.sent_request()
        self.observe(self.armor_slot(0, net_id=117), self.player_slot(22, 42, 1, 209))
        self.success(request, 0, 22)
        self.assertEqual((await task).result.name, "SUCCESS")
        self.assertEqual(self.bot.armor_slots[0].net_id_variant, 117)
        self.assertEqual(self.bot.inventory_slots[22].net_id_variant, 209)

    async def test_matching_response_preserves_each_slot_that_changed_and_restored(self):
        for changed in ("inventory", "armor"):
            with self.subTest(changed=changed):
                self.wire.sent.clear()
                self.observe(self.armor_slot(0), self.player_slot(22))
                task = asyncio.create_task(self.bot.unequip_armor("head"))
                request = await self.sent_request()
                if changed == "inventory":
                    self.observe(self.player_slot(22, 42, 1, 209), self.player_slot(22))
                else:
                    self.observe(self.armor_slot(0, 0, 0, None), self.armor_slot(0))
                self.success(request, 0, 22)
                response = await task
                self.assertEqual(response.result.name, "SUCCESS")
                self.assertEqual(response.client_request_id.raw_id, request.client_request_id.raw_id)
                self.assertEqual(self.bot.armor_slots[0].id, 42 if changed == "armor" else 0)
                self.assertEqual(self.bot.inventory_slots[22].id, 0 if changed == "inventory" else 42)

    async def test_matching_response_preserves_slots_omitted_and_restored_by_content(self):
        self.observe(self.armor_slot(0), self.player_slot(22))
        task = asyncio.create_task(self.bot.unequip_armor("head"))
        request = await self.sent_request()
        self.observe(self.b.InventoryContentPacket(inventory_id=self.b.ContainerID.ARMOR, slots=[]),
                     self.player_content(22))
        self.assertNotIn(0, self.bot.armor_slots)
        self.assertNotIn(22, self.bot.inventory_slots)
        self.observe(self.armor_slot(0), self.player_slot(22))
        self.success(request, 0, 22)
        self.assertEqual((await task).result.name, "SUCCESS")
        self.assertEqual(self.bot.armor_slots[0].net_id_variant, 101)
        self.assertEqual(self.bot.inventory_slots[22].id, 0)

    async def test_in_flight_automatic_request_blocks_only_overlapping_explicit_armor_actions(self):
        self.observe(self.armor_slot(0), self.armor_slot(1, net_id=102), self.player_slot(22),
                     self.player_slot(23, 50, 1, 207), self.player_slot(24))
        automatic = asyncio.create_task(self.bot.unequip_armor("head"))
        first = await self.sent_request()
        self.assertEqual(first.actions[0].destination.slot, 22)
        self.observe(self.player_slot(22, 42, 1, 209))
        for action, args in (
            (self.bot.unequip_armor, ("head", 23)),
            (self.bot.unequip_armor, ("chest", 22)),
            (self.bot.equip_armor, (23, 0)),
            (self.bot.equip_armor, (22, 1)),
        ):
            with self.subTest(action=action.__name__, args=args):
                with self.assertRaisesRegex(RuntimeError, "automatic unequip request in flight"):
                    await action(*args, timeout=0.05)
                self.assertEqual(len(self.wire.sent), 1)
        explicit = asyncio.create_task(self.bot.unequip_armor("chest", 24))
        second = await self.sent_request(1)
        self.assertEqual(second.actions[0].destination.slot, 24)
        self.success(second, 1, 24)
        self.success(first, 0, 22)
        self.assertEqual((await automatic).result.name, "SUCCESS")
        self.assertEqual((await explicit).result.name, "SUCCESS")

    async def test_explicit_requests_can_still_overlap_each_other(self):
        self.observe(self.armor_slot(0), self.player_slot(22, 50, 1, 207))
        tasks = [asyncio.create_task(self.bot.unequip_armor("head", 22)) for _ in range(2)]
        requests = [await self.sent_request(index) for index in range(2)]
        for request, task in zip(requests, tasks):
            self.assertIsInstance(request.actions[0], self.b.ItemStackRequestCereal.SwapActionData)
            self.observe(self.b.ItemStackResponsePacket(responses=[self.b.ItemStackResponseInfo(
                client_request_id=request.client_request_id, result=self.b.ItemStackNetResult.ERROR,
            )]))
            self.assertEqual((await task).result.name, "ERROR")
        self.assertEqual(self.bot.armor_slots[0].net_id_variant, 101)
        self.assertEqual(self.bot.inventory_slots[22].net_id_variant, 207)

    async def test_cancellation_keeps_submitted_slots_reserved_until_late_verdict(self):
        self.observe(self.armor_slot(0), self.armor_slot(1, net_id=102), self.player_slot(22))
        automatic = asyncio.create_task(self.bot.unequip_armor("head"))
        first = await self.sent_request()
        automatic.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await automatic
        with self.assertRaisesRegex(RuntimeError, "automatic unequip request in flight"):
            await self.bot.unequip_armor("chest", 22, timeout=0.05)
        self.assertEqual(len(self.wire.sent), 1)
        self.success(first, 0, 22)
        self.assertEqual(self.bot.armor_slots[0].id, 0)
        self.assertEqual(self.bot.inventory_slots[22].net_id_variant, 401)
        explicit = asyncio.create_task(self.bot.unequip_armor("chest", 22))
        second = await self.sent_request(1)
        self.assertIsInstance(second.actions[0], self.b.ItemStackRequestCereal.SwapActionData)
        self.observe(self.b.ItemStackResponsePacket(responses=[self.b.ItemStackResponseInfo(
            client_request_id=second.client_request_id, result=self.b.ItemStackNetResult.ERROR,
        )]))
        self.assertEqual((await explicit).result.name, "ERROR")

    async def test_disconnect_releases_submitted_slots_and_ignores_late_verdict(self):
        self.observe(self.armor_slot(0), self.armor_slot(1, net_id=102), self.player_slot(22))
        automatic = asyncio.create_task(self.bot.unequip_armor("head"))
        request = await self.sent_request()
        self.observe(self.b.DisconnectPacket())
        with self.assertRaises(ConnectionError):
            await automatic
        self.success(request, 0, 22)
        with self.assertRaises(ConnectionError):
            await self.bot.unequip_armor("chest", 22)
        self.assertEqual(self.bot.armor_slots[0].net_id_variant, 101)
        self.assertEqual(self.bot.inventory_slots[22].id, 0)
        self.assertEqual(len(self.wire.sent), 1)
