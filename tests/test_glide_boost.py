import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import AsyncMock, patch

from endpilot import Bot, GlideBoostEvent
from endpilot.protocol import RawPacket, write_batch
from test_scheduled_input import ManualClock, Wire


# Vanilla 2225 reports for runtime ID 1 from three independent air-rocket runs.
STOCK_BOOSTS = {
    25: RawPacket(318, bytes.fromhex("010032af01")),
    28: RawPacket(318, bytes.fromhex("010038ff01")),
    30: RawPacket(318, bytes.fromhex("01003cae01")),
}


class GlideBoostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = ManualClock()
        self.enterContext(self.clock.patch_scheduler())

    async def join(self, bot=None, *, runtime_id=13, before_spawn=()):
        if bot is None:
            bot = Bot("Glide")
            self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding
        wire = Wire(bot.protocol)
        wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.push_batch(wire,
            b.StartGamePacket(runtime_id=runtime_id, entity_id=-27),
            *before_spawn,
            b.PlayStatusPacket(status=3),
        )
        with patch("endpilot.transport.nethernet.connect", new=AsyncMock(return_value=wire)):
            await bot.join_nethernet("http://127.0.0.1:20882")
        return bot, wire

    def push_batch(self, wire, *packets):
        wire.incoming.put_nowait(write_batch(
            packet if isinstance(packet, RawPacket) else wire.protocol.encode(packet) for packet in packets
        ))

    async def deliver(self, bot, wire, *packets):
        marker = wire.protocol.binding.SetTimePacket(time=23)
        waiting = asyncio.create_task(bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.push_batch(wire, *packets, marker)
        await waiting

    def effect(self, wire, *, target=13, effect="GLIDE_BOOST", duration=25, tick=175):
        b = wire.protocol.binding
        return b.MovementEffectPacket(runtime_id=target, effect_type=b.MovementEffectType[effect],
                                      effect_duration=duration, tick=tick)

    async def test_estimate_uses_executed_ticks_from_the_last_original_report(self):
        bot, wire = await self.join(runtime_id=1)
        self.assertIsNone(bot.estimated_glide_boost_ticks_remaining)
        await self.clock.wake_at(50)
        await self.clock.wake_at(100)
        waiting = asyncio.create_task(bot.wait_for_glide_boost(timeout=1))
        await asyncio.sleep(0)
        await self.deliver(bot, wire, STOCK_BOOSTS[25])
        self.assertEqual(await waiting, GlideBoostEvent(1, 25, 175))
        self.assertEqual(bot.get_tick(), 2)
        self.assertEqual(bot.estimated_glide_boost_ticks_remaining, 25)

        # A delayed event-loop turn skips clock deadlines without executing ticks.
        await self.clock.wake_at(270)
        self.assertEqual(bot.get_tick(), 2)
        self.assertEqual(bot.estimated_glide_boost_ticks_remaining, 25)
        for milliseconds in (300, 350, 400, 450):
            await self.clock.wake_at(milliseconds)
        self.assertEqual(bot.estimated_glide_boost_ticks_remaining, 21)

        waiting = asyncio.create_task(bot.wait_for_glide_boost(timeout=1))
        await asyncio.sleep(0)
        await self.deliver(bot, wire, STOCK_BOOSTS[30], STOCK_BOOSTS[28])
        self.assertEqual(await waiting, GlideBoostEvent(1, 30, 174))
        self.assertEqual(bot.estimated_glide_boost_ticks_remaining, 28)
        for milliseconds in range(500, 1850, 50):
            await self.clock.wake_at(milliseconds)
        self.assertEqual(bot.estimated_glide_boost_ticks_remaining, 1)
        await self.clock.wake_at(1850)
        await self.clock.wake_at(1900)
        self.assertEqual(bot.estimated_glide_boost_ticks_remaining, 0)
        sent = len(wire.sent)
        self.assertEqual(bot.estimated_glide_boost_ticks_remaining, 0)
        self.assertEqual(len(wire.sent), sent)

    async def test_estimate_ignores_unknown_flags_and_ends_on_confirmed_own_non_glide(self):
        bot, wire = await self.join(runtime_id=1)
        b = bot.protocol.binding
        # Original low flags for glide start and end, and a valid high-only report.
        start = RawPacket(39, bytes.fromhex("01020007078080c280a08380033603039a99193f0000f601"))
        stop = RawPacket(39, bytes.fromhex("01040007078080c280808280033603036666e63f5c0707808880a08080108b010707000000a402"))
        high_only = RawPacket(39, bytes.fromhex("01015c0707808c80a0808010000060"))
        unknown_low = b.SetActorDataPacket(id=1, packed_items=b.SynchedActorData.CopyableDataList(data=[
            b.DataItemEntry(id=0, payload=b.DataItemIntPayload(value=0)),
        ]))
        other_player_stop = b.SetActorDataPacket(id=2, packed_items=bot.protocol.decode(stop).packed_items)
        rocket = b.AddActorPacket(runtime_id=3, entity_id=-4294967293, actor_type="minecraft:fireworks_rocket")
        despawn = b.RemoveActorPacket(entity_id=-4294967293)

        await self.deliver(bot, wire, stop, rocket, despawn, self.effect(wire, target=2),
                           self.effect(wire, target=1, effect="DOLPHIN_BOOST"))
        self.assertIsNone(bot.estimated_glide_boost_ticks_remaining)
        await self.deliver(bot, wire, start, STOCK_BOOSTS[28])
        await self.clock.wake_at(50)
        await self.deliver(bot, wire, rocket, despawn, other_player_stop, high_only, unknown_low)
        self.assertEqual(bot.estimated_glide_boost_ticks_remaining, 27)
        await self.deliver(bot, wire, stop)
        self.assertEqual(bot.estimated_glide_boost_ticks_remaining, 0)
        await self.deliver(bot, wire, start)
        self.assertEqual(bot.estimated_glide_boost_ticks_remaining, 0)
        await self.deliver(bot, wire, STOCK_BOOSTS[30])
        self.assertEqual(bot.estimated_glide_boost_ticks_remaining, 30)

    async def test_estimate_resets_for_dimension_respawn_and_rejoin(self):
        bot, wire = await self.join(runtime_id=1)
        b = bot.protocol.binding
        await self.deliver(bot, wire, STOCK_BOOSTS[25])
        self.assertEqual(bot.estimated_glide_boost_ticks_remaining, 25)

        sending = asyncio.Event()
        release = asyncio.Event()

        async def hold_loading_start():
            sending.set()
            await release.wait()

        wire.before_send = hold_loading_start
        transition = b.ChangeDimensionPacket(dimension_id=1, loading_screen_id=5)
        delivering = asyncio.create_task(self.deliver(bot, wire, transition))
        self.addCleanup(delivering.cancel)
        await asyncio.wait_for(sending.wait(), 1)
        self.assertIsNone(bot.estimated_glide_boost_ticks_remaining)
        release.set()
        await delivering
        wire.before_send = None
        await self.deliver(bot, wire, STOCK_BOOSTS[28])
        self.assertIsNone(bot.estimated_glide_boost_ticks_remaining)
        await self.deliver(bot, wire, b.PlayerActionPacket(runtime_id=1, action=b.PlayerActionType.CHANGE_DIMENSION_ACK))
        self.assertIsNone(bot.estimated_glide_boost_ticks_remaining)

        searching = b.PlayerRespawnState.SEARCHING_FOR_SPAWN
        ready = b.PlayerRespawnState.READY_TO_SPAWN
        await self.deliver(bot, wire, STOCK_BOOSTS[30], b.RespawnPacket(runtime_id=2, state=searching))
        self.assertEqual(bot.estimated_glide_boost_ticks_remaining, 30)
        await self.deliver(bot, wire, b.ActorEventPacket(runtime_id=1, event_id=b.ActorEvent.DEATH),
                           b.RespawnPacket(runtime_id=0, state=searching), STOCK_BOOSTS[25])
        self.assertIsNone(bot.estimated_glide_boost_ticks_remaining)
        await self.deliver(bot, wire, b.RespawnPacket(runtime_id=0, state=ready),
                           b.ActorEventPacket(runtime_id=1, event_id=b.ActorEvent.SPAWN_ALIVE))
        self.assertIsNone(bot.estimated_glide_boost_ticks_remaining)
        await self.deliver(bot, wire, STOCK_BOOSTS[28])
        self.assertEqual(bot.estimated_glide_boost_ticks_remaining, 28)

        self.push_batch(wire, b.DisconnectPacket(), STOCK_BOOSTS[30])
        await bot.wait_for_session_end(timeout=1)
        self.assertIsNone(bot.estimated_glide_boost_ticks_remaining)
        await bot.close()
        self.assertIsNone(bot.estimated_glide_boost_ticks_remaining)
        _, new_wire = await self.join(bot, runtime_id=1)
        self.assertIsNone(bot.estimated_glide_boost_ticks_remaining)
        await self.deliver(bot, new_wire, STOCK_BOOSTS[25])
        self.assertEqual(bot.estimated_glide_boost_ticks_remaining, 25)
        await bot.close()
        self.assertIsNone(bot.estimated_glide_boost_ticks_remaining)

    async def test_estimate_is_unavailable_before_spawn_and_on_legacy_protocol(self):
        bot = Bot("LateSpawn")
        self.addAsyncCleanup(bot.close)
        self.assertIsNone(bot.estimated_glide_boost_ticks_remaining)
        await self.join(bot, runtime_id=1, before_spawn=(STOCK_BOOSTS[25],))
        self.assertIsNone(bot.estimated_glide_boost_ticks_remaining)

        legacy = Bot("LegacyGlide", protocol=2193)
        self.addAsyncCleanup(legacy.close)
        _, wire = await self.join(legacy, runtime_id=1)
        waiting = asyncio.create_task(legacy.wait_for_glide_boost(timeout=1))
        await asyncio.sleep(0)
        await self.deliver(legacy, wire, STOCK_BOOSTS[25])
        self.assertEqual(await waiting, GlideBoostEvent(1, 25, 175))
        self.assertIsNone(legacy.estimated_glide_boost_ticks_remaining)

    async def test_public_wait_returns_the_next_own_glide_boost_in_wire_order(self):
        bot, wire = await self.join()
        b = bot.protocol.binding
        old = asyncio.create_task(bot.packets.wait_for_packet(b.MovementEffectPacket.ID, timeout=1))
        await asyncio.sleep(0)
        wire.push(self.effect(wire, duration=12, tick=100))
        await old

        waiting = asyncio.create_task(bot.wait_for_glide_boost(timeout=1))
        await asyncio.sleep(0)
        self.push_batch(wire, self.effect(wire, target=99), self.effect(wire, effect="DOLPHIN_BOOST"))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())

        self.push_batch(wire, self.effect(wire), self.effect(wire, duration=30, tick=177))
        observed = await waiting
        self.assertEqual(observed, GlideBoostEvent(runtime_id=13, duration_ticks=25, last_handled_input_tick=175))
        with self.assertRaises(FrozenInstanceError):
            observed.duration_ticks = 1

        same = asyncio.create_task(bot.wait_for_glide_boost(timeout=1))
        await asyncio.sleep(0)
        wire.push(self.effect(wire))
        repeated = await same
        self.assertEqual(repeated, observed)
        self.assertIsNot(repeated, observed)

    async def test_timeout_and_cancellation_leave_later_waits_usable(self):
        bot, wire = await self.join()
        with self.assertRaises(TimeoutError):
            await bot.wait_for_glide_boost(timeout=0)
        cancelled = asyncio.create_task(bot.wait_for_glide_boost())
        await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled

        waiting = asyncio.create_task(bot.wait_for_glide_boost(timeout=1))
        await asyncio.sleep(0)
        wire.push(self.effect(wire, duration=0, tick=0))
        self.assertEqual(await waiting, GlideBoostEvent(13, 0, 0))

    async def test_effect_before_disconnect_is_delivered_and_later_waits_fail(self):
        bot, wire = await self.join()
        b = wire.protocol.binding
        waiting = asyncio.create_task(bot.wait_for_glide_boost(timeout=1))
        await asyncio.sleep(0)
        self.push_batch(wire, self.effect(wire), b.DisconnectPacket())
        self.assertEqual(await waiting, GlideBoostEvent(13, 25, 175))
        with self.assertRaises(ConnectionError):
            await bot.wait_for_glide_boost(timeout=1)

    async def test_disconnect_terminates_unmatched_and_pending_waits(self):
        bot, wire = await self.join()
        b = wire.protocol.binding
        waiting = asyncio.create_task(bot.wait_for_glide_boost(timeout=1))
        await asyncio.sleep(0)
        self.push_batch(wire, self.effect(wire, target=99), b.DisconnectPacket(), self.effect(wire))
        with self.assertRaises(ConnectionError):
            await waiting

    async def test_transport_loss_preserves_the_existing_failure_cause(self):
        bot, wire = await self.join()
        await self.deliver(bot, wire, self.effect(wire))
        self.assertEqual(bot.estimated_glide_boost_ticks_remaining, 25)
        waiting = asyncio.create_task(bot.wait_for_glide_boost(timeout=1))
        await asyncio.sleep(0)
        failure = OSError("glide observation transport lost")
        wire.incoming.put_nowait(failure)
        with self.assertRaises(ConnectionError) as stopped:
            await waiting
        self.assertIs(stopped.exception.__cause__, failure)
        self.assertIsNone(bot.estimated_glide_boost_ticks_remaining)
        with self.assertRaises(OSError):
            await bot.close()
