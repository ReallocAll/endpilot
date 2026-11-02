from collections.abc import Awaitable, Callable, Iterable
from types import TracebackType
from typing import Literal, Self

from bedrock_protocol import BinaryView as BinaryView
from bedrock_protocol import Nbt as Nbt
from bedrock_protocol import NbtType as NbtType
from bedrock_protocol.v1001 import BlockPos as BlockPos
from bedrock_protocol.v1001 import PropertySyncData as PropertySyncData
from bedrock_protocol.v1001 import RedactableString as _RedactableString1001
from bedrock_protocol.v1001 import SyncedAttribute as SyncedAttribute
from bedrock_protocol.v2193 import RedactableString as _RedactableString2193

from ._projection import (
    ActorLink as ActorLink,
)
from ._projection import (
    ActorMetadata as ActorMetadata,
)
from ._projection import (
    ActorSignal as ActorSignal,
)
from ._projection import (
    ContainerOpen as ContainerOpen,
)
from ._projection import (
    FullContainerName as FullContainerName,
)
from ._projection import (
    ItemDescriptor as ItemDescriptor,
)
from ._projection import (
    ItemStackResponse as ItemStackResponse,
)
from ._projection import MetadataEntry1001 as MetadataEntry1001
from ._projection import MetadataEntry2193 as MetadataEntry2193
from ._projection import (
    ReadonlyMap,
    ReadonlySequence,
)
from ._projection import (
    RegistryItem as RegistryItem,
)
from ._projection import ResponseContainer1001 as ResponseContainer1001
from ._projection import ResponseContainer2193 as ResponseContainer2193
from ._projection import ResponseSlot1001 as ResponseSlot1001
from ._projection import ResponseSlot2193 as ResponseSlot2193
from ._projection import (
    UpdateTrade as UpdateTrade,
)
from .events import ALL_EVENTS as ALL_EVENTS
from .events import DEFAULT_EVENTS as DEFAULT_EVENTS
from .events import LEGACY_EVENTS as LEGACY_EVENTS
from .events import ActorEvent as ActorEvent
from .events import ActorMotionEvent as ActorMotionEvent
from .events import ActorSignalEvent as ActorSignalEvent
from .events import ChunkEvent as ChunkEvent
from .events import ContainerEvent as ContainerEvent
from .events import DeathEvent as DeathEvent
from .events import DecodeErrorEvent as DecodeErrorEvent
from .events import DimensionEvent as DimensionEvent
from .events import Event as Event
from .events import EventKind as EventKind
from .events import HealthEvent as HealthEvent
from .events import InventoryEvent as InventoryEvent
from .events import InventorySlotEvent as InventorySlotEvent
from .events import ItemRegistryEvent as ItemRegistryEvent
from .events import ItemRequestEvent as ItemRequestEvent
from .events import LatencyEvent as LatencyEvent
from .events import PacketDecodedEvent as PacketDecodedEvent
from .events import PositionEvent as PositionEvent
from .events import RespawnEvent as RespawnEvent
from .events import StateEvent as StateEvent
from .events import TakeItemEvent as TakeItemEvent
from .events import TickEvent as TickEvent
from .events import event_mask as event_mask
from .observations import ActorSnapshot as ActorSnapshot
from .observations import ContainerSnapshot as ContainerSnapshot
from .observations import InventorySnapshot as InventorySnapshot
from .observations import ItemRegistrySnapshot as ItemRegistrySnapshot
from .observations import SlotObservation as SlotObservation
from .observations import Snapshot as Snapshot
from .observations import Statistics as Statistics
from .observations import TradeOffers as TradeOffers
from .operations import ItemCancellation as ItemCancellation
from .operations import ItemOperation as ItemOperation
from .operations import ItemOperationPhase as ItemOperationPhase
from .operations import ItemOperationResult as ItemOperationResult
from .operations import ItemRequestOutcome as ItemRequestOutcome
from .types import BotOptions as BotOptions
from .types import BotState as BotState
from .types import BotStatus as BotStatus
from .types import BotToken as BotToken
from .types import EntityRef as EntityRef
from .types import Input as Input
from .types import InventoryArea as InventoryArea
from .types import InventorySlot as InventorySlot
from .types import ItemCount as ItemCount
from .types import ItemRequestKey as ItemRequestKey
from .types import ObservedEntity as ObservedEntity
from .types import ServerDisconnectReason as ServerDisconnectReason
from .types import SlotState as SlotState
from .types import TradeItem as TradeItem
from .types import TradeOffer as TradeOffer
from .types import TradeOfferRef as TradeOfferRef
from .types import TradeWindowObservation as TradeWindowObservation
from .types import Vec2 as Vec2
from .types import Vec3 as Vec3
from .types import WindowRef as WindowRef

__version__: str

class Runtime:
    def __init__(
        self, callback: Callable[[Bot, Event], object] | None = None, *, events: int = DEFAULT_EVENTS
    ) -> None: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> Literal[False]: ...
    def add_bot(self, options: BotOptions) -> Bot: ...
    def aclose(self) -> Awaitable[None]: ...
    async def wait_closed(self) -> None: ...
    def request_stop(self) -> None: ...
    def get_event_interests(self) -> int: ...
    def set_event_interests(self, mask: int) -> None: ...
    def subscribe(self, callback: Callable[[Bot, Event], object], *, events: int = DEFAULT_EVENTS) -> Callable[[], None]: ...

class Bot:
    def get_status(self) -> BotStatus: ...
    def wait_ready(self, timeout: float | None = None) -> Awaitable[None]: ...
    def disconnect(self, reason: str = "") -> Awaitable[None]: ...
    def reconnect(self) -> Awaitable[None]: ...
    def get_event_interests(self) -> int: ...
    def set_event_interests(self, mask: int) -> None: ...
    def capture_options(self) -> BotOptions: ...
    def capture_statistics(self) -> Statistics: ...
    def capture_snapshot(self) -> Snapshot: ...
    def capture_actor(self, runtime_id: int) -> ActorSnapshot | None: ...
    def capture_actors(self) -> Actors: ...
    def capture_inventory(self, inventory_id: int) -> InventorySnapshot | None: ...
    def capture_container(self) -> ContainerSnapshot | None: ...
    def capture_item_registry(self) -> ItemRegistrySnapshot: ...
    def get_observed_entities(self) -> list[ObservedEntity]: ...
    def player_slot(self, index: int) -> InventorySlot: ...
    def trade_input(self, window: WindowRef, index: int) -> InventorySlot: ...
    def get_item(self, slot: InventorySlot) -> SlotObservation: ...
    def get_player_inventory(self) -> list[SlotObservation]: ...
    def count_player_item(self, identifier: str) -> ItemCount: ...
    def get_trade_window(self) -> TradeWindowObservation | None: ...
    def get_trade_offers(self, window: WindowRef) -> TradeOffers | None: ...
    def request_move_item(self, source: InventorySlot, destination: InventorySlot, count: int) -> ItemRequestKey: ...
    def request_trade(self, offer: TradeOfferRef, destination: InventorySlot) -> ItemRequestKey: ...
    def begin_move_item(self, source: InventorySlot, destination: InventorySlot, count: int) -> ItemOperation: ...
    def begin_trade(self, offer: TradeOfferRef, destination: InventorySlot) -> ItemOperation: ...
    def send_packet(self, packet: object) -> None: ...
    def set_input(self, input: Input) -> None: ...
    def set_look(self, pitch: float, yaw: float) -> None: ...
    def clear_look(self) -> None: ...
    def select_hotbar(self, slot: int) -> None: ...
    def interact_actor(self, entity: EntityRef, hit_position: Vec3 = ...) -> None: ...
    def attack_actor(self, entity: EntityRef, hit_position: Vec3 = ...) -> None: ...
    def close_container(self, window: WindowRef) -> None: ...
    def cancel_item_operation(self, operation: ItemOperation) -> ItemCancellation: ...
    def request_chunk_radius(self, radius: int) -> None: ...
    def report_blob_status(self, missing: Iterable[int], present: Iterable[int] = ()) -> None: ...
    def request_sub_chunks(self, dimension: int, chunk_x: int, chunk_z: int, heights: Iterable[int]) -> None: ...
    def respawn(self, *, retry: bool = False) -> None: ...
    def probe_latency(self) -> None: ...


type IntProperty = PropertySyncData.PropertySyncIntEntry
type FloatProperty = PropertySyncData.PropertySyncFloatEntry
RedactableString1001 = _RedactableString1001
RedactableString2193 = _RedactableString2193
type PacketCounts = ReadonlySequence[int]
type BlobIds = ReadonlySequence[int]
type SlotObservations = ReadonlySequence[SlotObservation]
type IntProperties = ReadonlySequence[IntProperty]
type FloatProperties = ReadonlySequence[FloatProperty]
type Attributes = ReadonlySequence[SyncedAttribute]
type ActorLinks = ReadonlySequence[ActorLink]
type RegistryItems = ReadonlySequence[RegistryItem]
type MetadataEntries1001 = ReadonlySequence[MetadataEntry1001]
type MetadataEntries2193 = ReadonlySequence[MetadataEntry2193]
type ResponseSlots1001 = ReadonlySequence[ResponseSlot1001]
type ResponseSlots2193 = ReadonlySequence[ResponseSlot2193]
type ResponseContainers1001 = ReadonlySequence[ResponseContainer1001]
type ResponseContainers2193 = ReadonlySequence[ResponseContainer2193]
type Actors = ReadonlyMap[int, ActorSnapshot]
type Inventories = ReadonlyMap[int, InventorySnapshot]
type InventorySlots = ReadonlyMap[int, ItemDescriptor]
_Event = Event
