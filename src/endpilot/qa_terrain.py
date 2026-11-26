"""Load server-announced terrain independently of block queries."""

import asyncio
from contextlib import suppress

from .blocks import Blocks


class _TerrainObserver:
    def __init__(self, blocks: Blocks):
        if blocks._request_column is not None:
            raise RuntimeError("terrain loading already started")
        self._blocks = blocks
        self._packets = blocks.packets
        self._pending = asyncio.Queue()
        self._unsubscribe = self._packets.subscribe_raw(self._on_packet)
        self._sender = asyncio.create_task(self._send_requests())
        self._unsubscribe_end = self._packets.subscribe_reader_end(lambda cause: self.stop())
        blocks._request_column = self._queue_column_request

    def _on_packet(self, raw) -> None:
        if raw.packet_id == self._packets.protocol.binding.DisconnectPacket.ID:
            self.stop()

    def _queue_column_request(self, column: tuple[int, int], sections: range) -> None:
        state = self._blocks._coverage[column]
        known = state.received | state.requested | state.pending
        heights = [y for y in sections if y not in known]
        if not heights:
            return
        state.pending.update(heights)
        self._pending.put_nowait((column, state, self._blocks.dimension_id, heights))

    async def _send_requests(self) -> None:
        binding = self._packets.protocol.binding
        while True:
            column, state, dimension, heights = await self._pending.get()
            if self._blocks._coverage.get(column) is not state:
                continue
            x, z = column
            missing = [y for y in heights if y not in state.received and y not in state.requested]
            try:
                if missing:
                    await self._blocks._send_sub_chunk_request(binding.SubChunkRequestPacket(
                        dimension_type=dimension, center_pos=binding.SubChunkPos(x=x, y=0, z=z),
                        sub_chunk_pos_offsets=[
                            binding.SubChunkPacket.SubChunkPosOffset(x=0, y=y, z=0) for y in missing
                        ],
                    ))
            finally:
                state.pending.difference_update(heights)
            # Some transports accept sends without yielding to input and receive.
            if missing:
                await asyncio.sleep(0)

    def stop(self) -> None:
        self._unsubscribe()
        self._unsubscribe_end()
        if self._blocks._request_column == self._queue_column_request:
            self._blocks._request_column = None
        self._sender.cancel()

    async def close(self) -> None:
        self.stop()
        with suppress(asyncio.CancelledError, ConnectionError):
            await self._sender
