import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import AsyncMock, patch

from endpilot import Bot, GlideBoostEvent
from endpilot.protocol import write_batch
from test_scheduled_input import Wire


class GlideBoostTests(unittest.IsolatedAsyncioTestCase):
    async def join(self):
        bot = Bot("Glide")
        self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding
        wire = Wire(bot.protocol)
        for packet in (
            b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
            b.StartGamePacket(runtime_id=13, entity_id=-27),
            b.PlayStatusPacket(status=3),
        ):
            wire.push(packet)
        with patch("endpilot.transport.nethernet.connect", new=AsyncMock(return_value=wire)):
            await bot.join_nethernet("http://127.0.0.1:20882")
        return bot, wire

    def push_batch(self, wire, *packets):
        wire.incoming.put_nowait(write_batch([wire.protocol.encode(packet) for packet in packets]))

    def effect(self, wire, *, target=13, effect="GLIDE_BOOST", duration=25, tick=175):
        b = wire.protocol.binding
        return b.MovementEffectPacket(runtime_id=target, effect_type=b.MovementEffectType[effect],
                                      effect_duration=duration, tick=tick)

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
        waiting = asyncio.create_task(bot.wait_for_glide_boost(timeout=1))
        await asyncio.sleep(0)
        failure = OSError("glide observation transport lost")
        wire.incoming.put_nowait(failure)
        with self.assertRaises(ConnectionError) as stopped:
            await waiting
        self.assertIs(stopped.exception.__cause__, failure)
        with self.assertRaises(OSError):
            await bot.close()
