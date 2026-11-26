"""Packet observations for ordinary player QA scenarios."""

import time
from collections import Counter, deque
from dataclasses import dataclass, field

from .protocol import Protocol, RawPacket


@dataclass(frozen=True, slots=True)
class ReceivedPacket:
    """One raw observation with a logical receive tick when a client clock is available."""

    index: int
    received_at: float
    protocol: int
    name: str
    raw: RawPacket
    tick: int | None = None
    _decoded: object = field(default=None, repr=False, compare=False)
    _decode_error: str | None = field(default=None, repr=False, compare=False)


class PacketTimeline:
    """Opt-in receive history and typed send counts for a connection attempt."""

    def __init__(self, max_kept: int = 50_000, *, _retain_legacy_recipes: bool = False):
        self._received: deque[ReceivedPacket] = deque(maxlen=max_kept)
        self._counts: Counter[str] = Counter()
        self._sent_counts: Counter[str] = Counter()
        self._next_index = 0
        self._disconnect_reason: str | None = None
        self._retain_legacy_recipes = _retain_legacy_recipes

    @property
    def next_index(self) -> int:
        """Return the inclusive cursor for packets received from this point onward."""
        return self._next_index

    def received(self, name: str | None = None, *, since: int = 0) -> tuple[ReceivedPacket, ...]:
        """Return observations by name, using ``#ID`` for unknown or unreadable packets."""
        return tuple(packet for packet in self._received
                     if packet.index >= since and (name is None or packet.name == name))

    def received_count(self, name: str) -> int:
        """Count all observations of a named packet, including evicted entries."""
        return self._counts[name]

    def sent_count(self, name: str) -> int:
        """Count successful submissions of an exact typed packet class name."""
        return self._sent_counts[name]

    def disconnect_reason(self) -> str | None:
        """Return this attempt's reason, including a rejection during login."""
        return self._disconnect_reason

    def _record_transport_failure(self, error: Exception | None) -> None:
        if error is not None and self._disconnect_reason is None:
            self._disconnect_reason = str(error) or "connection closed"

    def _record_sent(self, name: str) -> None:
        self._sent_counts[name] += 1

    def _record(self, protocol: Protocol, raw: RawPacket, *, tick: int | None = None) -> None:
        received_at = time.monotonic()
        packet_type = protocol.binding.packets.get(raw.packet_id)
        name = f"#{raw.packet_id}"
        packet = None
        error = None
        if packet_type is None:
            error = "no packet class for this id"
        else:
            try:
                packet = protocol.decode(raw)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            else:
                name = packet_type.__name__
        self._received.append(ReceivedPacket(
            self._next_index, received_at, protocol.number, name, raw, tick, packet, error,
        ))
        self._next_index += 1
        self._counts[name] += 1
        if name == "DisconnectPacket":
            reason = getattr(packet.reason, "name", str(packet.reason))
            message = packet.messages.message if packet.messages is not None else ""
            self._disconnect_reason = f"{reason}: {message}" if message else reason
        elif name == "PlayStatusPacket" and int(packet.status) not in (0, 3):
            status = getattr(packet.status, "name", str(packet.status))
            self._disconnect_reason = f"PlayStatus {status}"
