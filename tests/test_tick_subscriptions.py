import asyncio
import gc
import unittest
from unittest.mock import AsyncMock, patch
from weakref import ref

from endpilot import Bot
from endpilot.client_clock import ClientClock

from test_scheduled_input import ManualClock, Wire


class ClientClockSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler = ManualClock()
        self.enterContext(self.scheduler.patch_scheduler())
        self.clock = ClientClock()
        self.addAsyncCleanup(self.clock.close)
        self.clock.start()

    async def test_notifies_only_executed_ticks_before_waiters_resume(self):
        clock = self.clock
        await self.scheduler.wake_at(50)
        seen = []
        clock.subscribe_ticks(lambda tick: seen.append(
            ("callback", tick, clock.get_tick(), clock.deadline_ns),
        ))

        async def wait():
            seen.append(("waiter", await clock.wait_ticks(1)))

        waiting = asyncio.create_task(wait())
        self.addCleanup(waiting.cancel)
        await self.scheduler.wake_at(170)
        self.assertEqual(seen, [])
        self.assertFalse(waiting.done())
        self.assertEqual(clock.get_tick(), 1)
        await self.scheduler.wake_at(200)
        await asyncio.wait_for(waiting, 1)
        await self.scheduler.wake_at(253)
        self.assertEqual(seen, [
            ("callback", 2, 2, 200_000_000),
            ("waiter", 2),
            ("callback", 3, 3, 250_000_000),
        ])

    async def test_late_tick_gives_waiter_a_turn_before_the_next_due_tick(self):
        clock = self.clock
        seen = []

        def callback(tick):
            seen.append(("callback", tick, clock.deadline_ns))
            if tick == 1:
                self.scheduler.now_ns += 2_000_000

        async def wait():
            seen.append(("waiter", await clock.wait_ticks(1), clock.deadline_ns))

        clock.subscribe_ticks(callback)
        waiting = asyncio.create_task(wait())
        self.addCleanup(waiting.cancel)
        await self.scheduler.wake_at(99)
        await asyncio.wait_for(waiting, 1)
        self.assertEqual(seen, [
            ("callback", 1, 50_000_000),
            ("waiter", 1, 50_000_000),
            ("callback", 2, 100_000_000),
        ])

    async def test_repeated_overruns_give_ready_close_a_turn_without_tail_notifications(self):
        clock = self.clock
        ready_to_close = asyncio.Event()
        seen = []

        def callback(tick):
            seen.append(("overrun", tick))
            ready_to_close.set()
            if tick < 5:
                self.scheduler.now_ns += 50_000_000

        async def close_when_ready():
            await ready_to_close.wait()
            await clock.close()

        clock.subscribe_ticks(callback)
        clock.subscribe_ticks(lambda tick: seen.append(("last", tick)))
        closing = asyncio.create_task(close_when_ready())
        self.addCleanup(closing.cancel)
        await self.scheduler.wake_at(50)
        await asyncio.wait_for(closing, 1)
        self.assertEqual(seen, [("overrun", 1), ("last", 1)])
        with self.assertRaisesRegex(ConnectionError, "clock closed"):
            clock.get_tick()

    async def test_registrations_are_independent_and_mutations_apply_in_order(self):
        subscribe = self.clock.subscribe_ticks
        seen = []

        def first(tick):
            seen.append(("first", tick))
            remove_first()
            remove_repeated()
            remove_repeated()
            subscribe(lambda next_tick: seen.append(("added", next_tick)))

        def repeated(tick):
            seen.append(("repeated", tick))

        remove_first = subscribe(first)
        remove_repeated = subscribe(repeated)
        keep_repeated = subscribe(repeated)
        subscribe(lambda tick: seen.append(("last", tick)))
        await self.scheduler.wake_at(50)
        await self.scheduler.wake_at(100)
        keep_repeated()
        await self.scheduler.wake_at(150)
        self.assertEqual(seen, [
            ("first", 1), ("repeated", 1), ("last", 1),
            ("repeated", 2), ("last", 2), ("added", 2),
            ("last", 3), ("added", 3),
        ])

    async def test_exceptions_are_reported_and_clock_continues(self):
        clock = self.clock
        loop = asyncio.get_running_loop()
        reports, seen = [], []
        self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
        loop.set_exception_handler(lambda _, context: reports.append(context))

        def broken(tick):
            raise ValueError(tick)

        def cancelled(tick):
            raise asyncio.CancelledError(tick)

        clock.subscribe_ticks(broken)
        clock.subscribe_ticks(cancelled)
        clock.subscribe_ticks(seen.append)
        await self.scheduler.wake_at(50)
        await self.scheduler.wake_at(100)
        self.assertEqual(await clock.wait_ticks(0), 2)
        self.assertEqual(seen, [1, 2])
        self.assertEqual([
            (context["callback"], type(context["exception"]), context["exception"].args)
            for context in reports
        ], [
            (broken, ValueError, (1,)), (cancelled, asyncio.CancelledError, (1,)),
            (broken, ValueError, (2,)), (cancelled, asyncio.CancelledError, (2,)),
        ])

    async def test_stopping_from_a_callback_ends_dispatch_and_keeps_the_failure(self):
        clock = self.clock
        failure = OSError("clock transport failed")
        seen = []

        def stop(tick):
            seen.append(("stop", tick))
            clock.stop(failure)
            self.scheduler.now_ns = 125_000_000

        clock.subscribe_ticks(stop)
        clock.subscribe_ticks(lambda tick: seen.append(("last", tick)))
        waiting = asyncio.create_task(clock.wait_ticks(2))
        self.addCleanup(waiting.cancel)
        await self.scheduler.wake_at(50)
        with self.assertRaises(OSError) as raised:
            await asyncio.wait_for(waiting, 1)
        self.assertIs(raised.exception, failure)
        self.assertEqual(seen, [("stop", 1)])
        self.assertEqual((clock.tick, clock.deadline_ns), (1, 50_000_000))
        with self.assertRaisesRegex(RuntimeError, "no active connection"):
            clock.subscribe_ticks(seen.append)

    async def test_stop_releases_callbacks_and_saved_handle_does_not_own_clock(self):
        clock = ClientClock()
        clock.start()
        try:
            def callback(_):
                pass

            callback_ref = ref(callback)
            unsubscribe = clock.subscribe_ticks(callback)
            del callback
            self.assertIsNotNone(callback_ref())
            clock.stop()
            gc.collect()
            self.assertIsNone(callback_ref())
        finally:
            await clock.close()
        owner_ref = ref(clock)
        del clock
        # Let asyncio clear the task's cancellation traceback.
        await asyncio.sleep(0)
        gc.collect()
        self.assertIsNone(owner_ref())
        unsubscribe()
        unsubscribe()


class ConnectionTickSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_loading_and_play_share_subscriptions_then_rejoin_starts_fresh(self):
        bot = Bot("TickObserver")
        self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding
        wire = Wire(bot.protocol)
        scheduler = ManualClock()
        connecting, transport_ready = asyncio.Event(), asyncio.Event()
        first, second, continuing = [], [], []

        with self.assertRaisesRegex(RuntimeError, "no active connection"):
            bot.subscribe_ticks(first.append)

        async def connect(*args, **kwargs):
            connecting.set()
            await transport_ready.wait()
            return wire

        def prepare(wire):
            wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
            wire.push(b.StartGamePacket(runtime_id=4, entity_id=17))

        prepare(wire)
        with scheduler.patch_scheduler(), patch("endpilot.transport.nethernet.connect", side_effect=connect):
            joining = asyncio.create_task(bot.join_nethernet("http://127.0.0.1:20232"))
            self.addCleanup(joining.cancel)
            await asyncio.wait_for(connecting.wait(), 1)
            with self.assertRaisesRegex(RuntimeError, "no active connection"):
                bot.subscribe_ticks(first.append)
            transport_ready.set()
            await scheduler.wake_at(50)
            self.assertFalse(joining.done())
            old_unsubscribe = bot.subscribe_ticks(lambda tick: first.append((tick, bot.get_tick())))
            self.assertEqual(first, [])
            await scheduler.wake_at(100)
            self.assertFalse(joining.done())
            self.assertEqual(first, [(2, 2)])
            wire.push(b.PlayStatusPacket(status=3))
            await asyncio.wait_for(joining, 1)
            await scheduler.wake_at(150)
            self.assertEqual(first, [(2, 2), (3, 3)])
            await bot.close()
            self.assertTrue(wire.closed)
            with self.assertRaisesRegex(RuntimeError, "no active connection"):
                bot.subscribe_ticks(first.append)

        fresh = Wire(bot.protocol)
        prepare(fresh)
        fresh.push(b.PlayStatusPacket(status=3))
        scheduler = ManualClock()
        with (
            scheduler.patch_scheduler(),
            patch("endpilot.transport.nethernet.connect", new=AsyncMock(return_value=fresh)),
        ):
            await bot.join_nethernet("http://127.0.0.1:20232")
            self.assertEqual(bot.get_tick(), 0)
            unsubscribe = bot.subscribe_ticks(second.append)
            bot.subscribe_ticks(continuing.append)
            old_unsubscribe()
            await scheduler.wake_at(50)
            self.assertEqual(await bot.wait_ticks(0), 1)
            unsubscribe()
            unsubscribe()
            await scheduler.wake_at(100)
            self.assertEqual(second, [1])
            self.assertEqual(continuing, [1, 2])
            self.assertEqual(first, [(2, 2), (3, 3)])

            waiting = asyncio.create_task(bot.wait_ticks(1))
            self.addCleanup(waiting.cancel)
            await asyncio.sleep(0)
            fresh.push(b.DisconnectPacket())
            with self.assertRaisesRegex(ConnectionError, "clock closed"):
                await asyncio.wait_for(waiting, 1)
            with self.assertRaisesRegex(RuntimeError, "no active connection"):
                bot.subscribe_ticks(continuing.append)
            await bot.close()
            self.assertTrue(fresh.closed)
