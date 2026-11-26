"""Server-corrected inventory swap through the public item-stack packet contract."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from item_response_stream import feed_item_replies

from endpilot.inventory import Inventory
from endpilot.protocol import Protocol, RawPacket


class SwapInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_swap_two_items_and_preserve_their_descriptors(self):
        codec = Protocol(2193)
        b = codec.binding

        def item(item_id, count, net_id, data):
            return b.SerializedNetworkItemStackDescriptor(
                id=item_id, stack_size=count, aux_value=7,
                net_id_variant=net_id, block_runtime_id=34,
                user_data_buffer=data,
            )

        dirt, stone = item(3, 2, 101, b"dirt"), item(1, 4, 202, b"stone")
        unrelated = item(6, 1, 303, b"other")
        replies = {}

        async def wait(packet_id, *, timeout, match):
            self.assertEqual(packet_id, b.ItemStackResponsePacket.ID)
            self.assertEqual(timeout, 3)
            raw = RawPacket(packet_id, b"reply")
            self.assertTrue(match(raw))
            return raw

        stream = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: replies[raw.body]),
            subscribe_raw=lambda cb: lambda: None,
            wait_for_packet=wait,
            send=AsyncMock(),
        )
        inventory = Inventory(stream, 1)
        feed_item_replies(inventory, timeout=3)
        inventory._set_slot(2, dirt)
        inventory._set_slot(3, stone)
        inventory._set_slot(4, unrelated)
        slot_name = SimpleNamespace(name="COMBINED_HOTBAR_AND_INVENTORY_CONTAINER")

        def slot(index, amount, net_id):
            return SimpleNamespace(
                slot=index, amount=amount,
                item_stack_net_id=SimpleNamespace(raw_id=net_id),
            )

        def response(result, slots):
            return SimpleNamespace(
                client_request_id=SimpleNamespace(raw_id=-1),
                result=SimpleNamespace(name=result),
                containers=[
                    SimpleNamespace(
                        full_container_name=SimpleNamespace(name=slot_name),
                        slots=slots,
                    ),
                ],
            )

        try:
            # ItemStackResponse may also carry corrections for unrelated slots.
            success = response("SUCCESS", [slot(2, 4, 204), slot(3, 2, 103),
                                            slot(4, 1, 303)])
            replies[b"reply"] = SimpleNamespace(responses=[success])
            self.assertIs(await inventory.swap_items(2, 3, timeout=3), success)
            request = codec.decode(codec.encode(stream.send.await_args.args[0]))
            self.assertIsInstance(request, b.ItemStackRequestPacket)
            action = request.requests[0].actions[0]
            self.assertIsInstance(action, b.ItemStackRequestCereal.SwapActionData)
            self.assertEqual((int(action.source.slot), int(action.destination.slot)), (2, 3))
            self.assertEqual((int(action.source.net_id_variant),
                              int(action.destination.net_id_variant)), (101, 202))
            self.assertEqual((inventory.slots[2].id, inventory.slots[2].stack_size,
                              inventory.slots[2].net_id_variant), (1, 4, 204))
            self.assertEqual((inventory.slots[3].id, inventory.slots[3].stack_size,
                              inventory.slots[3].net_id_variant), (3, 2, 103))
            self.assertEqual(inventory.slots[2].user_data_buffer, b"stone")
            self.assertEqual(inventory.slots[3].user_data_buffer, b"dirt")
            self.assertIs(inventory.slots[4], unrelated)

            # A rejected subsequent swap carries no fabricated state change.
            rejected = SimpleNamespace(
                client_request_id=SimpleNamespace(raw_id=-3),
                result=SimpleNamespace(name="ERROR"),
                containers=[],
            )
            replies[b"reply"] = SimpleNamespace(responses=[rejected])
            self.assertIs(await inventory.swap_items(2, 3, timeout=3), rejected)
            self.assertEqual((inventory.slots[2].id, inventory.slots[3].id), (1, 3))
            with self.assertRaises(ValueError):
                await inventory.swap_items(-1, 3)
            with self.assertRaises(ValueError):
                await inventory.swap_items(2, 256)
            self.assertEqual(stream.send.await_count, 2)
        finally:
            inventory.close()

    async def test_same_slot_server_update_does_not_restore_stale_item_identity(self):
        codec = Protocol(2193)
        b = codec.binding

        def item(item_id, count, net_id):
            return b.SerializedNetworkItemStackDescriptor(
                id=item_id, stack_size=count, aux_value=0,
                net_id_variant=net_id, block_runtime_id=0,
                user_data_buffer=b"",
            )

        for update_before, response_net_id, expected_id in (
            (True, 404, 57), (True, 505, None), (False, 505, 57)
        ):
            with self.subTest(update_before=update_before, response_net_id=response_net_id):
                old_source = item(3, 2, 101)
                old_destination = item(1, 1, 202)
                server_grant = item(57, 1, 404)
                reply = SimpleNamespace(
                    client_request_id=SimpleNamespace(raw_id=-1),
                    result=SimpleNamespace(name="SUCCESS"),
                    containers=[SimpleNamespace(
                        full_container_name=SimpleNamespace(
                            name=SimpleNamespace(
                                name="COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"
                            )
                        ),
                        slots=[
                            SimpleNamespace(
                                slot=2, amount=1,
                                item_stack_net_id=SimpleNamespace(raw_id=response_net_id),
                            ),
                            SimpleNamespace(
                                slot=3, amount=2,
                                item_stack_net_id=SimpleNamespace(raw_id=103),
                            ),
                        ],
                    )],
                )
                decoded = {
                    b"server_update": SimpleNamespace(
                        inventory_id=b.ContainerID.INVENTORY,
                        slot=2, item=server_grant,
                    ),
                    b"response": SimpleNamespace(responses=[reply]),
                }
                inventory = None

                async def wait(packet_id, *, timeout, match):
                    raw = RawPacket(packet_id, b"response")
                    update = RawPacket(b.InventorySlotPacket.ID, b"server_update")
                    if update_before:
                        inventory._on_packet(update)
                    self.assertTrue(match(raw))
                    if not update_before:
                        # A later server packet must win even if the awaiting
                        # coroutine has not resumed yet.
                        inventory._on_packet(update)
                    return raw

                stream = SimpleNamespace(
                    protocol=SimpleNamespace(
                        binding=b, decode=lambda raw: decoded[raw.body]
                    ),
                    subscribe_raw=lambda cb: lambda: None,
                    wait_for_packet=wait,
                    send=AsyncMock(),
                )
                inventory = Inventory(stream, 1)
                feed_item_replies(inventory)
                try:
                    inventory._set_slot(2, old_source)
                    inventory._set_slot(3, old_destination)
                    self.assertIs(await inventory.swap_items(2, 3), reply)
                    observed = inventory.slots.get(2)
                    if expected_id is None:
                        self.assertIsNone(observed)
                    else:
                        self.assertEqual(
                            (int(observed.id), int(observed.stack_size),
                             int(observed.net_id_variant)),
                            (expected_id, 1, response_net_id if update_before else 404),
                        )
                    self.assertEqual(
                        (int(inventory.slots[3].id), int(inventory.slots[3].stack_size)),
                        (3, 2),
                    )
                finally:
                    inventory.close()
