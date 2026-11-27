import asyncio
from functools import partial
from inspect import CORO_CREATED, getcoroutinestate
import unittest
from weakref import ref

from endpilot import Bot


class PlayerPluginTests(unittest.TestCase):
    def test_identity_and_lifetime_are_per_bot(self):
        first_bot, second_bot = Bot(), Bot()
        calls = []

        class Plugin:
            def __init__(self, name):
                self.name = name

            def __call__(self, bot):
                calls.append((self.name, bot))

            def __eq__(self, other):
                return isinstance(other, Plugin)

        first, equal = Plugin("first"), Plugin("equal")
        self.assertEqual(first, equal)
        self.assertIsNone(first_bot.load_plugin(first))
        self.assertFalse(first_bot.has_plugin(equal))
        self.assertFalse(first_bot.has_plugin(None))
        self.assertFalse(second_bot.has_plugin(first))
        self.assertIsNone(first_bot.load_plugins(iter((equal, first, equal))))
        second_bot.load_plugin(first)
        self.assertEqual(calls, [("first", first_bot), ("equal", first_bot), ("first", second_bot)])

        retained = ref(equal)
        del equal
        self.assertIsNotNone(retained())
        self.assertTrue(first_bot.has_plugin(retained()))
        self.assertFalse(second_bot.has_plugin(retained()))

    def test_self_recursion_and_mutual_dependencies_share_registration(self):
        bot = Bot()
        calls = []

        def outer(player):
            calls.append("outer")
            self.assertTrue(player.has_plugin(outer))
            player.load_plugin(outer)
            player.load_plugin(inner)

        def inner(player):
            calls.append("inner")
            self.assertTrue(player.has_plugin(inner))
            player.load_plugins([outer, inner])

        bot.load_plugins([outer, inner, outer])
        self.assertEqual(calls, ["outer", "inner"])

    def test_batch_validates_every_member_before_installing(self):
        bot = Bot()
        calls = []

        async def async_plugin(player):
            calls.append(player)

        async def async_generator(player):
            yield player

        class AsyncPlugin:
            async def __call__(self, player):
                calls.append(player)

        for invalid in ("module.name", async_plugin, async_generator, AsyncPlugin(), partial(AsyncPlugin())):
            with self.subTest(invalid=invalid):
                with self.assertRaises(TypeError):
                    bot.load_plugins(iter((calls.append, invalid)))
        self.assertEqual(calls, [])

        def broken_iterable():
            yield calls.append
            raise LookupError("iteration failed")

        with self.assertRaisesRegex(LookupError, "iteration failed"):
            bot.load_plugins(broken_iterable())
        self.assertEqual(calls, [])

    def test_batch_rejects_partial_with_async_call_override_before_installing(self):
        bot = Bot()
        calls = []

        class AsyncPartial(partial):
            async def __call__(self, player):
                calls.append(player)

        plugin = AsyncPartial(calls.append)
        with self.assertRaisesRegex(TypeError, "must be synchronous"):
            bot.load_plugins([calls.append, plugin])
        self.assertEqual(calls, [])
        self.assertFalse(bot.has_plugin(plugin))

    def test_partial_with_sync_call_override_can_wrap_an_async_function(self):
        bot = Bot()
        calls = []

        async def wrapped(player):
            self.fail("the override should handle the call")

        class SyncPartial(partial):
            def __call__(self, player):
                calls.append(player)

        plugin = SyncPartial(wrapped)
        bot.load_plugin(plugin)
        self.assertEqual(calls, [bot])
        self.assertTrue(bot.has_plugin(plugin))

    def test_failed_install_can_retry_and_keeps_successful_prefix(self):
        bot = Bot()
        calls = []
        failure = RuntimeError("installation failed")

        def first(player):
            calls.append("first")

        def retryable(player):
            calls.append("retryable")
            if calls.count("retryable") == 1:
                raise failure

        def last(player):
            calls.append("last")

        with self.assertRaises(RuntimeError) as raised:
            bot.load_plugins([first, retryable, last])
        self.assertIs(raised.exception, failure)
        self.assertTrue(bot.has_plugin(first))
        self.assertFalse(bot.has_plugin(retryable))
        self.assertFalse(bot.has_plugin(last))
        bot.load_plugins([first, retryable, last])
        self.assertEqual(calls, ["first", "retryable", "retryable", "last"])
        self.assertTrue(bot.has_plugin(retryable))

    def test_rejected_coroutine_stays_unstarted_and_open(self):
        bot = Bot()

        async def work():
            return 7

        coroutine = work()

        def plugin(player):
            return coroutine

        try:
            with self.assertRaisesRegex(TypeError, "returned an awaitable"):
                bot.load_plugin(plugin)
            self.assertFalse(bot.has_plugin(plugin))
            self.assertEqual(getcoroutinestate(coroutine), CORO_CREATED)
        finally:
            coroutine.close()


class PlayerPluginAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_returned_coroutine_remains_available_to_its_existing_task(self):
        bot = Bot()

        async def work():
            return 7

        coroutine = work()
        task = asyncio.create_task(coroutine)

        def plugin(player):
            return coroutine

        try:
            self.assertEqual(getcoroutinestate(coroutine), CORO_CREATED)
            with self.assertRaisesRegex(TypeError, "returned an awaitable"):
                bot.load_plugin(plugin)
            self.assertFalse(bot.has_plugin(plugin))
            self.assertEqual(await task, 7)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_returned_future_and_task_remain_owned_by_the_caller(self):
        bot = Bot()
        future = asyncio.get_running_loop().create_future()
        event = asyncio.Event()
        task = asyncio.create_task(event.wait())
        try:
            for result in (future, task):
                with self.subTest(result=result):
                    def plugin(player):
                        return result

                    with self.assertRaisesRegex(TypeError, "returned an awaitable"):
                        bot.load_plugin(plugin)
                    self.assertFalse(bot.has_plugin(plugin))
            self.assertFalse(future.done())
            future.set_result("done")
            self.assertEqual(await future, "done")
            event.set()
            self.assertTrue(await task)
        finally:
            future.cancel()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
