"""A RakNet message transport with the Bedrock 0xFE envelope and post-login cipher."""

import asyncio
import hashlib
import struct

import raknet
import raknet.asyncio
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _checksum(counter: int, payload: bytes, key: bytes) -> bytes:
    return hashlib.sha256(struct.pack("<Q", counter) + payload + key).digest()[:8]


async def _drain_task[T](task: asyncio.Task[T]) -> T:
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError:
            continue
    return task.result()


async def _create_peer() -> raknet.asyncio.Peer:
    # The RakNet 0.4.0 factory constructs the peer in a worker thread. Keep
    # the task alive until we can take ownership of the resulting bound peer.
    startup = asyncio.create_task(raknet.asyncio.Peer._create([("", 0)], max_connections=1))
    try:
        return await asyncio.shield(startup)
    except asyncio.CancelledError as cancellation:
        try:
            peer = await _drain_task(startup)
            await _drain_task(asyncio.create_task(peer.close(0)))
        except BaseException as error:
            cancellation.add_note(f"RakNet startup cleanup failed: {error!r}")
        raise


async def _close_peer(peer: raknet.asyncio.Peer, timeout: float) -> None:
    closing = asyncio.create_task(peer.close(timeout))
    try:
        await asyncio.shield(closing)
    except asyncio.CancelledError as cancellation:
        try:
            await _drain_task(closing)
        except BaseException as error:
            cancellation.add_note(f"RakNet peer close failed: {error!r}")
        raise


class RakNetTransport:
    def __init__(self, connection, *, peer: raknet.asyncio.Peer | None = None):
        self._connection = connection
        self._peer = peer
        self._key = None
        self._encryptor = None
        self._decryptor = None
        self._sent = 0
        self._received = 0

    def enable_encryption(self, key: bytes) -> None:
        # BDS uses the AES-GCM keystream without GCM tags, followed by a SHA256 checksum.
        cipher = Cipher(algorithms.AES(key), modes.GCM(key[:12]))
        self._key = key
        self._encryptor = cipher.encryptor()
        self._decryptor = cipher.decryptor()

    async def send(self, data: bytes) -> None:
        if self._key is not None:
            data = self._encryptor.update(data + _checksum(self._sent, data, self._key))
            self._sent += 1
        await self._connection.send(
            b"\xfe" + data,
            reliability=raknet.PacketReliability.RELIABLE_ORDERED,
            priority=raknet.PacketPriority.MEDIUM_PRIORITY,
            channel=0,
        )

    async def recv(self) -> bytes:
        while True:
            frame = await self._connection.recv()
            if not frame or frame[0] != 0xFE:
                continue
            data = frame[1:]
            if self._key is not None:
                decoded = self._decryptor.update(data)
                if len(decoded) <= 8:
                    raise ConnectionError("truncated encrypted RakNet batch")
                data, checksum = decoded[:-8], decoded[-8:]
                expected = _checksum(self._received, data, self._key)
                if checksum != expected:
                    raise ConnectionError("invalid encrypted RakNet batch checksum")
                self._received += 1
            return data

    async def close(self) -> None:
        try:
            await self._connection.close()
        finally:
            if self._peer is not None:
                await _close_peer(self._peer, 0.5)


async def connect(host: str, port: int) -> RakNetTransport:
    peer = await _create_peer()
    try:
        connection = await peer.connect((host, port))
    except BaseException:
        await _close_peer(peer, 0)
        raise
    return RakNetTransport(connection, peer=peer)


async def advertised_protocol(host: str, port: int, *, timeout: float = 5) -> int:
    """Read the protocol from an unconnected pong. Peer cleanup can outlast the timeout."""
    try:
        async with asyncio.timeout(timeout):
            peer = await _create_peer()
            try:
                pong = await peer.ping((host, port))
            finally:
                await _close_peer(peer, 0)
    except TimeoutError as error:
        raise TimeoutError(f"RakNet protocol discovery timed out after {timeout:g}s for {host}:{port}") from error

    try:
        _, identifier, advertisement = pong.data.decode("utf-8").partition("MCPE;")
        if not identifier:
            raise ValueError("missing MCPE identifier")
        protocol = int(advertisement.split(";", 2)[1])
        if protocol <= 0:
            raise ValueError("nonpositive protocol")
    except (IndexError, ValueError) as error:
        raise ValueError(f"RakNet advertisement from {host}:{port} has no valid protocol") from error
    return protocol
