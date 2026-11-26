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

## Choose a goal

| Goal | What it matches |
| --- | --- |
| `FootGoal((x, y, z))` | One exact feet block. |
| `XZGoal((x, z))`, `NearXZGoal((x, z), radius)`, `YGoal(y)` | A feet column, a horizontal radius around a column, or a feet block height. |
| `NearGoal((x, y, z), radius)` | A three-dimensional distance from the bottom center of the candidate feet block to a world point. |
| `BlockFaceGoal(position)` | A stance at a horizontal face of an observed block on the same level. Pass the resulting position and face to `open_container()` when approaching an ordinary chest. |
| `BlockInteractionGoal(position, max_distance=5.0)` | A clear ray to one of the sampled aim points on a supported block outline. The match includes `click_pos` for `use_item_on()`. |
| `EntityNearGoal(entity.entity_id, radius)` | A stance near an observed remote player or ordinary villager in the current session, identified by its unique ID. A target that leaves view becomes unknown. |
| `AnyGoal((goal_a, goal_b))`, `AllGoal((goal_a, goal_b))` | An endpoint satisfying any or all children. Matching block faces follow the child order. |

The [chest approach example](../../examples/approach_chest.py) uses the
`approach_block()` shortcut and opens the chest from the selected face. For
window lifecycle and item transfers, see [Work with containers](containers.md).

## Read the result

`plan_navigation()` sends no packets. It returns an immutable snapshot of
cached observations. A found route includes the start and endpoint in `nodes`,
any matching faces in `matches`, the total cost as a `Fraction`, and the
number of cells expanded. `NavigationRules` bounds the search on each axis
from the start, and `NavigationCosts` weights walking, ascent, and descent.

Both planning and execution can return `unknown` when missing observations
could affect the result, or `unreachable` when the bounded search rules out
a route. For an interaction goal, `unreachable` applies to the sampled aim
points within the search bounds. Request missing terrain with
`request_sub_chunk()` and wait for the observations before planning again.

Execution can replan as the terrain or goal changes. Replans share the
original bounds and the remaining search, frame, and time budgets. `budget`
means an applicable limit was exhausted. The `ticks` limit includes landing
waits and braking before a replan. Terminal input release can take additional
frames within `timeout`. The result's `frames` counts completed moving sends
and `expanded` counts all search work for the action.

The standing route uses observed support and clearance. It supports ordinary
walking, one-block height changes, and specific observed layouts for
one-block gaps, two-block drops, and ladders. Still-water crossings are limited
to supported pool layouts and require `NavigationRules(allow_swimming=True)`.
Elevation changes require server landing evidence before continuing. Use
`reached` and the final match to choose a click, then observe the server
effect separately.
