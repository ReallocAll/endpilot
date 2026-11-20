import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from endpilot.player_state import PlayerState
from endpilot.protocol import Protocol, RawPacket


class PlayerStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_health_only_for_local_player(self):
        b = Protocol(2193).binding
        decoded = {
            b"other": SimpleNamespace(runtime_id=99, attribute_data=[
                SimpleNamespace(name="minecraft:health", current_value=5),
            ]),
            b"speed": SimpleNamespace(runtime_id=7, attribute_data=[
                SimpleNamespace(name="minecraft:movement", current_value=0.1),
            ]),
            b"health": SimpleNamespace(runtime_id=7, attribute_data=[
                SimpleNamespace(name="minecraft:health", current_value=17),
            ]),
            b"set": SimpleNamespace(health=12),
        }
        observers = []
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: decoded[raw.body]),
            subscribe_raw=lambda callback: (
                observers.append(callback) or (lambda: observers.remove(callback))
            ),
            wait_for_packet=AsyncMock(),
        )
        state = PlayerState(packets, 7)
        self.assertIsNone(state.health)
        observers[0](RawPacket(b.UpdateAttributesPacket.ID, b"other"))
        observers[0](RawPacket(b.UpdateAttributesPacket.ID, b"speed"))
        self.assertIsNone(state.health)
        observers[0](RawPacket(b.UpdateAttributesPacket.ID, b"health"))
        self.assertEqual(state.health, 17.0)
        observers[0](RawPacket(b.SetHealthPacket.ID, b"set"))
        self.assertEqual(state.health, 12.0)

        async def wait(ids, *, timeout, match):
            self.assertEqual(set(ids), {b.SetHealthPacket.ID, b.UpdateAttributesPacket.ID})
            self.assertEqual(timeout, 3)
            self.assertFalse(match(RawPacket(b.UpdateAttributesPacket.ID, b"other")))
            self.assertFalse(match(RawPacket(b.UpdateAttributesPacket.ID, b"speed")))
            self.assertTrue(match(RawPacket(b.UpdateAttributesPacket.ID, b"health")))
            return RawPacket(b.UpdateAttributesPacket.ID, b"health")

        packets.wait_for_packet.side_effect = wait
        self.assertEqual(await state.wait_for_health_update(timeout=3), 17.0)
        state.close()
        self.assertEqual(observers, [])
