"""Bedrock post-NetworkSettings compression, batches and live packet dispatch."""

import asyncio
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
        self._subscribers = []
        self._waiters = deque()
        self._reader_task = None
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

    async def _read_wire(self) -> RawPacket:
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

    def subscribe_raw(self, callback):
        """Observe incoming packets after join without consuming another reader's stream."""
        self._subscribers.append(callback)

        def unsubscribe():
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def start_receiving(self):
        """Switch from login's direct reads to one reader serving all packet observers."""
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._dispatch())

    async def _dispatch(self):
        try:
            while True:
                raw = await self._read_wire()
                for callback in tuple(self._subscribers):
                    callback(raw)
                while self._waiters:
                    waiter = self._waiters.popleft()
                    if not waiter.done():
                        waiter.set_result(raw)
                        break
        except asyncio.CancelledError:
            raise
        except Exception as error:
            for waiter in self._waiters:
                if not waiter.done():
                    waiter.set_exception(error)
            self._waiters.clear()
            raise

    async def recv_raw(self) -> RawPacket:
        if self._reader_task is None:
            return await self._read_wire()
        if self._reader_task.done():
            await self._reader_task
        # Once dispatch runs, a direct read waits for the *next* live packet.
        # Subscribe before receiving to observe every packet of a particular type.
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            return await waiter
        finally:
            if not waiter.done():
                waiter.cancel()

    async def recv(self):
        return self.protocol.decode(await self.recv_raw())

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_exception(ConnectionError("connection closed"))
        self._waiters.clear()
        await self.transport.close()
