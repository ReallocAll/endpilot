import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
        try:
            inventory._set_slot(2, item(b, 42, 1, 101))
            result = await inventory.equip_armor(2, 0, timeout=4)
            self.assertIs(result, reply)
            sent = codec.decode(codec.encode(stream.send.await_args.args[0]))
            self.assertIsInstance(sent, b.ItemStackRequestPacket)
            request = sent.requests[0]
            self.assertEqual(int(request.client_request_id.raw_id), -1)
            action = request.actions[0]
            self.assertIsInstance(action, b.ItemStackRequestCereal.SwapActionData)
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
        try:
            inventory._set_slot(2, item(b, 42, 1, 44))
            inventory._armor_slots[0] = item(b, 50, 1, 55)
            result = await inventory.equip_armor(2, 0)
            self.assertIs(result, reply)
            self.assertEqual(int(inventory.armor_slots[0].id), 50)
            self.assertEqual(int(inventory.item_or_empty(2).id), 42)
            self.assertEqual(int(inventory.armor_slots[0].net_id_variant), 55)
        finally:
            inventory.close()

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
            for src, dst in ((-1, 0), (256, 0), (0, -1), (0, 4)):
                with self.assertRaises(ValueError):
                    await inventory.equip_armor(src, dst)
            with self.assertRaises(ValueError):
                await inventory.equip_armor(0, 0)
            stream.send.assert_not_awaited()
        finally:
            inventory.close()
