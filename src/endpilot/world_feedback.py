"""Passive observations of sound, particles and block feedback."""

from dataclasses import dataclass
from typing import Literal

from bedrock_protocol import nbt

from .blocks import Blocks
from .compression import PacketConnection


SoundKind = Literal["play", "stop", "level", "update"]
_Position = tuple[float, float, float]
_Cell = tuple[int, int, int]
_CRACK_EVENTS = {3600: "crack_start", 3601: "crack_end", 3602: "crack_progress"}
_PISTON_STATES = {0: "piston_retracted", 1: "piston_extending", 2: "piston_extended", 3: "piston_retracting"}
_CHEST_ROOT_TAGS = {"id": nbt.StringTag, "x": nbt.IntTag, "y": nbt.IntTag, "z": nbt.IntTag,
                    "pairx": nbt.IntTag, "pairz": nbt.IntTag, "pairlead": nbt.ByteTag}


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
    """Block feedback from one packet, with a kind derived when established."""

    kind: str
    dimension_id: int
    position: _Position | None
    packet: object
    chest_partner: _Cell | None = None
    piston_face: int | None = None
    piston_attached_blocks: tuple[_Cell, ...] | None = None

    @property
    def note_pitch(self) -> int | None:
        """Return the semitone index from a note event's packed integer."""
        return self.packet.data & 0xFF if self.kind == "note" and self.packet.data >= 0 else None

    @property
    def instrument_id(self) -> int | None:
        """Return the numeric Bedrock instrument from a note event."""
        return self.packet.data >> 8 if self.kind == "note" and self.packet.data >= 0 else None


class WorldFeedback:
    def __init__(self, packets: PacketConnection, blocks: Blocks):
        self.packets = packets
        self.blocks = blocks
        self.binding = packets.protocol.binding
        self._is_2225 = packets.protocol.number == 2225
        b = self.binding
        self._sound_ids = (b.PlaySoundPacket.ID, b.StopSoundPacket.ID, b.LevelSoundEventPacket.ID)
        update = getattr(b, "ClientboundUpdateSoundDataPacket", None)
        if update is not None:
            self._sound_ids += (update.ID,)
        self._block_ids = (b.BlockEventPacket.ID,)
        if self._is_2225:
            self._block_ids += (b.BlockActorDataPacket.ID, b.LevelEventPacket.ID, b.LevelSoundEventPacket.ID)

    async def _wait(self, packet_ids, project, matches, timeout):
        observed = None

        def observe(raw):
            nonlocal observed
            # Capture the dimension during this dispatch, before the next packet can change it.
            event = project(self.packets.protocol.decode(raw), self.blocks.dimension_id)
            if event is None or not matches(event):
                return False
            observed = event
            return True

        await self.packets.wait_for_packet(packet_ids, match=observe, timeout=timeout)
        return observed

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
        return await self._wait(
            self._sound_ids, self._sound,
            lambda event: (name is None or event.name == name) and (kind is None or event.kind == kind), timeout,
        )

    async def wait_for_particle(self, name: str | None, timeout: float) -> ParticleEvent:
        def particle(packet, dimension):
            return ParticleEvent(dimension, packet.effect_name, _xyz(packet.pos), packet)

        return await self._wait(
            self.binding.SpawnParticleEffectPacket.ID, particle,
            lambda event: name is None or event.name == name, timeout,
        )

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
                if state is not None and state.name in ("minecraft:chest", "minecraft:trapped_chest"):
                    kind = "chest_open" if packet.b1 == 1 else "chest_close"
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
        else:
            if packet.sound_event != "note":
                return None
            kind = "note"
        return BlockEvent(kind, dimension, position, packet, chest_partner, piston_face, piston_attached_blocks)

    async def wait_for_block_event(self, position: _Position | None, kind: str | None, timeout: float) -> BlockEvent:
        return await self._wait(
            self._block_ids, self._block,
            lambda event: (position is None or event.position == position) and (kind is None or event.kind == kind),
            timeout,
        )
