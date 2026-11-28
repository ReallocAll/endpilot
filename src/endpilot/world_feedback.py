"""Passive observations of sound, particles and block feedback."""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from bedrock_protocol import nbt

from .blocks import Blocks
from .compression import PacketConnection
from .containers import _ENDER_CHEST, _SHULKER_BOXES

if TYPE_CHECKING:
    from .login import _SessionEndState


SoundKind = Literal["play", "stop", "level", "update"]
_Position = tuple[float, float, float]
_Cell = tuple[int, int, int]
_CRACK_EVENTS = {3600: "crack_start", 3601: "crack_end", 3602: "crack_progress"}
_BLOCK_PARTICLE_EVENTS_2225 = frozenset((2001, 3603, 3604, 3605, 3606, 3607, 3608))
_PISTON_STATES = {0: "piston_retracted", 1: "piston_extending", 2: "piston_extended", 3: "piston_retracting"}
_PAIRABLE_CHESTS = frozenset((
    "minecraft:chest", "minecraft:trapped_chest",
    "minecraft:copper_chest", "minecraft:exposed_copper_chest",
    "minecraft:weathered_copper_chest", "minecraft:oxidized_copper_chest",
    "minecraft:waxed_copper_chest", "minecraft:waxed_exposed_copper_chest",
    "minecraft:waxed_weathered_copper_chest", "minecraft:waxed_oxidized_copper_chest",
))
_LID_BLOCKS = _SHULKER_BOXES.union(_PAIRABLE_CHESTS, (_ENDER_CHEST,))
_CHEST_ROOT_TAGS = {"id": nbt.StringTag, "x": nbt.IntTag, "y": nbt.IntTag, "z": nbt.IntTag,
                    "pairx": nbt.IntTag, "pairz": nbt.IntTag, "pairlead": nbt.ByteTag}
_NOTE_INSTRUMENTS_2225 = MappingProxyType({
    0: "note.harp",
    1: "note.bd",
    2: "note.snare",
    3: "note.hat",
    4: "note.bassattack",
    5: "note.flute",
    6: "note.bell",
    7: "note.guitar",
    8: "note.chime",
    9: "note.xylophone",
    10: "note.iron_xylophone",
    11: "note.cow_bell",
    12: "note.didgeridoo",
    13: "note.bit",
    14: "note.banjo",
    15: "note.pling",
    16: "note.trumpet",
    17: "note.trumpet_exposed",
    18: "note.trumpet_weathered",
    19: "note.trumpet_oxidized",
    20: "note.zombie",
    21: "note.skeleton",
    22: "note.creeper",
    23: "note.enderdragon",
    24: "note.witherskeleton",
    25: "note.piglin",
})


def vanilla_note_instruments(protocol: int = 2225) -> Mapping[int, str]:
    """Return the read-only vanilla catalog of Bedrock sound identifiers by instrument ID."""
    if type(protocol) is not int or protocol != 2225:
        raise ValueError(f"unsupported vanilla note instrument protocol {protocol!r}")
    return _NOTE_INSTRUMENTS_2225


def _xyz(pos):
    return (pos.x, pos.y, pos.z)


def _chest_pair(root, position: _Cell) -> tuple[_Cell, int] | None:
    if root is None or any(key not in root or type(root[key]) is not tag for key, tag in _CHEST_ROOT_TAGS.items()):
        return None
    if root["id"].value != "Chest" or tuple(root[axis].value for axis in "xyz") != position:
        return None
    lead = root["pairlead"].value
    if lead not in (0, 1):
        return None
    return (root["pairx"].value, position[1], root["pairz"].value), lead


def _piston_attached_blocks(root) -> tuple[_Cell, ...] | None:
    attached = root["AttachedBlocks"] if "AttachedBlocks" in root else None
    if type(attached) is not nbt.ListTag or len(attached) % 3:
        return None
    coordinates = tuple(attached)
    if any(type(coordinate) is not nbt.IntTag for coordinate in coordinates):
        return None
    return tuple((coordinates[i].value, coordinates[i + 1].value, coordinates[i + 2].value)
                 for i in range(0, len(coordinates), 3))


@dataclass(frozen=True, slots=True)
class SoundEvent:
    """One incoming sound packet, with its decoded fields in ``packet``."""

    kind: SoundKind
    dimension_id: int
    packet: object
    name: str | None = None
    position: _Position | None = None

    @property
    def volume(self) -> float | None:
        return getattr(getattr(self.packet, "event", self.packet), "volume", None)

    @property
    def pitch(self) -> float | None:
        return getattr(getattr(self.packet, "event", self.packet), "pitch", None)


@dataclass(frozen=True, slots=True)
class ParticleEvent:
    """A named particle with the source packet's dimension, attachment and Molang fields."""

    dimension_id: int
    name: str
    position: _Position
    packet: object


@dataclass(frozen=True, slots=True)
class BlockEvent:
    """Block feedback from one packet, with a kind derived when established.

    ``note_tuning`` is the block's tuning index on explicit note tuning events.
    """

    kind: str
    dimension_id: int
    position: _Position | None
    packet: object
    chest_partner: _Cell | None = None
    piston_face: int | None = None
    piston_attached_blocks: tuple[_Cell, ...] | None = None
    note_tuning: int | None = None

    def _note_data(self) -> int | None:
        if self.kind != "note":
            return None
        data = getattr(self.packet, "data", None)
        return data if type(data) is int and data >= 0 else None

    @property
    def note_pitch(self) -> int | None:
        """Return the note sound's low-byte pitch parameter.

        For mob heads, the sound pitch can differ from the block's tuning.
        """
        data = self._note_data()
        return None if data is None else data & 0xFF

    @property
    def instrument_id(self) -> int | None:
        """Return the numeric Bedrock instrument from a note event."""
        if self.kind == "note_tuning":
            return getattr(self.packet, "b0", None)
        data = self._note_data()
        return None if data is None else data >> 8

    @property
    def instrument_sound(self) -> str | None:
        """Return the vanilla Bedrock sound identifier for a known note instrument."""
        instrument_id = self.instrument_id
        return None if instrument_id is None else _NOTE_INSTRUMENTS_2225.get(instrument_id)


class WorldFeedback:
    def __init__(self, packets: PacketConnection, blocks: Blocks, end_state: "_SessionEndState"):
        self.packets = packets
        self.blocks = blocks
        self.binding = packets.protocol.binding
        self._is_2225 = packets.protocol.number == 2225
        b = self.binding
        self._sound_ids = (b.PlaySoundPacket.ID, b.StopSoundPacket.ID, b.LevelSoundEventPacket.ID)
        update = getattr(b, "ClientboundUpdateSoundDataPacket", None)
        if update is not None:
            self._sound_ids += (update.ID,)
        self._particle_ids = (b.SpawnParticleEffectPacket.ID,)
        self._block_ids = (b.BlockEventPacket.ID,)
        if self._is_2225:
            self._block_ids += (b.BlockActorDataPacket.ID, b.LevelEventPacket.ID, b.LevelSoundEventPacket.ID)
        self._end_state = end_state
        self._closed = False
        self._subscriptions: dict[object, Callable[[], None]] = {}
        self._unsubscribe_reader_end: Callable[[], None] | None = None

    def close(self, _cause: BaseException | None = None) -> None:
        self._closed = True
        if self._unsubscribe_reader_end is not None:
            self._unsubscribe_reader_end()
            self._unsubscribe_reader_end = None
        for unsubscribe in self._subscriptions.values():
            unsubscribe()
        self._subscriptions.clear()

    def _event(self, raw, project, filters):
        # Capture the dimension during this dispatch, before the next packet can change it.
        event = project(self.packets.protocol.decode(raw), self.blocks.dimension_id)
        if event is not None and all(value is None or getattr(event, field) == value for field, value in filters.items()):
            return event
        return None

    async def _wait(self, packet_ids, project, timeout, **filters):
        observed = None

        def observe(raw):
            nonlocal observed
            observed = self._event(raw, project, filters)
            return observed is not None

        await self.packets.wait_for_packet(packet_ids, match=observe, timeout=timeout)
        return observed

    def _subscribe(self, packet_ids, project, callback, **filters) -> Callable[[], None]:
        if self._closed or self._end_state.result is not None or self.packets._reader_end_result()[0]:
            raise ConnectionError("world feedback subscription stream is closed")
        registrations = self._subscriptions
        key = object()

        def observe(raw):
            # A previous observer can unsubscribe this one during the current dispatch.
            if key not in registrations:
                return
            if raw.packet_id == self.binding.DisconnectPacket.ID:
                self.close()
                return
            if raw.packet_id not in packet_ids:
                return
            event = self._event(raw, project, filters)
            if event is None:
                return
            try:
                callback(event)
            except asyncio.CancelledError as error:
                # A callback can raise cancellation without the shared reader being cancelled.
                asyncio.get_running_loop().call_exception_handler({
                    "message": "packet observer failed", "exception": error, "callback": callback,
                })

        def unsubscribe():
            remove_observer = registrations.pop(key, None)
            if remove_observer is not None:
                remove_observer()

        registrations[key] = self.packets.subscribe_raw(observe)
        if self._unsubscribe_reader_end is None:
            self._unsubscribe_reader_end = self.packets.subscribe_reader_end(self.close)
        return unsubscribe

    def _sound(self, packet, dimension: int) -> SoundEvent:
        b = self.binding
        if packet.ID == b.PlaySoundPacket.ID:
            # Vanilla 2225 encodes PlaySound positions in eighths of a block.
            position = tuple(axis / 8 for axis in _xyz(packet.pos)) if self._is_2225 else None
            return SoundEvent("play", dimension, packet, packet.name, position)
        if packet.ID == b.StopSoundPacket.ID:
            return SoundEvent("stop", dimension, packet, packet.name)
        if packet.ID == b.LevelSoundEventPacket.ID:
            name = packet.sound_event if isinstance(packet.sound_event, str) else None
            return SoundEvent("level", dimension, packet, name, _xyz(packet.pos))
        return SoundEvent("update", dimension, packet)

    async def wait_for_sound(self, name: str | None, kind: SoundKind | None, timeout: float) -> SoundEvent:
        return await self._wait(self._sound_ids, self._sound, timeout, name=name, kind=kind)

    def subscribe_sound(
        self, callback: Callable[[SoundEvent], None], *, name: str | None = None, kind: SoundKind | None = None,
    ) -> Callable[[], None]:
        return self._subscribe(self._sound_ids, self._sound, callback, name=name, kind=kind)

    @staticmethod
    def _particle(packet, dimension: int) -> ParticleEvent:
        return ParticleEvent(dimension, packet.effect_name, _xyz(packet.pos), packet)

    async def wait_for_particle(self, name: str | None, timeout: float) -> ParticleEvent:
        return await self._wait(self._particle_ids, self._particle, timeout, name=name)

    def subscribe_particle(
        self, callback: Callable[[ParticleEvent], None], *, name: str | None = None,
    ) -> Callable[[], None]:
        return self._subscribe(self._particle_ids, self._particle, callback, name=name)

    def _block_state(self, position: _Cell):
        block = self.blocks.observed_block(position)
        return None if block is None else self.blocks._vanilla_state(block.runtime_id)

    def _chest_partner(self, position: _Cell, chest_name: str) -> _Cell | None:
        first = _chest_pair(self.blocks.observed_block_entity(position), position)
        if first is None:
            return None
        partner, lead = first
        if abs(partner[0] - position[0]) + abs(partner[2] - position[2]) != 1:
            return None
        state = self._block_state(partner)
        if state is None or state.name != chest_name:
            return None
        return partner if _chest_pair(self.blocks.observed_block_entity(partner), partner) == (position, 1 - lead) else None

    def _block(self, packet, dimension: int) -> BlockEvent | None:
        b = self.binding
        position = _xyz(packet.pos)
        chest_partner = None
        piston_face = None
        piston_attached_blocks = None
        if packet.ID == b.BlockEventPacket.ID:
            kind = "block"
            if self._is_2225 and packet.b0 == 1 and packet.b1 in (0, 1):
                state = self._block_state(position)
                if state is not None and state.name in _LID_BLOCKS:
                    kind = "chest_open" if packet.b1 == 1 else "chest_close"
                    if state.name in _PAIRABLE_CHESTS:
                        chest_partner = self._chest_partner(position, state.name)
        elif packet.ID == b.BlockActorDataPacket.ID:
            tag = packet.data
            actor_id = tag["id"] if "id" in tag else None
            if not isinstance(actor_id, nbt.StringTag) or actor_id.value != "PistonArm":
                return None
            piston_attached_blocks = _piston_attached_blocks(tag)
            state = tag["State"] if "State" in tag else None
            kind = _PISTON_STATES.get(state.value, "piston") if isinstance(state, nbt.ByteTag) else "piston"
            if kind != "piston":
                block_state = self._block_state(position)
                if block_state is not None:
                    piston_face = block_state.piston_face
        elif packet.ID == b.LevelEventPacket.ID:
            kind = _CRACK_EVENTS.get(packet.event_id)
            if kind is None:
                return None
            if kind == "crack_end" and position == (0, 0, 0):
                # Vanilla sent an all-zero stop position after breaking a non-origin block.
                position = None
        elif self._is_2225 and packet.ID == b.LevelSoundEventPacket.ID and packet.sound_event == "note":
            kind = "note"
        else:
            return None
        return BlockEvent(kind, dimension, position, packet, chest_partner, piston_face, piston_attached_blocks)

    @staticmethod
    def _block_particle(packet, dimension: int) -> BlockEvent | None:
        if packet.event_id in _BLOCK_PARTICLE_EVENTS_2225:
            return BlockEvent("block_particle", dimension, _xyz(packet.pos), packet)
        return None

    def _note_tuning(self, packet, dimension: int) -> BlockEvent | None:
        if not 0 <= packet.b1 <= 24:
            return None
        position = _xyz(packet.pos)
        state = self._block_state(position)
        if state is not None and state.name == "minecraft:noteblock":
            return BlockEvent("note_tuning", dimension, position, packet, note_tuning=packet.b1)
        return None

    def _block_event_source(self, kind: str | None):
        if kind == "block_particle":
            packet_ids = (self.binding.LevelEventPacket.ID,) if self._is_2225 else ()
            return packet_ids, self._block_particle
        if kind == "note_tuning":
            packet_ids = (self.binding.BlockEventPacket.ID,) if self._is_2225 else ()
            return packet_ids, self._note_tuning
        return self._block_ids, self._block

    async def wait_for_block_event(self, position: _Position | None, kind: str | None, timeout: float) -> BlockEvent:
        packet_ids, project = self._block_event_source(kind)
        return await self._wait(packet_ids, project, timeout, position=position, kind=kind)

    def subscribe_block_events(
        self, callback: Callable[[BlockEvent], None], *, position: _Position | None = None, kind: str | None = None,
    ) -> Callable[[], None]:
        packet_ids, project = self._block_event_source(kind)
        return self._subscribe(packet_ids, project, callback, position=position, kind=kind)
