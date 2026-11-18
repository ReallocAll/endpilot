"""Opening Bedrock game-protocol exchange, before compression is negotiated."""

from .protocol import Protocol, read_batch, write_batch


async def request_network_settings(connection, protocol: Protocol):
    """Exchange the first game message on a newly opened batch transport."""
    request = protocol.binding.RequestNetworkSettingsPacket()
    request.client_network_version = protocol.number
    await connection.send(write_batch([protocol.encode(request)]))

    while True:
        for raw in read_batch(await connection.recv()):
            if raw.packet_id == protocol.binding.NetworkSettingsPacket.ID:
                return protocol.decode(raw)
            if raw.packet_id in (protocol.binding.DisconnectPacket.ID, protocol.binding.PlayStatusPacket.ID):
                raise ConnectionError(f"server replied to network settings request: {protocol.decode(raw)!r}")
