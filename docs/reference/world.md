# World and terrain

## Read an observed block

`bot.observed_block((x, y, z), layer=0)` reads the primary-layer cache at
integer block coordinates. `None` means the position is unknown. A returned
snapshot includes the runtime ID when known, and `is_air` is `True` for
confirmed air. This query sends no packets.

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
single-chest lid changes where the block is known, piston arm states and start,
progress and end of cracking. Note events expose `note_pitch` and
`instrument_id`. A crack-end packet with an all-zero position has
`event.position` set to `None`. Its native `packet.pos` remains available.

## Read and edit signs

`bot.observed_sign(position)` returns the observed front text, back text,
dimension ID, and wax status of a standing or hanging sign. It returns `None`
when the sign data is unknown. With the published codec wheel, sign data comes
from standalone `BlockActorDataPacket` updates. Sign data delivered solely in
a sub-chunk payload remains unknown.

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
