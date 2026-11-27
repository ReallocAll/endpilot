# Plan and execute navigation

Use `Bot.plan_navigation()` to inspect a route around an obstacle and
`Bot.navigate()` to reach a stance from which the bot can interact with a block.
This tutorial uses a stone block to obstruct the view of a furnace and verifies
the click by reading a slot from the window opened by the server.

## Prepare the world

Complete the [quickstart](../../README.md#try-it-offline) with the NetherNet
extra. Use a local offline-mode Bedrock Dedicated Server (BDS) on protocol
2225 and a disposable Overworld with `allow-cheats=true` and
`difficulty=peaceful`. Join in ordinary survival mode.

Run this once in the BDS console to keep the test area loaded.

```text
tickingarea add circle 8 220 8 1 endpilot_tutorial true
```

Save the Python example below as `navigate_furnace.py`. Adjust the URL in
the script if your BDS uses a different signaling address, then run it.

```sh
python navigate_furnace.py
```

When the script prompts you, run these commands in the BDS console, then press
Enter in the Python terminal.

```text
fill 0 219 0 15 219 15 minecraft:stone
fill 0 220 0 15 223 15 minecraft:air
setblock 8 221 8 minecraft:stone
setblock 9 221 8 minecraft:furnace
replaceitem block 9 221 8 slot.container 2 minecraft:diamond 4
gamemode survival Knight
clear Knight
tp Knight 7.5 220 8.5
```

## Interact from a navigation stance

`BlockInteractionGoal` seeks a standing position with a clear, in-range ray
from the bot's eye to a supported aim point. Use the returned execution
match to submit the click at the observed face and block-local point. Arm
the window waiter before clicking so it can see the server's response.

```python
import argparse
import asyncio

from endpilot import BlockInteractionGoal, Bot, NavigationRules


TARGET = (9, 221, 8)


async def run(stop: bool) -> None:
    bot = Bot("Knight")
    try:
        await bot.join_nethernet("http://127.0.0.1:19132")
        await asyncio.to_thread(
            input, "Prepare the furnace and teleport the bot, then press Enter. ",
        )
        await bot.wait_for_position_sync()
        await bot.request_sub_chunk(TARGET)
        await bot.wait_for_column_coverage((0, 0), range(13, 14))

        goal = BlockInteractionGoal(TARGET, max_distance=5.0)
        rules = NavigationRules(search_radius=3, vertical_radius=0)
        plan = bot.plan_navigation(goal, rules=rules)
        print("Plan:", plan.status, plan.nodes, plan.cost, plan.expanded)
        if plan.status != "found":
            return

        if stop:
            async with asyncio.TaskGroup() as tasks:
                route = tasks.create_task(bot.navigate(goal, rules=rules))
                await bot.wait_ticks(2)
                await bot.stop_moving()
            result = route.result()
        else:
            result = await bot.navigate(goal, rules=rules)

        print("Navigation:", result.status, result.stand_position, result.frames)
        if stop or result.status != "reached":
            return

        hit, = result.matches
        print("Aim:", hit.position, hit.face, hit.click_pos)
        binding = bot.protocol.binding

        def opened_target(raw):
            pos = bot.protocol.decode(raw).pos
            return (pos.x, pos.y, pos.z) == hit.position

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(bot.packets.wait_for_packet(
                binding.ContainerOpenPacket.ID, match=opened_target,
            ))
            await asyncio.sleep(0)
            await bot.use_item_on(hit.position, face=hit.face, click_pos=hit.click_pos)

        try:
            slots = await bot.wait_for_container_slots()
            print("Furnace output:", slots[2].stack_size)
        finally:
            await bot.close_container()
    finally:
        await bot.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true")
    asyncio.run(run(parser.parse_args().stop))
```

The plan prints `found` and a route starting at feet block `(7, 220, 8)`.
The bot moves out from behind the obstacle and reports `reached`. The aim
identifies the furnace, and the server's window contents report
`Furnace output: 4`. The window confirms that the click opened the furnace.

## Navigate to place a block

Use `BlockPlacementGoal` with the destination cell for an ordinary adjacent
placement. Navigation looks for a clear, in-range hit on a supporting face
that adjoins that cell. It requires observed air in both destination layers,
a trusted vanilla full-cube collision shape and outline on the support, and
observed air in the support's extra layer. The stance leaves the destination
clear of the bot's feet and head.

With the desired block item selected in survival mode, pass the final match to
`place_block()`. The match's `destination` identifies the target, and `position`
identifies the clicked support.

```python
from endpilot import BlockPlacementGoal

goal = BlockPlacementGoal((10, 220, 8))
route = await bot.navigate(goal)
if route.status == "reached":
    hit, = route.matches
    placement = await bot.place_block(
        against=hit.position, face=hit.face, click_pos=hit.click_pos,
        expected_block_name="minecraft:stone",
    )
    print(hit.destination, placement.reason, placement.world_change)
```

Navigation completes at the aiming stance. `place_block()` attempts the click
and reports the resulting server observations. [Placement reference](../reference/testing.md#place-a-block)

## Stop a route

Run the same script with `--stop` and repeat the setup. The script asks the
active route to stop after two client ticks and awaits its result. A route
interrupted while active reports `stopped`. A route that finishes before the
stop request keeps its completed result. This run ends before clicking.

```sh
python navigate_furnace.py --stop
```

`await bot.stop_moving()` waits for the active action's input release. A new
`navigate()` call starts from the bot's current stance. Cancelling the
navigation task also interrupts the route and propagates
`asyncio.CancelledError` to its caller.

## Observe adopted routes

Pass `on_plan` to see the route this navigation call adopts and any replacement
routes it adopts as observations change. The synchronous callback runs before
the first movement frame for each route and receives an immutable
`NavigationPlan`. For example, keep the snapshots to inspect after the call.

```python
plans = []
result = await bot.navigate(goal, rules=rules, on_plan=plans.append)
for plan in plans:
    print(plan.start, plan.nodes, plan.cost, plan.expanded)
```

A found route can contain just the current feet block. With no movement frames
remaining, the callback receives a route only if the bot is already at the
goal. An unsuccessful search delivers no snapshot. Read `result.status`
for the outcome of the action and `result.expanded` for its total search work.
Each snapshot's `expanded` counts the work of its own search. An exception from
the callback ends this navigation and propagates to its caller after movement
cleanup. Time spent in the callback counts toward `timeout`.

## Observe a navigation action

Pass `on_event` to collect immutable `NavigationEvent` objects from one
`navigate()` call. Every event carries the original `goal`.

```python
events = []
result = await bot.navigate(goal, rules=rules, on_event=events.append)
for event in events:
    if event.kind == "plan":
        print(event.plan.status, event.adopted, event.plan.expanded)
    elif event.kind == "invalidated":
        print(event.reason, event.plan.endpoint)
```

| Event | When it runs |
| --- | --- |
| `started` | After argument validation, before waiting for the movement lock. |
| `plan` | When an execution search returns a `plan`, including unsuccessful searches. `adopted` tells whether the search produced a route eligible for `on_plan`. This event comes before `on_plan` and the first input for that route. |
| `invalidated` | When the action abandons an adopted `plan`. `reason` is `position_changed`, `stance_unavailable`, `edge_unavailable`, `goal_unmatched`, or `dimension_changed`. A dimension reload can invalidate a route even when the dimension ID stays the same. |
| `transition_completed` | When the action completes a special edge in the adopted `plan`, such as a confirmed ascent or descent, before planning from the observed landing. |
| `finished` | Once after the action exits and releases any movement lock it acquired. `result` is the exact object returned, including for `stopped` and `budget`. An action that raises delivers `reason="cancelled"` or `reason="error"` and propagates the exception. |

The synchronous observer runs on the event loop. The event loop's exception
handler receives errors from `on_event` and the action continues. Time spent in
the observer before completion counts toward `timeout`.

## Choose a goal

| Goal | What it matches |
| --- | --- |
| `FootGoal((x, y, z))` | One exact feet block. |
| `XZGoal((x, z))`, `NearXZGoal((x, z), radius)`, `YGoal(y)` | A feet column, a horizontal radius around a column, or a feet block height. |
| `NearGoal((x, y, z), radius)` | A three-dimensional distance from the bottom center of the candidate feet block to a world point. |
| `BlockFaceGoal(position)` | A stance at a horizontal face of an observed block on the same level. Pass the resulting position and face to `open_container()` when approaching an ordinary chest. |
| `BlockInteractionGoal(position, max_distance=5.0)` | A clear ray to one of the sampled aim points on a supported block outline. The match includes `click_pos` for `use_item_on()`. |
| `BlockPlacementGoal(destination, max_distance=5.0)` | A clear ray to a supported face adjoining the observed air destination. The match includes `position`, `face`, and `click_pos` for `place_block()`. |
| `EntityNearGoal(entity.entity_id, radius)` | A stance near an observed remote player or ordinary villager in the current session, identified by its unique ID. A target that leaves view becomes unknown. |
| `EntityVisibilityGoal(entity.entity_id, aim_offset=(0, 1.4, 0), max_distance=2.2)` | A stance with a clear block ray from the standing eye to a chosen point relative to an observed remote player or ordinary villager. |
| `AnyGoal((goal_a, goal_b))`, `AllGoal((goal_a, goal_b))` | An endpoint satisfying any or all children. Matching block faces follow the child order. |

`EntityVisibilityGoal` adds the world-axis `(x, y, z)` offset to the target's
latest verified feet position. The ray starts 1.52 blocks above the standing
player's feet. The distance to the aim point can equal `max_distance`. A confirmed
block outline on the segment rules out that stance. An unresolved target position
or a potentially obstructing outline can make the route `unknown`. Execution
follows the same observed entity instance and checks the ray from the actual
standing position at arrival. Held sneak input, an observed crouch, or an
explicit airborne observation leaves the current stance `unknown` for this goal.
Planning can still find a future standing position.
Every plan or execution gets a separate limit of `NavigationRules.max_expansions`
ray cells, shared by its visibility checks. Exhausting that limit returns
`budget`. The `expanded` count still reports standing nodes.
After `reached`, use `look_at()` with the latest aim point if needed and
observe the server's response to the action you choose.

The [chest approach example](../../examples/approach_chest.py) uses the
`approach_block()` shortcut and opens the chest from the selected face. For
window lifecycle and item transfers, see [Work with containers](containers.md).

## Follow an entity around obstacles

Pass `follow=True` with a top-level `EntityNearGoal` to keep a route active after
arrival. The action follows the same observed entity instance for its entire
lifetime.

```python
from endpilot import EntityNearGoal

goal = EntityNearGoal(entity.entity_id, radius=1.5)
result = await bot.navigate(goal, follow=True, ticks=200, timeout=25)
print(result.status, result.stand_position, result.frames)
```

After the bot releases its movement input and confirms arrival, it keeps
exclusive control of movement while it waits for observations. The hold
sends no input frames and spends no search expansions. If the entity moves
out of range, the bot replans from its latest stance within the original
search bounds, using the remaining tick and expansion budgets. Release
frames count toward the remaining tick budget when following resumes.
`result.frames` counts moving frames across all legs.

At the deadline, the result is `reached` if the latest settled stance still
matches the same entity after a confirmed arrival. The result is `budget`
if time expires while pursuing the target. Removal, replacement, unresolved
target feet, or a dimension change can yield `unknown`. Use
`await bot.stop_moving()` to end the action with `stopped`.

## Read the result

`plan_navigation()` sends no packets. It returns an immutable snapshot of
cached observations. A found route includes the start and endpoint in `nodes`,
any matching faces in `matches`, the total cost as a `Fraction`, and the
number of cells expanded. `NavigationRules` bounds the search on each axis
from the start, and `NavigationCosts` weights walking, ascent, and descent.

Both planning and execution can return `unknown` when missing observations
could affect the result, or `unreachable` when the bounded search rules out
a route. For interaction and placement goals, `unreachable` applies to the
sampled aim points within the search bounds. Request missing terrain with
`request_sub_chunk()` and wait for the observations before planning again.

Execution can replan as the terrain or goal changes. Replans share the
original bounds and the remaining search, frame, and time budgets. `budget`
means an applicable limit was exhausted. The `ticks` limit includes landing
waits and braking before a replan. Terminal input release can take additional
frames within `timeout`. The result's `frames` counts completed moving sends
and `expanded` counts all search work for the action.

The standing route uses observed support and clearance. It supports ordinary
walking, one-block height changes, and specific observed layouts for
one-block gaps, platform drops, and ladders. A two- or three-block drop needs
observed dry ordinary stone at the ledge and under the first three columns of
the lower platform along the route, with clearance in both block layers.
Navigation waits for server corrections matching submitted inputs to confirm
the descent and grounded landing, then settles before walking on. It returns
`budget` before stepping off if the remaining frames or time fall below the
minimum for the drop.

Ladder descents support four to six rungs in the observed stone-backed layout.
Set `vertical_radius` high enough to include the landing. For a direct six-rung
descent, use `NavigationRules(vertical_radius=6)`.
Still-water crossings are limited to supported pool layouts and require
`NavigationRules(allow_swimming=True)`.
On protocol 2225, ordinary same-level walking can pass through observed vanilla
`minecraft:dandelion`, `minecraft:short_grass`, and `minecraft:wildflowers`
in the primary feet layer.
The extra feet layer and both head layers must contain observed air, and the
floor must provide safe support. The same vegetation is allowed at the
launch stance of a one-block descent, but not at its landing or runout.
A one-block ascent can enter beneath a known full-cell vanilla leaf canopy
when the launch and both landing body layers still have clear space.
Elevation changes require server landing evidence before continuing. A ladder
descent waits for a grounded server correction at the ladder foot before
stepping off. Use `reached` and the final match to choose a click, then observe
the server effect separately.

With standard Survival movement, navigation can cross a straight lane of
three contiguous dry lower smooth-stone slabs between observed dry stone banks.
Both clearance levels must have observed air in the main and extra block layers.
The crossing is one edge between integer-height standing cells and costs four
walking steps. Missing terrain that could affect the route can yield `unknown`.
The crossing requires grounded server corrections at slab height and then bank
height, each matched to a submitted input. The bot releases movement input and
settles on the far bank before resuming ordinary walking.
