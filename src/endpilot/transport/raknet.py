"""A RakNet message transport with the Bedrock 0xFE envelope and post-login cipher."""

import asyncio
import hashlib
import struct

import raknet
import raknet.asyncio
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _checksum(counter: int, payload: bytes, key: bytes) -> bytes:
    return hashlib.sha256(struct.pack("<Q", counter) + payload + key).digest()[:8]


class RakNetTransport:
    def __init__(self, connection):
        self._connection = connection
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
        await self._connection.close()


async def connect(host: str, port: int) -> RakNetTransport:
    return RakNetTransport(await raknet.asyncio.create_connection((host, port)))


async def advertised_protocol(host: str, port: int, *, timeout: float = 5) -> int:
    """Read the Bedrock protocol from an unconnected RakNet pong."""
    try:
        async with asyncio.timeout(timeout):
            pong = await raknet.asyncio.ping((host, port))
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
