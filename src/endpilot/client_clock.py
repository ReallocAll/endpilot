"""The executed ticks and input deadlines of one connected client."""

import asyncio
from asyncio import sleep
from collections.abc import Callable
from time import perf_counter_ns
from weakref import ref


TICK_PERIOD_NS = 50_000_000


class ClientClock:
    def __init__(self):
        self.tick = 0
        self.deadline_ns = 0
        self._changed = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._closed = False
        self._failure: Exception | None = None
        self._tick_observers: dict[object, Callable[[int], None]] = {}

    def start(self) -> None:
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._run(perf_counter_ns() + TICK_PERIOD_NS))

    async def _run(self, deadline_ns: int) -> None:
        try:
            while not self._closed:
                remaining_ns = deadline_ns - perf_counter_ns()
                if remaining_ns > 0:
                    await sleep(remaining_ns / 1_000_000_000)
                    continue
                if -remaining_ns >= TICK_PERIOD_NS:
                    deadline_ns += (-remaining_ns // TICK_PERIOD_NS + 1) * TICK_PERIOD_NS
                    continue
                self.tick += 1
                self.deadline_ns = deadline_ns
                self._changed.set()
                if self._tick_observers:
                    self._notify_tick(self.tick)
                    # Give ready tasks a turn even if the next deadline is already due.
                    await asyncio.sleep(0)
                deadline_ns += TICK_PERIOD_NS
        finally:
            self._closed = True
            self._tick_observers.clear()
            self._changed.set()

    def subscribe_ticks(self, callback: Callable[[int], None]) -> Callable[[], None]:
        """Observe future executed ticks for this connection until it stops."""
        if self._task is None or self._closed:
            raise RuntimeError("player has no active connection")
        token = object()
        self._tick_observers[token] = callback
        owner_ref = ref(self)

        def unsubscribe() -> None:
            owner = owner_ref()
            if owner is not None:
                owner._tick_observers.pop(token, None)

        return unsubscribe

    def _notify_tick(self, tick: int) -> None:
        observers = self._tick_observers
        for token in tuple(observers):
            if token not in observers:
                continue
            callback = observers[token]
            try:
                callback(tick)
            except (Exception, asyncio.CancelledError) as error:
                asyncio.get_running_loop().call_exception_handler({
                    "message": "client tick observer failed", "exception": error, "callback": callback,
                })

    async def wait_until_tick(self, target: int) -> int:
        while not self._closed and self.tick < target:
            self._changed.clear()
            await self._changed.wait()
        return self.get_tick()

    def get_tick(self) -> int:
        if self._failure is not None:
            raise self._failure
        if self._closed:
            raise ConnectionError("client clock closed")
        return self.tick

    async def wait_ticks(self, ticks: int) -> int:
        return await self.wait_until_tick(self.tick + ticks)

    def stop(self, failure: Exception | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        self._failure = failure
        self._tick_observers.clear()
        self._changed.set()
        if self._task is not None:
            self._task.cancel()

    async def close(self) -> None:
        self.stop()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
