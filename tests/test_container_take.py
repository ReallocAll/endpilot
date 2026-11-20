import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from endpilot.containers import ContainerWindow, Containers
from endpilot.inventory import Inventory
from endpilot.protocol import Protocol, RawPacket


def item(b, item_id=0, count=0, net_id=None):
    return b.SerializedNetworkItemStackDescriptor(
        id=item_id, stack_size=count, aux_value=0, net_id_variant=net_id,
        block_runtime_id=0, user_data_buffer=b"",
    )


def result(b, request_id, *, accepted=True):
    def container(name, slot, count, net):
        return SimpleNamespace(
            full_container_name=SimpleNamespace(name=SimpleNamespace(name=name)),
            slots=[SimpleNamespace(
                slot=slot, amount=count,
                item_stack_net_id=None if net is None else SimpleNamespace(raw_id=net),
            )],
        )

    return SimpleNamespace(
        client_request_id=SimpleNamespace(raw_id=request_id),
        result=b.ItemStackNetResult.SUCCESS if accepted else b.ItemStackNetResult.ERROR,
        containers=[
            container("LEVEL_ENTITY_CONTAINER", 0, 2, 17),
            container("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 2, 2, 21),
        ] if accepted else None,
    )


class TakeContainerTests(unittest.IsolatedAsyncioTestCase):
    async def test_split_stack_uses_net_ids_and_applies_only_matching_server_response(self):
        codec = Protocol(2193)
        b = codec.binding
        responses = {
            b"unrelated": SimpleNamespace(responses=[result(b, -5)]),
            b"ok": SimpleNamespace(responses=[result(b, -1)]),
            b"reject": SimpleNamespace(responses=[result(b, -3, accepted=False)]),
        }
        callbacks = []
        attempts = 0

        async def wait(packet_id, *, timeout, match):
            self.assertEqual(packet_id, b.ItemStackResponsePacket.ID)
            self.assertEqual(timeout, 3)
            self.assertFalse(match(RawPacket(packet_id, b"unrelated")))
            nonlocal attempts
            reply = b"ok" if attempts == 0 else b"reject"
            attempts += 1
            self.assertTrue(match(RawPacket(packet_id, reply)))
            return RawPacket(packet_id, reply)

        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: responses[raw.body]),
            subscribe_raw=lambda cb: callbacks.append(cb) or (lambda: callbacks.remove(cb)),
            send=AsyncMock(),
            wait_for_packet=wait,
        )
        sent_packets = []
        async def send(packet):
            sent_packets.append(packet)
        packets.send.side_effect = send
        inventory = Inventory(packets, 11)
        containers = Containers(packets, None, inventory)
        window = ContainerWindow(2, "CONTAINER", (1, 100, 0), -1)
        containers.window = window
        containers._slots = {0: item(b, 3, 4, 17), 1: item(b)}
        try:
            first = await containers.take_from_container(0, 2, 2, timeout=3)
            self.assertEqual(first.result.name, "SUCCESS")
            self.assertEqual(int(containers.slots[0].stack_size), 2)
            self.assertEqual(int(inventory.item_or_empty(2).stack_size), 2)
            self.assertEqual(int(inventory.item_or_empty(2).id), 3)
            self.assertEqual(int(inventory.item_or_empty(2).net_id_variant), 21)
            self.assertEqual(int(containers.slots[1].id), 0)

            outgoing = codec.decode(codec.encode(sent_packets[0]))
            self.assertIsInstance(outgoing, b.ItemStackRequestPacket)
            req = outgoing.requests[0]
            self.assertEqual(int(req.client_request_id.raw_id), -1)
            self.assertEqual(int(req.actions[0].amount), 2)
            self.assertEqual(req.actions[0].source.full_container_name.name,
                             b.ContainerEnumName.LEVEL_ENTITY_CONTAINER)
            self.assertEqual(int(req.actions[0].source.slot), 0)
            self.assertEqual(int(req.actions[0].source.net_id_variant), 17)
            self.assertEqual(req.actions[0].destination.full_container_name.name,
                             b.ContainerEnumName.COMBINED_HOTBAR_AND_INVENTORY_CONTAINER)
            self.assertEqual(int(req.actions[0].destination.slot), 2)
            self.assertEqual(int(req.actions[0].destination.net_id_variant), 0)

            second = await containers.take_from_container(0, 2, 1, timeout=3)
            self.assertEqual(second.result.name, "ERROR")
            self.assertEqual(int(containers.slots[0].stack_size), 2)
            self.assertEqual(int(inventory.item_or_empty(2).stack_size), 2)
            outgoing = codec.decode(codec.encode(sent_packets[1]))
            self.assertEqual(int(outgoing.requests[0].client_request_id.raw_id), -3)
            self.assertEqual(int(outgoing.requests[0].actions[0].destination.net_id_variant), 21)
        finally:
            containers.close()
            inventory.close()
        self.assertEqual(callbacks, [])

    async def test_missing_observation_and_timeout_preserve_state(self):
        codec = Protocol(2193)
        b = codec.binding
        packets = SimpleNamespace(
            protocol=codec,
            subscribe_raw=lambda cb: (lambda: None),
            send=AsyncMock(),
            wait_for_packet=AsyncMock(side_effect=TimeoutError),
        )
        inventory = Inventory(packets, 11)
        containers = Containers(packets, None, inventory)
        try:
            with self.assertRaises(RuntimeError):
                await containers.take_from_container(0, 2)
            containers.window = ContainerWindow(2, "CONTAINER", (1, 100, 0), -1)
            with self.assertRaises(RuntimeError):
                await containers.take_from_container(0, 2)
            containers._slots = {0: item(b, 3, 4, 17)}
            with self.assertRaises(ValueError):
                await containers.take_from_container(0, 2, 0)
            with self.assertRaises(TimeoutError):
                await containers.take_from_container(0, 2, 2)
            self.assertEqual(int(containers.slots[0].stack_size), 4)
            self.assertEqual(int(inventory.item_or_empty(2).id), 0)
        finally:
            containers.close()
            inventory.close()
