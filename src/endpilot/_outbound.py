"""Ordered, bounded outbound command queue with protocol boundaries."""

import asyncio
from collections import deque
from dataclasses import dataclass

import bedrock_protocol as bp

from . import transport


@dataclass(slots=True)
class _Command:
    token: bp.EncodedPacket
    size: int
    play: bool
    operation: object = None
    valid: object = None


@dataclass(slots=True)
class _Boundary:
    future: asyncio.Future
    change: object = None


class _OutboundQueue:
    """Own command admission, FIFO barriers, wakeups and accounting."""

    def __init__(self):
        self.queue = deque()
        self.bytes = 0
        self.packets = 0
        self.event = asyncio.Event()

    def admit(self, token, *, play, operation=None, valid=None):
        size = len(token) + 5
        if self.packets >= transport.COMMAND_PACKETS or size > transport.COMMAND_BYTES - self.bytes:
            raise RuntimeError("outbound queue limit exceeded")
        self.queue.append(_Command(token, size, play, operation, valid))
        self.bytes += size
        self.packets += 1
        self.event.set()

    def add_boundary(self, future, change):
        self.queue.append(_Boundary(future, change))
        self.event.set()

    def wake(self):
        self.event.set()

    async def wait(self):
        await self.event.wait()
        self.event.clear()

    def advance_boundary(self):
        boundary = self.queue.popleft()
        assert isinstance(boundary, _Boundary)
        if boundary.change:
            boundary.change()
        if not boundary.future.done():
            boundary.future.set_result(None)

    def batch(self):
        commands = []
        for command in self.queue:
            if isinstance(command, _Boundary):
                break
            commands.append(command)
        return commands

    def consume(self, commands):
        for command in commands:
            assert self.queue.popleft() is command
            self.bytes -= command.size
            self.packets -= 1

    def discard(self):
        for command in self.queue:
            if isinstance(command, _Boundary) and not command.future.done():
                command.future.cancel()
        self.queue.clear()
        self.bytes = self.packets = 0
