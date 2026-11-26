"""Read little-endian NBT carried in Bedrock item user data."""

# The item user data decoder includes adapted code covered by this notice.
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
from typing import Any


_COMPOUND = 10
_FIXED = {1: "<b", 2: "<h", 3: "<i", 4: "<q", 5: "<f", 6: "<d"}
_ARRAYS = {7: "b", 11: "i", 12: "q"}


def read_item_user_data(data: bytes) -> dict[str, Any]:
    if not data:
        return {}
    try:
        marker, = struct.unpack_from("<h", data)
        if marker == 0:
            return {}
        if marker != -1 or data[2:4] != bytes((1, _COMPOUND)):
            raise ValueError("unsupported item user data header")
        _, offset = _read_string(data, 4)
        value, _ = _read_payload(data, _COMPOUND, offset)
    except (IndexError, RecursionError, struct.error) as error:
        raise ValueError("invalid item user data") from error
    return value


def _read_string(data: bytes, offset: int) -> tuple[str, int]:
    size, = struct.unpack_from("<H", data, offset)
    start = offset + 2
    end = start + size
    if end > len(data):
        raise ValueError("truncated item user data string")
    return data[start:end].decode("utf-8"), end


def _read_payload(data: bytes, tag: int, offset: int) -> tuple[Any, int]:
    if tag in _FIXED:
        fmt = _FIXED[tag]
        return struct.unpack_from(fmt, data, offset)[0], offset + struct.calcsize(fmt)
    if tag == 8:
        return _read_string(data, offset)
    if tag in _ARRAYS:
        count, = struct.unpack_from("<i", data, offset)
        start = offset + 4
        fmt = f"<{count}{_ARRAYS[tag]}"
        return list(struct.unpack_from(fmt, data, start)), start + struct.calcsize(fmt)
    if tag == 9:
        element, count = struct.unpack_from("<Bi", data, offset)
        offset += 5
        if not 0 <= count <= len(data) - offset:
            raise ValueError("invalid item user data list length")
        values = []
        for _ in range(count):
            value, offset = _read_payload(data, element, offset)
            values.append(value)
        return values, offset
    if tag == _COMPOUND:
        values = {}
        while data[offset] != 0:
            child = data[offset]
            key, offset = _read_string(data, offset + 1)
            values[key], offset = _read_payload(data, child, offset)
        return values, offset + 1
    raise ValueError(f"unknown item user data tag {tag}")
