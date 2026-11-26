"""Both form examples share a real Bot's request lifecycle."""

import asyncio

from examples.form_extension.src.endpilot_form_extension.mcp_server import FormPlayer
from examples.respond_form import answer_next_action_form
from test_forms import ACTION, CONFIRMATION, FormTestCase


class FormExtensionTests(FormTestCase):
    async def watch_example(self, button_index):
        task = asyncio.create_task(answer_next_action_form(self.bot, button_index, timeout=1))
        await asyncio.sleep(0)
        return task

    async def test_example_submission_consumes_the_bot_request(self):
        await self.join()
        pending = await self.watch_example(1)
        await self.deliver(self.request(73, {**ACTION, "title": "Choose"}))
        self.assertEqual(await pending, {"form_id": 73, "title": "Choose", "button_index": 1})
        self.assertIsNone(self.bot.get_pending_form(73))
        with self.assertRaisesRegex(RuntimeError, "no form"):
            await self.bot.answer_form(0, 73)
        self.assert_responses((73, "1", None))

    async def test_example_rejects_other_forms_and_invalid_buttons(self):
        await self.join()
        pending = await self.watch_example(1)
        await self.deliver(self.request(3, CONFIRMATION))
        with self.assertRaisesRegex(ValueError, "expected a server action form"):
            await pending

        pending = await self.watch_example(2)
        await self.deliver(self.request(4, ACTION))
        with self.assertRaisesRegex(ValueError, "button index"):
            await pending
        self.assertIsNotNone(self.bot.get_pending_form(4))
        self.assert_responses()

    async def test_example_rejects_an_identical_replacement_for_its_observed_request(self):
        await self.join()
        pending = await self.watch_example(1)
        request = self.request(9, ACTION)
        await self.deliver(request, request)
        with self.assertRaisesRegex(RuntimeError, "no longer pending"):
            await pending
        self.assertEqual(self.bot.get_pending_form(9), {"form_id": 9, "form": ACTION})
        self.assert_responses()

    async def test_example_send_failure_leaves_the_request_available_for_retry(self):
        await self.join()
        self.transport.fail_response = True
        pending = await self.watch_example(1)
        await self.deliver(self.request(12, ACTION))
        with self.assertRaisesRegex(OSError, "transport failed"):
            await pending
        self.assertIsNotNone(self.bot.get_pending_form(12))
        self.assert_responses()

        self.transport.fail_response = False
        await self.bot.answer_form(1, 12)
        self.assertIsNone(self.bot.get_pending_form(12))
        self.assert_responses((12, "1", None))

    async def test_mcp_retries_failed_send_then_consumes_the_bot_request(self):
        await self.join()
        player = FormPlayer()
        player.bot = self.bot
        self.addAsyncCleanup(player.disconnect)
        self.assertEqual(await player.watch_form(timeout=1), {"watching": True})
        await self.deliver(self.request(19, {**ACTION, "title": "Prompt"}))
        form = await player.read_form()
        self.assertEqual((form["form_id"], form["button_count"]), (19, 2))
        with self.assertRaisesRegex(RuntimeError, "matching"):
            await player.answer_form(20, 1)
        with self.assertRaisesRegex(ValueError, "button index"):
            await player.answer_form(19, 2)

        self.transport.fail_response = True
        with self.assertRaisesRegex(OSError, "transport failed"):
            await player.answer_form(19, 1)
        self.assertIsNotNone(self.bot.get_pending_form(19))
        self.assert_responses()
        self.transport.fail_response = False
        self.assertEqual(await player.answer_form(19, 1), {
            "form_id": 19, "title": "Prompt", "button_index": 1, "submitted": True,
        })
        self.assertIsNone(self.bot.get_pending_form(19))
        with self.assertRaisesRegex(RuntimeError, "no form"):
            await self.bot.close_form(form_id=19)
        self.assert_responses((19, "1", None))

    async def test_mcp_can_watch_again_after_an_identical_replacement(self):
        await self.join()
        player = FormPlayer()
        player.bot = self.bot
        self.addAsyncCleanup(player.disconnect)
        request = self.request(19, ACTION)
        await player.watch_form(timeout=1)
        await self.deliver(request)
        await player.read_form()

        await self.deliver(request)
        with self.assertRaisesRegex(RuntimeError, "matching"):
            await player.answer_form(19, 1)
        self.assertIsNotNone(self.bot.get_pending_form(19))
        self.assert_responses()

        await player.watch_form(timeout=1)
        await self.deliver(request)
        await player.read_form()
        await player.answer_form(19, 0)
        self.assert_responses((19, "0", None))
