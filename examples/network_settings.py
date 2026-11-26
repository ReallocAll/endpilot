"""Encode and inspect the first game request without a running server."""

from endpilot.protocol import Protocol, read_batch, write_batch

protocol = Protocol()
request = protocol.binding.RequestNetworkSettingsPacket()
request.client_network_version = protocol.number
batch = write_batch([protocol.encode(request)])
for packet in read_batch(batch):
    decoded = protocol.decode(packet)
    print(type(decoded).__name__, decoded.client_network_version, batch.hex())
