"""Observe server-issued title and action bar packets."""

from dataclasses import dataclass
from typing import Literal

from bedrock_protocol import SetTitlePacket

from .compression import PacketConnection
from .protocol import RawPacket


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


class Titles:
    def __init__(self, packets: PacketConnection):
        self.packets = packets

    async def wait_for_event(self, *, kind: TitleKind | None, timeout: float) -> TitleEvent:
        event = None

        def matches(raw: RawPacket) -> bool:
            nonlocal event
            packet = self.packets.protocol.decode(raw)
            observed_kind = _TITLE_KINDS.get(packet.type)
            if observed_kind is None or (kind is not None and observed_kind != kind):
                return False
            if observed_kind in ("title", "subtitle", "actionbar"):
                event = TitleEvent(
                    observed_kind,
                    text=packet.title_text,
                    filtered_text=packet.filtered_title_text or None,
                    text_object=packet.type in _TEXT_OBJECT_TYPES,
                )
            elif observed_kind == "times":
                event = TitleEvent(
                    observed_kind,
                    fade_in_ticks=packet.fade_in_time,
                    stay_ticks=packet.stay_time,
                    fade_out_ticks=packet.fade_out_time,
                )
            else:
                event = TitleEvent(observed_kind)
            return True

        await self.packets.wait_for_packet(SetTitlePacket.ID, match=matches, timeout=timeout)
        assert event is not None
        return event
