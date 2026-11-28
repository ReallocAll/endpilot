# World and terrain

## Read an observed block

`bot.observed_block((x, y, z), layer=0)` reads the primary-layer cache at
integer block coordinates. `None` means the position is unknown. A returned
snapshot includes the runtime ID when known, and `is_air` is `True` for
confirmed air. This query sends no packets.

`bot.observed_block_state((x, y, z), layer=0)` resolves a cached layer against
the protocol 2225 vanilla palette. The immutable `BlockStateObservation`
holds `position`, `runtime_id`, `layer`, `name` and a read-only `properties`
mapping. Byte and Int properties are Python integers. String properties are
Python strings. Later world updates leave the snapshot unchanged.

Known air has the name `minecraft:air` and an empty property mapping. The
`runtime_id` can be `None` for a server-confirmed empty layer. The query
returns `None` for missing observations, unresolved IDs, unverified sequential
IDs and unsupported protocol versions. It sends no packets.

Use `bot.block_state_by_id(runtime_id)` to resolve a saved network ID during
the same joined session. It returns an immutable `BlockState` with
`runtime_id`, `name` and read-only `properties`. It reads the session's local
protocol 2225 palette and sends no packets. The ID need not be in the current
terrain cache. Hash mode accepts signed 32-bit and unsigned 32-bit IDs and
returns the unsigned ID used on the wire. An unknown ID, an unverified
sequential palette, an unsupported protocol or an inactive session yields
`None`.

## Read observed block collision boxes

`bot.observed_block_collision_boxes((x, y, z), layer=0)` reads the cached
collision geometry for the specified layer in the current dimension. It is
synchronous and sends no packets. The result is an immutable tuple of boxes
in the order in the protocol 2225 block export. Each box is
`(minX, minY, minZ, maxX, maxY, maxZ)` in block-local coordinates. Bounds can
extend beyond the unit cell. Add the integer block position to both corners
to convert them to world coordinates.

Known empty geometry is `()`. This includes air, an observed empty extra
layer, buttons and open fence gates. `None` means the requested layer is
negative or unobserved, the state or protocol is unsupported, or the session has ended.
A sparse update describes its own layer, and a complete section can confirm
that its extra layer is empty. Later observations and eviction leave earlier
results unchanged.

The table covers the exported collision shapes of selected static vanilla
blocks, including common full cubes, slabs, stairs, fences, gates and walls.
An oak fence with only its north connection bit set reaches local Z `0`.
The isolated post starts at local Z `0.375`.
Custom or unresolved IDs and states with runtime-dependent shapes, such as
doors, pistons, shulker boxes and scaffolding, yield `None`. Use the raycast
methods for selection outlines. A stone button can be selected and has an
empty collision tuple. [Pinned data and regeneration](block-data.md)

## Observe block entity updates

`bot.observed_block_entity((x, y, z))` reads an independent native NBT copy
from the current dimension's cache. An empty compound is a known value.
`None` means there is no current observation.

```python
def on_update(event):
    print(event.dimension_id, event.position, event.data)

unsubscribe = bot.subscribe_block_entity_updates(on_update, position=(8, 64, 9), dimension_id=0)
```

Omit either filter to receive updates at any position or in any dimension
the bot visits during the session. The synchronous callback runs after the
cache and sign handling for each accepted standalone `BlockActorDataPacket`.
Equal retransmissions each deliver their own event in packet order.
The frozen `BlockEntityUpdate` holds the dimension ID, integer position, and
a mutable native `CompoundTag` copy owned by this callback, including its
nested tags.

Delivery begins with the next dispatched packet after registration. Existing
cache entries, retained login terrain, block replacements, evictions, and NBT
in chunk payloads produce no events. Calling `unsubscribe()` stops delivery
immediately and is safe to repeat. Delivery also ends with the session.
Both APIs send no packets.

## Edit a command block

On protocol 2225, use `bot.set_command_block()` to change the command text of
an existing impulse, repeating, or chain command block. The bot needs a
current observation of the block and its block entity in the current
dimension. On stock Bedrock Dedicated Server, give the bot operator
permission and use creative mode in a world with cheats and command blocks
enabled.

Pass `mode` as `"impulse"`, `"repeating"`, or `"chain"`. The
`always_active`, `conditional`, and `track_output` keywords take booleans.
Omit any of these options or pass `None` to keep its observed value. Pass
`False` to turn a boolean setting off.

In a world you control, set a message and disable output tracking on an
observed impulse block that requires redstone.

```python
update = await bot.set_command_block(
    (8, 220, 8), "say Test pulse received", track_output=False, timeout=15
)
```

The action sends one edit and preserves the other settings. It returns a
fresh `BlockEntityUpdate` when a standalone block entity packet at the same
position in the same dimension confirms the command text and requested
boolean settings, and the observed block state matches the target mode. If
you set `conditional` explicitly, the block state's conditional flag must
also match.

The server determines when the command runs. For the example block, supply
a redstone pulse and meet any configured condition.

If the deadline expires before confirmation, the call raises `TimeoutError`.
Cancelling the action ends the wait. In either case, the server may still
apply an edit that was already sent.

## Estimate a source-free script explosion

On vanilla Bedrock 2225, use this synchronous query before a single
`Dimension.createExplosion` call with `source` omitted. Pass the world-space
center and script radius. The query sends no packets and reads the latest
server-observed standing position and terrain in the current dimension.

```python
center = (9.5, 220.5, 8.5)
damage = bot.estimate_sourceless_explosion_damage(center, 2.25)
raw_damage = bot.estimate_sourceless_explosion_damage(center, 2.25, raw=True)
```

The default estimates the damage after the local player's observed armor.
In range, it requires positive observed health, survival mode, normal or hard
difficulty, zero observed absorption, no active observed effects, and complete
unenchanted equipment with neither hand holding a shield. The estimate is
independent of the remaining health. Read `bot.health` to observe an actual
health change.

`raw=True` uses distance and terrain exposure. Pass another observed standing
player's integer runtime ID as `target_runtime_id`, or omit it to select the
local player. Within range, remote players require raw mode. Either mode
returns `0.0` for a confirmed target beyond twice the radius, even if its
equipment or the terrain is unknown. At exactly twice the radius the raw
result is `1.0`. Unknown required observations, unresolved collision,
unsupported poses, and ended or changing sessions return `None`.
Pass a bool for `raw` and an int or `None` for `target_runtime_id`.
Other types raise `TypeError`.

## Estimate damage near observed primed TNT

On vanilla Bedrock 2225, `bot.estimate_tnt_base_damage(tnt_runtime_id)`
uses the last observation of a primed `minecraft:tnt` entity in the current
dimension and session. Use its runtime ID from `bot.observed_entities`.
The synchronous query sends no packets and returns an immutable
`TntDamageEstimate`.

- `center` is the entity's last observed position.
- `base_damage` estimates unmitigated damage to the local player if the TNT
  exploded at that position now. It uses vanilla strength 4, the local
  player's observed standing geometry, and cached terrain.

The estimate is independent of gear and remaining health. It uses the
observed position without projecting entity motion or the future explosion
center. Read `bot.health` for the actual health change. A missing or wrong
actor, an unsupported pose, unknown required terrain, or an unavailable
session returns `None`.

## Search observed blocks

After position sync, `find_blocks()` searches the current dimension's
cached primary-layer blocks in an inclusive sphere centered on the bot's
floored position. Pass a block name or several names. Bare names use the
`minecraft` namespace.

You can also pass a runtime ID, an iterable of runtime IDs, or only a
predicate. Use runtime IDs observed in this session.
In hash mode the same ID can be passed as a signed 32-bit integer. A result
can have `name=None` if the cache cannot resolve the block name. The
positional selector and `names=` accept the same forms. In the example
below, `position` is the location of a block whose state you want to find.

```python
by_name = bot.find_blocks("chest", radius=8, limit=3)
print(by_name.matches, by_name.complete)
sample = bot.observed_block(position)
if sample is not None and sample.runtime_id is not None:
    same_state = bot.find_block(sample.runtime_id, radius=8)
    print(same_state.matches, same_state.complete)

on_level = bot.find_blocks(radius=8, predicate=lambda block: block.position[1] == 64)
print(on_level.matches, on_level.complete)
```

The predicate runs synchronously on each eligible immutable `BlockMatch`.
Return `True` to include it, `False` to omit it, or `None` to omit it while
marking the result incomplete. Missing observations can also make
`complete=False`. This includes positions without a known ID during an ID
search and positions without a resolvable name during a name search.

Matches are ordered by squared distance, then X, Y, and Z. `limit` caps
matches after filtering, and `find_block()` uses `limit=1` with the same
coverage rules. Both methods return a `BlockSearchResult` and send no packets.

## Wait for terrain coverage

Use `bot.column_coverage((chunk_x, chunk_z))` to read one column's current
`ColumnCoverage`. To wait for several columns with different section ranges,
pass a nonempty mapping.

```python
coverage = await bot.wait_for_terrain_coverage({
    (1, 0): range(10, 12),
    (0, 0): range(13, 14),
}, timeout=15)
```

Keys are integer chunk coordinate pairs. Each value is a nonempty `range`
with step 1 and section Y indices in `[-128, 128)`. A section spans 16 blocks
vertically. For one column, `await bot.wait_for_column_coverage(column, sections)`
returns a single snapshot under the same rules.

The wait records the mapping's entries when it starts. It returns a tuple
of immutable `ColumnCoverage` snapshots in input order once every selected
section is cached at the same time in the current dimension. Each snapshot
includes the column's full coverage at that point. Server-confirmed all-air
sections count. Announcements, requests and individual block updates leave
section coverage pending. Both waits send zero packets and return immediately
when coverage is already ready, including with `timeout=0`.

A selected column outside the known publication area or evicted during the
wait raises `ColumnCoverageInvalidated` with reason `"unloaded"`, even if
that column was already ready. A `ChangeDimension` packet raises the same
exception with reason `"dimension_changed"`. An ended packet stream raises
`ConnectionError`. The timeout applies to the entire wait and raises
`TimeoutError`. Cancellation is local to the caller.

## Store received terrain

Pass a `TerrainStorage` to `join_address`, `join_raknet` or `join_nethernet`
to record the block layers the bot receives in each dimension. Choose a stable
`world_key` for the world and a new key when the world is replaced.

```python
from endpilot import Bot, TerrainStorage

storage = TerrainStorage.sqlite("terrain.sqlite", world_key="survival")
bot = Bot("Surveyor")
await bot.join_raknet("127.0.0.1", terrain_storage=storage, load_announced_terrain=True)
try:
    column = await bot.terrain_column((0, 0))
    if column is not None:
        block = column.block((4, 100, 7), layer=0)
        if block is not None:
            print(block.name, block.source, block.session_id)
finally:
    await bot.close()

# The database can also be opened from an independent process.
column = await storage.load_column((0, 0), protocol=2225, dimension=0)
```

Recording starts with the terrain applied from the login buffer and includes
complete sections and individual block updates anywhere the server sends
them. A column announcement provides no block data. An unload clears the live
cache and leaves its committed observations available in storage.

`bot.terrain_column()` requires a joined session with storage. It waits for the
terrain already applied at the start of the call to commit, then reads storage
and takes a snapshot of the live column. A dimension change during the query
raises `ColumnCoverageInvalidated`; a session end raises `ConnectionError`.
`storage.load_column()` reads committed observations for the specified world,
protocol and dimension and can be used independently of any bot. Both calls
send no packets and return `None` when the column has no recorded or live data.
`observed_block()` and `observed_block_state()` continue to read the live cache.

The snapshot lists complete sections under `current_sections` and
`stored_sections`, using section Y indices. Section `6` starts at block Y `96`.
An individual block update covers only its cell and layer. A newly received
complete section replaces the earlier section and its sparse updates, including
layers omitted by the replacement. `column.block()` returns `None` for an
undescribed cell or layer. A described cell can have `name=None` when its ID
was unresolved. Known air has `is_air=True`, `is_air=None` means the air status
was indeterminate, and `runtime_id=None` occurs for air described without an
explicit runtime ID. Each returned cell has a `current` or `stored` source,
the observing `session_id`, and the name and properties resolved at observation
time. A reconnect uses its own palette for new observations. The column and its
cells are immutable.

One `TerrainStorage` can be reused for successive joins. It has one sequential
writer, with one batch in flight and one pending batch merged by section and
cell. The default pending budget is 16 MiB for packed data, including key
charges. This budget measures the write backlog. If it fills, `storage.error`
reports a `TerrainStorageError` with the first rejected `sequence` and its
`session_id`. Recording stops for that join and attempts to drain the accepted
prefix. The bot can continue playing. `await storage.flush()` waits for the
prefix accepted when called and raises a recording failure, if any. `await
bot.close()` closes the network and waits for storage to drain and close. It
also reports a recording failure. A remote disconnect drains and closes the
writer without a normal completion marker.

For another backend, use `TerrainStorage(world_key, open_store,
max_pending_bytes=...)`. The factory receives `(world_key, protocol, dimension)`
and returns a new `TerrainStore` handle, either directly or through an
awaitable. Independent read handles can overlap a writer or outlive a bot
session. A handle provides async `load_column(column)`,
`commit(session_id, through, sections, *, finished=False)` and `aclose()`.
`load_column` returns a mapping of section Y to `TerrainSectionData`. The
writer submits commits in order, with section keys `(chunk_x, section_y,
chunk_z)`. `through` is the highest applied sequence in that commit's
dimension. Commit the merged sections and the boundary together. Complete
bases replace older data, and `older.merged_with(newer)` applies sparse changes
to the existing section. `to_bytes()` and `TerrainSectionData.from_bytes()`
provide the codec. A `finished=True` commit can carry no section data and
repeat that dimension's last boundary.

## Observe dimension changes

`bot.is_changing_dimension` reports whether the local player has an active
server-directed transition. `bot.active_dimension_change` holds its destination
dimension ID, raw packet position, respawn flag and optional loading screen ID.
The state opens on receipt of `ChangeDimension` after the server identifies the
local player.

`await bot.wait_for_dimension_change()` returns the current or next transition
after the server's acknowledgment for this player and the client's reply.
The client sends loading-screen updates when the server provides a screen ID.
A newer `ChangeDimension` replaces the active snapshot until the player is ready.
The wait raises `ConnectionError` if the session ends. The transition position
may be temporary. After the exchange, normal movement uses the new destination
for ordinary transfers and a fresh position update for death returns.
Scheduled input pauses during the exchange. Explicit input submission raises
`RuntimeError` if the exchange is still active when the action would send a frame.

## Read the current dimension's Y range

`bot.dimension_bounds` returns an immutable `DimensionBounds` snapshot with
`dimension_id`, inclusive `min_y`, exclusive `max_y_exclusive`, and `source`.
Use `[min_y, max_y_exclusive)` as the block Y range the SDK uses to interpret
terrain sections. Placement permission is determined separately by the server.

`source` is `"dimension_data"` when the server supplied a valid `DimensionData`
definition, including when it supplies the same bounds as vanilla. Otherwise
`"vanilla_default"` identifies the protocol 2225 defaults for the Overworld
`[-64, 320)`, Nether `[0, 128)`, and End `[0, 256)`.

The range is available immediately after joining and follows the current
terrain dimension when `ChangeDimension` arrives. It returns `None` before
joining, after the session ends, or when the dimension has no effective
bounds. The property sends no packets.

## Read a biome ID

After joining, `bot.observed_biome((x, y, z))` returns the latest observed
numeric biome ID at those integer block coordinates in the current dimension.
`None` means the biome is unknown. `0` is a valid ID. The query reads the cache
and sends no packets.

For a known vanilla protocol 2225 world, look up the observed ID in the bundled
read-only catalog. Unknown IDs return `None` with `get()`.

```python
from endpilot import vanilla_biomes

catalog = vanilla_biomes(protocol=2225)
biome = catalog.get(bot.observed_biome((x, y, z)))
```

Each `VanillaBiome` contains the ID, namespaced name, temperature, downfall,
rain flag, and tags. Use the server's own IDs for custom worlds. To regenerate
the catalog, pass the pinned EndstoneMC/bedrock-data checkout to
`python scripts/generate_biome_data.py ../bedrock-data`. [Pinned data and setup](block-data.md)

## Read advertised biome definitions

On protocol 2225, `bot.biome_definitions` is a read-only mapping keyed by the
exact names advertised by the current server, such as `"minecraft:ocean"`.
Each `BiomeDefinition` gives its name, temperature, downfall, rain flag, and
ordered tags. The property sends no packets. It returns `None` until the
current session receives a valid advertisement and an empty mapping for an
explicitly empty advertisement. A later valid advertisement replaces the
mapping. Malformed advertisements leave the last valid mapping available.
Previously saved snapshots remain readable after a new advertisement or
disconnect.

Use `bot.weather` for current weather and `bot.observed_biome(position)` for a
coordinate's numeric biome ID. The advertisement's string indices do not map
those numeric IDs to names.

## Check whether the world is hardcore

`bot.world_hardcore` is the flag advertised when joining. `True` means
hardcore, `False` means an ordinary world, and `None` means the current
session has no observation. Read `bot.world_difficulty` separately for the
server-reported difficulty.

## Read the advertised world generator

`bot.advertised_world_generator` reads `StartGame.settings.generator` from
the current session without sending packets. Known values are members of
`bedrock_protocol.GeneratorType`. An unknown value is returned as its original
integer. The property returns `None` before joining and after the session ends.

Vanilla BDS 1.26.66.1 advertises `GeneratorType.OVERWORLD` for `DEFAULT`
worlds and `GeneratorType.FLAT` for `FLAT` worlds. The declaration stays
the same through dimension changes and respawns. Use it as the server's
login declaration about the world. Observed terrain and the current dimension
have their own queries.

## Observe world time and weather

After joining, `bot.world_time` and `bot.weather` return immutable snapshots.
`world_time.daylight_ticks` is the last reported daylight time. `daytime` and
`day` give its position in a 24,000-tick day and its zero-based day number.
`game_ticks_at_join` records the separate game tick sent at login.
`do_daylight_cycle` reports the server gamerule. Each registered clock in
`world_time.clocks` has its own name, reported time and `paused` flag.

`weather.raining` and `weather.thundering` report the last server-announced
world weather across dimension changes. `rain_level` and `lightning_level`
record the float samples sent with the initial settings. A subsequent state
change clears that channel's sample until a new one is received. Unknown
values appear as `None`.

`await bot.wait_for_world_time_update()` and `await bot.wait_for_weather_update()`
return the first changed snapshot observed after the wait starts. Weather
packets can update rain and thunder separately.

## Observe sounds, particles and block feedback

`await bot.wait_for_sound("random.pop", kind="play")` waits for a future sound
packet. Use `kind="stop"`, `"level"` or `"update"` for named stop requests,
level sound events or sound-handle updates. `event.packet` preserves the decoded
Bedrock fields. In protocol 2225, `PlaySound` keeps the encoded integer position
in `event.packet.pos` and exposes world coordinates in `event.position`.

`await bot.wait_for_particle("minecraft:basic_flame_particle")` returns a named
spawn event. The decoded packet provides `vanilla_dimension_id`, `actor_id`
and `molang_variables`. All observations carry the bot's dimension at receipt.

`await bot.wait_for_block_event(position, kind="chest_open")` can filter by
position and kind. Supported feedback includes raw block events, note sounds,
chest lid changes, piston arm states and start, progress and end of cracking.
In protocol 2225, note events expose the numeric `instrument_id` and packed
sound `note_pitch`. The sound pitch of a mob-head note can differ from the
block's tuning. `instrument_sound` resolves a known ID to a vanilla Bedrock
sound identifier such as `note.bd`. It is `None` for unknown IDs, missing or
negative note sound data. The level sound packet carries the generic sound
event `note`.

For protocol 2225, pass `kind="note_tuning"` to `wait_for_block_event` or
`subscribe_block_events` to read the tuning index. The event is available
when the latest main-layer block observation at the packet position is
`minecraft:noteblock` and the packet's `b1` is in `0..24`.
`event.note_tuning` is that index, and `event.instrument_id` comes from
`b0` of the same packet. `event.note_pitch` is `None` on tuning events.
These fields are also available to MCP script waits and callbacks.
The default stream and `kind="block"` continue to deliver generic feedback
for those packets. Use `kind="note"` for sound pitch.

Use `vanilla_note_instruments(protocol=2225)` from `endpilot` to look up these
identifiers without a connection. The mapping is read-only and unsupported
protocol numbers raise `ValueError`.

A crack-end packet with an all-zero position has `event.position` set to
`None`. Its native `packet.pos` remains available.

For protocol 2225, pass `kind="block_particle"` to `wait_for_block_event` or
`subscribe_block_events` to observe the native `LevelEvent` packets for
block-mining particles (`3603`–`3608`) and block-breaking particles (`2001`).
The event keeps the floating-point position and the bot's dimension at receipt.
`event.packet.event_id` and `event.packet.data` expose the native event ID and
signed data. In MCP scripts these are `event.event_id` and `event.data`.
Omitting `kind` preserves the usual block-feedback stream. Named particle
spawns continue to use `wait_for_particle` and `subscribe_particle`.

Use `bot.subscribe_sound(callback, name=..., kind=...)`,
`bot.subscribe_particle(callback, name=...)`, or
`bot.subscribe_block_events(callback, position=..., kind=...)` to receive
every future matching event in packet order. All filters are optional and
use the same matching rules and typed events as the corresponding waits.
Each callback runs synchronously during packet dispatch. Subscriptions start
after registration and end when the bot closes, receives a server disconnect,
or its packet reader ends. The returned function stops delivery immediately
and can be called more than once.

In protocol 2225, `chest_open` and `chest_close` apply when the observed main
layer at the packet's position is an ordinary chest, trapped chest, ender chest,
or vanilla shulker box, including dyed variants. A `BlockEventPacket` with
`(b0, b1)` equal to `(1, 1)` reports opening and `(1, 0)` reports closing.
The event kind is `block` for unknown or other block types. For a double chest,
`event.chest_partner` gives the other half's position when both cells have the
same ordinary or trapped chest type and current, reciprocal chest block entity
data. The field is `None` for single containers or when pairing cannot be
confirmed. The position filter uses the packet's position.

For a protocol 2225 `PistonArm` packet, `event.position` is the piston base.
`event.piston_attached_blocks` is an immutable tuple of signed, absolute
`(x, y, z)` source block positions in the order reported by that packet's
`AttachedBlocks` list. A valid empty list gives `()`. Missing or malformed
lists and other kinds of block feedback give `None`. The list is available
independently of the piston phase and `event.piston_face`.

In observed vanilla 2225 moves, the source positions are reported on
`piston_extending` and on `piston_retracting` when a sticky piston pulls blocks.
The final `piston_extended` and `piston_retracted` packets report an empty list.
Read block updates to confirm where the blocks finish.

## Read and edit signs

`bot.observed_sign(position)` returns the observed front text, back text,
dimension ID, and wax status of a standing or hanging sign. It returns `None`
when the sign data is unknown. Sign data comes from standalone
`BlockActorDataPacket` updates and, on protocol 2225, from received
sub-chunk payloads. Reading the observation does not request world data.

Stand close to a sign the bot has observed. `await bot.edit_sign(position, text,
face=2)` clicks the specified block face and edits the side the server opens.
The returned `"front"` or `"back"` reports which side the bot submitted. Use a
subsequent server observation to confirm the written text. A waxed sign raises
`PermissionError`. If the server never opens the sign editor, the wait raises
`TimeoutError`. An observed replacement or unload invalidates a pending edit.

To handle an editor opening separately, start
`bot.wait_for_sign_open(position=position)` before triggering the open. The
returned `SignEditor` identifies the side chosen by the server. Pass that
object to `await bot.submit_sign_text(editor, text)` while the editor is still
active. The bot needs both sides and the wax status, and the submission
preserves the other side.
