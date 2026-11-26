import asyncio
import json
import unittest
from unittest.mock import patch

from endpilot import Bot
from endpilot.protocol import Protocol, read_batch, write_batch
from endpilot.qa import PacketTimeline
from test_scheduled_input import ManualClock


ACTION = {"type": "form", "elements": [
    {"type": "label", "text": "Information"}, {"type": "button", "text": "Yes"},
    {"type": "divider"}, {"type": "button", "text": "No"},
]}
CONFIRMATION = {"type": "modal", "button1": "Yes", "button2": "No"}
CUSTOM = {"type": "custom_form", "content": [
    {"type": kind} for kind in ("label", "toggle", "slider", "dropdown", "step_slider", "input", "header", "divider")
]}


class BatchTransport:
    def __init__(self, protocol):
        self.protocol = protocol
        self.incoming = asyncio.Queue()
        self.responses = []
        self.fail_response = False
        self.response_started = asyncio.Event()
        self.resume_response = None

    def deliver(self, *packets):
        self.incoming.put_nowait(write_batch(self.protocol.encode(packet) for packet in packets))

    async def recv(self):
        return await self.incoming.get()

    async def send(self, data):
        for raw in read_batch(data):
            if raw.packet_id == self.protocol.binding.ModalFormResponsePacket.ID:
                self.response_started.set()
                if self.resume_response is not None:
                    await self.resume_response.wait()
                if self.fail_response:
                    raise OSError("form response transport failed")
                self.responses.append(self.protocol.decode(raw))

    async def close(self):
        pass


class FormTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("FormObserver", protocol=2225)
        self.binding = self.bot.protocol.binding
        self.addAsyncCleanup(self.bot.close)

    def request(self, form_id, form):
        return self.binding.ModalFormRequestPacket(form_id=form_id, form_json=json.dumps(form))

    async def join(self, *before_spawn):
        self.transport = BatchTransport(self.bot.protocol)
        self.transport.deliver(self.binding.NetworkSettingsPacket(
            compression_algorithm=self.binding.PacketCompressionAlgorithm.NONE,
        ))
        self.transport.deliver(
            self.binding.StartGamePacket(runtime_id=42, entity_id=7),
            *before_spawn, self.binding.PlayStatusPacket(status=3),
        )
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    async def deliver(self, *packets):
        waiter = asyncio.create_task(self.bot.packets.wait_for_packet(packets[-1].ID, timeout=1))
        await asyncio.sleep(0)
        self.transport.deliver(*packets)
        return await waiter

    def assert_responses(self, *expected):
        self.assertEqual([
            (packet.form_id, packet.json_response, packet.form_cancel_reason)
            for packet in self.transport.responses
        ], list(expected))


class FormTests(FormTestCase):
    async def test_early_and_live_forms_are_independent_and_support_id_selection(self):
        await self.join(self.request(73, ACTION))
        snapshot = self.bot.get_pending_form()
        self.assertEqual(snapshot, {"form_id": 73, "form": ACTION})
        snapshot["form"]["elements"].clear()

        await self.deliver(self.request(5, CONFIRMATION))
        self.assertEqual(self.bot.get_pending_form(), {"form_id": 5, "form": CONFIRMATION})
        self.assertEqual(self.bot.get_pending_form(73), {"form_id": 73, "form": ACTION})
        for invalid in (True, "1", 2):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await self.bot.answer_form(invalid, 73)
        self.assert_responses()

        await self.bot.answer_form(1, 73)
        self.assertIsNone(self.bot.get_pending_form(73))
        self.assertEqual(self.bot.get_pending_form()["form_id"], 5)
        await self.bot.answer_form(False)
        self.assertIsNone(self.bot.get_pending_form())
        legacy_action = {"type": "form", "buttons": [{"text": "Only"}]}
        await self.deliver(self.request(6, legacy_action))
        await self.bot.answer_form(0)
        self.assert_responses((73, "1", None), (5, "false", None), (6, "0", None))

    async def test_custom_form_validation_and_both_cancel_reasons(self):
        await self.join(self.request(9, CUSTOM))
        values = ["ignored", True, 2.5, 1, 0, "测试", None, None]
        with self.assertRaises(ValueError):
            await self.bot.answer_form(values[:-1])
        with self.assertRaises(ValueError):
            await self.bot.answer_form([None, True, True, 1, 0, "x", None, None])
        with self.assertRaises(ValueError):
            await self.bot.answer_form([None, True, float("nan"), 1, 0, "x", None, None])
        self.assert_responses()
        self.assertEqual(self.bot.get_pending_form()["form_id"], 9)
        await self.bot.answer_form(tuple(values))
        response, = self.transport.responses
        self.assertEqual(json.loads(response.json_response), [None, True, 2.5, 1, 0, "测试", None, None])
        self.assertIsNone(response.form_cancel_reason)
        self.assertIsNone(self.bot.get_pending_form())

        await self.deliver(self.request(1, ACTION), self.request(2, CUSTOM))
        with self.assertRaisesRegex(ValueError, "cancel reason"):
            await self.bot.close_form("UNKNOWN", 1)
        await self.bot.close_form("USER_BUSY", 1)
        await self.bot.close_form()
        reason = self.binding.ModalFormCancelReason
        self.assert_responses((9, response.json_response, None), (1, None, reason.USER_BUSY), (2, None, reason.USER_CLOSED))

    async def test_expired_ids_and_session_end_clear_forms(self):
        close = self.binding.ClientboundCloseFormPacket
        await self.join(self.request(1, ACTION), close(), self.request(2, ACTION))
        self.assertIsNone(self.bot.get_pending_form(1))
        await self.deliver(self.request(3, ACTION), close())
        self.assertIsNone(self.bot.get_pending_form())
        for form_id in (2, 3):
            with self.assertRaisesRegex(RuntimeError, "no form"):
                await self.bot.answer_form(0, form_id)
        self.assert_responses()

        await self.deliver(self.request(4, ACTION), self.binding.DisconnectPacket())
        self.assertIsNone(self.bot.get_pending_form())
        await self.bot.close()
        await self.join(self.request(5, ACTION))
        self.assertEqual(self.bot.get_pending_form()["form_id"], 5)
        await self.bot.close()
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            self.bot.get_pending_form()
        await self.join()
        self.assertIsNone(self.bot.get_pending_form())

    async def test_failed_submission_preserves_the_form_and_duplicate_submission_fails(self):
        await self.join(self.request(11, ACTION))
        self.transport.fail_response = True
        with self.assertRaisesRegex(OSError, "transport failed"):
            await self.bot.answer_form(0, 11)
        self.assertEqual(self.bot.get_pending_form(11)["form_id"], 11)
        self.transport.fail_response = False
        self.transport.response_started.clear()
        self.transport.resume_response = asyncio.Event()

        answer = asyncio.create_task(self.bot.answer_form(0, 11))
        await asyncio.wait_for(self.transport.response_started.wait(), timeout=1)
        duplicate = asyncio.create_task(self.bot.close_form(form_id=11))
        await asyncio.sleep(0)
        self.transport.resume_response.set()
        await answer
        with self.assertRaisesRegex(RuntimeError, "no longer pending"):
            await duplicate
        self.assert_responses((11, "0", None))

    async def test_reused_id_is_latest_and_survives_an_older_in_flight_answer(self):
        await self.join(self.request(1, ACTION), self.request(2, ACTION))
        self.transport.resume_response = asyncio.Event()
        answer = asyncio.create_task(self.bot.answer_form(0, 1))
        await asyncio.wait_for(self.transport.response_started.wait(), timeout=1)
        await self.deliver(self.request(1, CONFIRMATION))
        self.transport.resume_response.set()
        await answer
        self.assertEqual(self.bot.get_pending_form(), {"form_id": 1, "form": CONFIRMATION})
        self.assertEqual(self.bot.get_pending_form(2)["form_id"], 2)
        self.assert_responses((1, "0", None))


class LiveFormTests(FormTestCase):
    async def asyncSetUp(self):
        self.clock = ManualClock()
        self.enterContext(self.clock.patch_scheduler())
        self.bot = Bot("FormObserver", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.timeline = PacketTimeline()
        protocol = self.bot.protocol
        self.binding = protocol.binding
        self.transport = BatchTransport(protocol)
        self.transport.deliver(self.binding.NetworkSettingsPacket(
            compression_algorithm=self.binding.PacketCompressionAlgorithm.NONE,
        ))
        self.transport.deliver(
            self.binding.StartGamePacket(runtime_id=42, entity_id=7),
            self.binding.PlayStatusPacket(status=3),
        )
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeline=self.timeline)

    async def test_wire_request_and_close_have_distinct_receive_cursors(self):
        await self.clock.wake_at(50)
        since = self.timeline.next_index
        request = self.binding.ModalFormRequestPacket(form_id=19, form_json=' { "type": "modal" }\n')
        request_raw = await self.deliver(request)
        received, = self.timeline.received("ModalFormRequestPacket", since=since)
        self.assertEqual((received.index, received.tick, received.raw), (since, 1, request_raw))
        decoded = self.bot.protocol.decode(received.raw)
        self.assertEqual((decoded.form_id, decoded.form_json), (19, request.form_json))
        self.assertEqual(self.bot.get_pending_form(19), {
            "form_id": 19, "form": {"type": "modal"},
        })

        closing = self.timeline.next_index
        self.assertEqual(self.timeline.received("ModalFormRequestPacket", since=closing), ())
        await self.clock.wake_at(100)
        raw_close = await self.deliver(self.binding.ClientboundCloseFormPacket())
        received, = self.timeline.received("ClientboundCloseFormPacket", since=closing)
        self.assertEqual((received.index, received.tick, received.raw), (closing, 2, raw_close))
        self.assertIsInstance(self.bot.protocol.decode(received.raw), self.binding.ClientboundCloseFormPacket)
        self.assertIsNone(self.bot.get_pending_form(19))
        with self.assertRaisesRegex(RuntimeError, "no form"):
            await self.bot.answer_form(True, 19)
        self.assert_responses()

    async def test_responses_use_pending_ids_and_original_requests(self):
        old = await self.deliver(self.request(1, CONFIRMATION))
        await self.deliver(self.request(2, ACTION))
        with self.assertRaises(ValueError):
            await self.bot.answer_form(True, 2)
        self.assert_responses()

        self.transport.fail_response = True
        with self.assertRaisesRegex(OSError, "transport failed"):
            await self.bot.answer_form(1, 2)
        self.assertEqual(self.bot.get_pending_form(2)["form_id"], 2)
        self.transport.fail_response = False
        await self.bot.answer_form(1, 2)
        self.assertIsNone(self.bot.get_pending_form(2))
        self.assertEqual(self.bot.get_pending_form(old)["form_id"], 1)

        current = await self.deliver(self.request(1, CONFIRMATION))
        with self.assertRaisesRegex(RuntimeError, "no form"):
            await self.bot.answer_form(False, old)
        await self.bot.answer_form(False, current)
        await self.deliver(self.request(3, CONFIRMATION))
        await self.bot.close_form("USER_CLOSED", 3)
        self.assertIsNone(self.bot.get_pending_form())
        self.assert_responses((2, "1", None), (1, "false", None), (3, None, self.binding.ModalFormCancelReason.USER_CLOSED))

    async def test_concurrent_cancel_only_consumes_the_selected_form(self):
        await self.deliver(self.request(21, CONFIRMATION))
        self.transport.resume_response = asyncio.Event()
        async with asyncio.TaskGroup() as group:
            group.create_task(self.bot.close_form("USER_CLOSED", 21))
            await asyncio.wait_for(self.transport.response_started.wait(), timeout=1)
            await self.deliver(self.request(34, ACTION))
            self.transport.resume_response.set()
        self.assertIsNone(self.bot.get_pending_form(21))
        self.assertEqual(self.bot.get_pending_form(34)["form"], ACTION)

        await self.deliver(self.request(35, CONFIRMATION))
        await self.bot.close_form("USER_BUSY", 34)
        self.assertIsNone(self.bot.get_pending_form(34))
        with self.assertRaisesRegex(RuntimeError, "no form"):
            await self.bot.close_form("USER_CLOSED", 21)
        self.assertEqual(self.bot.get_pending_form(), {"form_id": 35, "form": CONFIRMATION})

        reason = self.binding.ModalFormCancelReason
        self.assert_responses((21, None, reason.USER_CLOSED), (34, None, reason.USER_BUSY))
