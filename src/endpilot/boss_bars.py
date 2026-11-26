"""Observe the boss bars sent to one Bedrock player."""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Literal

from bedrock_protocol import BossEventPacket

from .compression import PacketConnection
from .protocol import RawPacket


BossBarEventKind = Literal["created", "updated", "removed"]


@dataclass(frozen=True, slots=True)
class BossBar:
    """A boss bar snapshot keyed by its Bedrock actor unique ID."""

    boss_id: int
    title: str
    progress: float
    color: int
    style: int
    filtered_title: str | None = None


@dataclass(frozen=True, slots=True)
class BossBarEvent:
    """A new snapshot on creation or update, or the last snapshot on removal."""

    kind: BossBarEventKind
    bar: BossBar


class BossBars:
    def __init__(self, packets: PacketConnection, initial_packets: Iterable[BossEventPacket]):
        self.packets = packets
        self._packet_id = packets.protocol.binding.BossEventPacket.ID
        self._event_types = packets.protocol.binding.BossEventUpdateType
        self._bars: dict[int, BossBar] = {}
        self._last_change: tuple[RawPacket, BossBarEvent] | None = None
        for packet in initial_packets:
            self._apply(packet)
        self._unsubscribe = packets.subscribe_raw(self._on_packet)

    def snapshot(self) -> tuple[BossBar, ...]:
        return tuple(self._bars.values())

    def close(self) -> None:
        self._unsubscribe()
        self._bars.clear()
        self._last_change = None

    def _on_packet(self, raw: RawPacket) -> None:
        if raw.packet_id != self._packet_id:
            return
        event = self._apply(self.packets.protocol.decode(raw))
        if event is not None:
            # PacketConnection dispatches this state observer before the waiters below.
            self._last_change = (raw, event)

    def _apply(self, packet: BossEventPacket) -> BossBarEvent | None:
        action, boss_id = packet.event_type, packet.boss_id
        previous = self._bars.get(boss_id)
        types = self._event_types

        kind: BossBarEventKind
        if action == types.ADD:
            bar = BossBar(
                boss_id, packet.name, packet.health_percent, int(packet.color), int(packet.overlay),
                getattr(packet, "filtered_name", "") or None,
            )
            self._bars[boss_id] = bar
            kind = "created" if previous is None else "updated"
        elif previous is None:
            return None
        elif action == types.REMOVE:
            bar = self._bars.pop(boss_id)
            kind = "removed"
        else:
            if action == types.UPDATE_NAME:
                bar = replace(
                    previous, title=packet.name, filtered_title=getattr(packet, "filtered_name", "") or None,
                )
            elif action == types.UPDATE_PERCENT:
                bar = replace(previous, progress=packet.health_percent)
            elif action == types.UPDATE_STYLE:
                bar = replace(previous, color=int(packet.color), style=int(packet.overlay))
            else:
                # UPDATE_PROPERTIES has no sky or fog flags in the current protocol.
                return None
            self._bars[boss_id] = bar
            kind = "updated"

        return BossBarEvent(kind, bar)

    async def wait_for_event(
        self, *, kind: BossBarEventKind | None, boss_id: int | None, timeout: float,
    ) -> BossBarEvent:
        event = None

        def matches(raw: RawPacket) -> bool:
            nonlocal event
            change = self._last_change
            if change is None or change[0] is not raw:
                return False
            observed = change[1]
            if (
                (kind is not None and observed.kind != kind)
                or (boss_id is not None and observed.bar.boss_id != boss_id)
            ):
                return False
            event = observed
            return True

        await self.packets.wait_for_packet(self._packet_id, match=matches, timeout=timeout)
        assert event is not None
        return event
