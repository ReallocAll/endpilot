"""Public player entry point; game state delegates to the relevant feature."""

import asyncio
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from functools import partial
from inspect import isasyncgenfunction, isawaitable, iscoroutinefunction
from math import inf
from re import Pattern, fullmatch
from typing import Any, Literal
from uuid import UUID
from weakref import ref

from bedrock_protocol import GameType, GeneratorType, nbt
from bedrock_protocol.v712 import CommandOutputPacket

from ._join_address import _parse_join_address
from .anvil import AnvilPreview
from .authentication import (
    BedrockCredentialProvider, DEFAULT_LOCALE, OfflineIdentity, _OnlineIdentity, acquire_online_identity,
)
from .beds import BedObservation, Beds, PlayerRespawnPoint, _BedEvent
from .block_raycast import BlockRaycastResult, MiningFaceResult
from .blocks import (
    Blocks, BlockChange, BlockEntityUpdate, BlockMatch, BlockObservation, BlockSearchResult, BlockStateObservation,
    BlockUpdate, ColumnCoverage, ColumnCoverageEvent, DimensionBounds, MiningEvent, MiningTarget,
)
from .books import BookEdit, BookSnapshot, Books
from .boss_bars import BossBar, BossBarEvent, BossBarEventKind, BossBars
from .chat import Chat, ChatMatch, ChatMessage, ChatPatterns, DisplayMatch, TextEvent, TextPacketType
from .client_clock import ClientClock
from .collection import BlockCollection, BlockCollectionBatch, CountedBlockCollection, MixedCollectionBatch, _BlockCollector
from .collection_storage import CollectionStorage
from .commands import CommandCatalog, CommandCompletion, Commands
from .compression import PacketConnection
from .containers import BrewingProgress, Containers, ContainerSlotChange, ContainerWindow, FurnaceProgress
from .creative_inventory import CreativeCatalog, CreativeClearResult, CreativeEntry, CreativeInventory
from .dimension_transition import DimensionTransition
from .enchanting import Enchanting, EnchantOption
from .entity_registry import EntityRegistrySnapshot
from .entities import (
    Entities, EntityActorEvent, EntityAttributeUpdate, EntityCriticalEffect, EntityCriticalEffectKind,
    EntityCursorResult, EntityEffectUpdate, EntityHealthEvent, EntityHealthEventKind, EntityLifecycleEvent,
    EntityMovement, EntityMovementUpdate, EntitySpawn, EntityState, EntitySwing, EntityVisibleEffectUpdate, ItemPickup,
    PlayerEquipmentSnapshot, PlayerListChange, PlayerListEntry, ShieldBlockEvent, VehicleLink, VehicleLinkChange,
)
from .expression import skin_packet
from .fishing import Fishing, FishingResult
from .forms import Forms
from .game_hp_estimate import _estimate_sourceless_explosion_damage
from .harvest import HarvestTool, HarvestToolChest, HarvestTools
from .inventory import CursorObservation, HeldItemSnapshot, Inventory, InventorySlotChange
from .item_registry import ItemRegistrySnapshot
from .item_use import ItemConsumption, ItemCooldownEvent, ItemUse
from .item_user_data import read_item_user_data
from .login import (
    JoinSession, SessionEnd, _PendingOnlineJoin, _SessionEndState, _close_after_failed_join, _validate_join,
)
from .movement import BlockApproach, FlightResult, ForwardInput, Movement, MovementState
from .navigation import NavigationEvent, NavigationGoal, NavigationPlan, NavigationResult, NavigationRules
from .placement import (
    BoatPlacementResult, EntityPlacementResult, PlacementResult, observe_entity_placement, observe_placement,
    WaterBucketFillResult, observe_water_bucket_fill,
)
from .player_state import DeathEvent, GlideBoostEvent, PlayerAbilities, PlayerEffect, PlayerState, PlayerVitals
from .qa import PacketTimeline
from .qa_terrain import _TerrainObserver
from .recipes import Recipe, Recipes
from .recoverable_error import RecoverableError
from .resource_packs import PackDecisionHandler, ResourcePackIndex
from .scoreboard import Scoreboard, ScoreboardObserver, ScoreboardSnapshot
from .signs import SignEditor, SignObservation, SignSide, Signs
from .skin import PlayerSkinSnapshot, Skin
from .titles import TitleEvent, TitleKind, Titles
from .trading import TradeManyResult, TradeOffers, Trading
from .protocol import DEFAULT_PROTOCOL, Protocol, RawPacket
from .world_environment import Weather, WorldEnvironment, WorldRule, WorldSpawn, WorldTime
from .world_feedback import BlockEvent, ParticleEvent, SoundEvent, SoundKind, WorldFeedback


def _client_option(enum_type, name):
    if not isinstance(name, str) or name == "COUNT" or name not in enum_type.__members__:
        raise ValueError(f"{name!r} is not a {enum_type.__name__} option")
    return enum_type[name]


def _validate_plugin(plugin: object) -> None:
    if not callable(plugin):
        raise TypeError("plugin must be callable")
    while isinstance(plugin, partial):
        call = type(plugin).__call__
        if call is not partial.__call__:
            plugin = call
            break
        plugin = plugin.func
    call = getattr(type(plugin), "__call__", None)
    if any(iscoroutinefunction(target) or isasyncgenfunction(target) for target in (plugin, call)):
        raise TypeError("plugin must be synchronous")


class Bot:
    def __init__(self, name: str = "EndPilot", protocol: int = DEFAULT_PROTOCOL, *,
                 input_source: Callable[[], ForwardInput] | None = None,
                 on_input_sent: Callable[[int, int], None] | None = None,
                 auto_respawn: bool = False, skin: Skin | None = None):
        """Create a bot with an optional classic skin for each login.

        Optionally supply forward input for the regular 50 ms movement clock.
        The synchronous source runs after the server provides a position. The
        clock keeps one monotonic schedule for the session and skips missed
        frames. After each successful scheduled send, the synchronous observer
        receives the client tick and monotonic nanoseconds. Explicit player
        actions use the same lock.
        """
        if skin is not None and not isinstance(skin, Skin):
            raise TypeError("skin must be a Skin")
        self._skin = skin
        self._offline_identity = OfflineIdentity(name)
        self.protocol = Protocol(protocol)
        self._auto_respawn = auto_respawn
        self._plugins: dict[int, Callable[["Bot"], object]] = {}
        self._spawn_observers: dict[object, Callable[[Literal["initial", "respawn"]], None]] = {}
        self._input_source = input_source
        self._on_input_sent = on_input_sent
        self._clock: ClientClock | None = None
        self._online_join: _PendingOnlineJoin | None = None
        self._session: JoinSession | None = None
        self._error_observers: dict[object, Callable[[RecoverableError], None]] = {}
        self._session_end_state: _SessionEndState | None = None
        self._session_end_observers: dict[object, Callable[[SessionEnd], None]] = {}
        self._movement: Movement | None = None
        self._inventory: Inventory | None = None
        self._books: Books | None = None
        self._item_use: ItemUse | None = None
        self._containers: Containers | None = None
        self._creative: CreativeInventory | None = None
        self._enchanting: Enchanting | None = None
        self._blocks: Blocks | None = None
        self._placement_completion_observers: dict[object, Callable[[PlacementResult], None]] = {}
        self._terrain: _TerrainObserver | None = None
        self._beds: Beds | None = None
        self._block_collector: _BlockCollector | None = None
        self._boss_bars: BossBars | None = None
        self._forms: Forms | None = None
        self._harvest_tools: HarvestTools | None = None
        self._signs: Signs | None = None
        self._chat: Chat | None = None
        self._titles: Titles | None = None
        self._commands: Commands | None = None
        self._entities: Entities | None = None
        self._fishing: Fishing | None = None
        self._player_state: PlayerState | None = None
        self._trading: Trading | None = None
        self._recipes: Recipes | None = None
        self._scoreboard: Scoreboard | None = None
        self._environment: WorldEnvironment | None = None
        self._feedback: WorldFeedback | None = None

    def load_plugin(self, plugin: Callable[["Bot"], object]) -> None:
        """Run a synchronous plugin on this Bot once, including before joining.

        Registration follows the callable's identity and survives reconnects.
        An exception or awaitable result removes the registration so the caller
        can retry. The plugin is responsible for cleanup of its own side effects.
        A returned awaitable stays under its creator's management.
        """
        _validate_plugin(plugin)
        self._load_plugin(plugin)

    def load_plugins(self, plugins: Iterable[Callable[["Bot"], object]]) -> None:
        """Validate the entire iterable, then load each plugin in iteration order.

        If installation fails, earlier successful registrations remain installed.
        """
        plugins = tuple(plugins)
        for plugin in plugins:
            _validate_plugin(plugin)
        for plugin in plugins:
            self._load_plugin(plugin)

    def has_plugin(self, plugin: object) -> bool:
        """Check whether this exact callable is installing or already installed."""
        registered = self._plugins.get(id(plugin))
        return registered is not None and registered is plugin

    def _load_plugin(self, plugin: Callable[["Bot"], object]) -> None:
        if self.has_plugin(plugin):
            return
        key = id(plugin)
        self._plugins[key] = plugin
        try:
            if isawaitable(plugin(self)):
                raise TypeError("plugin returned an awaitable")
        except BaseException:
            del self._plugins[key]
            raise

    @property
    def identity(self) -> OfflineIdentity | _OnlineIdentity:
        """Return the joined online identity or the configured offline identity."""
        session = self._session
        if session is not None and isinstance(session.identity, _OnlineIdentity):
            return session.identity
        return self._offline_identity

    @identity.setter
    def identity(self, identity: OfflineIdentity) -> None:
        self._offline_identity = identity

    async def join_nethernet(
        self, url: str, *, timeout: float = 30, server_key_sha256: str | None = None,
        timeline: PacketTimeline | None = None, chunk_radius: int = 8, locale: str = DEFAULT_LOCALE,
        load_screen: bool = True,
        retain_initial_chunks: bool = False, load_announced_terrain: bool = False,
        on_resource_packs: PackDecisionHandler | None = None,
        credential_provider: BedrockCredentialProvider | None = None,
    ) -> None:
        """Join a server with optional online credentials.

        The provider completes account authorization before the network timeout starts.
        Subscribe immediately after joining to observe retained raw chunks. Set
        load_announced_terrain to request missing sections from server-announced columns.
        """
        await self._join_session(
            JoinSession.connect_nethernet,
            url, timeout=timeout, credential_provider=credential_provider,
            server_key_sha256=server_key_sha256, timeline=timeline,
            chunk_radius=chunk_radius, locale=locale, load_screen=load_screen,
            retain_initial_chunks=retain_initial_chunks,
            load_announced_terrain=load_announced_terrain,
            on_resource_packs=on_resource_packs,
        )

    async def join_raknet(
        self, host: str, port: int = 19132, *, timeout: float = 30, timeline: PacketTimeline | None = None,
        chunk_radius: int = 8, locale: str = DEFAULT_LOCALE, retain_initial_chunks: bool = False,
        load_announced_terrain: bool = False,
        on_resource_packs: PackDecisionHandler | None = None,
        credential_provider: BedrockCredentialProvider | None = None,
    ) -> None:
        """Join a server with optional online credentials.

        The provider completes account authorization before the network timeout starts.
        """
        await self._join_session(
            JoinSession.connect_raknet, host, port, timeout=timeout, credential_provider=credential_provider,
            timeline=timeline,
            chunk_radius=chunk_radius, locale=locale, retain_initial_chunks=retain_initial_chunks,
            load_announced_terrain=load_announced_terrain,
            on_resource_packs=on_resource_packs,
        )

    async def join_address(
        self, address: str, *, timeout: float = 30, server_key_sha256: str | None = None,
        timeline: PacketTimeline | None = None, chunk_radius: int = 8, locale: str = DEFAULT_LOCALE,
        retain_initial_chunks: bool = False, load_announced_terrain: bool = False,
        on_resource_packs: PackDecisionHandler | None = None,
        credential_provider: BedrockCredentialProvider | None = None,
    ) -> None:
        """Join an explicit server URL or select a transport for a bare host:port.

        Bare addresses try NetherNet over HTTPS, then HTTP, then RakNet, with a
        timeout for each candidate. A NetherNet server key pin limits selection
        to NetherNet. Once a transport opens, the bot uses it to log in and
        spawn. Account authorization precedes the first connection timeout.
        """
        target = _parse_join_address(address)
        if server_key_sha256 is not None and (
            target.scheme == "raknet" or not isinstance(server_key_sha256, str)
            or fullmatch(r"[0-9a-f]{64}", server_key_sha256) is None
        ):
            raise ValueError("server_key_sha256 requires a NetherNet candidate and a lowercase SHA-256 digest")
        await self._join_session(
            JoinSession._connect_address, target, timeout=timeout, credential_provider=credential_provider,
            server_key_sha256=server_key_sha256, timeline=timeline, chunk_radius=chunk_radius, locale=locale,
            retain_initial_chunks=retain_initial_chunks, load_announced_terrain=load_announced_terrain,
            on_resource_packs=on_resource_packs, _track_join=True,
        )

    @property
    def reported_server_version(self) -> str | None:
        """Return the server version string from StartGame in the active joined session.

        An empty string or the absence of an active joined session yields None.
        """
        session = self._session
        state = self._get_session_end_state()
        if session is None or state is None or state.result is not None:
            return None
        return session.reported_server_version

    @property
    def resource_pack_index(self) -> ResourcePackIndex | None:
        """Return the resource-pack index of the joined session, or None while offline."""
        session = self._session
        return None if session is None else session.resource_pack_index

    @property
    def item_registry(self) -> ItemRegistrySnapshot | None:
        """Return the active session's observed item registry, or None when unavailable."""
        session = self._session
        return None if session is None else session.item_registry

    @property
    def entity_registry(self) -> EntityRegistrySnapshot | None:
        """Return the active session's latest entity type advertisement, if received."""
        session = self._session
        return None if session is None or self.session_end is not None else session.entity_registry

    async def _join_session(
        self, connect, *endpoint, timeout: float, credential_provider: BedrockCredentialProvider | None,
        chunk_radius: int, locale: str, load_announced_terrain: bool = False, _track_join: bool = False, **kwargs,
    ) -> None:
        if self._clock is not None:
            raise RuntimeError("player is already connecting or connected")
        _validate_join(self.protocol, chunk_radius, locale, timeout, online=credential_provider is not None)
        self._session_end_state = None
        clock = ClientClock()
        self._clock = clock
        start_complete = asyncio.Event()

        async def connect_and_start() -> None:
            identity = self._offline_identity
            if credential_provider is not None:
                identity = await acquire_online_identity(credential_provider)
                if self._clock is not clock:
                    raise ConnectionError("player closed while joining")
            if self._skin is not None:
                identity = identity._with_skin(self._skin)
            session = await connect(
                *endpoint, identity, self.protocol, timeout, clock=clock, chunk_radius=chunk_radius, locale=locale,
                **kwargs,
            )
            if self._clock is not clock:
                error = ConnectionError("player closed while joining")
                await _close_after_failed_join(session, error, "session cleanup after invalidated join")
                raise error
            try:
                session.packets._defer_receiving_until(start_complete)
                self._start_session(session, load_announced_terrain=load_announced_terrain)
            except BaseException as error:
                await _close_after_failed_join(self, error, "session cleanup after failed join")
                raise
            if self._session is not session:
                raise ConnectionError("player closed while joining")

        def finish_join() -> None:
            session = self._session
            self._session_end_state = session._end_state
            if session.spawned:
                self._player_state._start_spawn_observation(self._publish_spawn)
            session._set_session_end_observer(self._publish_session_end)
            start_complete.set()

        try:
            if credential_provider is None and not _track_join:
                await connect_and_start()
                finish_join()
                return
            pending = self._online_join = _PendingOnlineJoin()
            kwargs["_online_join"] = pending
            task = pending.task = asyncio.create_task(connect_and_start())
            try:
                # Waiting independently lets close and caller cancellation share one cancellation.
                await asyncio.wait((task,))
            except asyncio.CancelledError as error:
                clock.stop()
                if not task.cancelling():
                    task.cancel()
                await asyncio.wait((task,))
                try:
                    task.result()
                except BaseException as stopped:
                    for note in getattr(stopped, "__notes__", ()):
                        error.add_note(note)
                if self._clock is clock:
                    await _close_after_failed_join(self, error, "session cleanup after cancelled join")
                raise
            finally:
                if self._online_join is pending:
                    self._online_join = None
            task.result()
            if self._clock is not clock:
                raise ConnectionError("player closed while joining")
            finish_join()
        except BaseException:
            if self._clock is clock:
                self._clock = None
            clock.stop()
            raise

    def _start_terrain_loading(self) -> None:
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        if self._terrain is None:
            self._terrain = _TerrainObserver(self._blocks)

    def _start_session(self, session: JoinSession, *, load_announced_terrain: bool = False) -> None:
        self._clock = session.clock
        self._session = session
        identity = session.identity
        xuid = identity._xuid if isinstance(identity, _OnlineIdentity) else ""
        session._observe_session_end()
        session.observe_chunk_radius()

        bot_ref, session_ref = ref(self), ref(session)

        def publish_error(error: RecoverableError) -> None:
            bot, source_session = bot_ref(), session_ref()
            if bot is not None and source_session is not None:
                bot._publish_error(source_session, error)

        self._entities = Entities(
            session.packets, item_names=session.item_names if session.item_registry is None else None,
            own_runtime_id=session.player_id, own_entity_id=session.player_unique_id,
        )
        # Capture the player state assembled later for this session.
        player_state: PlayerState | None = None
        self._movement = Movement(
            session.packets, session.player_id,
            input_source=self._input_source, on_input_sent=self._on_input_sent, clock=session.clock,
            movement_speed=session.movement_speed, entities=self._entities, _dimensions=session._dimensions,
            _gap_jump_allowed=lambda: player_state is not None and player_state._gap_jump_allowed(),
            _navigation_air_supply=lambda: None if player_state is None else player_state.air_supply,
        )
        self._inventory = Inventory(session.packets, session.player_id)
        for raw in session.inventory_packets:
            self._inventory._on_packet(raw)
        session.inventory_packets.clear()
        self._books = Books(self._inventory, session.item_names, identity.name, _xuid=xuid)
        self._item_use = ItemUse(session.packets, self._movement, self._inventory, session.player_id)
        self._blocks = Blocks(
            session.packets, self._movement, self._inventory,
            hashed_runtime_ids=session.block_network_ids_are_hashes,
            custom_blocks=session.has_custom_blocks,
            dimension_id=session.dimension_id,
            player_game_type=session.player_game_type, player_unique_id=session.player_unique_id,
            _publish_error=publish_error,
        )
        self._containers = Containers(
            session.packets, self._movement, self._inventory, entities=self._entities, blocks=self._blocks,
            item_names=session.item_names,
        )
        def observe_game_mode(mode):
            if player_state is not None:
                player_state._observe_game_mode(mode)

        creative = self._creative = CreativeInventory(
            session.packets, self._inventory, self._containers, session.player_unique_id,
            session.item_names, session.creative_packets, on_game_mode=observe_game_mode,
        )
        session.creative_packets.clear()
        self._enchanting = Enchanting(session.packets, self._containers, self._inventory, session.item_names)
        self._beds = Beds(session.packets, self._blocks, self._movement, self._inventory,
                          session.player_id, session.bed_packets)
        session.bed_packets.clear()
        self._boss_bars = BossBars(session.packets, session.boss_bar_packets)
        session.boss_bar_packets.clear()
        self._forms = Forms(session.packets, session.form_packets)
        session.form_packets.clear()
        self._harvest_tools = HarvestTools(
            self._blocks, self._inventory, self._containers, session.item_names,
        )
        self._signs = Signs(session.packets, self._blocks, self._movement, self._inventory, session.player_id)
        self._chat = Chat(session.packets, identity.name, resource_pack_index=session.resource_pack_index, _xuid=xuid)
        self._titles = Titles(session.packets, session._end_state, _publish_error=publish_error)
        self._commands = Commands(
            session.packets, session.identity.uuid, session.player_unique_id,
            initial_catalog_packets=session.command_catalog_packets,
            initial_commands_enabled=session.commands_enabled,
        )
        session.command_catalog_packets.clear()
        self._commands.start()
        self._block_collector = _BlockCollector(
            self._blocks, self._movement, self._entities, self._harvest_tools,
        )

        def replay_game_mode(packet):
            creative._apply(packet)
            return creative.game_mode

        player_state = self._player_state = PlayerState(
            session.packets, session.player_id, unique_id=session.player_unique_id,
            auto_respawn=self._auto_respawn, initial_packets=session.player_state_packets,
            replay_game_mode=replay_game_mode,
        )
        for raw in session.movement_packets:
            self._movement._on_packet(raw, startup=True)
        player_state._replay_collision_size(session.movement_packets)
        session.movement_packets.clear()
        session.player_state_packets.clear()
        self._environment = WorldEnvironment(session.packets, session.environment_packets)
        session.environment_packets.clear()
        self._feedback = WorldFeedback(session.packets, self._blocks, session._end_state)
        self._recipes = Recipes(
            session.packets, session.item_names, session.crafting_data_packets,
            inventory=self._inventory, containers=self._containers, environment=self._environment,
            vanilla_recipe_tags=session.vanilla_recipe_tags,
        )
        session.crafting_data_packets.clear()
        self._scoreboard = Scoreboard(session.packets, session.scoreboard_packets)
        session.scoreboard_packets.clear()

        def block_id_mode() -> Literal["hashed", "sequential"] | None:
            if not session.spawned or session.packets.protocol.number != 2225:
                return None
            if session.block_network_ids_are_hashes:
                return "hashed"
            # Login treats an unverified sequential palette as custom.
            return None if session.has_custom_blocks else "sequential"

        self._trading = Trading(
            session.packets, self._entities, self._movement, self._inventory, self._containers,
            item_names=session.item_names, block_id_mode=block_id_mode,
        )
        self._entities.start()
        for raw in session.entity_packets:
            self._entities._on_packet(raw, startup=True)
        session.entity_packets.clear()
        self._fishing = Fishing(self._entities, self._item_use, session.item_names, session.player_unique_id)
        self._blocks.start(
            observe_block_entity=self._signs._on_block_entity,
            replayed_dimension_packets=session.replayed_dimension_packets,
        )
        session.replayed_dimension_packets.clear()
        self._signs.start()
        if load_announced_terrain:
            self._start_terrain_loading()
        # Prime the block cache in wire order without replaying raw history.
        terrain_observers = (self._blocks._on_packet,)
        for raw in session.terrain_packets:
            PacketConnection._notify(terrain_observers, raw)
        session.terrain_packets.clear()
        if self._session is not session:
            return  # The caller reports the invalidated join.
        session.packets.subscribe_reader_end(session._dimensions.close)
        self._movement.start(_before_dispatch=session._receive_dimension_packet)

    @property
    def active_dimension_change(self) -> DimensionTransition | None:
        """Return the local player's current server-directed dimension transition, if any."""
        if self._session is None:
            raise RuntimeError("player has not joined")
        return self._session._dimensions.active

    @property
    def is_changing_dimension(self) -> bool:
        """Whether the local player is in a server-directed dimension transition."""
        return self.active_dimension_change is not None

    async def wait_for_dimension_change(self, *, timeout: float | None = 15) -> DimensionTransition:
        """Wait for the current or next transition to complete its loading exchange."""
        if self._session is None:
            raise RuntimeError("player has not joined")
        return await self._session._dimensions.wait(timeout)

    @property
    def boss_bars(self) -> tuple[BossBar, ...]:
        """Return immutable snapshots of the boss bars visible to this bot."""
        if self._boss_bars is None:
            raise RuntimeError("player has not joined")
        return self._boss_bars.snapshot()

    async def wait_for_boss_bar(
        self, *, kind: BossBarEventKind | None = None, boss_id: int | None = None,
        timeout: float = 15,
    ) -> BossBarEvent:
        """Wait for the next matching boss bar change dispatched after this wait starts."""
        if self._boss_bars is None:
            raise RuntimeError("player has not joined")
        return await self._boss_bars.wait_for_event(kind=kind, boss_id=boss_id, timeout=timeout)

    def get_pending_form(self, form_id: int | RawPacket | None = None) -> dict | None:
        """Return an independent snapshot by ID, by the received raw request, or for the latest form."""
        if self._forms is None:
            raise RuntimeError("player has not joined")
        return self._forms.get_pending(form_id)

    async def answer_form(self, response: Any, form_id: int | RawPacket | None = None) -> None:
        """Submit a typed answer by ID, by the received raw request, or to the latest pending form."""
        if self._forms is None:
            raise RuntimeError("player has not joined")
        await self._forms.answer(response, form_id)

    async def close_form(self, reason: str = "USER_CLOSED", form_id: int | RawPacket | None = None) -> None:
        """Submit a cancellation by ID, by the received raw request, or to the latest pending form."""
        if self._forms is None:
            raise RuntimeError("player has not joined")
        await self._forms.dismiss(reason, form_id)

    @property
    def scoreboard(self) -> ScoreboardSnapshot:
        """Take an independent snapshot of this session's observed scoreboard."""
        if self._scoreboard is None:
            raise RuntimeError("player has not joined")
        return self._scoreboard.snapshot()

    def subscribe_scoreboard(self, callback: ScoreboardObserver) -> Callable[[], None]:
        """Observe future scoreboard changes in this session and return an unsubscribe function.

        After applying a packet, call the synchronous callback with its ordered
        tuple of changes. The changes retain intermediate values within the packet.
        A batch goes to the observers registered when its delivery begins. Any
        subscription changes made by a callback take effect with the next batch.
        """
        if self._scoreboard is None:
            raise RuntimeError("player has not joined")
        return self._scoreboard.subscribe(callback)

    @property
    def world_time(self) -> WorldTime:
        """Return the latest server-observed world time and named clocks."""
        if self._environment is None:
            raise RuntimeError("player has not joined")
        return self._environment.time()

    @property
    def weather(self) -> Weather:
        """Return the weather state announced by the server for this world."""
        if self._environment is None:
            raise RuntimeError("player has not joined")
        return self._environment.weather

    @property
    def world_rules(self) -> tuple[WorldRule, ...]:
        """Return the last announced values and edit flags for known world rules."""
        if self._environment is None:
            raise RuntimeError("player has not joined")
        return self._environment.rules()

    @property
    def world_difficulty(self) -> int | None:
        """Return this session's latest advertised difficulty, if known."""
        environment = self._environment
        return None if environment is None else environment.difficulty

    @property
    def world_hardcore(self) -> bool | None:
        """Return this session's advertised hardcore setting, if known."""
        environment = self._environment
        return None if environment is None else environment.hardcore

    @property
    def advertised_world_generator(self) -> GeneratorType | int | None:
        """Return the generator declared at login, or None without an active session."""
        environment = self._environment
        if environment is None or self.session_end is not None:
            return None
        return environment.advertised_generator

    @property
    def world_spawn(self) -> WorldSpawn | None:
        """Return this session's latest public spawn advertisement, if known."""
        environment = self._environment
        return None if environment is None else environment.world_spawn

    def subscribe_world_spawn_changes(self, callback: Callable[[WorldSpawn], None]) -> Callable[[], None]:
        """Observe public spawn changes advertised to the currently joined session.

        A synchronous callback receives the new immutable WorldSpawn after world_spawn
        is updated by a WORLD_SPAWN packet. The initial value is available in world_spawn.
        The returned function unsubscribes and can be called repeatedly.
        """
        environment = self._environment
        state = self._get_session_end_state()
        if environment is None or state is None:
            raise RuntimeError("player has not joined")
        if state.result is not None:
            raise ConnectionError("world spawn subscription stream is closed")
        return environment.subscribe_world_spawn_changes(callback)

    async def wait_for_world_time_update(self, *, timeout: float = 15) -> WorldTime:
        """Wait for the next packet that changes the observed time snapshot."""
        if self._environment is None:
            raise RuntimeError("player has not joined")
        return await self._environment.wait_for_time_update(timeout)

    async def wait_for_weather_update(self, *, timeout: float = 15) -> Weather:
        """Wait for the next packet that changes the observed weather snapshot."""
        if self._environment is None:
            raise RuntimeError("player has not joined")
        return await self._environment.wait_for_weather_update(timeout)

    async def wait_for_world_rules_update(self, *, timeout: float = 15) -> tuple[WorldRule, ...]:
        """Wait for the next change in observed world rules."""
        if self._environment is None:
            raise RuntimeError("player has not joined")
        return await self._environment.wait_for_rules_update(timeout)

    async def wait_for_sound(
        self, name: str | None = None, *, kind: SoundKind | None = None, timeout: float = 15,
    ) -> SoundEvent:
        """Wait for the next matching server sound packet."""
        if self._feedback is None:
            raise RuntimeError("player has not joined")
        return await self._feedback.wait_for_sound(name, kind, timeout)

    def subscribe_sound(
        self, callback: Callable[[SoundEvent], None], *, name: str | None = None, kind: SoundKind | None = None,
    ) -> Callable[[], None]:
        """Call a synchronous observer for each future matching sound and return an unsubscribe function."""
        if self._feedback is None:
            raise RuntimeError("player has not joined")
        return self._feedback.subscribe_sound(callback, name=name, kind=kind)

    async def wait_for_particle(self, name: str | None = None, *, timeout: float = 15) -> ParticleEvent:
        """Wait for the next matching named particle packet."""
        if self._feedback is None:
            raise RuntimeError("player has not joined")
        return await self._feedback.wait_for_particle(name, timeout)

    def subscribe_particle(
        self, callback: Callable[[ParticleEvent], None], *, name: str | None = None,
    ) -> Callable[[], None]:
        """Call a synchronous observer for each future matching particle and return an unsubscribe function."""
        if self._feedback is None:
            raise RuntimeError("player has not joined")
        return self._feedback.subscribe_particle(callback, name=name)

    async def wait_for_block_event(
        self, position: tuple[float, float, float] | None = None, *, kind: str | None = None, timeout: float = 15,
    ) -> BlockEvent:
        """Wait for the next matching block feedback event in the server stream."""
        if self._feedback is None:
            raise RuntimeError("player has not joined")
        return await self._feedback.wait_for_block_event(position, kind, timeout)

    def subscribe_block_events(
        self, callback: Callable[[BlockEvent], None], *,
        position: tuple[float, float, float] | None = None, kind: str | None = None,
    ) -> Callable[[], None]:
        """Call a synchronous observer for each future matching block event and return an unsubscribe function."""
        if self._feedback is None:
            raise RuntimeError("player has not joined")
        return self._feedback.subscribe_block_events(callback, position=position, kind=kind)

    @property
    def recipes(self) -> tuple[Recipe, ...]:
        """Current server-issued recipes, not locally assumed vanilla recipes."""
        if self._recipes is None:
            raise RuntimeError("player has not joined")
        return self._recipes.snapshot()

    def get_recipe(self, recipe_id: str | int) -> Recipe | None:
        """Find a server-issued recipe by string ID or network ID."""
        if self._recipes is None:
            raise RuntimeError("player has not joined")
        return self._recipes.get(recipe_id)

    @property
    def limited_crafting(self) -> bool | None:
        """Server's doLimitedCrafting rule, or None before it is observed."""
        return None if self._environment is None else self._environment.limited_crafting

    @property
    def unlocked_recipe_ids(self) -> frozenset[str]:
        """Recipe book IDs known to be unlocked for this player."""
        if self._recipes is None:
            raise RuntimeError("player has not joined")
        return self._recipes.unlocked_ids

    def recipes_all(self, item: str, aux_value: int | None = None, *,
                    crafting_table: bool = True, grid_size: Literal[2, 3] | None = None) -> tuple[Recipe, ...]:
        """Catalog matches for this output in a 2x2 or 3x3 grid.

        Specify grid_size to query either size without opening a window. With
        grid_size=None, an open workbench selects 3x3 when crafting_table is
        true, and other cases use 2x2. crafting_table=False only allows 2x2.
        The catalog includes recipes regardless of materials or recipe-book
        membership.
        """
        if self._recipes is None:
            raise RuntimeError("player has not joined")
        return self._recipes.recipes_all(item, aux_value, crafting_table=crafting_table, grid_size=grid_size)

    def recipes_for(self, item: str, aux_value: int | None = None, min_result_count: int = 1, *,
                    crafting_table: bool = True, grid_size: Literal[2, 3] | None = None) -> tuple[Recipe, ...]:
        """Catalog matches with known permission and carried ingredients for the requested output.

        Grid selection follows recipes_all(). Each server recipe is checked
        individually. Materials already staged in the grid are outside the
        carried inventory considered here.
        """
        if self._recipes is None:
            raise RuntimeError("player has not joined")
        return self._recipes.recipes_for(
            item, aux_value, min_result_count, crafting_table=crafting_table, grid_size=grid_size,
        )

    async def wait_for_recipes(self, *, timeout: float = 15) -> tuple[Recipe, ...]:
        """Wait for the first CraftingData from BDS, which may be an empty catalog."""
        if self._recipes is None:
            raise RuntimeError("player has not joined")
        return await self._recipes.wait_loaded(timeout=timeout)

    async def craft_recipe_auto(
        self, recipe_id: str | int, destination_slot: int, *,
        times: int = 1, timeout: float = 15,
    ):
        """Craft using the open player-inventory screen; return the BDS verdict.

        Open the inventory screen first with open_inventory_screen(). BDS 1.26.52
        rejects a recipe-book request without that active window.
        """
        if self._recipes is None:
            raise RuntimeError("player has not joined")
        return await self._recipes.craft_auto(
            recipe_id, destination_slot, times=times, timeout=timeout,
        )

    async def set_recipe_book_settings(
        self, filtering: bool, inventory_layout: str = "DEFAULT", crafting_layout: str = "DEFAULT",
        left_tab: str = "NONE", right_tab: str = "NONE",
    ) -> None:
        """Send inventory options to BDS each time this is called."""
        packets = self.packets
        b = packets.protocol.binding
        if type(filtering) is not bool:
            raise TypeError("filtering must be bool")

        await packets.send(b.SetPlayerInventoryOptionsPacket(inventory_options=b.InventoryOptions(
            filtering=filtering, layout_inv=_client_option(b.InventoryLayout, inventory_layout),
            layout_craft=_client_option(b.InventoryLayout, crafting_layout),
            left_inventory_tab=_client_option(b.InventoryLeftTabIndex, left_tab),
            right_inventory_tab=_client_option(b.InventoryRightTabIndex, right_tab),
        )))

    async def update_client_options(
        self, *, graphics_mode: str | None = None, filter_profanity: bool | None = None,
    ) -> None:
        """Report the supplied client options to BDS. None omits a field."""
        packets = self.packets
        b = packets.protocol.binding
        if graphics_mode is None and filter_profanity is None:
            raise ValueError("provide graphics_mode or filter_profanity")
        if filter_profanity is not None and type(filter_profanity) is not bool:
            raise TypeError("filter_profanity must be bool or None")
        graphics = None if graphics_mode is None else _client_option(b.GraphicsMode, graphics_mode)
        await packets.send(b.UpdateClientOptionsPacket(graphics_mode=graphics, filter_profanity=filter_profanity))

    async def stage_crafting_ingredient(self, source_slot: int, grid_slot: int = 28,
                                        amount: int = 1, *, timeout: float = 15):
        """Place inventory material in the active 2x2 or workbench 3x3 grid."""
        if self._recipes is None:
            raise RuntimeError("player has not joined")
        return await self._recipes.stage_grid(source_slot, grid_slot, amount, timeout=timeout)

    async def return_crafting_ingredient(self, grid_slot: int, destination_slot: int, amount: int = 1, *,
                                         timeout: float = 15):
        """Return an observed grid ingredient to main inventory; return the matching BDS verdict."""
        if self._recipes is None:
            raise RuntimeError("player has not joined")
        if not 0 <= destination_slot < 36:
            raise ValueError("destination slot must be in the main inventory (0..35)")
        return await self._recipes.return_grid(grid_slot, destination_slot, amount, timeout=timeout)

    async def craft_recipe_grid(self, recipe_id: str | int, destination_slot: int, *,
                                additional_destination_slots: tuple[int, ...] = (),
                                times: int = 1, timeout: float = 15):
        """Craft from observed grid inputs and return the matching BDS verdict.

        For one craft with multiple results in the personal inventory or a
        workbench, supply a distinct confirmed empty main inventory slot for
        each result, in the server's recipe order.
        """
        if self._recipes is None:
            raise RuntimeError("player has not joined")
        return await self._recipes.craft_grid(recipe_id, destination_slot,
                                              additional_destination_slots=additional_destination_slots,
                                              times=times, timeout=timeout)

    @property
    def player_id(self) -> int:
        if self._session is None or self._session.player_id is None:
            raise RuntimeError("player has not joined")
        return self._session.player_id

    @property
    def spawn_chunk_radius(self) -> int | None:
        """The server's confirmed view radius at spawn, if reported during login."""
        if self._session is None:
            raise RuntimeError("player has not joined")
        return self._session.chunk_radius

    @property
    def requested_chunk_radius(self) -> int | None:
        """Latest radius sent in a request for this session, in chunks."""
        if self._session is None:
            raise RuntimeError("player has not joined")
        return self._session.requested_chunk_radius

    @property
    def accepted_chunk_radius(self) -> int | None:
        """Latest server-reported radius for this session, in chunks."""
        if self._session is None:
            raise RuntimeError("player has not joined")
        return self._session.accepted_chunk_radius

    async def request_chunk_radius(self, radius: int) -> None:
        """Send a radius request. The server reports its own radius separately."""
        if self._session is None:
            raise RuntimeError("player has not joined")
        await self._session.request_chunk_radius(radius)

    async def wait_for_chunk_radius_update(self, *, timeout: float = 15) -> int:
        """Wait for the next radius reported by BDS, independent of requests."""
        if self._session is None:
            raise RuntimeError("player has not joined")
        return await self._session.wait_for_chunk_radius_update(timeout)

    @property
    def published_chunk_bounds(self) -> tuple[int, int, int, int] | None:
        """Latest publisher bounds as inclusive chunk coordinates (min_x, max_x, min_z, max_z)."""
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.published_bounds

    @property
    def position(self) -> tuple[float, float, float] | None:
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return self._movement.position

    @property
    def movement_state(self) -> MovementState:
        """Current predicted motion and inputs, and server grounding, corrections and poses."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return self._movement.state

    def _usable_elytra(self) -> bool:
        if self._inventory is None or self._session is None:
            return False
        chest = self._inventory.armor_slots.get(1)
        if (chest is None or int(chest.stack_size) != 1
                or self._session.item_names.get(int(chest.id)) != "minecraft:elytra"):
            return False
        try:
            damage = read_item_user_data(bytes(chest.user_data_buffer)).get("Damage", int(chest.aux_value))
        except ValueError:
            return False
        # Vanilla elytra has 432 durability and stops working with one point left.
        return isinstance(damage, int) and 0 <= damage < 431

    async def set_gliding(self, gliding: bool) -> int | None:
        """Submit ordinary elytra input and return its tick when a request was sent.

        A start requires usable elytra in the observed chest slot and an observed
        fall. Read movement_state.gliding for the server's subsequent pose.
        """
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.set_gliding(gliding, self._usable_elytra)

    async def set_crawling(self, crawling: bool) -> int | None:
        """Request crawl under a known low roof or stand where there is clearance.

        Return the submitted tick when a request was sent. Read
        movement_state.crawling for the server's subsequent pose.
        """
        if self._movement is None or self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._movement.set_crawling(crawling, self._blocks)

    async def set_move_vector(self, strafe: float, forward: float) -> None:
        """Hold directional input until replaced or released. Positive means left or forward."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        await self._movement.set_move_vector(strafe, forward)

    async def move_to(self, position: tuple[float, float, float]) -> None:
        """Set a same-level feet target and steer to rest on the scheduled inputs."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        await self._movement.move_to(position)

    async def wait_for_position_sync(self, timeout: float = 15) -> tuple[float, float, float]:
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.wait_for_position_sync(timeout)

    @property
    def rotation(self) -> tuple[float, float]:
        """Local input facing as (pitch, yaw) in degrees."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return self._movement.rotation

    async def set_rotation(self, pitch: float, yaw: float) -> None:
        """Set the facing for subsequent input frames, with positive pitch looking down."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        self._movement.set_rotation(pitch, yaw)

    async def look_at(self, target: tuple[float, float, float]) -> tuple[float, float]:
        """Aim toward a world-space eye-height target on subsequent input frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.look_at(target)

    async def walk_forward(self, ticks: int = 24) -> int:
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.walk_forward(ticks)

    async def walk_backward(self, ticks: int = 24) -> int:
        """Walk opposite the facing direction; return submitted frames, not BDS acceptance."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.walk_backward(ticks)

    async def strafe_left(self, ticks: int = 24) -> int:
        """Move left without turning; return submitted frames, not BDS acceptance."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.strafe_left(ticks)

    async def strafe_right(self, ticks: int = 24) -> int:
        """Move right without turning; return submitted frames, not BDS acceptance."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.strafe_right(ticks)

    async def walk_forward_left(self, ticks: int = 24) -> int:
        """Move diagonally forward-left; return submitted frames, not BDS acceptance."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.walk_forward_left(ticks)

    async def walk_forward_right(self, ticks: int = 24) -> int:
        """Move diagonally forward-right; return submitted frames, not BDS acceptance."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.walk_forward_right(ticks)

    async def walk_backward_left(self, ticks: int = 24) -> int:
        """Move diagonally backward-left; return submitted frames, not BDS acceptance."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.walk_backward_left(ticks)

    async def walk_backward_right(self, ticks: int = 24) -> int:
        """Move diagonally backward-right; return submitted frames, not BDS acceptance."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.walk_backward_right(ticks)

    async def set_sneaking(self, sneaking: bool) -> int:
        """Submit the held sneak state and return the client tick, not BDS acceptance."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.set_sneaking(sneaking)

    async def set_sprinting(self, sprinting: bool) -> int:
        """Hold the sprint key for later forward input; return a submitted tick."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.set_sprinting(sprinting)

    async def set_flying(self, flying: bool) -> None:
        """Submit a flight switch if authorized and the target has changed."""
        if self._movement is None or self._player_state is None:
            raise RuntimeError("player has not joined")
        await self._movement.set_flying(flying, self._player_state)

    async def fly_to(
        self, feet_position: tuple[float, float, float], *,
        horizontal_tolerance: float = 0.35, vertical_tolerance: float = 0.35, ticks: int = 160,
    ) -> FlightResult:
        """Fly a short, clear path in Creative and release input to hover near the target."""
        if self._movement is None or self._player_state is None or self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._movement.fly_to(
            self._blocks, self._player_state, feet_position,
            horizontal_tolerance=horizontal_tolerance, vertical_tolerance=vertical_tolerance, ticks=ticks,
        )

    @property
    def is_swimming(self) -> bool | None:
        """Latest local-player swim metadata, or None until the server reports it."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return self._movement.swimming

    async def set_swimming(self, swimming: bool) -> int | None:
        """Submit a swim transition and return its input tick.

        Starting requires water observed in the player's center column.
        Return None when no frame was needed or water was unconfirmed.
        Server acceptance is observed separately through `is_swimming`.
        """
        if self._movement is None or self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._movement.set_swimming(
            swimming, water_contact=self._blocks._water_contact, mounted=lambda: self.vehicle is not None,
        )

    async def jump(self) -> int:
        """Submit a jump press; return its tick, not confirmed server movement."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.jump()

    async def jump_forward(self, ticks: int = 24) -> int:
        """Jump once during forward walking; return submitted moving frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.jump_forward(ticks)

    async def hop_forward(self, ticks: int = 32) -> int:
        """Repeat forward hops on level ground; return submitted moving frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.hop_forward(ticks)

    async def hop_backward(self, ticks: int = 32) -> int:
        """Repeat backward hops on level ground; return submitted moving frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.hop_backward(ticks)

    async def hop_left(self, ticks: int = 32) -> int:
        """Repeat left-strafing jumps on level ground; return submitted frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.hop_left(ticks)

    async def hop_right(self, ticks: int = 32) -> int:
        """Repeat right-strafing jumps on level ground; return submitted frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.hop_right(ticks)

    async def hop_forward_left(self, ticks: int = 32) -> int:
        """Repeat forward-left hops on level ground; return submitted frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.hop_forward_left(ticks)

    async def hop_forward_right(self, ticks: int = 32) -> int:
        """Repeat forward-right hops on level ground; return submitted frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.hop_forward_right(ticks)

    async def hop_backward_left(self, ticks: int = 32) -> int:
        """Repeat backward-left hops on level ground; return submitted frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.hop_backward_left(ticks)

    async def hop_backward_right(self, ticks: int = 32) -> int:
        """Repeat backward-right hops on level ground; return submitted frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.hop_backward_right(ticks)

    async def jump_backward(self, ticks: int = 24) -> int:
        """Jump once during backward walking; return submitted moving frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.jump_backward(ticks)

    async def jump_forward_left(self, ticks: int = 24) -> int:
        """Jump diagonally forward-left; return submitted moving frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.jump_forward_left(ticks)

    async def jump_forward_right(self, ticks: int = 24) -> int:
        """Jump diagonally forward-right; return submitted moving frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.jump_forward_right(ticks)

    async def jump_backward_left(self, ticks: int = 24) -> int:
        """Jump diagonally backward-left; return submitted moving frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.jump_backward_left(ticks)

    async def jump_backward_right(self, ticks: int = 24) -> int:
        """Jump diagonally backward-right; return submitted moving frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.jump_backward_right(ticks)

    async def jump_left(self, ticks: int = 24) -> int:
        """Jump once while strafing left; return submitted moving frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.jump_left(ticks)

    async def jump_right(self, ticks: int = 24) -> int:
        """Jump once while strafing right; return submitted moving frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.jump_right(ticks)

    async def stop_moving(self) -> None:
        """Release held directions and wait for an active action's input release."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        await self._movement.stop_moving()

    async def follow_entity(self, runtime_id: int, *, ticks: int = 80,
                            stop_distance: float = 1.5) -> int:
        """Follow the latest server-observed actor position for a bounded time."""
        if self._movement is None or self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._movement.follow_entity(
            self._entities, runtime_id, ticks=ticks, stop_distance=stop_distance
        )

    def switch_follow_target(self, runtime_id: int) -> None:
        """Redirect an active follow to another actor on the next input frame."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        self._movement.switch_follow_target(runtime_id)

    async def navigate_to(self, target: tuple[float, float, float], *,
                          ticks: int = 160, search_radius: int = 8) -> int:
        """Submit a flat route through observed blocks; return moving frames."""
        if self._movement is None or self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._movement.navigate_to(
            self._blocks, target, ticks=ticks, search_radius=search_radius
        )

    def plan_navigation(self, goal: NavigationGoal, *, rules: NavigationRules = NavigationRules()) -> NavigationPlan:
        """Plan from the current feet block and observations without sending packets."""
        if self._movement is None or self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._movement.plan_navigation(self._blocks, goal, rules)

    async def navigate(self, goal: NavigationGoal, *, rules: NavigationRules = NavigationRules(),
                       ticks: int = 160, timeout: float = 15, follow: bool = False,
                       on_plan: Callable[[NavigationPlan], None] | None = None,
                       on_event: Callable[[NavigationEvent], None] | None = None) -> NavigationResult:
        """Plan from the actual start, then move while rechecking terrain and the goal.

        The starting search bounds, expansion limit, frame budget and timeout
        apply throughout the action. Reached means the latest stance satisfies
        the goal. A later interaction has its own server response.

        With a top-level EntityNearGoal, follow=True keeps the action active after arrival.
        It holds the movement lock and sends no inputs while the route remains
        settled in range. It can replan for the same observed actor within the
        original limits. At the deadline, reached requires a confirmed arrival
        and a current stance that still matches the goal.

        If supplied, on_plan runs synchronously each time this call adopts a
        route, before sending movement for it. Callback exceptions propagate
        to this caller after movement cleanup.

        If supplied, on_event synchronously observes this call's searches,
        route changes and outcome. Observer exceptions go to the event loop's
        exception handler. Time spent observing before completion counts
        toward timeout.
        """
        if self._movement is None or self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._movement.navigate(
            self._blocks, goal, rules, ticks=ticks, timeout=timeout, follow=follow, on_plan=on_plan, on_event=on_event,
        )

    async def approach_block(self, position: tuple[int, int, int], *,
                             ticks: int = 160, search_radius: int = 8,
                             vertical_radius: int = 4) -> BlockApproach:
        """Reach a horizontal face of an observed block over several levels.

        The search stays within search_radius blocks along each horizontal axis
        and within vertical_radius levels vertically from the start. Each edge
        changes height by at most one block and needs observed clearance and suitable support.
        A descent needs two clear, supported lower columns. Each ascent can
        submit one ordinary jump and needs overhead clearance in both columns.
        Movement and each server landing confirmation share the frame budget.

        On predicted arrival, return the feet block and Bedrock face ready for
        a subsequent interaction. The result counts submitted moving frames.
        Other outcomes report insufficient terrain
        evidence, no route in bounds, an explicit stop or budget exhaustion.
        """
        if self._movement is None or self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._movement.approach_block(
            self._blocks, position, ticks=ticks, search_radius=search_radius, vertical_radius=vertical_radius,
        )

    async def wait_for_block_update(
        self, position: tuple[int, int, int], *, layer: int = 0, timeout: float = 15
    ) -> BlockUpdate:
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._blocks.wait_for_update(position, layer=layer, timeout=timeout)

    def observed_block(
        self, position: tuple[int, int, int], *, layer: int = 0
    ) -> BlockObservation | None:
        """Last server-supplied block, or None until that position is known."""
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.observed_block(position, layer=layer)

    def observed_block_state(
        self, position: tuple[int, int, int], *, layer: int = 0,
    ) -> BlockStateObservation | None:
        """Return the cached vanilla state snapshot, or None if it cannot be resolved."""
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.observed_block_state(position, layer=layer)

    def observed_block_entity(self, position: tuple[int, int, int]) -> nbt.CompoundTag | None:
        """Return an independent copy of the latest observed block entity NBT.

        Return None when the position has no current block entity observation.
        """
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.observed_block_entity(position)

    def subscribe_block_entity_updates(
        self, callback: Callable[[BlockEntityUpdate], None], *,
        position: tuple[int, int, int] | None = None, dimension_id: int | None = None,
    ) -> Callable[[], None]:
        """Observe future direct server block entity updates after the cache is updated.

        The synchronous callback receives its own mutable NBT copy. Filters are
        optional. The returned function stops delivery, as does session end.
        """
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.subscribe_block_entity_updates(callback, position=position, dimension_id=dimension_id)

    @property
    def dimension_bounds(self) -> DimensionBounds | None:
        """Read the current terrain's effective block Y range without sending packets."""
        if self._blocks is None or self.session_end is not None:
            return None
        return self._blocks.dimension_bounds

    def observed_biome(self, position: tuple[int, int, int]) -> int | None:
        """Read the latest observed numeric biome ID at this block position.

        Return None when the current dimension has no observation for it.
        This cache query sends no packets.
        """
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.observed_biome(position)

    def find_blocks(
        self, names: str | int | Iterable[str] | Iterable[int] | None = None, *,
        radius: int = 16, limit: int | None = None,
        predicate: Callable[[BlockMatch], bool | None] | None = None,
        center: tuple[float, float, float] | None = None,
    ) -> BlockSearchResult:
        """Search cached primary-layer blocks in the current dimension.

        Each coordinate of center is floored to a block coordinate. Omit center
        to use the player's synchronized position.

        Pass a name, a Bedrock session runtime ID, an iterable of names, or an
        iterable of IDs. Bare names use the minecraft namespace. Hash sessions
        accept signed 32-bit representations of their IDs. Omit names to test
        every observed block with the predicate. ID and predicate-only matches
        can have name=None.

        The radius includes its boundary. Results are filtered before sorting by
        squared distance, X, Y and Z and applying limit. Completeness requires enough
        cached information to evaluate the selector throughout the sphere. A predicate
        result of None omits that match and makes complete false. This query sends no packets.
        """
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.find_blocks(names, radius=radius, limit=limit, predicate=predicate, center=center)

    def find_block(
        self, names: str | int | Iterable[str] | Iterable[int] | None = None, *, radius: int = 16,
        predicate: Callable[[BlockMatch], bool | None] | None = None,
        center: tuple[float, float, float] | None = None,
    ) -> BlockSearchResult:
        """Run find_blocks with limit=1 and the same completeness rules."""
        return self.find_blocks(names, radius=radius, limit=1, predicate=predicate, center=center)

    def find_block_vein(
        self, position: tuple[int, int, int], *, max_blocks: int = 100,
        max_distance: int = 16, flood_radius: int = 1,
    ) -> BlockSearchResult:
        """Find the observed primary-layer vein of the seed's vanilla block name.

        Traverse breadth-first from the seed, considering neighbors in
        lexicographic (X, Y, Z) order. The Chebyshev flood radius and seed-relative
        Manhattan distance are inclusive. Known air and coordinates outside the
        dimension's known height yield a complete empty result. An unresolved
        seed, unknown reachable neighbors, or omitted matches make the result
        incomplete. This cache query sends no packets.
        """
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.find_block_vein(
            position, max_blocks=max_blocks, max_distance=max_distance, flood_radius=flood_radius,
        )

    def raycast_block(self, *, max_distance: float = 5.0) -> BlockRaycastResult:
        """Trace the current eye and input facing through cached primary block outlines.

        A hit provides the first surface, Bedrock face, local click point and distance.
        A missing pose or insufficient local shape data yields unknown.
        The maximum distance must be finite and nonnegative.
        """
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.raycast_block(max_distance)

    def estimate_sourceless_explosion_damage(
        self, center: tuple[float, float, float], radius: float, *,
        target_runtime_id: int | None = None, raw: bool = False,
    ) -> float | None:
        """Estimate one vanilla Bedrock 2225 script explosion with source omitted.

        Omit the target for the local player. Raw mode uses distance and cached
        terrain exposure and also supports observed standing remote players.
        The default applies observed local armor under supported conditions.
        Unknown state yields None. The synchronous query sends no packets.
        """
        return _estimate_sourceless_explosion_damage(
            self, center, radius, target_runtime_id=target_runtime_id, raw=raw,
        )

    def raycast_player_view(self, runtime_id: int, *, max_distance: float = 5.0) -> BlockRaycastResult:
        """Trace a visible remote player's observed view through cached block outlines.

        A hit gives the first surface in the current dimension. A missing pose or
        insufficient block data yields unknown. This query sends no packets.
        """
        blocks, entities = self._blocks, self._entities
        if blocks is None or entities is None:
            raise RuntimeError("player has not joined")
        return blocks._raycast_view(entities.player_view_pose(runtime_id), max_distance)

    def raycast_block_towards(
        self, point: tuple[float, float, float], *, max_distance: float = 5.0,
    ) -> BlockRaycastResult:
        """Read cached outlines along a ray from the current eye toward a world point.

        The point sets the direction. The finite, nonnegative distance limits the ray.
        """
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.raycast_block_towards(point, max_distance)

    def column_coverage(self, column: tuple[int, int]) -> ColumnCoverage:
        """Read current coverage for one integer (chunk x, chunk z) without sending a packet."""
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.column_coverage(column)

    async def wait_for_column_coverage(
        self, column: tuple[int, int], sections: range, *, timeout: float = 15,
    ) -> ColumnCoverage:
        """Wait for all section Y indices in an ascending range, without requesting them.

        Unload and dimension change raise ColumnCoverageInvalidated, and an ended
        packet stream raises ConnectionError. Cancelling this wait is local to it.
        """
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._blocks.wait_for_column_coverage(column, sections, timeout=timeout)

    async def wait_for_terrain_coverage(
        self, columns: Mapping[tuple[int, int], range], *, timeout: float = 15,
    ) -> tuple[ColumnCoverage, ...]:
        """Wait for all selected sections to be cached together in the current dimension.

        Pass a nonempty mapping from integer (chunk x, chunk z) pairs to ascending
        section Y ranges. Return immutable snapshots in mapping order without
        sending requests. Unload and dimension change raise ColumnCoverageInvalidated,
        and an ended packet stream raises ConnectionError.
        """
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._blocks.wait_for_terrain_coverage(columns, timeout=timeout)

    def subscribe_column_coverage(self, callback: Callable[[ColumnCoverageEvent], None]) -> Callable[[], None]:
        """Observe future loaded, coverage growth and unloaded events synchronously."""
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.subscribe_column_coverage(callback)

    def subscribe_block_changes(
        self, callback: Callable[[BlockChange], None], *, dimension_id: int | None = None,
        position: tuple[int, int, int] | None = None, layer: int | None = None,
        on_dimension_leave: Callable[[int, int], None] | None = None,
    ) -> Callable[[], None]:
        """Observe immutable before/after values from sparse updates in packet order.

        With ``on_dimension_leave``, ``position`` is required and the subscription is bound
        to ``dimension_id`` or the current terrain dimension at registration. On leaving that
        dimension, the subscription ends and the callback receives the old and new IDs
        synchronously after the old terrain is cleared.
        """
        if on_dimension_leave is not None and position is None:
            raise ValueError("on_dimension_leave requires a position")
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.subscribe_block_changes(
            callback, dimension_id=dimension_id, position=position, layer=layer, on_dimension_leave=on_dimension_leave,
        )

    async def request_sub_chunk(self, position: tuple[int, int, int]) -> None:
        """Ask BDS for the containing section; inspect observed_block afterwards."""
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        await self._blocks.request_sub_chunk(position)

    def observed_bed(self, position: tuple[int, int, int]) -> BedObservation | None:
        """Read an observed bed half and the occupation reported by its head."""
        if self._beds is None:
            raise RuntimeError("player has not joined")
        return self._beds.observed(position)

    @property
    def is_sleeping(self) -> bool | None:
        """Return the sleep flag most recently reported for this player."""
        if self._beds is None:
            raise RuntimeError("player has not joined")
        return self._beds.sleeping

    @property
    def sleeping_bed(self) -> tuple[int, int, int] | None:
        """Return the reported bed position while the player is sleeping."""
        if self._beds is None:
            raise RuntimeError("player has not joined")
        return self._beds.sleeping_bed

    @property
    def player_respawn_point(self) -> PlayerRespawnPoint | None:
        """Return the latest personal respawn point reported by the server."""
        if self._beds is None:
            raise RuntimeError("player has not joined")
        return self._beds.respawn_point

    def subscribe_bed_events(self, callback: Callable[[_BedEvent], None]) -> Callable[[], None]:
        """Observe future local bed events synchronously in the current session.

        The callback receives "sleep", "wake", or "respawn_point_invalidated"
        after the corresponding server observation updates the local cache.
        The returned function removes this registration.
        """
        if self._beds is None:
            raise RuntimeError("player has not joined")
        return self._beds.subscribe(callback)

    async def sleep(self, position: tuple[int, int, int], *, face: int = 1,
                    timeout: float = 15) -> tuple[int, int, int]:
        """Use the observed bed and await this player's confirmed bed position."""
        if self._beds is None:
            raise RuntimeError("player has not joined")
        return await self._beds.sleep(position, face=face, timeout=timeout)

    async def stop_sleeping(self) -> None:
        """Submit the Leave Bed input."""
        if self._beds is None:
            raise RuntimeError("player has not joined")
        await self._beds.stop_sleeping()

    async def wake(self, *, timeout: float = 15) -> None:
        """Leave the observed sleep session and await the server's awake flag."""
        if self._beds is None:
            raise RuntimeError("player has not joined")
        await self._beds.wake(timeout=timeout)

    def observed_sign(self, position: tuple[int, int, int]) -> SignObservation | None:
        """Return both sides from an observed sign update, or None if unknown."""
        if self._signs is None:
            raise RuntimeError("player has not joined")
        return self._signs.observed(position)

    @property
    def sign_editor(self) -> SignEditor | None:
        """Return the current server-opened sign editor, if one is active."""
        return None if self._signs is None else self._signs.editor

    async def wait_for_sign_open(
        self, *, position: tuple[int, int, int] | None = None, timeout: float | None = 15,
    ) -> SignEditor:
        """Observe the next matching sign open in this session without sending a packet."""
        if self._signs is None:
            raise RuntimeError("player has not joined")
        return await self._signs.wait_for_open(position=position, timeout=timeout)

    async def submit_sign_text(self, editor: SignEditor, text: str) -> None:
        """Submit text to the opened side of this editor, preserving the other side.

        The editor must still be active and both sides and wax status must be
        known. A later server observation confirms the resulting sign text.
        """
        if self._signs is None:
            raise RuntimeError("player has not joined")
        await self._signs.submit_text(editor, text)

    async def edit_sign(self, position: tuple[int, int, int], text: str, *, face: int,
                        timeout: float = 15) -> SignSide:
        """Click a known sign and submit the edit to the side opened by the server.

        The returned side describes the submitted write. Confirm the text from a
        subsequent server observation. Both sides of the sign must have been observed.
        """
        if self._signs is None:
            raise RuntimeError("player has not joined")
        return await self._signs.edit(position, text, face=face, timeout=timeout)

    def inventory_count(self, item_id: int) -> int:
        """Count items in the latest applied inventory snapshot, which may be stale."""
        if self._inventory is None:
            raise RuntimeError("player has not joined")
        return self._inventory.count_item(item_id)

    async def wait_for_inventory_item_count(
        self, item_id: int, minimum: int, *, timeout: float = 15
    ) -> int:
        """Wait for a server-observed item count to reach the minimum.

        Raise RuntimeError if a main inventory slot update failed and fresh
        server observations have not yet restored the missing slots.
        """
        if self._inventory is None:
            raise RuntimeError("player has not joined")
        return await self._inventory.wait_for_item_count(
            item_id, minimum, timeout=timeout
        )

    async def select_hotbar(self, slot: int) -> None:
        """Request a hotbar slot; observe BDS to confirm the equipped item."""
        if self._inventory is None:
            raise RuntimeError("player has not joined")
        await self._inventory.select_hotbar(slot)

    @property
    def last_cursor_observation(self) -> CursorObservation | None:
        """Read the last server report for the active connection's inventory UI cursor.

        Return None until a report arrives and after the connection ends. The
        cursor may have changed since the report. A stack response can report
        count and stack network ID, while item details require a full descriptor.
        """
        inventory = self._inventory
        state = self._session_end_state
        if (inventory is None or (state is not None and state.result is not None)
                or inventory.packets._reader_end_result()[0]):
            return None
        return inventory._last_cursor_observation

    @property
    def held_item(self) -> HeldItemSnapshot:
        """Snapshot the main-hand slot selected for the next input.

        The snapshot carries the latest server description for that slot and
        any local count prediction. The source identifies a default, local
        submission, or server-reported selection for this player.
        """
        if self._inventory is None:
            raise RuntimeError("player has not joined")
        return self._inventory.held_item

    async def wait_for_held_item_change(self, *, timeout: float | None = 15) -> HeldItemSnapshot:
        """Wait for the first changed main-hand snapshot after this call starts.

        A change can follow a successful local submission or a server report
        for this player.
        """
        if self._inventory is None:
            raise RuntimeError("player has not joined")
        return await self._inventory.wait_for_held_item_change(timeout=timeout)


    def get_book(self, slot: int) -> BookSnapshot | None:
        """Read the observed book in a main inventory slot."""
        if self._books is None:
            raise RuntimeError("player has not joined")
        return self._books.get(slot)

    async def wait_for_book_update(self, slot: int, *, timeout: float = 15) -> BookSnapshot | None:
        """Wait for an observed book change in this slot, including removal."""
        if self._books is None:
            raise RuntimeError("player has not joined")
        return await self._books.wait_for_update(slot, timeout=timeout)

    async def edit_book(
        self, slot: int, operation: BookEdit, page_index: int = 0, text: str = "", other_index: int = 0,
    ) -> None:
        """Submit a page operation on an observed writable book."""
        if self._books is None:
            raise RuntimeError("player has not joined")
        await self._books.edit(slot, operation, page_index, text, other_index)

    async def sign_book(self, slot: int, title: str, author: str | None = None) -> None:
        """Submit a signing request with the given author or this player's name."""
        if self._books is None:
            raise RuntimeError("player has not joined")
        await self._books.sign(slot, title, author)

    @property
    def inventory_slots(self) -> dict[int, object]:
        """Latest server-described player inventory slots."""
        if self._inventory is None:
            raise RuntimeError("bot is not joined")
        return self._inventory.slots

    def subscribe_inventory_changes(
        self, callback: Callable[[InventorySlotChange], None], *, slot: int | None = None,
    ) -> Callable[[], None]:
        """Observe future server-reported changes in the player's main inventory.

        Each synchronous callback sees the state after a complete update or matched
        verdict. Omit slot to watch every main inventory slot. The returned function
        unsubscribes and can be called repeatedly.
        """
        if self._inventory is None:
            raise RuntimeError("player has not joined")
        return self._inventory.subscribe_inventory_changes(callback, slot=slot)

    @property
    def game_mode(self) -> GameType | None:
        """Server-reported game mode for this player, or None before it is known."""
        return None if self._creative is None else self._creative.game_mode

    def subscribe_game_mode(self, callback: Callable[[GameType | None], None]) -> Callable[[], None]:
        """Observe future changes in this player's effective game mode in the joined session.

        The synchronous callback receives the updated game_mode, including None
        when the server reports an unresolved mode. The returned function
        removes this registration and can be called repeatedly.
        """
        creative = self._require_creative_inventory()
        state = self._get_session_end_state()
        if state is None:
            raise RuntimeError("player has not joined")
        if state.result is not None:
            raise ConnectionError("game mode subscription stream is closed")
        return creative.subscribe_game_mode(callback)

    @property
    def creative_catalog(self) -> CreativeCatalog | None:
        """Latest server-issued creative groups and entries, or None before receipt."""
        return None if self._creative is None else self._creative.catalog

    def _require_creative_inventory(self) -> CreativeInventory:
        if self._creative is None:
            raise RuntimeError("player has not joined")
        return self._creative

    async def take_creative_item(self, entry: CreativeEntry, slot: int, amount: int = 1, *, timeout: float = 15):
        """Take from the current catalog into a player inventory slot and await BDS's verdict."""
        return await self._require_creative_inventory().take(entry, slot, amount, timeout=timeout)

    async def destroy_creative_item(self, slot: int, amount: int, *, timeout: float = 15):
        """Destroy a quantity in a player inventory slot and await BDS's verdict."""
        return await self._require_creative_inventory().destroy(slot, amount, timeout=timeout)

    async def fill_creative_slot(self, slot: int, entry: CreativeEntry, count: int = 1, *, timeout: float = 15):
        """Set a player inventory slot from the current catalog and return the ordered verdicts.

        Replacing a different stack can require two requests. An empty tuple means no
        request was needed. Check the observed slot for the final item and count.
        If interrupted, the original exception's creative_progress records each
        prepared request, transport submission, and any confirmed verdict.
        """
        return await self._require_creative_inventory().fill(slot, entry, count, timeout=timeout)

    async def clear_creative_slot(self, slot: int, *, timeout: float = 15):
        """Destroy the observed stack, or return None for a confirmed empty slot."""
        return await self._require_creative_inventory().clear(slot, timeout=timeout)

    async def clear_creative_inventory(
        self, slots: Iterable[int] | None = None, *, include_armor: bool = False, timeout: float = 15,
    ) -> CreativeClearResult:
        """Clear selected observed slots, defaulting to all main inventory slots.

        The result reports each selected slot in order. With include_armor,
        four additional results report armor clearing after the main inventory.
        If interrupted, the original exception's creative_progress carries the
        partial result.
        """
        return await self._require_creative_inventory().clear_inventory(
            slots, include_armor=include_armor, timeout=timeout,
        )

    @property
    def armor_slots(self) -> dict[int, object]:
        """Latest server-described armor slots; absent slots are not confirmed empty."""
        if self._inventory is None:
            raise RuntimeError("bot is not joined")
        return self._inventory.armor_slots

    @property
    def offhand_item(self):
        """Latest observed offhand item, or None if the server has not described it."""
        if self._inventory is None:
            raise RuntimeError("bot is not joined")
        return self._inventory.offhand_item

    async def equip_armor(self, source_slot: int, armor_slot: int, *, timeout: float = 15):
        """Return the server's item-stack verdict for an inventory/armor swap."""
        if self._inventory is None:
            raise RuntimeError("bot is not joined")
        return await self._inventory.equip_armor(source_slot, armor_slot, timeout=timeout)


    async def unequip_armor(
        self, armor_slot: int | Literal["head", "chest", "legs", "feet"],
        destination_slot: int | None = None, *, timeout: float = 15,
    ):
        """Return the matching BDS verdict for taking armor into inventory.

        Omit the destination to use a confirmed empty main inventory slot.
        An explicit occupied destination swaps stacks.
        """
        if self._inventory is None:
            raise RuntimeError("bot is not joined")
        return await self._inventory.unequip_armor(
            armor_slot, destination_slot, timeout=timeout,
        )

    async def equip_offhand(self, source_slot: int, *, timeout: float = 15):
        """Equip an entire inventory stack and return the matching ItemStackResponse.

        Open the player inventory screen first. An occupied offhand swaps stacks.
        """
        if self._inventory is None or self._containers is None:
            raise RuntimeError("bot is not joined")
        return await self._inventory.equip_offhand(source_slot, containers=self._containers, timeout=timeout)

    async def unequip_offhand(
        self, destination_slot: int | None = None, *, amount: int | None = None, timeout: float = 15,
    ):
        """Move items from the offhand to inventory and return the matching ItemStackResponse.

        Open the player inventory screen first. Omit the destination to move the
        whole stack into the first confirmed empty main inventory slot. An explicit
        occupied slot swaps stacks when amount is omitted. With an explicit slot
        and amount, submit exactly that amount once for BDS to accept or reject.
        """
        if self._inventory is None or self._containers is None:
            raise RuntimeError("bot is not joined")
        return await self._inventory.unequip_offhand(
            destination_slot, containers=self._containers, amount=amount, timeout=timeout,
        )


    async def drop_inventory_item(self, slot: int, amount: int = 1, *, timeout: float = 15):
        """Return BDS's verdict for dropping an observed inventory stack."""
        if self._inventory is None:
            raise RuntimeError("player has not joined")
        return await self._inventory.drop_item(slot, amount, timeout=timeout)

    async def drop_selected_item(self, whole_stack: bool = False) -> None:
        """Submit the normal drop-key transaction for the selected hotbar item."""
        if self._inventory is None or self._containers is None:
            raise RuntimeError("player has not joined")
        await self._inventory.drop_selected_item(whole_stack, self._containers)

    async def use_item(self) -> None:
        """Submit use of the selected main-hand item in air."""
        if self._item_use is None:
            raise RuntimeError("player has not joined")
        await self._item_use.submit()

    @property
    def fishing_hook(self) -> EntityState | None:
        """The server-observed hook owned by this player."""
        if self._fishing is None:
            raise RuntimeError("player has not joined")
        return self._fishing.hook

    async def cast_rod(self, *, timeout: float = 10) -> EntityState:
        """Use the selected fishing rod and wait for an owned hook."""
        if self._fishing is None:
            raise RuntimeError("player has not joined")
        return await self._fishing.cast(timeout=timeout)

    async def wait_for_fishing_bite(self, *, timeout: float = 90) -> EntityState:
        """Observe a bite event on the currently owned hook."""
        if self._fishing is None:
            raise RuntimeError("player has not joined")
        return await self._fishing.wait_for_bite(timeout=timeout)

    async def reel_rod(self, *, timeout: float = 5) -> FishingResult:
        """Reel the owned hook and observe matching pickup and inventory evidence."""
        if self._fishing is None:
            raise RuntimeError("player has not joined")
        return await self._fishing.reel(timeout=timeout)

    async def cancel_fishing(self, *, timeout: float = 10) -> bool:
        """Recall an owned hook and confirm its removal. Return False when no hook is observed."""
        if self._fishing is None:
            raise RuntimeError("player has not joined")
        return await self._fishing.cancel(timeout=timeout)

    async def fish(
        self, *, bite_timeout: float = 90, cast_timeout: float = 10, reel_timeout: float = 5,
    ) -> FishingResult:
        """Cast, wait for the owned hook's bite, then reel and observe receipt."""
        if self._fishing is None:
            raise RuntimeError("player has not joined")
        return await self._fishing.fish(bite_timeout=bite_timeout, cast_timeout=cast_timeout, reel_timeout=reel_timeout)

    @property
    def using_item(self) -> bool | None:
        """Latest server-reported held-item use flag, or None before it is observed."""
        if self._item_use is None:
            raise RuntimeError("player has not joined")
        return self._item_use.using_item

    async def wait_for_item_cooldown(
        self, *, category: str | None = None, timeout: float | None = 15,
    ) -> ItemCooldownEvent:
        """Wait for the next server cooldown announcement matching an optional exact category.

        Read the category and starting duration in ticks from the announcement.
        Consumption completion is observed separately through consume.
        """
        if self._item_use is None:
            raise RuntimeError("player has not joined")
        return await self._item_use.wait_for_cooldown(category=category, timeout=timeout)

    async def release_using_item(self) -> None:
        """Submit release of the selected main-hand item."""
        if self._item_use is None:
            raise RuntimeError("player has not joined")
        await self._item_use.release()

    async def consume(self, *, timeout: float = 5) -> ItemConsumption:
        """Submit use and wait for BDS's completion and selected-slot update.

        The timeout covers position sync, submission and the server response.
        A timeout leaves the outcome unknown. Observe using_item or submit
        release_using_item separately if needed.
        """
        if self._item_use is None:
            raise RuntimeError("player has not joined")
        return await self._item_use.consume(timeout=timeout)

    @property
    def open_window(self) -> ContainerWindow | None:
        """Latest server-opened window, or None if closed or not yet opened."""
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return self._containers.window

    @property
    def container_slots(self) -> dict[int, object] | None:
        """Current window slots from BDS; None until initial contents arrive."""
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return self._containers.slots

    def subscribe_container_slot_changes(
        self, callback: Callable[[ContainerSlotChange], None], *, slot: int | None = None,
    ) -> Callable[[], None]:
        """Observe subsequent changes to container_slots in the open window.

        Use a logical slot index or None for all slots. Closing or replacing
        the window ends the subscription.
        """
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return self._containers.subscribe_slot_changes(callback, slot=slot)

    @property
    def enchantment_seed_at_join(self) -> int | None:
        """Return the signed enchantment seed from StartGame, or None without an active session.

        This login snapshot can differ from the server's current enchantment seed after enchanting.
        """
        session = self._session
        state = self._get_session_end_state()
        if session is None or state is None or state.result is not None:
            return None
        return session.enchantment_seed_at_join

    def _require_enchanting(self) -> Enchanting:
        if self._enchanting is None:
            raise RuntimeError("player has not joined")
        return self._enchanting

    @property
    def enchant_options(self) -> tuple[EnchantOption, ...] | None:
        """Latest observed choices in current button order, or None until observed."""
        return self._require_enchanting().options

    async def wait_for_enchant_options(self, *, timeout: float = 15) -> tuple[EnchantOption, ...]:
        """Wait for an observed list in the active enchanting table, including an empty list."""
        return await self._require_enchanting().wait_for_options(timeout=timeout)

    async def wait_for_enchant_choices(self, *, timeout: float = 15) -> tuple[EnchantOption, ...]:
        """Wait for usable options for the observed input, in server button order.

        Wait for each input change's server reply before using the returned choices.
        """
        return await self._require_enchanting().wait_for_choices(timeout=timeout)

    @property
    def enchanting_slots(self) -> dict[int, object]:
        """Stacks at 14 (input) and 15 (lapis), including metadata when received."""
        return self._require_enchanting().slots

    async def stage_enchanting_item(
        self, source_slot: int, table_slot: int = 14, amount: int = 1, *, timeout: float = 15,
    ):
        """Move an inventory stack into the active enchanting table and await its reply."""
        return await self._require_enchanting().transfer(
            source_slot, table_slot, amount, into_table=True, timeout=timeout,
        )

    async def take_enchanting_item(
        self, table_slot: int, destination_slot: int, amount: int = 1, *, timeout: float = 15,
    ):
        """Retrieve a stack from the active enchanting table and await its reply."""
        return await self._require_enchanting().transfer(
            destination_slot, table_slot, amount, into_table=False, timeout=timeout,
        )

    async def enchant(self, option: int, *, timeout: float = 15):
        """Select a survival button. The verdict can omit the result's item metadata."""
        return await self._require_enchanting().select(option, timeout=timeout)

    async def wait_for_container_slots(self, *, timeout: float = 15) -> dict[int, object]:
        """Wait for server-reported slots of the currently open container."""
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return await self._containers.wait_for_slots(timeout=timeout)

    def preview_anvil(self, new_name: str | None = None) -> AnvilPreview | None:
        """Estimate the level cost and material count for the current anvil inputs.

        The preview uses observed vanilla item data in Survival on protocol 2225.
        It returns None for missing observations or unsupported operations. It
        sends no packets and works without a known experience level or destination.
        Use the same new_name for preview and submission, either None or a
        nonempty string.
        """
        session = self._session
        if self._containers is None or session is None:
            raise RuntimeError("player has not joined")
        return self._containers.preview_anvil(new_name, session.item_names, game_mode=self.game_mode)

    async def apply_anvil(
        self, destination_slot: int, new_name: str | None = None, *,
        material_count: int | None = None, timeout: float = 15,
    ):
        """Commit the open anvil in survival or creative and await BDS's verdict.

        The server-observed game mode must still match when the request is sent.
        Stage the item in slot 1 and any material in slot 2 using
        put_into_container. The destination must be an observed empty slot.
        Omitting the name during a repair can restore the item's default name
        and add a level to the cost. Pass the existing custom name to keep it.
        Supply material_count when you know the amount the recipe requires.
        The default submits the entire staged material stack. BDS validates
        the requested amount and the cost. Read the verdict for name and damage
        corrections and a full inventory observation for the item metadata.
        """
        if self._containers is None:
            raise RuntimeError("player has not joined")
        mode = self.game_mode
        game_type = self.protocol.binding.GameType
        if mode not in (game_type.SURVIVAL, game_type.CREATIVE):
            raise RuntimeError("anvil transactions require an observed survival or creative game mode")

        def check_game_mode():
            if self.game_mode != mode:
                raise RuntimeError("game mode changed before anvil submission")

        return await self._containers.apply_anvil(
            destination_slot, new_name, material_count=material_count, timeout=timeout, check_game_mode=check_game_mode,
        )

    @property
    def furnace_progress(self) -> FurnaceProgress | None:
        """Read the open furnace-family window's progress, with None for unreported fields."""
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return self._containers.furnace_progress

    async def wait_for_furnace_progress(self, *, timeout: float = 15) -> FurnaceProgress:
        """Wait for the next recognized update for the current furnace window.

        Ingredient, fuel and result are slots 0, 1 and 2 in container_slots.
        """
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return await self._containers.wait_for_furnace_progress(timeout=timeout)

    def subscribe_furnace_progress(self, callback: Callable[[FurnaceProgress], None]) -> Callable[[], None]:
        """Observe each supported packet for the open furnace, including repeated values.

        The subscription ends when the window closes or changes. Use the
        returned function to unsubscribe sooner.
        """
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return self._containers.subscribe_furnace_progress(callback)

    @property
    def brewing_progress(self) -> BrewingProgress | None:
        """Read the open brewing stand's brew ticks and fuel units, if reported."""
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return self._containers.brewing_progress

    async def wait_for_brewing_progress(self, *, timeout: float = 15) -> BrewingProgress:
        """Wait for the next recognized progress report for the open brewing stand."""
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return await self._containers.wait_for_brewing_progress(timeout=timeout)

    async def take_from_container(
        self, source_slot: int, destination_slot: int, amount: int = 1, *, timeout: float = 15
    ):
        """Return the server's item-stack verdict; response slots update observed state.

        For a horse opened with open_entity_container(), take one saddle from
        slot 0 into a confirmed empty main inventory slot.
        """
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return await self._containers.take_from_container(
            source_slot, destination_slot, amount, timeout=timeout
        )

    async def move_inventory_item(
        self, source_slot: int, destination_slot: int, amount: int = 1, *, timeout: float = 15
    ):
        """Split or merge a player stack and return the matching BDS verdict."""
        if self._inventory is None:
            raise RuntimeError("player has not joined")
        return await self._inventory.move_item(
            source_slot, destination_slot, amount, timeout=timeout
        )

    async def swap_inventory_items(
        self, source_slot: int, destination_slot: int, *, timeout: float = 15
    ):
        """Swap player slots; return the matching BDS item-stack verdict."""
        if self._inventory is None:
            raise RuntimeError("player has not joined")
        return await self._inventory.swap_items(
            source_slot, destination_slot, timeout=timeout
        )

    async def put_into_container(
        self, source_slot: int, destination_slot: int, amount: int = 1, *, timeout: float = 15
    ):
        """Return the server's verdict for inventory-to-container transfer.

        For a horse opened with open_entity_container(), put one saddle from
        main inventory into its confirmed empty slot 0.
        """
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return await self._containers.put_into_container(
            source_slot, destination_slot, amount, timeout=timeout
        )

    async def open_inventory_screen(self, *, timeout: float = 15) -> ContainerWindow:
        """Open the player inventory UI and await its server-created window.

        Raise RuntimeError if another player inventory open is in progress.
        """
        if self._containers is None:
            raise RuntimeError("bot is not joined")
        return await self._containers.open_inventory_screen(timeout=timeout)

    async def close_container(self, *, timeout: float = 15) -> ContainerWindow | None:
        """Close the active window; wait for the matching server close packet."""
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return await self._containers.close_window(timeout=timeout)

    async def wait_for_container_close(self, *, timeout: float = 15):
        """Observe the active window closing; return BDS's typed close packet.

        Does not send a close request. Arm before a possible server-initiated close.
        """
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return await self._containers.wait_for_close(timeout=timeout)

    async def use_item_on(
        self, position: tuple[int, int, int], face: int = 1,
        click_pos: tuple[float, float, float] | None = None,
    ) -> None:
        """Submit one main-hand click at a block face and optional block-local point.

        Returns after sending the input. Observe the relevant server effect separately.
        """
        containers, blocks = self._containers, self._blocks
        if containers is None or blocks is None:
            raise RuntimeError("player has not joined")
        await self.wait_for_position_sync()
        await containers.click_block(position, blocks, face=face, click_pos=click_pos)

    async def place_block(
        self, against: tuple[int, int, int], *, face: int,
        expected_runtime_id: int | None = None, expected_block_name: str | None = None,
        replace: bool = False, click_pos: tuple[float, float, float] | None = None, timeout: float = 15,
    ) -> PlacementResult:
        """Attempt survival placement with one click on ``against``.

        By default, the target is observed air adjacent to the support face.
        Clicking observed vanilla short grass requires ``replace=True``,
        which targets the clicked cell. A failed target precondition sends no click.

        Supply exactly one expected value, a runtime ID from this session or
        a fully qualified vanilla block name. Name matching resolves the
        target's newly observed main-layer ID using the trusted vanilla palette.
        The target change and the selected slot's one-item decrease are independent
        server observations. Identifying who placed the block requires separate evidence.
        """
        containers, blocks, creative = self._containers, self._blocks, self._creative
        if containers is None or blocks is None or creative is None:
            raise RuntimeError("player has not joined")
        result = await observe_placement(
            containers, blocks, creative, against,
            face=face, expected_runtime_id=expected_runtime_id, expected_block_name=expected_block_name,
            replace=replace, click_pos=click_pos, timeout=timeout,
        )
        observers = self._placement_completion_observers
        if result.reason != "observations_complete" or self._blocks is not blocks or not observers:
            return result
        # The reader can finish before its done callback clears the observers.
        state = self._get_session_end_state()
        if state is None or state.result is not None:
            return result
        for key, callback in tuple(observers.items()):
            if key not in observers:
                continue
            try:
                callback(result)
            except (Exception, asyncio.CancelledError) as error:
                asyncio.get_running_loop().call_exception_handler({
                    "message": "placement completion observer failed", "exception": error, "callback": callback,
                })
        return result

    def subscribe_placement_completions(self, callback: Callable[[PlacementResult], None]) -> Callable[[], None]:
        """Observe future complete results from this Bot's ``place_block`` in the joined session.

        The synchronous callback receives the same immutable result just before
        the action returns, once the target change and the selected stack's
        one-item decrease have both been observed. Those observations establish
        no server attribution. The returned function unsubscribes and can be
        called repeatedly. Registrations end with the session.
        """
        state = self._get_session_end_state()
        if self._session is None or state is None:
            raise RuntimeError("player has not joined")
        if state.result is not None:
            raise ConnectionError("placement completion subscription stream is closed")
        key = object()
        self._placement_completion_observers[key] = callback
        owner_ref = ref(self)

        def unsubscribe() -> None:
            owner = owner_ref()
            if owner is not None:
                owner._placement_completion_observers.pop(key, None)

        return unsubscribe

    async def fill_water_bucket(
        self, position: tuple[int, int, int], *, timeout: float = 15,
    ) -> WaterBucketFillResult:
        """Click an observed main-layer water source once with a selected empty bucket in Survival.

        Aim at the source before calling. With stacked buckets, an observed empty
        main inventory slot is required and the server chooses the destination.
        The result preserves independent observations of source removal, the
        selected slot's change, and the water bucket's destination. Those server
        effects carry no request ID linking them to the click. On cancellation,
        the exception's ``placement_progress`` holds the observations so far.
        """
        session = self._session
        containers, blocks, creative = self._containers, self._blocks, self._creative
        if session is None or containers is None or blocks is None or creative is None:
            raise RuntimeError("player has not joined")
        return await observe_water_bucket_fill(containers, blocks, creative, lambda: session.item_registry,
                                               position, timeout=timeout)

    async def place_entity(
        self, position: tuple[int, int, int], *, timeout: float = 15,
    ) -> EntityPlacementResult:
        """Place a supported held item with one click on observed terrain in Survival.

        Report a matching new spawn and a one-item decrease in the selected slot
        when each observation arrives after submission.
        """
        return await self._place_entity(position, EntityPlacementResult, timeout)

    async def place_oak_boat(
        self, position: tuple[int, int, int], *, timeout: float = 15,
    ) -> BoatPlacementResult:
        """Click observed source water once while holding an oak boat in Survival.

        The result reports a new boat spawn at the click point and a one-item
        decrease in the selected slot, when each is observed after submission.
        These server observations carry no request ID linking them to the click.
        """
        return await self._place_entity(position, BoatPlacementResult, timeout)

    async def _place_entity[Result: EntityPlacementResult](
        self, position: tuple[int, int, int], result_type: type[Result], timeout: float,
    ) -> Result:
        session = self._session
        containers, blocks, creative, entities = self._containers, self._blocks, self._creative, self._entities
        if session is None or containers is None or blocks is None or creative is None or entities is None:
            raise RuntimeError("player has not joined")
        return await observe_entity_placement(
            containers, blocks, creative, entities, lambda: session.item_registry, position,
            result_type=result_type, timeout=timeout,
        )

    async def open_container(
        self, position: tuple[int, int, int], *, face: int = 1, timeout: float = 15
    ) -> ContainerWindow:
        """Use a block and wait for the server's container-open response.

        On protocol 2225, request an announced but unobserved target section
        within the opening timeout before clicking. No click is retried.
        """
        if self._containers is None or self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._containers.open(position, self._blocks, face=face, timeout=timeout, _observe_target=True)

    async def open_entity_container(self, runtime_id: int, *, timeout: float = 15) -> ContainerWindow:
        """Open an observed chest minecart, hopper minecart, chest watercraft, or horse.

        For chest watercraft and horse equipment, submit set_sneaking(True)
        and keep the observed main hand empty. Open horse equipment on foot.
        The click waits for server-reported sneaking within the same timeout.
        The method leaves your pose alone.
        Return the matching server-opened window. Use wait_for_container_slots
        for its contents. Exact saddle transfers use slot 0 on a horse opened
        through this method. Horse armor and range transfers are unsupported.
        Chest watercraft and hopper minecart transfers require matching
        27-slot and 5-slot windows, respectively.
        """
        if self._containers is None or self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._containers.open_entity(runtime_id, self._entities, timeout=timeout)

    @property
    def mining_target(self) -> MiningTarget | None:
        """The active held target; block changes are observed separately."""
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.mining_target

    def mining_face(self, position: tuple[int, int, int]) -> MiningFaceResult:
        """Sample visible mining faces within reach of the current input eye.

        The query sends no packets and keeps the current aim. A finite search
        with no hit yields no_sample unless observed geometry proves exclusion.
        """
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.mining_face(position)

    def subscribe_mining_events(self, callback: Callable[[MiningEvent], None]) -> Callable[[], None]:
        """Observe mining outcomes on this session's event loop and return an unsubscribe callback.

        Completion reports observed air at the target, regardless of what broke
        it. Creative finish observes a matching update for up to 15 seconds.
        """
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.subscribe_mining_events(callback)

    async def start_mining(self, position: tuple[int, int, int], *, face: int = 1) -> int:
        """Begin holding an observed target; return its submitted client tick."""
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._blocks.start_mining(position, face=face)

    async def continue_mining(self, position: tuple[int, int, int], *, face: int = 1) -> int:
        """Continue holding this block or release the old target and start this one."""
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._blocks.continue_mining(position, face=face)

    async def stop_mining(self) -> int | None:
        """Stop the active break or release held input, returning the release tick if sent."""
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._blocks.stop_mining()

    async def finish_mining(self, position: tuple[int, int, int], *, face: int = 1) -> int:
        """Submit an instant break in confirmed creative mode; return the input tick."""
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._blocks.finish_mining(position, face=face)

    async def mine_block(
        self, position: tuple[int, int, int], *, face: int | None = 1, timeout: float = 15
    ) -> BlockUpdate:
        """Mine until a server block update.

        Face None selects and aims at a visible mining face without moving.
        Raise MiningFaceUnavailable if automatic selection cannot submit START.
        Raise MiningAborted if stop_mining stops the break before its matching update.
        """
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._blocks.mine_block(position, face=face, timeout=timeout)

    async def collect_block(
        self, position: tuple[int, int, int], *, ticks: int = 160,
        search_radius: int = 8, timeout: float = 15,
    ) -> BlockCollection:
        """Approach an observed block, equip a tool and try one break and pickup.

        Return the evidence available when the attempt ends. Collection
        requires a matching drop, pickup by this player and a confirmed main
        inventory increase. The approach uses the given moving-frame budget
        and search radius. Timeout applies to equipment waits and to the
        combined break and pickup observation.
        """
        if self._block_collector is None:
            raise RuntimeError("player has not joined")
        return await self._block_collector.collect(
            position, ticks=ticks, search_radius=search_radius, timeout=timeout,
        )

    async def collect_targets(
        self, positions: Iterable[tuple[int, int, int]], *, count: int | None = None,
        ticks: int = 160, search_radius: int = 8, timeout: float = 15, tool_chest: HarvestToolChest | None = None,
        tool_chests: Sequence[HarvestToolChest] | None = None, storage: CollectionStorage | None = None,
    ) -> BlockCollectionBatch:
        """Collect unique targets in input order, up to count.

        Apply the movement and wait budgets to each target. Stop at the first
        result whose status is not collected. An exception can carry known
        progress in its collection_progress attribute.

        With tool_chest, a confirmed inventory shortage can trigger one trip
        to the specified chest for that target, then a return to the target.
        Each leg gets the same movement budget. See the target result's
        tool_acquisition for the chest and transfer evidence.

        Alternatively, pass tool_chests to try a sequence in order. A
        confirmed shortage in a closed chest or an unreachable approach
        before clicking permits the next attempt. Read tool_refill_attempts
        for the sequence of outcomes on each target.

        With storage, a sole live drop blocked by a fully observed inventory
        can trigger one trip to store an authorized stack. The next target
        requires a complete transfer, matching close and confirmed pickup
        of the original drop. Read storage_attempt for the target's evidence.
        """
        if self._block_collector is None:
            raise RuntimeError("player has not joined")
        return await self._block_collector.collect_targets(
            self, positions, count=count, ticks=ticks, search_radius=search_radius, timeout=timeout,
            tool_chest=tool_chest, tool_chests=tool_chests, storage=storage,
        )

    async def collect_by_count(
        self, names: str | int | Iterable[str] | Iterable[int], *, item_id: int, count: int,
        radius: int = 16, refresh_candidates: bool = False, ticks: int = 160, search_radius: int = 8, timeout: float = 15,
        tool_chest: HarvestToolChest | None = None, tool_chests: Sequence[HarvestToolChest] | None = None,
        storage: CollectionStorage | None = None,
    ) -> CountedBlockCollection:
        """Collect a positive count of one item from nearby observed matching blocks.

        Snapshot candidates in distance order. Pass refresh_candidates=True
        to check the same starting sphere for new coordinates after each
        exhausted batch. The check uses cached observations and ends when
        there are no new candidates. Skip a changed block only before START
        in the same world generation. Stop on other failures.
        The result counts confirmed main-inventory gains for item_id and can
        exceed count. Exceptions carry known collection_progress.

        Pass tool_chest or tool_chests to enable the same shortage refills as
        collect_targets. A change during an unsettled ordered refill ends
        the batch. Pass storage to free space as in collect_targets.
        """
        if self._block_collector is None:
            raise RuntimeError("player has not joined")
        return await self._block_collector.collect_by_count(
            self, names, item_id=item_id, count=count, radius=radius, refresh_candidates=refresh_candidates, ticks=ticks,
            search_radius=search_radius, timeout=timeout, tool_chest=tool_chest, tool_chests=tool_chests,
            storage=storage,
        )

    async def collect_vein_by_count(
        self, seed: tuple[int, int, int], *, item_id: int, count: int, max_blocks: int = 100,
        max_distance: int = 16, flood_radius: int = 1, ticks: int = 160,
        search_radius: int = 8, timeout: float = 15, tool_chest: HarvestToolChest | None = None,
        tool_chests: Sequence[HarvestToolChest] | None = None, storage: CollectionStorage | None = None,
    ) -> CountedBlockCollection:
        """Collect a positive count of one item from an observed block vein.

        Snapshot the candidates in find_block_vein order and attempt known
        matches even when search_complete is false. Count confirmed
        main-inventory gains for item_id, including a pickup that exceeds
        count. Each attempt requires a finite positive timeout. Exceptions
        carry known collection_progress.

        Pass tool_chest or tool_chests to enable the same shortage refills as
        collect_by_count. Pass storage to free space as in collect_targets.
        """
        if self._block_collector is None:
            raise RuntimeError("player has not joined")
        if timeout is None or not 0 < timeout < inf:
            raise ValueError("collection timeout must be finite and positive")
        return await self._block_collector.collect_vein_by_count(
            self, seed, item_id=item_id, count=count, max_blocks=max_blocks, max_distance=max_distance,
            flood_radius=flood_radius, ticks=ticks, search_radius=search_radius, timeout=timeout,
            tool_chest=tool_chest, tool_chests=tool_chests, storage=storage,
        )

    async def collect_mixed_targets(
        self, targets: Iterable[tuple[int, int, int] | EntityState], *, count: int | None = None,
        ticks: int = 160, search_radius: int = 8, timeout: float = 15,
    ) -> MixedCollectionBatch:
        """Collect block coordinates and observed item actors in the supplied order.

        At admission, a drop snapshot's two IDs select the current observed item.
        Each subsequent pickup is bound to that instance. The batch stops at
        the first unsuccessful target. Exceptions carry known collection_progress.
        """
        if self._block_collector is None:
            raise RuntimeError("player has not joined")
        return await self._block_collector.collect_mixed_targets(
            targets, count=count, ticks=ticks, search_radius=search_radius, timeout=timeout,
        )

    def best_harvest_tool(
        self, position: tuple[int, int, int], *, require_harvest: bool = True,
    ) -> HarvestTool | None:
        """Rank vanilla tools for the observed block without sending packets.

        By default only tools that can harvest the block are eligible. Pass
        require_harvest=False to rank ordinary breaking progress, including
        choices that yield no drop. Pickaxes, axes, shovels, hoes and shears
        receive the Efficiency I–V bonus where their base speed exceeds bare
        hands. The returned speed_multiplier is the base speed.
        Bare hands have item_name=None. An absent candidate, unknown block or
        protocol other than 2225 returns None.
        """
        if self._harvest_tools is None:
            raise RuntimeError("player has not joined")
        return self._harvest_tools.best(position, require_harvest=require_harvest)

    async def equip_harvest_tool(
        self, position: tuple[int, int, int], *, timeout: float = 15, require_harvest: bool = True,
    ) -> HarvestTool | None:
        """Select the best tool or an empty hand for the observed block.

        Pass require_harvest=False to use the breaking-progress ranking from
        best_harvest_tool. Await inventory transfers if needed and close any
        inventory screen opened for the transfer. The returned slot is the
        requested hotbar slot.
        Return None when no candidate or usable slot is known. A changed stack
        or a rejected or unconfirmed transfer raises RuntimeError. Each server
        wait uses timeout.
        """
        if self._harvest_tools is None:
            raise RuntimeError("player has not joined")
        return await self._harvest_tools.equip(position, timeout=timeout, require_harvest=require_harvest)

    async def place_against(
        self, against: BlockUpdate | BlockObservation, *, face: int = 1,
        slot: int = 0, click_pos: tuple[float, float, float] | None = None,
    ) -> int:
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._blocks.place_against(against, face=face, slot=slot, click_pos=click_pos)

    async def send_chat(self, message: str) -> None:
        """Submit a chat packet; server delivery is not implied."""
        if self._chat is None:
            raise RuntimeError("player has not joined")
        await self._chat.send(message)

    async def send_whisper(self, recipient: str, message: str) -> None:
        """Submit a private chat command addressed to one literal player name."""
        if self._chat is None or self._session is None:
            raise RuntimeError("player has not joined")
        await self._chat.send_whisper(
            recipient, message, player_uuid=self.identity.uuid,
            player_unique_id=self._session.player_unique_id,
        )

    async def wait_for_chat(self, *, author: str | None = None, timeout: float = 15) -> ChatMessage:
        """Wait for a new player chat relayed by the server."""
        if self._chat is None:
            raise RuntimeError("player has not joined")
        return await self._chat.wait_for_message(author=author, timeout=timeout)

    def subscribe_chat(
        self, callback: Callable[[ChatMessage], None], *, author: str | None = None,
    ) -> Callable[[], None]:
        """Run a synchronous callback for matching chat and return an idempotent unsubscribe."""
        if self._chat is None:
            raise RuntimeError("player has not joined")
        return self._chat.subscribe_message(callback, author=author)

    async def wait_for_text(
        self, *, kind: TextPacketType | None = None, author: str | None = None,
        message: str | Pattern[str] | None = None, timeout: float = 15,
    ) -> TextEvent:
        """Wait for the next matching server TextPacket dispatched after the wait starts.

        Match the packet's original message field. Strings compare the entire
        field and compiled regular expressions search within it.
        """
        if self._chat is None:
            raise RuntimeError("player has not joined")
        return await self._chat.wait_for_text(kind=kind, author=author, message=message, timeout=timeout)

    async def wait_for_message(
        self, *matchers: str | Pattern[str] | Sequence[str | Pattern[str]], timeout: float = 15,
        include_text_objects: bool = False, locale: str | None = None,
    ) -> str:
        """Wait for the first matching incoming text with a supported format.

        Strings match the entire text. Compiled regular expressions search within it.
        A sequence supplies alternative matchers.
        Set include_text_objects to include fully literal rawtext objects.
        An explicit Bedrock locale also includes rawtext resolved in full from
        this connection's resource packs.
        """
        if self._chat is None:
            raise RuntimeError("player has not joined")
        return await self._chat.wait_for_content(
            *matchers, timeout=timeout, include_text_objects=include_text_objects, locale=locale,
        )

    def subscribe_text(
        self, callback: Callable[[TextEvent], None], *, kind: TextPacketType | None = None,
        author: str | None = None,
    ) -> Callable[[], None]:
        """Run a synchronous callback for matching text and return an idempotent unsubscribe."""
        if self._chat is None:
            raise RuntimeError("player has not joined")
        return self._chat.subscribe_text(callback, kind=kind, author=author)

    def add_chat_pattern(self, name: str, pattern: ChatPatterns, *, repeat: bool = True) -> int:
        """Register a regex or a nonempty, ordered sequence of regexes and return its ID.

        Each server-relayed player chat message can advance a pattern by one step.
        Each regex searches the message text. Intervening messages are skipped.
        After completion the pattern starts over, or is removed if repeat is false.
        """
        if self._chat is None:
            raise RuntimeError("player has not joined")
        return self._chat.add_pattern(name, pattern, repeat=repeat)

    def remove_chat_pattern(self, name_or_id: str | int) -> int:
        """Remove all patterns with the given name or one by ID and return the count."""
        if self._chat is None:
            raise RuntimeError("player has not joined")
        return self._chat.remove_pattern(name_or_id)

    async def wait_for_chat_match(self, name_or_id: str | int, *, timeout: float = 15) -> ChatMatch:
        """Wait for the next completion by registered name or ID and return its captures.

        If several registrations of the same name complete together, the earliest
        registration wins. An unknown selector at the start raises KeyError.
        Registrations and partial progress survive a wait's timeout or cancellation.
        """
        if self._chat is None:
            raise RuntimeError("player has not joined")
        return await self._chat.wait_for_match(name_or_id, timeout=timeout)

    def add_display_pattern(
        self, name: str, pattern: ChatPatterns, *, repeat: bool = True, locale: str | None = None,
    ) -> int:
        """Register an ordered pattern over incoming display text.

        Chat uses the same <author> message format as wait_for_message.
        By default, text objects must be entirely literal. An explicit Bedrock
        locale also includes rawtext resolved in full from this connection's packs.
        """
        if self._chat is None:
            raise RuntimeError("player has not joined")
        return self._chat.add_display_pattern(name, pattern, repeat=repeat, locale=locale)

    def remove_display_pattern(self, name_or_id: str | int) -> int:
        """Remove display patterns by name or ID and return the number removed."""
        if self._chat is None:
            raise RuntimeError("player has not joined")
        return self._chat.remove_display_pattern(name_or_id)

    async def wait_for_display_match(self, name_or_id: str | int, *, timeout: float = 15) -> DisplayMatch:
        """Wait for the next matching display pattern and return its events and captures."""
        if self._chat is None:
            raise RuntimeError("player has not joined")
        return await self._chat.wait_for_display_match(name_or_id, timeout=timeout)

    def subscribe_chat_match(
        self, name_or_id: str | int, callback: Callable[[ChatMatch], None],
    ) -> Callable[[], None]:
        """Run a synchronous callback for pattern completions and return an idempotent unsubscribe."""
        if self._chat is None:
            raise RuntimeError("player has not joined")
        return self._chat.subscribe_match(name_or_id, callback)

    async def wait_for_title(self, *, kind: TitleKind | None = None, timeout: float = 15) -> TitleEvent:
        """Wait for the next matching title packet dispatched after this wait starts.

        Raise TimeoutError if no match arrives by the deadline and ConnectionError
        if the stream closes before a match.
        """
        if self._titles is None:
            raise RuntimeError("player has not joined")
        return await self._titles.wait_for_event(kind=kind, timeout=timeout)

    def subscribe_titles(
        self, callback: Callable[[TitleEvent], None], *, kind: TitleKind | None = None,
    ) -> Callable[[], None]:
        """Run a synchronous callback for matching titles and return an idempotent unsubscribe."""
        if self._titles is None:
            raise RuntimeError("player has not joined")
        return self._titles.subscribe(callback, kind=kind)

    @property
    def command_catalog(self) -> CommandCatalog | None:
        """Return the latest immutable command advertisement, if available."""
        commands = self._commands
        return None if commands is None else commands.command_catalog

    @property
    def commands_enabled(self) -> bool | None:
        """Return the server's command-enabled flag for this session, if known."""
        commands = self._commands
        return None if commands is None else commands.commands_enabled

    def complete_command_name(self, prefix: str) -> tuple[str, ...] | None:
        """Complete the first command word from the current advertisement."""
        commands = self._commands
        return None if commands is None else commands.complete_name(prefix)

    def complete_command(self, text: str) -> tuple[str, ...] | None:
        """Suggest advertised command names, enum tokens, and literal tell targets.

        Return None when no catalog is available. This method sends no packets.
        """
        completion = self.complete_command_with_span(text)
        return None if completion is None else completion.tokens

    def complete_command_with_span(self, text: str) -> CommandCompletion | None:
        """Return completion tokens and their replacement span at the end of text.

        Start and end are Python string indices. Return None when no catalog is
        available, or an empty tokens tuple when no candidate matches.
        This method sends no packets.
        """
        commands = self._commands
        if commands is None:
            return None
        players = () if self._entities is None else self._entities.player_list()
        return commands.complete_command_with_span(text, (player.name for player in players))

    async def send_command(self, command: str) -> None:
        """Submit a player command, accepting an optional leading slash."""
        if self._commands is None:
            raise RuntimeError("player has not joined")
        await self._commands.send(command)

    async def execute_command(self, command: str, *, timeout: float = 15):
        """Submit a player command and observe the next command-style server text.

        The text has no request ID and does not prove the command took effect.
        """
        if self._commands is None:
            raise RuntimeError("player has not joined")
        return await self._commands.execute(command, timeout=timeout)

    async def execute_command_output(self, command: str, *, timeout: float = 15) -> CommandOutputPacket:
        """Submit a player command and return its matching native output on protocol 2225.

        A leading slash is optional. The timeout covers waiting to send, the send,
        and receiving the output. A server-reported success count of zero is returned
        in the packet. A timeout can occur after the server receives the command.
        """
        if self.protocol.number != 2225:
            raise NotImplementedError("typed command output is supported on protocol 2225")
        if self._commands is None:
            raise ConnectionError("player is not connected")
        return await self._commands.execute_output(command, timeout=timeout)

    async def set_command_block(
        self, position: tuple[int, int, int], command: str, *, timeout: float = 15,
        mode: Literal["impulse", "repeating", "chain"] | None = None, always_active: bool | None = None,
        conditional: bool | None = None, track_output: bool | None = None,
    ) -> BlockEntityUpdate:
        """Set the command text and selected settings of an observed command block on protocol 2225.

        A setting of None preserves its observed value. Send one edit and return
        the next matching direct block entity update within the deadline. The server
        decides whether to accept the edit. The update reports the observed edit,
        without proving command execution.
        """
        if self.protocol.number != 2225:
            raise NotImplementedError("command block editing is supported on protocol 2225")
        commands, blocks = self._commands, self._blocks
        if commands is None or blocks is None:
            raise ConnectionError("player is not connected")
        return await commands.set_command_block(
            blocks, position, command, timeout=timeout, mode=mode, always_active=always_active,
            conditional=conditional, track_output=track_output,
        )

    @property
    def health(self) -> float | None:
        """Latest server-reported health; None until a health packet arrives."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return self._player_state.health

    async def wait_for_health_update(self, timeout: float = 15) -> float:
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return await self._player_state.wait_for_health_update(timeout)

    @property
    def hunger(self) -> float | None:
        """Latest server-reported food level, or None until observed."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return self._player_state.hunger

    @property
    def experience_level(self) -> int | None:
        """Latest server-reported local-player experience level."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return self._player_state.experience_level

    @property
    def vitals(self) -> PlayerVitals:
        """Return server-reported health, food, air and experience bar values."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return self._player_state.vitals()

    async def wait_for_vitals_update(self, *, timeout: float = 15) -> PlayerVitals:
        """Wait for the next change in local-player vitals."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return await self._player_state.wait_for_vitals_update(timeout)

    @property
    def abilities(self) -> PlayerAbilities:
        """Latest ability snapshot reported by the server for this player."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return self._player_state.abilities

    async def wait_for_abilities(self, *, timeout: float = 15) -> PlayerAbilities:
        """Wait for the next server ability packet addressed to this player."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return await self._player_state.wait_for_abilities(timeout)

    async def wait_for_glide_boost(self, *, timeout: float | None = 15) -> GlideBoostEvent:
        """Wait for the next glide boost reported by the server for this player."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return await self._player_state.wait_for_glide_boost(timeout)

    def subscribe_attached_fireworks(self, callback: Callable[[EntityState], None]) -> Callable[[], None]:
        """Observe future sightings of fireworks attached to this player."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_attached_fireworks(callback)

    async def wait_for_attached_firework(self, *, timeout: float | None = 15) -> EntityState:
        """Wait for the next visible firework sighting attached to this player."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_attached_firework(timeout=timeout)

    @property
    def status_effects(self) -> tuple[PlayerEffect, ...]:
        """Return the observed active effects and their last announced durations."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return self._player_state.status_effects()

    async def wait_for_status_effects_update(self, *, timeout: float = 15) -> tuple[PlayerEffect, ...]:
        """Wait for the next change in local-player status effects."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return await self._player_state.wait_for_status_effects_update(timeout)

    async def wait_for_hunger_update(self, timeout: float = 15) -> float:
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return await self._player_state.wait_for_hunger_update(timeout)

    def subscribe_spawns(self, callback: Callable[[Literal["initial", "respawn"]], None]) -> Callable[[], None]:
        """Observe future local spawns synchronously, including across joins on this Bot.

        Register before joining to receive "initial" after the session is ready.
        "respawn" follows the server's first spawn-alive event after a local death.
        The returned function removes this individual registration.
        """
        observers = self._spawn_observers
        token = object()
        observers[token] = callback

        def unsubscribe() -> None:
            observers.pop(token, None)

        return unsubscribe

    def _publish_spawn(self, kind: Literal["initial", "respawn"]) -> None:
        for callback in tuple(self._spawn_observers.values()):
            try:
                callback(kind)
            except (Exception, asyncio.CancelledError) as error:
                asyncio.get_running_loop().call_exception_handler({
                    "message": "spawn observer failed", "exception": error, "callback": callback,
                })

    async def respawn(self, *, timeout: float = 15) -> tuple[float, float, float]:
        """Request respawn and acknowledge the server-provided spawn position."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return await self._player_state.respawn(timeout=timeout)

    @property
    def is_dead(self) -> bool:
        """Whether the server has reported zero or lower health for this player."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return self._player_state.is_dead

    async def wait_for_death(self, *, timeout: float = 15) -> DeathEvent:
        """Wait for the next death message sent directly to this player."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return await self._player_state.wait_for_death(timeout)

    async def wait_for_respawn(self, *, timeout: float = 15) -> tuple[float, float, float]:
        """Wait for the next server-reported respawn position after a death."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return await self._player_state.wait_for_respawn(timeout)

    async def submit_respawn(self, *, timeout: float = 15) -> None:
        """Submit the Respawn input and leave acknowledgment to the session."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        await self._player_state.submit_respawn(timeout=timeout)

    @property
    def observed_players(self) -> tuple[PlayerListEntry, ...]:
        """Snapshot this session's server player list, including entries beyond entity visibility."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.player_list()

    def observed_player_equipment(self, runtime_id: int) -> PlayerEquipmentSnapshot | None:
        """Read the latest equipment snapshot for a visible remote player."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.player_equipment(runtime_id)

    async def wait_for_player_equipment_change(
        self, runtime_id: int, *, timeout: float | None = 15,
    ) -> PlayerEquipmentSnapshot:
        """Wait for the next equipment change on the current visible player instance."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_player_equipment_change(runtime_id, timeout=timeout)

    def subscribe_player_equipment_changes(
        self, callback: Callable[[PlayerEquipmentSnapshot], None], *, runtime_id: int,
    ) -> Callable[[], None]:
        """Observe changes to a visible remote player's equipment via a synchronous callback.

        Returns an idempotent unsubscribe function. The subscription ends when
        the current player instance leaves view, the dimension changes, or the session ends.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_player_equipment_changes(callback, runtime_id=runtime_id)

    async def wait_for_player_list_update(
        self, *, action: Literal["add", "remove"] | None = None,
        player_uuid: UUID | None = None, timeout: float = 15,
    ) -> tuple[PlayerListChange, ...]:
        """Return matching entries from the next player-list packet dispatched after the wait starts."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_player_list_update(
            action=action, player_uuid=player_uuid, timeout=timeout,
        )

    def subscribe_player_list_updates(
        self, callback: Callable[[tuple[PlayerListChange, ...]], None], *,
        action: Literal["add", "remove"] | None = None, player_uuid: UUID | None = None,
    ) -> Callable[[], None]:
        """Call back with matching changes in packet order after each live roster update.

        Returns an idempotent unsubscribe function for the synchronous callback.
        The subscription ends with the current session.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_player_list_updates(callback, action=action, player_uuid=player_uuid)

    async def wait_for_player_skin_update(
        self, player_uuid: UUID, *, timeout: float | None = 15,
    ) -> PlayerSkinSnapshot | None:
        """Return the skin from the next live update applied to this listed UUID."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_player_skin_update(player_uuid, timeout=timeout)

    def subscribe_player_skin_updates(
        self, callback: Callable[[PlayerListEntry], None], *, player_uuid: UUID | None = None,
    ) -> Callable[[], None]:
        """Call back with the updated roster entry after each live skin packet for a listed UUID.

        Returns an idempotent unsubscribe function for the synchronous callback.
        The subscription ends with the current session.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_player_skin_updates(callback, player_uuid=player_uuid)

    @property
    def observed_entities(self) -> tuple[EntityState, ...]:
        """Server-observed actors, including other players; not a complete world scan."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.snapshot()

    def subscribe_entity_lifecycle(self, callback: Callable[[EntityLifecycleEvent], None]) -> Callable[[], None]:
        """Observe future entries and departures of visible remote actors in this session.

        The callback runs synchronously after the packet updates the cache and
        receives an immutable snapshot. The returned unsubscribe function is
        idempotent. The subscription continues across dimension changes.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_entity_lifecycle(callback)

    def subscribe_entity_glides(self, callback: Callable[[EntityState], None]) -> Callable[[], None]:
        """Observe glide starts by visible remote players during this session.

        Each callback receives the immutable snapshot at a server-reported change
        from non-gliding to gliding. The first known flag for each visible instance
        establishes its initial state. The returned function unsubscribes and can
        be called repeatedly.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_entity_glides(callback)

    def subscribe_entity_crouch_changes(
        self, callback: Callable[[EntityState], None], *, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        """Observe changes to the reported sneaking flag of visible remote players.

        Callbacks receive the immutable snapshot after each change between known
        flag values. A runtime filter binds the current player instance. The
        returned function unsubscribes and can be called repeatedly.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_entity_crouch_changes(callback, runtime_id=runtime_id)

    async def wait_for_entity_crouch_change(
        self, *, runtime_id: int | None = None, timeout: float | None = 15,
    ) -> EntityState:
        """Wait for the next change between known sneaking flag values.

        The result is the updated snapshot. A runtime filter binds the current
        remote player instance. Use ``timeout=None`` to wait without a deadline.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_entity_crouch_change(runtime_id=runtime_id, timeout=timeout)

    def subscribe_entity_sleep_changes(
        self, callback: Callable[[EntityState], None], *, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        """Observe changes between known sleeping flag values for visible remote players.

        The callback receives the updated immutable snapshot. A runtime filter
        binds the current player instance. The returned unsubscribe function is
        idempotent.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_entity_sleep_changes(callback, runtime_id=runtime_id)

    async def wait_for_entity_sleep_change(
        self, *, runtime_id: int | None = None, timeout: float | None = 15,
    ) -> EntityState:
        """Wait for the next change between known sleeping flag values.

        The result is the updated snapshot. A runtime filter binds the current
        remote player instance. Use ``timeout=None`` to wait without a deadline.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_entity_sleep_change(runtime_id=runtime_id, timeout=timeout)

    def entity_at_cursor(self, *, max_distance: float = 3.5) -> EntityCursorResult:
        """Read the first supported target on the ordinary player's current view ray.

        Eligibility uses the nominal box and the integer part of the finite,
        nonnegative range. The returned distance is to the expanded box.
        Equal entity distances use ascending runtime ID. Unresolved geometry
        or coincident block entries yield unknown. This query sends no packets.
        """
        entities, movement, blocks = self._entities, self._movement, self._blocks
        if entities is None or movement is None or blocks is None:
            raise RuntimeError("player has not joined")
        pose = movement._entity_cursor_pose()
        if self._player_state is not None and self._player_state.abilities.flying is True:
            pose = None
        return entities.entity_at_cursor(pose, blocks, max_distance)

    def nearest_player(self, match: Callable[[EntityState], bool] | None = None) -> EntityState | None:
        """Return the nearest observed remote player matching an optional predicate.

        Return None when the local feet position is unknown or there is no match.
        """
        if self._entities is None or self._movement is None:
            raise RuntimeError("player has not joined")
        return self._entities.nearest_player(self._movement.observed_feet_position, match)

    def nearest_entity(self, match: Callable[[EntityState], bool] | None = None) -> EntityState | None:
        """Return the nearest observed entity in the current dimension, excluding yourself.

        Compare three-dimensional distance using observed player feet and the
        observed position of other entities. A tie uses the smaller unique ID.
        An optional synchronous predicate receives each eligible snapshot.
        Return None if the local feet position is unknown or there is no match.
        This query sends no packets.
        """
        if self._entities is None or self._movement is None:
            return None
        return self._entities.nearest_entity(self._movement.observed_feet_position, match)

    @property
    def observed_drops(self) -> tuple[EntityState, ...]:
        """Dropped items visible on this player stream, not all world drops."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.drops()

    async def attack_entity(self, runtime_id: int, *, hit_height: float = 0.5) -> None:
        """Submit a held-item attack; server damage or removal must be observed separately."""
        if self._entities is None or self._movement is None or self._inventory is None:
            raise RuntimeError("player has not joined")
        await self._entities.attack(self._movement, self._inventory, runtime_id,
                                    hit_height=hit_height)

    async def swing(self, source: str = "NONE") -> None:
        """Submit a hand swing without attacking; no BDS acceptance is implied."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        await self._entities.swing(self.player_id, source)

    async def animate(self, action_name: str, data: float = 0.0) -> None:
        """Submit a protocol-supported animation for this player."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        await self._entities.animate(self.player_id, action_name, data)

    async def missed_swing(self) -> int:
        """Left-click air; return the submitted input tick, not BDS acceptance."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.missed_swing()

    async def emote(self, piece_id: str, mute_chat: bool = False) -> None:
        """Submit this player's emote and its requested chat preference."""
        session = self._session
        if session is None or session.player_id is None:
            raise RuntimeError("player has not joined")
        identity = session.identity
        xuid = identity._xuid if isinstance(identity, _OnlineIdentity) else ""
        binding = self.protocol.binding
        await session.packets.send(binding.EmotePacket(
            runtime_id=session.player_id, piece_id=piece_id, emote_ticks=0,
            xuid=xuid, platform_id="", flags=int(binding.EmotePacket.Flags.MUTE_EMOTE_CHAT) if mute_chat else 0,
        ))

    async def change_skin(self, skin_id: str | Skin, rgba: Sequence[int] | None = None) -> None:
        """Submit a classic Skin or a string ID with an optional solid RGBA color."""
        session = self._session
        if session is None or session.player_uuid is None:
            raise RuntimeError("player has not joined")
        await session.packets.send(skin_packet(self.protocol.binding, session.player_uuid, skin_id, rgba))

    async def interact_entity(self, runtime_id: int, *, hit_height: float = 0.5) -> None:
        """Submit a right-click on an actor; observe server events to confirm its effect."""
        if self._entities is None or self._movement is None or self._inventory is None:
            raise RuntimeError("player has not joined")
        await self._entities.interact(self._movement, self._inventory, runtime_id,
                                      hit_height=hit_height)

    @property
    def vehicle(self) -> VehicleLink | None:
        """The local player's current ride link as reported by the server."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.vehicle()

    @property
    def vehicle_links(self) -> tuple[VehicleLink, ...]:
        """Independent snapshots of all ride links observed by this player."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.vehicle_links()

    def subscribe_vehicle_links(
        self, callback: Callable[[VehicleLinkChange], None], *,
        rider_entity_id: int | None = None, vehicle_entity_id: int | None = None,
    ) -> Callable[[], None]:
        """Observe future ride transitions and return an unsubscribe function.

        Filters use server unique IDs. A vehicle filter matches either side of
        a transition. Link packets have reason `link`; cache invalidations
        have reason `unobserved`. One synchronous callback runs per change.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_vehicle_links(
            callback, rider_entity_id=rider_entity_id, vehicle_entity_id=vehicle_entity_id,
        )

    async def mount(self, runtime_id: int) -> None:
        """Submit a right-click on an observed vehicle; watch its link to confirm seating."""
        if self._entities is None or self._movement is None or self._inventory is None:
            raise RuntimeError("player has not joined")
        await self._entities.mount(self._movement, self._inventory, runtime_id)

    async def dismount(self) -> None:
        """Submit a dismount from the known vehicle; observe `vehicle` for the result."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        await self._entities.dismount()

    async def set_boat_input(self, *, forward: bool = False, turn: Literal["left", "right"] | None = None) -> None:
        """Hold paddle controls on the scheduled input frames for an observed boat driver.

        The default releases both inputs without clearing the observed seat. A seat
        or dimension change discards held controls. Ground input resumes from a
        current server-observed player position. Return after setting input for
        subsequent frames. Observe boat motion separately to confirm the effect.
        """
        if self._movement is None:
            raise RuntimeError("player has not joined")
        await self._movement.set_boat_input(forward=forward, turn=turn)

    @property
    def trade_offers(self) -> TradeOffers | None:
        """Return the current trade window's latest server quote, or None if unavailable."""
        trading = self._trading
        return None if trading is None else trading.current_offers

    async def open_trade(self, runtime_id: int, *, timeout: float = 15) -> TradeOffers:
        """Interact with an observed actor and return its server-issued trade offers."""
        if self._trading is None:
            raise RuntimeError("player has not joined")
        return await self._trading.open(runtime_id, timeout=timeout)

    async def execute_trade(self, recipe_net_id: int, destination_slot: int, *, timeout: float = 15):
        """Submit a quoted villager exchange and await the matching server response."""
        if self._trading is None:
            raise RuntimeError("player has not joined")
        return await self._trading.exchange(recipe_net_id, destination_slot, timeout=timeout)

    async def trade_many(self, recipe_net_id: int, count: int, *, timeout: float = 15) -> TradeManyResult:
        """Attempt up to count exchanges for the current quote in the open trade window.

        The operation stays in that window. The result counts confirmed trades
        and records partial staging. An interruption can attach known progress
        to the original exception as trade_progress.
        """
        if self._trading is None:
            raise RuntimeError("player has not joined")
        return await self._trading.trade_many(recipe_net_id, count, timeout=timeout)

    async def stage_trade_ingredient(
        self, source_slot: int, amount: int, *, ingredient: int = 1,
        timeout: float = 15,
    ):
        """Place an inventory stack into an open trade input; await BDS's response."""
        if self._trading is None:
            raise RuntimeError("player has not joined")
        return await self._trading.stage_ingredient(
            source_slot, amount, ingredient=ingredient, timeout=timeout
        )

    async def unstage_trade_ingredient(
        self, destination_slot: int, *, ingredient: int = 1, timeout: float = 15
    ):
        """Return a staged trade input to inventory; await the BDS stack response."""
        if self._trading is None:
            raise RuntimeError("player has not joined")
        return await self._trading.unstage_ingredient(
            destination_slot, ingredient=ingredient, timeout=timeout
        )

    async def collect_drop(
        self, runtime_id: int, *, ticks: int = 120, timeout: float = 15
    ) -> EntityState:
        """Approach the observed drop, routing around known obstacles when possible, and confirm pickup."""
        if self._entities is None or self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._entities.collect_drop(
            self._movement, runtime_id, self.player_id, ticks=ticks, timeout=timeout, blocks=self._blocks,
        )

    async def wait_for_item_drop(self, timeout: float = 15) -> EntityState:
        """Observe the next item actor spawn sent by the server."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_item_drop(timeout)

    async def wait_for_item_pickup(
        self, *, item_runtime_id: int | None = None, collector_runtime_id: int | None = None,
        item_entity_id: int | None = None, collector_entity_id: int | None = None, timeout: float = 15,
    ) -> ItemPickup:
        """Wait for the next server-reported item pickup matching the supplied IDs."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_item_pickup(
            item_runtime_id=item_runtime_id, collector_runtime_id=collector_runtime_id,
            item_entity_id=item_entity_id, collector_entity_id=collector_entity_id, timeout=timeout,
        )

    async def wait_for_entity_spawn(
        self, *, actor_type: str | None = None, timeout: float = 15
    ) -> EntitySpawn:
        """Wait for the next matching AddActorPacket.

        Passing actor_type="minecraft:painting" also accepts AddPaintingPacket (22).
        Visible paintings are available through observed_entities independently of this wait.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_spawn(actor_type=actor_type, timeout=timeout)

    async def wait_for_entity_removal(self, entity_id: int, *, timeout: float = 15) -> None:
        """Observe the server removing the actor with this unique ID from view."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        await self._entities.wait_for_removal(entity_id, timeout=timeout)

    async def wait_for_entity_swing(
        self, *, runtime_id: int | None = None, timeout: float | None = 15,
    ) -> EntitySwing:
        """Wait for the next server swing in the current dimension.

        A runtime filter binds the current entity instance or the local player.
        An unfiltered wait can return a swing without an observed entity.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_entity_swing(runtime_id=runtime_id, timeout=timeout)

    def subscribe_shield_blocks(
        self, callback: Callable[[ShieldBlockEvent], None], *, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        """Observe server-reported shield blocks by the local player or a tracked remote player."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_shield_blocks(callback, runtime_id=runtime_id)

    async def wait_for_shield_block(
        self, *, runtime_id: int | None = None, timeout: float | None = 15,
    ) -> ShieldBlockEvent:
        """Wait for the next shield block by the local player or a tracked remote player."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_shield_block(runtime_id=runtime_id, timeout=timeout)

    def subscribe_entity_swings(
        self, callback: Callable[[EntitySwing], None], *, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        """Observe received swings and return an idempotent unsubscribe function.

        A runtime-filtered subscription ends when its instance leaves view or the
        dimension changes. Unfiltered subscriptions continue for the session.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_entity_swings(callback, runtime_id=runtime_id)

    async def wait_for_entity_critical_effect(
        self, *, kind: EntityCriticalEffectKind | None = None, runtime_id: int | None = None,
        timeout: float | None = 15,
    ) -> EntityCriticalEffect:
        """Wait for the next matching server critical-hit visual in the current dimension.

        A runtime filter binds the current entity instance or the local player.
        Without that filter, the visual can refer to an unobserved actor.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_entity_critical_effect(kind=kind, runtime_id=runtime_id, timeout=timeout)

    def subscribe_entity_critical_effects(
        self, callback: Callable[[EntityCriticalEffect], None], *,
        kind: EntityCriticalEffectKind | None = None, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        """Deliver critical-hit visuals to a synchronous callback and return an idempotent unsubscribe function.

        A runtime-filtered subscription ends when the actor leaves view, its
        identity changes, or the dimension changes. Other subscriptions last
        until the session ends or they are unsubscribed.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_entity_critical_effects(callback, kind=kind, runtime_id=runtime_id)

    async def wait_for_entity_health_event(
        self, *, kind: EntityHealthEventKind | None = None, runtime_id: int | None = None, timeout: float | None = 15,
    ) -> EntityHealthEvent:
        """Wait for the next received hurt or death of a tracked remote entity.

        A runtime filter binds the current instance. This wait ends if that
        instance leaves view, the dimension changes, or the connection closes.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_entity_health_event(kind=kind, runtime_id=runtime_id, timeout=timeout)

    def subscribe_entity_health_events(
        self, callback: Callable[[EntityHealthEvent], None], *,
        kind: EntityHealthEventKind | None = None, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        """Observe received hurt and death events and return an idempotent unsubscribe function.

        The callback is synchronous. A runtime-filtered subscription ends
        when that instance leaves view or the dimension changes.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_entity_health_events(callback, kind=kind, runtime_id=runtime_id)

    async def wait_for_entity_actor_event(
        self, *, runtime_id: int | None = None, event_id: int | None = None, timeout: float | None = 15,
    ) -> EntityActorEvent:
        """Wait for the next matching actor event from a tracked remote entity.

        A runtime filter binds the current instance. This wait ends if that
        instance leaves view, the dimension changes, or the connection closes.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_entity_actor_event(runtime_id=runtime_id, event_id=event_id, timeout=timeout)

    def subscribe_entity_actor_events(
        self, callback: Callable[[EntityActorEvent], None], *, runtime_id: int | None = None,
        event_id: int | None = None,
    ) -> Callable[[], None]:
        """Observe matching actor events and return an idempotent unsubscribe function.

        The callback is synchronous. A runtime-filtered subscription ends
        when that instance leaves view or the dimension changes.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_entity_actor_events(callback, runtime_id=runtime_id, event_id=event_id)

    async def wait_for_entity_attribute_update(
        self, *, runtime_id: int | None = None, timeout: float | None = 15,
    ) -> EntityAttributeUpdate:
        """Wait for the next attribute packet applied to a tracked remote entity.

        A runtime filter binds the current instance. This wait ends if that
        instance leaves view, the dimension changes, or the connection closes.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_entity_attribute_update(runtime_id=runtime_id, timeout=timeout)

    def subscribe_entity_attribute_updates(
        self, callback: Callable[[EntityAttributeUpdate], None], *, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        """Observe received attribute updates and return an idempotent unsubscribe function.

        The callback is synchronous. A runtime-filtered subscription ends
        when that instance leaves view or the dimension changes.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_entity_attribute_updates(callback, runtime_id=runtime_id)

    async def wait_for_entity_effect_update(
        self, *, runtime_id: int | None = None, timeout: float | None = 15,
    ) -> EntityEffectUpdate:
        """Wait for the next effect packet applied to a tracked remote entity.

        A runtime filter binds the current instance. This wait ends if that
        instance leaves view, the dimension changes, or the connection closes.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_entity_effect_update(runtime_id=runtime_id, timeout=timeout)

    def subscribe_entity_effect_updates(
        self, callback: Callable[[EntityEffectUpdate], None], *, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        """Observe received effect updates and return an idempotent unsubscribe function.

        The callback is synchronous. A runtime-filtered subscription ends
        when that instance leaves view or the dimension changes.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_entity_effect_updates(callback, runtime_id=runtime_id)

    def subscribe_entity_visible_effect_changes(
        self, callback: Callable[[EntityVisibleEffectUpdate], None], *, runtime_id: int | None = None,
    ) -> Callable[[], None]:
        """Observe future visual-effect metadata changes for tracked remote entities."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_entity_visible_effect_changes(callback, runtime_id=runtime_id)

    async def wait_for_entity_visible_effect_change(
        self, *, runtime_id: int | None = None, timeout: float | None = 15,
    ) -> EntityVisibleEffectUpdate:
        """Wait for the next matching visual-effect metadata change."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_entity_visible_effect_change(runtime_id=runtime_id, timeout=timeout)

    def subscribe_entity_movement(self, callback: Callable[[EntityMovementUpdate], None]) -> Callable[[], None]:
        """Observe future movement packets for visible remote entities during this session.

        The synchronous callback receives an immutable update after the cache is
        updated. The returned unsubscribe function is idempotent. Subscriptions
        continue across dimension changes and entity replacement.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.subscribe_entity_movement(callback)

    async def wait_for_entity_movement(
        self, runtime_id: int, *, timeout: float = 15
    ) -> EntityMovement:
        """Observe one actor movement event, not a continuously tracked position."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_movement(runtime_id, timeout=timeout)

    async def wait_for_entity_movement_matching(
        self, runtime_id: int, predicate: Callable[[EntityState], bool], *,
        entity_id: int | None = None, timeout: float | None = 15,
    ) -> EntityState:
        """Return the first snapshot of a visible entity that matches the synchronous predicate.

        The current snapshot is checked first, followed by each server movement
        update in packet order. Pass the entity's unique ID to verify the runtime
        ID binding. The wait ends when the entity leaves view or is replaced,
        the dimension changes, or the session ends.
        """
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_movement_matching(
            runtime_id, predicate, entity_id=entity_id, timeout=timeout,
        )

    async def wait_for_server_disconnect(self, timeout: float = 15):
        if self._session is None:
            raise RuntimeError("player has not joined")
        return await self._session.wait_for_server_disconnect(timeout)

    def subscribe_error(self, callback: Callable[[RecoverableError], None]) -> Callable[[], None]:
        """Observe future recoverable inbound parse errors synchronously.

        Register before or after joining. The subscription follows this Bot across joins.
        The returned function unsubscribes. Use subscribe_session_end for reader termination.
        """
        token = object()
        self._error_observers[token] = callback
        owner_ref = ref(self)

        def unsubscribe() -> None:
            owner = owner_ref()
            if owner is not None:
                owner._error_observers.pop(token, None)

        return unsubscribe

    def _publish_error(self, session: JoinSession, diagnostic: RecoverableError) -> None:
        if self._session is not session or session._end_state.result is not None:
            return
        observers = self._error_observers
        for token, callback in tuple(observers.items()):
            if token not in observers:
                continue
            try:
                callback(diagnostic)
            except (Exception, asyncio.CancelledError) as error:
                asyncio.get_running_loop().call_exception_handler({
                    "message": "error observer failed", "exception": error, "callback": callback,
                })

    def subscribe_session_end(self, callback: Callable[[SessionEnd], None]) -> Callable[[], None]:
        """Call back synchronously with the first end of each joined session.

        Register before or after joining. The subscription persists across
        joins and only observes future ends. The returned function unsubscribes.
        """
        observers = self._session_end_observers
        token = object()
        observers[token] = callback

        def unsubscribe() -> None:
            observers.pop(token, None)

        return unsubscribe

    def _publish_session_end(self, result: SessionEnd) -> None:
        self._placement_completion_observers.clear()
        for callback in tuple(self._session_end_observers.values()):
            try:
                callback(result)
            except (Exception, asyncio.CancelledError) as error:
                asyncio.get_running_loop().call_exception_handler({
                    "message": "session end observer failed", "exception": error, "callback": callback,
                })

    def _get_session_end_state(self) -> _SessionEndState | None:
        state = self._session_end_state
        session = self._session
        if state is not None and state.result is None and session is not None:
            session._sync_session_end()
        return state

    @property
    def session_end(self) -> SessionEnd | None:
        """Return the first observed end through teardown and until the next join attempt."""
        state = self._get_session_end_state()
        return None if state is None else state.result

    async def wait_for_session_end(self, timeout: float | None = 15) -> SessionEnd:
        """Wait for the last joined session's first end, or return its existing snapshot."""
        state = self._get_session_end_state()
        if state is None:
            raise RuntimeError("player has not joined")
        return await state.wait(timeout)

    async def wait_for_input_end(self) -> None:
        """Wait for the session's scheduled input to stop, or raise its send failure."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        await self._movement.wait_for_input_end()

    def _require_clock(self) -> ClientClock:
        clock = self._clock
        if clock is None:
            raise RuntimeError("player has no active connection")
        return clock

    def get_tick(self) -> int:
        """Return the active connection's executed logical tick count, including during loading."""
        return self._require_clock().get_tick()

    async def wait_ticks(self, ticks: int) -> int:
        """Wait for the current connection to execute more logical ticks and return its tick count."""
        return await self._require_clock().wait_ticks(ticks)

    def subscribe_ticks(self, callback: Callable[[int], None]) -> Callable[[], None]:
        """Call back synchronously on future executed client ticks, including during loading.

        Each call receives the current count from get_tick(). The subscription ends
        when the connection's clock stops. The returned function unsubscribes sooner.
        Raise RuntimeError without an active connection.
        """
        return self._require_clock().subscribe_ticks(callback)

    @property
    def packets(self):
        """Expose typed and raw packet I/O for external extensions."""
        if self._session is None:
            raise RuntimeError("player has not joined")
        return self._session.packets

    async def close(self, *, reason: str | None = None) -> None:
        """Close the connection, using reason as the message if local close ends the session."""
        session = self._session
        state = self._session_end_state
        if state is not None and state.result is None and session is not None:
            session._on_local_close(reason)
        component_errors: list[BaseException] = []

        @contextmanager
        def cleanup_step(errors: list[BaseException]):
            try:
                yield
            except BaseException as error:
                errors.append(error)

        def close_component(component):
            if component is not None:
                with cleanup_step(component_errors):
                    component.close()

        terrain, self._terrain = self._terrain, None
        if terrain is not None:
            with cleanup_step(component_errors):
                terrain.stop()
        clock, self._clock = self._clock, None
        if clock is not None:
            with cleanup_step(component_errors):
                clock.stop()
        pending, self._online_join = self._online_join, None
        joining = None if pending is None else pending.task
        if joining is asyncio.current_task():
            joining = None
        if joining is not None and not joining.cancelling():
            joining.cancel()
        session, self._session = self._session, None
        movement, self._movement = self._movement, None
        inventory, self._inventory = self._inventory, None
        self._books = None
        item_use, self._item_use = self._item_use, None
        containers, self._containers = self._containers, None
        creative, self._creative = self._creative, None
        enchanting, self._enchanting = self._enchanting, None
        blocks, self._blocks = self._blocks, None
        beds, self._beds = self._beds, None
        collector, self._block_collector = self._block_collector, None
        chat, self._chat = self._chat, None
        titles, self._titles = self._titles, None
        boss_bars, self._boss_bars = self._boss_bars, None
        forms, self._forms = self._forms, None
        self._harvest_tools = None
        close_component(collector)
        signs, self._signs = self._signs, None
        for component in (signs, boss_bars, forms, beds, blocks, creative, enchanting, containers, chat, titles):
            close_component(component)
        commands, self._commands = self._commands, None
        close_component(commands)
        entities, self._entities = self._entities, None
        fishing, self._fishing = self._fishing, None
        close_component(fishing)
        player_state, self._player_state = self._player_state, None
        trading, self._trading = self._trading, None
        close_component(trading)
        recipes, self._recipes = self._recipes, None
        scoreboard, self._scoreboard = self._scoreboard, None
        close_component(scoreboard)
        environment, self._environment = self._environment, None
        feedback, self._feedback = self._feedback, None
        for component in (feedback, environment, recipes, player_state, entities, item_use):
            close_component(component)
        # Cancel the input ticker before draining another component can yield to it.
        movement_errors: list[BaseException] = []
        if movement is not None:
            with cleanup_step(movement_errors):
                movement.close()
        drain_errors: list[BaseException] = []
        if pending is not None:
            if joining is not None:
                with cleanup_step(drain_errors):
                    await asyncio.wait((joining,))
                if pending.transport_close_error is not None:
                    drain_errors.append(pending.transport_close_error)
            else:
                # A provider or pack callback can close directly from the joining task.
                transport = pending.take_transport()
                if transport is not None:
                    with cleanup_step(drain_errors):
                        await transport.close()
        if player_state is not None:
            with cleanup_step(drain_errors):
                await player_state.aclose()
        if movement is not None:
            with cleanup_step(movement_errors):
                await movement.aclose()
            drain_errors.extend(movement_errors)
        if terrain is not None:
            with cleanup_step(drain_errors):
                await terrain.close()
        session_errors: list[BaseException] = []
        if session is not None:
            with cleanup_step(session_errors):
                await session.close()
        inventory_errors: list[BaseException] = []
        if inventory is not None:
            with cleanup_step(inventory_errors):
                inventory.close()
            with cleanup_step(inventory_errors):
                await inventory._wait_for_request_sends()
        # Keep the error precedence of the previous nested finally blocks.
        errors = component_errors + inventory_errors + session_errors + drain_errors
        if errors:
            failure, *secondary = errors
            for error in secondary:
                if error is not failure:
                    failure.add_note(f"another session cleanup step failed: {error!r}")
            raise failure
