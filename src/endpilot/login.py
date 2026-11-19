"""Network settings and the opening Bedrock player join exchange."""

import asyncio
import base64
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from .compression import PacketConnection
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


def _verify_handshake(token: str) -> None:
    """The NetherNet transport is already encrypted; verify the server's signed handshake."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid server handshake token")
    decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    header = json.loads(decode(parts[0]))
    if header.get("alg") != "ES384":
        raise ValueError("unexpected server handshake algorithm")
    public = serialization.load_der_public_key(base64.b64decode(header["x5u"]))
    if not isinstance(public, ec.EllipticCurvePublicKey):
        raise ValueError("invalid server handshake key")
    signature = decode(parts[2])
    if len(signature) != 96:
        raise ValueError("invalid handshake signature")
    r, s = int.from_bytes(signature[:48], "big"), int.from_bytes(signature[48:], "big")
    public.verify(encode_dss_signature(r, s), ".".join(parts[:2]).encode(), ec.ECDSA(hashes.SHA384()))


class JoinSession:
    """Owns join progress and the established packet stream, not gameplay actions."""

    def __init__(self, packets: PacketConnection, identity, address: str):
        self.packets = packets
        self.identity = identity
        self.address = address
        self.player_id = None
        self.start_game = None
        self.spawned = False

    @classmethod
    async def connect_nethernet(cls, url: str, identity, protocol: Protocol, timeout: float = 30):
        from .transport.nethernet import connect

        transport = None
        try:
            async with asyncio.timeout(timeout):
                transport = await connect(url, private_key=identity.key)
                settings = await request_network_settings(transport, protocol)
                session = cls(PacketConnection(transport, protocol, settings), identity, url)
                await session.join()
            return session
        except BaseException:
            if transport is not None:
                await transport.close()
            raise

    async def join(self) -> None:
        binding = self.packets.protocol.binding
        login = binding.LoginPacket()
        login.client_network_version = self.packets.protocol.number
        login.connection_request = self.identity.login_request(self.address)
        await self.packets.send(login)

        while not self.spawned:
            raw = await self.packets.recv_raw()
            if raw.packet_id == binding.ServerToClientHandshakePacket.ID:
                _verify_handshake(self.packets.protocol.decode(raw).token)
                await self.packets.send(binding.ClientToServerHandshakePacket())
            elif raw.packet_id == binding.DisconnectPacket.ID:
                raise ConnectionError(f"server disconnected during login: {self.packets.protocol.decode(raw)!r}")
            elif raw.packet_id == binding.PlayStatusPacket.ID:
                status = self.packets.protocol.decode(raw).status
                if int(status) == 3:
                    self.spawned = True
                elif int(status) != 0:
                    raise ConnectionError(f"server refused login: {status}")
            elif raw.packet_id == binding.ResourcePacksInfoPacket.ID:
                info = self.packets.protocol.decode(raw)
                if info.resource_pack_required and info.resource_packs:
                    raise NotImplementedError("required server resource packs are not supported")
                response = binding.ResourcePackClientResponsePacket()
                response.response = binding.ResourcePackClientResponsePacket.DownloadingFinished()
                await self.packets.send(response)
            elif raw.packet_id == binding.ResourcePackStackPacket.ID:
                stack = self.packets.protocol.decode(raw)
                if stack.texture_pack_required and stack.texture_pack_ids_and_versions:
                    raise NotImplementedError("required texture packs are not supported")
                response = binding.ResourcePackClientResponsePacket()
                response.response = binding.ResourcePackClientResponsePacket.ResourcePackStackFinished()
                await self.packets.send(response)
            elif raw.packet_id == binding.StartGamePacket.ID:
                game = self.packets.protocol.decode(raw)
                self.player_id = int(game.runtime_id)
                self.start_game = game
                radius = binding.RequestChunkRadiusPacket()
                radius.chunk_radius = 8
                radius.max_chunk_radius = 8
                await self.packets.send(radius)
                initialized = binding.SetLocalPlayerAsInitializedPacket()
                initialized.player_id = game.runtime_id
                await self.packets.send(initialized)

    async def close(self) -> None:
        await self.packets.close()
