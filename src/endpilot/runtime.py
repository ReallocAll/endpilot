from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Awaitable, Callable

from .events import DEFAULT_EVENTS, event_mask, validate_mask
from .types import BotOptions


class Runtime:
    def __init__(self, callback: Callable | None = None, *, events: int = DEFAULT_EVENTS):
        if callback is not None and (not callable(callback) or inspect.iscoroutinefunction(callback)):
            raise TypeError("callback must be a synchronous callable")
        self._callback = callback
        self._subscribers = {}
        self._interests = validate_mask(events)
        self._loop = None
        self._bots = []
        self._exited = False
        self._explicit_close = False
        self._stopping = False
        self._suppressed = False
        self._callback_depth = 0
        self._failure = None

    async def __aenter__(self) -> Runtime:
        if self._loop is not None or self._exited:
            raise RuntimeError("Runtime cannot be entered twice")
        loop = asyncio.get_running_loop()
        if sys.platform == "win32" and isinstance(loop, asyncio.ProactorEventLoop):
            raise RuntimeError(
                "Windows requires SelectorEventLoop; use asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)"
            )
        # Complete required protocol and transport imports before entering Runtime.
        from . import session  # noqa: F401

        self._loop = loop
        self._stop_event = asyncio.Event()
        self._lifetime = self._loop.create_task(self._run(), name="bedrock-runtime")
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        self._guard(deliver=False, allow_exited=True)
        self._explicit_close = True
        self._request_stop()
        cancellation = None
        try:
            while True:
                try:
                    await self._join()
                    break
                except asyncio.CancelledError as error:
                    if self._lifetime.cancelled():
                        raise
                    cancellation = error
        finally:
            self._exited = True
        if exc is None:
            if cancellation is not None:
                raise cancellation
            self._deliver_failure()
        return False

    def _guard(self, *, deliver=True, allow_exited=False):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError("operation requires the Runtime owner loop") from None
        if self._loop is None or loop is not self._loop:
            raise RuntimeError("operation requires the Runtime owner loop")
        if self._exited and not allow_exited:
            raise RuntimeError("Runtime is closed")
        if deliver:
            self._deliver_failure()

    def _deliver_failure(self):
        if self._failure is not None:
            error, tb = self._failure
            self._failure = None
            raise error.with_traceback(tb)

    def _background_failure(self, error):
        if self._failure is None:
            self._failure = (error, error.__traceback__)
        self._suppressed = True
        self._request_stop()

    def _request_stop(self):
        if self._stopping:
            return
        self._stopping = True
        for bot in self._bots:
            bot._request("stop", "runtime stopped")
        self._stop_event.set()

    async def _run(self):
        await self._stop_event.wait()
        await asyncio.gather(*(bot._lifetime for bot in self._bots))
        if self._explicit_close:
            self._exited = True

    async def _join(self):
        # Cleanup is owned by _lifetime even if a waiter is cancelled.
        await asyncio.shield(self._lifetime)

    def request_stop(self) -> None:
        if self._loop is None:
            raise RuntimeError("Runtime has not been entered")
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._request_stop)

    def aclose(self) -> Awaitable[None]:
        self._guard(deliver=False, allow_exited=True)
        if self._callback_depth:
            raise RuntimeError("closing Runtime inside a callback is not allowed")
        self._explicit_close = True
        self._request_stop()
        if self._lifetime.done():
            self._exited = True
        return self._close_waiter()

    async def _close_waiter(self):
        await self._join()
        self._deliver_failure()

    async def wait_closed(self) -> None:
        self._guard(deliver=False, allow_exited=True)
        if self._callback_depth:
            raise RuntimeError("waiting inside a synchronous callback is not allowed")
        await self._join()
        self._deliver_failure()

    def add_bot(self, options: BotOptions):
        from .bot import Bot

        self._guard()
        if self._callback_depth or self._stopping:
            raise RuntimeError("cannot add a bot during a callback or shutdown")
        bot = Bot(self, options)
        self._bots.append(bot)
        return bot

    def get_event_interests(self) -> int:
        self._guard(deliver=False)
        return self._interests

    def set_event_interests(self, mask: int) -> None:
        self._guard()
        self._interests = validate_mask(mask)

    def subscribe(self, callback: Callable, *, events: int = DEFAULT_EVENTS) -> Callable[[], None]:
        """Register an independent synchronous observer; return its unsubscribe function."""
        self._guard()
        if not callable(callback) or inspect.iscoroutinefunction(callback):
            raise TypeError("observer must be a synchronous callable")
        mask = validate_mask(events)
        key = object()
        self._subscribers[key] = (callback, mask)

        def unsubscribe() -> None:
            self._guard(deliver=False, allow_exited=True)
            self._subscribers.pop(key, None)

        return unsubscribe

    def _wants(self, bot, kind) -> bool:
        if self._suppressed:
            return False
        bit = event_mask(kind)
        return (
            (self._callback is not None and bool((self._interests | bot._interests) & bit))
            or any(mask & bit for _, mask in self._subscribers.values())
        )

    def _emit(self, bot, kind, make_event):
        if not self._wants(bot, kind):
            return
        bit = event_mask(kind)
        legacy = self._callback is not None and bool((self._interests | bot._interests) & bit)
        observers = tuple(
            (key, callback) for key, (callback, mask) in self._subscribers.items() if mask & bit
        )
        self._callback_depth += 1
        try:
            event = make_event()
            if legacy:
                result = self._callback(bot, event)
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    raise TypeError("event callbacks must not return an awaitable")
            for key, callback in observers:
                if key not in self._subscribers or self._suppressed:
                    continue
                try:
                    result = callback(bot, event)
                    if inspect.isawaitable(result):
                        if inspect.iscoroutine(result):
                            result.close()
                        raise TypeError("event observers must not return an awaitable")
                except Exception as error:
                    self._loop.call_exception_handler({
                        "message": "EndPilot event observer failed",
                        "exception": error,
                        "callback": callback,
                    })
        except BaseException:
            self._suppressed = True
            self._request_stop()
            raise
        finally:
            self._callback_depth -= 1
