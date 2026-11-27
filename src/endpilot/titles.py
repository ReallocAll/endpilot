"""Observe server-issued title and action bar packets."""

import asyncio
from collections.abc import Callable
from copy import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from bedrock_protocol import DecodeError, SetTitlePacket

from .compression import PacketConnection
from .protocol import RawPacket
from .recoverable_error import RecoverableError

if TYPE_CHECKING:
    from .login import _SessionEndState


TitleKind = Literal["title", "subtitle", "actionbar", "times", "clear", "reset"]

_TitleType = SetTitlePacket.TitleType
_TITLE_KINDS: dict[_TitleType, TitleKind] = {
    _TitleType.TITLE: "title",
    _TitleType.SUBTITLE: "subtitle",
    _TitleType.ACTIONBAR: "actionbar",
    _TitleType.TITLE_TEXT_OBJECT: "title",
    _TitleType.SUBTITLE_TEXT_OBJECT: "subtitle",
    _TitleType.ACTIONBAR_TEXT_OBJECT: "actionbar",
    _TitleType.TIMES: "times",
    _TitleType.CLEAR: "clear",
    _TitleType.RESET: "reset",
}
_TEXT_OBJECT_TYPES = (
    _TitleType.TITLE_TEXT_OBJECT, _TitleType.SUBTITLE_TEXT_OBJECT, _TitleType.ACTIONBAR_TEXT_OBJECT,
)


@dataclass(frozen=True, slots=True)
class TitleEvent:
    """One title update as sent by the server."""

    kind: TitleKind
    text: str | None = None
    filtered_text: str | None = None
    text_object: bool = False
    fade_in_ticks: int | None = None
    stay_ticks: int | None = None
    fade_out_ticks: int | None = None


def _title_event(packet: SetTitlePacket) -> TitleEvent | None:
    kind = _TITLE_KINDS.get(packet.type)
    if kind is None:
        return None
    if kind in ("title", "subtitle", "actionbar"):
        return TitleEvent(
            kind,
            text=packet.title_text,
            filtered_text=packet.filtered_title_text or None,
            text_object=packet.type in _TEXT_OBJECT_TYPES,
        )
    if kind == "times":
        return TitleEvent(
            kind,
            fade_in_ticks=packet.fade_in_time,
            stay_ticks=packet.stay_time,
            fade_out_ticks=packet.fade_out_time,
        )
    return TitleEvent(kind)


class Titles:
    def __init__(self, packets: PacketConnection, end_state: "_SessionEndState", *,
                 _publish_error: Callable[[RecoverableError], None] | None = None):
        self.packets = packets
        self._end_state = end_state
        self._publish_error = _publish_error
        self._subscriptions: dict[object, Callable[[], None]] = {}
        self._unsubscribe_reader_end: Callable[[], None] | None = None
        self._closed = False
        self._last_event: tuple[RawPacket, TitleEvent | DecodeError | None] | None = None
        self._decode_error_reported = False

    def _read_event(self, raw: RawPacket) -> TitleEvent | DecodeError | None:
        # Waiters and subscribers see the same raw packet during dispatch.
        # Use identity so identical packets received twice are projected independently.
        cached = self._last_event
        if cached is not None and cached[0] is raw:
            return cached[1]

        event: TitleEvent | DecodeError | None
        try:
            event = _title_event(self.packets.protocol.decode(raw))
        except DecodeError as error:
            event = error.with_traceback(None)
        self._last_event = raw, event
        if self._closed and cached is None:
            asyncio.get_running_loop().call_soon(self._clear_last_event)
        self._decode_error_reported = False
        if isinstance(event, DecodeError) and self._publish_error is not None:
            self._publish_error(RecoverableError(
                "titles", raw.packet_id, type(event).__name__, "could not decode SetTitle",
            ))
        return event

    async def wait_for_event(self, *, kind: TitleKind | None, timeout: float) -> TitleEvent:
        event = None

        def matches(raw: RawPacket) -> bool:
            nonlocal event
            observed = self._read_event(raw)
            if isinstance(observed, DecodeError):
                # Each wait raises its own exception with the native error details.
                raise copy(observed)
            if observed is None or (kind is not None and observed.kind != kind):
                return False
            event = observed
            return True

        await self.packets.wait_for_packet(SetTitlePacket.ID, match=matches, timeout=timeout)
        assert event is not None
        return event

    def subscribe(
        self, callback: Callable[[TitleEvent], None], *, kind: TitleKind | None = None,
    ) -> Callable[[], None]:
        if self._closed or self._end_state.result is not None or self.packets._reader_end_result()[0]:
            raise ConnectionError("title subscription stream is closed")

        registrations = self._subscriptions
        key = object()

        def observe(raw: RawPacket) -> None:
            # The raw observer snapshot excludes new registrations. This check
            # skips observers removed since that snapshot was taken.
            if key not in registrations:
                return
            if raw.packet_id == self.packets.protocol.binding.DisconnectPacket.ID:
                self.close()
                return
            if raw.packet_id != SetTitlePacket.ID:
                return
            event = self._read_event(raw)
            if isinstance(event, DecodeError):
                if not self._decode_error_reported:
                    self._decode_error_reported = True
                    asyncio.get_running_loop().call_exception_handler({
                        "message": "title packet decode failed", "exception": copy(event),
                    })
                return
            if event is None or (kind is not None and kind != event.kind):
                return
            try:
                callback(event)
            except (Exception, asyncio.CancelledError) as error:
                asyncio.get_running_loop().call_exception_handler({
                    "message": "packet observer failed", "exception": error, "callback": callback,
                })

        unsubscribe_raw = self.packets.subscribe_raw(observe)
        if self._unsubscribe_reader_end is None:
            self._unsubscribe_reader_end = self.packets.subscribe_reader_end(self.close)

        def detach() -> None:
            unsubscribe_raw()
            if not registrations and self._unsubscribe_reader_end is not None:
                self._unsubscribe_reader_end()
                self._unsubscribe_reader_end = None

        registrations[key] = detach

        def unsubscribe() -> None:
            remove = registrations.pop(key, None)
            if remove is not None:
                remove()

        return unsubscribe

    def _clear_last_event(self) -> None:
        self._last_event = None

    def close(self, _cause: BaseException | None = None) -> None:
        if not self._closed and self._last_event is not None:
            # Waiters already in the current dispatch still share the cached result.
            asyncio.get_running_loop().call_soon(self._clear_last_event)
        self._closed = True
        while self._subscriptions:
            _, detach = self._subscriptions.popitem()
            detach()
