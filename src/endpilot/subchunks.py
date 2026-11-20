"""Decode the runtime-ID sub-chunk palettes used by Bedrock 2193.

Layout follows EndstoneMC/client's world/level/chunk/sub_chunk.py.
"""

import struct
from dataclasses import dataclass


def _signed_varint(data: bytes, offset: int) -> tuple[int, int]:
    unsigned = 0
    for shift in range(0, 32, 7):
        if offset >= len(data):
            raise ValueError("truncated sub-chunk varint")
        byte = data[offset]
        offset += 1
        if shift == 28 and byte > 15:
            raise ValueError("sub-chunk varint exceeds 32 bits")
        unsigned |= (byte & 127) << shift
        if byte < 128:
            return (unsigned >> 1) ^ -(unsigned & 1), offset
    raise ValueError("unterminated sub-chunk varint")


@dataclass(frozen=True, slots=True)
class _BlockStorage:
    bits: int
    words: bytes
    palette: tuple[int, ...]

    def get(self, index: int) -> int:
        if self.bits == 0:
            return self.palette[0]
        per_word = 32 // self.bits
        (word,) = struct.unpack_from("<I", self.words, (index // per_word) * 4)
        return self.palette[(word >> ((index % per_word) * self.bits)) & ((1 << self.bits) - 1)]


@dataclass(frozen=True, slots=True)
class SubChunk:
    storages: tuple[_BlockStorage, ...]

    def get_block(self, x: int, y: int, z: int, layer: int = 0) -> int | None:
        if layer >= len(self.storages):
            return None
        return self.storages[layer].get((x << 8) | (z << 4) | y)


def parse_sub_chunk(data: bytes) -> SubChunk:
    """Read versions 8/9; a trailing block-actor payload is not needed for blocks."""
    try:
        version, count = data[0], data[1]
        if version not in (8, 9):
            raise ValueError(f"unsupported sub-chunk version {version}")
        offset = 3 if version == 9 else 2  # v9 carries its signed Y index
        storages = []
        for _ in range(count):
            header = data[offset]
            offset += 1
            if not header & 1:
                raise ValueError("sub-chunk palette is not in runtime-ID form")
            bits = header >> 1
            if bits == 0:
                runtime_id, offset = _signed_varint(data, offset)
                storages.append(_BlockStorage(0, b"", (runtime_id & 0xFFFFFFFF,)))
                continue
            per_word = 32 // bits
            nbytes = ((4096 + per_word - 1) // per_word) * 4
            words = data[offset:offset + nbytes]
            if len(words) != nbytes:
                raise ValueError("truncated sub-chunk palette words")
            offset += nbytes
            palette_size, offset = _signed_varint(data, offset)
            if not 0 < palette_size <= 4096:
                raise ValueError("invalid sub-chunk palette size")
            palette = []
            for _ in range(palette_size):
                runtime_id, offset = _signed_varint(data, offset)
                palette.append(runtime_id & 0xFFFFFFFF)
            storages.append(_BlockStorage(bits, words, tuple(palette)))
        return SubChunk(tuple(storages))
    except (IndexError, struct.error, ZeroDivisionError) as exc:
        raise ValueError("truncated or invalid sub-chunk data") from exc
