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

## Find a nearby or targeted entity

`bot.nearest_entity(match=None)` searches the observed actors in the current
dimension, excluding the local player. With a predicate, only matching actors
are eligible. The query uses three-dimensional distance from the bot's last
synchronized feet to each observed position and breaks ties by smaller
`entity_id`. It returns `None` when the local feet position is unknown or no
candidate matches.

`bot.entity_at_cursor(max_distance=3.5)` reads the bot's current view ray from
the cache. A `"hit"` gives the actor and hit distance, `"miss"` reports that no
supported actor was hit, and `"unknown"` means missing or ambiguous pose,
terrain, or actor geometry could affect the result. Both queries send no packets.

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
visible. A server can report some effects only in actor metadata. For example,
stock BDS 1.26.66 sent visible Fire Resistance on a sheep through metadata
alone. Those changes remain available through raw packets. An empty tuple means
there are no effects currently recorded from `MobEffect` packets for the actor.

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
