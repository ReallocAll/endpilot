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

## Equip a harvest tool from a chest

`endpilot.harvest.equip_harvest_tool_from_chest()` first searches the main
inventory for a tool for the observed block. It takes one from the allowed
chest slots if needed. Read `result.status`, `result.tool`, and
`result.transfer` together to tell whether the tool was taken and equipped.
The tutorial shows how to prepare the chest and confirm the pickup.

## Collect several blocks

`collect_targets` removes duplicate coordinates, then attempts the remaining
positions in the supplied order up to `count`. Omit `count` to attempt all unique
positions. Each target gets its own `ticks`, `search_radius`, and `timeout`
settings, as in `collect_block`. The first result with a status other than
`collected` ends the batch. The returned `BlockCollectionBatch` records the
results of the attempted targets. When an exception interrupts collection,
read `collection_progress` on the original exception for any known progress.

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

Read `CountedBlockCollection.collected_count` for the confirmed increase of
`item_id` in the main inventory. A single pickup can exceed the remaining
count. `results` contains the evidence for each attempted or skipped block.
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

## Collect blocks and existing drops together

Pass block coordinates and current item snapshots from `bot.observed_drops`
to `bot.collect_mixed_targets()`. Targets are deduplicated before the optional
`count` limit. A drop is bound to its observed instance by its runtime and
unique IDs. The returned `MixedCollectionBatch.results` contains the attempted
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
and `observed_block` to check the targets and chest. Call
`start_collect_and_store` with the following arguments.

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
has insufficient evidence to determine the quantity moved. The same bot
stays online and can run another task or use the built-in tools before
`disconnect`. [MCP server example](../../examples/collect_and_store_mcp.py)
