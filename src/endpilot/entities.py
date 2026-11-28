"""Observe server actors, dropped items and listed players."""

import asyncio
import math
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Literal
from uuid import UUID

from bedrock_protocol import ActorSwingSource, DecodeError, GameType, HandSlot, PlayerListPacketType

from .entity_cursor import Box, expanded_box, trace_entities
from .expression import animation
from .inventory import HeldItemStack
from .player_state import PlayerEffect, _effect_from_packet
from .protocol import RawPacket, _read_uint
from .skin import PlayerSkinSnapshot, _player_skin_snapshot


@dataclass(frozen=True, slots=True)
class EntitySpawn:
    entity_id: int
    runtime_id: int
    actor_type: str
    position: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class EntityMovement:
    """Coordinates sent in one server movement packet; None means axis omitted."""

    runtime_id: int
    x: float | None
    y: float | None
    z: float | None


@dataclass(frozen=True, slots=True)
class PaintingSnapshot:
    """Fields from an AddPainting packet, including its original position and direction."""

    entity_id: int
    runtime_id: int
    position: tuple[float, float, float]
    dir: int
    motif: str


@dataclass(frozen=True, slots=True)
class EntityAttributeModifier:
    """One server-reported modifier, with numeric operation and operand codes."""

    id: str
    name: str
    amount: float
    operation: int
    operand: int
    serialize: bool


@dataclass(frozen=True, slots=True)
class EntityAttribute:
    """A server-reported attribute, with None for fields omitted by the packet."""

    name: str
    min_value: float
    current_value: float
    max_value: float
    default_min_value: float | None = None
    default_value: float | None = None
    default_max_value: float | None = None
    modifiers: tuple[EntityAttributeModifier, ...] | None = None


@dataclass(frozen=True, slots=True)
class EntityState:
    """Immutable actor state, with details from item and painting packets.

    ``last_observed_offhand`` records the last offhand stack received for this
    visible remote player instance. It is ``None`` until an offhand packet
    arrives. A stack with item ID and count zero represents an observed empty hand.
    The server can omit updates, so the observed stack can be stale.

    ``gliding``, ``sneaking``, and ``sleeping`` are the latest valid server
    reports for these flags on this visible remote player instance. ``None``
    means the flag is unknown or does not apply.

    ``owner_entity_id`` holds the latest unique entity ID reported for a wolf's
    owner or the ID reported at a fishing hook's spawn. The owner can be outside
    the observed actors or player list. ``None`` means absent or unknown.

    ``sitting`` holds the latest valid sitting flag for this visible wolf
    instance. ``None`` means the flag is unknown or does not apply.

    ``firework_attached_entity_id`` is the unique ID reported in the spawn
    metadata of a firework rocket, when the packet identifies an attachment.

    ``visible_effects_value`` retains the latest signed visual-effect metadata
    value, when the server supplies it for this remote actor instance.
    ``visible_effect_ids`` reads the IDs represented by supported metadata.

    ``attributes`` holds the latest server report for each observed attribute of
    this actor instance. An empty tuple means no attributes have been observed.

    ``status_effects`` holds effects observed through MobEffect packets for this
    actor instance, ordered by effect ID. Durations are the last reported values.

    ``trade_tier`` and ``trade_experience`` hold the latest valid metadata for
    this visible villager instance. Each is ``None`` until observed.

    ``pitch``, ``yaw``, and ``head_yaw`` hold the last observed angle in
    degrees on each axis. Each is ``None`` until observed for this instance.

    ``name_tag`` holds the last custom label from actor metadata. ``None``
    means unobserved. An empty string is a reported empty label. ``name``
    retains the identity name from AddPlayer.
    """

    entity_id: int
    runtime_id: int
    actor_type: str
    position: tuple[float, float, float]
    item_id: str | None = None
    item_count: int | None = None
    item_network_id: int | None = None
    name: str | None = None
    owner_entity_id: int | None = None
    is_from_fishing: bool = False
    boat_rotation: tuple[float, float] | None = None
    boat_velocity: tuple[float, float, float] = (0.0, 0.0, 0.0)
    painting: PaintingSnapshot | None = None
    last_observed_offhand: HeldItemStack | None = None
    gliding: bool | None = None
    attributes: tuple[EntityAttribute, ...] = ()
    status_effects: tuple[PlayerEffect, ...] = ()
    sneaking: bool | None = None
    sleeping: bool | None = None
    firework_attached_entity_id: int | None = None
    visible_effects_value: int | None = None
    sitting: bool | None = None
    trade_tier: int | None = None
    trade_experience: int | None = None
    pitch: float | None = None
    yaw: float | None = None
    head_yaw: float | None = None
    name_tag: str | None = None

    @property
    def visible_effect_ids(self) -> tuple[int, ...] | None:
        """Encoded effect IDs in descending order, or None for unknown metadata."""
        value = self.visible_effects_value
        # Protocol 2225 packs up to eight 7-bit entries, least significant first.
        if value is None or not 0 <= value < (1 << (7 * 8)):
            return None

        effect_ids: list[int] = []
        previous_id = 1 << 6  # One above the highest 6-bit ID.
        while value:
            entry = value & 0x7F
            effect_id = entry >> 1
            if entry & 1 or not 0 < effect_id < previous_id:
                return None
            effect_ids.append(effect_id)
            previous_id = effect_id
            value >>= 7
        return tuple(effect_ids)


@dataclass(frozen=True, slots=True)
class EntityMovementUpdate:
    """A movement packet's applied entity snapshot and reported rotation and ground state."""

    entity: EntityState
    pitch: float | None
    yaw: float | None
    head_yaw: float | None
    on_ground: bool


@dataclass(frozen=True, slots=True)
class ShieldBlockEvent:
    """A server-reported shield block and the originating metadata tick."""

    runtime_id: int
    entity_id: int | None
    tick: int


_EntityLifecycleReason = Literal[
    "spawn_packet", "remove_actor", "pickup", "replaced", "roster_removed", "identity_invalidated",
    "dimension_changed",
]


@dataclass(frozen=True, slots=True)
class EntityLifecycleEvent:
    """One visible actor instance entering or leaving, with its snapshot at the transition."""

    kind: Literal["entered", "left"]
    entity: EntityState
    reason: _EntityLifecycleReason


_MovementWaiters = dict[asyncio.Future[EntityState | BaseException], Callable[[EntityState], bool]]


@dataclass(frozen=True, slots=True)
class EntitySwing:
    """One received swing, with the entity and protocol fields observed at that packet."""

    runtime_id: int
    entity_id: int | None
    entity: EntityState | None
    source: ActorSwingSource | str | None
    hand: HandSlot | int | None


EntityCriticalEffectKind = Literal["critical", "magic_critical"]


@dataclass(frozen=True, slots=True)
class EntityCriticalEffect:
    """One received critical-hit visual with the target identity at that packet."""

    kind: EntityCriticalEffectKind
    runtime_id: int
    entity_id: int | None
    entity: EntityState | None
    data: float


EntityHealthEventKind = Literal["hurt", "death"]


@dataclass(frozen=True, slots=True)
class EntityHealthEvent:
    """A received actor hurt or death event with its packet-time snapshot."""

    kind: EntityHealthEventKind
    entity: EntityState


@dataclass(frozen=True, slots=True)
class EntityActorEvent:
    """One received actor event with the entity state observed at that packet."""

    entity: EntityState
    event_id: int
    data: int
    fire_at_position: tuple[float, float, float] | None


@dataclass(frozen=True, slots=True)
class EntityAttributeUpdate:
    """One attribute update and the entity snapshot after it was applied."""

    entity: EntityState
    names: tuple[str, ...]
    tick: int


@dataclass(frozen=True, slots=True)
class EntityEffectUpdate:
    """One MobEffect packet and the entity snapshot after it was applied."""

    entity: EntityState
    action: Literal["add", "update", "remove"]
    effect: PlayerEffect
    tick: int


@dataclass(frozen=True, slots=True)
class EntityVisibleEffectUpdate:
    """An applied visible-effect metadata update and its packet tick."""

    entity: EntityState
    tick: int


@dataclass(frozen=True, slots=True, eq=False)
class _EntityObserver[Event]:
    runtime_id: int | None
    callback: Callable[[Event], None]
    waiter: asyncio.Future[Event] | None = None
    kind: str | int | None = None


def _decode_unknown_swing_source(protocol, raw: RawPacket):
    """Read the unknown textual source rejected by the native enum decoder."""
    body = raw.body
    if getattr(protocol, "number", 0) < 898 or not body or body[0] != int(protocol.binding.AnimatePacket.Action.SWING):
        return None
    for offset in range(1, min(len(body), 11)):
        if body[offset] < 128:
            break
    else:
        return None
    # The runtime varint is followed by a float and a source flag. Any nonzero byte means present.
    presence = offset + 5
    if presence >= len(body) or body[presence] == 0:
        return None
    try:
        size, start = _read_uint(memoryview(body), presence + 1)
        end = start + size
        if end + hasattr(protocol.binding.AnimatePacket, "hand_slot") != len(body):
            return None
        source = body[start:end].decode("utf-8")
        if not source:
            return None
        packet = protocol.decode(RawPacket(raw.header, body[:presence] + b"\x00" + body[end:]))
    except (DecodeError, ValueError):
        return None
    return packet, source


@dataclass(frozen=True, slots=True)
class EntityCursorResult:
    """Closest observed target, absence of a candidate, or insufficient observations."""

    status: Literal["hit", "miss", "unknown"]
    dimension_id: int
    entity: EntityState | None = None
    distance: float | None = None


@dataclass(frozen=True, slots=True)
class PlayerEquipmentSnapshot:
    """Visible equipment sent for one remote player; None denotes an unobserved field."""

    runtime_id: int
    entity_id: int | None
    name: str
    selected_slot: int | None = None
    main_hand: HeldItemStack | None = None
    head: HeldItemStack | None = None
    chest: HeldItemStack | None = None
    legs: HeldItemStack | None = None
    feet: HeldItemStack | None = None


@dataclass(frozen=True, slots=True)
class ActorEquipmentSnapshot:
    """Equipment reported by the server for one observed non-player actor."""

    runtime_id: int
    entity_id: int
    actor_type: str
    main_hand: HeldItemStack | None = None
    head: HeldItemStack | None = None
    chest: HeldItemStack | None = None
    legs: HeldItemStack | None = None
    feet: HeldItemStack | None = None
    body: HeldItemStack | None = None


@dataclass(frozen=True, slots=True)
class ItemPickup:
    """A server-named pickup with identities and a drop snapshot when observed."""

    item_runtime_id: int
    collector_runtime_id: int
    item_entity_id: int | None
    collector_entity_id: int | None
    item: EntityState | None


@dataclass(frozen=True, slots=True)
class PlayerListEntry:
    """Player-list identity, skin, and last explicit remote game mode."""

    uuid: UUID
    name: str
    entity_id: int
    xuid: str | None
    play_fab_id: str | None
    platform_online_id: str | None
    build_platform: int
    is_teacher: bool
    is_host: bool
    is_sub_client: bool
    color: int
    skin: PlayerSkinSnapshot | None = None
    game_mode: GameType | None = None


@dataclass(frozen=True, slots=True)
class PlayerListChange:
    """A player-list entry sent by the server. Removes carry only the UUID."""

    action: Literal["add", "remove"]
    uuid: UUID
    player: PlayerListEntry | None = None


@dataclass(frozen=True, slots=True)
class VehicleLink:
    """A server-observed ride. Runtime IDs are absent until the actors are known."""

    vehicle_entity_id: int
    rider_entity_id: int
    role: Literal["RIDING", "PASSENGER"]
    vehicle_runtime_id: int | None
    rider_runtime_id: int | None


@dataclass(frozen=True, slots=True)
class VehicleLinkChange:
    """A link transition or loss of observation, in wire order."""

    rider_entity_id: int
    before: VehicleLink | None
    after: VehicleLink | None
    reason: Literal["link", "unobserved"]


_Ride = tuple[int, Literal["RIDING", "PASSENGER"]]
_PlayerFeet = tuple[tuple[float, float, float], int]  # Position and unresolved ride epoch.
_PlayerFlagWords = tuple[int | None, int | None]
_PlayerRotation = tuple[float, float]
_PLAYER = "minecraft:player"
_COW = "minecraft:cow"
_SHEEP = "minecraft:sheep"
_VILLAGER = "minecraft:villager_v2"
_FIREWORK = "minecraft:fireworks_rocket"
_WOLF = "minecraft:wolf"
_ACTOR_SPAWN_FEET_TYPES = frozenset((_COW, _SHEEP, _VILLAGER))
_CURSOR_BODY_TYPES = frozenset((_COW, _SHEEP))
_UNBOUNDED_BOX: Box = ((-math.inf,) * 3, (math.inf,) * 3)
_EXPLICIT_GAME_MODES = frozenset((GameType.SURVIVAL, GameType.CREATIVE, GameType.ADVENTURE, GameType.SPECTATOR))


@dataclass(eq=False, slots=True)
class _ActorInstance:
    feet_known: bool
    unlisted_game_mode: tuple[int, GameType] | None = None
    equipment: ActorEquipmentSnapshot | None = None


@dataclass(frozen=True, slots=True)
class _PlayerView:
    spawn_entity_id: int | None
    rotation: _PlayerRotation | None
    flags: _PlayerFlagWords
    ordinary_move: bool = True
    riding_block: bool = False


@dataclass(frozen=True, slots=True)
class _CursorBody:
    size: tuple[float | None, float | None] = (None, None)
    flags: _PlayerFlagWords = (None, None)


def _cursor_body(metadata, binding, previous: _CursorBody = _CursorBody()) -> _CursorBody:
    # Protocol 2225 metadata 53 and 54 are separate width and height writes.
    width, height = previous.size
    for item in metadata:
        if int(item.id) not in (53, 54):
            continue
        value = float(item.payload.value) if isinstance(item.payload, binding.DataItemFloatPayload) else None
        if value is not None and (not math.isfinite(value) or value <= 0):
            value = None
        if int(item.id) == 53:
            width = value
        else:
            height = value
    return replace(previous, size=(width, height), flags=_player_flag_words(metadata, binding, previous.flags))


def _upright_box(feet, width: float | None, height: float | None) -> Box:
    x, y, z = feet
    half = math.inf if width is None else width / 2
    top = math.inf if height is None else y + height
    return (x - half, y, z - half), (x + half, top, z + half)


def _player_flag_words(metadata, binding, previous: _PlayerFlagWords = (None, None)) -> _PlayerFlagWords:
    low, high = previous
    for item in metadata:
        if item.id in (0, 92):
            value = int(item.payload.value) if isinstance(item.payload, binding.DataItemInt64Payload) else None
            if item.id == 0:
                low = value
            else:
                high = value
    return low, high


def _player_flag(words: _PlayerFlagWords, flag: int) -> bool:
    index = int(flag)
    word = words[index // 64]
    return word is not None and bool(word & (1 << (index % 64)))


def _player_posture(words: _PlayerFlagWords, binding) -> tuple[bool | None, bool | None]:
    if words[0] is None:
        return None, None
    flags = binding.ActorFlags
    return _player_flag(words, flags.GLIDING), _player_flag(words, flags.SNEAKING)


def _player_sleeping(metadata, binding) -> bool | None:
    sleeping = None
    for item in metadata:
        if item.id == binding.ActorDataIDs.PLAYER_FLAGS and isinstance(item.payload, binding.DataItemBytePayload):
            sleeping = bool(item.payload.value & 0x02)
    return sleeping


def _actor_name_tag(metadata, binding) -> str | None:
    return next((entry.payload.value for entry in reversed(metadata)
                 if entry.id == binding.ActorDataIDs.NAME and isinstance(entry.payload, binding.DataItemStringPayload)), None)


def _visible_effects_value(metadata, binding) -> int | None:
    return next((int(entry.payload.value) for entry in reversed(metadata)
                 if entry.id == binding.ActorDataIDs.VISIBLE_MOB_EFFECTS
                 and isinstance(entry.payload, binding.DataItemInt64Payload)), None)


def _owner_entity_id(metadata, binding, previous: int | None = None) -> int | None:
    for entry in metadata:
        if entry.id == binding.ActorDataIDs.OWNER and isinstance(entry.payload, binding.DataItemInt64Payload):
            owner = int(entry.payload.value)
            return None if owner == -1 else owner
    return previous


def _wolf_sitting(metadata, binding, previous: bool | None = None) -> bool | None:
    flag = binding.ActorFlags.SITTING
    words = _player_flag_words(metadata, binding)
    if words[int(flag) // 64] is None:
        return previous
    return _player_flag(words, flag)


def _villager_trade_progress(
    metadata, binding, tier: int | None = None, experience: int | None = None,
) -> tuple[int | None, int | None]:
    ids = binding.ActorDataIDs
    for entry in metadata:
        if not isinstance(entry.payload, binding.DataItemIntPayload):
            continue
        if entry.id == ids.TRADE_TIER:
            tier = int(entry.payload.value)
        elif entry.id == ids.TRADE_EXPERIENCE:
            experience = int(entry.payload.value)
    return tier, experience


def _unsupported_view_pose(words: _PlayerFlagWords, binding) -> bool:
    flags = binding.ActorFlags
    return any(_player_flag(words, flag)
               for flag in (flags.RIDING, flags.GLIDING, flags.SWIMMING, flags.SLEEPING, flags.CRAWLING))


def _adult_cursor_pose(body: _CursorBody, actor_type: str, binding) -> bool:
    flags = binding.ActorFlags
    words = body.flags
    if None in words or _player_flag(words, flags.BABY) or _unsupported_view_pose(words, binding):
        return False
    return actor_type != _SHEEP or not any(_player_flag(words, flag)
                                           for flag in (flags.SNEAKING, flags.SITTING, flags.LAYING_DOWN))


def _actor_angle(value: int) -> float:
    # Packed actor angles are unsigned fractions of a turn.
    return (int(value) & 255) * 360 / 256


def _last_angle(reported: float | None, previous: float | None = None) -> float | None:
    return reported if reported is not None and math.isfinite(reported) else previous


def _float_angles(packet, head_field: str) -> tuple[float | None, float | None, float | None]:
    rotation = getattr(packet, "rot", None)
    return tuple(None if angle is None else float(angle) for angle in (
        getattr(rotation, "x", None), getattr(rotation, "y", None), getattr(packet, head_field, None),
    ))


def _player_rotation(packet) -> _PlayerRotation | None:
    rotation = getattr(packet, "rot", None)
    if rotation is None:
        return None
    pitch, yaw, head_yaw = float(rotation.x), float(rotation.y), float(packet.y_head_rot)
    # A turn can relay the new head heading with the previous pitch and yaw.
    if not all(map(math.isfinite, (pitch, yaw, head_yaw))) or math.remainder(yaw - head_yaw, 360):
        return None
    return pitch, yaw
_PAINTING = "minecraft:painting"


def _painting_snapshot(packet) -> PaintingSnapshot:
    return PaintingSnapshot(
        int(packet.entity_id), int(packet.runtime_id),
        (float(packet.pos.x), float(packet.pos.y), float(packet.pos.z)),
        int(packet.dir), str(packet.motif),
    )


def _equipment_stack(item) -> HeldItemStack | None:
    if item is None:
        return None
    net_id = item.net_id_variant
    return HeldItemStack(int(item.id), int(item.stack_size), int(item.aux_value),
                         None if net_id is None else int(net_id), int(item.block_runtime_id),
                         bytes(item.user_data_buffer))


def _player_list_change(entry) -> PlayerListChange:
    if entry.action == PlayerListPacketType.REMOVE:
        return PlayerListChange("remove", entry.uuid)
    return PlayerListChange("add", entry.uuid, PlayerListEntry(
        uuid=entry.uuid,
        name=entry.name,
        entity_id=int(entry.id),
        xuid=entry.xuid or None,
        play_fab_id=getattr(entry, "play_fab_id", None) or None,
        platform_online_id=entry.platform_online_id or None,
        build_platform=int(entry.build_platform),
        is_teacher=entry.is_teacher,
        is_host=entry.is_host,
        is_sub_client=entry.is_sub_client,
        color=int(entry.color),
        skin=_player_skin_snapshot(getattr(entry, "skin", None)),
    ))


class Entities:
    def __init__(self, packets, *, item_names: dict[int, str] | None = None,
                 own_runtime_id: int | None = None, own_entity_id: int | None = None,
                 own_uuid: UUID | None = None):
        self.packets = packets
        self._own_runtime_id = own_runtime_id
        self._own_entity_id = own_entity_id
        self._own_uuid = own_uuid
        self._players_by_uuid: dict[UUID, PlayerListEntry] = {}
        self._player_list_observers: list[Callable[[tuple[PlayerListChange, ...]], None]] = []
        self._player_skin_observers: list[Callable[[PlayerListEntry], None]] = []
        self._player_change_observers: list[Callable[[PlayerListEntry], None]] = []
        self._last_skin_update: tuple[RawPacket, UUID] | None = None
        self._player_runtime_by_uuid: dict[UUID, int] = {}
        self._player_uuid_by_runtime: dict[int, UUID] = {}
        self._player_feet_by_runtime: dict[int, _PlayerFeet] = {}
        self._player_views: dict[int, _PlayerView] = {}
        self._item_names = dict(item_names or {})
        self._by_runtime: dict[int, EntityState] = {}
        self._lifecycle_observers: dict[object, Callable[[EntityLifecycleEvent], None]] = {}
        self._actor_instances: dict[int, _ActorInstance] = {}
        self._movement_waiters: dict[int, _MovementWaiters] = {}
        self._movement_observers: list[_EntityObserver[EntityMovementUpdate]] = []
        self._cursor_bodies: dict[int, _CursorBody] = {}
        self._player_equipment: dict[int, PlayerEquipmentSnapshot] = {}
        self._equipment_waiters: dict[int, set[asyncio.Future[PlayerEquipmentSnapshot]]] = {}
        self._equipment_observers: list[_EntityObserver[PlayerEquipmentSnapshot]] = []
        self._actor_equipment_observers: list[_EntityObserver[ActorEquipmentSnapshot]] = []
        self._swing_observers: list[_EntityObserver[EntitySwing]] = []
        self._critical_effect_observers: list[_EntityObserver[EntityCriticalEffect]] = []
        self._health_observers: list[_EntityObserver[EntityHealthEvent]] = []
        self._actor_event_observers: list[_EntityObserver[EntityActorEvent]] = []
        self._attribute_observers: list[_EntityObserver[EntityAttributeUpdate]] = []
        self._effect_observers: list[_EntityObserver[EntityEffectUpdate]] = []
        self._visible_effect_observers: list[_EntityObserver[EntityVisibleEffectUpdate]] = []
        self._glide_observers: list[Callable[[EntityState], None]] = []
        self._crouch_observers: list[_EntityObserver[EntityState]] = []
        self._sleep_observers: list[_EntityObserver[EntityState]] = []
        self._name_tag_observers: list[_EntityObserver[EntityState]] = []
        self._attached_firework_observers: list[_EntityObserver[EntityState]] = []
        self._shield_block_observers: list[_EntityObserver[ShieldBlockEvent]] = []
        self._shield_block_asserted: set[int] = set()
        self._unidentified_equipment_since: dict[int, int] = {}
        # AddPlayer can identify an instance before its unique ID appears in EntityState.
        self._unlisted_player_runtime_by_id: dict[int, int] = {}
        self._equipment_removals: deque[tuple[int, int]] = deque(maxlen=128)
        self._runtime_by_id: dict[int, int] = {}
        self._last_pickup: tuple[RawPacket, ItemPickup, _ActorInstance | None] | None = None
        self._rides: dict[int, _Ride] = {}
        self._own_ride_epoch = 0
        self._unresolved_ride_epoch = 0
        self._unresolved_ride_changes: deque[tuple[int, int]] = deque(maxlen=128)
        self._ride_observers: list[Callable[[VehicleLinkChange], None]] = []
        self._dimension_epoch = 0
        self._closed = False
        self._unsubscribe = None
        self._unsubscribe_reader_end = None

    def start(self) -> None:
        self._unsubscribe = self.packets.subscribe_raw(self._on_packet)
        subscribe_reader_end = getattr(self.packets, "subscribe_reader_end", None)
        if subscribe_reader_end is not None:
            self._unsubscribe_reader_end = subscribe_reader_end(self._end_session)

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._end_session()
        self._players_by_uuid.clear()

    def _end_session(self, _cause: Exception | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        self._last_pickup = None
        self._last_skin_update = None
        self._player_list_observers.clear()
        self._player_skin_observers.clear()
        self._player_change_observers.clear()
        self._lifecycle_observers.clear()
        self._movement_observers.clear()
        self._end_movement_waiters(ConnectionError, "entity session is closed", _cause)
        self._clear_player_equipment(ConnectionError, "entity session is closed", _cause)
        for observers in (self._swing_observers, self._critical_effect_observers, self._health_observers,
                          self._actor_event_observers, self._attribute_observers, self._effect_observers,
                          self._crouch_observers, self._sleep_observers, self._name_tag_observers,
                          self._attached_firework_observers,
                          self._shield_block_observers, self._visible_effect_observers,
                          self._actor_equipment_observers):
            self._end_entity_observers(observers, lambda _: True, ConnectionError, "entity session is closed", _cause)
        self._shield_block_asserted.clear()
        self._glide_observers.clear()
        self._actor_instances.clear()
        self._cursor_bodies.clear()
        if self._unsubscribe_reader_end is not None:
            self._unsubscribe_reader_end()
            self._unsubscribe_reader_end = None
        self._player_feet_by_runtime.clear()
        self._player_views.clear()
        self._unresolved_ride_changes.clear()
        try:
            self._clear_links()
        finally:
            self._ride_observers.clear()

    def player_list(self) -> tuple[PlayerListEntry, ...]:
        return tuple(self._players_by_uuid.values())

    def _record_player_list_add(
        self, player: PlayerListEntry, runtime_id: int | None,
        observers: tuple[Callable[[PlayerListEntry], None], ...],
    ) -> None:
        previous = self._players_by_uuid.get(player.uuid)
        instance = self._actor_instances.get(runtime_id)
        pending = None
        if instance is not None:
            # A spawn before the first roster entry can only seed that entry once.
            pending, instance.unlisted_game_mode = instance.unlisted_game_mode, None
        mode = None
        if previous is not None and previous.entity_id == player.entity_id:
            mode = previous.game_mode
        elif previous is None and pending is not None and pending[0] == player.entity_id:
            mode = pending[1]
        updated = replace(player, game_mode=mode) if mode is not None else player
        self._players_by_uuid[player.uuid] = updated
        if observers and previous is not None and updated != previous:
            self.packets._notify(observers, updated)

    def _record_player_game_mode(self, player: PlayerListEntry, mode: GameType, *, startup: bool) -> None:
        if player.game_mode == mode:
            return
        updated = replace(player, game_mode=mode)
        self._players_by_uuid[player.uuid] = updated
        if not startup and self._player_change_observers:
            self.packets._notify(self._player_change_observers, updated)

    async def wait_for_player_list_update(
        self, *, action: Literal["add", "remove"] | None, player_uuid: UUID | None, timeout: float,
    ) -> tuple[PlayerListChange, ...]:
        binding = self.packets.protocol.binding
        changes = ()

        def matches(raw):
            nonlocal changes
            packet = self.packets.protocol.decode(raw)
            changes = tuple(
                _player_list_change(entry) for entry in packet.entries
                if (player_uuid is None or entry.uuid == player_uuid)
                and (action is None or entry.action.name.lower() == action)
            )
            return bool(changes)

        await self.packets.wait_for_packet(binding.PlayerListPacket.ID, match=matches, timeout=timeout)
        return changes

    def subscribe_player_list_updates(
        self, callback: Callable[[tuple[PlayerListChange, ...]], None], *,
        action: Literal["add", "remove"] | None, player_uuid: UUID | None,
    ) -> Callable[[], None]:
        self._require_live()

        def observe(changes: tuple[PlayerListChange, ...]) -> None:
            if action is not None or player_uuid is not None:
                changes = tuple(
                    change for change in changes
                    if (action is None or change.action == action)
                    and (player_uuid is None or change.uuid == player_uuid)
                )
            if changes:
                callback(changes)

        return self.packets._subscribe(self._player_list_observers, observe)

    async def wait_for_player_skin_update(
        self, player_uuid: UUID, *, timeout: float | None,
    ) -> PlayerSkinSnapshot | None:
        self._require_live()
        binding = self.packets.protocol.binding
        skin = None

        def matches(raw):
            nonlocal skin
            self._require_live()
            # The roster observer has applied this packet before the waiter runs.
            update = self._last_skin_update
            if update is None or update[0] is not raw or update[1] != player_uuid:
                return False
            skin = self._players_by_uuid[player_uuid].skin
            return True

        await self.packets.wait_for_packet(
            (binding.PlayerSkinPacket.ID, binding.DisconnectPacket.ID), match=matches, timeout=timeout,
        )
        return skin

    def subscribe_player_skin_updates(
        self, callback: Callable[[PlayerListEntry], None], *, player_uuid: UUID | None,
    ) -> Callable[[], None]:
        self._require_live()

        def observe(player: PlayerListEntry) -> None:
            if player_uuid is None or player.uuid == player_uuid:
                callback(player)

        return self.packets._subscribe(self._player_skin_observers, observe)

    def subscribe_player_changes(
        self, callback: Callable[[PlayerListEntry], None], *, player_uuid: UUID | None,
    ) -> Callable[[], None]:
        self._require_live()

        def observe(player: PlayerListEntry) -> None:
            if player_uuid is None or player.uuid == player_uuid:
                callback(player)

        return self.packets._subscribe(self._player_change_observers, observe)

    def snapshot(self) -> tuple[EntityState, ...]:
        """Snapshot actors observed since subscription; not a world entity scan."""
        return tuple(self._by_runtime.values())

    def get(self, runtime_id: int) -> EntityState | None:
        return self._by_runtime.get(runtime_id)

    def subscribe_entity_lifecycle(self, callback: Callable[[EntityLifecycleEvent], None]) -> Callable[[], None]:
        self._require_live()
        observers = self._lifecycle_observers
        token = object()
        observers[token] = callback

        def unsubscribe() -> None:
            observers.pop(token, None)

        return unsubscribe

    def subscribe_entity_glides(self, callback: Callable[[EntityState], None]) -> Callable[[], None]:
        self._require_live()

        def observe(entity: EntityState) -> None:
            try:
                callback(entity)
            except asyncio.CancelledError as error:
                asyncio.get_running_loop().call_exception_handler({
                    "message": "entity glide observer failed", "exception": error, "callback": callback,
                })

        return self.packets._subscribe(self._glide_observers, observe)

    def _require_remote_player_target(self, runtime_id: int | None) -> None:
        self._require_live()
        if runtime_id is not None and runtime_id not in self._player_views:
            raise LookupError("remote player is not currently observed")

    def subscribe_entity_crouch_changes(
        self, callback: Callable[[EntityState], None], *, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        self._require_remote_player_target(runtime_id)
        return self.packets._subscribe(self._crouch_observers, _EntityObserver(runtime_id, callback))

    async def wait_for_entity_crouch_change(
        self, *, runtime_id: int | None = None, timeout: float | None = 15,
    ) -> EntityState:
        self._require_remote_player_target(runtime_id)
        return await self._wait_for_entity_event(self._crouch_observers, runtime_id, timeout)

    def subscribe_entity_sleep_changes(
        self, callback: Callable[[EntityState], None], *, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        self._require_remote_player_target(runtime_id)
        return self.packets._subscribe(self._sleep_observers, _EntityObserver(runtime_id, callback))

    async def wait_for_entity_sleep_change(
        self, *, runtime_id: int | None = None, timeout: float | None = 15,
    ) -> EntityState:
        self._require_remote_player_target(runtime_id)
        return await self._wait_for_entity_event(self._sleep_observers, runtime_id, timeout)

    def subscribe_entity_name_tag_changes(
        self, callback: Callable[[EntityState], None], *, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        self._require_confirmed_remote_entity(runtime_id)
        return self.packets._subscribe(self._name_tag_observers, _EntityObserver(runtime_id, callback))

    async def wait_for_entity_name_tag_change(
        self, *, runtime_id: int | None = None, timeout: float | None = 15,
    ) -> EntityState:
        self._require_confirmed_remote_entity(runtime_id)
        return await self._wait_for_entity_event(self._name_tag_observers, runtime_id, timeout)

    def subscribe_attached_fireworks(self, callback: Callable[[EntityState], None]) -> Callable[[], None]:
        self._require_live()
        return self.packets._subscribe(self._attached_firework_observers, _EntityObserver(None, callback))

    async def wait_for_attached_firework(self, *, timeout: float | None = 15) -> EntityState:
        self._require_live()
        return await self._wait_for_entity_event(self._attached_firework_observers, None, timeout)

    def _require_known_actor_target(self, runtime_id: int | None) -> None:
        self._require_live()
        if runtime_id is not None and runtime_id != self._own_runtime_id and runtime_id not in self._actor_instances:
            raise LookupError("entity is not currently observed")

    def subscribe_shield_blocks(
        self, callback: Callable[[ShieldBlockEvent], None], *, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        self._require_remote_player_target(None if runtime_id == self._own_runtime_id else runtime_id)
        return self.packets._subscribe(self._shield_block_observers, _EntityObserver(runtime_id, callback))

    async def wait_for_shield_block(
        self, *, runtime_id: int | None = None, timeout: float | None = 15,
    ) -> ShieldBlockEvent:
        self._require_remote_player_target(None if runtime_id == self._own_runtime_id else runtime_id)
        return await self._wait_for_entity_event(self._shield_block_observers, runtime_id, timeout)

    def subscribe_entity_swings(
        self, callback: Callable[[EntitySwing], None], *, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        self._require_known_actor_target(runtime_id)
        observer = _EntityObserver(runtime_id, callback)
        return self.packets._subscribe(self._swing_observers, observer)

    async def wait_for_entity_swing(
        self, *, runtime_id: int | None = None, timeout: float | None = 15,
    ) -> EntitySwing:
        self._require_known_actor_target(runtime_id)
        return await self._wait_for_entity_event(self._swing_observers, runtime_id, timeout)

    def _require_critical_effect_target(self, kind: EntityCriticalEffectKind | None, runtime_id: int | None) -> None:
        if kind not in (None, "critical", "magic_critical"):
            raise ValueError("kind must be 'critical' or 'magic_critical'")
        self._require_known_actor_target(runtime_id)

    def subscribe_entity_critical_effects(
        self, callback: Callable[[EntityCriticalEffect], None], *,
        kind: EntityCriticalEffectKind | None = None, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        self._require_critical_effect_target(kind, runtime_id)
        return self.packets._subscribe(self._critical_effect_observers, _EntityObserver(runtime_id, callback, kind=kind))

    async def wait_for_entity_critical_effect(
        self, *, kind: EntityCriticalEffectKind | None = None, runtime_id: int | None = None,
        timeout: float | None = 15,
    ) -> EntityCriticalEffect:
        self._require_critical_effect_target(kind, runtime_id)
        return await self._wait_for_entity_event(self._critical_effect_observers, runtime_id, timeout, kind=kind)

    def _require_health_event_target(self, kind: EntityHealthEventKind | None, runtime_id: int | None) -> None:
        self._require_live()
        if kind not in (None, "hurt", "death"):
            raise ValueError("kind must be 'hurt' or 'death'")
        self._require_remote_event_target(runtime_id)

    def _require_remote_event_target(self, runtime_id: int | None) -> None:
        if runtime_id is not None and (runtime_id == self._own_runtime_id or runtime_id not in self._actor_instances):
            raise LookupError("remote entity is not currently observed")

    def subscribe_entity_health_events(
        self, callback: Callable[[EntityHealthEvent], None], *,
        kind: EntityHealthEventKind | None = None, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        self._require_health_event_target(kind, runtime_id)
        return self.packets._subscribe(self._health_observers, _EntityObserver(runtime_id, callback, kind=kind))

    async def wait_for_entity_health_event(
        self, *, kind: EntityHealthEventKind | None = None, runtime_id: int | None = None, timeout: float | None = 15,
    ) -> EntityHealthEvent:
        self._require_health_event_target(kind, runtime_id)
        return await self._wait_for_entity_event(self._health_observers, runtime_id, timeout, kind=kind)

    def _require_actor_event_target(self, runtime_id: int | None, event_id: int | None) -> None:
        self._require_live()
        if event_id is not None and (
            isinstance(event_id, bool) or not isinstance(event_id, int) or not 0 <= event_id <= 255
        ):
            raise ValueError("event_id must be an integer between 0 and 255")
        self._require_remote_event_target(runtime_id)

    def subscribe_entity_actor_events(
        self, callback: Callable[[EntityActorEvent], None], *, runtime_id: int | None = None,
        event_id: int | None = None,
    ) -> Callable[[], None]:
        self._require_actor_event_target(runtime_id, event_id)
        return self.packets._subscribe(self._actor_event_observers, _EntityObserver(runtime_id, callback, kind=event_id))

    async def wait_for_entity_actor_event(
        self, *, runtime_id: int | None = None, event_id: int | None = None, timeout: float | None = 15,
    ) -> EntityActorEvent:
        self._require_actor_event_target(runtime_id, event_id)
        return await self._wait_for_entity_event(self._actor_event_observers, runtime_id, timeout, kind=event_id)

    def subscribe_entity_attribute_updates(
        self, callback: Callable[[EntityAttributeUpdate], None], *, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        self._require_live()
        self._require_remote_event_target(runtime_id)
        return self.packets._subscribe(self._attribute_observers, _EntityObserver(runtime_id, callback))

    async def wait_for_entity_attribute_update(
        self, *, runtime_id: int | None = None, timeout: float | None = 15,
    ) -> EntityAttributeUpdate:
        self._require_live()
        self._require_remote_event_target(runtime_id)
        return await self._wait_for_entity_event(self._attribute_observers, runtime_id, timeout)

    def subscribe_entity_effect_updates(
        self, callback: Callable[[EntityEffectUpdate], None], *, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        self._require_live()
        self._require_remote_event_target(runtime_id)
        return self.packets._subscribe(self._effect_observers, _EntityObserver(runtime_id, callback))

    async def wait_for_entity_effect_update(
        self, *, runtime_id: int | None = None, timeout: float | None = 15,
    ) -> EntityEffectUpdate:
        self._require_live()
        self._require_remote_event_target(runtime_id)
        return await self._wait_for_entity_event(self._effect_observers, runtime_id, timeout)

    def _is_confirmed_remote_entity(self, runtime_id: int) -> bool:
        return (runtime_id != self._own_runtime_id and runtime_id in self._actor_instances
                and (runtime_id not in self._player_uuid_by_runtime or runtime_id in self._player_views))

    def _require_confirmed_remote_entity(self, runtime_id: int | None) -> None:
        self._require_live()
        if runtime_id is not None and not self._is_confirmed_remote_entity(runtime_id):
            raise LookupError("remote entity is not currently observed")

    def subscribe_entity_visible_effect_changes(
        self, callback: Callable[[EntityVisibleEffectUpdate], None], *, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        self._require_confirmed_remote_entity(runtime_id)
        return self.packets._subscribe(self._visible_effect_observers, _EntityObserver(runtime_id, callback))

    async def wait_for_entity_visible_effect_change(
        self, *, runtime_id: int | None = None, timeout: float | None = 15,
    ) -> EntityVisibleEffectUpdate:
        self._require_confirmed_remote_entity(runtime_id)
        return await self._wait_for_entity_event(self._visible_effect_observers, runtime_id, timeout)

    async def _wait_for_entity_event[Event](
        self, observers: list[_EntityObserver[Event]], runtime_id: int | None, timeout: float | None,
        *, kind: str | int | None = None,
    ) -> Event:
        future = asyncio.get_running_loop().create_future()

        def observe(event: Event) -> None:
            if not future.done():
                future.set_result(event)

        unsubscribe = self.packets._subscribe(observers, _EntityObserver(runtime_id, observe, future, kind))
        try:
            async with asyncio.timeout(timeout):
                return await future
        finally:
            unsubscribe()

    @staticmethod
    def _end_entity_observers(observers, matches, error_type, message, cause=None):
        for observer in tuple(observers):
            if not matches(observer):
                continue
            observers.remove(observer)
            waiter = observer.waiter
            if waiter is not None and not waiter.done():
                error = error_type(message)
                error.__cause__ = cause
                waiter.set_exception(error)
                waiter.exception()

    def _forget_entity_observers(self, observers, runtime_id: int) -> None:
        self._end_entity_observers(observers, lambda observer: observer.runtime_id == runtime_id,
                                   LookupError, "entity is no longer observed")

    def _shield_block_edge(self, runtime_id: int, metadata, binding) -> bool:
        if runtime_id != self._own_runtime_id and runtime_id not in self._player_views:
            return False
        word = next((int(entry.payload.value) for entry in reversed(metadata)
                     if entry.id == 92 and isinstance(entry.payload, binding.DataItemInt64Payload)), None)
        if word is None:
            return False
        if not _player_flag((None, word), binding.ActorFlags.BLOCKED_USING_SHIELD):
            self._shield_block_asserted.discard(runtime_id)
            return False
        if runtime_id in self._shield_block_asserted:
            return False
        self._shield_block_asserted.add(runtime_id)
        return True

    def _observe_animation(self, raw: RawPacket) -> None:
        if not self._swing_observers and not self._critical_effect_observers:
            return
        protocol = self.packets.protocol
        try:
            packet = protocol.decode(raw)
            source = getattr(packet, "swing_source", None)
        except DecodeError as error:
            if not self._swing_observers or error.path != ("swing_source",) or error.reason != "unknown value":
                return
            decoded = _decode_unknown_swing_source(protocol, raw)
            if decoded is None:
                return
            packet, source = decoded
        actions = protocol.binding.AnimatePacket.Action
        action = packet.action
        if action == actions.SWING and self._swing_observers:
            runtime_id = int(packet.runtime_id)
            event = EntitySwing(
                runtime_id, self._animation_entity_id(runtime_id), self._by_runtime.get(runtime_id),
                source, getattr(packet, "hand_slot", None),
            )
            self._notify_entity_observers(self._swing_observers, event, runtime_id, "entity swing observer failed")
            return

        if not self._critical_effect_observers:
            return
        kind: EntityCriticalEffectKind
        if action == actions.CRITICAL_HIT:
            kind = "critical"
        elif action == actions.MAGIC_CRITICAL_HIT:
            kind = "magic_critical"
        else:
            return

        runtime_id = int(packet.runtime_id)
        entity = None if runtime_id == self._own_runtime_id else self._by_runtime.get(runtime_id)
        effect = EntityCriticalEffect(
            kind, runtime_id, self._animation_entity_id(runtime_id), entity, float(packet.data),
        )
        self._notify_entity_observers(self._critical_effect_observers, effect, runtime_id,
                                      "entity critical effect observer failed", kind=kind)

    def _observe_actor_event(self, raw: RawPacket) -> None:
        if not self._health_observers and not self._actor_event_observers:
            return
        protocol = self.packets.protocol
        packet = protocol.decode(raw)
        runtime_id = int(packet.runtime_id)
        state = self._by_runtime.get(runtime_id)
        if runtime_id == self._own_runtime_id or state is None:
            return

        event_id = packet.event_id
        actor_event_observers = tuple(self._actor_event_observers)
        if self._health_observers:
            types = protocol.binding.ActorEvent
            kind: EntityHealthEventKind | None = None
            if event_id == types.HURT:
                kind = "hurt"
            elif event_id == types.DEATH:
                kind = "death"
            if kind is not None:
                self._notify_entity_observers(self._health_observers, EntityHealthEvent(kind, state), runtime_id,
                                              "entity health observer failed", kind=kind)
        if actor_event_observers:
            pos = getattr(packet, "fire_at_position", None)
            fire_at_position = None if pos is None else (float(pos.x), float(pos.y), float(pos.z))
            event = EntityActorEvent(state, event_id, int(packet.data), fire_at_position)
            self._notify_entity_observers(actor_event_observers, event, runtime_id,
                                          "entity actor event observer failed", kind=event_id)

    def _observe_attribute_update(self, raw: RawPacket) -> None:
        packet = self.packets.protocol.decode(raw)
        runtime_id = int(packet.runtime_id)
        state = self._by_runtime.get(runtime_id)
        if runtime_id == self._own_runtime_id or state is None:
            return

        updated: dict[str, EntityAttribute] = {}
        for attribute in packet.attribute_data:
            name = str(attribute.name)
            updated[name] = EntityAttribute(
                name, float(attribute.min_value), float(attribute.current_value), float(attribute.max_value),
                default_min_value=getattr(attribute, "default_min_value", None),
                default_value=float(attribute.default_value),
                default_max_value=getattr(attribute, "default_max_value", None),
                modifiers=tuple(EntityAttributeModifier(
                    str(modifier.id), str(modifier.name), float(modifier.amount),
                    int(modifier.operation), int(modifier.operand), bool(modifier.serialize),
                ) for modifier in attribute.modifiers),
            )
        if updated:
            attributes = {attribute.name: attribute for attribute in state.attributes}
            attributes.update(updated)
            state = replace(state, attributes=tuple(attributes.values()))
            self._by_runtime[runtime_id] = state
        if self._attribute_observers:
            event = EntityAttributeUpdate(state, tuple(updated), int(packet.tick))
            self._notify_entity_observers(self._attribute_observers, event, runtime_id,
                                          "entity attribute observer failed")

    def _observe_effect_update(self, raw: RawPacket) -> None:
        protocol = self.packets.protocol
        packet = protocol.decode(raw)
        runtime_id = int(packet.runtime_id)
        state = self._by_runtime.get(runtime_id)
        if runtime_id == self._own_runtime_id or state is None:
            return

        events = protocol.binding.MobEffectPacket.Event
        action: Literal["add", "update", "remove"]
        match packet.event_id:
            case events.ADD:
                action = "add"
            case events.UPDATE:
                action = "update"
            case events.REMOVE:
                action = "remove"
            case _:
                return

        effect = _effect_from_packet(packet)
        effects = {entry.id: entry for entry in state.status_effects}
        if action == "remove":
            effects.pop(effect.id, None)
        else:
            effects[effect.id] = effect
        state = replace(state, status_effects=tuple(effects[effect_id] for effect_id in sorted(effects)))
        self._by_runtime[runtime_id] = state
        if self._effect_observers:
            event = EntityEffectUpdate(state, action, effect, int(packet.tick))
            self._notify_entity_observers(self._effect_observers, event, runtime_id, "entity effect observer failed")

    @staticmethod
    def _notify_entity_observers[Event](
        observers: Sequence[_EntityObserver[Event]], event: Event, runtime_id: int, message: str,
        *, kind: str | int | None = None,
    ) -> None:
        for observer in tuple(observers):
            if observer.runtime_id is not None and observer.runtime_id != runtime_id:
                continue
            if observer.kind is not None and observer.kind != kind:
                continue
            try:
                observer.callback(event)
            except (Exception, asyncio.CancelledError) as error:
                asyncio.get_running_loop().call_exception_handler({
                    "message": message, "exception": error, "callback": observer.callback,
                })

    def player_equipment(self, runtime_id: int) -> PlayerEquipmentSnapshot | None:
        """Return the equipment observed for the currently visible remote player."""
        self._sync_reader_end()
        return self._player_equipment.get(runtime_id)

    def _require_equipment_target(self, runtime_id: int) -> None:
        self._require_live()
        if runtime_id not in self._player_equipment:
            raise LookupError("remote player is not currently observed")

    def subscribe_player_equipment_changes(
        self, callback: Callable[[PlayerEquipmentSnapshot], None], *, runtime_id: int,
    ) -> Callable[[], None]:
        self._require_equipment_target(runtime_id)
        return self.packets._subscribe(self._equipment_observers, _EntityObserver(runtime_id, callback))

    async def wait_for_player_equipment_change(
        self, runtime_id: int, *, timeout: float | None = 15,
    ) -> PlayerEquipmentSnapshot:
        """Wait for the next equipment change on this visible player instance."""
        self._require_equipment_target(runtime_id)
        future = asyncio.get_running_loop().create_future()
        waiters = self._equipment_waiters.setdefault(runtime_id, set())
        waiters.add(future)
        try:
            async with asyncio.timeout(timeout):
                return await future
        finally:
            waiters.discard(future)
            if not waiters and self._equipment_waiters.get(runtime_id) is waiters:
                self._equipment_waiters.pop(runtime_id)

    def _publish_player_equipment(
        self, previous: PlayerEquipmentSnapshot, current: PlayerEquipmentSnapshot,
    ) -> None:
        if current != previous:
            self._player_equipment[current.runtime_id] = current
            for waiter in self._equipment_waiters.pop(current.runtime_id, ()):
                if not waiter.done():
                    waiter.set_result(current)
            self._notify_entity_observers(self._equipment_observers, current, current.runtime_id,
                                          "player equipment observer failed")

    def _fail_equipment_waiters(self, runtime_id, error_type, message, cause=None):
        for waiter in self._equipment_waiters.pop(runtime_id, ()):
            if not waiter.done():
                error = error_type(message)
                error.__cause__ = cause
                waiter.set_exception(error)
                waiter.exception()

    def _forget_player_equipment(self, runtime_id):
        equipment = self._player_equipment.pop(runtime_id, None)
        if equipment is not None and self._unlisted_player_runtime_by_id.get(equipment.entity_id) == runtime_id:
            self._unlisted_player_runtime_by_id.pop(equipment.entity_id)
        self._unidentified_equipment_since.pop(runtime_id, None)
        self._fail_equipment_waiters(runtime_id, LookupError, "remote player is no longer observed")
        self._forget_entity_observers(self._equipment_observers, runtime_id)

    def _clear_player_equipment(self, error_type, message, cause=None):
        self._player_equipment.clear()
        self._equipment_observers.clear()
        self._unidentified_equipment_since.clear()
        self._unlisted_player_runtime_by_id.clear()
        self._equipment_removals.clear()
        for runtime_id in tuple(self._equipment_waiters):
            self._fail_equipment_waiters(runtime_id, error_type, message, cause)

    def _nonplayer_actor(self, runtime_id: int) -> _ActorInstance | None:
        state = self._by_runtime.get(runtime_id)
        if runtime_id == self._own_runtime_id or state is None or state.actor_type == _PLAYER:
            return None
        return self._actor_instances.get(runtime_id)

    def actor_equipment(self, runtime_id: int) -> ActorEquipmentSnapshot | None:
        self._sync_reader_end()
        instance = self._nonplayer_actor(runtime_id)
        return None if instance is None else instance.equipment

    def _require_actor_equipment_target(self, runtime_id: int) -> None:
        self._require_live()
        if self._nonplayer_actor(runtime_id) is None:
            raise LookupError("non-player actor is not currently observed")

    def subscribe_actor_equipment_changes(
        self, callback: Callable[[ActorEquipmentSnapshot], None], *, runtime_id: int,
    ) -> Callable[[], None]:
        self._require_actor_equipment_target(runtime_id)
        return self.packets._subscribe(self._actor_equipment_observers, _EntityObserver(runtime_id, callback))

    async def wait_for_actor_equipment_change(
        self, runtime_id: int, *, timeout: float | None = 15,
    ) -> ActorEquipmentSnapshot:
        self._require_actor_equipment_target(runtime_id)
        return await self._wait_for_entity_event(self._actor_equipment_observers, runtime_id, timeout)

    def _publish_actor_equipment(
        self, runtime_id: int, instance: _ActorInstance, *, startup: bool, **slots: HeldItemStack | None,
    ) -> None:
        previous = instance.equipment
        if previous is None:
            state = self._by_runtime[runtime_id]
            current = ActorEquipmentSnapshot(runtime_id, state.entity_id, state.actor_type, **slots)
        else:
            current = replace(previous, **slots)
        if current != previous:
            instance.equipment = current
            if not startup:
                self._notify_entity_observers(self._actor_equipment_observers, current, runtime_id,
                                              "actor equipment observer failed")

    def _equipment_removal_cursor(self):
        return self._equipment_removals[-1][0] if self._equipment_removals else 0

    def _equipment_removal_may_match(self, since, entity_id):
        removals = self._equipment_removals
        # A gap means this appearance may already have been removed before its identity was known.
        return bool(removals) and (since + 1 < removals[0][0] or any(
            cursor > since and removed == entity_id for cursor, removed in removals
        ))

    def _actor_instance(self, runtime_id: int) -> object | None:
        """Identify the observed actor instance across ordinary state updates."""
        self._sync_reader_end()
        return self._actor_instances.get(runtime_id)

    def _bound_item(self, target: EntityState, instance: object) -> EntityState | None:
        current = self.get(target.runtime_id)
        if (current is not None and current.actor_type == "minecraft:item"
                and current.entity_id == target.entity_id and self._actor_instance(target.runtime_id) is instance):
            return current
        return None

    def _observed_pickup(self, raw: RawPacket) -> tuple[ItemPickup, object | None] | None:
        observed = self._last_pickup
        return observed[1:] if observed is not None and observed[0] is raw else None

    def _apply_boat_correction(self, state: EntityState, packet) -> None:
        self._by_runtime[state.runtime_id] = replace(
            state,
            position=(float(packet.pos.x), float(packet.pos.y), float(packet.pos.z)),
            boat_rotation=(float(packet.vehicle_rotation.x), float(packet.vehicle_rotation.y)),
            boat_velocity=(float(packet.pos_delta.x), float(packet.pos_delta.y), float(packet.pos_delta.z)),
        )

    def _get_by_unique_id(self, entity_id: int) -> EntityState | None:
        return self._by_runtime.get(self._runtime_by_id.get(entity_id))

    def _non_player_feet(self, state: EntityState) -> tuple[float, float, float] | None:
        instance = self._actor_instances.get(state.runtime_id)
        if instance is not None and instance.feet_known and all(map(math.isfinite, state.position)):
            return state.position
        return None

    def _item_navigation_anchor(self, state: EntityState) -> tuple[float, float, float] | None:
        if state.entity_id not in self._rides:
            return self._non_player_feet(state)
        return None

    def _navigation_target(self, entity_id: int) -> tuple[object, tuple[float, float, float] | None] | None:
        """Resolve a visible instance and its feet when the position reference is known."""
        self._sync_reader_end()
        state = self._get_by_unique_id(entity_id)
        if state is None:
            return None
        instance = self._actor_instances.get(state.runtime_id)
        if instance is None:
            return None
        feet = self._player_feet_by_runtime.get(state.runtime_id)
        if feet is not None:
            return instance, feet[0]
        if state.actor_type == _VILLAGER:
            return instance, self._non_player_feet(state)
        return instance, None

    def entity_at_cursor(self, pose, blocks, max_distance: float) -> EntityCursorResult:
        self._sync_reader_end()
        if self._closed or getattr(self.packets.protocol, "number", None) != 2225:
            pose = None
        candidates, unresolved = [], []
        if pose is not None:
            binding = self.packets.protocol.binding
            for state in self._by_runtime.values():
                if state.runtime_id == self._own_runtime_id or state.entity_id == self._own_entity_id:
                    continue
                body = self._cursor_bodies.get(state.runtime_id)
                width, height = (None, None) if body is None else body.size
                if (state.actor_type == "minecraft:item" and width is not None and height is not None
                        and state.entity_id not in self._rides):
                    # Item coordinates anchor the box bottom; bound uncertainty without selecting the item.
                    feet, supported = state.position, False
                else:
                    feet, supported = self._cursor_feet(state, binding)
                if feet is None or not all(map(math.isfinite, feet)):
                    unresolved.append(_UNBOUNDED_BOX)
                    continue
                box = _upright_box(feet, width, height)
                if supported and width is not None and height is not None:
                    candidates.append((state, box))
                else:
                    # Keep independently observed planes. Unobserved planes
                    # stay open, since an anchor is not a surface bound.
                    unresolved.append(expanded_box(box))
        status, entity, distance = trace_entities(
            pose, candidates, unresolved, max_distance=max_distance,
            trace_blocks=lambda reach: blocks._raycast_view(pose, reach),
        )
        return EntityCursorResult(status, blocks.dimension_id, entity, distance)

    def _cursor_feet(self, state: EntityState, binding):
        runtime_id, entity_id = state.runtime_id, state.entity_id
        if entity_id in self._rides:
            return None, False
        stable_identity = entity_id and self._runtime_by_id.get(entity_id) == runtime_id
        if state.actor_type == _PLAYER:
            observed = self._player_feet_by_runtime.get(runtime_id)
            if observed is None:
                return None, False
            view = self._player_views.get(runtime_id)
            upright = (view is not None and view.ordinary_move and None not in view.flags
                       and not _unsupported_view_pose(view.flags, binding))
            return (observed[0], bool(stable_identity)) if upright else (None, False)
        if state.actor_type in _CURSOR_BODY_TYPES:
            body = self._cursor_bodies.get(runtime_id)
            feet = self._non_player_feet(state)
            if body is not None and feet is not None and _adult_cursor_pose(body, state.actor_type, binding):
                return feet, bool(stable_identity)
        return None, False

    def player_view_pose(self, runtime_id: int) -> tuple[tuple[float, float, float], _PlayerRotation] | None:
        """Return a visible remote player's observed eye position and (pitch, yaw)."""
        self._sync_reader_end()
        view = self._player_views.get(runtime_id)
        observed = self._player_feet_by_runtime.get(runtime_id)
        if view is None or view.rotation is None or observed is None or runtime_id in self._unidentified_equipment_since:
            return None
        feet, _ = observed
        low, high = view.flags
        if low is None or high is None or not all(map(math.isfinite, feet)):
            return None
        sneaking = self.packets.protocol.binding.ActorFlags.SNEAKING
        height = 1.17 if _player_flag(view.flags, sneaking) else 1.52
        return (feet[0], feet[1] + height, feet[2]), view.rotation

    def nearest_player(
        self, origin: tuple[float, float, float] | None, match: Callable[[EntityState], bool] | None = None,
    ) -> EntityState | None:
        """Find the nearest observed remote player with a known identity and feet position."""
        return self._nearest_entity(origin, match, players_only=True)

    def nearest_entity(
        self, origin: tuple[float, float, float] | None, match: Callable[[EntityState], bool] | None = None,
    ) -> EntityState | None:
        """Find the nearest observed remote actor with a known identity and position."""
        return self._nearest_entity(origin, match, players_only=False)

    def _nearest_entity(self, origin, match, *, players_only: bool) -> EntityState | None:
        self._sync_reader_end()
        if self._closed or origin is None or not all(map(math.isfinite, origin)):
            return None
        nearest = None
        nearest_key = None
        runtime_ids = self._player_feet_by_runtime if players_only else self._by_runtime
        matches = [] if match is not None else None
        epoch = self._dimension_epoch
        for runtime_id in tuple(runtime_ids) if match is not None else runtime_ids:
            state = self._by_runtime.get(runtime_id)
            if (state is None or runtime_id == self._own_runtime_id or state.entity_id == self._own_entity_id
                    or not state.entity_id or self._runtime_by_id.get(state.entity_id) != runtime_id):
                continue
            feet = None
            if state.actor_type == _PLAYER:
                feet = self._player_feet_by_runtime.get(runtime_id)
                if feet is None:
                    continue
                position, _ = feet
            elif players_only:
                continue
            else:
                position = state.position
            if not all(map(math.isfinite, position)):
                continue
            if match is not None:
                accepted = bool(match(state))
                self._sync_reader_end()
                if self._closed or self._dimension_epoch != epoch:
                    return None
                if not accepted or not self._nearest_candidate_current(state, feet):
                    continue
            dx, dy, dz = (coordinate - base for coordinate, base in zip(position, origin))
            key = (dx * dx + dy * dy + dz * dz, state.entity_id)
            if matches is not None:
                matches.append((key, state, feet))
            elif nearest_key is None or key < nearest_key:
                nearest, nearest_key = state, key

        # A later predicate can invalidate an earlier match. Retain alternatives
        # during callback scans so the nearest surviving match still wins.
        if matches is not None:
            for key, state, feet in matches:
                if (nearest_key is None or key < nearest_key) and self._nearest_candidate_current(state, feet):
                    nearest, nearest_key = state, key
        return nearest

    def _nearest_candidate_current(self, state: EntityState, feet: _PlayerFeet | None) -> bool:
        runtime_id = state.runtime_id
        return (self._by_runtime.get(runtime_id) is state
                and self._runtime_by_id.get(state.entity_id) == runtime_id
                and (feet is None or self._player_feet_by_runtime.get(runtime_id) is feet))

    def _actor_runtime_id(self, entity_id: int) -> int | None:
        if entity_id == self._own_entity_id:
            return self._own_runtime_id
        return self._runtime_by_id.get(entity_id)

    def _actor_entity_id(self, runtime_id: int | None) -> int | None:
        if runtime_id is not None and runtime_id == self._own_runtime_id:
            return self._own_entity_id or None
        state = self._by_runtime.get(runtime_id)
        return (state.entity_id or None) if state is not None else None

    def _animation_entity_id(self, runtime_id: int) -> int | None:
        entity_id = self._actor_entity_id(runtime_id)
        if entity_id is not None:
            return entity_id
        equipment = self._player_equipment.get(runtime_id)
        return equipment.entity_id if equipment is not None else None

    def _vehicle_link(self, rider: int, vehicle: int, role: Literal["RIDING", "PASSENGER"]) -> VehicleLink:
        return VehicleLink(vehicle, rider, role, self._actor_runtime_id(vehicle), self._actor_runtime_id(rider))

    def vehicle_links(self) -> tuple[VehicleLink, ...]:
        self._sync_reader_end()
        return tuple(self._vehicle_link(rider, vehicle, role) for rider, (vehicle, role) in self._rides.items())

    def vehicle(self) -> VehicleLink | None:
        self._sync_reader_end()
        rider = self._own_entity_id
        if rider is None or rider not in self._rides:
            return None
        return self._vehicle_link(rider, *self._rides[rider])

    @property
    def _own_unmounted_epoch(self) -> int | None:
        """Identify the current interval in which the local actor has no observed ride."""
        self._sync_reader_end()
        if self._closed or self._own_entity_id in self._rides:
            return None
        return self._own_ride_epoch

    def subscribe_vehicle_links(
        self, callback: Callable[[VehicleLinkChange], None], *,
        rider_entity_id: int | None, vehicle_entity_id: int | None,
    ) -> Callable[[], None]:
        self._require_live()

        def observe(change: VehicleLinkChange) -> None:
            if rider_entity_id is not None and change.rider_entity_id != rider_entity_id:
                return
            if vehicle_entity_id is not None and all(
                link is None or link.vehicle_entity_id != vehicle_entity_id for link in (change.before, change.after)
            ):
                return
            try:
                callback(change)
            except asyncio.CancelledError as error:
                asyncio.get_running_loop().call_exception_handler({
                    "message": "vehicle link observer failed", "exception": error, "callback": callback,
                })

        return self.packets._subscribe(self._ride_observers, observe)

    def _set_ride(self, rider: int, ride: _Ride | None, reason: Literal["link", "unobserved"]) -> None:
        previous = self._rides.get(rider)
        if previous == ride:
            return
        if ride is None:
            del self._rides[rider]
        else:
            self._rides[rider] = ride
        if rider == self._own_entity_id:
            self._own_ride_epoch += 1
        else:
            runtime_id = self._runtime_by_id.get(rider)
            if runtime_id is not None:
                self._player_feet_by_runtime.pop(runtime_id, None)
                instance = self._actor_instances.get(runtime_id)
                if instance is not None:
                    instance.feet_known = False
                state = self._by_runtime.get(runtime_id)
                if state is not None and state.actor_type == "minecraft:item":
                    self._cursor_bodies.pop(runtime_id, None)
            else:
                self._unresolved_ride_epoch += 1
                self._unresolved_ride_changes.append((self._unresolved_ride_epoch, rider))
                # The spawn can identify this view before the roster binds its entity ID.
                for runtime, view in self._player_views.items():
                    if view.spawn_entity_id == rider:
                        self._player_views[runtime] = replace(view, rotation=None)
        if self._ride_observers:
            before = None if previous is None else self._vehicle_link(rider, *previous)
            after = None if ride is None else self._vehicle_link(rider, *ride)
            self.packets._notify(self._ride_observers, VehicleLinkChange(rider, before, after, reason))

    def _observe_link(self, link) -> None:
        if self._closed:
            return
        types = self.packets.protocol.binding.ActorLinkType
        vehicle, rider = int(link.a), int(link.b)
        if link.type == types.RIDING:
            self._set_ride(rider, (vehicle, "RIDING"), "link")
        elif link.type == types.PASSENGER:
            self._set_ride(rider, (vehicle, "PASSENGER"), "link")
        elif link.type == types.NONE:
            current = self._rides.get(rider)
            if current is not None and current[0] == vehicle:
                self._set_ride(rider, None, "link")

    def _forget_links(self, entity_id: int) -> None:
        for rider, (vehicle, _) in tuple(self._rides.items()):
            if rider == entity_id or vehicle == entity_id:
                self._set_ride(rider, None, "unobserved")

    def _clear_links(self) -> None:
        rides, self._rides = self._rides, {}
        if self._own_entity_id in rides:
            self._own_ride_epoch += 1
        if self._ride_observers:
            for rider, ride in rides.items():
                before = self._vehicle_link(rider, *ride)
                self.packets._notify(self._ride_observers, VehicleLinkChange(rider, before, None, "unobserved"))

    def _sync_reader_end(self) -> None:
        # Raw read waiters can resume before the reader's done callback runs.
        if self._unsubscribe_reader_end is not None:
            reader = self.packets._reader_task
            if reader is not None and reader.done():
                self._end_session()

    def _require_live(self) -> None:
        self._sync_reader_end()
        if self._closed:
            raise ConnectionError("entity session is closed")

    async def dismount(self) -> None:
        self._require_live()
        link = self.vehicle()
        if link is None or link.vehicle_runtime_id is None:
            raise RuntimeError("the player is not riding a vehicle the client knows")
        packet = self.packets.protocol.binding.InteractPacket
        await self.packets.send(packet(action=packet.Action.STOP_RIDING, target_id=link.vehicle_runtime_id, pos=None))

    def drops(self) -> tuple[EntityState, ...]:
        return tuple(state for state in self._by_runtime.values()
                     if state.actor_type == "minecraft:item")

    def _record(self, state: EntityState, *, player_uuid: UUID | None = None,
                player_feet: _PlayerFeet | None = None, new_instance: bool = True,
                unlisted_entity_id: int | None = None, identity_matches: bool = True,
                lifecycle_events: list[EntityLifecycleEvent] | None = None) -> None:
        runtime_id = state.runtime_id
        previous = self._by_runtime.get(runtime_id)
        known_id = self._animation_entity_id(runtime_id)
        # A conflicting roster ID can appear in the snapshot without claiming that identity.
        entity_id = (state.entity_id or unlisted_entity_id) if identity_matches else None
        replaced = new_instance or (known_id is not None and state.entity_id and known_id != state.entity_id)
        if replaced:
            self._end_movement_waiters(LookupError, "entity is no longer observed", runtime_id=runtime_id)
            self._forget_entity_observers(self._swing_observers, runtime_id)
        if previous is not None and self._runtime_by_id.get(previous.entity_id) == runtime_id:
            if previous.entity_id != entity_id:
                self._forget_links(previous.entity_id)
            self._runtime_by_id.pop(previous.entity_id, None)
        old_uuid = self._player_uuid_by_runtime.get(runtime_id)
        if old_uuid is not None and old_uuid != player_uuid:
            self._forget_runtime(runtime_id, lifecycle_events=lifecycle_events if new_instance else None,
                                 reason="replaced")
        collision_reason: _EntityLifecycleReason = "replaced" if new_instance else "identity_invalidated"
        if player_uuid is not None:
            old_runtime = self._player_runtime_by_uuid.get(player_uuid)
            if old_runtime is not None and old_runtime != runtime_id:
                self._forget_runtime(old_runtime, lifecycle_events=lifecycle_events, reason=collision_reason)
        if entity_id is not None:
            previous_runtime = self._runtime_by_id.get(entity_id)
            if previous_runtime is not None and previous_runtime != runtime_id:
                self._forget_runtime(previous_runtime, lifecycle_events=lifecycle_events, reason=collision_reason)
            previous_unlisted = self._unlisted_player_runtime_by_id.pop(entity_id, None)
            if previous_unlisted is not None and previous_unlisted != runtime_id:
                self._forget_runtime(previous_unlisted, lifecycle_events=lifecycle_events, reason=collision_reason)
            if state.entity_id:
                self._runtime_by_id[entity_id] = runtime_id
        if replaced:
            # Link callbacks can register observers while the previous actor is still visible.
            self._forget_entity_observers(self._actor_equipment_observers, runtime_id)
            self._forget_entity_observers(self._critical_effect_observers, runtime_id)
            self._forget_entity_observers(self._health_observers, runtime_id)
            self._forget_entity_observers(self._actor_event_observers, runtime_id)
            self._forget_entity_observers(self._attribute_observers, runtime_id)
            self._forget_entity_observers(self._effect_observers, runtime_id)
            self._forget_entity_observers(self._visible_effect_observers, runtime_id)
            self._forget_entity_observers(self._crouch_observers, runtime_id)
            self._forget_entity_observers(self._sleep_observers, runtime_id)
            self._forget_entity_observers(self._name_tag_observers, runtime_id)
            self._forget_entity_observers(self._shield_block_observers, runtime_id)
            self._shield_block_asserted.discard(runtime_id)
            if not new_instance:
                # A conflicting roster identity must not inherit the previous actor's reports.
                state = replace(state, attributes=(), status_effects=(), visible_effects_value=None,
                                pitch=None, yaw=None, head_yaw=None, name_tag=None)
        if lifecycle_events is not None and new_instance:
            outgoing = self._by_runtime.get(runtime_id)
            if outgoing is not None:
                lifecycle_events.append(EntityLifecycleEvent("left", outgoing, "replaced"))
            lifecycle_events.append(EntityLifecycleEvent("entered", state, "spawn_packet"))
        self._by_runtime[runtime_id] = state
        if new_instance:
            self._forget_player_equipment(runtime_id)
            self._player_views.pop(runtime_id, None)
            self._cursor_bodies.pop(runtime_id, None)
            self._actor_instances[runtime_id] = _ActorInstance(
                state.actor_type in _ACTOR_SPAWN_FEET_TYPES and state.entity_id not in self._rides,
            )
        if unlisted_entity_id is not None:
            self._unlisted_player_runtime_by_id[unlisted_entity_id] = runtime_id
        if player_uuid is not None:
            self._player_runtime_by_uuid[player_uuid] = runtime_id
            self._player_uuid_by_runtime[runtime_id] = player_uuid
        if player_feet is None or state.entity_id in self._rides:
            self._player_feet_by_runtime.pop(runtime_id, None)
        else:
            self._player_feet_by_runtime[runtime_id] = player_feet

    def _forget_runtime(
        self, runtime_id: int, *, lifecycle_events: list[EntityLifecycleEvent] | None = None,
        reason: _EntityLifecycleReason = "identity_invalidated",
    ) -> None:
        self._end_movement_waiters(LookupError, "entity is no longer observed", runtime_id=runtime_id)
        self._forget_entity_observers(self._actor_equipment_observers, runtime_id)
        self._forget_entity_observers(self._swing_observers, runtime_id)
        self._forget_entity_observers(self._critical_effect_observers, runtime_id)
        self._forget_entity_observers(self._health_observers, runtime_id)
        self._forget_entity_observers(self._actor_event_observers, runtime_id)
        self._forget_entity_observers(self._attribute_observers, runtime_id)
        self._forget_entity_observers(self._effect_observers, runtime_id)
        self._forget_entity_observers(self._visible_effect_observers, runtime_id)
        self._forget_entity_observers(self._crouch_observers, runtime_id)
        self._forget_entity_observers(self._sleep_observers, runtime_id)
        self._forget_entity_observers(self._name_tag_observers, runtime_id)
        self._forget_entity_observers(self._shield_block_observers, runtime_id)
        self._shield_block_asserted.discard(runtime_id)
        self._forget_player_equipment(runtime_id)
        state = self._by_runtime.pop(runtime_id, None)
        if lifecycle_events is not None and state is not None:
            lifecycle_events.append(EntityLifecycleEvent("left", state, reason))
        self._actor_instances.pop(runtime_id, None)
        self._cursor_bodies.pop(runtime_id, None)
        self._player_feet_by_runtime.pop(runtime_id, None)
        self._player_views.pop(runtime_id, None)
        if state is not None and self._runtime_by_id.get(state.entity_id) == runtime_id:
            self._runtime_by_id.pop(state.entity_id, None)
        player_uuid = self._player_uuid_by_runtime.pop(runtime_id, None)
        if player_uuid is not None:
            self._player_runtime_by_uuid.pop(player_uuid, None)

    def _item_state(self, packet) -> EntityState:
        item = packet.item
        network_id = int(item.id)
        return EntityState(
            int(packet.id), int(packet.runtime_id), "minecraft:item",
            (float(packet.pos.x), float(packet.pos.y), float(packet.pos.z)),
            self._item_names.get(network_id, f"#{network_id}"),
            int(item.stack_size), network_id,
            is_from_fishing=bool(getattr(packet, "is_from_fishing", False)),
            name_tag=_actor_name_tag(getattr(getattr(packet, "data", None), "data", ()), self.packets.protocol.binding),
        )

    def _on_packet(self, raw, *, startup: bool = False) -> None:
        if self._closed:
            return
        packet_id = raw.packet_id
        b = self.packets.protocol.binding
        lifecycle_observers: tuple[Callable[[EntityLifecycleEvent], None], ...] = ()
        lifecycle_events: list[EntityLifecycleEvent] | None = None
        attached_firework = None
        firework_observers = ()
        if self._lifecycle_observers:
            match packet_id:
                case (b.AddActorPacket.ID | b.AddPlayerPacket.ID | b.AddItemActorPacket.ID | b.AddPaintingPacket.ID
                      | b.RemoveActorPacket.ID | b.TakeItemActorPacket.ID | b.PlayerListPacket.ID
                      | b.ChangeDimensionPacket.ID):
                    lifecycle_observers = tuple(self._lifecycle_observers.values())
                    lifecycle_events = []
        if packet_id == b.DisconnectPacket.ID:
            self._end_session()
        elif packet_id == b.AnimatePacket.ID:
            self._observe_animation(raw)
        elif packet_id == b.ActorEventPacket.ID:
            self._observe_actor_event(raw)
        elif packet_id == b.UpdateAttributesPacket.ID:
            self._observe_attribute_update(raw)
        elif packet_id == b.MobEffectPacket.ID:
            self._observe_effect_update(raw)
        elif packet_id == b.ItemRegistryPacket.ID:
            packet = self.packets.protocol.decode(raw)
            self._item_names = {int(item.id): str(item.name) for item in packet.items}
        elif packet_id == b.PlayerListPacket.ID:
            packet = self.packets.protocol.decode(raw)
            observers = tuple(self._player_list_observers)
            player_observers = () if startup else tuple(self._player_change_observers)
            observed_changes = [] if observers else None
            for entry in packet.entries:
                change = _player_list_change(entry)
                if observed_changes is not None:
                    observed_changes.append(change)
                runtime_id = self._player_runtime_by_uuid.get(change.uuid)
                if change.player is not None:
                    self._record_player_list_add(change.player, runtime_id, player_observers)
                    state = self._by_runtime.get(runtime_id)
                    if state is not None:
                        entity_id = change.player.entity_id
                        if not entity_id:
                            # A roster entry without a unique ID supplies no new entity identity.
                            continue
                        since = self._unidentified_equipment_since.pop(runtime_id, None)
                        if since is not None and self._equipment_removal_may_match(since, entity_id):
                            self._forget_runtime(runtime_id, lifecycle_events=lifecycle_events)
                            continue
                        feet = self._player_feet_by_runtime.get(runtime_id)
                        changes = self._unresolved_ride_changes
                        if feet is not None and not state.entity_id and changes:
                            _, observed_epoch = feet
                            # A late identity can expose a ride after the foot observation.
                            # If older changes have expired, that interval is unverified.
                            if (observed_epoch + 1 < changes[0][0] or any(
                                epoch > observed_epoch and rider == entity_id for epoch, rider in changes
                            )):
                                feet = None
                        identity_matches = (runtime_id in self._player_views
                                            and self._animation_entity_id(runtime_id) in (None, entity_id))
                        state = replace(state, entity_id=entity_id)
                        self._record(state, player_uuid=change.uuid, player_feet=feet, new_instance=False,
                                     identity_matches=identity_matches, lifecycle_events=lifecycle_events)
                        equipment = self._player_equipment.get(runtime_id)
                        if equipment is not None:
                            if equipment.entity_id is None:
                                self._player_equipment[runtime_id] = replace(equipment, entity_id=entity_id)
                            elif equipment.entity_id != entity_id:
                                self._forget_player_equipment(runtime_id)
                                self._player_views.pop(runtime_id, None)
                                state = self._by_runtime[runtime_id]
                                if (state.last_observed_offhand is not None or state.gliding is not None
                                        or state.sneaking is not None or state.sleeping is not None):
                                    self._by_runtime[runtime_id] = replace(
                                        state, last_observed_offhand=None, gliding=None, sneaking=None, sleeping=None,
                                    )
                else:
                    removed = self._players_by_uuid.pop(change.uuid, None)
                    if removed is not None and (runtime_id is None
                                                or self._runtime_by_id.get(removed.entity_id) == runtime_id):
                        self._forget_links(removed.entity_id)
                    if runtime_id is not None:
                        self._forget_runtime(runtime_id, lifecycle_events=lifecycle_events, reason="roster_removed")
            if observed_changes:
                self.packets._notify(observers, tuple(observed_changes))
        elif packet_id == b.UpdatePlayerGameTypePacket.ID:
            packet = self.packets.protocol.decode(raw)
            entity_id = int(packet.target_player)
            mode = packet.player_game_type
            if entity_id and entity_id != self._own_entity_id and mode in _EXPLICIT_GAME_MODES:
                players = [player for player in self._players_by_uuid.values() if player.entity_id == entity_id]
                if len(players) == 1 and players[0].uuid != self._own_uuid:
                    self._record_player_game_mode(players[0], mode, startup=startup)
        elif packet_id == b.PlayerSkinPacket.ID:
            packet = self.packets.protocol.decode(raw)
            listed = self._players_by_uuid.get(packet.uuid)
            if listed is not None:
                skin = _player_skin_snapshot(getattr(packet, "skin", None))
                updated = replace(listed, skin=skin)
                self._players_by_uuid[packet.uuid] = updated
                self._last_skin_update = raw, packet.uuid
                player_observers = () if startup else tuple(self._player_change_observers)
                self.packets._notify(self._player_skin_observers, updated)
                if player_observers and skin != listed.skin:
                    self.packets._notify(player_observers, updated)
        elif packet_id == b.AddPlayerPacket.ID:
            player = self.packets.protocol.decode(raw)
            for link in player.links:
                self._observe_link(link)
            runtime_id = int(player.runtime_id)
            if runtime_id == self._own_runtime_id:
                return
            player_uuid = player.uuid
            listed = self._players_by_uuid.get(player_uuid)
            position = (float(player.pos.x), float(player.pos.y), float(player.pos.z))
            metadata = getattr(getattr(player, "unpack", None), "data", ())
            entity_id = int(getattr(getattr(player, "abilities_data", None), "target_player", 0) or 0)
            listed_id = listed.entity_id if listed is not None else 0
            identity_matches = not entity_id or not listed_id or entity_id == listed_id
            words = _player_flag_words(metadata, b) if identity_matches else (None, None)
            gliding, sneaking = _player_posture(words, b)
            sleeping = _player_sleeping(metadata, b) if identity_matches else None
            feet = None
            riding_block = getattr(player, "passenger_of_block_data", None) is not None
            if not riding_block and not _unsupported_view_pose(words, b):
                feet = position, self._unresolved_ride_epoch
            pitch, yaw, head_yaw = map(_last_angle, _float_angles(player, "y_head_rot")) if identity_matches else (
                None, None, None,
            )
            self._record(EntityState(
                listed_id, runtime_id, _PLAYER, position, name=str(player.name), gliding=gliding, sneaking=sneaking,
                sleeping=sleeping,
                visible_effects_value=_visible_effects_value(metadata, b) if identity_matches else None,
                pitch=pitch, yaw=yaw, head_yaw=head_yaw,
                name_tag=_actor_name_tag(metadata, b) if identity_matches else None,
            ), player_uuid=player_uuid, player_feet=feet,
                unlisted_entity_id=entity_id if entity_id and not listed_id else None,
                identity_matches=identity_matches, lifecycle_events=lifecycle_events)
            mode = getattr(player, "player_game_type", None)
            if (entity_id and entity_id != self._own_entity_id and player_uuid != self._own_uuid
                    and mode in _EXPLICIT_GAME_MODES):
                if listed is None:
                    self._actor_instances[runtime_id].unlisted_game_mode = entity_id, mode
                elif listed_id == entity_id:
                    self._record_player_game_mode(listed, mode, startup=startup)
            if getattr(self.packets.protocol, "number", None) == 2225:
                self._cursor_bodies[runtime_id] = _cursor_body(metadata, b)
            if identity_matches:
                spawn_entity_id = entity_id or listed_id or None
                if spawn_entity_id is None:
                    self._unidentified_equipment_since[runtime_id] = self._equipment_removal_cursor()
                self._player_equipment[runtime_id] = PlayerEquipmentSnapshot(
                    runtime_id, spawn_entity_id, str(player.name),
                    main_hand=_equipment_stack(getattr(player, "carried_item", None)),
                )
                rotation = None
                if spawn_entity_id not in self._rides and feet is not None:
                    rotation = _player_rotation(player)
                self._player_views[runtime_id] = _PlayerView(
                    spawn_entity_id, rotation, words,
                    ordinary_move=feet is not None and mode != b.GameType.SPECTATOR,
                    riding_block=riding_block,
                )
                if _player_flag(words, b.ActorFlags.BLOCKED_USING_SHIELD):
                    self._shield_block_asserted.add(runtime_id)
        elif packet_id == b.SetActorDataPacket.ID:
            packet = self.packets.protocol.decode(raw)
            runtime_id = int(packet.id)
            metadata = packet.packed_items.data
            shield_observers = ()
            if self._shield_block_edge(runtime_id, metadata, b) and not startup and self._shield_block_observers:
                shield_observers = tuple(self._shield_block_observers)
                shield_block = ShieldBlockEvent(runtime_id, self._animation_entity_id(runtime_id), int(packet.tick))
            state = self._by_runtime.get(runtime_id)
            confirmed_remote_entity = self._is_confirmed_remote_entity(runtime_id)
            name_tag_observers = ()
            if state is not None and confirmed_remote_entity:
                name_tag = _actor_name_tag(metadata, b)
                if name_tag is not None and name_tag != state.name_tag:
                    state = self._by_runtime[runtime_id] = replace(state, name_tag=name_tag)
                    if not startup:
                        name_tag_observers = tuple(self._name_tag_observers)
            if state is not None and state.actor_type == _WOLF:
                owner = _owner_entity_id(metadata, b, state.owner_entity_id)
                sitting = _wolf_sitting(metadata, b, state.sitting)
                if owner != state.owner_entity_id or sitting != state.sitting:
                    state = replace(state, owner_entity_id=owner, sitting=sitting)
                    self._by_runtime[runtime_id] = state
            elif state is not None and state.actor_type == _VILLAGER:
                tier, experience = _villager_trade_progress(metadata, b, state.trade_tier, state.trade_experience)
                if tier != state.trade_tier or experience != state.trade_experience:
                    state = replace(state, trade_tier=tier, trade_experience=experience)
                    self._by_runtime[runtime_id] = state
            visible_observers = ()
            if state is not None and confirmed_remote_entity:
                visible_effects = _visible_effects_value(metadata, b)
                if visible_effects is not None and visible_effects != state.visible_effects_value:
                    state = replace(state, visible_effects_value=visible_effects)
                    self._by_runtime[runtime_id] = state
                    if not startup:
                        visible_observers = tuple(self._visible_effect_observers)
            body = self._cursor_bodies.get(runtime_id)
            if body is not None:
                body = _cursor_body(metadata, b, body)
                actor_type = self._by_runtime[runtime_id].actor_type
                if actor_type == "minecraft:item" and _player_flag(body.flags, b.ActorFlags.RIDING):
                    # Metadata can report a ride without a link; its end does not supply a new anchor.
                    self._cursor_bodies.pop(runtime_id)
                else:
                    self._cursor_bodies[runtime_id] = body
                    if actor_type == _SHEEP and not _adult_cursor_pose(body, actor_type, b):
                        self._actor_instances[runtime_id].feet_known = False
            view = self._player_views.get(runtime_id)
            if view is not None:
                words = _player_flag_words(metadata, b, view.flags)
                # Pose metadata invalidates the anchor but cannot establish a new one.
                unsupported = _unsupported_view_pose(words, b)
                if unsupported:
                    self._player_feet_by_runtime.pop(runtime_id, None)
                self._player_views[runtime_id] = replace(
                    view, rotation=None if unsupported else view.rotation, flags=words,
                    ordinary_move=view.ordinary_move and not unsupported,
                )
                gliding, sneaking = _player_posture(words, b)
                sleeping = _player_sleeping(metadata, b)
                previous = self._by_runtime[runtime_id]
                if gliding is None:
                    gliding, sneaking = previous.gliding, previous.sneaking
                if sleeping is None:
                    sleeping = previous.sleeping
                if gliding != previous.gliding or sneaking != previous.sneaking or sleeping != previous.sleeping:
                    state = replace(previous, gliding=gliding, sneaking=sneaking, sleeping=sleeping)
                    self._by_runtime[runtime_id] = state
                    # Bind both observer lists before another callback can change them.
                    crouch_observers = (
                        tuple(self._crouch_observers)
                        if previous.sneaking is not None and sneaking != previous.sneaking else ()
                    )
                    sleep_observers = (
                        tuple(self._sleep_observers)
                        if previous.sleeping is not None and sleeping != previous.sleeping else ()
                    )
                    if previous.gliding is False and gliding:
                        self.packets._notify(self._glide_observers, state)
                    if crouch_observers:
                        self._notify_entity_observers(
                            crouch_observers, state, runtime_id, "entity crouch observer failed",
                        )
                    if sleep_observers:
                        self._notify_entity_observers(
                            sleep_observers, state, runtime_id, "entity sleep observer failed",
                        )
            if shield_observers and not self._closed:
                self._notify_entity_observers(shield_observers, shield_block, runtime_id, "shield block observer failed")
            if visible_observers and not self._closed:
                event = EntityVisibleEffectUpdate(state, int(packet.tick))
                self._notify_entity_observers(visible_observers, event, runtime_id, "visible effect observer failed")
            if name_tag_observers and not self._closed:
                self._notify_entity_observers(name_tag_observers, state, runtime_id, "entity name tag observer failed")
        elif packet_id == b.MobEquipmentPacket.ID:
            packet = self.packets.protocol.decode(raw)
            runtime_id = int(packet.runtime_id)
            current = self._player_equipment.get(runtime_id)
            if current is None:
                # Protocol 2225 uses the zero selectors in INVENTORY for the actor's main hand.
                if (self.packets.protocol.number == 2225 and packet.container_id == b.ContainerID.INVENTORY
                        and packet.slot == packet.selected_slot == 0
                        and (instance := self._nonplayer_actor(runtime_id)) is not None):
                    self._publish_actor_equipment(runtime_id, instance, startup=startup,
                                                  main_hand=_equipment_stack(packet.item))
                return
            if packet.container_id == b.ContainerID.INVENTORY:
                self._publish_player_equipment(current, replace(
                    current, selected_slot=int(packet.selected_slot), main_hand=_equipment_stack(packet.item),
                ))
            elif packet.container_id == b.ContainerID.OFFHAND:
                state = self._by_runtime[runtime_id]
                offhand = _equipment_stack(packet.item)
                if offhand != state.last_observed_offhand:
                    self._by_runtime[runtime_id] = replace(state, last_observed_offhand=offhand)
        elif packet_id == b.MobArmorEquipmentPacket.ID:
            packet = self.packets.protocol.decode(raw)
            runtime_id = int(packet.runtime_id)
            current = self._player_equipment.get(runtime_id)
            if current is not None:
                self._publish_player_equipment(current, replace(
                    current, head=_equipment_stack(packet.head), chest=_equipment_stack(packet.torso),
                    legs=_equipment_stack(packet.legs), feet=_equipment_stack(packet.feet),
                ))
            elif (instance := self._nonplayer_actor(runtime_id)) is not None:
                self._publish_actor_equipment(
                    runtime_id, instance, startup=startup,
                    head=_equipment_stack(packet.head), chest=_equipment_stack(packet.torso),
                    legs=_equipment_stack(packet.legs), feet=_equipment_stack(packet.feet),
                    body=_equipment_stack(getattr(packet, "body", None)),
                )
        elif packet_id == b.AddActorPacket.ID:
            actor = self.packets.protocol.decode(raw)
            runtime_id = int(actor.runtime_id)
            owner = None
            if actor.actor_type in (_WOLF, "minecraft:fishing_hook"):
                owner = _owner_entity_id(actor.data.data, b)
            trade_tier, trade_experience = (
                _villager_trade_progress(actor.data.data, b) if actor.actor_type == _VILLAGER else (None, None)
            )
            attachment = None
            if actor.actor_type == _FIREWORK:
                # Fireworks use this metadata for the attached actor's unique ID.
                attachment = next((int(entry.payload.value) for entry in actor.data.data
                                   if int(entry.id) == int(b.ActorDataIDs.CUSTOM_DISPLAY)
                                   and isinstance(entry.payload, b.DataItemInt64Payload)), None)
            is_boat = actor.actor_type == "minecraft:boat"
            spawn_attributes = getattr(actor, "attributes", ()) if runtime_id != self._own_runtime_id else ()
            attributes: dict[str, EntityAttribute] = {}
            for attribute in spawn_attributes:
                name = str(attribute.name)
                attributes[name] = EntityAttribute(
                    name, float(attribute.min_value), float(attribute.current_value), float(attribute.max_value),
                )
            pitch, yaw, head_yaw = map(_last_angle, _float_angles(actor, "y_head_rotation"))
            state = EntityState(
                int(actor.entity_id), runtime_id, actor.actor_type,
                (float(actor.pos.x), float(actor.pos.y), float(actor.pos.z)),
                owner_entity_id=owner,
                boat_rotation=(float(actor.rot.x), float(actor.rot.y)) if is_boat else None,
                boat_velocity=(float(actor.velocity.x), float(actor.velocity.y), float(actor.velocity.z))
                if is_boat else (0.0, 0.0, 0.0),
                attributes=tuple(attributes.values()),
                firework_attached_entity_id=None if attachment == -1 else attachment,
                visible_effects_value=_visible_effects_value(getattr(getattr(actor, "data", None), "data", ()), b)
                if runtime_id != self._own_runtime_id else None,
                sitting=_wolf_sitting(actor.data.data, b) if actor.actor_type == _WOLF else None,
                trade_tier=trade_tier,
                trade_experience=trade_experience,
                pitch=pitch, yaw=yaw, head_yaw=head_yaw,
                name_tag=_actor_name_tag(getattr(getattr(actor, "data", None), "data", ()), b),
            )
            if (not startup and self._attached_firework_observers and self._own_entity_id
                    and runtime_id != self._own_runtime_id and state.firework_attached_entity_id == self._own_entity_id):
                previous = self._by_runtime.get(runtime_id)
                if (previous is None or previous.actor_type != _FIREWORK or previous.entity_id != state.entity_id
                        or previous.firework_attached_entity_id != state.firework_attached_entity_id):
                    attached_firework = state
                    firework_observers = tuple(self._attached_firework_observers)
            self._record(state, lifecycle_events=lifecycle_events)
            if (actor.actor_type in _ACTOR_SPAWN_FEET_TYPES
                    and getattr(actor, "passenger_of_block_data", None) is not None):
                self._actor_instances[runtime_id].feet_known = False
            if actor.actor_type in _CURSOR_BODY_TYPES and getattr(self.packets.protocol, "number", None) == 2225:
                body = self._cursor_bodies[runtime_id] = _cursor_body(actor.data.data, b)
                if actor.actor_type == _SHEEP and not _adult_cursor_pose(body, actor.actor_type, b):
                    self._actor_instances[runtime_id].feet_known = False
            for link in actor.links:
                self._observe_link(link)
        elif packet_id == b.SetActorLinkPacket.ID:
            self._observe_link(self.packets.protocol.decode(raw).link)
        elif packet_id == getattr(getattr(b, "SetPassengerOfBlockPacket", None), "ID", None):
            packet = self.packets.protocol.decode(raw)
            entity_id = int(packet.passenger)
            runtime_id = self._runtime_by_id.get(entity_id)
            if runtime_id is None:
                runtime_id = self._unlisted_player_runtime_by_id.get(entity_id)
            view = self._player_views.get(runtime_id)
            if view is not None:
                riding_block = packet.passenger_of_block_data is not None
                self._player_views[runtime_id] = replace(view, riding_block=riding_block)
                if riding_block:
                    self._player_feet_by_runtime.pop(runtime_id, None)
        elif packet_id == b.AddItemActorPacket.ID:
            packet = self.packets.protocol.decode(raw)
            state = self._item_state(packet)
            self._record(state, lifecycle_events=lifecycle_events)
            if getattr(self.packets.protocol, "number", None) == 2225:
                body = _cursor_body(packet.data.data, b)
                if not _player_flag(body.flags, b.ActorFlags.RIDING):
                    self._actor_instances[state.runtime_id].feet_known = True
                    self._cursor_bodies[state.runtime_id] = body
        elif packet_id == b.AddPaintingPacket.ID:
            painting = _painting_snapshot(self.packets.protocol.decode(raw))
            self._record(EntityState(
                painting.entity_id, painting.runtime_id, _PAINTING, painting.position, painting=painting,
            ), lifecycle_events=lifecycle_events)
        elif packet_id == b.ChangeDimensionPacket.ID:
            # Actors from the previous dimension are no longer observable.
            self._dimension_epoch += 1
            self._last_pickup = None
            self._end_movement_waiters(RuntimeError, "dimension changed while waiting for entity movement")

            def dimension_bound(observer):
                return observer.runtime_id is not None or observer.waiter is not None

            self._end_entity_observers(self._swing_observers, dimension_bound,
                                       RuntimeError, "dimension changed while waiting for an entity swing")
            self._clear_player_equipment(LookupError, "remote player is no longer observed")
            self._clear_links()
            self._end_entity_observers(self._actor_equipment_observers, dimension_bound,
                                       LookupError, "entity is no longer observed")
            self._end_entity_observers(self._critical_effect_observers, dimension_bound,
                                       RuntimeError, "dimension changed while waiting for an entity critical effect")
            self._end_entity_observers(self._health_observers, dimension_bound,
                                       RuntimeError, "dimension changed while waiting for an entity health event")
            self._end_entity_observers(self._actor_event_observers, dimension_bound,
                                       RuntimeError, "dimension changed while waiting for an entity actor event")
            self._end_entity_observers(self._attribute_observers, dimension_bound,
                                       RuntimeError, "dimension changed while waiting for an entity attribute update")
            self._end_entity_observers(self._effect_observers, dimension_bound,
                                       RuntimeError, "dimension changed while waiting for an entity effect update")
            self._end_entity_observers(self._visible_effect_observers, dimension_bound,
                                       RuntimeError, "dimension changed while waiting for a visible effect change")
            self._end_entity_observers(self._crouch_observers, dimension_bound,
                                       RuntimeError, "dimension changed while waiting for an entity crouch change")
            self._end_entity_observers(self._sleep_observers, dimension_bound,
                                       RuntimeError, "dimension changed while waiting for an entity sleep change")
            self._end_entity_observers(self._name_tag_observers, dimension_bound,
                                       RuntimeError, "dimension changed while waiting for an entity name tag change")
            self._end_entity_observers(self._attached_firework_observers, dimension_bound,
                                       RuntimeError, "dimension changed while waiting for an attached firework")
            self._end_entity_observers(self._shield_block_observers, dimension_bound,
                                       RuntimeError, "dimension changed while waiting for a shield block")
            self._shield_block_asserted.clear()
            if lifecycle_events is not None:
                lifecycle_events.extend(
                    EntityLifecycleEvent("left", state, "dimension_changed") for state in self._by_runtime.values()
                )
            self._by_runtime.clear()
            self._actor_instances.clear()
            self._cursor_bodies.clear()
            self._runtime_by_id.clear()
            # PlayerList membership survives a dimension switch.
            self._player_runtime_by_uuid.clear()
            self._player_uuid_by_runtime.clear()
            self._player_feet_by_runtime.clear()
            self._player_views.clear()
            self._unresolved_ride_changes.clear()
        elif packet_id == b.RespawnPacket.ID and self._own_runtime_id in self._shield_block_asserted:
            packet = self.packets.protocol.decode(raw)
            if (packet.runtime_id in (0, self._own_runtime_id)
                    and packet.state == b.PlayerRespawnState.READY_TO_SPAWN):
                self._shield_block_asserted.discard(self._own_runtime_id)
        elif packet_id == b.RemoveActorPacket.ID:
            entity_id = int(self.packets.protocol.decode(raw).entity_id)
            self._forget_links(entity_id)
            runtime_id = self._runtime_by_id.get(entity_id)
            if runtime_id is None:
                runtime_id = self._unlisted_player_runtime_by_id.get(entity_id)
            if runtime_id is not None:
                self._forget_runtime(runtime_id, lifecycle_events=lifecycle_events, reason="remove_actor")
            elif entity_id and self._unidentified_equipment_since:
                self._equipment_removals.append((self._equipment_removal_cursor() + 1, entity_id))
        elif packet_id == b.MovePlayerPacket.ID:
            move = self.packets.protocol.decode(raw)
            runtime_id = int(move.player_id)
            state = self._by_runtime.get(runtime_id)
            if state is not None and state.actor_type == "minecraft:player":
                x, y, z = position = (float(move.pos.x), float(move.pos.y), float(move.pos.z))
                pitch, yaw, head_yaw = angles = _float_angles(move, "y_head_rot")
                state = replace(state, position=position, pitch=_last_angle(pitch, state.pitch),
                                yaw=_last_angle(yaw, state.yaw), head_yaw=_last_angle(head_yaw, state.head_yaw))
                self._by_runtime[runtime_id] = state
                view = self._player_views.get(runtime_id)
                unsupported = view is not None and (view.riding_block or _unsupported_view_pose(view.flags, b))
                if unsupported or move.riding_id or int(move.reset_position) == 3 or state.entity_id in self._rides:
                    self._player_feet_by_runtime.pop(runtime_id, None)
                else:
                    # Ordinary MovePlayer retains its standing-height reference while sneaking.
                    self._player_feet_by_runtime[runtime_id] = ((x, y - 1.62, z), self._unresolved_ride_epoch)
                if view is not None:
                    rotation = None
                    ordinary_move = (move.on_ground and int(move.reset_position) == 0 and not move.riding_id
                                     and view.spawn_entity_id not in self._rides and not unsupported)
                    if ordinary_move:
                        rotation = _player_rotation(move)
                    self._player_views[runtime_id] = replace(
                        view, rotation=rotation, ordinary_move=ordinary_move,
                    )
                self._observe_movement(state, angles, bool(move.on_ground))
        elif packet_id == b.TakeItemActorPacket.ID:
            packet = self.packets.protocol.decode(raw)
            runtime_id, collector = int(packet.item_id), int(packet.actor_id)
            state = self._by_runtime.get(runtime_id)
            # The packet waiters run after the entity observer removes the item.
            self._last_pickup = (raw, ItemPickup(
                runtime_id, collector, self._actor_entity_id(runtime_id), self._actor_entity_id(collector),
                state if state is not None and state.actor_type == "minecraft:item" else None,
            ), self._actor_instances.get(runtime_id))
            self._forget_runtime(runtime_id, lifecycle_events=lifecycle_events, reason="pickup")
        elif packet_id == b.SetActorMotionPacket.ID:
            motion = self.packets.protocol.decode(raw)
            runtime_id = int(motion.runtime_id)
            state = self._by_runtime.get(runtime_id)
            if state is not None and state.boat_rotation is not None:
                self._by_runtime[runtime_id] = replace(
                    state, boat_velocity=(float(motion.motion.x), float(motion.motion.y), float(motion.motion.z)),
                )
        elif packet_id in (b.MoveActorAbsolutePacket.ID, b.MoveActorDeltaPacket.ID):
            move = self.packets.protocol.decode(raw).move_data
            runtime_id = int(move.runtime_id)
            state = self._by_runtime.get(runtime_id)
            if state is None:
                return
            protocol_2225 = getattr(self.packets.protocol, "number", None) == 2225
            item = state.actor_type == "minecraft:item" and protocol_2225
            instance = self._actor_instances.get(runtime_id) if item or state.actor_type in _ACTOR_SPAWN_FEET_TYPES else None
            if packet_id == b.MoveActorAbsolutePacket.ID:
                position = (float(move.pos.x), float(move.pos.y), float(move.pos.z))
                if instance is not None:
                    instance.feet_known = False
                if state.actor_type == "minecraft:item":
                    # Only the spawn and delta references are verified for drops.
                    self._cursor_bodies.pop(runtime_id, None)
            else:
                axes = move.new_position_x, move.new_position_y, move.new_position_z
                position = tuple(
                    float(value) if value is not None else old
                    for value, old in zip(axes, state.position)
                )
                # A delta with omitted axes can only keep a previously verified feet position.
                if instance is not None:
                    if state.actor_type == _SHEEP:
                        if not protocol_2225:
                            instance.feet_known = instance.feet_known and all(value is None for value in axes)
                        elif move.force_completion or move.force_move_local_entity:
                            instance.feet_known = False
                        elif move.force_move:
                            # Stock sheep teleports supply all three feet coordinates in one forced delta.
                            body = self._cursor_bodies.get(runtime_id)
                            instance.feet_known = (all(value is not None for value in axes)
                                                   and body is not None and _adult_cursor_pose(body, _SHEEP, b)
                                                   and state.entity_id not in self._rides)
                    elif all(value is not None for value in axes):
                        instance.feet_known = state.entity_id not in self._rides
            pitch, yaw, head_yaw = angles = tuple(
                None if angle is None else _actor_angle(angle)
                for angle in (getattr(move, "rot_x", None), getattr(move, "rot_y", None), getattr(move, "rot_y_head", None))
            )
            rotation = state.boat_rotation
            if rotation is not None:
                rotation = _last_angle(pitch, rotation[0]), _last_angle(yaw, rotation[1])
            state = replace(state, position=position, boat_rotation=rotation, pitch=_last_angle(pitch, state.pitch),
                            yaw=_last_angle(yaw, state.yaw), head_yaw=_last_angle(head_yaw, state.head_yaw))
            self._by_runtime[runtime_id] = state
            self._player_feet_by_runtime.pop(runtime_id, None)
            on_ground = (bool(getattr(move, "header", 0) & 1) if packet_id == b.MoveActorAbsolutePacket.ID
                         else bool(getattr(move, "is_on_ground", False)))
            self._observe_movement(state, angles, on_ground)

        # Other packet observers may already have changed the subscriptions.
        if lifecycle_events and not self._closed:
            for event in lifecycle_events:
                # Roster updates can project the local unique ID onto a remote runtime.
                if event.entity.runtime_id != self._own_runtime_id:
                    self.packets._notify(lifecycle_observers, event)
        if attached_firework is not None and not self._closed:
            self._notify_entity_observers(firework_observers, attached_firework, attached_firework.runtime_id,
                                          "attached firework observer failed")

    async def attack(self, movement, inventory, runtime_id: int, *,
                     hit_height: float = 0.5) -> None:
        """Submit an attack transaction; server damage is observed separately."""
        await self._use_actor(movement, inventory, runtime_id,
                              hit_height=hit_height, interact=False)

    async def interact(self, movement, inventory, runtime_id: int, *,
                       hit_height: float = 0.5, _before_send: Callable[[int, object], None] | None = None) -> None:
        """Submit a right-click actor transaction; no server effect is implied."""
        await self._use_actor(movement, inventory, runtime_id,
                              hit_height=hit_height, interact=True, _before_send=_before_send)

    async def interact_at(self, movement, inventory, runtime_id: int,
                          position: tuple[float, float, float]) -> None:
        b = self.packets.protocol.binding
        invalid_position = "position must contain three finite world coordinates representable by Vec3"
        if not isinstance(position, Sequence) or len(position) != 3:
            raise ValueError(invalid_position)
        try:
            hit_pos = b.Vec3(x=position[0], y=position[1], z=position[2])
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(invalid_position) from error
        if not all(map(math.isfinite, (hit_pos.x, hit_pos.y, hit_pos.z))):
            raise ValueError(invalid_position)

        await self._use_actor(movement, inventory, runtime_id, interact=True, hit_pos=hit_pos)

    async def mount(self, movement, inventory, runtime_id: int) -> None:
        await self._use_actor(movement, inventory, runtime_id, hit_height=0.5, interact=True, mounting=True)

    async def swing(self, player_id: int, source: str = "NONE") -> None:
        """Submit an arm-swing animation; packet submission is not server receipt."""
        await self.animate(player_id, "SWING", source=source)

    async def animate(self, player_id: int, action_name: str, data: float = 0.0, *, source: str | None = None) -> None:
        self._require_live()
        b = self.packets.protocol.binding
        await self.packets.send(animation(b, player_id, action_name, data, source=source))

    async def _use_actor(self, movement, inventory, runtime_id: int, *,
                         hit_height: float = 0.5, interact: bool, mounting: bool = False, hit_pos=None,
                         _before_send: Callable[[int, object], None] | None = None) -> None:
        """Use an ItemUseOnActor transaction with the selected item and position."""
        self._require_live()
        actor = self.get(runtime_id)
        if hit_pos is not None:
            instance = self._actor_instance(runtime_id)
            if instance is None:
                raise LookupError("entity is not currently observed")
            target_entity_id = self._animation_entity_id(runtime_id)
        unsubscribe = None
        if mounting:
            if actor is None or not actor.entity_id:
                raise ValueError("vehicle actor is not currently observed")
            entity_id, epoch = actor.entity_id, self._dimension_epoch
            invalidated = False

            def check_target(_raw=None):
                nonlocal invalidated
                current = self.get(runtime_id)
                if self._dimension_epoch != epoch or current is None or current.entity_id != entity_id:
                    invalidated = True
                return current

            unsubscribe = self.packets.subscribe_raw(check_target)
        try:
            if hit_pos is None:
                slot = inventory.selected_slot
                item = inventory.item_or_empty(slot)
            await movement.wait_for_position_sync()
        finally:
            if unsubscribe is not None:
                unsubscribe()
        self._require_live()
        if hit_pos is not None:
            if (self._actor_instance(runtime_id) is not instance or (target_entity_id is not None
                    and self._animation_entity_id(runtime_id) != target_entity_id)):
                raise LookupError("entity target changed while waiting to interact")
            slot = inventory.selected_slot
            item = inventory.item_or_empty(slot)
        elif mounting:
            actor = check_target()
            if invalidated:
                raise RuntimeError("vehicle target changed while waiting to mount")
        elif _before_send is not None:
            actor = self.get(runtime_id)
        # Position sync waits for a server pose. Flat walking can then advance the
        # input origin through committed frames before the server sends another pose.
        x, y, z = movement._actor_interaction_origin()
        b = self.packets.protocol.binding
        if hit_pos is None:
            # Use the player's position when the target actor is outside the tracking cache.
            target = actor.position if actor is not None else (x, y, z)
            hit_pos = b.Vec3(x=target[0], y=target[1] + hit_height, z=target[2])
        action = b.ItemUseOnActorInventoryTransaction(
            transaction=b.InventoryTransaction(actions=[]),
            runtime_id=runtime_id,
            action_type=(b.ItemUseOnActorInventoryTransaction.ActionType.INTERACT
                         if interact else b.ItemUseOnActorInventoryTransaction.ActionType.ATTACK),
            slot=slot,
            item=item,
            from_pos=b.Vec3(x=x, y=y, z=z),
            hit_pos=hit_pos,
        )
        packet = b.InventoryTransactionPacket(
            legacy_request_id=b.ItemStackLegacyRequestId(raw_id=0),
            legacy_set_item_slots=None,
            variant_transaction=action,
        )
        if _before_send is None:
            await self.packets.send(packet)
        else:
            await self.packets.send(packet, _before_send=lambda: _before_send(slot, item))

    async def wait_for_spawn(self, *, actor_type: str | None = None, timeout: float = 15) -> EntitySpawn:
        """Wait for AddActor, also accepting AddPainting for a painting filter."""
        b = self.packets.protocol.binding
        packet_ids = (b.AddActorPacket.ID, b.AddPaintingPacket.ID) if actor_type == _PAINTING else b.AddActorPacket.ID

        def matches(raw):
            return (raw.packet_id == b.AddPaintingPacket.ID or actor_type is None
                    or self.packets.protocol.decode(raw).actor_type == actor_type)

        raw = await self.packets.wait_for_packet(
            packet_ids, match=matches, timeout=timeout
        )
        packet = self.packets.protocol.decode(raw)
        if raw.packet_id == b.AddPaintingPacket.ID:
            painting = _painting_snapshot(packet)
            return EntitySpawn(painting.entity_id, painting.runtime_id, _PAINTING, painting.position)
        return EntitySpawn(
            int(packet.entity_id),
            int(packet.runtime_id),
            packet.actor_type,
            (float(packet.pos.x), float(packet.pos.y), float(packet.pos.z)),
        )

    async def wait_for_item_pickup(
        self, *, item_runtime_id: int | None = None, collector_runtime_id: int | None = None,
        item_entity_id: int | None = None, collector_entity_id: int | None = None, timeout: float = 15,
        _item_instance: object | None = None,
    ) -> ItemPickup:
        """Observe the next matching server pickup in the current dimension."""
        self._require_live()
        b = self.packets.protocol.binding
        known_item_id = self._actor_entity_id(item_runtime_id)
        known_collector_id = self._actor_entity_id(collector_runtime_id)
        result = None

        def matches(raw):
            nonlocal result
            self._require_live()
            if raw.packet_id == b.ChangeDimensionPacket.ID:
                raise RuntimeError("dimension changed while waiting for a pickup")
            observed = self._observed_pickup(raw)
            if observed is None:
                return False
            pickup, instance = observed
            if _item_instance is not None and instance is not _item_instance:
                return False
            identities = (
                (item_runtime_id, pickup.item_runtime_id),
                (collector_runtime_id, pickup.collector_runtime_id),
                (item_entity_id, pickup.item_entity_id),
                (collector_entity_id, pickup.collector_entity_id),
                (known_item_id, pickup.item_entity_id),
                (known_collector_id, pickup.collector_entity_id),
            )
            if any(expected is not None and expected != actual for expected, actual in identities):
                return False
            result = pickup
            return True

        await self.packets.wait_for_packet(
            (b.TakeItemActorPacket.ID, b.ChangeDimensionPacket.ID, b.DisconnectPacket.ID),
            match=matches, timeout=timeout,
        )
        assert result is not None
        return result

    async def collect_drop(
        self, movement, runtime_id: int, player_id: int, *,
        ticks: int = 120, timeout: float = 15, blocks=None,
    ) -> EntityState:
        """Approach an observed drop and return only after this player's pickup."""
        drop = self.get(runtime_id)
        instance = self._actor_instance(runtime_id)
        if drop is None or drop.actor_type != "minecraft:item" or instance is None:
            raise ValueError("item actor is not currently observed")
        deadline = asyncio.timeout(timeout)
        pickup = asyncio.create_task(self.wait_for_item_pickup(
            item_runtime_id=runtime_id, collector_runtime_id=player_id,
            item_entity_id=drop.entity_id or None, timeout=timeout,
            _item_instance=instance,
        ))
        follow = None

        def resolve(selected):
            return self._bound_item(drop, instance) if selected == runtime_id else None

        try:
            async with deadline:
                # Register the packet waiter before sending any movement input.
                await asyncio.sleep(0)
                if blocks is None:
                    approach = movement.follow_entity(self, runtime_id, ticks=ticks, stop_distance=0.0, _resolve=resolve)
                else:
                    approach = movement._approach_drop(
                        blocks, self, drop, resolve, ticks=ticks, deadline=deadline.when(),
                    )
                follow = asyncio.create_task(approach)
                done, _ = await asyncio.wait((pickup, follow), return_when=asyncio.FIRST_COMPLETED)
                if follow in done:
                    await follow
                await pickup
                return drop
        finally:
            tasks = (pickup, follow) if follow is not None else (pickup,)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_for_item_drop(self, timeout: float = 15) -> EntityState:
        """Observe the next server-sent item spawn, not an inventory transfer."""
        self._require_live()
        b = self.packets.protocol.binding
        drop = None

        def capture(raw):
            nonlocal drop
            if self._closed or raw.packet_id == b.DisconnectPacket.ID:
                raise ConnectionError("entity session is closed")
            drop = self._item_state(self.packets.protocol.decode(raw))
            return True

        await self.packets.wait_for_packet(
            (b.AddItemActorPacket.ID, b.DisconnectPacket.ID), match=capture, timeout=timeout,
        )
        assert drop is not None
        return drop

    async def wait_for_removal(self, entity_id: int, *, timeout: float = 15) -> None:
        """Wait for a new RemoveActor referencing the server's unique actor ID."""
        b = self.packets.protocol.binding

        def matches(raw):
            return int(self.packets.protocol.decode(raw).entity_id) == entity_id

        await self.packets.wait_for_packet(
            b.RemoveActorPacket.ID, match=matches, timeout=timeout
        )

    def subscribe_entity_movement(self, callback: Callable[[EntityMovementUpdate], None]) -> Callable[[], None]:
        self._require_live()
        return self.packets._subscribe(self._movement_observers, _EntityObserver(None, callback))

    async def wait_for_movement(self, runtime_id: int, *, timeout: float = 15) -> EntityMovement:
        """Observe the next movement packet for a specific actor runtime ID.

        Delta packets omit unchanged axes; this is not a full position snapshot.
        """
        b = self.packets.protocol.binding

        def matches(raw):
            packet = self.packets.protocol.decode(raw)
            actor_id = (packet.player_id if raw.packet_id == b.MovePlayerPacket.ID
                        else packet.move_data.runtime_id)
            return int(actor_id) == runtime_id

        raw = await self.packets.wait_for_packet(
            (b.MoveActorAbsolutePacket.ID, b.MoveActorDeltaPacket.ID,
             b.MovePlayerPacket.ID),
            match=matches, timeout=timeout,
        )
        packet = self.packets.protocol.decode(raw)
        if raw.packet_id == b.MovePlayerPacket.ID:
            return EntityMovement(
                runtime_id, float(packet.pos.x), float(packet.pos.y), float(packet.pos.z),
            )
        move = packet.move_data
        if raw.packet_id == b.MoveActorAbsolutePacket.ID:
            return EntityMovement(
                runtime_id, float(move.pos.x), float(move.pos.y), float(move.pos.z)
            )
        return EntityMovement(
            runtime_id,
            float(move.new_position_x) if move.new_position_x is not None else None,
            float(move.new_position_y) if move.new_position_y is not None else None,
            float(move.new_position_z) if move.new_position_z is not None else None,
        )

    async def wait_for_movement_matching(
        self, runtime_id: int, predicate: Callable[[EntityState], bool], *,
        entity_id: int | None = None, timeout: float | None = 15,
    ) -> EntityState:
        """Match this visible actor's current snapshot, then snapshots after movement packets.

        Pass its unique ID to check the initial runtime ID binding. The wait ends
        if the actor leaves view or is replaced, the dimension changes, or the
        entity session closes. The synchronous predicate runs in packet order.
        """
        self._require_live()
        state = self.get(runtime_id)
        # A player with contradictory AddPlayer and roster identities has no view.
        if (state is None or runtime_id not in self._actor_instances
                or (state.entity_id and self._get_by_unique_id(state.entity_id) is not state)
                or (runtime_id in self._player_uuid_by_runtime and runtime_id not in self._player_views)
                or (entity_id is not None and entity_id != (state.entity_id or None))):
            raise LookupError("entity is not currently observed")

        future = asyncio.get_running_loop().create_future()
        waiters = self._movement_waiters.setdefault(runtime_id, {})
        waiters[future] = predicate
        try:
            # An eager task started by the predicate can close the session inline.
            if predicate(state) and not future.done():
                return state
            async with asyncio.timeout(timeout):
                result = await future
            if isinstance(result, BaseException):
                raise result
            return result
        finally:
            waiters.pop(future)
            if not waiters and self._movement_waiters.get(runtime_id) is waiters:
                del self._movement_waiters[runtime_id]

    def _observe_movement(
        self, state: EntityState, angles: tuple[float | None, float | None, float | None], on_ground: bool,
    ) -> None:
        # Predicates can change subscriptions while matching this packet.
        observers = ()
        if self._movement_observers and state.runtime_id != self._own_runtime_id:
            observers = tuple(self._movement_observers)
        self._match_movement_waiters(state)
        if not observers or self._closed:
            return

        event = EntityMovementUpdate(state, *angles, on_ground)
        self._notify_entity_observers(observers, event, state.runtime_id, "entity movement observer failed")

    def _match_movement_waiters(self, state: EntityState) -> None:
        waiters = self._movement_waiters.get(state.runtime_id)
        if not waiters:
            return
        for future, predicate in tuple(waiters.items()):
            if future.done():
                continue
            result: EntityState | BaseException
            try:
                if not predicate(state):
                    continue
            except (Exception, asyncio.CancelledError) as error:
                result = error
            else:
                result = state
            # Raise predicate errors in the awaiting coroutine, including StopIteration.
            if not future.done():
                future.set_result(result)

    def _end_movement_waiters(self, error_type, message, cause=None, *, runtime_id=None):
        targets = tuple(self._movement_waiters) if runtime_id is None else (runtime_id,)
        for target in targets:
            for future in self._movement_waiters.pop(target, ()):
                if not future.done():
                    error = error_type(message)
                    error.__cause__ = cause
                    future.set_result(error)
