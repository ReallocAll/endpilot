import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from endpilot.entities import Entities, EntitySpawn
from endpilot.protocol import Protocol, RawPacket


class EntityTests(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_filters_type_and_returns_server_ids_and_position(self):
        binding = Protocol(2193).binding
        other = RawPacket(binding.AddActorPacket.ID, b"other")
        cow = RawPacket(binding.AddActorPacket.ID, b"cow")
        decoded = {
            b"other": SimpleNamespace(actor_type="minecraft:pig"),
            b"cow": SimpleNamespace(
                actor_type="minecraft:cow", entity_id=42, runtime_id=77,
                pos=SimpleNamespace(x=2.5, y=101.0, z=0.5),
            ),
        }
        protocol = SimpleNamespace(binding=binding, decode=lambda raw: decoded[raw.body])
        packets = SimpleNamespace(protocol=protocol, wait_for_packet=AsyncMock())

        async def wait(packet_id, *, match, timeout):
            self.assertEqual(packet_id, binding.AddActorPacket.ID)
            self.assertEqual(timeout, 5)
            self.assertFalse(match(other))
            self.assertTrue(match(cow))
            return cow

        packets.wait_for_packet.side_effect = wait
        entity = await Entities(packets).wait_for_spawn(actor_type="minecraft:cow", timeout=5)
        self.assertEqual(entity, EntitySpawn(42, 77, "minecraft:cow", (2.5, 101.0, 0.5)))

    async def test_removal_matches_unique_id_not_runtime_id(self):
        binding = Protocol(2193).binding
        packet = binding.RemoveActorPacket(entity_id=42)
        protocol = Protocol(2193)
        raw = protocol.encode(packet)
        self.assertEqual(protocol.decode(raw).entity_id, 42)
        packets = SimpleNamespace(protocol=protocol, wait_for_packet=AsyncMock())

        async def wait(packet_id, *, match, timeout):
            self.assertEqual(packet_id, binding.RemoveActorPacket.ID)
            self.assertFalse(match(protocol.encode(binding.RemoveActorPacket(entity_id=77))))
            self.assertTrue(match(raw))
            return raw

        packets.wait_for_packet.side_effect = wait
        self.assertIsNone(await Entities(packets).wait_for_removal(42))
