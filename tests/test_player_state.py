import asyncio
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

    async def test_respawn_uses_server_ready_position_then_acknowledges(self):
        from endpilot.movement import Movement

        protocol = Protocol(2193)
        b = protocol.binding
        observer = []
        sent = []
        received = asyncio.get_running_loop().create_future()
        waiting = asyncio.Event()

        class FakePackets:
            def __init__(self):
                self.protocol = protocol

            def subscribe_raw(self, callback):
                observer.append(callback)
                return lambda: observer.remove(callback)

            async def wait_for_packet(self, packet_id, *, timeout, match):
                self_id = b.RespawnPacket.ID
                assert packet_id == self_id
                assert timeout == 3
                waiting.set()
                async with asyncio.timeout(timeout):
                    while True:
                        raw = await received
                        if match(raw):
                            return raw
                        raise AssertionError("unexpected packet")

            async def send(self, packet):
                sent.append(protocol.decode(protocol.encode(packet)))

        packets = FakePackets()
        state = PlayerState(packets, 7)
        movement = Movement(packets, 7)
        observer.append(movement._on_packet)
        pending = asyncio.create_task(state.respawn(timeout=3))
        await waiting.wait()
        await asyncio.sleep(0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].state, b.PlayerRespawnState.CLIENT_READY_TO_SPAWN)
        self.assertEqual(int(sent[0].runtime_id), 7)

        # A server reply for this player updates movement before the waiter resolves.
        reply = b.RespawnPacket(
            pos=b.Vec3(x=3.5, y=103.62, z=9.5),
            state=b.PlayerRespawnState.READY_TO_SPAWN, runtime_id=7,
        )
        raw = protocol.encode(reply)
        for callback in tuple(observer):
            callback(raw)
        received.set_result(raw)
        self.assertEqual(await pending, (3.5, 103.62, 9.5))
        self.assertEqual(movement.position, (3.5, 103.62, 9.5))
        self.assertEqual(movement._input_position, movement.position)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[1].action, b.PlayerActionType.RESPAWN)
        self.assertEqual(int(sent[1].runtime_id), 7)
        state.close()

    async def test_respawn_timeout_does_not_acknowledge(self):
        protocol = Protocol(2193)
        b = protocol.binding
        sent = []
        registered = asyncio.Event()

        class FakePackets:
            def __init__(self):
                self.protocol = protocol

            def subscribe_raw(self, callback):
                return lambda: None

            async def wait_for_packet(self, packet_id, *, timeout, match):
                registered.set()
                raise TimeoutError("no server response")

            async def send(self, packet):
                sent.append(packet)

        state = PlayerState(FakePackets(), 7)
        with self.assertRaises(TimeoutError):
            await state.respawn(timeout=0.1)
        self.assertTrue(registered.is_set())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].state, b.PlayerRespawnState.CLIENT_READY_TO_SPAWN)

