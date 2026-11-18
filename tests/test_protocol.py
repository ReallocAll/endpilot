import unittest

from endpilot.protocol import Protocol, RawPacket, read_batch, write_batch


class ProtocolTests(unittest.TestCase):
    def test_network_settings_and_unmodelled_packet_in_one_batch(self):
        protocol = Protocol(2193)
        request = protocol.binding.RequestNetworkSettingsPacket()
        request.client_network_version = 2193
        raw = RawPacket(0xFFF, b"custom")
        batch = write_batch([protocol.encode(request), raw])

        # Length, varuint packet ID 193, then big-endian protocol 2193.
        self.assertEqual(batch[:7], bytes.fromhex("06 c1 01 00 00 08 91"))
        received, extension = read_batch(batch)
        self.assertEqual(protocol.decode(received).client_network_version, 2193)
        self.assertEqual(extension, raw)

    def test_partial_frame_cannot_consume_the_next_packet_header(self):
        with self.assertRaises(ValueError):
            list(read_batch(bytes.fromhex("01 80 01 00")))
