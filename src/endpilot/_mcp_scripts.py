"""Run local MCP scripts in workers that borrow the resident Bot's gameplay API."""

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from enum import Enum
from fractions import Fraction
import inspect
import json
import multiprocessing
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict
from uuid import uuid4

from . import navigation
from .beds import SleepRejected
from .blocks import BlockObservation, BlockUpdate, ColumnCoverageInvalidated, MiningFaceUnavailable
from .collection import BlockCollectionBatch, CountedBlockCollection, MixedCollectionBatch
from .entities import EntityState
from .inventory import _stack_snapshot
from .placement import BoatPlacementResult, EntityPlacementResult, PlacementResult
from .world_environment import WorldTime
from .world_feedback import BlockEvent, ParticleEvent, SoundEvent

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from .bot import Bot
    from .mcp_server import MCPPlayer


_MAX_RUNS = 32
_MAX_SOURCE = 1024 * 1024
_MAX_FRAME = 2 * 1024 * 1024
_MAX_OUTGOING_FRAMES = 64
_MAX_PENDING = 64
_OUTPUT_CHUNKS = 128
_CHUNK_SIZE = 2048
_State = Literal["running", "succeeded", "failed", "stopped"]
_Stream = Literal["stdout", "stderr", "log"]


# The bridge owns this allowlist independently of the worker. Listed methods
# have data-only parameters and copyable results, subject to the checks below.
_MEMBERS = {
    name: kind
    for kind, names in {
        "property": """
            active_dimension_change is_changing_dimension boss_bars world_time weather world_rules
            world_difficulty world_hardcore world_spawn player_id spawn_chunk_radius
            requested_chunk_radius accepted_chunk_radius published_chunk_bounds position movement_state
            rotation is_swimming dimension_bounds is_sleeping sleeping_bed player_respawn_point
            held_item inventory_slots armor_slots offhand_item open_window container_slots
            furnace_progress brewing_progress mining_target health hunger experience_level vitals abilities
            status_effects is_dead observed_entities observed_drops vehicle vehicle_links commands_enabled
        """,
        "sync": """
            get_tick get_pending_form plan_navigation observed_block observed_block_state observed_biome
            find_blocks find_block find_block_vein raycast_block raycast_player_view raycast_block_towards
            column_coverage observed_bed inventory_count mining_face best_harvest_tool
            observed_player_equipment entity_at_cursor nearest_player nearest_entity switch_follow_target
            complete_command_name complete_command
        """,
        "async": """
            wait_ticks wait_for_dimension_change wait_for_world_time_update wait_for_weather_update
            wait_for_world_rules_update wait_for_sound wait_for_particle wait_for_block_event
            request_chunk_radius wait_for_chunk_radius_update
            set_gliding set_crawling set_move_vector move_to wait_for_position_sync set_rotation look_at
            walk_forward walk_backward strafe_left strafe_right walk_forward_left walk_forward_right
            walk_backward_left walk_backward_right set_sneaking set_sprinting set_flying fly_to set_swimming
            jump jump_forward jump_backward jump_left jump_right jump_forward_left jump_forward_right
            jump_backward_left jump_backward_right hop_forward hop_backward hop_left hop_right
            hop_forward_left hop_forward_right hop_backward_left hop_backward_right stop_moving
            follow_entity navigate_to navigate approach_block wait_for_block_update wait_for_column_coverage
            wait_for_terrain_coverage request_sub_chunk sleep stop_sleeping wake
            wait_for_inventory_item_count select_hotbar wait_for_held_item_change move_inventory_item
            swap_inventory_items equip_armor unequip_armor equip_offhand unequip_offhand drop_inventory_item
            drop_selected_item use_item consume release_using_item
            wait_for_container_slots wait_for_furnace_progress wait_for_brewing_progress take_from_container
            put_into_container open_inventory_screen close_container open_container open_entity_container
            use_item_on place_block place_against start_mining continue_mining stop_mining finish_mining
            mine_block collect_block collect_targets collect_by_count collect_vein_by_count equip_harvest_tool
            answer_form close_form send_chat send_whisper wait_for_chat wait_for_message send_command
            execute_command_output
            wait_for_health_update wait_for_vitals_update wait_for_abilities wait_for_status_effects_update
            wait_for_hunger_update respawn wait_for_death wait_for_respawn submit_respawn attack_entity
            swing animate missed_swing emote interact_entity mount dismount set_boat_input collect_drop
            wait_for_item_drop wait_for_item_pickup wait_for_entity_spawn wait_for_entity_removal
            wait_for_entity_movement
        """,
        "subscribe": """
            subscribe_ticks subscribe_chat subscribe_sound subscribe_particle subscribe_block_events
            subscribe_column_coverage subscribe_block_changes subscribe_inventory_changes
            subscribe_container_slot_changes subscribe_furnace_progress subscribe_spawns
            subscribe_entity_lifecycle subscribe_entity_movement subscribe_vehicle_links
        """,
    }.items()
    for name in names.split()
}

_RECORDS = {
    "beds": "BedObservation PlayerRespawnPoint",
    "block_raycast": "BlockRaycastResult MiningFaceResult",
    "blocks": "BlockUpdate BlockObservation BlockStateObservation DimensionBounds ColumnCoverage "
              "ColumnCoverageEvent BlockChange MiningTarget BlockMatch BlockSearchResult",
    "boss_bars": "BossBar",
    "chat": "ChatMessage",
    "collection": "BlockCollection BlockCollectionBatch CountedBlockCollection DropCollection MixedCollectionBatch",
    "containers": "ContainerWindow ContainerSlotChange FurnaceProgress BrewingProgress",
    "dimension_transition": "DimensionTransition",
    "entities": "EntityState PaintingSnapshot EntityAttribute EntityAttributeModifier EntityMovementUpdate "
                "EntityMovement EntityLifecycleEvent EntityCursorResult EntitySpawn ItemPickup "
                "PlayerEquipmentSnapshot VehicleLink VehicleLinkChange",
    "harvest": "HarvestTool HarvestToolAcquisition HarvestToolChest",
    "inventory": "HeldItemStack HeldItemSnapshot InventorySlotChange",
    "item_use": "ItemConsumption",
    "movement": "BlockApproach FlightResult MovementState",
    "navigation": "NavigationPlan NavigationResult BlockFaceMatch BlockInteractionMatch BlockPlacementMatch",
    "placement": "PlacementResult PlacementInventoryChange EntityPlacementResult BoatPlacementResult",
    "player_state": "PlayerVitals PlayerEffect PlayerAbilities DeathEvent",
    "world_environment": "WorldClock WorldTime Weather WorldRule WorldSpawn",
    "world_feedback": "SoundEvent ParticleEvent BlockEvent",
}
_RECORDS = {(f"endpilot.{module}", name) for module, names in _RECORDS.items() for name in names.split()}
_INPUT_RECORDS = {cls.__name__: cls for cls in (
    navigation.FootGoal, navigation.XZGoal, navigation.NearXZGoal, navigation.YGoal, navigation.NearGoal,
    navigation.BlockFaceGoal, navigation.BlockInteractionGoal, navigation.BlockPlacementGoal,
    navigation.EntityNearGoal, navigation.EntityVisibilityGoal, navigation.AnyGoal, navigation.AllGoal,
    navigation.NavigationRules, navigation.NavigationCosts,
)}
_UNSUPPORTED_PARAMETERS = frozenset({"predicate", "match", "on_plan", "on_event", "tool_chest", "tool_chests", "storage"})
_SLOT_MAPS = frozenset({"inventory_slots", "armor_slots", "container_slots", "wait_for_container_slots"})
_ITEM_VERDICTS = frozenset({
    "move_inventory_item", "swap_inventory_items", "equip_armor", "unequip_armor", "equip_offhand",
    "unequip_offhand", "drop_inventory_item", "take_from_container", "put_into_container",
})
_ERROR_FIELDS = {SleepRejected: "translation_key", ColumnCoverageInvalidated: "reason", MiningFaceUnavailable: "status"}
_ERROR_PROGRESS = {"collection_progress": (BlockCollectionBatch, CountedBlockCollection, MixedCollectionBatch),
                   "placement_progress": (PlacementResult, EntityPlacementResult, BoatPlacementResult)}
_ERROR_ATTRIBUTES = frozenset((*_ERROR_FIELDS.values(), *_ERROR_PROGRESS))


class _Started(TypedDict):
    run_id: str


class _Status(_Started):
    state: _State


class _Output(TypedDict):
    stream: _Stream
    text: str


class _Error(TypedDict):
    error_type: str
    message: str
    attributes: NotRequired[dict[str, Any]]


class _Observation(_Status):
    cursor: int
    lost: int
    output: list[_Output]
    result: Any
    error: _Error | None


def _record(values):
    return {"$endpilot": "record", "fields": values}


def _feedback_fields(event):
    packet = event.packet
    if isinstance(event, SoundEvent):
        values = {"volume": event.volume, "pitch": event.pitch}
        if event.kind == "play":
            names = ("loop_count", "bypass_listener_range_check", "server_sound_handle", "playback_position_seconds")
        elif event.kind == "stop":
            names = ("stop_all", "stop_music_legacy")
        elif event.kind == "level":
            names = ("sound_event", "data", "actor_identifier", "actor", "is_global", "is_baby")
            fire = getattr(packet, "fire_at_position", None)
            values["fire_at_position"] = None if fire is None else (fire.x, fire.y, fire.z)
        else:
            names = ("server_sound_handle",)
            update = packet.event
            values.update(update_kind=type(update).__name__, duration=getattr(update, "duration", None),
                          target_volume=getattr(update, "target_volume", None), seconds=getattr(update, "seconds", None))
    elif isinstance(event, ParticleEvent):
        values = {}
        names = ("vanilla_dimension_id", "actor_id", "molang_variables")
    else:
        values = {"note_pitch": event.note_pitch, "instrument_id": event.instrument_id,
                  "instrument_sound": event.instrument_sound}
        names = ("event_id", "b0", "b1")
        data = getattr(packet, "data", None)
        values["data"] = data if type(data) is int else None
    values.update((name, getattr(packet, name, None)) for name in names)
    return {name: _project(value) for name, value in values.items()}


def _project(value):
    """Project owned gameplay data without exposing SDK or native objects."""
    if isinstance(value, Enum):
        return value.name
    if value is None or type(value) in (bool, int, float, str):
        return value
    if type(value) is bytes:
        return value.hex()
    if type(value) is Fraction:
        return _record({"numerator": value.numerator, "denominator": value.denominator})
    if type(value) in (tuple, list, frozenset):
        return [_project(item) for item in value]
    if type(value) in (dict, MappingProxyType) and all(type(key) in (str, int) for key in value):
        return {str(key): _project(item) for key, item in value.items()}
    kind = type(value)
    if (kind.__module__, kind.__name__) in _RECORDS or _INPUT_RECORDS.get(kind.__name__) is kind:
        output = {field.name: _project(getattr(value, field.name))
                  for field in fields(value) if not field.name.startswith("_") and field.name != "packet"}
        if kind.__name__ == "BlockCollection":
            output["pickup_confirmed"] = value.pickup_confirmed
        elif kind.__name__ == "NavigationPlan":
            output["endpoint"] = _project(value.endpoint)
        elif kind is WorldTime:
            output.update(daytime=value.daytime, day=value.day, observed_moon_phase=value.observed_moon_phase)
        elif kind is EntityState:
            output["visible_effect_ids"] = _project(value.visible_effect_ids)
        elif isinstance(value, (SoundEvent, ParticleEvent, BlockEvent)):
            output.update(_feedback_fields(value))
        return _record(output)
    raise TypeError(f"{kind.__name__} is unavailable through the MCP script Bot")


def _snapshot(name, value):
    if name in _SLOT_MAPS and value is not None:
        value = {slot: _stack_snapshot(item) for slot, item in value.items()}
    elif name == "offhand_item":
        value = _stack_snapshot(value)
    elif name in _ITEM_VERDICTS:
        return _record({"result": _record({"name": value.result.name}),
                        "client_request_id": _record({"raw_id": int(value.client_request_id.raw_id)})})
    elif name == "execute_command_output":
        origin, output = value.origin_data, value.output
        return _record({
            "origin_data": _record({"type": _project(origin.type), "uuid": str(origin.uuid),
                                    "request_id": origin.request_id, "player_id": origin.player_id}),
            "output": _record({
                "type": _project(output.type), "success_count": output.success_count, "data": output.data,
                "messages": [_record({"message_id": message.message_id, "successful": message.successful,
                                      "params": _project(message.params)}) for message in output.messages],
            }),
        })
    return _project(value)


def _input_record(value):
    if isinstance(value, list):
        return tuple(_input_record(item) for item in value)
    if isinstance(value, dict):
        if value.get("$endpilot") != "record" or value.get("type") not in _INPUT_RECORDS:
            raise TypeError("expected a supported navigation goal or rules value")
        if not isinstance(value.get("fields"), dict):
            raise TypeError("expected fields for a navigation value")
        return _INPUT_RECORDS[value["type"]](**{key: _input_record(item) for key, item in value["fields"].items()})
    return value


def _tuples(value):
    if isinstance(value, list):
        return tuple(_tuples(item) for item in value)
    if isinstance(value, dict) and "$endpilot" in value:
        raise TypeError("unsupported SDK input record")
    return value


def _bind(member, args, kwargs):
    bound = inspect.signature(member).bind(*args, **kwargs)
    for name, value in bound.arguments.items():
        if name in _UNSUPPORTED_PARAMETERS and value is not None:
            raise TypeError(f"{name} is unavailable through the MCP script Bot")
        if name in {"goal", "rules"}:
            value = _input_record(value)
        elif name == "sections" and isinstance(value, dict) and value.get("$endpilot") == "range":
            bounds = tuple(value.get(key) for key in ("start", "stop", "step"))
            if not all(type(bound) is int for bound in bounds):
                raise TypeError("expected integer section range bounds")
            value = range(*bounds)
        elif name == "against" and isinstance(value, dict):
            if value.get("$endpilot") != "record" or not isinstance(value.get("fields"), dict):
                raise TypeError("expected a block observation")
            data = {key: _tuples(item) for key, item in value["fields"].items()}
            value = (BlockObservation if "is_air" in data else BlockUpdate)(**data)
        elif name != "response":  # A form answer can be a JSON array.
            value = _tuples(value)
        bound.arguments[name] = value
    return bound


def _encode(frame):
    data = json.dumps(frame, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(data) > _MAX_FRAME:
        raise ValueError("MCP script message exceeds the frame size limit")
    return data


def _invalid_constant(_name):
    raise ValueError("MCP script message contains a non-finite number")


def _read(pipe):
    frame = json.loads(pipe.recv_bytes(_MAX_FRAME), parse_constant=_invalid_constant)
    if not isinstance(frame, dict):
        raise ValueError("expected an MCP script message object")
    return frame


def _error(error):
    detail = {"type": type(error).__name__, "message": str(error)}
    attributes = {}
    field = _ERROR_FIELDS.get(type(error))
    if field is not None and type(value := getattr(error, field, None)) is str:
        attributes[field] = _project(value)
    for name, kinds in _ERROR_PROGRESS.items():
        value = getattr(error, name, None)
        if type(value) in kinds:
            attributes[name] = _project(value)
    if attributes:
        detail["attributes"] = attributes
    return detail


def _reported_error(detail) -> _Error:
    if not isinstance(detail, dict) or not all(isinstance(detail.get(key), str) for key in ("type", "message")):
        raise ValueError("invalid MCP script error message")
    report = {"error_type": detail["type"], "message": detail["message"]}
    fields = detail.get("attributes", {})
    if not isinstance(fields, dict):
        raise ValueError("invalid MCP script error attributes")
    attributes = {key: value for key, value in fields.items() if key in _ERROR_ATTRIBUTES}
    if attributes:
        report["attributes"] = attributes
    return report


class _Run:
    def __init__(self, bot: "Bot", source: str | bytes, filename: str, arguments, workspace: Path):
        self.id = uuid4().hex
        self.bot: Bot | None = bot
        self.state: _State = "running"
        self.stopping = False
        self._settling = False
        self.error: _Error | None = None
        self.result = None
        self.output: deque[tuple[_Stream, str]] = deque(maxlen=_OUTPUT_CHUNKS)
        self.cursor = 0
        self._actions: dict[int, asyncio.Task] = {}
        self._subscriptions = {}
        self._outgoing = asyncio.Queue(maxsize=_MAX_OUTGOING_FRAMES)
        self._outgoing_bytes = 0
        self._process = None
        self._pipe = None
        self._writer = None
        self._io = None
        self.task = asyncio.create_task(self._run(source, filename, arguments, workspace), name=f"mcp-script-{self.id}")

    def append(self, stream: _Stream, text: str) -> None:
        chunks = (len(text) + _CHUNK_SIZE - 1) // _CHUNK_SIZE
        self.cursor += chunks
        start = max(0, chunks - _OUTPUT_CHUNKS) * _CHUNK_SIZE
        for offset in range(start, len(text), _CHUNK_SIZE):
            self.output.append((stream, text[offset:offset + _CHUNK_SIZE]))

    def observe(self, cursor: int) -> _Observation:
        if not 0 <= cursor <= self.cursor:
            raise ValueError("cursor must be between zero and the current script cursor")
        floor = self.cursor - len(self.output)
        return {"run_id": self.id, "state": self.state, "cursor": self.cursor, "lost": max(0, floor - cursor),
                "result": self.result, "error": self.error,
                "output": [{"stream": stream, "text": text} for index, (stream, text) in enumerate(self.output, floor)
                           if index >= cursor]}

    def request_stop(self) -> None:
        if self.state == "running" and not self.stopping and not self._settling and self.error is None:
            self.stopping = True
            self._kill_worker()
            self._cancel_actions()

    def _cancel_actions(self):
        for action in self._actions.values():
            if not action.cancelling():
                action.cancel()

    def _kill_worker(self):
        if self._process is not None and self._process.is_alive():
            # A worker can catch or ignore POSIX SIGTERM. Kill before waiting
            # for the peer to close the pipe or for the writer to drain.
            self._process.kill()

    def _fail(self, error):
        if self.error is None and not self.stopping:
            self.error = _reported_error(_error(error))
        self._kill_worker()

    def _send(self, frame):
        if self.stopping or self._settling or self.error is not None:
            return
        try:
            data = _encode(frame)
        except (TypeError, ValueError, RecursionError):
            if frame["op"] != "reply":
                raise
            # Settle the originating request even when its value or error details
            # cannot travel across the worker pipe.
            data = _encode({"op": "reply", "id": frame["id"], "error": {
                "type": "ValueError", "message": "MCP script Bot reply exceeds the frame limit or contains unsupported data",
            }})
        if self._outgoing.full() or self._outgoing_bytes + len(data) > _MAX_FRAME:
            self._fail(RuntimeError("MCP script worker is not consuming Bot replies or notifications"))
            return
        self._outgoing.put_nowait(data)
        self._outgoing_bytes += len(data)

    async def _write(self):
        loop = asyncio.get_running_loop()
        try:
            while (data := await self._outgoing.get()) is not None:
                self._outgoing_bytes -= len(data)
                # The worker can be stuck in arbitrary Python. A full pipe must
                # never block the resident event loop, including SDK callbacks.
                await loop.run_in_executor(self._io, self._pipe.send_bytes, data)
        except (OSError, EOFError):
            # The reader determines exit status and drains worker output.
            return

    def _notify(self, request_id, *values):
        try:
            if not self.stopping and not self._settling and self.error is None:
                self._send({"op": "event", "id": request_id, "args": [_project(value) for value in values]})
        except (TypeError, ValueError, RecursionError) as error:
            self._fail(error)

    async def _perform(self, frame, request_id):
        if self.stopping or self._settling or self.error is not None:
            return
        op, name = frame["op"], frame.get("name", "")
        args, kwargs = frame.get("args", []), frame.get("kwargs", {})
        if not isinstance(args, list) or not isinstance(kwargs, dict) or not isinstance(name, str):
            raise TypeError("invalid Bot request arguments")
        if op == "unsubscribe":
            if len(args) != 1 or type(args[0]) is not int or kwargs:
                raise TypeError("unsubscribe needs a subscription ID")
            unsubscribe = self._subscriptions.pop(args[0], None)
            if unsubscribe is not None:
                unsubscribe()
            return

        kind = _MEMBERS.get(name)
        if not ((op == "get" and kind == "property") or (op == "call" and kind in {"sync", "async"})
                or (op == "subscribe" and kind == "subscribe")):
            raise AttributeError(f"{name!r} is unavailable through the MCP script Bot")
        if op == "get" and (args or kwargs):
            raise TypeError("Bot properties take no arguments")
        member = getattr(self.bot, name)
        if op == "get":
            return _snapshot(name, member)
        if op == "subscribe":
            if len(self._subscriptions) >= _MAX_PENDING:
                raise RuntimeError("too many MCP script subscriptions")

            def callback(*values):
                self._notify(request_id, *values)

            bound = _bind(member, [callback, *args], kwargs)
            self._subscriptions[request_id] = member(*bound.args, **bound.kwargs)
            return

        bound = _bind(member, args, kwargs)
        value = member(*bound.args, **bound.kwargs)
        if kind == "async":
            value = await value
        return _snapshot(name, value)

    def _dispatch(self, frame):
        key = frame.get("id")
        if type(key) is not int or key < 0:
            raise ValueError("invalid MCP script request ID")
        if frame["op"] == "cancel":
            action = self._actions.get(key)
            if action is not None and not action.cancelling():
                action.cancel()
        elif key in self._actions or key in self._subscriptions or len(self._actions) >= _MAX_PENDING:
            raise ValueError("duplicate or excessive MCP script requests")
        else:
            self._actions[key] = action = asyncio.create_task(self._perform(frame, key))

            def settled(task):
                self._actions.pop(key, None)
                try:
                    # The finished task retains the SDK's CancelledError
                    # and any partial observations attached during cleanup.
                    try:
                        response = {"value": task.result()}
                    except (Exception, asyncio.CancelledError) as error:
                        if self.stopping or self._settling or self.error is not None:
                            return
                        response = {"error": _error(error)}
                    self._send({"op": "reply", "id": key, **response})
                except Exception as error:
                    self._fail(error)

            action.add_done_callback(settled)

    def _spawn(self, source, filename, args, workspace):
        from ._mcp_script_worker import worker_main

        spawn = multiprocessing.get_context("spawn")
        pipe, child = spawn.Pipe()
        process = spawn.Process(target=worker_main, args=(child, source, filename, args, str(workspace), _MEMBERS),
                                name=f"endpilot-mcp-script-{self.id}")
        try:
            process.start()
        except BaseException:
            pipe.close()
            raise
        finally:
            child.close()
        return process, pipe

    async def _run(self, source, filename, args, workspace):
        finished = False
        try:
            # An eager factory must also return the run ID before spawning.
            await asyncio.sleep(0)
            if self.stopping:
                return
            loop = asyncio.get_running_loop()
            startup = loop.run_in_executor(None, self._spawn, source, filename, args, workspace)
            try:
                self._process, self._pipe = await asyncio.shield(startup)
            except asyncio.CancelledError:
                self._process, self._pipe = await startup
                raise
            if self.stopping:
                return
            # A reader parked for each run cannot exhaust the host's default
            # executor or prevent its dedicated writer from making progress.
            self._io = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mcp-script-io")
            self._writer = asyncio.create_task(self._write())
            while True:
                frame = await loop.run_in_executor(self._io, _read, self._pipe)
                op = frame.get("op")
                if op == "output":
                    stream, text = frame.get("stream"), frame.get("text")
                    if stream not in ("stdout", "stderr", "log") or not isinstance(text, str):
                        raise ValueError("invalid MCP script output message")
                    self.append(stream, text)
                elif self.stopping or self.error is not None:
                    continue
                elif op in ("get", "call", "subscribe", "unsubscribe", "cancel"):
                    self._dispatch(frame)
                elif op == "result" and "value" in frame:
                    self.result = frame["value"]
                    finished = True
                    break
                elif op == "error" and isinstance(frame.get("error"), dict):
                    self.error = _reported_error(frame["error"])
                    break
                else:
                    raise ValueError("invalid MCP script message")
        except (EOFError, BrokenPipeError):
            self._fail(RuntimeError("MCP script worker exited without a result"))
        except Exception as error:
            self._fail(error)
        finally:
            self._settling = True
            await self._release()
            if self.stopping:
                self.state = "stopped"
            elif finished and self.error is None:
                self.state = "succeeded"
            else:
                self.state = "failed"

    async def _release(self):
        self._kill_worker()
        for unsubscribe in self._subscriptions.values():
            try:
                unsubscribe()
            except Exception as error:
                self._fail(error)
        self._subscriptions.clear()
        actions = tuple(self._actions.values())
        self._cancel_actions()
        self._actions.clear()
        if self._process is not None:
            await asyncio.to_thread(self._process.join)
            self._process.close()
            self._process = None
        if self._writer is not None:
            while not self._outgoing.empty():
                self._outgoing.get_nowait()
            self._outgoing.put_nowait(None)
            await self._writer
        if self._pipe is not None:
            self._pipe.close()
        if self._io is not None:
            self._io.shutdown(wait=True)
        if actions:
            await asyncio.gather(*actions, return_exceptions=True)
        self.bot = None


class MCPScripts:
    def __init__(self, player: "MCPPlayer"):
        self._player = player
        self._workspace = Path.cwd().resolve()
        self._runs: dict[str, _Run] = {}
        self._closing = False

    def register(self, server: "MCPServer") -> None:
        server.tool()(self.run_script)
        server.tool()(self.observe_script)
        server.tool()(self.stop_script)

    async def run_script(self, source: str | None = None, path: str | None = None,
                         args: list[Any] | dict[str, Any] | None = None) -> _Started:
        """Start async main(bot, ...) in an isolated local Python process.

        Provide source or a Python path under the server's working directory.
        A list passes positional arguments and a mapping passes keyword arguments.
        The Bot proxy copies gameplay data. Subscriptions deliver queued notifications
        to worker callbacks. Lifecycle, credentials, raw handles and synchronous
        predicates are unavailable through the proxy.
        """
        if (source is None) == (path is None):
            raise ValueError("provide exactly one of source or path")
        if self._closing:
            raise RuntimeError("the player connection is closing")
        bot = self._player.connected_bot()
        if path is not None:
            script = (self._workspace / path).resolve(strict=True)
            if not script.is_relative_to(self._workspace) or not script.is_file():
                raise ValueError("script path must be a file under the workspace")
            with script.open("rb") as stream:
                source = stream.read(_MAX_SOURCE + 1)
            filename = str(script)
        else:
            filename = "<endpilot script>"
        if len(source) > _MAX_SOURCE:
            raise ValueError("script exceeds the source size limit")
        if args is not None and not isinstance(args, (list, dict)):
            raise ValueError("args must be a list or mapping")
        arguments = json.loads(json.dumps(args, allow_nan=False))
        if len(self._runs) == _MAX_RUNS:
            oldest = next((run.id for run in self._runs.values() if run.state != "running"), None)
            if oldest is None:
                raise RuntimeError("too many active script runs")
            del self._runs[oldest]
        run = _Run(bot, source, filename, arguments, self._workspace)
        self._runs[run.id] = run
        return {"run_id": run.id}

    def _get(self, run_id: str) -> _Run:
        try:
            return self._runs[run_id]
        except KeyError:
            raise ValueError("unknown or expired script run ID") from None

    async def observe_script(self, run_id: str, cursor: int = 0) -> _Observation:
        """Read output after a cursor. Lost counts output chunks that expired."""
        return self._get(run_id).observe(cursor)

    async def stop_script(self, run_id: str) -> _Status:
        """Terminate a worker and drain its host Bot actions and subscriptions.

        Forced worker termination can interrupt script finally blocks and callbacks.
        """
        run = self._get(run_id)
        run.request_stop()
        await asyncio.shield(run.task)
        return {"run_id": run.id, "state": run.state}

    async def close(self) -> None:
        """Release all workers before the current Bot is transferred or closed."""
        self._closing = True
        try:
            active = tuple(run for run in self._runs.values() if run.state == "running")
            for run in active:
                run.request_stop()
            if active:
                await asyncio.wait(tuple(run.task for run in active))
        finally:
            self._closing = False
