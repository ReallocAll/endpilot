"""World settings, time and weather observed on the server stream."""

from collections.abc import Callable
from dataclasses import dataclass, replace

from bedrock_protocol import GeneratorType

from .compression import PacketConnection


_TICKS_PER_DAY = 24_000
_MOON_PHASE_COUNT = 8
_OVERWORLD_CLOCK = "minecraft:overworld"
_START_RAIN = 3001
_START_THUNDER = 3002
_STOP_RAIN = 3003
_STOP_THUNDER = 3004
_OVERWORLD_DIMENSION = 0
_UNFIXED_SPAWN_HEIGHT = 32767


@dataclass(frozen=True, slots=True)
class WorldClock:
    id: int
    name: str
    time: int
    paused: bool


@dataclass(frozen=True, slots=True)
class WorldTime:
    """Last reported daylight time, gamerule and named clocks for this world."""

    daylight_ticks: int | None = None
    game_ticks_at_join: int | None = None
    do_daylight_cycle: bool | None = None
    clocks: tuple[WorldClock, ...] = ()

    @property
    def daytime(self) -> int | None:
        """Ticks within the current in-game day, if its time is known."""
        return None if self.daylight_ticks is None else self.daylight_ticks % _TICKS_PER_DAY

    @property
    def day(self) -> int | None:
        """Zero-based day number according to the daylight clock."""
        return None if self.daylight_ticks is None else self.daylight_ticks // _TICKS_PER_DAY

    @property
    def observed_moon_phase(self) -> int | None:
        """Latest observed Bedrock moon phase index (0 full moon, 4 new moon).

        Return None until a nonnegative named Overworld clock is known. The
        phase is tied to this snapshot's received clock value.
        """
        for clock in self.clocks:
            if clock.name == _OVERWORLD_CLOCK and clock.time >= 0:
                return (clock.time // _TICKS_PER_DAY) % _MOON_PHASE_COUNT
        return None


@dataclass(frozen=True, slots=True)
class Weather:
    """Server-announced weather, with optional levels from the login settings."""

    raining: bool | None = None
    thundering: bool | None = None
    rain_level: float | None = None
    lightning_level: float | None = None


@dataclass(frozen=True, slots=True)
class WorldRule:
    name: str
    value: bool | int | float | None
    can_be_modified_by_player: bool


@dataclass(frozen=True, slots=True)
class WorldSpawn:
    """Latest advertised public spawn, with None for an unfixed Y coordinate."""

    position: tuple[int, int | None, int]
    dimension_id: int


def world_time_packet_ids(binding) -> tuple[int, ...]:
    return (binding.StartGamePacket.ID, binding.SetTimePacket.ID,
            binding.GameRulesChangedPacket.ID, binding.SyncWorldClocksPacket.ID)


def world_environment_packet_ids(binding) -> tuple[int, ...]:
    return (*world_time_packet_ids(binding), binding.LevelEventPacket.ID,
            binding.SetDifficultyPacket.ID, binding.SetSpawnPositionPacket.ID)


class WorldEnvironment:
    def __init__(self, packets, initial_packets):
        self.packets = packets
        binding = packets.protocol.binding
        self._time_ids = world_time_packet_ids(binding)
        self._weather_ids = (binding.StartGamePacket.ID, binding.LevelEventPacket.ID)
        self._packet_ids = world_environment_packet_ids(binding)
        self._disconnect_id = binding.DisconnectPacket.ID
        self._rule_ids = (binding.StartGamePacket.ID, binding.GameRulesChangedPacket.ID)
        self._disconnected = False
        self._daylight_ticks = None
        self._game_ticks_at_join = None
        self._do_daylight_cycle = None
        self.difficulty: int | None = None
        self.hardcore: bool | None = None
        self.advertised_generator: GeneratorType | int | None = None
        self.world_spawn: WorldSpawn | None = None
        self._world_spawn_observers: dict[object, Callable[[WorldSpawn], None]] = {}
        self.limited_crafting: bool | None = None
        self._clocks: dict[int, WorldClock] = {}
        self._rules: dict[str, WorldRule] = {}
        self.weather = Weather()
        for packet in initial_packets:
            self._apply(packet, at_join=True)
        self._subscriptions = (packets.subscribe_raw(self._on_packet),
                               packets.subscribe_reader_end(self._on_reader_end))

    def close(self) -> None:
        self._invalidate()
        for unsubscribe in self._subscriptions:
            unsubscribe()

    def _on_reader_end(self, _cause) -> None:
        self._invalidate()

    def _invalidate(self) -> None:
        self._disconnected = True
        self.difficulty = None
        self.hardcore = None
        self.advertised_generator = None
        self.world_spawn = None
        self._world_spawn_observers.clear()

    def subscribe_world_spawn_changes(self, callback: Callable[[WorldSpawn], None]) -> Callable[[], None]:
        observers = self._world_spawn_observers
        token = object()
        observers[token] = callback

        def unsubscribe() -> None:
            observers.pop(token, None)

        return unsubscribe

    def time(self) -> WorldTime:
        return WorldTime(self._daylight_ticks, self._game_ticks_at_join,
                         self._do_daylight_cycle, tuple(self._clocks.values()))

    def rules(self) -> tuple[WorldRule, ...]:
        return tuple(self._rules[name] for name in sorted(self._rules))

    def _on_packet(self, raw) -> None:
        if raw.packet_id == self._disconnect_id:
            self._invalidate()
        elif raw.packet_id in self._packet_ids:
            self._apply(self.packets.protocol.decode(raw))

    def _apply(self, packet, *, at_join: bool = False) -> None:
        b = self.packets.protocol.binding
        if isinstance(packet, b.StartGamePacket):
            settings = packet.settings
            if at_join:
                self.advertised_generator = settings.generator
            if not self._disconnected:
                self.difficulty = int(settings.game_difficulty)
                self.hardcore = getattr(settings, "is_hardcore", None)
                # The default spawn is in the Overworld even when the player joins elsewhere.
                self._set_world_spawn(settings.default_spawn, _OVERWORLD_DIMENSION)
            self._game_ticks_at_join = int(packet.level_current_time)
            self._daylight_ticks = int(settings.time) if settings.time >= 0 else None
            self._apply_rules(settings.game_rules.rules)
            rain = float(settings.rain_level)
            thunder = float(settings.lightning_level)
            self.weather = Weather(rain > 0, thunder > 0, rain, thunder)
        elif isinstance(packet, b.SetTimePacket):
            self._daylight_ticks = int(packet.time)
        elif isinstance(packet, b.GameRulesChangedPacket):
            self._apply_rules(packet.rule_data.rules)
        elif isinstance(packet, b.SyncWorldClocksPacket):
            self._apply_clocks(packet.data)
        elif isinstance(packet, b.LevelEventPacket):
            self._apply_weather(packet.event_id)
        elif not self._disconnected:
            if isinstance(packet, b.SetDifficultyPacket):
                self.difficulty = int(packet.difficulty)
            elif (isinstance(packet, b.SetSpawnPositionPacket)
                  and packet.spawn_pos_type == b.SpawnPositionType.WORLD_SPAWN):
                self._set_world_spawn(packet.pos, int(packet.dimension_type), notify=not at_join)

    def _set_world_spawn(self, pos, dimension_id: int, *, notify: bool = False) -> None:
        y = None if pos.y == _UNFIXED_SPAWN_HEIGHT else int(pos.y)
        spawn = WorldSpawn((int(pos.x), y, int(pos.z)), dimension_id)
        if spawn != self.world_spawn:
            self.world_spawn = spawn
            if notify:
                PacketConnection._notify(self._world_spawn_observers.values(), spawn)

    def _apply_rules(self, rules) -> None:
        for rule in rules:
            name = rule.name.lower()
            self._rules[name] = WorldRule(rule.name, rule.value, rule.can_be_modified_by_player)
            if name == "dodaylightcycle" and isinstance(rule.value, bool):
                self._do_daylight_cycle = rule.value
            elif name == "dolimitedcrafting":
                self.limited_crafting = rule.value if isinstance(rule.value, bool) else None

    def _apply_clocks(self, data) -> None:
        packet_type = self.packets.protocol.binding.SyncWorldClocksPacket
        if isinstance(data, packet_type.InitializeRegistryData):
            self._clocks.clear()
            updated = (WorldClock(clock.id, clock.name, clock.time, clock.is_paused)
                       for clock in data.clock_data)
        elif isinstance(data, packet_type.SyncStateData):
            updated = (replace(self._clocks[entry.clock_id], time=entry.time, paused=entry.is_paused)
                       for entry in data.clock_data if entry.clock_id in self._clocks)
        else:
            return
        for clock in updated:
            self._clocks[clock.id] = clock
            if clock.name == _OVERWORLD_CLOCK:
                self._daylight_ticks = clock.time

    def _apply_weather(self, event_id: int) -> None:
        # Weather transition payloads do not supply a StartGame float level.
        if event_id in (_START_RAIN, _STOP_RAIN):
            raining = event_id == _START_RAIN
            if self.weather.raining != raining:
                self.weather = replace(self.weather, raining=raining, rain_level=None)
        elif event_id in (_START_THUNDER, _STOP_THUNDER):
            thundering = event_id == _START_THUNDER
            if self.weather.thundering != thundering:
                self.weather = replace(self.weather, thundering=thundering, lightning_level=None)

    async def _wait_for_change(self, packet_ids, snapshot, timeout, observation):
        # Let a stopped reader report its transport failure. A DisconnectPacket can arrive
        # while the reader is still open.
        if self._disconnected and not self.packets._reader_end_result()[0]:
            raise ConnectionError("server already disconnected")
        before = snapshot()
        update = before

        def changed(raw):
            nonlocal update
            if raw.packet_id == self._disconnect_id:
                return True
            # The standing observer applies this packet before the waiter runs.
            update = snapshot()
            return update != before

        raw = await self.packets.wait_for_packet((*packet_ids, self._disconnect_id), timeout=timeout, match=changed)
        if raw.packet_id == self._disconnect_id:
            raise ConnectionError(f"server disconnected while waiting for {observation}")
        return update

    async def wait_for_time_update(self, timeout: float) -> WorldTime:
        return await self._wait_for_change(self._time_ids, self.time, timeout, "world time")

    async def wait_for_weather_update(self, timeout: float) -> Weather:
        return await self._wait_for_change(self._weather_ids, lambda: self.weather, timeout, "weather")

    async def wait_for_rules_update(self, timeout: float) -> tuple[WorldRule, ...]:
        if self._disconnected:
            raise ConnectionError("server already disconnected")
        return await self._wait_for_change(self._rule_ids, self.rules, timeout, "world rules")
