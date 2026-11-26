import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from item_response_stream import feed_item_replies

from endpilot.containers import ContainerWindow, Containers
from endpilot.inventory import Inventory
from endpilot.protocol import Protocol, RawPacket


def item(b, identity=0, count=0, net_id=None):
    return b.SerializedNetworkItemStackDescriptor(
        id=identity, stack_size=count, aux_value=0, net_id_variant=net_id,
        block_runtime_id=0, user_data_buffer=b"",
    )


class FurnaceTransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingredient_fuel_and_output_use_distinct_container_names(self):
        codec = Protocol(2193)
        b = codec.binding
        replies = {}

        def changed(name, slot, count, net_id=None):
            return SimpleNamespace(
                full_container_name=SimpleNamespace(name=b.ContainerEnumName[name]),
                slots=[SimpleNamespace(
                    slot=slot, amount=count,
                    item_stack_net_id=None if net_id is None else SimpleNamespace(raw_id=net_id),
                )],
            )

        for request_id, key, name, physical_slot, count, player_slot, player_count in [
            (-1, b"ore", "FURNACE_INGREDIENT_CONTAINER", 0, 1, 0, 0),
            (-3, b"coal", "FURNACE_FUEL_CONTAINER", 1, 1, 1, 0),
            (-5, b"ingot", "FURNACE_RESULT_CONTAINER", 2, 0, 2, 1),
        ]:
            replies[key] = SimpleNamespace(responses=[SimpleNamespace(
                client_request_id=SimpleNamespace(raw_id=request_id),
                result=b.ItemStackNetResult.SUCCESS,
                containers=[
                    changed(name, physical_slot, count, 50 if count else None),
                    changed("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                            player_slot, player_count, 60 if player_count else None),
                ],
            )])
        # A single ingredient response may also correct other furnace slots.
        replies[b"ore"].responses[0].containers.extend([
            changed("FURNACE_FUEL_CONTAINER", 1, 2, 51),
            changed("FURNACE_RESULT_CONTAINER", 2, 2, 52),
        ])
        replies[b"other"] = SimpleNamespace(responses=[
            SimpleNamespace(client_request_id=SimpleNamespace(raw_id=-99))
        ])
        labels = iter((b"ore", b"coal", b"ingot"))
        sent = []

        async def wait(packet_id, *, timeout, match):
            self.assertEqual(packet_id, b.ItemStackResponsePacket.ID)
            self.assertEqual(timeout, 3)
            self.assertFalse(match(RawPacket(packet_id, b"other")))
            raw = RawPacket(packet_id, next(labels))
            self.assertTrue(match(raw))
            return raw

        def decode(raw):
            if raw.packet_id == b.ItemStackResponsePacket.ID:
                return replies[raw.body]
            return codec.decode(raw)

        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=decode),
            subscribe_raw=lambda cb: lambda: None,
            wait_for_packet=wait,
            send=AsyncMock(side_effect=lambda packet: sent.append(packet)),
        )
        inventory = Inventory(packets, 11)
        feed_item_replies(inventory, timeout=3)
        containers = Containers(packets, None, inventory)
        containers.window = ContainerWindow(2, "FURNACE", (1, 100, 0), -1)
        containers._slots = {0: item(b), 1: item(b), 2: item(b, 3, 1, 32)}
        inventory._set_slot(0, item(b, 1, 1, 11))
        inventory._set_slot(1, item(b, 2, 1, 12))
        try:
            self.assertEqual((await containers.put_into_container(0, 0, timeout=3)).result.name,
                             "SUCCESS")
            self.assertNotIn(1, containers.slots)
            self.assertNotIn(2, containers.slots)
            containers._on_packet(codec.encode(b.InventorySlotPacket(inventory_id=2, slot=2, item=item(b, 4, 2, 52))))
            self.assertEqual((int(containers.slots[2].id), int(containers.slots[2].stack_size),
                              int(containers.slots[2].net_id_variant)), (4, 2, 52))
            self.assertEqual((await containers.put_into_container(1, 1, timeout=3)).result.name,
                             "SUCCESS")
            self.assertEqual((await containers.take_from_container(2, 2, timeout=3)).result.name,
                             "SUCCESS")
            expected = (
                ("FURNACE_INGREDIENT_CONTAINER", "destination", 0),
                ("FURNACE_FUEL_CONTAINER", "destination", 1),
                ("FURNACE_RESULT_CONTAINER", "source", 2),
            )
            for index, (name, field, physical_slot) in enumerate(expected):
                request = codec.decode(codec.encode(sent[index])).requests[0]
                self.assertEqual(int(request.client_request_id.raw_id), -1 - index * 2)
                named = getattr(request.actions[0], field)
                self.assertEqual(named.full_container_name.name, b.ContainerEnumName[name])
                self.assertEqual(int(named.slot), physical_slot)
            self.assertEqual(int(containers.slots[0].stack_size), 1)
            self.assertEqual(int(containers.slots[1].stack_size), 1)
            self.assertEqual(int(containers.slots[2].id), 0)
            self.assertEqual((int(inventory.item_or_empty(2).id),
                              int(inventory.item_or_empty(2).stack_size)), (4, 1))
            self.assertEqual(int(inventory.item_or_empty(0).id), 0)
            self.assertEqual(int(inventory.item_or_empty(1).id), 0)
        finally:
            containers.close()
            inventory.close()

    async def test_out_of_range_furnace_slot_does_not_send(self):
        b = Protocol(2193).binding
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b),
            subscribe_raw=lambda cb: lambda: None,
            send=AsyncMock(),
        )
        inventory = Inventory(packets, 11)
        containers = Containers(packets, None, inventory)
        containers.window = ContainerWindow(2, "FURNACE", (1, 100, 0), -1)
        containers._slots = {3: item(b, 3, 1, 9)}
        inventory._set_slot(0, item(b, 1, 1, 11))
        try:
            with self.assertRaisesRegex(ValueError, "furnace slots"):
                await containers.put_into_container(0, 3)
            with self.assertRaisesRegex(ValueError, "furnace slots"):
                await containers.take_from_container(3, 0)
            packets.send.assert_not_awaited()
        finally:
            containers.close()
            inventory.close()
