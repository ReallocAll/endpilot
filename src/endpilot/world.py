"""Small, session-scoped Bedrock subchunk observation for navigation clients.

Subscribe WorldQuery.observe to packet_decoded, dimension and state events.
Use BotOptions(validate_all_packets=True, client_cache=False): ordinary
ChunkEvent snapshots intentionally contain metadata, not block storage.
Unknown space and unsupported collision shapes remain unknown.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from struct import unpack_from

from .events import DimensionEvent, PacketDecodedEvent, StateEvent
from .types import BotState


def _block_hash(name: str) -> int:
    """Bedrock hashed network ID for a block with empty states (FNV-1a/NBT)."""
    def string(value: str) -> bytes:
        encoded = value.encode("utf-8")
        return len(encoded).to_bytes(2, "little") + encoded

    data = b"\x0a\x00\x00\x08" + string("name") + string(name)
    data += b"\x0a" + string("states") + b"\x00\x00"
    value = 2166136261
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


_AIR = _block_hash("minecraft:air")
_KNOWN = {
    _AIR: ("minecraft:air", False),
    **{
        _block_hash(name): (name, True)
        for name in ("minecraft:stone", "minecraft:glass", "minecraft:diamond_block")
    },
}


@dataclass(frozen=True, slots=True)
class Block:
    runtime_id: int
    name: str | None
    solid: bool | None

    @property
    def air(self) -> bool:
        return self.runtime_id == _AIR


def _block(runtime_id: int) -> Block:
    name, solid = _KNOWN.get(runtime_id, (None, None))
    return Block(runtime_id, name, solid)


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    number = 0
    for shift in range(0, 35, 7):
        if offset >= len(data):
            raise ValueError("truncated palette varint")
        byte = data[offset]
        offset += 1
        if shift == 28 and byte > 0x0F:
            raise ValueError("palette varint overflow")
        number |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return (number >> 1) ^ -(number & 1), offset
    raise ValueError("unterminated palette varint")


@dataclass(frozen=True, slots=True)
class _Storage:
    bits: int
    words: bytes
    palette: tuple[int, ...]

    def at(self, x: int, y: int, z: int) -> int:
        index = (x << 8) | (z << 4) | y
        if not self.bits:
            return self.palette[0]
        cells_per_word = 32 // self.bits
        word = unpack_from("<I", self.words, (index // cells_per_word) * 4)[0]
        palette_index = (word >> ((index % cells_per_word) * self.bits)) & ((1 << self.bits) - 1)
        if palette_index >= len(self.palette):
            raise ValueError("subchunk palette index out of range")
        return self.palette[palette_index]


def _decode_subchunk(payload: bytes, y: int) -> tuple[_Storage, ...]:
    if len(payload) < 3 or payload[0] != 9:
        raise ValueError("unsupported subchunk version")
    if int.from_bytes(payload[2:3], "little", signed=True) != y:
        raise ValueError("subchunk Y mismatch")
    count = payload[1]
    if count > 2 or len(payload) > 131072:
        raise ValueError("unsupported subchunk size or layer count")
    offset = 3
    layers = []
    for _ in range(count):
        if offset >= len(payload):
            raise ValueError("missing storage header")
        header = payload[offset]
        offset += 1
        if not header & 1:
            raise ValueError("disk palette in network storage")
        bits = header >> 1
        if bits not in (0, 1, 2, 3, 4, 5, 6, 8, 16):
            raise ValueError("unsupported palette bit width")
        word_count = (4096 + (32 // bits) - 1) // (32 // bits) if bits else 0
        end = offset + word_count * 4
        if end > len(payload):
            raise ValueError("truncated subchunk index words")
        words = payload[offset:end]
        offset = end
        palette_size = 1
        if bits:
            palette_size, offset = _varint(payload, offset)
        if not 1 <= palette_size <= 4096:
            raise ValueError("invalid subchunk palette count")
        palette = []
        for _ in range(palette_size):
            value, offset = _varint(payload, offset)
            palette.append(value & 0xFFFFFFFF)
        layers.append(_Storage(bits, words, tuple(palette)))
    if offset != len(payload):
        raise ValueError("trailing subchunk data")
    return tuple(layers)


class _Slice:
    __slots__ = ("layers", "patches")

    def __init__(self, layers: tuple[_Storage, ...] | None = None):
        self.layers = layers
        self.patches: dict[tuple[int, int], int] = {}

    def block(self, index: int, layer: int) -> int | None:
        patched = self.patches.get((index, layer))
        if patched is not None:
            return patched
        if self.layers is None:
            return None
        if layer >= len(self.layers):
            return _AIR
        return self.layers[layer].at(index >> 8, index & 15, (index >> 4) & 15)


class WorldQuery:
    """Observe only verified subchunks and block updates from one Bot.

    Returns None for unknown blocks and collision properties. This does not
    predict chunk loading, entities, fluids or complex voxel collision.
    """

    def __init__(self, bot, *, max_subchunks: int = 512):
        if not 1 <= max_subchunks <= 8192:
            raise ValueError("max_subchunks out of range")
        self.bot = bot
        self.max_subchunks = max_subchunks
        self._session_id: int | None = None
        self._dimension: int | None = None
        self._hashes = False
        self._publisher: tuple[int, int, int] | None = None
        self._slices: OrderedDict[tuple[int, int, int], _Slice] = OrderedDict()

    def clear(self) -> None:
        self._slices.clear()
        self._publisher = None

    @property
    def observed_subchunks(self) -> int:
        return sum(item.layers is not None for item in self._slices.values())

    def _inside_publisher(self, x: int, z: int) -> bool:
        if self._publisher is None:
            return True
        px, pz, radius = self._publisher
        # Radius is in blocks, unlike RequestChunkRadius's chunk units.
        dx = max(x * 16 - px, px - (x * 16 + 15), 0)
        dz = max(z * 16 - pz, pz - (z * 16 + 15), 0)
        return dx * dx + dz * dz <= radius * radius

    def _set(self, key: tuple[int, int, int], slice_: _Slice) -> None:
        if not self._inside_publisher(key[0], key[2]):
            return
        self._slices[key] = slice_
        self._slices.move_to_end(key)
        if len(self._slices) > self.max_subchunks:
            self._slices.popitem(last=False)

    def _update(self, x: int, y: int, z: int, layer: int, runtime_id: int) -> None:
        if layer not in (0, 1) or not self._inside_publisher(x // 16, z // 16):
            return
        key = (x // 16, y // 16, z // 16)
        slice_ = self._slices.get(key)
        if slice_ is None:
            slice_ = _Slice()
            self._set(key, slice_)
        slice_.patches[((x & 15) << 8 | (z & 15) << 4 | (y & 15), layer)] = int(runtime_id)

    def observe(self, bot, event) -> None:
        if bot is not self.bot:
            return
        if isinstance(event, PacketDecodedEvent) and event.packet.header.packet_id not in (
            11, 21, 61, 110, 121, 172, 174
        ):
            return
        status = bot.get_status()
        if status.session_id != self._session_id:
            self.clear()
            self._session_id = status.session_id
            self._dimension = status.dimension
            self._hashes = False
        if isinstance(event, StateEvent):
            if event.state >= BotState.closing:
                self.clear()
            return
        if isinstance(event, DimensionEvent):
            self.clear()
            self._dimension = event.dimension
            return
        if not isinstance(event, PacketDecodedEvent):
            return
        packet = event.packet
        packet_id = packet.header.packet_id
        value = packet.payload
        if packet_id == 11:
            self.clear()
            self._hashes = bool(value.block_network_ids_are_hashes)
            self._dimension = int(value.settings.spawn_settings.dimension)
        elif packet_id == 61:
            self.clear()  # Clear before Session projects DimensionEvent.
            self._dimension = None
        elif packet_id == 121 and self._hashes and self._dimension is not None:
            self._publisher = (value.position.x, value.position.z, int(value.radius))
            for key in tuple(self._slices):
                if not self._inside_publisher(key[0], key[2]):
                    del self._slices[key]
        elif packet_id == 174 and self._hashes and int(value.dimension_type) == self._dimension:
            if value.cache_enabled:
                return  # No Blob manager: cached data is never presumed present.
            for entry in value.sub_chunk_data:
                pos = entry.sub_chunk_pos_offset
                key = (
                    value.center_pos.x + pos.x,
                    value.center_pos.y + pos.y,
                    value.center_pos.z + pos.z,
                )
                result = int(entry.result)
                if result == 6:  # SUCCESS_ALL_AIR, not an unknown/missing subchunk.
                    self._set(key, _Slice(()))
                elif result == 1 and entry.serialized_sub_chunk is not None:
                    try:
                        layers = _decode_subchunk(bytes(entry.serialized_sub_chunk), key[1])
                    except ValueError:
                        self._slices.pop(key, None)
                    else:
                        self._set(key, _Slice(layers))
                else:
                    self._slices.pop(key, None)
        elif packet_id in (21, 110) and self._hashes and self._dimension is not None:
            self._update(value.pos.x, value.pos.y, value.pos.z, int(value.layer), int(value.runtime_id))
        elif packet_id == 172 and self._hashes and self._dimension is not None:
            for layer, entries in enumerate((value.blocks_changed.standards, value.blocks_changed.extras)):
                for item in entries:
                    self._update(item.pos.x, item.pos.y, item.pos.z, layer, int(item.runtime_id))

    def block(self, x: int, y: int, z: int) -> Block | None:
        if not self._hashes or self._dimension is None:
            return None
        key = (x // 16, y // 16, z // 16)
        slice_ = self._slices.get(key)
        if slice_ is None:
            return None
        index = ((x & 15) << 8) | ((z & 15) << 4) | (y & 15)
        try:
            runtime_id = slice_.block(index, 0)
        except ValueError:
            self._slices.pop(key, None)
            return None
        return None if runtime_id is None else _block(runtime_id)

    def can_stand(self, x: int, feet_y: int, z: int) -> bool | None:
        """Conservative three-block full-cube check, not player AABB physics."""
        foot = self.block(x, feet_y, z)
        head = self.block(x, feet_y + 1, z)
        ground = self.block(x, feet_y - 1, z)
        if any(b is not None and b.solid is True for b in (foot, head)):
            return False
        if ground is not None and ground.solid is False:
            return False
        if any(b is None or b.solid is None for b in (foot, head, ground)):
            return None
        if not (foot.air and head.air and ground.solid is True):
            return False
        # Extra Bedrock storage can contain fluid/unsupported blocks; the
        # ordinary-ground motion model must not assume that it is safe.
        for y in (feet_y - 1, feet_y, feet_y + 1):
            key = (x // 16, y // 16, z // 16)
            slice_ = self._slices.get(key)
            if slice_ is None:
                return None
            index = ((x & 15) << 8) | ((z & 15) << 4) | (y & 15)
            try:
                secondary = slice_.block(index, 1)
            except ValueError:
                self._slices.pop(key, None)
                return None
            if secondary != _AIR:
                return None
        return True
