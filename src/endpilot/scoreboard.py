"""Observe the server's scoreboard for one player session."""

import asyncio
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from bedrock_protocol import (
    ChangeEntityScore,
    ChangeFakePlayerScore,
    ChangePlayerScore,
    ObjectiveSortOrder,
    RemoveObjectivePacket,
    RemoveScore,
    ScoreboardIdentityPacketType,
    SetDisplayObjectivePacket,
    SetScoreboardIdentityPacket,
    SetScorePacket,
)

from .compression import PacketConnection
from .protocol import RawPacket


SCOREBOARD_PACKET_IDS = (
    RemoveObjectivePacket.ID, SetDisplayObjectivePacket.ID,
    SetScorePacket.ID, SetScoreboardIdentityPacket.ID,
)


@dataclass(frozen=True, slots=True)
class ScoreboardScore:
    scoreboard_id: int
    value: int
    kind: Literal["player", "entity", "fake_player"]
    player_unique_id: int | None = None
    entity_unique_id: int | None = None
    fake_player_name: str | None = None


@dataclass(frozen=True, slots=True)
class ScoreboardObjective:
    name: str
    display_name: str | None
    criteria_name: str | None
    scores: tuple[ScoreboardScore, ...]


@dataclass(frozen=True, slots=True)
class ScoreboardDisplay:
    objective_name: str
    display_name: str
    criteria_name: str
    sort_order: ObjectiveSortOrder


@dataclass(frozen=True, slots=True)
class ScoreboardObjectiveChange:
    name: str
    before: ScoreboardObjective | None
    after: ScoreboardObjective | None


@dataclass(frozen=True, slots=True)
class ScoreboardDisplayChange:
    slot: str
    before: ScoreboardDisplay | None
    after: ScoreboardDisplay | None


@dataclass(frozen=True, slots=True)
class ScoreboardScoreChange:
    objective_name: str
    before: ScoreboardScore | None
    after: ScoreboardScore | None


@dataclass(frozen=True, slots=True)
class ScoreboardIdentityChange:
    scoreboard_id: int
    before: int | None
    after: int | None


type ScoreboardChange = (
    ScoreboardObjectiveChange | ScoreboardDisplayChange | ScoreboardScoreChange | ScoreboardIdentityChange
)
type ScoreboardObserver = Callable[[tuple[ScoreboardChange, ...]], None]


@dataclass(frozen=True, slots=True)
class ScoreboardSnapshot:
    """An independent view of scoreboard packets received in this session."""

    objectives: Mapping[str, ScoreboardObjective]
    display_slots: Mapping[str, ScoreboardDisplay]
    player_identities: Mapping[int, int]

    def scores_in_slot(self, slot: str) -> tuple[ScoreboardScore, ...]:
        """Return the displayed scores in the server-specified numeric order."""
        display = self.display_slots.get(slot)
        if display is None:
            return ()
        scores = self.objectives[display.objective_name].scores
        return tuple(sorted(scores, key=lambda entry: entry.value,
                            reverse=display.sort_order == ObjectiveSortOrder.DESCENDING))


@dataclass(slots=True)
class _Objective:
    display: ScoreboardDisplay | None = None
    scores: dict[int, ScoreboardScore] = field(default_factory=dict)

    def snapshot(self, name: str) -> ScoreboardObjective:
        return ScoreboardObjective(
            name,
            self.display.display_name if self.display is not None else None,
            self.display.criteria_name if self.display is not None else None,
            tuple(self.scores.values()),
        )


class Scoreboard:
    def __init__(self, packets: PacketConnection, initial_packets: Iterable[RawPacket]):
        self.packets = packets
        self._objectives: defaultdict[str, _Objective] = defaultdict(_Objective)
        self._display_slots: dict[str, ScoreboardDisplay] = {}
        self._player_identities: dict[int, int] = {}
        self._observers: dict[object, ScoreboardObserver] = {}
        for raw in initial_packets:
            self._on_packet(raw)
        self._unsubscribe = packets.subscribe_raw(self._on_packet)

    def close(self) -> None:
        self._unsubscribe()
        self._observers.clear()

    def subscribe(self, callback: ScoreboardObserver) -> Callable[[], None]:
        observers = self._observers
        token = object()
        observers[token] = callback

        def unsubscribe() -> None:
            observers.pop(token, None)

        return unsubscribe

    def snapshot(self) -> ScoreboardSnapshot:
        return ScoreboardSnapshot(
            MappingProxyType({
                name: objective.snapshot(name) for name, objective in self._objectives.items()
            }),
            MappingProxyType(self._display_slots.copy()),
            MappingProxyType(self._player_identities.copy()),
        )

    def _on_packet(self, raw: RawPacket) -> None:
        if raw.packet_id not in SCOREBOARD_PACKET_IDS:
            return
        changes = tuple(self._apply_packet(self.packets.protocol.decode(raw)))
        if not changes:
            return
        for observer in tuple(self._observers.values()):
            try:
                observer(changes)
            except (Exception, asyncio.CancelledError) as error:
                asyncio.get_running_loop().call_exception_handler({
                    "message": "scoreboard observer failed",
                    "exception": error,
                    "callback": observer,
                })

    def _apply_packet(self, packet) -> Iterator[ScoreboardChange]:
        if isinstance(packet, SetDisplayObjectivePacket):
            name = packet.objective_name
            slot = packet.display_slot_name
            previous_display = self._display_slots.get(slot)
            if not name:
                if previous_display is not None:
                    del self._display_slots[slot]
                    yield ScoreboardDisplayChange(slot, previous_display, None)
                return
            display = ScoreboardDisplay(
                name, packet.objective_display_name, packet.criteria_name, packet.sort_order,
            )
            previous_objective = self._objectives.get(name)
            before = previous_objective.snapshot(name) if previous_objective is not None else None
            self._objectives[name].display = display
            after = self._objectives[name].snapshot(name)
            if before != after:
                yield ScoreboardObjectiveChange(name, before, after)
            if slot and previous_display != display:
                self._display_slots[slot] = display
                yield ScoreboardDisplayChange(slot, previous_display, display)
        elif isinstance(packet, RemoveObjectivePacket):
            name = packet.objective_name
            removed = self._objectives.pop(name, None)
            if removed is not None:
                yield ScoreboardObjectiveChange(name, removed.snapshot(name), None)
            for slot, display in tuple(self._display_slots.items()):
                if display.objective_name == name:
                    del self._display_slots[slot]
                    yield ScoreboardDisplayChange(slot, display, None)
        elif isinstance(packet, SetScorePacket):
            for entry in packet.score_info:
                yield from self._apply_score(entry)
        elif isinstance(packet, SetScoreboardIdentityPacket):
            for identity in packet.identity_info:
                scoreboard_id = identity.scoreboard_id
                before = self._player_identities.get(scoreboard_id)
                if packet.type == ScoreboardIdentityPacketType.REMOVE:
                    if before is not None:
                        del self._player_identities[scoreboard_id]
                        yield ScoreboardIdentityChange(scoreboard_id, before, None)
                elif identity.player_id is not None and identity.player_id != before:
                    self._player_identities[scoreboard_id] = identity.player_id
                    yield ScoreboardIdentityChange(scoreboard_id, before, identity.player_id)

    def _apply_score(
        self, entry: RemoveScore | ChangePlayerScore | ChangeEntityScore | ChangeFakePlayerScore,
    ) -> Iterator[ScoreboardChange]:
        name = entry.objective_name
        scoreboard_id = entry.scoreboard_id
        if isinstance(entry, RemoveScore):
            targets = ((name, self._objectives.get(name)),) if name else self._objectives.items()
            for objective_name, objective in targets:
                if objective is not None:
                    removed = objective.scores.pop(scoreboard_id, None)
                    if removed is not None:
                        yield ScoreboardScoreChange(objective_name, removed, None)
            return

        value = entry.score_value
        if isinstance(entry, ChangePlayerScore):
            score = ScoreboardScore(scoreboard_id, value, "player", player_unique_id=entry.player_id.actor_unique_id)
        elif isinstance(entry, ChangeEntityScore):
            score = ScoreboardScore(scoreboard_id, value, "entity", entity_unique_id=entry.entity_id)
        else:
            score = ScoreboardScore(scoreboard_id, value, "fake_player", fake_player_name=entry.fake_player_name)
        if name not in self._objectives:
            yield ScoreboardObjectiveChange(name, None, self._objectives[name].snapshot(name))
        scores = self._objectives[name].scores
        previous = scores.get(scoreboard_id)
        if previous != score:
            scores[scoreboard_id] = score
            yield ScoreboardScoreChange(name, previous, score)
