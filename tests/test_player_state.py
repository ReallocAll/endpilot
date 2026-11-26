import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from endpilot import Bot, DeathEvent, PlayerEffect, PlayerVitals, Weather, WorldRule
from endpilot.compression import PacketConnection
from endpilot.login import JoinSession
from endpilot.player_state import PlayerState
from endpilot.protocol import Protocol, RawPacket, read_batch, write_batch
from endpilot.qa import PacketTimeline


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
            self.assertEqual(set(ids), {b.SetHealthPacket.ID, b.UpdateAttributesPacket.ID, b.DisconnectPacket.ID})
            self.assertEqual(timeout, 3)
            self.assertFalse(match(RawPacket(b.UpdateAttributesPacket.ID, b"other")))
            self.assertFalse(match(RawPacket(b.UpdateAttributesPacket.ID, b"speed")))
            self.assertTrue(match(RawPacket(b.UpdateAttributesPacket.ID, b"health")))
            return RawPacket(b.UpdateAttributesPacket.ID, b"health")

        packets.wait_for_packet.side_effect = wait
        self.assertEqual(await state.wait_for_health_update(timeout=3), 17.0)
        state.close()
        self.assertEqual(observers, [])

    async def test_hunger_updates_only_from_local_player(self):
        b = Protocol(2193).binding
        decoded = {
            b"other": SimpleNamespace(runtime_id=99, attribute_data=[
                SimpleNamespace(name="minecraft:player.hunger", current_value=6),
            ]),
            b"health": SimpleNamespace(runtime_id=7, attribute_data=[
                SimpleNamespace(name="minecraft:health", current_value=15),
            ]),
            b"hunger": SimpleNamespace(runtime_id=7, attribute_data=[
                SimpleNamespace(name="minecraft:player.hunger", current_value=17),
            ]),
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
        self.assertIsNone(state.hunger)
        observers[0](RawPacket(b.UpdateAttributesPacket.ID, b"other"))
        observers[0](RawPacket(b.UpdateAttributesPacket.ID, b"health"))
        self.assertIsNone(state.hunger)
        observers[0](RawPacket(b.UpdateAttributesPacket.ID, b"hunger"))
        self.assertEqual(state.hunger, 17.0)

        async def wait(ids, *, timeout, match):
            self.assertEqual(set(ids), {b.UpdateAttributesPacket.ID, b.DisconnectPacket.ID})
            self.assertEqual(timeout, 3)
            self.assertFalse(match(RawPacket(b.UpdateAttributesPacket.ID, b"other")))
            self.assertFalse(match(RawPacket(b.UpdateAttributesPacket.ID, b"health")))
            self.assertTrue(match(RawPacket(b.UpdateAttributesPacket.ID, b"hunger")))
            return RawPacket(b.UpdateAttributesPacket.ID, b"hunger")

        packets.wait_for_packet.side_effect = wait
        self.assertEqual(await state.wait_for_hunger_update(timeout=3), 17.0)
        state.close()
        self.assertEqual(observers, [])


class Transport:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def recv(self):
        frame = await self.incoming.get()
        if isinstance(frame, Exception):
            raise frame
        return frame

    async def send(self, frame):
        self.outgoing.put_nowait(frame)

    async def close(self):
        pass


class RespawnTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, *, auto_respawn=False):
        self.bot = Bot("RespawnProbe", protocol=2225, auto_respawn=auto_respawn)
        self.addAsyncCleanup(self.bot.close)
        self.protocol = self.bot.protocol
        self.b = self.protocol.binding
        self.transport = Transport()

    async def join(self, *, auto_respawn=False):
        self.make_bot(auto_respawn=auto_respawn)
        packets = PacketConnection(self.transport, self.protocol, self.b.NetworkSettingsPacket(
            compression_algorithm=self.b.PacketCompressionAlgorithm.NONE,
        ))
        session = JoinSession(packets, self.bot.identity, "http://127.0.0.1:19132")
        session.player_id, session.player_unique_id = 42, 7
        with patch.object(JoinSession, "connect_nethernet", return_value=session):
            await self.bot.join_nethernet("http://127.0.0.1:19132")

    def deliver(self, *packets):
        self.transport.incoming.put_nowait(write_batch(map(self.protocol.encode, packets)))

    def respawn_packet(self, state, *, runtime_id=0, x=8.5):
        return self.b.RespawnPacket(pos=self.b.Vec3(x=x, y=221.625, z=7.5), state=state,
                                     runtime_id=runtime_id)

    def death_info(self):
        return self.b.DeathInfoPacket(death_cause_message=self.b.DeathInfoPacket.DeathCauseMessage(
            attack_name="death.attack.generic", message_list=[self.bot.identity.name],
        ))

    async def sent(self, kind):
        async with asyncio.timeout(1):
            while True:
                raw = next(read_batch(await self.transport.outgoing.get()))
                if raw.packet_id == kind.ID:
                    return self.protocol.decode(raw)

    def assert_no_respawn_packets(self):
        while not self.transport.outgoing.empty():
            raw = next(read_batch(self.transport.outgoing.get_nowait()))
            self.assertNotEqual(raw.packet_id, self.b.RespawnPacket.ID)
            if raw.packet_id == self.b.PlayerActionPacket.ID:
                self.assertNotEqual(self.protocol.decode(raw).action, self.b.PlayerActionType.RESPAWN)

    async def test_respawn_waits_for_own_search_and_rebases_movement_on_ready(self):
        await self.join()
        b = self.b
        pending = asyncio.create_task(self.bot.respawn(timeout=1))
        await asyncio.sleep(0)
        self.deliver(self.respawn_packet(b.PlayerRespawnState.READY_TO_SPAWN),
                     self.respawn_packet(b.PlayerRespawnState.SEARCHING_FOR_SPAWN, runtime_id=43))
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        self.assert_no_respawn_packets()

        self.deliver(self.respawn_packet(b.PlayerRespawnState.SEARCHING_FOR_SPAWN))
        request = await self.sent(b.RespawnPacket)
        self.assertEqual((request.state, request.runtime_id), (b.PlayerRespawnState.CLIENT_READY_TO_SPAWN, 42))
        self.deliver(self.respawn_packet(b.PlayerRespawnState.READY_TO_SPAWN, runtime_id=43, x=3.5))
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        self.assertNotEqual(self.bot.position, (3.5, 221.625, 7.5))
        self.deliver(self.respawn_packet(b.PlayerRespawnState.READY_TO_SPAWN))
        self.assertEqual(await pending, (8.5, 221.625, 7.5))
        self.assertEqual(self.bot.position, (8.5, 221.625, 7.5))
        ack = await self.sent(b.PlayerActionPacket)
        self.assertEqual((ack.action, ack.runtime_id), (b.PlayerActionType.RESPAWN, 42))

    async def test_respawn_submission_returns_before_ready_and_cancellation_preserves_ack(self):
        await self.join()
        b = self.b
        self.assertFalse(self.bot.is_dead)
        self.deliver(b.SetHealthPacket(health=0), self.respawn_packet(b.PlayerRespawnState.SEARCHING_FOR_SPAWN))
        await asyncio.sleep(0)
        self.assertTrue(self.bot.is_dead)
        await self.bot.submit_respawn(timeout=1)
        self.assertEqual((await self.sent(b.RespawnPacket)).state, b.PlayerRespawnState.CLIENT_READY_TO_SPAWN)
        self.assert_no_respawn_packets()

        waiting = asyncio.create_task(self.bot.respawn(timeout=1))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.deliver(self.respawn_packet(b.PlayerRespawnState.READY_TO_SPAWN), b.SetHealthPacket(health=20))
        self.assertEqual((await self.sent(b.PlayerActionPacket)).action, b.PlayerActionType.RESPAWN)
        self.assertFalse(self.bot.is_dead)
        self.assert_no_respawn_packets()

    async def test_auto_respawn_runs_once_per_death_and_ignores_foreign_death_and_dimension(self):
        await self.join(auto_respawn=True)
        b = self.b
        death = asyncio.create_task(self.bot.wait_for_death(timeout=1))
        ready = asyncio.create_task(self.bot.wait_for_respawn(timeout=1))
        await asyncio.sleep(0)
        self.deliver(
            b.ActorEventPacket(runtime_id=43, event_id=b.ActorEvent.DEATH),
            b.ChangeDimensionPacket(dimension_id=1, pos=b.Vec3(x=8.5, y=221.625, z=7.5)),
            self.respawn_packet(b.PlayerRespawnState.READY_TO_SPAWN),
        )
        await asyncio.sleep(0)
        self.assertFalse(death.done())
        self.assertFalse(ready.done())
        self.assert_no_respawn_packets()

        for cycle in range(2):
            with self.subTest(cycle=cycle):
                if cycle:
                    death = asyncio.create_task(self.bot.wait_for_death(timeout=1))
                    ready = asyncio.create_task(self.bot.wait_for_respawn(timeout=1))
                await asyncio.sleep(0)
                self.deliver(self.death_info(), b.SetHealthPacket(health=0),
                             self.respawn_packet(b.PlayerRespawnState.SEARCHING_FOR_SPAWN),
                             self.respawn_packet(b.PlayerRespawnState.SEARCHING_FOR_SPAWN))
                self.assertEqual(await death, DeathEvent("death.attack.generic", ("RespawnProbe",)))
                self.assertTrue(self.bot.is_dead)
                self.assertEqual((await self.sent(b.RespawnPacket)).state, b.PlayerRespawnState.CLIENT_READY_TO_SPAWN)
                self.deliver(self.respawn_packet(b.PlayerRespawnState.READY_TO_SPAWN), b.SetHealthPacket(health=20))
                self.assertEqual(await ready, (8.5, 221.625, 7.5))
                self.assertEqual((await self.sent(b.PlayerActionPacket)).action, b.PlayerActionType.RESPAWN)
                self.assertFalse(self.bot.is_dead)
                self.assert_no_respawn_packets()

    async def test_timeout_keeps_ack_gated_on_ready(self):
        await self.join()
        b = self.b
        state = b.PlayerRespawnState
        self.deliver(self.respawn_packet(state.SEARCHING_FOR_SPAWN))
        await self.bot.packets.wait_for_packet(b.RespawnPacket.ID, timeout=1)
        timeout = asyncio.timeout
        deadline = timeout(None)
        delay = 0.05

        with patch.object(asyncio, "timeout", side_effect=lambda seconds: deadline if seconds == delay else timeout(seconds)):
            pending = asyncio.create_task(self.bot.respawn(timeout=delay))
            try:
                self.assertEqual((await self.sent(b.RespawnPacket)).state, state.CLIENT_READY_TO_SPAWN)
                deadline.reschedule(asyncio.get_running_loop().time())
                with self.assertRaises(TimeoutError):
                    await pending
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        self.assertTrue(deadline.expired())
        self.assert_no_respawn_packets()
        self.deliver(self.respawn_packet(state.READY_TO_SPAWN))
        self.assertEqual((await self.sent(b.PlayerActionPacket)).action, b.PlayerActionType.RESPAWN)

    async def test_cancellation_before_search_and_disconnect_while_waiting(self):
        await self.join()
        cancelled = asyncio.create_task(self.bot.respawn(timeout=1))
        await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        self.deliver(self.respawn_packet(self.b.PlayerRespawnState.SEARCHING_FOR_SPAWN))
        await asyncio.sleep(0)
        self.assert_no_respawn_packets()

        pending = asyncio.create_task(self.bot.respawn(timeout=1))
        await self.sent(self.b.RespawnPacket)
        self.deliver(self.b.DisconnectPacket(), self.respawn_packet(self.b.PlayerRespawnState.READY_TO_SPAWN))
        with self.assertRaises(ConnectionError):
            await pending
        self.assert_no_respawn_packets()

    async def test_disconnect_before_search_completes_without_submission(self):
        await self.join()
        pending = asyncio.create_task(self.bot.respawn(timeout=1))
        await asyncio.sleep(0)
        self.deliver(self.b.DisconnectPacket())
        with self.assertRaises(ConnectionError):
            await pending
        self.assert_no_respawn_packets()

    async def test_close_while_respawn_is_pending_stops_input_before_draining(self):
        self.make_bot(auto_respawn=True)
        b = self.b
        self.deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.deliver(b.StartGamePacket(runtime_id=42, entity_id=7),
                     b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132")

        clock = self.bot._clock
        ticker = self.bot._movement._ticker
        waiting_for_tick = asyncio.Event()
        wait_for_tick = clock.wait_until_tick

        async def observe_wait(tick):
            waiting_for_tick.set()
            return await wait_for_tick(tick)

        state = self.bot._player_state
        drain = state.aclose
        draining, release = asyncio.Event(), asyncio.Event()

        async def pause_drain():
            draining.set()
            await release.wait()
            await drain()

        with patch.object(clock, "wait_until_tick", observe_wait):
            await asyncio.wait_for(waiting_for_tick.wait(), 1)
            self.deliver(self.respawn_packet(b.PlayerRespawnState.SEARCHING_FOR_SPAWN))
            request = await self.sent(b.RespawnPacket)
            self.assertEqual(request.state, b.PlayerRespawnState.CLIENT_READY_TO_SPAWN)
            self.assertFalse(ticker.done())
            with patch.object(state, "aclose", pause_drain):
                closing = asyncio.create_task(self.bot.close())
                try:
                    await asyncio.wait_for(draining.wait(), 1)
                    await asyncio.wait_for(asyncio.gather(ticker, return_exceptions=True), 1)
                finally:
                    release.set()
                    await closing
        self.assert_no_respawn_packets()

    async def test_close_preserves_transport_error_after_movement_stop_error(self):
        await self.join()
        movement = self.bot._movement
        ticker = movement._ticker
        reader = self.bot.packets._reader_task
        close_movement = movement.close
        close_transport = self.transport.close
        movement_failure = RuntimeError("movement stopped with an error")
        transport_failure = OSError("transport closed with an error")

        def fail_after_stopping():
            already_stopping = movement._closing
            close_movement()
            if not already_stopping:
                raise movement_failure

        async def fail_after_transport_close():
            await close_transport()
            raise transport_failure

        with (
            patch.object(movement, "close", fail_after_stopping),
            patch.object(self.transport, "close", side_effect=fail_after_transport_close) as transport_close,
        ):
            with self.assertRaises(OSError) as caught:
                await self.bot.close()
            transport_close.assert_awaited_once()
        self.assertIs(caught.exception, transport_failure)
        self.assertIn(repr(movement_failure), " ".join(transport_failure.__notes__))
        self.assertTrue(reader.done())
        self.assertTrue(ticker.done())

    async def test_join_replays_local_health_and_current_respawn_search_in_order(self):
        for before_spawn in (True, False):
            with self.subTest(before_spawn=before_spawn):
                self.make_bot(auto_respawn=True)
                b = self.b
                state = b.PlayerRespawnState
                timeline = PacketTimeline()
                events = (
                    b.SetHealthPacket(health=0), self.respawn_packet(state.SEARCHING_FOR_SPAWN),
                    self.respawn_packet(state.READY_TO_SPAWN, x=3.5), b.SetHealthPacket(health=20),
                    self.respawn_packet(state.SEARCHING_FOR_SPAWN, runtime_id=43),
                    b.SetHealthPacket(health=0), self.respawn_packet(state.SEARCHING_FOR_SPAWN, x=11.5),
                    b.UpdateAttributesPacket(runtime_id=42, attribute_data=[
                        b.AttributeData(name="minecraft:player.hunger", current_value=6),
                    ]),
                    b.UpdateAttributesPacket(runtime_id=43, attribute_data=[
                        b.AttributeData(name="minecraft:health", current_value=20),
                    ]),
                )
                self.deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
                self.deliver(
                    b.SetHealthPacket(health=19),
                    b.StartGamePacket(runtime_id=42, entity_id=7, block_network_ids_are_hashes=True),
                    *(events if before_spawn else ()), b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
                    *(events if not before_spawn else ()),
                )
                with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
                    await self.bot.join_nethernet("http://127.0.0.1:19132", timeline=timeline)
                if not before_spawn:
                    await asyncio.sleep(0)
                self.assertEqual(self.bot.health, 0)
                self.assertEqual(self.bot.hunger, 6)
                self.assertEqual(self.bot.position, (3.5, 221.625, 7.5))
                self.assertEqual(timeline.received_count("SetHealthPacket"), 4)
                self.assertEqual([entry.raw for entry in timeline.received("RespawnPacket")],
                                 [self.protocol.encode(packet) for packet in events if isinstance(packet, b.RespawnPacket)])
                self.assertEqual((await self.sent(b.RespawnPacket)).state, state.CLIENT_READY_TO_SPAWN)
                self.deliver(self.respawn_packet(state.READY_TO_SPAWN, x=11.5))
                self.assertEqual((await self.sent(b.PlayerActionPacket)).action, b.PlayerActionType.RESPAWN)
                self.assert_no_respawn_packets()
                await self.bot.close()

    async def test_next_search_has_its_own_request_after_previous_ack(self):
        await self.join(auto_respawn=True)
        b = self.b
        state = b.PlayerRespawnState
        send = self.transport.send
        ack_pending, release_ack = asyncio.Event(), asyncio.Event()

        async def pause_first_ack(frame):
            raw = next(read_batch(frame))
            if raw.packet_id == b.PlayerActionPacket.ID and not ack_pending.is_set():
                ack_pending.set()
                await release_ack.wait()
            await send(frame)

        with patch.object(self.transport, "send", side_effect=pause_first_ack):
            first = asyncio.create_task(self.bot.respawn(timeout=2))
            self.deliver(self.respawn_packet(state.SEARCHING_FOR_SPAWN))
            await self.sent(b.RespawnPacket)
            self.deliver(self.respawn_packet(state.READY_TO_SPAWN))
            await asyncio.wait_for(ack_pending.wait(), 1)
            self.deliver(self.respawn_packet(state.SEARCHING_FOR_SPAWN, x=11.5))
            await asyncio.sleep(0)
            second = asyncio.create_task(self.bot.respawn(timeout=2))
            try:
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                self.assert_no_respawn_packets()
                release_ack.set()
                self.assertEqual((await self.sent(b.PlayerActionPacket)).action, b.PlayerActionType.RESPAWN)
                self.assertEqual((await self.sent(b.RespawnPacket)).state, state.CLIENT_READY_TO_SPAWN)
                self.deliver(self.respawn_packet(state.READY_TO_SPAWN, x=11.5))
                self.assertEqual(await second, (11.5, 221.625, 7.5))
                self.assertEqual((await self.sent(b.PlayerActionPacket)).action, b.PlayerActionType.RESPAWN)
            finally:
                release_ack.set()
                second.cancel()
                await asyncio.gather(second, return_exceptions=True)

    async def test_submission_timeout_does_not_limit_later_acknowledgment(self):
        await self.join()
        b = self.b
        state = b.PlayerRespawnState
        self.deliver(self.respawn_packet(state.SEARCHING_FOR_SPAWN))
        await self.bot.packets.wait_for_packet(b.RespawnPacket.ID, timeout=1)
        delay = 1
        await self.bot.submit_respawn(timeout=delay)
        self.assertEqual((await self.sent(b.RespawnPacket)).state, state.CLIENT_READY_TO_SPAWN)
        self.assert_no_respawn_packets()

        loop = asyncio.get_running_loop()
        now = loop.time
        deadline_passed = asyncio.Event()
        loop.call_later(delay, deadline_passed.set)
        with patch.object(loop, "time", lambda: now() + delay + 1):
            await asyncio.wait_for(deadline_passed.wait(), 1)
            self.deliver(self.respawn_packet(state.READY_TO_SPAWN))
            self.assertEqual((await self.sent(b.PlayerActionPacket)).action, b.PlayerActionType.RESPAWN)

    async def test_reader_failure_ends_the_session_owned_ack_wait(self):
        await self.join()
        state = self.b.PlayerRespawnState
        self.deliver(self.respawn_packet(state.SEARCHING_FOR_SPAWN))
        await self.bot.submit_respawn(timeout=1)
        await self.sent(self.b.RespawnPacket)
        waiter = asyncio.create_task(self.bot.respawn(timeout=1))
        self.transport.incoming.put_nowait(ConnectionError("transport lost"))
        with self.assertRaisesRegex(ConnectionError, "packet stream lost"):
            await waiter
        self.assert_no_respawn_packets()
        with self.assertRaisesRegex(ConnectionError, "transport lost"):
            await self.bot.close()


class VitalSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("VitalsProbe", protocol=2225)
        self.b = self.bot.protocol.binding
        self.transport = Transport()
        self.addAsyncCleanup(self.bot.close)

    def deliver(self, *packets):
        self.transport.incoming.put_nowait(write_batch(map(self.bot.protocol.encode, packets)))

    async def join(self, *before_spawn, rules=None):
        b = self.b
        settings = b.LevelSettings(game_rules=self.rules() if rules is None else rules)
        self.deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.deliver(
            b.StartGamePacket(runtime_id=42, entity_id=7, settings=settings),
            *before_spawn, b.PlayStatusPacket(status=3),
        )
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    async def processed(self, *packets):
        observed = asyncio.create_task(self.bot.packets.wait_for_packet(packets[-1].ID, timeout=1))
        await asyncio.sleep(0)
        self.deliver(*packets)
        await observed

    def rules(self, **values):
        b = self.b
        return b.GameRulesChangedPacketData(rules=[
            b.GameRule(name=name, value=value, can_be_modified_by_player=name == "randomTickSpeed")
            for name, value in values.items()
        ])

    def attributes(self, runtime_id=42, health=None, **values):
        b = self.b
        names = {f"minecraft:player.{name}": value for name, value in values.items()}
        if health is not None:
            names["minecraft:health"] = health
        return b.UpdateAttributesPacket(runtime_id=runtime_id, attribute_data=[
            b.AttributeData(name=name, current_value=value) for name, value in names.items()
        ])

    def air(self, runtime_id=42, **values):
        b = self.b
        return b.SetActorDataPacket(id=runtime_id, packed_items=b.SynchedActorData.CopyableDataList(data=[
            b.DataItemEntry(id=int(b.ActorDataIDs[name]), payload=b.DataItemShortPayload(value=value))
            for name, value in values.items()
        ]))

    def effect(self, event="ADD", effect_id=1, runtime_id=42, *, duration=50, amplifier=0):
        packet = self.b.MobEffectPacket
        return packet(runtime_id=runtime_id, event_id=packet.Event[event], effect_id=effect_id,
                      effect_duration_ticks=duration, effect_amplifier=amplifier, show_particles=amplifier == 0,
                      ambient=amplifier > 0)

    async def test_prespawn_vitals_effects_and_world_rules_are_replayed(self):
        b = self.b
        await self.join(
            self.attributes(health=18, hunger=17, saturation=4, level=5, experience=0.5),
            self.attributes(43, level=99), self.attributes(saturation=3),
            self.air(AIR_SUPPLY=270, AIR_SUPPLY_MAX=300), self.air(43, AIR_SUPPLY=2),
            self.air(AIR_SUPPLY=250),
            self.effect(), self.effect("ADD", 7, runtime_id=43), self.effect("REMOVE"),
            self.effect("ADD", 2, duration=25),
            b.GameRulesChangedPacket(rule_data=self.rules(doDaylightCycle=False, randomTickSpeed=4)),
            rules=self.rules(doDayLightCycle=True, doLimitedCrafting=True),
        )
        self.assertEqual(self.bot.vitals, PlayerVitals(18, 17, 3, 250, 300, 5, 0.5))
        self.assertEqual(self.bot.experience_level, self.bot.vitals.experience_level)
        self.assertEqual(self.bot.status_effects, (PlayerEffect(2, 0, 25, True, False),))
        self.assertEqual(self.bot.world_rules, (
            WorldRule("doDaylightCycle", False, False), WorldRule("doLimitedCrafting", True, False),
            WorldRule("randomTickSpeed", 4, True),
        ))
        self.assertIs(self.bot.world_time.do_daylight_cycle, False)
        self.assertIs(self.bot.limited_crafting, True)

    async def test_each_waiter_returns_the_first_matching_sparse_change(self):
        b = self.b
        await self.join(self.effect(duration=1), rules=self.rules(randomTickSpeed=3))
        self.assertEqual(self.bot.vitals, PlayerVitals())
        vitals = asyncio.create_task(self.bot.wait_for_vitals_update(timeout=1))
        effects = asyncio.create_task(self.bot.wait_for_status_effects_update(timeout=1))
        rules = asyncio.create_task(self.bot.wait_for_world_rules_update(timeout=1))
        await self.processed(
            self.attributes(43, saturation=15), self.air(43, AIR_SUPPLY=5),
            self.effect("REMOVE", runtime_id=43), b.SetTimePacket(time=500),
            b.GameRulesChangedPacket(rule_data=self.rules(randomTickSpeed=3)),
        )
        self.assertFalse(any(waiter.done() for waiter in (vitals, effects, rules)))
        self.assertEqual(self.bot.status_effects, (PlayerEffect(1, 0, 1, True, False),))

        await self.processed(
            self.air(AIR_SUPPLY=260), self.attributes(experience=0.25),
            self.effect("UPDATE", duration=45, amplifier=2), self.effect("REMOVE", duration=0),
            b.GameRulesChangedPacket(rule_data=self.rules(randomTickSpeed=4)),
            b.GameRulesChangedPacket(rule_data=self.rules(randomTickSpeed=5)),
        )
        self.assertEqual(await vitals, PlayerVitals(air_supply=260))
        self.assertEqual(self.bot.vitals, PlayerVitals(air_supply=260, experience_progress=0.25))
        self.assertEqual(await effects, (PlayerEffect(1, 2, 45, False, True),))
        self.assertEqual(self.bot.status_effects, ())
        self.assertEqual(await rules, (WorldRule("randomTickSpeed", 4, True),))
        self.assertEqual(self.bot.world_rules, (WorldRule("randomTickSpeed", 5, True),))

    async def test_dimension_and_respawn_follow_the_reported_updates(self):
        b = self.b
        await self.join(
            self.attributes(health=20, saturation=12, level=3, experience=0.25),
            self.air(AIR_SUPPLY=80, AIR_SUPPLY_MAX=300), self.effect(),
        )
        before = self.bot.vitals, self.bot.status_effects
        await self.processed(b.ChangeDimensionPacket(dimension_id=1))
        self.assertEqual((self.bot.vitals, self.bot.status_effects), before)
        await self.processed(b.RespawnPacket(state=b.PlayerRespawnState.SEARCHING_FOR_SPAWN),
                             self.attributes(health=0))
        self.assertEqual(self.bot.status_effects, before[1])
        self.assertEqual(self.bot.vitals, PlayerVitals(health=0, saturation=12, air_supply=80,
                                                     air_supply_max=300, experience_level=3,
                                                     experience_progress=0.25))
        await self.processed(
            b.RespawnPacket(state=b.PlayerRespawnState.READY_TO_SPAWN), self.effect("REMOVE"),
            self.attributes(health=20, saturation=5, level=0, experience=0), self.air(AIR_SUPPLY=300),
        )
        self.assertEqual(self.bot.status_effects, ())
        self.assertEqual(self.bot.vitals, PlayerVitals(health=20, saturation=5, air_supply=300,
                                                     air_supply_max=300, experience_level=0,
                                                     experience_progress=0))

    def scalar_waits(self):
        return (self.bot.wait_for_health_update, self.bot.wait_for_hunger_update,
                self.bot.wait_for_world_time_update, self.bot.wait_for_weather_update)

    async def test_snapshot_updates_before_same_batch_disconnect_are_delivered(self):
        b = self.b
        await self.join(rules=self.rules(randomTickSpeed=3))
        vitals = asyncio.create_task(self.bot.wait_for_vitals_update(timeout=1))
        effects = asyncio.create_task(self.bot.wait_for_status_effects_update(timeout=1))
        rules = asyncio.create_task(self.bot.wait_for_world_rules_update(timeout=1))
        scalar_updates = [asyncio.create_task(wait(timeout=1)) for wait in self.scalar_waits()]
        await self.processed(
            b.SetHealthPacket(health=16), self.attributes(health=15, hunger=12), self.effect(),
            b.GameRulesChangedPacket(rule_data=self.rules(randomTickSpeed=4)),
            b.SetTimePacket(time=25), b.SetTimePacket(time=30), b.LevelEventPacket(event_id=3001),
            b.DisconnectPacket(),
        )
        updates = await asyncio.gather(vitals, effects, rules, return_exceptions=True)
        self.assertEqual(updates, [
            PlayerVitals(health=16), (PlayerEffect(1, 0, 50, True, False),),
            (WorldRule("randomTickSpeed", 4, True),),
        ])
        health, hunger, world_time, weather = await asyncio.gather(*scalar_updates)
        self.assertEqual((health, hunger, world_time.daylight_ticks, weather.raining), (16, 12, 25, True))

    async def test_disconnect_rejects_unmatched_and_new_snapshot_waiters(self):
        b = self.b
        await self.join(rules=self.rules(randomTickSpeed=3))
        waits = (self.bot.wait_for_vitals_update, self.bot.wait_for_status_effects_update,
                 self.bot.wait_for_world_rules_update, *self.scalar_waits())
        pending = [asyncio.create_task(wait(timeout=0.1)) for wait in waits]
        await self.processed(
            self.attributes(43, saturation=4), self.effect(runtime_id=43),
            b.GameRulesChangedPacket(rule_data=self.rules(randomTickSpeed=3)),
            b.DisconnectPacket(), self.attributes(health=14, hunger=12), b.SetTimePacket(time=500),
            b.LevelEventPacket(event_id=3001),
        )
        pending_errors = await asyncio.gather(*pending, return_exceptions=True)
        self.assertEqual(self.bot.world_time.daylight_ticks, 500)
        self.assertEqual(self.bot.weather, Weather(True, False, None, 0.0))
        self.assertFalse(self.bot.packets.start_receiving().done())
        new_errors = await asyncio.gather(*(wait(timeout=0.1) for wait in waits), return_exceptions=True)
        for error in (*pending_errors, *new_errors):
            self.assertIsInstance(error, ConnectionError)

    async def test_snapshot_waiters_keep_reader_failure_cause(self):
        await self.join()
        pending = [asyncio.create_task(wait(timeout=1)) for wait in (
            self.bot.wait_for_vitals_update, self.bot.wait_for_status_effects_update,
            self.bot.wait_for_world_rules_update, *self.scalar_waits(),
        )]
        await asyncio.sleep(0)
        failure = ConnectionError("test transport lost")
        self.transport.incoming.put_nowait(failure)
        for error in await asyncio.gather(*pending, return_exceptions=True):
            self.assertIsInstance(error, ConnectionError)
            self.assertIs(error.__cause__, failure)
        for wait in self.scalar_waits():
            with self.assertRaises(ConnectionError) as raised:
                await wait(timeout=1)
            self.assertIs(raised.exception.__cause__, failure)
        with self.assertRaisesRegex(ConnectionError, "test transport lost"):
            await self.bot.close()

    async def test_scalar_waits_timeout_or_cancel_independently_on_live_connection(self):
        await self.join()
        packets = self.bot.packets
        observers = len(packets._subscribers)
        waits = self.scalar_waits()
        done = await asyncio.gather(*(wait(timeout=0) for wait in waits), return_exceptions=True)
        self.assertTrue(all(isinstance(error, TimeoutError) for error in done))
        pending = [asyncio.create_task(wait(timeout=1)) for wait in waits]
        await asyncio.sleep(0)
        for task in pending:
            task.cancel()
        self.assertTrue(all(isinstance(error, asyncio.CancelledError)
                            for error in await asyncio.gather(*pending, return_exceptions=True)))
        self.assertEqual(len(packets._subscribers), observers)
        self.assertIsNone(self.bot.session_end)
