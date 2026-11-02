# First-generation known-flat navigation (experimental)

This API is available only on the stacked navigation branch. It requires the
unmerged WorldQuery and flat Movement Controller PRs. Do **not** treat the
temporary integration branch or its code as part of released `main`.

## Single-writer usage

```python
import asyncio
from endpilot import BotOptions, EventKind, Runtime, event_mask
from endpilot.navigation import GoalNear, Navigator
from endpilot.world import WorldQuery

async def main(server_url):
    async with Runtime() as runtime:
        bot = runtime.add_bot(BotOptions(
            server_url=server_url,
            transport="raknet" if server_url.startswith("raknet://") else "nethernet",
            validate_all_packets=True,
            client_cache=False,
        ))
        world = WorldQuery(bot)
        navigator = Navigator(bot, world)

        def observe(source, event):
            if source is not bot:
                return
            frame = navigator.on_event(event)  # Also calls world.observe.
            if frame is not None:
                bot.set_input(frame)  # The application's sole input writer.

        mask = 0
        for kind in (EventKind.tick, EventKind.state, EventKind.position,
                     EventKind.dimension, EventKind.packet_decoded):
            mask |= event_mask(kind)
        unsubscribe = runtime.subscribe(observe, events=mask)
        try:
            await bot.wait_ready(60)
            # First ensure the relevant subchunks are observed. For example,
            # bot.request_sub_chunks(dimension, chunk_x, chunk_z, heights)
            # and check world.can_stand(x, feet_y, z) is True.
            await navigator.goto(GoalNear(10, 100, 10))
        finally:
            navigator.stop()
            unsubscribe()
```

Subscribe **before** waiting for Ready, so WorldQuery receives StartGame and
world packets. This example shows input ownership, not a ready-made test world:
a real application must fetch/observe the target subchunks and confirm they
contain supported block hashes. If other Behaviors are active, compose the
returned frame with their intents within the **same** writer callback; do not
install another writer for navigation.

## Geometry and guarantees

`GoalNear(x, feet_y, z, radius=0)` takes integer block X/Z coordinates and
integer feet-level Y. For example, on solid stone at Y=99, standable air has
`feet_y=100`, while the corresponding observed player position is normally
`(x + 0.5, 101.62001, z + 0.5)`. `radius` measures permissible **horizontal
block-center** distance, and is limited to 0..8.

Navigation requires the bot to start within 0.15 blocks of a cell center, with
a grounded eye height within 0.15 blocks of the probed flat-ground offset.
These deliberately conservative restrictions prevent unverified clearance
from being mistaken for traversable terrain.

The action graph uses four cardinal and four diagonal actions. All target
cells must pass `WorldQuery.can_stand()`, and diagonals require both orthogonal
side cells to be standable. A single supported cell is a full, one-block cube
whose horizontal center leaves at least 0.2 blocks of margin from a player's
0.6-wide collision body. No jump, height change, water, slabs, fences,
doors, dynamic entity obstacles or path smoothing are supported.

The planner returns `PlanResult.status`: `complete`, `need_world`,
`unreachable` or `budget_exhausted`. Unknown terrain cannot form a complete
route. The default search budget is 4096 expanded nodes; the event loop is
yielded after each 64-node batch.

After each waypoint has settled, the next block-center target is given to the
existing Movement Controller. On observed block updates the remaining path is
revalidated. `stop()` invalidates the pending path and requests friction-only
coasting, while cancellation and session changes invalidate old intentions.

**Arrival is a prediction, not BDS authority.** The session's local status
can be updated by submitted input frames; no independent server position query
is available in the SDK. Real integration tests must obtain server-owned
coordinates using BDS `querytarget` and check both final error and post-stop
drift. A mismatch is a navigation failure even if `goto()` returned.

## Deferred work

No frontier/partial-path execution, chunk streaming, waypoint smoothing,
variable-height actions, replanning, entity collision, native heap, JPS, HPA*,
or long-distance guarantees are implemented. Do not add these optimizations
without matching real BDS evidence.
