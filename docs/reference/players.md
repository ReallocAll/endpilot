# Players and entities

## Walk on NetherNet or RakNet

From the SDK checkout, run the walk example with either transport. Press
Enter after it reports spawn to start walking.

```sh
python -m pip install -e ".[nethernet,raknet]"
python examples/walk.py http://localhost:19132
python examples/walk.py raknet://127.0.0.1:19132
```

`walk_forward()` returns the number of submitted input frames. On BDS, use
`querytarget` to compare the player's position before and after the walk.
[Walk example](../../examples/walk.py)

## Switch flight on or off

Use `await bot.set_flying(True)` to submit a flight start and `False` to
submit a flight stop. You can attempt the switch when the server reports
Creative for your player or an explicit `MAY_FLY=True` grant is still valid
for your current game mode. The action raises `PermissionError` if
authorization is missing or invalidated during the switch. A return of `None`
means the input was submitted or the target already matched. BDS decides
whether the switch takes effect. Observe the server's
reported flight state in `bot.abilities.flying` or await
`bot.wait_for_abilities()` to receive the next ability packet.

## Fly to a position in Creative

```python
result = await bot.fly_to((7.5, 222.5, 8.5))
if result.status == "predicted_arrived":
    print(result.feet_position)
```

Give the absolute feet coordinates for a short flight through clear, observed
space. The horizontal radius and vertical tolerance default to `0.35` blocks.
The action uses up to `ticks=160` client ticks before releasing its controls.
It takes off when needed and releases the directions to hover. Use
`await bot.set_flying(False)` to request landing.

The immutable `FlightResult` carries `status`, `frames` submitted by the
action, and the last predicted `feet_position`. Read `status` to distinguish
these outcomes.

- `predicted_arrived` means the SDK predicts stable flight within both
  tolerances after releasing input. The result describes the client prediction.
- `blocked` means the standing body or its expected stopping path intersects a
  known collision box, including partial block shapes. The action follows the
  direct path.
- `unknown` means the predicted path lacks enough terrain or pose information.
  Partial shapes and liquids require more information.
- `budget` means the client tick budget expired.
- `stopped` means active flight control ended early, for example after
  `stop_moving()`, an authorization change, or a dimension transition.

Calling with an input source that owns scheduled movement raises
`RuntimeError`. Initial flight authorization uses the same rule as
`set_flying()` within Creative mode. A disconnect raises `ConnectionError`.

## Observe local spawns

Register a synchronous callback with `bot.subscribe_spawns(callback)` before
joining to receive `"initial"` once the session is ready. The callback receives
`"respawn"` on the first server-confirmed live spawn after a local death. Keep
the returned unsubscribe function to remove this registration. The subscription
follows the same `Bot` across joins.

Load a reusable observer with `bot.load_plugin(plugin)` or pass several to
`bot.load_plugins(plugins)`. Each plugin is a synchronous callable that receives
the `Bot` immediately, so it can register with `subscribe_spawns` before the
first join. Each Bot retains the exact callable across reconnects.
`bot.has_plugin(plugin)` is true during installation and after success, so
plugins can load each other without installing twice. A batch validates its
members before installing them in order. If a plugin raises or returns an
awaitable, its registration is removed and earlier successes stay installed.
An awaitable result raises `TypeError` and remains under its creator's
management. The caller can retry after the plugin cleans up its own side
effects.

## Observe advertised entity types

`bot.entity_registry` is an immutable snapshot of the entity types advertised
by the current server. It is `None` until this connection receives a valid
advertisement and after disconnection. An explicitly empty advertisement
produces a snapshot with `entries == ()`. A later advertisement replaces the
snapshot. Snapshots you already hold remain available after replacement or
disconnection.

```python
registry = bot.entity_registry
if registry is not None:
    dragon = registry.entry_for_name("minecraft:ender_dragon")
    if dragon is not None:
        print(dragon.type_id, dragon.summonable)
```

Each `EntityRegistryEntry` retains the server's full `name` and the `type_id`
from the announcement's `rid` field. The `bid` string and the `has_spawn_egg`
and `summonable` flags are `None` when omitted by the server. Lookups by exact
name or `entry_for_type_id(type_id)` return the first match in `entries`, which
preserves announcement order, including duplicate entries. The type ID
identifies a kind of entity. `bot.observed_entities` reports visible instances
with their own `runtime_id` values.

## Observe the server player list

`bot.observed_players` is an immutable snapshot of the player list the server
has sent to this session. It can include the local player and players outside
entity visibility. Use `bot.observed_entities` for observed actors and their
positions.

On an `EntityState` from `bot.observed_entities`, `last_observed_offhand`
holds the most recent offhand stack received for that visible remote player
instance. The field is `None` until an offhand description arrives. A stack
with item ID and count zero represents a received empty offhand. The server
can omit updates during play, so even a new snapshot can contain a stale
stack.

Each listed player has a `uuid`, `name`, and `entity_id` from the server. The
`entity_id` is the unique actor ID. Use `uuid` to match player-list entries
across updates. Other fields include the build platform code, packed color and
player flags. `xuid`, `platform_online_id`, and `play_fab_id` are `None` when
unavailable.

On `bot.observed_players`, `entry.game_mode` holds the last explicit game mode
reported for that remote roster member. It starts as `None`. The server can
report it when the player becomes visible or sends a mode update, including
while the player is outside entity visibility. Leaving visibility or changing
dimension preserves the value while the same identity is listed. A roster
removal, a changed unique actor ID, or a new session ends that observation.
Use `bot.game_mode` for the local player. The local player's roster field is
`None`. Player-list callbacks and waits describe roster packets, so read
`bot.observed_players` for the latest combined state.

Each entry's `skin` is an immutable `PlayerSkinSnapshot` with `skin_id`,
`width`, `height`, `rgba` bytes, `model` (`"wide"`, `"slim"`, or `None`), and
`resource_patch`. It is `None` when the server reports no skin or sends a
placeholder with an empty ID, 0×0 dimensions, and no RGBA bytes. The skin
follows the entry across actor visibility and dimension changes. Removing the
entry discards its current skin from the roster. Previously returned entries
keep their skin snapshots.

`await bot.wait_for_player_skin_update(player_uuid, timeout=15)` returns the
skin from the next live skin packet applied to that UUID while it is listed.
Repeated reports complete the wait, and an empty report returns `None`.
Use `timeout=None` to wait without a deadline. The wait sends no packets and
raises `TimeoutError` at its deadline or `ConnectionError` if the session ends.

`await bot.wait_for_player_list_update(action="add", timeout=15)` returns
matching entries from the first player-list packet dispatched after the wait
starts. Use `action="remove"` for removals and `player_uuid` for a particular
UUID. An add includes the player's metadata and can update an existing entry.
A remove provides the UUID. The wait raises `TimeoutError` at its deadline or
`ConnectionError` if the stream ends first.

`bot.subscribe_player_list_updates(callback, action=..., player_uuid=...)`
calls back once per live player-list packet with a nonempty tuple of matching
`PlayerListChange` snapshots in packet order. By the time the callback runs,
`observed_players` and affected entity identities reflect the whole packet.
Repeated adds and removals of unlisted UUIDs are delivered as received.
Omit either filter to accept any value for it. Visibility and dimension
changes leave roster membership alone.

`bot.subscribe_player_skin_updates(callback, player_uuid=...)` calls back
with the updated `PlayerListEntry` after each live skin packet applied to a
listed UUID. Omit the filter to receive updates for any listed player.
Repeated and empty reports are delivered, with `entry.skin` set to `None`
for an empty report. Skins carried by a player-list add are available in the
add entry and through `observed_players`.

Both subscriptions receive future packets during the joined session. Use
`observed_players` to read the roster and skins already observed during join.
Callbacks run synchronously on the packet reader, and the immutable snapshots
can be retained. Callback failures go to the event loop's exception handler.
The subscriptions send no packets and return idempotent unsubscribe functions.
A UUID filter stays active across dimension changes and when that UUID leaves
and later rejoins the roster. Subscriptions end when the server disconnects,
the packet reader stops, or the bot closes. Registration raises `RuntimeError`
before joining or after closing the bot, and `ConnectionError` after the joined
connection ends.

Run `python examples/observe_player_list.py URL --action add`, then join the
same server from a second client with a different name after
`player_list_watcher_ready` appears.
[Player list example](../../examples/observe_player_list.py)

## Observe entities entering and leaving view

`bot.subscribe_entity_lifecycle(callback)` delivers an immutable
`EntityLifecycleEvent` for each observed remote actor instance that enters
or leaves the entity cache. The event has a `kind` (`"entered"` or `"left"`),
the actor's `entity` snapshot at that transition, and a `reason`. A departure
describes the bot's visibility. An actor can leave view while it remains in
the world and later enter again with the same IDs.

- `"spawn_packet"` is an entry from `AddActor`, `AddPlayer`, `AddItemActor`,
  or `AddPainting`.
- `"remove_actor"` is a departure when `RemoveActor` removes a tracked actor.
- `"pickup"` is a departure when `TakeItemActor` removes a tracked drop. A
  matching `RemoveActor` after the pickup produces no second departure.
- `"replaced"` is a departure when a new spawn takes over a tracked instance's
  runtime ID or identity. The departure comes before the new entry, including
  when a spawn reuses the same IDs.
- `"roster_removed"` is a departure when removing a player-list entry also
  removes its visible actor.
- `"identity_invalidated"` is a departure when roster reconciliation evicts
  an actor. Roster details added or corrected in place produce no event.
- `"dimension_changed"` is a departure for each actor cleared from the old
  dimension, in cache insertion order.

Callbacks run synchronously on the packet reader after the whole packet has
updated the cache. Each callback can read the resulting `bot.observed_entities`
while retaining the event's snapshot. Changes to subscriptions during a
callback take effect for future packets. Registration begins with future
packets. Read `bot.observed_entities` for actors already visible when you
subscribe. Local actors and session teardown produce no events.

The subscription sends no packets, survives dimension changes, and returns an
idempotent unsubscribe function. Callback failures go to the event loop's
exception handler. The subscription ends when the server disconnects, the
packet reader stops, or the bot closes. Registration raises `RuntimeError`
before joining or after closing the bot, and `ConnectionError` after the
joined connection ends.

## Observe remote entity movement

`bot.subscribe_entity_movement(callback)` delivers one immutable
`EntityMovementUpdate` per received `MovePlayer`, `MoveActorAbsolute`, or
`MoveActorDelta` applied to a visible remote entity, including look-only and
repeated reports. Spawns, velocity packets, and movement for the local player
or actors outside the cache produce no updates.

The `entity` snapshot reflects the cache after applying the packet. Its
`position` uses the cache's packet coordinate reference, with omitted delta
axes carried forward from the previous snapshot. `pitch`, `yaw`, and
`head_yaw` hold the angles reported in that packet in degrees. Player angles
retain their reported values, even when head and body yaw differ. Actor angles
use units of `360 / 256` degrees, and a delta packet's omitted angles are
`None`. `on_ground` is the packet's ground flag.

The callback runs synchronously on the packet reader in packet order, after
cache updates and movement predicates. Changes to subscriptions during
delivery or a movement predicate take effect for future packets. Callback
failures go to the event loop's exception handler.

The subscription sends no packets and returns an idempotent unsubscribe
function. It starts with future packets and continues across entity
replacement and dimension changes. It ends when the server disconnects, the
packet reader stops, or the bot closes. Registration raises `RuntimeError`
before joining or after closing the bot, and `ConnectionError` after the
joined connection ends.

## Observe remote player equipment

`bot.observed_player_equipment(runtime_id)` reads the latest immutable
`PlayerEquipmentSnapshot` for a visible remote player. It returns `None`
when the player has no observed equipment snapshot.

`bot.subscribe_player_equipment_changes(callback, runtime_id=runtime_id)`
calls a synchronous callback with the updated snapshot for each observed
change to the selected slot, main hand, or armor. Each armor packet applies
all four armor slots before the callback runs. Use the query to read the
initial state. The subscription sends no packets.

The subscription returns an idempotent unsubscribe function and ends when
the current player instance leaves view or is replaced, the dimension
changes, or the session ends. Register again to observe a new instance,
even if it has the same runtime ID. Registration raises `LookupError` for
the local player or a target without observed equipment, `RuntimeError`
when the bot has no joined session, and `ConnectionError` when the joined
session has lost its connection.

Use `await bot.wait_for_player_equipment_change(runtime_id)` for one change.

## Observe remote player crouching

`EntityState.sneaking` in `bot.observed_entities` holds the last valid server
report of `ActorFlags.SNEAKING` for a visible remote player instance.
`None` means the flag is unknown or does not apply. The spawn packet can
provide the initial value. Later metadata that omits the flag word or carries
it in an unexpected format leaves the last valid value intact. The flag
describes observed Bedrock posture. The SDK reads it directly from metadata,
independently of movement, bounding boxes, and input actions.

`bot.subscribe_entity_crouch_changes(callback, runtime_id=...)` calls a
synchronous callback with the immutable updated `EntityState` when the flag
changes from `False` to `True` or from `True` to `False`.
`await bot.wait_for_entity_crouch_change(runtime_id=..., timeout=15)` returns
the same kind of snapshot for the next matching change. Both methods send
no packets. Read the initial value from `observed_entities`. The first known
value, repeated reports, spawns, and removals do not produce events. Java
Mineflayer's `entityCrouch` can fire on repeated crouching reports. EndPilot
delivers one event per observed transition.

Omit `runtime_id` to observe any visible remote player. A runtime filter binds
the current instance and raises `LookupError` for an untracked target or the
local player. Removal, replacement, or a conflicting roster identity ends the
filtered subscription and raises `LookupError` in a pending filtered wait.
Dimension changes end filtered subscriptions and raise `RuntimeError` in all
pending waits. Unfiltered subscriptions continue across dimensions. The
subscription returns an idempotent unsubscribe function. A deadline raises
`TimeoutError`. Use `timeout=None` to wait without a deadline. Disconnection
ends subscriptions and raises `ConnectionError` in pending waits.

## Observe remote player sleep changes

`EntityState.sleeping` in `bot.observed_entities` holds the last valid server
report of the sleeping bit in the `PLAYER_FLAGS` byte for a visible remote
player. The field is `None` until the first valid report and remains `None`
for other actors. A later packet that omits the byte or supplies a different
payload type leaves the last valid value in place.

`bot.subscribe_entity_sleep_changes(callback, runtime_id=...)` calls a
synchronous callback with the committed immutable `EntityState` for each
change between `False` and `True`. The spawn packet or the first valid
update establishes the initial value silently. Use
`await bot.wait_for_entity_sleep_change(runtime_id=..., timeout=15)` for
the next matching change. Both methods send zero packets.

Omit `runtime_id` to observe any visible remote player. A runtime filter
binds to the current instance. An untracked runtime ID or local player
raises `LookupError`, as does a filtered wait whose target leaves view,
is replaced, or has a conflicting roster identity. A dimension change
ends filtered subscriptions and raises `RuntimeError` in pending waits.
Unfiltered subscriptions continue across dimensions. The returned
unsubscribe function is idempotent. A deadline raises `TimeoutError`.
Use `timeout=None` to wait without a deadline. When the connection ends,
subscriptions close and pending waits raise `ConnectionError`.

## Observe attached fireworks

An observed firework rocket's `EntityState.firework_attached_entity_id`
holds the attached actor's signed unique ID when the spawn packet supplies
it. The value is `None` when the spawn reports no attachment or the field
is unavailable. Snapshots can report attachments to the bot or to other
observed actors.

`bot.subscribe_attached_fireworks(callback)` calls a synchronous callback
with the committed immutable snapshot of a newly sighted rocket attached to
the bot. `await bot.wait_for_attached_firework(timeout=15)` returns the next
such sighting. A sighting can occur when an existing rocket enters view.
The subscription observes future sightings, including reentries after an
actor leaves view. It returns an idempotent unsubscribe function. Both
methods send zero packets.

A dimension change ends pending waits with `RuntimeError`. Subscriptions
continue across dimensions until the connection ends, when pending waits
raise `ConnectionError`. The deadline raises `TimeoutError`. Pass
`timeout=None` to wait without a deadline.

Use `await bot.wait_for_glide_boost()` to observe the server's separate
boost effect for this player. Rocket sightings and boost reports are
independent observations.

## Find a nearby or targeted entity

`EntityState.position` keeps the latest packet's coordinate reference.
For an ordinary standing spawn, `AddPlayer` reports the feet; ordinary
`MovePlayer` uses the standing-height offset. The snapshot does not normalize
the value.

`bot.nearest_entity(match=None)` ranks actors in the current dimension, excluding
the local player, by three-dimensional distance from the bot's synchronized
feet. It uses known feet for remote players and the reported position for
other actors. Players without known feet are skipped. An optional predicate
filters the candidates, and ties use the smaller `entity_id`. The query returns
`None` if the local feet are unknown or no candidate matches.

`bot.entity_at_cursor(max_distance=3.5)` reads the bot's current view ray from
the cache. A `"hit"` gives the actor and hit distance, `"miss"` reports that no
supported actor was hit, and `"unknown"` means missing or ambiguous pose,
terrain, or actor geometry could affect the result. Both queries send no packets.
Adult cows and sheep with observed dimensions can be selected when their upright
feet position is known. For sheep, spawn and verified position updates establish
the feet position. Juvenile sheep and sheep in an unverified pose can leave the
result unknown.

`attack_entity` and `interact_entity` submit input; a normal return does not
confirm a hit, damage, or another server-side effect.

## Observe shield blocks

`bot.subscribe_shield_blocks(callback, runtime_id=...)` calls a synchronous
callback when the server asserts the `BLOCKED_USING_SHIELD` actor flag.
`await bot.wait_for_shield_block(runtime_id=..., timeout=15)` returns the next
matching event. Both methods send zero packets. The immutable
`ShieldBlockEvent` holds the defender's runtime ID, its unique ID when
known, and the tick reported in the metadata packet.

Repeated assertions count once until a typed report clears the flag. Login
metadata and remote spawn packets can establish that initial state silently.
The event reports the impact flag separately from shield posture. The
attacker, health, and shield wear require their own observations.

Omit `runtime_id` to observe the local player and visible remote players. An
explicit remote filter binds to the current player instance. An unknown ID
raises `LookupError`, and a filtered wait raises `LookupError` if its actor
leaves view or is replaced. A dimension change ends filtered subscriptions
and raises `RuntimeError` in pending waits. Unfiltered subscriptions continue
across dimensions. The unsubscribe function is idempotent. A wait raises
`TimeoutError` at its deadline or `ConnectionError` when the connection
ends. Use `timeout=None` to wait without a deadline.

## Observe entity swings

`await bot.wait_for_entity_swing(runtime_id=runtime_id)` waits for the next
swing for the currently tracked instance of that actor. Omit `runtime_id` to
observe any received swing. `bot.subscribe_entity_swings(callback, runtime_id=...)`
delivers the same `EntitySwing` values to a synchronous callback and returns
an unsubscribe function. A filtered subscription ends when its actor leaves
view or the dimension changes. An observed swing reports the animation.
Use separate observations to confirm a hit or damage.

## Observe critical-hit visuals

`await bot.wait_for_entity_critical_effect(kind="critical", runtime_id=runtime_id)`
waits for the next matching server animation on the target actor. Use
`kind="magic_critical"` for the magic critical visual, or omit `kind` to accept
both. These correspond to `CRITICAL_HIT` and `MAGIC_CRITICAL_HIT` in
`AnimatePacket`. Each matching packet produces one `EntityCriticalEffect`.
Subscriptions preserve duplicates and arrival order. Use hurt events and
health updates to observe damage separately.

An `EntityCriticalEffect` is immutable and contains the visual `kind`, the
target `runtime_id`, its unique `entity_id` when known, an optional packet-time
`entity` snapshot, and the decoded `data` float. For the local player, the
effect carries the bot's IDs and `entity=None`. For an unobserved actor, it
carries the packet's runtime ID with `entity_id=None` and `entity=None`.

`bot.subscribe_entity_critical_effects(callback, kind=..., runtime_id=...)`
uses the same filters, delivers events to a synchronous callback, and returns
an idempotent unsubscribe function. Both methods send no packets. A runtime
filter binds the local player or the currently observed actor instance.
Omit it to include unobserved actors and to keep a subscription active across
dimension changes.

An unsupported `kind` raises `ValueError`. A runtime filter for an unobserved
actor raises `LookupError`. If a filtered actor leaves view or is replaced,
its subscription ends and its wait raises `LookupError`. A dimension change
ends runtime-filtered subscriptions and all pending waits, which raise
`RuntimeError`. A deadline raises `TimeoutError`. Connection end closes all
subscriptions and raises `ConnectionError` in pending waits.

## Observe entity hurt and death

`await bot.wait_for_entity_health_event(kind="hurt", runtime_id=runtime_id)`
waits for a received hurt event for the remote entity currently tracked under
that runtime ID. Use `kind="death"` to wait for death, or omit `kind` to accept
both. Omit `runtime_id` to observe any tracked remote entity. An
`EntityHealthEvent` contains the event `kind` and the immutable `entity`
snapshot at that packet. Actor events supply no damage amount or remaining
health through this API. Use `wait_for_death` to observe the local player's
death.

`bot.subscribe_entity_health_events(callback, kind=..., runtime_id=...)`
applies the same filters and delivers events to a synchronous callback. It
returns an idempotent unsubscribe function. Runtime filters bind the current
instance, so a filtered subscription ends when the entity leaves view, its
runtime ID is reused, or the dimension changes. A subscription without a
runtime filter continues across dimension changes. Both methods send no packets.

An unsupported `kind` raises `ValueError`. A runtime filter for the local
player or an unobserved entity raises `LookupError`, as does a pending wait
whose instance leaves view or is replaced. A dimension change ends pending
waits with `RuntimeError`. A deadline raises `TimeoutError`, and connection
end raises `ConnectionError` in pending waits and ends all subscriptions.

## Observe remote entity attributes

Each `EntityState` from `bot.observed_entities` has an immutable `attributes`
tuple for the current remote instance. An actor spawn can report attribute names
and their minimum, current, and maximum values. A player spawn starts with an
empty tuple. Later attribute packets replace entries by exact name and preserve
entries omitted from the packet. The tuple contains the last values the server
sent, which can become stale.

Each `EntityAttribute` has `name`, `min_value`, `current_value`, `max_value`,
`default_min_value`, `default_value`, `default_max_value`, and `modifiers`.
Omitted defaults are `None`. An actor spawn omits the modifier list, giving
`modifiers=None`. An update reporting an empty modifier list gives
`modifiers=()`. Each `EntityAttributeModifier` carries `id`, `name`, `amount`,
the numeric `operation` and `operand`, and the `serialize` flag.

```python
sheep = next((entity for entity in bot.observed_entities
              if entity.actor_type == "minecraft:sheep"), None)
if sheep is not None:
    for attribute in sheep.attributes:
        print(attribute.name, attribute.current_value)

    update = await bot.wait_for_entity_attribute_update(runtime_id=sheep.runtime_id)
    for attribute in update.entity.attributes:
        if attribute.name in update.names:
            print(update.tick, attribute.name, attribute.current_value)
```

`bot.subscribe_entity_attribute_updates(callback, runtime_id=...)` calls a
synchronous callback with an `EntityAttributeUpdate` after each matching update
packet is applied, including repeated values and empty reports. Spawn reports
only initialize the snapshot. The event contains the immutable `entity`
snapshot at that packet, the packet's `tick`, and `names` in first occurrence
order. When a name occurs more than once, the last entry supplies its value.
Both methods send no packets. The subscription returns an idempotent unsubscribe
function, and the wait accepts `timeout=None` for no deadline.

Omit `runtime_id` to observe any tracked remote entity. A runtime filter binds
the current instance and raises `LookupError` for a local or unobserved target.
Removal or replacement ends its filtered subscription and raises `LookupError`
in its pending wait. Dimension changes end filtered subscriptions and raise
`RuntimeError` in all pending waits. Unfiltered subscriptions continue across
dimension changes. A deadline raises `TimeoutError`. Disconnection ends all
subscriptions and raises `ConnectionError` in pending waits.

## Observe remote entity effects

Each `EntityState` from `bot.observed_entities` has an immutable `status_effects`
tuple sorted by numeric effect ID. It starts empty for each newly visible
remote instance. An add or update stores the reported effect by ID, and a
remove drops that ID. The SDK retains the last reported `duration_ticks`
until a later packet changes or removes the entry. The duration can become
stale as time passes.

`bot.subscribe_entity_effect_updates(callback, runtime_id=...)` calls a
synchronous callback with an `EntityEffectUpdate` after each matching
`MobEffect` add, update, or remove packet, including repeats and removals of
an unrecorded ID. It returns an idempotent unsubscribe function.
`await bot.wait_for_entity_effect_update(runtime_id=..., timeout=15)` waits
for the next matching update. Use `timeout=None` to wait without a deadline.
Both methods send no packets.

Each update has the immutable `entity` snapshot after the packet was applied,
the `action` (`"add"`, `"update"`, or `"remove"`), and the packet's `tick`.
This tick can be zero and is independent of the bot's input ticks. The `effect`
is a `PlayerEffect` with `id`, `amplifier`, `duration_ticks`, `show_particles`,
and `ambient` copied from the packet. Remove packets can carry a nonzero duration.

```python
update = await bot.wait_for_entity_effect_update()
print(update.entity.runtime_id, update.action, update.effect.id)
print(update.entity.status_effects)
```

Omit `runtime_id` to observe any tracked remote entity. A runtime filter binds
the current instance and raises `LookupError` for a local or unobserved target.
Removal or replacement ends the filtered subscription and raises `LookupError`
in its pending wait. Dimension changes end filtered subscriptions and raise
`RuntimeError` in all pending waits. Unfiltered subscriptions continue across
dimensions. A deadline raises `TimeoutError`. Disconnection ends all
subscriptions and raises `ConnectionError` in pending waits.

The tuple covers `MobEffect` packets received while that actor instance was
visible. An empty tuple means there are no effects currently recorded from
`MobEffect` packets for the actor.

## Observe visible-effect metadata

`EntityState.visible_effects_value` stores the signed integer reported by
the server's `VISIBLE_MOB_EFFECTS` metadata. `None` means the value is
unknown, and `0` is a reported value. The spawn packet can supply the initial
value. Later packets that omit the field or carry an unexpected type preserve
the last valid value.

`EntityState.visible_effect_ids` reads the IDs encoded in the protocol 2225
metadata as a tuple in descending order. A reported zero yields `()`. Missing
metadata or unsupported packing yields `None`. For unsupported packing,
`visible_effects_value` still holds the exact signed integer.

`bot.subscribe_entity_visible_effect_changes(callback, runtime_id=...)`
calls a synchronous callback with an immutable `EntityVisibleEffectUpdate`.
The event includes the committed `EntityState` and the packet tick. The
first valid live metadata value and each subsequent change produce an
event. Use `await bot.wait_for_entity_visible_effect_change(runtime_id=...,
timeout=15)` for the next matching update. Both methods send zero packets.

Omit `runtime_id` to observe any tracked remote actor. A runtime filter
binds to the current instance. An untracked actor or the local player
raises `LookupError`, as does a filtered wait whose actor leaves view,
is replaced, or has a conflicting roster identity. A dimension change ends
filtered subscriptions and raises `RuntimeError` in pending waits.
Unfiltered subscriptions continue across dimensions. The returned
unsubscribe function is idempotent. A deadline raises `TimeoutError`.
Use `timeout=None` to wait without a deadline. Connection end closes
subscriptions and raises `ConnectionError` in pending waits.

Visible-effect metadata is a visual observation and represents up to eight IDs.
Hidden effects may leave the value unchanged, and even a tuple with eight IDs
can omit other active effects.
`EntityState.status_effects` continues to reflect separately received
`MobEffect` packets, which can report effect IDs, amplifiers, and durations.

## Observe native entity actor events

`bot.subscribe_entity_actor_events(callback, runtime_id=..., event_id=...)`
calls a synchronous callback with one `EntityActorEvent` for each matching
packet, including repeats, and returns an idempotent unsubscribe function.
Use
`await bot.wait_for_entity_actor_event(runtime_id=..., event_id=..., timeout=15)`
for the next matching packet, or set `timeout=None` to wait without a deadline.
Both methods send no packets.

Omit `runtime_id` to observe any currently tracked remote entity. Omit
`event_id` to receive events without filtering by ID, or pass a native
`ActorEvent` member or an integer from 0 through 255. An invalid filter
value raises `ValueError`.
Each immutable `EntityActorEvent` contains the packet's numeric `event_id`,
opaque integer `data`, and `fire_at_position` as an `(x, y, z)` tuple or
`None`. Its `entity` is the immutable `EntityState` tracked under the
packet's runtime ID at packet time. The snapshot includes the unique
`entity_id` when known. For a player awaiting a roster entry, it is `0`.

```python
ActorEvent = bot.protocol.binding.ActorEvent
event = await bot.wait_for_entity_actor_event(event_id=ActorEvent.EAT_GRASS)
print(event.entity.runtime_id, event.event_id)
```

`EAT_GRASS` is the server's grazing notification. Observe the block
separately to confirm a terrain change.
[Read an observed block](world.md#read-an-observed-block)

A runtime filter binds to the current entity instance. If the entity leaves
view or its runtime ID is reused, a filtered subscription ends and a pending
filtered wait raises `LookupError`. A runtime filter for the local player
or an unobserved entity also raises `LookupError`. Dimension changes end
filtered subscriptions and raise `RuntimeError` in pending waits.
Subscriptions without a runtime filter continue across dimension changes.
A deadline raises `TimeoutError`. Disconnection removes all subscriptions
and raises `ConnectionError` in pending waits.
