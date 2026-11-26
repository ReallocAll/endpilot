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
stack. `wait_for_player_equipment_change` reports selected-slot, main-hand
and armor changes.

Each listed player has a `uuid`, `name`, and `entity_id` from the server. The
`entity_id` is the unique actor ID. Use `uuid` to match player-list entries
across updates. Other fields include the build platform code, packed color and
player flags. `xuid`, `platform_online_id`, and `play_fab_id` are `None` when
unavailable.

`await bot.wait_for_player_list_update(action="add", timeout=15)` returns
matching entries from the first player-list packet dispatched after the wait
starts. Use `action="remove"` for removals and `player_uuid` for a particular
UUID. An add includes the player's metadata and can update an existing entry.
A remove provides the UUID. The wait raises `TimeoutError` at its deadline or
`ConnectionError` if the stream ends first.

Run `python examples/observe_player_list.py URL --action add`, then join the
same server from a second client with a different name after
`player_list_watcher_ready` appears.
[Player list example](../../examples/observe_player_list.py)

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
