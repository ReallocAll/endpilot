"""Decode runtime-ID block and biome palettes from Bedrock chunk data."""

# The following MIT notice applies to _PalettedStorage.get and SubChunk.get_block.
# MIT License
#
# Copyright (c) 2026, Vincent (Zhengyu) Wu
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

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
class _PalettedStorage:
    bits: int
    words: bytes
    palette: tuple[int, ...]

    def get(self, x: int, y: int, z: int) -> int:
        if self.bits == 0:
            return self.palette[0]
        index = (x << 8) | (z << 4) | y
        per_word = 32 // self.bits
        (word,) = struct.unpack_from("<I", self.words, (index // per_word) * 4)
        return self.palette[(word >> ((index % per_word) * self.bits)) & ((1 << self.bits) - 1)]


@dataclass(frozen=True, slots=True)
class SubChunk:
    storages: tuple[_PalettedStorage, ...]

    def get_block(self, x: int, y: int, z: int, layer: int = 0) -> int | None:
        if layer >= len(self.storages):
            return None
        return self.storages[layer].get(x, y, z)


def parse_sub_chunk(data: bytes) -> SubChunk:
    """Read versions 8/9; a trailing block-actor payload is not needed for blocks."""
    return parse_sub_chunk_prefix(data)[0]


def parse_sub_chunk_prefix(data: bytes) -> tuple[SubChunk, int]:
    """Read block palettes and return the offset where the actor payload starts."""
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
                storages.append(_PalettedStorage(0, b"", (runtime_id & 0xFFFFFFFF,)))
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
            storages.append(_PalettedStorage(bits, words, tuple(palette)))
        return SubChunk(tuple(storages)), offset
    except (IndexError, struct.error, ZeroDivisionError) as exc:
        raise ValueError("truncated or invalid sub-chunk data") from exc


def parse_biome_sections(data: bytes, count: int) -> tuple[_PalettedStorage, ...]:
    """Read a complete biome section sequence before the border block count."""
    sections = []
    offset = 0
    for _ in range(count):
        if offset >= len(data):
            raise ValueError("missing biome section")
        header = data[offset]
        offset += 1
        if header == 0xFF:
            if not sections:
                raise ValueError("first biome section cannot copy a predecessor")
            sections.append(sections[-1])
            continue

        bits = header >> 1
        if not header & 1 or bits not in (0, 1, 2, 3, 4, 5, 6, 8, 16):
            raise ValueError("unsupported biome storage header")
        words = b""
        size = 1
        if bits:
            per_word = 32 // bits
            end = offset + ((4096 + per_word - 1) // per_word) * 4
            words = data[offset:end]
            offset = end
            size, offset = _signed_varint(data, offset)
            if not 0 < size <= min(4096, 1 << bits):
                raise ValueError("invalid biome palette size")

        palette = []
        for _ in range(size):
            biome_id, offset = _signed_varint(data, offset)
            if biome_id < 0:
                raise ValueError("invalid biome ID")
            palette.append(biome_id)
        if bits and size < 1 << bits:
            mask = (1 << bits) - 1
            remaining = 4096
            for (word,) in struct.iter_unpack("<I", words):
                for _ in range(min(remaining, per_word)):
                    if (word & mask) >= size:
                        raise ValueError("biome palette index out of bounds")
                    word >>= bits
                remaining -= per_word
        sections.append(_PalettedStorage(bits, words, tuple(palette)))

    if data[offset:offset + 1] != b"\x00":
        raise ValueError("missing zero border block count after biome sections")
    return tuple(sections)
