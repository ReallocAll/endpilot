import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from endpilot.entities import Entities, EntityMovement, EntitySpawn
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

    async def test_movement_matches_runtime_id_and_preserves_omitted_axes(self):
        binding = Protocol(2193).binding
        other = RawPacket(binding.MoveActorDeltaPacket.ID, b"other")
        moved = RawPacket(binding.MoveActorDeltaPacket.ID, b"moved")
        decoded = {
            b"other": SimpleNamespace(move_data=SimpleNamespace(runtime_id=999)),
            b"moved": SimpleNamespace(move_data=SimpleNamespace(
                runtime_id=77, new_position_x=6.0,
                new_position_y=None, new_position_z=None,
            )),
        }
        protocol = SimpleNamespace(binding=binding, decode=lambda raw: decoded[raw.body])
        packets = SimpleNamespace(protocol=protocol, wait_for_packet=AsyncMock())

        async def wait(packet_ids, *, match, timeout):
            self.assertEqual(set(packet_ids), {
                binding.MoveActorAbsolutePacket.ID, binding.MoveActorDeltaPacket.ID,
            })
            self.assertFalse(match(other))
            self.assertTrue(match(moved))
            self.assertEqual(timeout, 8)
            return moved

        packets.wait_for_packet.side_effect = wait
        result = await Entities(packets).wait_for_movement(77, timeout=8)
        self.assertEqual(result, EntityMovement(77, 6.0, None, None))

    async def test_absolute_actor_movement_has_all_coordinates(self):
        binding = Protocol(2193).binding
        packet = RawPacket(binding.MoveActorAbsolutePacket.ID, b"absolute")
        move_data = SimpleNamespace(
            runtime_id=77, pos=SimpleNamespace(x=6.0, y=100.0, z=0.5),
        )
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=binding, decode=lambda raw: SimpleNamespace(move_data=move_data)),
            wait_for_packet=AsyncMock(return_value=packet),
        )
        result = await Entities(packets).wait_for_movement(77)
        self.assertEqual(result, EntityMovement(77, 6.0, 100.0, 0.5))
