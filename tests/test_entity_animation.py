"""Exercise incoming swings through the public SDK and the native packet codec."""

import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch
from uuid import UUID

from bedrock_protocol import ActorSwingSource, DecodeError, HandSlot

from endpilot import Bot, EntitySwing
from endpilot.compression import PacketConnection
from endpilot.entities import Entities
from endpilot.protocol import Protocol, RawPacket, _write_uint, write_batch
from test_scheduled_input import ManualClock, Wire


class EntityAnimationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        await self.join(2225)

    async def join(self, protocol):
        self.bot = Bot("SwingWatch", protocol=protocol)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)
        for packet in (
            b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
            b.StartGamePacket(runtime_id=7, entity_id=17),
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        ):
            self.wire.push(packet)
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132")

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket(time=23)
        ready = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(
            packet if isinstance(packet, RawPacket) else self.bot.protocol.encode(packet)
            for packet in (*packets, marker)
        ))
        await ready

    def spawn(self, runtime_id=8, entity_id=108):
        b = self.b
        return b.AddActorPacket(runtime_id=runtime_id, entity_id=entity_id, actor_type="minecraft:zombie",
                                pos=b.Vec3(x=1.5, y=100, z=1.5))

    def swing(self, runtime_id=8, *, source=ActorSwingSource.NONE, action="SWING", hand=HandSlot.MAINHAND):
        packet = self.b.AnimatePacket
        kwargs = {"hand_slot": hand} if hasattr(packet, "hand_slot") else {}
        return packet(action=packet.Action[action], runtime_id=runtime_id, swing_source=source, **kwargs)

    async def arm(self, runtime_id=None):
        task = asyncio.create_task(self.bot.wait_for_entity_swing(runtime_id=runtime_id, timeout=1))
        self.addAsyncCleanup(self.cancel, task)
        await asyncio.sleep(0)
        return task

    @staticmethod
    async def cancel(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_filtered_and_global_events_preserve_source_hand_and_unknown_identity(self):
        await self.deliver(self.spawn())
        seen, filtered = [], []
        self.bot.subscribe_entity_swings(seen.append)
        unsubscribe = self.bot.subscribe_entity_swings(filtered.append, runtime_id=8)
        targeted = await self.arm(8)
        global_wait = await self.arm()
        await self.deliver(self.swing(action="WAKE_UP"), self.swing(99, source=None, hand=HandSlot.OFFHAND))
        self.assertEqual(await global_wait, EntitySwing(99, None, None, None, HandSlot.OFFHAND))
        self.assertFalse(targeted.done())
        await self.deliver(self.swing(source=ActorSwingSource.ATTACK))
        event = await targeted
        entity, = self.bot.observed_entities
        self.assertEqual(event, EntitySwing(8, 108, entity, ActorSwingSource.ATTACK, HandSlot.MAINHAND))
        self.assertEqual(filtered, [event])
        with self.assertRaises(FrozenInstanceError):
            event.runtime_id = 9
        await self.deliver(self.swing(), self.swing())
        self.assertEqual([event.source for event in filtered], [ActorSwingSource.ATTACK, ActorSwingSource.NONE, ActorSwingSource.NONE])
        unsubscribe()
        unsubscribe()
        await self.deliver(self.swing())
        self.assertEqual(len(filtered), 3)
        self.assertEqual(len(seen), 5)

    async def test_unknown_wire_values_and_the_legacy_absent_hand_are_preserved(self):
        seen = []
        self.bot.subscribe_entity_swings(seen.append)
        runtime_id = 2**63 + 9
        packet = self.bot.protocol.encode(self.swing(runtime_id, source=ActorSwingSource.ATTACK))
        start = packet.body.index(b"attack")
        for name, present in (("future-swing", 1), ("挥臂", 2)):
            encoded = name.encode("utf-8")
            body = packet.body[:start - 2] + bytes([present]) + _write_uint(len(encoded)) + encoded + b"\x5b"
            future = RawPacket(packet.header, body)
            with self.assertRaises(DecodeError) as caught:
                self.bot.protocol.decode(future)
            self.assertEqual((caught.exception.path, caught.exception.reason), (("swing_source",), "unknown value"))
            await self.deliver(future, RawPacket(packet.header, body[:-1]))
        self.assertEqual(seen, [
            EntitySwing(runtime_id, None, None, "future-swing", 91),
            EntitySwing(runtime_id, None, None, "挥臂", 91),
        ])
        await self.bot.close()
        await self.join(2193)
        waiting = await self.arm()
        await self.deliver(self.swing(77, source=ActorSwingSource.NONE))
        self.assertEqual(await waiting, EntitySwing(77, None, None, ActorSwingSource.NONE, None))

    async def test_protocol_898_recovers_a_source_without_a_hand_field(self):
        protocol = Protocol(898)
        b = protocol.binding
        wire = Wire(protocol)
        connection = PacketConnection(wire, protocol, b.NetworkSettingsPacket(
            compression_algorithm=b.PacketCompressionAlgorithm.NONE,
        ))
        self.addAsyncCleanup(connection.close)
        entities = Entities(connection)
        entities.start()
        connection.start_receiving()
        waiting = asyncio.create_task(entities.wait_for_entity_swing(timeout=1))
        await asyncio.sleep(0)
        animate = b.AnimatePacket
        packet = protocol.encode(animate(
            action=animate.Action.SWING, runtime_id=77, swing_source=ActorSwingSource.ATTACK,
        ))
        start = packet.body.index(b"attack")
        source = b"future-swing"
        body = packet.body[:start - 2] + b"\xff" + _write_uint(len(source)) + source
        wire.incoming.put_nowait(write_batch([RawPacket(packet.header, body)]))
        self.assertEqual(await waiting, EntitySwing(77, None, None, "future-swing", None))

    async def test_empty_invalid_and_truncated_wire_sources_do_not_publish(self):
        seen = []
        self.bot.subscribe_entity_swings(seen.append)
        packet = self.bot.protocol.encode(self.swing(source=ActorSwingSource.ATTACK))
        start = packet.body.index(b"attack")
        await self.deliver(
            RawPacket(packet.header, packet.body[:start - 1] + b"\x00" + packet.body[start + 6:]),
            RawPacket(packet.header, packet.body.replace(b"attack", b"\xffuture")),
            RawPacket(packet.header, packet.body[:start + 1]),
            RawPacket(packet.header, packet.body.replace(b"attack", b"future") + b"\x00"),
        )
        self.assertEqual(seen, [])

    async def test_recovery_requires_both_native_error_field_and_reason(self):
        protocol = self.bot.protocol
        packet = protocol.encode(self.swing(source=ActorSwingSource.ATTACK))
        future = RawPacket(packet.header, packet.body.replace(b"attack", b"future"))
        seen = []
        self.bot.subscribe_entity_swings(seen.append)
        decode = protocol.decode
        for path, reason in ((("hand_slot",), "unknown value"), (("swing_source",), "truncated")):
            def reject_future(raw):
                if raw == future:
                    raise DecodeError("test codec error", reason, path, 0)
                return decode(raw)

            with patch.object(protocol, "decode", side_effect=reject_future):
                await self.deliver(future)
        self.assertEqual(seen, [])

    async def test_local_send_never_completes_a_wait_but_an_actual_inbound_swing_can(self):
        received = []
        self.bot.subscribe_entity_swings(received.append, runtime_id=7)
        waiting = await self.arm(7)
        await self.bot.swing()
        self.assertTrue(any(isinstance(packet, self.b.AnimatePacket) for packet in self.wire.sent))
        self.assertEqual(received, [])
        self.assertFalse(waiting.done())
        await self.deliver(self.swing(7))
        self.assertEqual(await waiting, EntitySwing(7, 17, None, ActorSwingSource.NONE, HandSlot.MAINHAND))

    async def test_removal_and_runtime_reuse_end_the_old_filtered_instance(self):
        await self.deliver(self.spawn())
        old, seen = [], []
        self.bot.subscribe_entity_swings(old.append, runtime_id=8)
        self.bot.subscribe_entity_swings(seen.append)
        waiting = await self.arm(8)
        await self.deliver(self.b.RemoveActorPacket(entity_id=108), self.swing(), self.spawn(entity_id=109), self.swing())
        with self.assertRaisesRegex(LookupError, "no longer observed"):
            await waiting
        self.assertEqual(old, [])
        self.assertEqual([event.entity_id for event in seen], [None, 109])
        fresh = await self.arm(8)
        await self.deliver(self.swing())
        self.assertEqual((await fresh).entity_id, 109)
        displaced = await self.arm(8)
        await self.deliver(self.spawn(entity_id=110), self.swing())
        with self.assertRaises(LookupError):
            await displaced

    def spawn_player(self, uuid, abilities_id):
        b = self.b
        return b.AddPlayerPacket(
            uuid=uuid, runtime_id=8, name="Observed", pos=b.Vec3(),
            abilities_data=b.SerializedAbilitiesData(target_player=abilities_id),
        )

    def list_player(self, uuid, entity_id):
        b = self.b
        return b.PlayerListPacket(entries=[b.PlayerListPacket.AddEntry(
            action=b.PlayerListPacketType.ADD, uuid=uuid, id=entity_id, name="Observed",
        )])

    async def test_roster_conflict_ends_the_instance_identified_by_spawn_abilities(self):
        uuid = UUID(int=108)
        seen, filtered = [], []
        self.bot.subscribe_entity_swings(seen.append)
        await self.deliver(self.spawn_player(uuid, 108), self.swing())
        self.assertEqual((seen[0].entity_id, seen[0].entity.entity_id), (108, 0))
        waiting = await self.arm(8)
        self.bot.subscribe_entity_swings(filtered.append, runtime_id=8)
        await self.deliver(self.list_player(uuid, 109), self.swing())
        with self.assertRaisesRegex(LookupError, "no longer observed"):
            await waiting
        self.assertEqual(filtered, [])
        self.assertEqual((seen[1].entity_id, seen[1].entity.entity_id), (109, 109))
        fresh = await self.arm(8)
        await self.deliver(self.swing())
        self.assertIs(await fresh, seen[2])

    async def test_roster_confirmation_and_first_known_identity_keep_the_filtered_instance(self):
        for abilities_id, roster_id in ((108, 108), (0, 109)):
            with self.subTest(abilities_id=abilities_id):
                uuid = UUID(int=roster_id)
                await self.deliver(self.spawn_player(uuid, abilities_id))
                initial = await self.arm(8)
                await self.deliver(self.swing())
                self.assertEqual((await initial).entity_id, abilities_id or None)
                filtered = []
                unsubscribe = self.bot.subscribe_entity_swings(filtered.append, runtime_id=8)
                waiting = await self.arm(8)
                await self.deliver(self.list_player(uuid, roster_id), self.swing())
                event = await waiting
                self.assertEqual(event.entity_id, roster_id)
                self.assertEqual(filtered, [event])
                unsubscribe()

    async def test_unknown_roster_id_keeps_either_existing_source_of_identity(self):
        for listed_id, expected in ((None, 108), (109, 109)):
            with self.subTest(listed_id=listed_id):
                uuid = UUID(int=expected)
                await self.deliver(self.spawn_player(uuid, 108))
                if listed_id is not None:
                    await self.deliver(self.list_player(uuid, listed_id))
                filtered = []
                unsubscribe = self.bot.subscribe_entity_swings(filtered.append, runtime_id=8)
                waiting = await self.arm(8)
                await self.deliver(self.list_player(uuid, 0), self.swing())
                event = await waiting
                self.assertEqual(event.entity_id, expected)
                self.assertEqual(filtered, [event])
                unsubscribe()

    async def test_dimension_cut_ends_waits_but_global_subscription_remains(self):
        await self.deliver(self.spawn())
        seen, filtered = [], []
        self.bot.subscribe_entity_swings(seen.append)
        self.bot.subscribe_entity_swings(filtered.append, runtime_id=8)
        waiting = (await self.arm(), await self.arm(8))
        await self.deliver(self.b.ChangeDimensionPacket(dimension_id=1), self.swing())
        for task in waiting:
            with self.assertRaisesRegex(RuntimeError, "dimension changed"):
                await task
        self.assertEqual([event.entity_id for event in seen], [None])
        self.assertEqual(filtered, [])
        with self.assertRaisesRegex(LookupError, "not currently observed"):
            self.bot.subscribe_entity_swings(filtered.append, runtime_id=8)

    async def test_callback_changes_and_failures_leave_current_dispatch_stable(self):
        seen, errors = [], []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, details: errors.append(details["exception"]))
        self.addCleanup(loop.set_exception_handler, previous)

        def first(_):
            seen.append("first")
            if len(seen) == 1:
                stop_second()
                self.bot.subscribe_entity_swings(lambda _: seen.append("third"))
                raise asyncio.CancelledError("test callback error")

        self.bot.subscribe_entity_swings(first)
        stop_second = self.bot.subscribe_entity_swings(lambda _: seen.append("second"))
        await self.deliver(self.swing(), self.swing())
        self.assertEqual(seen, ["first", "second", "first", "third"])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], asyncio.CancelledError)

    async def test_wait_cancel_and_disconnect_end_only_the_expected_observations(self):
        seen = []
        unsubscribe = self.bot.subscribe_entity_swings(seen.append)
        cancelled = await self.arm()
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        next_event = await self.arm()
        await self.deliver(self.swing())
        self.assertIs(await next_event, seen[0])
        failing = await self.arm()
        await self.deliver(self.b.DisconnectPacket(), self.swing())
        with self.assertRaises(ConnectionError):
            await failing
        self.assertEqual(len(seen), 1)
        with self.assertRaises(ConnectionError):
            self.bot.subscribe_entity_swings(seen.append)
        unsubscribe()

    async def test_reader_failure_ends_wait_and_keeps_the_cause(self):
        waiting = await self.arm()
        error = OSError("synthetic reader failure")
        self.wire.incoming.put_nowait(error)
        with self.assertRaises(ConnectionError) as caught:
            await waiting
        self.assertIs(caught.exception.__cause__, error)
        with self.assertRaises(OSError):
            await self.bot.close()
