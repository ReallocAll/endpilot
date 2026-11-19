"""Bedrock post-NetworkSettings compression and batch I/O."""

from collections import deque
import zlib

from .protocol import RawPacket, read_batch, write_batch


class PacketConnection:
    """Typed/raw packets over an already-negotiated message transport."""

    def __init__(self, transport, protocol, settings):
        self.transport = transport
        self.protocol = protocol
        self.algorithm = int(settings.compression_algorithm)
        self.threshold = int(settings.compression_threshold)
        self._pending = deque()
        if self.algorithm not in (0, 65535):
            raise NotImplementedError(f"compression algorithm {self.algorithm} is not supported")

    async def send(self, packet) -> None:
        raw = packet if isinstance(packet, RawPacket) else self.protocol.encode(packet)
        data = write_batch([raw])
        if self.algorithm != 65535:
            if self.threshold > 0 and len(data) >= self.threshold:
                compressor = zlib.compressobj(level=7, wbits=-15)
                compressed = compressor.compress(data) + compressor.flush()
                data = b"\x00" + compressed if len(compressed) < len(data) else b"\xff" + data
            else:
                data = b"\xff" + data
        await self.transport.send(data)

    async def recv_raw(self) -> RawPacket:
        while not self._pending:
            data = await self.transport.recv()
            if self.algorithm != 65535:
                if not data:
                    raise ConnectionError("empty compressed batch")
                algorithm, data = data[0], data[1:]
                if algorithm == 0:
                    data = zlib.decompress(data, wbits=-15)
                elif algorithm != 255:
                    raise ValueError(f"unexpected compression header {algorithm}")
            self._pending.extend(read_batch(data))
        return self._pending.popleft()

    async def recv(self):
        return self.protocol.decode(await self.recv_raw())

    async def close(self) -> None:
        await self.transport.close()
