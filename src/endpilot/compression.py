"""Bedrock post-NetworkSettings compression, batches and live packet dispatch."""

import asyncio
from collections import deque
import zlib

from cramjam import DecompressionError, snappy

from .client_clock import ClientClock
from .protocol import RawPacket, read_batch, write_batch
from .qa import PacketTimeline


MAX_DECOMPRESSED_BATCH_SIZE = 64 * 1024 * 1024


class PacketConnection:
    """Typed/raw packets over an already-negotiated message transport."""

    def __init__(self, transport, protocol, settings, *, timeline: PacketTimeline | None = None,
                 clock: ClientClock | None = None):
        self.transport = transport
        self.protocol = protocol
        self.algorithm = int(settings.compression_algorithm)
        self.threshold = int(settings.compression_threshold)
        self._timeline = timeline
        self._clock = clock
        self._pending = deque()
        self._pending_tick: int | None = None
        self._replayed = deque()
        self._subscribers = []
        self._sent_subscribers = []
        self._reader_end_subscribers = []
        self._waiters = deque()
        self._reader_task = None
        self._read_start: asyncio.Event | None = None
        if self.algorithm not in (0, 1, 65535):
            raise NotImplementedError(f"compression algorithm {self.algorithm} is not supported")

    async def send(self, packet, *, _before_send=None) -> None:
        reader = self._reader_task
        if reader is not None and reader.done():
            if reader.cancelled():
                raise ConnectionError("packet stream closed")
            reader.result()
            raise ConnectionError("packet reader stopped")
        raw = packet if isinstance(packet, RawPacket) else self.protocol.encode(packet)
        data = write_batch([raw])
        if self.algorithm != 65535:
            if self.threshold > 0 and len(data) >= self.threshold:
                if self.algorithm == 0:
                    compressed = zlib.compress(data, level=7, wbits=-15)
                else:
                    compressed = snappy.compress_raw(data)
                data = bytes([self.algorithm]) + compressed if len(compressed) < len(data) else b"\xff" + data
            else:
                data = b"\xff" + data
        if _before_send is not None:
            _before_send()
        await self.transport.send(data)
        if self._timeline is not None and not isinstance(packet, RawPacket):
            self._timeline._record_sent(type(packet).__name__)
        self._notify(self._sent_subscribers, raw.packet_id)

    async def _read_wire(self) -> RawPacket:
        if self._replayed:
            return self._replayed.popleft()
        while not self._pending:
            data = await self.transport.recv()
            if self._timeline is not None and self._clock is not None:
                # Login can suspend between packets from this same wire batch.
                self._pending_tick = self._clock.tick
            if self.algorithm != 65535:
                if not data:
                    raise ConnectionError("empty compressed batch")
                algorithm, data = data[0], data[1:]
                if algorithm not in (self.algorithm, 255):
                    raise ValueError(f"unexpected compression header {algorithm}")
                if algorithm == 0:
                    decoder = zlib.decompressobj(wbits=-15)
                    try:
                        data = decoder.decompress(data, MAX_DECOMPRESSED_BATCH_SIZE + 1)
                    except zlib.error as error:
                        raise ValueError("invalid compressed batch") from error
                    if (
                        len(data) > MAX_DECOMPRESSED_BATCH_SIZE
                        or not decoder.eof
                        or decoder.unused_data
                        or decoder.unconsumed_tail
                    ):
                        raise ValueError("invalid compressed batch")
                elif algorithm == 1:
                    try:
                        if snappy.decompress_raw_len(data) > MAX_DECOMPRESSED_BATCH_SIZE:
                            raise ValueError("invalid compressed batch")
                        data = snappy.decompress_raw(data)
                    except DecompressionError as error:
                        raise ValueError("invalid compressed batch") from error
            self._pending.extend(read_batch(data))
        raw = self._pending.popleft()
        if self._timeline is not None:
            self._timeline._record(self.protocol, raw, tick=self._pending_tick)
        return raw

    def subscribe_raw(self, callback):
        """Observe incoming packets after join without consuming another reader's stream."""
        return self._subscribe(self._subscribers, callback)

    def subscribe_sent(self, callback):
        """Pass the packet ID to a synchronous observer after a successful transport send."""
        return self._subscribe(self._sent_subscribers, callback)

    @staticmethod
    def _subscribe(subscribers, callback):
        subscribers.append(callback)

        def unsubscribe():
            if callback in subscribers:
                subscribers.remove(callback)

        return unsubscribe

    @staticmethod
    def _notify(subscribers, value):
        for callback in tuple(subscribers):
            try:
                callback(value)
            except (Exception, asyncio.CancelledError) as error:
                asyncio.get_running_loop().call_exception_handler({
                    "message": "packet observer failed",
                    "exception": error,
                    "callback": callback,
                })

    def _replay_before_receiving(self, packets):
        """Deliver already observed login packets before new play traffic."""
        self._replayed.extendleft(reversed(packets))

    async def wait_for_packet(self, packet_id: int | tuple[int, ...], *, timeout: float | None = 15, match=None) -> RawPacket:
        """Wait for one of the specified packet IDs; distinguish loss from timeout."""
        packet_ids = (packet_id,) if isinstance(packet_id, int) else packet_id
        reader = self._reader_task
        if reader is None:
            raise RuntimeError("packet receiver has not started")
        received = asyncio.get_running_loop().create_future()

        def observe(raw):
            if raw.packet_id not in packet_ids or received.done():
                return
            try:
                if match is None or match(raw):
                    received.set_result(raw)
            except (Exception, asyncio.CancelledError) as error:
                received.set_exception(error)

        unsubscribe = self.subscribe_raw(observe)
        try:
            async with asyncio.timeout(timeout):
                done, _ = await asyncio.wait((received, reader), return_when=asyncio.FIRST_COMPLETED)
            if received in done:
                return received.result()
            if reader.cancelled():
                raise ConnectionError("packet stream closed while waiting")
            failure = reader.exception()
            if failure is not None:
                raise ConnectionError("packet stream lost while waiting") from failure
            raise ConnectionError("packet reader stopped while waiting")
        finally:
            unsubscribe()

    def start_receiving(self, *, _before_dispatch=None):
        """Start or return the one reader that serves all packet observers."""
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._dispatch(_before_dispatch))
            self._reader_task.add_done_callback(self._notify_reader_end)
        return self._reader_task

    def _defer_receiving_until(self, ready: asyncio.Event) -> None:
        """Hold the initial live read until the join publishes its state."""
        self._read_start = ready

    def subscribe_reader_end(self, callback):
        """Observe the live reader ending without starting a second read."""
        return self._subscribe(self._reader_end_subscribers, callback)

    def _reader_end_result(self) -> tuple[bool, BaseException | None]:
        """Read the reader's completion and cause even before its callbacks run."""
        reader = self._reader_task
        if reader is None or not reader.done():
            return False, None
        return True, None if reader.cancelled() else reader.exception()

    def _publish_reader_end(self, cause: BaseException | None) -> None:
        subscribers = tuple(self._reader_end_subscribers)
        self._reader_end_subscribers.clear()
        for callback in subscribers:
            try:
                callback(cause)
            except (Exception, asyncio.CancelledError) as error:
                asyncio.get_running_loop().call_exception_handler({
                    "message": "reader end observer failed",
                    "exception": error,
                    "callback": callback,
                })

    def _fail_raw_reads(self, error):
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_exception(error)

    def _notify_reader_end(self, reader):
        cause = None if reader.cancelled() else reader.exception()
        # Dispatch reports receive errors inline. This callback still covers cancellation,
        # including cancellation before the coroutine starts.
        if not isinstance(cause, Exception):
            self._publish_reader_end(cause)
            self._fail_raw_reads(ConnectionError("connection closed"))

    async def _dispatch(self, before_dispatch):
        try:
            if self._read_start is not None:
                await self._read_start.wait()
                self._read_start = None
            while True:
                raw = await self._read_wire()
                if before_dispatch is not None:
                    await before_dispatch(raw)
                self._notify(self._subscribers, raw)
                while self._waiters:
                    waiter = self._waiters.popleft()
                    if not waiter.done():
                        waiter.set_result(raw)
                        break
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Invalidate observations before releasing readers or completing the reader task.
            self._publish_reader_end(error)
            self._fail_raw_reads(error)
            raise

    async def recv_raw(self) -> RawPacket:
        reader = self._reader_task
        if reader is None:
            return await self._read_wire()
        if reader.done():
            if reader.cancelled():
                raise ConnectionError("connection closed")
            reader.result()
            raise ConnectionError("packet reader stopped")
        # Once dispatch runs, a direct read waits for the *next* live packet.
        # Subscribe before receiving to observe every packet of a particular type.
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            return await waiter
        except asyncio.CancelledError:
            try:
                self._waiters.remove(waiter)
            except ValueError:
                pass  # Dispatch or close already removed it.
            raise

    async def recv(self):
        return self.protocol.decode(await self.recv_raw())

    async def close(self) -> None:
        try:
            if self._reader_task is not None:
                self._reader_task.cancel()
                try:
                    await self._reader_task
                except asyncio.CancelledError:
                    pass
        finally:
            self._fail_raw_reads(ConnectionError("connection closed"))
            await self.transport.close()
