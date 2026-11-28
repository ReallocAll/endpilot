# Work with containers

Open a known chest or chest minecart, wait for its server-reported contents,
and move items by exact slot or across a range of slots. Then use a specified
chest to replenish a missing harvest tool.

## Prepare a chest

Complete the [quickstart](../../README.md#try-it-offline) with the NetherNet
extra. Use a local offline-mode Bedrock Dedicated Server (BDS) on protocol
2225 and a disposable Overworld with `allow-cheats=true` and
`difficulty=peaceful`. The bot plays in survival mode.

Run this once in the BDS console to keep the test area loaded.

```text
tickingarea add circle 8 220 8 1 endpilot_tutorial true
```

Save the following Python example as `transfer_paper.py`. Adjust the URL in
the script if your BDS uses a different signaling address, then run it.

```sh
python transfer_paper.py
```

When the script prompts you, run these commands in the BDS console, then press
Enter in the Python terminal. The bot starts immediately west of a single
chest, so the click uses the west face (Bedrock face 4).

```text
fill 0 219 0 15 219 15 minecraft:stone
fill 0 220 0 15 223 15 minecraft:air
setblock 9 220 8 minecraft:chest
replaceitem block 9 220 8 slot.container 0 minecraft:paper 10
gamemode survival Knight
clear Knight
tp Knight 8.5 220 8.5
```

## Transfer items across slot ranges

Opening waits for the server window. `wait_for_container_slots()` then waits
for that window's contents. The example takes one sheet of paper into a known
empty inventory slot and returns it using exact slots. It then takes eight
sheets across allowed ranges and reopens the container to read back the
remaining stack. Finally it moves those sheets between ranges in the player's
own inventory.

```python
import argparse
import asyncio

from endpilot import Bot, transfer_inventory_range, transfer_range


CHEST = (9, 220, 8)


async def run(minecart: bool) -> None:
    bot = Bot("Knight")
    try:
        await bot.join_nethernet("http://127.0.0.1:19132")
        await asyncio.to_thread(
            input, "Prepare the container and teleport the bot, then press Enter. ",
        )
        await bot.wait_for_position_sync()
        await bot.request_sub_chunk(CHEST)
        await bot.wait_for_column_coverage((0, 0), range(13, 14))

        if minecart:
            carts = [
                entity for entity in bot.observed_entities
                if entity.actor_type == "minecraft:chest_minecart" and entity.entity_id
            ]
            if len(carts) != 1:
                raise RuntimeError("Expected one observed chest minecart with a known identity.")
            cart = carts[0]

        async def open_target():
            if minecart:
                return await bot.open_entity_container(cart.runtime_id)
            return await bot.open_container(CHEST, face=4)

        window = await open_target()
        try:
            slots = await bot.wait_for_container_slots()
            print("Opened:", window.position, window.entity_unique_id, len(slots))
            print("Initial stack:", slots[0].stack_size)

            taken = await bot.take_from_container(source_slot=0, destination_slot=5)
            print("Take one:", taken.result.name)
            if taken.result.name != "SUCCESS":
                return
            stored = await bot.put_into_container(source_slot=5, destination_slot=0)
            print("Return one:", stored.result.name)
            if stored.result.name != "SUCCESS":
                return

            result = await transfer_range(
                bot, direction="take", item="minecraft:paper", count=8,
                source_slots=range(27), destination_slots=range(9, 11),
            )
            print("From container:", result.confirmed_count, result.stop_reason)
        finally:
            await bot.close_container()

        await open_target()
        try:
            slots = await bot.wait_for_container_slots()
            print("Remaining in slot 0:", slots[0].stack_size)
        finally:
            await bot.close_container()

        if result.stop_reason == "complete":
            await bot.open_inventory_screen()
            try:
                moved = await transfer_inventory_range(
                    bot, item="minecraft:paper", count=8,
                    source_slots=range(9, 11), destination_slots=range(18, 21),
                )
                print("Within inventory:", moved.confirmed_count, moved.stop_reason)
            finally:
                await bot.close_container()
    except (Exception, asyncio.CancelledError) as error:
        progress = getattr(error, "transfer_progress", None)
        if progress is not None:
            print("Transfer progress:", progress.confirmed_count, progress.stop_reason)
            print("Steps:", progress.steps)
        raise
    finally:
        await bot.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--minecart", action="store_true")
    asyncio.run(run(parser.parse_args().minecart))
```

The prepared container opens with 27 slots and reports 10 sheets in slot 0.
Expect `SUCCESS` for the take and return, `8 complete` from both range
transfers, and two sheets in slot 0 when the container is reopened.

## Open a chest minecart

Run the same example with `--minecart`. Prepare the same platform and player,
skipping the two commands that create and fill the chest. Before pressing
Enter, use a second Minecraft client to place a rail at `(9, 220, 8)`, put a
chest minecart on it, and put 10 sheets of paper in the cart's first slot.
Keep only one chest minecart in the bot's view, within interaction range.

```sh
python transfer_paper.py --minecart
```

The script selects the observed cart, passes its current runtime ID to
`open_entity_container()`, and prints the entity unique ID from the matching
server window. The same take, return, range transfer, and reopen checks
apply. Transfers require the cart to remain observed as the same entity.

For a hopper minecart, select an observed `minecraft:hopper_minecart` and
pass its runtime ID to `open_entity_container()`. The server opens a
`HOPPER` window with the cart's unique ID. Wait for its five slots with
`wait_for_container_slots()` before taking or putting by slot. A window
opened by `interact_entity()` supports the same transfers when it is bound
to the observed cart. Use `range(5)` for the cart side of `transfer_range()`
or call `transfer_item()` to use the whole window.

## Transfer through dispensers, droppers, and powered hoppers

Open the block with `open_container()` and wait for
`wait_for_container_slots()`. The bot needs an observation of the primary
block at the window position. Use `range(9)` for the dispenser or dropper
side of `transfer_range()`, and `range(5)` for the hopper side. Narrow
the range to select particular slots, or call `transfer_item()` to wait for
and use the full window.

Power a block hopper before transferring and keep it powered during the
operation. You can check `bot.observed_block_state(position)` for
`minecraft:hopper` with `properties["toggle_bit"] == 1`. A known unpowered
hopper raises `RuntimeError` and an unknown state returns `unobserved`.
If the observed block, power, or window changes before a step is sent,
the result is `window_changed` and keeps any earlier confirmed steps.

## Use a workstation

Use `transfer_range()` and `transfer_item()` for the storage layouts in this
tutorial. Opening a workstation and reading its slots does not enable range
transfers. Use the operations for that window instead.

| Server window | Public slots and operations |
| --- | --- |
| Furnace, smoker, or blast furnace | Use exact-slot `put_into_container()` and `take_from_container()`: `0` is ingredient, `1` fuel, and `2` the result. Take the result rather than placing into that slot. |
| Brewing stand | Put the ingredient in `0`, bottles in `1` through `3`, and fuel in `4` with `put_into_container()`. Use `take_from_container()` to retrieve bottles from `1` through `3`. |
| Anvil | Use `put_into_container()` for input `1` and material `2`. These are EndPilot's logical slots; the window's content packet lists the inputs at `0` and `1`. Call `apply_anvil()` with a confirmed empty main inventory slot as the destination. |

Check the matching server verdict for a slot move. For ongoing processing,
read `bot.furnace_progress` or `bot.brewing_progress` separately. See the item
reference for [crafting grids](../reference/items.md#craft-from-the-observed-recipe-book),
[enchanting](../reference/items.md#observe-enchanting-choices), and
[anvil previews](../reference/items.md#preview-anvil-costs). For villager
exchanges, use `open_trade()` and the current server quote with
`execute_trade()` or `trade_many()`. For supported native economy quotes,
`trade_many()` uses the payable count advertised by the server, including
a discounted first ingredient such as a Hero of the Village quote.
It prefers a compatible output stack, then an observed empty slot. With a
full bag, it can reuse a source slot that the current payment fully empties.
The matching staging receipts and the observed empty slot are required before
it sends the exchange.

For a visible `minecraft:villager_v2`, match `TradeOffers.trader_unique_id`
to an `entity_id` in `bot.observed_entities` to read the villager's latest
`trade_tier` and `trade_experience` metadata. Each field is `None` until
observed for that actor instance. `TradeOffers.trader_tier` describes the
window's last server quote. The actor can level up while that quote still
reports the earlier tier. Use the server verdict to confirm an exchange.

## Choose ranges and interpret partial results

- With `direction="take"`, the source range is in the open container and
  the destination range is in the player's main inventory. With
  `direction="put"`, supply those ranges in the opposite order.
- Use forward, half-open ranges within the observed window's slots.
  Ordinary single chests, barrels, and chest minecarts use `range(27)`.
  Hopper minecarts use `range(5)`.
  A double chest uses its server-reported 54-slot order. Main inventory
  slots are in `range(36)`. For `transfer_inventory_range()`, open the
  player inventory screen and use two non-overlapping main inventory ranges.
- Use `transfer_container_range()` to move items within the open window
  of a 27-slot ordinary chest, trapped chest, barrel, vanilla shulker box,
  personal ender chest, or a 54-slot ordinary double chest with two
  non-overlapping ranges.
  For example, `await transfer_container_range(bot, item="minecraft:paper",
  count=6, source_slots=range(20, 22), destination_slots=range(2, 4))`
  fills compatible stacks first, then uses observed empty container slots.
  For an ordinary double chest, observe both halves and their pairing before
  opening the window. Use its 54-slot order even when opening from the other
  half. Reopen the window after an observed split or replacement.
  For an ender chest, observe the block before opening and wait for the
  complete 27-slot contents. Within one connection, ender chests at
  different positions or in different dimensions open the same personal
  slots. Reopen the window after an observed block replacement.
- Use the exact registered item name. The range helpers accept an optional
  synchronous `match` that sees immutable `HeldItemStack` observations
  with fields such as `aux_value` and `user_data_buffer`. Keep the
  predicate free of side effects because it can run again before each step.
- `confirmed_count` sums quantities confirmed at both ends of each request.
  Check `stop_reason` and `steps` even when some items moved. For example,
  `destination_full` can follow successful steps when the remaining items
  do not fit. Predicate errors, wait failures, and cancellation can carry
  `transfer_progress` on the original exception. This snapshot does not change
  if a late reply updates the slots.
- After a timeout or cancellation, a step without a `request_id` never went
  out. An ID with no `verdict` means the request may still be sent or receive a
  reply, not that the server received it. `verdict="SUCCESS"` with
  `confirmed=None` means the server accepted the request but the available
  slot evidence cannot establish how many items moved.
- Before retrying an unsettled step, check `bot.session_end`, the active
  `bot.open_window`, and the latest `bot.inventory_slots` and
  `bot.container_slots`; do not blindly replay the original count.
  If necessary, watch for fresh observations with
  `subscribe_inventory_changes()` or `subscribe_container_slot_changes()`.
  For packet-level diagnostics, subscribe before the transfer with
  `bot.packets.subscribe_raw()` and match the decoded
  `ItemStackResponsePacket` by the step's request ID in that session.

To walk to a block and choose the interaction face before opening the
container, see [Plan and execute navigation](navigation.md).

## Replenish a missing harvest tool

Use the same platform, single chest, and player as in the first setup.
Save this example as `replenish_pickaxe.py` and run it with
`python replenish_pickaxe.py`. At the prompt, prepare the same chest and
clear the bot's inventory as before. Put an iron pickaxe in chest slot 4 and
place a diamond ore block nearby, then press Enter.

```text
replaceitem block 9 220 8 slot.container 4 minecraft:iron_pickaxe 1
setblock 12 220 8 minecraft:diamond_ore
```

```python
import asyncio

from endpilot import Bot
from endpilot.harvest import equip_harvest_tool_from_chest


async def main() -> None:
    chest = (9, 220, 8)
    ore = (12, 220, 8)
    bot = Bot("Knight")
    try:
        await bot.join_nethernet("http://127.0.0.1:19132")
        await asyncio.to_thread(input, "Prepare the tool chest and ore, then press Enter. ")
        await bot.wait_for_position_sync()
        await bot.request_sub_chunk(ore)
        await bot.wait_for_column_coverage((0, 0), range(13, 14))

        result = await equip_harvest_tool_from_chest(
            bot, ore, chest=chest, face=4,
            chest_slots=range(4, 5), inventory_slots=range(14, 16),
        )
        print("Tool:", result.status, result.tool)
        if result.transfer is not None:
            print("From tool chest:", result.transfer.confirmed_count, result.transfer.stop_reason)
        if result.status == "equipped":
            collected = await bot.collect_block(ore)
            print("Collect:", collected.status, collected.inventory_delta)
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
```

With an empty inventory and the prepared chest, expect one confirmed pickaxe
transfer and `equipped`. The separate collection step reports `collected`
and an inventory increase of one when the server confirms the bot picked up
the diamond. A suitable tool already in the main inventory is equipped
without opening the chest.

Supply a currently reachable chest, its face, and the allowed source and
destination slots. The destination range constrains the take. Equipping
then uses normal hotbar selection. The helper closes a window it opens.
If you pass a chest you already opened, close that window yourself before
starting another activity. Check `status` and `transfer` together because a
confirmed take can precede an equipment failure. A timeout, cancellation, or
connection failure can carry the known outcome on the exception as
`harvest_progress`.

To check reachable supply chests in order, pass them to
`equip_harvest_tool_from_chests` with their own authorized ranges.

```python
from endpilot.harvest import HarvestToolChest, equip_harvest_tool_from_chests

result = await equip_harvest_tool_from_chests(
    bot,
    ore,
    chests=[
        HarvestToolChest(first_chest, range(4, 5), range(3, 4)),
        HarvestToolChest(second_chest, range(6, 7), range(3, 4)),
    ],
)
print(result.status, result.attempts)
```

The caller supplies chests reachable from the current position. After a
confirmed shortage, the next chest is considered once an operation-owned
window has closed with server confirmation. A borrowed chest stays open
and ends this call. Supply stops after an equipped tool or any uncertain
result. Start collection separately after checking `result.status`.
