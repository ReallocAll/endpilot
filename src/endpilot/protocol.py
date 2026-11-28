"""Bedrock game packet framing, after transport, compression and encryption."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from importlib import import_module

import bedrock_protocol
from bedrock_protocol import v712, v924, v2168


DEFAULT_PROTOCOL = 2225

_FEATURE_PROTOCOLS = {
    "preview_anvil": (2225,),
    "xbox_login": (2225,),
}


def _supports_feature(number: int, name: str) -> bool:
    return number in _FEATURE_PROTOCOLS[name]


# These packet constructors use helper types declared in earlier codec modules.
_HELPER_OWNERS = {
    "ActorDataFlagComponent": {2193: v2168, 2225: v2168},
    "CommandOutput": {2193: v712, 2225: v712},
    "CommandOutputMessage": {2193: v712, 2225: v712},
    "DataStoreChange": {2193: v712, 2225: v712},
    "DataStoreRemoval": {2193: v712, 2225: v712},
    "DataStoreUpdate": {2193: v924, 2225: v924},
    "ItemReleaseInventoryTransaction": {2193: v2168},
    "ItemStackRequestCereal": {2193: v2168},
    "ItemStackRequestPacketData": {2193: v2168},
    "ItemUseOnActorInventoryTransaction": {2193: v2168},
    "LevelSettings": {2193: v2168},
    "PassengerOfBlockArguments": {2193: v712, 2225: v712},
    "SerializedSkinRef": {1001: v712, 2193: v2168},
    "ServerConfigurationJoinInfo": {2193: v2168, 2225: v2168},
}


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


class _Binding:
    """Expose the selected native codec and its packet classes by name."""

    def __init__(self, number: int):
        self._version = import_module(f"bedrock_protocol.v{number}")
        self._packet_types = {packet.__name__: packet for packet in self._version.packets.values()}

    def __getattr__(self, name: str):
        if (packet := self._packet_types.get(name)) is not None:
            return packet
        try:
            return getattr(self._version, name)
        except AttributeError:
            pass
        owners = _HELPER_OWNERS.get(name)
        if owners is not None and (owner := owners.get(self._version.VERSION)) is not None:
            return getattr(owner, name)
        helper = getattr(bedrock_protocol, name, None)
        if isinstance(helper, type) and not hasattr(helper, "ID"):
            return helper
        raise AttributeError(name)


class Protocol:
    """Use one upstream protocol binding for the lifetime of a connection."""

    def __init__(self, number: int = DEFAULT_PROTOCOL):
        self.number = number
        self.binding = _Binding(number)

    def supports_feature(self, name: str) -> bool:
        """Report SDK support for a feature on this protocol.

        Supported names are `preview_anvil` and `xbox_login`. An unknown
        name raises KeyError. The query needs only the selected protocol number.
        """
        return _supports_feature(self.number, name)

    def encode(self, packet) -> RawPacket:
        return RawPacket(type(packet).ID, self.binding.encode(packet))

    def decode(self, packet: RawPacket):
        packet_type = self.binding.packets[packet.packet_id]
        return self.binding.decode(packet_type, packet.body)
