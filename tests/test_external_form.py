"""External action-form example uses the public typed packet stream."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from endpilot.protocol import Protocol, RawPacket
from examples.respond_form import answer_next_action_form


class FormExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def test_submits_matching_form_id_and_json_button_index(self):
        protocol = Protocol(2193)
        binding = protocol.binding
        incoming = binding.ModalFormRequestPacket(
            form_id=73,
            form_json=json.dumps({
                "type": "form", "title": "Choose", "content": "Test",
                "buttons": [{"text": "No"}, {"text": "Yes"}],
            }),
        )
        raw = protocol.encode(incoming)
        sent = []
        async def wait(packet_id, *, timeout):
            self.assertEqual(packet_id, binding.ModalFormRequestPacket.ID)
            self.assertEqual(timeout, 15)
            return raw

        async def send(packet):
            sent.append(packet)
        bot = SimpleNamespace(
            protocol=protocol,
            packets=SimpleNamespace(wait_for_packet=wait, send=send),
        )
        observed = await answer_next_action_form(bot, 1)
        self.assertEqual(observed, {
            "form_id": 73, "title": "Choose", "button_index": 1,
        })
        self.assertEqual(len(sent), 1)
        returned = protocol.decode(protocol.encode(sent[0]))
        self.assertEqual(int(returned.form_id), 73)
        self.assertEqual(returned.json_response, "1")
        self.assertIsNone(returned.form_cancel_reason)

    async def test_rejects_wrong_form_shape_without_submitting(self):
        protocol = Protocol(2193)
        binding = protocol.binding
        raw = protocol.encode(binding.ModalFormRequestPacket(
            form_id=3, form_json='{"type":"modal","button1":"Yes","button2":"No"}',
        ))
        packets = SimpleNamespace(
            wait_for_packet=AsyncMock(return_value=raw), send=AsyncMock(),
        )
        bot = SimpleNamespace(protocol=protocol, packets=packets)
        with self.assertRaisesRegex(ValueError, "expected a server action form"):
            await answer_next_action_form(bot, 1)
        packets.send.assert_not_awaited()

    async def test_modern_action_form_elements_count_only_buttons(self):
        protocol = Protocol(2193)
        raw = protocol.encode(protocol.binding.ModalFormRequestPacket(
            form_id=5,
            form_json=json.dumps({"type": "form", "title": "Modern", "elements": [
                {"type": "label", "text": "Info"},
                {"type": "button", "text": "A"},
                {"type": "button", "text": "B"},
            ]}),
        ))
        packets = SimpleNamespace(
            wait_for_packet=AsyncMock(return_value=raw), send=AsyncMock(),
        )
        bot = SimpleNamespace(protocol=protocol, packets=packets)
        result = await answer_next_action_form(bot, 1)
        self.assertEqual(result["button_index"], 1)
        returned = protocol.decode(protocol.encode(packets.send.await_args.args[0]))
        self.assertEqual(returned.json_response, "1")

    async def test_out_of_range_button_does_not_send(self):
        protocol = Protocol(2193)
        raw = protocol.encode(protocol.binding.ModalFormRequestPacket(
            form_id=4, form_json='{"type":"form","buttons":[{"text":"Only"}]}',
        ))
        packets = SimpleNamespace(
            wait_for_packet=AsyncMock(return_value=raw), send=AsyncMock(),
        )
        bot = SimpleNamespace(protocol=protocol, packets=packets)
        with self.assertRaisesRegex(ValueError, "button index"):
            await answer_next_action_form(bot, 1)
        packets.send.assert_not_awaited()
