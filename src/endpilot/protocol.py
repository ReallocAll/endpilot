"""Bedrock game packet framing, after transport, compression and encryption."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from importlib import import_module


DEFAULT_PROTOCOL = 2225


def _write_uint(value: int) -> bytes:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("expected an unsigned 32-bit integer")
    data = bytearray()
    while value >= 128:
        data.append((value & 127) | 128)
        value >>= 7
    data.append(value)
    return bytes(data)


def _read_uint(data: memoryview, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 32, 7):
        if offset == len(data):
            raise ValueError("truncated packet length or header")
        byte = data[offset]
        offset += 1
        if shift == 28 and byte > 15:
            raise ValueError("packet length or header exceeds 32 bits")
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("unterminated varint")


@dataclass(frozen=True, slots=True)
class RawPacket:
    """A game packet with its complete header and undecoded body."""

    header: int
    body: bytes

    @property
    def packet_id(self) -> int:
        return self.header & 0x3FF


def read_batch(data: bytes) -> Iterator[RawPacket]:
    """Read length-prefixed packets in wire order from an uncompressed batch."""
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        length, offset = _read_uint(view, offset)
        end = offset + length
        if end > len(view):
            raise ValueError("truncated packet in batch")
        frame = view[offset:end]
        header, start = _read_uint(frame, 0)
        yield RawPacket(header, bytes(frame[start:]))
        offset = end


def write_batch(packets: Iterable[RawPacket]) -> bytes:
    """Frame game packets as an uncompressed batch."""
    data = bytearray()
    for packet in packets:
        header = _write_uint(packet.header)
        data.extend(_write_uint(len(header) + len(packet.body)))
        data.extend(header)
        data.extend(packet.body)
    return bytes(data)


class Protocol:
    """Use one upstream protocol binding for the lifetime of a connection."""

    def __init__(self, number: int = DEFAULT_PROTOCOL):
        self.number = number
        self.binding = import_module(f"bedrock_protocol.v{number}")

    def encode(self, packet) -> RawPacket:
        return RawPacket(type(packet).ID, self.binding.encode(packet))

    def decode(self, packet: RawPacket):
        packet_type = self.binding.packets[packet.packet_id]
        return self.binding.decode(packet_type, packet.body)
