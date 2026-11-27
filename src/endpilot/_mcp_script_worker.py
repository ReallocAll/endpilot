"""Execute one MCP script against the host player's local RPC connection."""

import asyncio
from concurrent.futures import Future
from dataclasses import fields
import inspect
from io import TextIOBase
from itertools import count
import json
import logging
import os
from pathlib import Path
import sys
from threading import BoundedSemaphore, Lock, Thread
from types import SimpleNamespace

from ._mcp_scripts import (
    _ERROR_FIELDS, _ERROR_PROGRESS, _INPUT_RECORDS, _MAX_FRAME, _MAX_OUTGOING_FRAMES, _SLOT_MAPS, _read,
)
from .bot import Bot


class _RemoteError(RuntimeError):
    def __init__(self, error_type, message):
        super().__init__(message)
        self.error_type = error_type


_REMOTE_ERRORS = {error.__name__: error for error in (
    asyncio.CancelledError, AttributeError, ConnectionError, IndexError, KeyError, NotImplementedError,
    OSError, PermissionError, RuntimeError, TimeoutError, TypeError, ValueError,
)}
_SDK_ERRORS = {cls.__name__: (cls, name) for cls, name in _ERROR_FIELDS.items()}


class _Snapshot(SimpleNamespace):
    """An independent attribute view of a host gameplay record."""

    __slots__ = ()


def _decode(value):
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, dict):
        if value.get("$endpilot") == "record" and value.keys() == {"$endpilot", "fields"}:
            return _Snapshot(**{key: _decode(item) for key, item in value["fields"].items()})
        return {key: _decode(item) for key, item in value.items()}
    return value


def _argument(value):
    kind = type(value)
    if _INPUT_RECORDS.get(kind.__name__) is kind:
        return {"$endpilot": "record", "type": kind.__name__,
                "fields": {field.name: getattr(value, field.name) for field in fields(value)}}
    if kind is range:
        return {"$endpilot": "range", "start": value.start, "stop": value.stop, "step": value.step}
    if kind in (SimpleNamespace, _Snapshot):
        return {"$endpilot": "record", "fields": vars(value)}
    if callable(value):
        raise TypeError("Bot calls cannot receive Python callbacks or predicates. Use a Bot subscription for events.")
    raise TypeError(f"{kind.__name__} cannot cross the MCP script Bot connection")


def _result(value):
    if type(value) is _Snapshot:
        return vars(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _restore(error, detail, names):
    attributes = detail.get("attributes", {})
    for name in names:
        if name in attributes:
            setattr(error, name, _decode(attributes[name]))


def _remote_error(detail):
    kind, message = detail["type"], detail["message"]
    if kind in _SDK_ERRORS:
        cls, name = _SDK_ERRORS[kind]
        # The SDK constructors take domain values. The frame already carries
        # the rendered message and the independent public field.
        error = RuntimeError.__new__(cls)
        RuntimeError.__init__(error, message)
        _restore(error, detail, (name,))
    elif kind in _REMOTE_ERRORS:
        error = _REMOTE_ERRORS[kind](message)
    else:
        error = _RemoteError(kind, message)
    _restore(error, detail, _ERROR_PROGRESS)
    return error


def _reported_error(error):
    kind = error.error_type if isinstance(error, _RemoteError) else type(error).__name__
    detail = {"type": kind, "message": str(error)}
    values = vars(error)
    attributes = {name: values[name] for name in _ERROR_PROGRESS if type(values.get(name)) is _Snapshot}
    name = _ERROR_FIELDS.get(type(error))
    if name is not None and type(values.get(name)) is str:
        attributes[name] = values[name]
    if attributes:
        detail["attributes"] = attributes
    return detail


class _Channel:
    def __init__(self, conn):
        self.conn = conn
        self.loop = asyncio.get_running_loop()
        self.owner = asyncio.current_task()
        self.ids = count(1)
        self.pending = {}
        self.subscriptions = {}
        self.notifications = BoundedSemaphore(_MAX_OUTGOING_FRAMES)
        self.closed = False
        self.terminal_sent = False
        self.pending_lock = Lock()
        self.write_lock = Lock()
        Thread(target=self._receive, daemon=True).start()

    def send(self, frame, *, default=None):
        payload = json.dumps(frame, allow_nan=False, default=default, separators=(",", ":")).encode("utf-8")
        if len(payload) > _MAX_FRAME:
            raise ValueError("MCP script message exceeds the frame size limit")
        with self.write_lock:
            if not self.terminal_sent:
                self.conn.send_bytes(payload)
                self.terminal_sent = frame["op"] in {"result", "error"}

    def _receive(self):
        try:
            while True:
                frame = _read(self.conn)
                if frame["op"] == "reply":
                    with self.pending_lock:
                        future = self.pending.pop(frame["id"], None)
                    if future is not None:
                        future.set_result(frame)
                elif frame["op"] == "event":
                    if not self.notifications.acquire(blocking=False):
                        # The worker loop may be stuck in a callback. Report the
                        # overflow from the reader so the host can reap the run.
                        self.send({"op": "error", "error": {"type": "RuntimeError",
                                   "message": "MCP script worker notification queue is full"}})
                        break
                    self.loop.call_soon_threadsafe(self._dispatch, frame["id"], frame["args"])
        except (EOFError, OSError, RuntimeError, ValueError):
            pass
        finally:
            with self.pending_lock:
                self.closed = True
                pending, self.pending = self.pending, {}
            for future in pending.values():
                future.set_exception(ConnectionError("MCP script Bot connection closed"))
            try:
                self.loop.call_soon_threadsafe(self.owner.cancel)
            except RuntimeError:
                pass

    def _dispatch(self, sub_id, args):
        try:
            callback = self.subscriptions.get(sub_id)
            if callback is not None:
                callback(*_decode(args))
        finally:
            self.notifications.release()

    def start(self, op, name, args, kwargs, *, callback=None):
        future = Future()
        with self.pending_lock:
            if self.closed:
                raise ConnectionError("MCP script Bot connection closed")
            request_id = next(self.ids)
            self.pending[request_id] = future
            if callback is not None:
                self.subscriptions[request_id] = callback
        try:
            self.send({"op": op, "id": request_id, "name": name, "args": args, "kwargs": kwargs},
                      default=_argument)
        except BaseException:
            with self.pending_lock:
                self.pending.pop(request_id, None)
            self.subscriptions.pop(request_id, None)
            raise
        return request_id, future

    @staticmethod
    def reply(frame, name):
        if "error" in frame:
            raise _remote_error(frame["error"])
        value = _decode(frame["value"])
        if name in _SLOT_MAPS and value is not None:
            return {int(slot): item for slot, item in value.items()}
        return value

    def request(self, op, name, args=(), kwargs=None):
        _, future = self.start(op, name, args, kwargs or {})
        return self.reply(future.result(), name)

    async def call(self, name, args, kwargs):
        request_id, future = self.start("call", name, args, kwargs)
        response = asyncio.wrap_future(future)
        try:
            frame = await asyncio.shield(response)
        except asyncio.CancelledError as error:
            if not future.done():
                try:
                    self.send({"op": "cancel", "id": request_id})
                except OSError:
                    pass  # The reader will settle the response when the peer closes.
            while True:
                try:
                    frame = await asyncio.shield(response)
                    break
                except asyncio.CancelledError:
                    continue  # A repeated local cancel cannot abandon host cleanup.
                except ConnectionError:
                    raise error from None
            if (detail := frame.get("error")) is not None and detail["type"] == "CancelledError":
                _restore(error, detail, _ERROR_PROGRESS)
            raise
        return self.reply(frame, name)

    def subscribe(self, name, args, kwargs):
        signature = inspect.signature(getattr(Bot, name))
        arguments = signature.bind(None, *args, **kwargs).arguments
        arguments.pop("self")
        callback = arguments.pop("callback")
        if not callable(callback) or inspect.iscoroutinefunction(callback):
            raise TypeError("Bot subscriptions require a synchronous callback")
        sub_id, future = self.start("subscribe", name, (), arguments, callback=callback)
        try:
            self.reply(future.result(), name)
        except BaseException:
            self.subscriptions.pop(sub_id, None)
            raise

        def unsubscribe():
            if self.subscriptions.pop(sub_id, None) is not None:
                self.request("unsubscribe", "", (sub_id,))

        return unsubscribe


class _BotProxy:
    __slots__ = ("_channel", "_members")

    def __init__(self, channel, members):
        self._channel = channel
        self._members = members

    def __getattr__(self, name):
        kind = self._members.get(name)
        if kind == "property":
            return self._channel.request("get", name)
        if kind == "sync":
            return lambda *args, **kwargs: self._channel.request("call", name, args, kwargs)
        if kind == "async":
            async def call(*args, **kwargs):
                return await self._channel.call(name, args, kwargs)

            return call
        if kind == "subscribe":
            return lambda *args, **kwargs: self._channel.subscribe(name, args, kwargs)
        raise AttributeError(f"Bot.{name} is unavailable in MCP scripts")


class _OutputStream(TextIOBase):
    def __init__(self, channel, stream):
        self.channel = channel
        self.stream = stream

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError("expected text")
        chunk_size = 8192
        for offset in range(0, len(text), chunk_size):
            self.channel.send({"op": "output", "stream": self.stream, "text": text[offset:offset + chunk_size]})
        return len(text)


class _ScriptLog(logging.Handler):
    def __init__(self, channel):
        super().__init__()
        self.output = _OutputStream(channel, "log")
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def emit(self, record):
        self.output.write(self.format(record) + "\n")


def _capture_output(channel):
    # The spawn child inherits the host's OS stdio. Its separate connection
    # carries RPC while script and library reads or writes stay off MCP stdio.
    with open(os.devnull, "r+b", buffering=0) as quiet:
        for channel_fd in (0, 1, 2):
            os.dup2(quiet.fileno(), channel_fd)
    sys.stdout = sys.__stdout__ = _OutputStream(channel, "stdout")
    sys.stderr = sys.__stderr__ = _OutputStream(channel, "stderr")
    logging.getLogger().addHandler(_ScriptLog(channel))


async def _drain_tasks():
    while tasks := asyncio.all_tasks() - {asyncio.current_task()}:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _run(channel, source, filename, args, members):
    cleanups = []

    def defer_cleanup(callback):
        if not callable(callback):
            raise TypeError("expected a cleanup callable")
        cleanups.append(callback)

    def subscribe(register, callback, /, *args, **kwargs):
        return register(callback, *args, **kwargs)

    namespace = {"__name__": "__endpilot_script__", "__file__": filename,
                 "defer_cleanup": defer_cleanup, "subscribe": subscribe}
    bot = _BotProxy(channel, members)
    failure = None
    result = None
    try:
        exec(compile(source, filename, "exec", dont_inherit=True), namespace)
        main = namespace.get("main")
        if not inspect.iscoroutinefunction(main):
            raise TypeError("script must define async main(bot, ...)")
        if isinstance(args, dict):
            result = await main(bot, **args)
        else:
            result = await main(bot, *(args or ()))
    except BaseException as error:
        failure = error

    channel.subscriptions.clear()
    await _drain_tasks()
    while cleanups:
        try:
            cleanup = cleanups.pop()()
            if inspect.isawaitable(cleanup):
                await cleanup
        except BaseException as error:
            if failure is None:
                failure = error
    await _drain_tasks()
    if failure is not None:
        raise failure
    return result


async def _worker(conn, source, filename, args, workspace, members):
    channel = _Channel(conn)
    _capture_output(channel)
    try:
        os.chdir(workspace)
        sys.path.insert(0, os.getcwd())
        if not filename.startswith("<"):
            sys.path.insert(0, str(Path(filename).parent))
        result = await _run(channel, source, filename, args, members)
        channel.send({"op": "result", "value": result}, default=_result)
    except BaseException as error:
        channel.send({"op": "error", "error": _reported_error(error)}, default=_result)


def worker_main(conn, source, filename, args, workspace, members):
    with conn:
        try:
            asyncio.run(_worker(conn, source, filename, args, workspace, members))
        except (BrokenPipeError, EOFError):
            pass
