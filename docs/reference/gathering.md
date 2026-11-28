# Mining and gathering

## Find a vein or mine from your current position

`bot.find_block_vein(position, max_blocks=100, max_distance=16, flood_radius=1)`
starts at an observed block and returns connected primary-layer blocks of
the same vanilla name, in breadth-first order. Read `.matches` for the block
positions and `.complete` to see whether the bounded search could account
for every reachable match. Unknown observations and truncation can make it
incomplete. The cache query sends no packets. Pass the positions to
`collect_targets()` to attempt the matches in that order.

`await bot.mine_block(position, face=None)` selects an observed reachable
face and aims without moving. It raises `MiningFaceUnavailable` before
starting the break if no face can be confirmed. Omit `face` to use the
default top face (`1`), or pass the face explicitly. A completed action
returns the observed `BlockUpdate`.

## Choose a tool for a specific drop

The ordinary tool ranking can retain a confirmed held pickaxe or shovel in
dry, grounded survival with Haste I when it and the best eligible tool both
cross the known vanilla block's first-tick threshold. If any required
observation is unavailable, the normal speed ranking applies. Equipping an
ordinary selection that already matches the full confirmed hand stack sends
no equipment packet.

Pass `requested_drop_id` to `bot.best_harvest_tool(position)` or
`await bot.equip_harvest_tool(position)` to choose for an observed block and a
specific drop. Look up the item network ID with the current session's
`bot.item_registry.id_for_name()`. Passing `None` uses the ordinary ranking.
An explicit drop ID requires `require_harvest=True`.

For iron and diamond ores, including their deepslate variants, request the
matching ore item to select a pickaxe with Silk Touch I. Request
`minecraft:raw_iron` or `minecraft:diamond` to select an eligible pickaxe
without Silk Touch. For `minecraft:web`, request `minecraft:string` to select
a sword or `minecraft:web` to select shears.

The query sends no packets. An unobserved block, an unsupported combination,
or a lack of matching tools returns `None`. When the equip call cannot find a
suitable tool at the start, it sends no packets. During an explicit drop
request, the equip call checks the observed block and inventory again before
opening, transferring, or selecting. A stale observation skips the affected
write and returns `None`. Any confirmed transfer stays applied if selection
later becomes invalid. The equip call closes any inventory screen it opened.

## Equip a harvest tool from a chest

`endpilot.harvest.equip_harvest_tool_from_chest()` first searches the main
inventory for a tool for the observed block. It takes one from the allowed
chest slots if needed. Read `result.status`, `result.tool`, and
`result.transfer` together to tell whether the tool was taken and equipped.
The tutorial shows how to prepare the chest and confirm the pickup.

`endpilot.harvest.equip_harvest_tool_from_chests()` accepts an ordered
sequence of `HarvestToolChest` entries at the player's current position.
It checks the main inventory first. An empty sequence limits the operation
to that inventory. A confirmed shortage in a chest opened by the operation
allows the next attempt after the server acknowledges the close. A borrowed
window stays with its caller and ends the search at that chest. Supply runs
from the player's current position. Mining is a separate operation.

Both chest helpers accept `max_tools`, a positive integer that defaults to
one. A higher limit lets the first supplying chest provide spare tools within
its authorized source and destination slots. The helper equips the best
confirmed tool when the limit is reached, eligible stock runs out, or the
authorized destination is known full. The remaining tools stay in inventory.
Check `transfer.confirmed_count` and `transfer.stop_reason` for the actual
result. An existing suitable tool takes priority and leaves the chests alone.

Multi-tool supply uses one normal-operation timeout, including for the
single-chest helper. A rejection, unconfirmed take, or stale observation ends
the attempt with the existing receipts preserved. Interrupted calls still
expose progress on the original exception.

`HarvestToolSupply` reports the terminal `status`, ordered `attempts` as
`HarvestToolAcquisition` snapshots, and the selected `tool` if known. The
timeout applies to the entire normal operation, including waiting for another
equipment operation. Settling a submitted window operation may take longer.
Inspect `harvest_supply_progress` on an interruption and `harvest_progress`
for the interrupted single acquisition when available.

## Collect several blocks

`collect_targets` removes duplicate coordinates, then attempts the remaining
positions in the supplied order up to `count`. Omit `count` to attempt all unique
positions. Each target gets its own `ticks`, `search_radius`, and `timeout`
settings, as in `collect_block`. The first result with a status other than
`collected` ends the batch. The returned `BlockCollectionBatch` records the
results of the attempted targets. When an exception interrupts collection,
read `collection_progress` on the original exception for any known progress.

Pass a fresh `CollectionTargetQueue` as `additions` to append targets while
`collect_targets` is active. Call `additions.extend(positions)` synchronously
on the same event loop. The returned tuple contains the newly admitted
coordinates, in input order. Initial and added targets share the `count`
limit. The SDK snapshots each added target's current block when admitting it.

`BlockCollectionBatch.positions` includes admitted targets that collection
did not reach. `results` and `active` report attempted and interrupted work.
The queue seals when the batch finishes, stops, or is cancelled. Use a new
queue for each call. Empty initial input or `count=0` completes immediately.

On a local offline BDS, replace the sample coordinates with two nearby blocks
and give the bot a suitable tool in its main inventory.

```python
import asyncio

from endpilot import Bot


async def main():
    bot = Bot("Knight")
    positions = [(2, 64, 1), (3, 64, 1)]
    try:
        await bot.join_nethernet("http://127.0.0.1:19132", load_announced_terrain=True)
        for x, y, z in positions:
            section_y = y // 16
            await bot.wait_for_column_coverage((x // 16, z // 16), range(section_y, section_y + 1))

        try:
            print(await bot.collect_targets(positions, count=2))
        except (Exception, asyncio.CancelledError) as error:
            progress = getattr(error, "collection_progress", None)
            if progress is not None:
                print(progress)
            raise
    finally:
        await bot.close()


asyncio.run(main())
```

## Collect by item count

`await bot.collect_by_count("oak_log", item_id=oak_log_id, count=4, radius=8)`
uses the block selectors from `find_blocks` and a separate session item
network ID for the expected drop. Look up `oak_log_id` with
`bot.item_registry.id_for_name("minecraft:oak_log")` once the registry is
available. At the start of the call, it snapshots
observed matching blocks inside `radius` in distance order. It tries each
coordinate once and skips a block if its observation changes before START in
the same world generation. It stops at the requested count, when it runs out
of candidates, or at any other failure. `ticks`, `search_radius`, and `timeout`
apply to each attempt as in `collect_block`.

Pass `refresh_candidates=True` to check the cache again when the bot has
tried the last candidate and still needs more items. Each check uses the
initial search center, radius and block selectors. It sends no packets and
ends the collection if there are no new matching coordinates. A coordinate
admitted in an earlier round cannot be admitted again. `positions` lists
all admitted coordinates in round order, with distance order within each
round. `results` lists attempted and skipped coordinates in that same
order. A failure or a timeout still ends the collection, even on the last
candidate of a round. With refresh enabled, an unconfirmed tool refill
or an approach that was stopped or exhausted its budget ends collection
even if the target also changed.

For `minecraft:web` targets, both counted methods use the requested item to
choose the tool. Request `minecraft:string` to use a wooden, stone, golden,
copper, iron, diamond, or netherite sword. Request `minecraft:web` to use
shears. The same requirement applies to authorized refill chests. The bot
checks the equipped tool again before starting to mine. A confirmed shortage
ends the attempt with `tool_unavailable` before mining.

For diamond ore and deepslate diamond ore, request `minecraft:diamond` to
use an eligible pickaxe without Silk Touch. Request the matching ore item
to use an eligible pickaxe with Silk Touch I. Both counted methods apply
this rule to inventory tools and authorized refill chests. The pickaxe's
enchantments must be readable. Fortune pickups count by the actual quantity
confirmed in inventory.

Read `CountedBlockCollection.collected_count` for the confirmed increase of
`item_id` in the main inventory. A single pickup can exceed the remaining
count. `results` contains the evidence for each attempted or skipped block.
Each `BlockCollection.drops` keeps the new item actors observed in the
harvested cell, including drops of other items. For example, when mature
wheat yields wheat and seeds, a request for wheat pursues the wheat actor.
Two actors with the requested ID, or a requested actor alongside one whose
item ID is unknown, leave the pickup ambiguous. If no requested actor
appears, the bot waits until the observation window ends without pursuing
other items. The result is `drop_unobserved` unless the bot confirmed its
pickup of the sole observed drop and its matching inventory gain. In that
case it reports `collected` with `timed_out=True` and credits zero toward
the requested count. `BlockCollection.pickup_drop` identifies the actor
from `drops` whose pickup and inventory gain were confirmed.
An interrupted call attaches its evidence to the exception as
`collection_progress`.

`await bot.collect_vein_by_count(seed, item_id=diamond_id, count=3)` uses
`find_block_vein` to snapshot connected blocks from `seed` in breadth-first
order. It accepts the same `max_blocks`, `max_distance`, and `flood_radius`
bounds as the search. `search_complete` describes that initial search. The
bot attempts the known candidates even if the search was incomplete and
uses the same count and stopping rules as `collect_by_count`. An unresolved
seed within the dimension's bounds yields an empty result with
`search_complete=False`. Observed air or a position outside the known height
yields an empty result with `search_complete=True`. An interrupted call
includes the initial search completeness in `collection_progress` if the
search finished. `collect_by_count` leaves `search_complete` as `None`.

## Refill harvest tools while collecting

`collect_targets`, `collect_by_count`, and `collect_vein_by_count` accept
`tool_chest=HarvestToolChest(chest_position, chest_slots=range(4, 7),
inventory_slots=range(14, 16))`. Import `HarvestToolChest` from
`endpilot.harvest`. If the bot confirms it lacks a required tool, it can
visit that chest once for the target and take a suitable tool from the
authorized slots. It waits for the chest to close before returning to the
target.

Pass `tool_chests=[first_chest, second_chest]` to authorize several entries
in order. The bot snapshots the sequence at the start of the call and tries
each entry at most once per target. An empty sequence disables refills.
Supply only one of the two options.

The bot tries the next entry after a confirmed shortage in the authorized
slots and a matching close of the window it opened, or after finding the
chest unreachable before clicking. It checks that the target and world
generation still match and that no window is open or opening before trying
another chest. An uncertain approach, transfer or close ends the attempt.
A counted collection also stops if the target changes during an unsettled
refill.

Read each block result's `tool_acquisition` for the final refill outcome.
With `tool_chests`, `tool_refill_attempts` contains the ordered immutable
tuple of `(chest, acquisition)` pairs. An acquisition can be `None` if the
attempt ended before an outcome was observed. The exception's
`collection_progress` includes known refill evidence in the attempted
results and the active target.

## Free inventory space during collection

Pass a `CollectionStorage` to `collect_targets`, `collect_by_count`, or
`collect_vein_by_count` to authorize a chest trip during a target attempt.

```python
from endpilot import CollectionStorage

storage = CollectionStorage(
    chest_position,
    "minecraft:cobblestone",
    inventory_slots=range(4, 5),
    chest_slots=range(0, 1),
    additional_positions=(backup_chest_position,),
)
batch = await bot.collect_targets(positions, storage=storage)
```

Pass one exact registered name or transferable network ID, or a nonempty
tuple of these selectors. The bot validates every selector against the current
session before starting the batch. A name and ID for the same item authorize
that item once. Inventory slot order determines which stack is selected.

The bot considers storage after observing the break and a single new drop
that is still present. Counted collection applies this check to its unique
requested drop and uses the configured item for the chest transfer.
It checks all 36 main inventory slots for a settled
server snapshot with no available space for that drop. An empty slot or a
same-item stack with possible spare capacity keeps collection on its usual
pickup path. The trip uses the remaining break and pickup timeout and the
target's movement and search budgets.

For storage after a break the bot chooses the first eligible authorized stack
in `inventory_slots` and attempts to store that entire stack in `chest_slots`.
Omit `additional_positions` for a single chest. Every listed chest uses the
same authorized item and slot ranges. When several chests are authorized,
the bot chooses the nearest untried chest by straight-line distance from its
observed feet to the block center, breaking ties in the configured order.
It tries another only after confirming no room, making zero transfer
requests, and receiving the matching acknowledgment of its own window
close. The original drop and source stack must still qualify.

After confirming the whole transfer and receiving the chest's matching
close, it continues with the original drop. The batch advances only after
the bot confirms its own pickup of that instance and the matching inventory
gain. Counted collection continues to count confirmed pickups even when the
same item was stored during the detour.

Set `before_dig_when_full=True` to check inventory before each target.
All 36 main slots must have settled observations. If every slot is occupied,
the bot stores each whole stack of an authorized item from `inventory_slots`,
in slot order. Before each write it checks that the registered identities still
match the batch authorization.
One authorized chest must have room for the entire source stack. The bot
prefers one destination slot and may use several authorized slots in the same
chest. It can try the next authorized chest after confirming insufficient
capacity for the current source and closing its own window. A partial transfer,
unsettled result, or slot identity change stops preflight storage.

The bot waits for its chest close and verifies an empty main slot before
starting the break. If it cannot establish these conditions, the target
reports `storage_unconfirmed`. A main inventory with a known empty slot
uses ordinary collection. The option defaults to `False`.

Read `BlockCollection.storage_attempt` for the last storage result. A
multi-chest trip also records the ordered `(position, result)` pairs in
`storage_attempts`, including confirmed and pending transfer steps. Preflight
storage records these pairs for each attempted source stack, so the same chest
can appear more than once. When storage after a break stops short, the batch
ends with `pickup_unconfirmed`, or with
`storage_incomplete` if the original pickup was already confirmed. The
exception's `collection_progress` includes known storage and pickup evidence
for the interrupted target.

The same three methods accept `storage` in scripts started with `run_script`.
Import `CollectionStorage` inside the script and pass it as shown above.
The worker gets snapshots of `storage_attempt` and the ordered
`storage_attempts` pairs. Read `attempt.transfer.confirmed_count` when a
transfer exists. A pending step retains its request ID with an unknown
confirmation. If an interruption with `collection_progress` goes uncaught,
`observe_script` includes it under `error.attributes.collection_progress`.

## Collect an observed drop

`await bot.collect_drop(runtime_id, ticks=120, timeout=15)` approaches an item
from `bot.observed_drops` and returns the original snapshot when the server
confirms this player's pickup of that instance. If the nearby direct path is
blocked by observed terrain and the drop has a known ground anchor, the bot
uses bounded ground navigation to reach a clear approach. A clear path or
insufficient terrain observations keep the direct approach. The bot also
checks the path as the drop moves. Direct movement, routing, and the final
approach share the call's movement and timeout budgets. Pass `ticks=0` to wait
for the pickup without walking.

## Collect blocks and existing drops together

Pass block coordinates and current item snapshots from `bot.observed_drops`
to `bot.collect_mixed_targets()`. Targets are deduplicated before the optional
`count` limit. A drop is bound to its observed instance by its runtime and
unique IDs. Each item leg uses the same ground approach with the supplied
`search_radius`. The returned `MixedCollectionBatch.results` contains the attempted
prefix, ending at the first target whose result differs from `"collected"` for
a block or `"picked_up"` for a drop. Inspect `collection_progress` if the batch
raises.

## Fish with an observed fishing rod

Select the rod and aim at water with `look_at()`. `await bot.fish()` casts,
observes a bite on the owned hook, and reels. For individual steps use
`cast_rod()`, `wait_for_fishing_bite()`, and `reel_rod()`.
`bot.fishing_hook` exposes the currently observed owned hook. Use
`cancel_fishing()` to recall it and confirm removal, including while a bite
wait is pending. `fish()` attempts the same cleanup if the bite wait times out
or is cancelled. A local rejection leaves the hook's first reel available.
Once a reel reaches the transport, later reels and cancellations for that hook
wait for removal. A disconnect or reader failure invalidates the hook and raises
`ConnectionError` in fishing actions.

A `FishingResult` records hook removal, observed bite, matching fishing drops
and pickup pairs `(item_runtime_id, collector_runtime_id)`. A result of
`received` carries the quantity confirmed by the personal pickup and a
matching main-inventory increase at the end of the reel window. The pickup
record remains available when a later inventory response reduces that gain.
BDS sends the fishing-drop flag and leaves the drop's owner
unspecified, so the item fields record what was observed. Observations can
report ambiguity, an unconfirmed pickup or inventory change, an empty reel,
or an interrupted reel. `reel_rod(timeout=...)` bounds the observation window.

## Collect blocks and store the pickups in a chest

On a local offline NetherNet server, give the bot an iron pickaxe and start it
near two diamond ore blocks and a chest on walkable terrain. Use the address
and coordinates for your world. The task collects the blocks in order, then
approaches the chest and stores the confirmed diamond pickups in slot 0.

```python
import asyncio

from endpilot import Bot
from endpilot.collect_and_store import collect_and_store


async def main():
    bot = Bot("Knight")
    positions = [(12, 220, 7), (13, 220, 9)]
    chest = (3, 220, 12)
    try:
        await bot.join_nethernet("http://127.0.0.1:19132", load_announced_terrain=True)
        for x, y, z in (*positions, chest):
            section = y // 16
            await bot.wait_for_column_coverage((x // 16, z // 16), range(section, section + 1))

        result = await collect_and_store(
            bot, positions, item="minecraft:diamond", chest=chest,
            chest_slots=range(1), ticks=320, search_radius=15,
        )
        print(result.picked_up_count, result.stored_count, result.stop_reason)
    finally:
        await bot.close()


asyncio.run(main())
```

`picked_up_count` is the quantity confirmed by the bot's matching pickups.
`stored_count` is the quantity confirmed by matching changes in the inventory
and the chest. If the bot picks up two diamonds and slot 0 has room for only
one, the result is `2`, `1`, `destination_full`.

To run the same task over MCP, install the SDK with the `mcp` and `nethernet`
extras and launch this command as a stdio server from the SDK source directory.

```sh
python -m examples.collect_and_store_mcp
```

Call `connect` once with the server URL and player name. If terrain is
missing, use `request_sub_chunk` to request the sections along the route
and `observed_block` to check the chest. To discover nearby ore without its
coordinates, call `find_observed_blocks` with `{"name": "diamond_ore"}`. It
returns up to 16 positions within 16 blocks and only searches the cache by
default. Set `"load_nearby": true` to request up to three nearby,
server-announced sections at the player's current height and wait up to six
seconds. It does not explore or request sections above or below. An empty
result with `complete: false` does not rule out more ore. Pass the returned
`positions` and the known chest to `start_collect_and_store`.

```json
{
  "positions": [[12, 220, 7], [13, 220, 9]],
  "item": "minecraft:diamond",
  "chest": [3, 220, 12],
  "chest_slots": [0, 1],
  "ticks": 320,
  "search_radius": 15
}
```

Call `observe_collect_and_store` to read the current phase and confirmed
counts. `stop_collect_and_store` cancels the task and waits for it to settle,
returning any confirmed partial counts. A transfer step with `confirmed: null`
has insufficient evidence to determine the quantity moved, even if
`stored_count` is zero. If the server drops the connection, save the task report
before calling `disconnect` and reconnecting: this example clears the old task
on disconnect and does not retry it. Reconnect with the same player identity
and inspect both affected slots before retrying.
[MCP server example](../../examples/collect_and_store_mcp.py)
