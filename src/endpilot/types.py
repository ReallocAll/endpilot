from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from ._projection import Record


class BotState(IntEnum):
    scheduled = 0
    connecting = 1
    negotiating = 2
    logging_in = 3
    spawning = 4
    ready = 5
    closing = 6
    closed = 7
    failed = 8


@dataclass(frozen=True, slots=True)
class Vec2(Record):
    x: float = 0
    y: float = 0


@dataclass(frozen=True, slots=True)
class Vec3(Record):
    x: float = 0
    y: float = 0
    z: float = 0


@dataclass(slots=True)
class BotOptions:
    server_url: str = ""
    name: str = "Bot"
    uuid: str = ""
    protocol: int = 2193
    chunk_radius: int = 10
    client_cache: bool = False
    client_side_generation: bool = False
    start_delay_ms: int = 0
    connect_timeout_ms: int = 10000
    spawn_timeout_ms: int = 60000
    item_request_timeout_ms: int = 5000
    server_key_sha256: str = ""
    load_screen: bool = True
    validate_all_packets: bool = False
    recover_packet_errors: bool = False
    xuid: str = ""
    transport: Literal["nethernet", "raknet"] = "nethernet"

    def _validate(self) -> None:
        if self.transport not in ("nethernet", "raknet"):
            raise ValueError("transport must be 'nethernet' or 'raknet'")
        for name in ("load_screen", "validate_all_packets", "recover_packet_errors"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        if not isinstance(self.xuid, str) or (
            self.xuid and (not re.fullmatch(r"[0-9]{1,20}", self.xuid) or int(self.xuid) > (1 << 64) - 1)
        ):
            raise ValueError("xuid must be empty or an ASCII decimal unsigned 64-bit integer of at most 20 digits")
        if not isinstance(self.server_key_sha256, str) or (
            self.server_key_sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", self.server_key_sha256)
        ):
            raise ValueError("server_key_sha256 must be empty or 64 hexadecimal characters")
        self.server_key_sha256 = self.server_key_sha256.lower()
        if self.transport == "raknet":
            try:
                parsed = urlsplit(self.server_url)
                port = parsed.port
            except ValueError:
                raise ValueError("server_url must be an explicit raknet://host:port URL") from None
            if (
                parsed.scheme != "raknet"
                or not parsed.hostname
                or port is None
                or parsed.username is not None
                or parsed.password is not None
                or "@" in parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
                or "?" in self.server_url
                or "#" in self.server_url
            ):
                raise ValueError("server_url must be an explicit raknet://host:port URL without credentials or extras")
            if not 1 <= port <= 65535:
                raise ValueError("RakNet server port must be in 1..65535")
        else:
            parsed = urlsplit(self.server_url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("server_url must be an explicit HTTP signaling URL without credentials")
        if not self.name:
            raise ValueError("name must not be empty")
        if self.protocol not in (1001, 2193):
            raise ValueError("supported protocols are 1001 and 2193")
        if not 1 <= self.chunk_radius <= 96:
            raise ValueError("chunk radius must be in 1..96")
        for name in ("start_delay_ms", "connect_timeout_ms", "spawn_timeout_ms", "item_request_timeout_ms"):
            value = getattr(self, name)
            if type(value) is not int or not (0 if name == "start_delay_ms" else 1) <= value <= 0xFFFFFFFF:
                raise ValueError("timeouts must fit unsigned 32-bit milliseconds and be positive")
        if self.uuid:
            if len(self.uuid) != 36 or str(UUID(self.uuid)) != self.uuid.lower():
                raise ValueError("invalid identity UUID")
        else:
            self.uuid = str(uuid4())


class _CapturedOptions(BotOptions):
    __slots__ = ()

    def __setattr__(self, name, value):
        raise AttributeError("captured options are immutable")

    def __delattr__(self, name):
        raise AttributeError("captured options are immutable")


@dataclass(slots=True)
class Input:
    position: Vec3 = Vec3()
    delta: Vec3 = Vec3()
    move: Vec2 = Vec2()
    pitch: float = 0
    yaw: float = 0
    vertical_collision: bool = True
    jumping: bool = False
    sneaking: bool = False
    sprinting: bool = False
    flags: tuple[str, ...] = ()


class BotToken:
    __slots__ = ()


@dataclass(frozen=True, slots=True)
class ServerDisconnectReason(Record):
    reason_code: int
    message: str | None = None
    filtered_message: str | None = None


@dataclass(frozen=True, slots=True)
class EntityRef(Record):
    bot: BotToken
    session_id: int
    runtime_id: int
    unique_id: int


@dataclass(frozen=True, slots=True)
class WindowRef(Record):
    bot: BotToken
    session_id: int
    generation: int
    window_id: int
    container_type: int
    trader_unique_id: int


@dataclass(frozen=True, slots=True)
class TradeOfferRef(Record):
    window: WindowRef
    revision: int
    recipe_net_id: int


@dataclass(frozen=True, slots=True)
class TradeItem(Record):
    identifier: str
    network_id: int
    count: int


@dataclass(frozen=True, slots=True)
class TradeOffer(Record):
    index: int
    ref: TradeOfferRef | None = None
    input: TradeItem | None = None
    second_input: TradeItem | None = None
    output: TradeItem | None = None
    uses: int | None = None
    max_uses: int | None = None
    tier: int | None = None
    supported: bool = False
    unsupported_reason: str = ""


class InventoryArea(IntEnum):
    player = 0
    trade_input = 1


class SlotState(IntEnum):
    unknown = 0
    empty = 1
    occupied = 2


@dataclass(frozen=True, slots=True)
class InventorySlot(Record):
    bot: BotToken
    session_id: int
    area: InventoryArea
    index: int
    window: WindowRef | None = None


@dataclass(frozen=True, slots=True)
class ItemRequestKey(Record):
    bot: BotToken
    session_id: int
    request_id: int
    window: WindowRef | None = None


@dataclass(frozen=True, slots=True)
class BotStatus(Record):
    state: BotState = BotState.scheduled
    session_id: int = 0
    actor_runtime_id: int = 0
    tick: int = 0
    position: Vec3 = Vec3()
    dimension: int = 0
    chunk_radius: int = 0
    alive: bool = True
    health: float | None = None
    selected_hotbar: int | None = 0


@dataclass(frozen=True, slots=True)
class ObservedEntity(Record):
    ref: EntityRef
    type: str
    position: Vec3


@dataclass(frozen=True, slots=True)
class ItemCount(Record):
    session_id: int
    observed_count: int
    complete: bool


@dataclass(frozen=True, slots=True)
class TradeWindowObservation(Record):
    ref: WindowRef
    closing: bool
    offer_revision: int
