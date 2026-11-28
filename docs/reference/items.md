# Inventory and items

## Look up vanilla stack capacities

`vanilla_item_stack_limits(protocol=2225)` returns a new read-only snapshot
from exact namespaced item names to their vanilla stack capacities. Keep the
snapshot for repeated lookups. Paper stacks to 64, snowballs to 16, and iron
pickaxes to 1. Lookup is local and sends no packets. Other protocols raise
`ValueError`.

Use the current session's item registry to resolve an observed stack's item
ID before looking up its capacity. For example, for a known occupied slot:

```python
from endpilot import vanilla_item_stack_limits

stack = bot.inventory_slots[slot]
registry = bot.item_registry
name = registry.name_for_id(stack.id) if registry is not None else None
limits = vanilla_item_stack_limits(bot.protocol.number)
limit = limits.get(name) if name is not None else None
room = max(0, limit - stack.stack_size) if limit is not None else None
```

A stack of 15 snowballs has room for one more at the vanilla limit. An
unregistered item ID or an item name outside the catalog yields `None` in
this example. The mapping describes vanilla behavior. A server may use
different limits, so check its transfer verdict when moving items.

## Move items within a storage window

Call `transfer_container_range()` with two non-overlapping ranges in an
active 27-slot ordinary chest, trapped chest, barrel, vanilla shulker box,
or personal ender chest, or in an ordinary double chest with 54 slots.
For a double chest, the SDK must observe both halves and their pairing
before the window opens. For an ender chest, the SDK must observe the
block before opening and receive the complete 27-slot contents.
Use `await bot.wait_for_container_slots()` after opening the window. The
helper fills compatible stacks before using observed empty slots and can
move items across multiple stacks. Check `confirmed_count` and
`stop_reason` before continuing, especially after a partial move.
[Choose ranges and interpret partial results](../tutorials/containers.md#choose-ranges-and-interpret-partial-results)

### Move ranges from MCP scripts

In a script started with `run_script`, use the worker's Bot proxy.

```python
async def main(bot):
    return await bot.transfer_container_range(
        item="minecraft:paper", count=6, source_slots=range(20, 22), destination_slots=range(29, 31),
    )
```

This example uses the server's slot order in a double chest. With the player
inventory screen open, use `await bot.transfer_inventory_range(...)` with
the same keyword arguments and slots within `range(36)`. Both methods accept
`timeout` and use the SDK's default matching with `match=None`. The proxy
rejects Python predicate callbacks before sending a transfer.

The returned snapshot includes `requested`, `steps`, `confirmed_count`, and
`stop_reason`. Each ordered step reports its slots, requested quantity,
confirmed quantity, request ID, and verdict. A `confirmed` value of `None`
means the amount is unknown. A rejected step can confirm zero. When the SDK
supplies progress on a timeout or local cancellation, the script can read
`error.transfer_progress`. If the host's result has already arrived when
the script cancels the call, the cancellation carries that result. An uncaught
error reports the snapshot under
`error.attributes.transfer_progress` in `observe_script`. Use `stop_script`
to terminate the worker and drain its actions on the resident connection.

## Swap occupied slots in an ordinary chest window

On protocol 2225, observe the ordinary single chest before opening it and
wait for its 27-slot contents with `await bot.wait_for_container_slots()`.
Both endpoints need observed, occupied stacks of different items with valid
stack network IDs.

- `await bot.swap_container_items(3, 20)` swaps two chest slots.
- `await bot.swap_container_items(12, 12, other_area="inventory")` swaps chest
  slot 12 with main inventory slot 12.

The call sends one whole-stack exchange and returns the matching BDS verdict.
The SDK publishes exchanged slot descriptions when both corrections corroborate
the original stacks in their new slots. Check `bot.container_slots` and, for
a mixed exchange, `bot.inventory_slots`. If the verdict is rejected or lacks
the required evidence, wait for fresh descriptions of affected slots before
retrying. A timeout or cancellation after submission can still be followed by
a server verdict.

## Return workbench cursor items to the main inventory

With a protocol 2225 crafting table window open, call
`await bot.take_inventory_to_cursor(12)` to pick up the entire observed stack
in main inventory slot 12. The current window must have reported an empty
cursor, and the source needs a known stack network ID.

Call `await bot.return_cursor_to_inventory(range(13, 15))` to place the cursor
stack in main inventory slots 13 and 14. The call fills compatible stacks
first, then uses observed empty slots in the range. It stops when authorized
space runs out. Each call returns a `RangeTransferResult` with its own confirmed
count and step records. Check `stop_reason` and the confirmed count before
continuing. A timeout or cancellation carries `error.transfer_progress`. A step
with a request ID and no verdict may still complete in the same window.

After rejection or an incomplete receipt, wait for a fresh full cursor report
in the current window before retrying. The main inventory slot involved in a
rejected step also needs a fresh description or a matching correction. Closing
or replacing the window, changing dimensions, or ending the connection invalidates
cursor state for writes.
`bot.last_cursor_observation` retains the latest historical report during the
connection and can still show a previously confirmed quantity.

## Move horse equipment

Stand near a tamed horse while holding sneak with an empty hand. Open it
with `await bot.open_entity_container(horse.runtime_id)` and wait for the
two reported equipment slots with `await bot.wait_for_container_slots()`.
Slot `0` holds the saddle and slot `1` holds horse armor. For example, use
`await bot.take_from_container(1, player_slot, 1)` to remove the armor into
a confirmed empty main inventory slot (`0`–`35`), or
`await bot.put_into_container(player_slot, 1, 1)` to equip it in an observed
empty armor slot. Check the returned server verdict before making another move.

The server must identify the source stack by its network ID and register its
item as `minecraft:saddle` for slot `0` or as leather, iron, golden, diamond,
copper, or netherite horse armor for slot `1`. Equipment moves use the
single-item calls. Horse windows that arrive outside a matching
`open_entity_container` call support observation only.

## Read donkey equipment and storage

Stand near a tamed donkey on foot with an empty main hand and submit
`await bot.set_sneaking(True)`. Call `await bot.open_entity_container(donkey.runtime_id)`
and wait for `await bot.wait_for_container_slots()`. The server reports both
forms as a `HORSE` window. The full contents have only the saddle slot `0`
on a donkey without a chest, or the saddle slot and storage slots `1`–`15`
on a donkey with a chest. Wait for the current window's full snapshot each
time it opens.

For a chested donkey, pass the entire observed stack count to
`await bot.take_from_container(cargo_slot, player_slot, count)` or
`await bot.put_into_container(player_slot, cargo_slot, count)`. Cargo slots
are `1`–`15`, and the other endpoint is a main inventory slot (`0`–`35`).
The destination must be confirmed empty. Slot `0` is available for observation.
Both calls return the matching server verdict.

Use `await bot.close_container()` to wait for the server to confirm closure.

## Unequip armor

Use the numeric armor slot (`0`–`3`) or its name (`head`, `chest`, `legs`,
`feet`, in that order). With the destination omitted, the bot moves the
observed armor into the first confirmed empty main inventory slot (`0`–`35`).

```python
verdict = await bot.unequip_armor("head")
print(verdict.result.name)
```

The call returns the matching server item-stack verdict. Read
`bot.armor_slots` and `bot.inventory_slots` for the resulting observations.
Automatic selection raises `RuntimeError` with `the main inventory is full`
when all 36 slots are confirmed occupied, or `no empty main inventory slot is
confirmed` when the available observations cannot establish an empty slot.
In both cases the armor stays equipped and the bot sends no request. For a
specific target, use `await bot.unequip_armor("head", destination_slot=12)`.
If slot 12 is occupied, the request asks the server to swap the two stacks.
While an automatic unequip awaits its verdict, an explicit armor action
that uses either of its slots raises `RuntimeError` before sending.

## Craft from the observed recipe book

Use `await bot.wait_for_recipes()` to read the recipes advertised by the
server. With the player inventory screen open, call
`await bot.craft_recipe_auto(recipe.net_id, destination_slot, times=1)`
for a recipe that fits that screen. The destination is a main inventory
slot with room for the output. The call returns the server's item-stack
verdict. Check the verdict and the observed destination stack.

When several recipes produce the same item, use `bot.recipes_for` to find
ones that match your inventory. For example, `bot.recipes_for("bed", 7)`
finds recipes for a gray bed using the input colors you actually carry.
The query uses confirmed main inventory stacks. After a drop-key prediction,
the affected slot is skipped until the server describes it again. Other
confirmed stacks remain available.

For supported vanilla protocol 2225 recipe tags, automatic crafting
can assign different matching materials to the same tag. For example,
planks of multiple wood types can contribute to a crafting table.
An unknown tag or an inventory that cannot supply the requested ingredients
raises before sending an item request.
[Recipe-book example](../../examples/craft_recipe.py)

Pass `grid_size=2` to query personal-grid recipes even before opening
the inventory screen. With one honey bottle, the announced
`minecraft:honey_bottle_to_sugar` recipe matches
`bot.recipes_for("glass_bottle", min_result_count=1, grid_size=2)` since
it produces three sugar and one glass bottle. The same recipe needs two
honey bottles to satisfy a request for two glass bottles.

Use `bot.plan_recipe_delta(recipe_id, times=1)` to estimate the change
to your main inventory from the current announcement and observed
carried stacks. Pass the recipe's string ID or network ID. The call is
synchronous, sends no packets, and works with the inventory screen closed.

```python
plan = bot.plan_recipe_delta("minecraft:honey_bottle_to_sugar")
if plan is not None:
    for item in plan.delta:
        print(item.name or item.item_id, item.aux_value, item.count)
```

The immutable plan includes the recipe, requested craft count, and
`materials` grouped in `plan.recipe.ingredients` order. A group can
contain different compatible variants across multiple crafts. For each
craft, an ingredient position uses its full quantity from one variant.
`bot.recipes_for` uses the same confirmed materials and assignment rules. The
`delta` rows combine consumption and all announced outputs by numeric
item ID and aux value. Negative counts are consumed, positive counts are
produced, and zero rows show that consumption and production cancel.
An output name can be absent before the item registry identifies it.

`times` must be a positive integer. The call returns `None` if the
current catalog and observed inventory cannot establish a concrete plan.
An announced, locked recipe can still have a plan. Use the discovery and
crafting calls separately to check access, grid size, and the server's
verdict. The plan describes one compatible material assignment. Manual
staging can use another assignment, and available output space depends on
the actual crafting action.

To craft multiple results from a manually staged personal or workbench grid,
call `bot.craft_recipe_grid` once with one distinct confirmed empty main
inventory slot per announced result, in recipe order. After staging the
honey bottle in personal grid slot 28, the following call sends the
sugar to slot 9 and the glass bottle to slot 10 if the server accepts
the request and both slots are confirmed empty.

```python
verdict = await bot.craft_recipe_grid(
    "minecraft:honey_bottle_to_sugar", 9, additional_destination_slots=(10,),
)
```

To undo part of a manually staged grid without closing the screen, use
`await bot.return_crafting_ingredient(32, 9, amount=1)`. This returns one
item from workbench grid slot 32 to main inventory slot 9 if BDS accepts the
request. Use grid slots 28–31 in the personal inventory or 32–40 in a
workbench, and check the returned server verdict before continuing to craft.

### Query and craft recipes from MCP scripts

In a script started with `run_script`, call `bot.recipes_all(output, grid_size=2)`
for catalog matches and `bot.recipes_for(output, grid_size=2)` for recipes
with observed materials and crafting access. Both queries return lists of
recipe snapshots. Call `bot.plan_recipe_delta(recipe.net_id, times=1)` to
inspect the grouped `materials` and signed `delta`, or receive `None` when
the catalog and inventory cannot establish a plan. These queries send no
packets. A recipe's ingredients, results and shape are available by
attribute. The result's `user_data_buffer` is a hex string.

With the personal inventory screen open, call
`await bot.craft_recipe_auto(recipe.net_id, destination_slot, times=1)`.
The response carries `reply.result.name` and
`reply.client_request_id.raw_id`. Inspect the verdict and read
`bot.inventory_slots` for the observed result.

## Observe enchanting choices

`bot.enchantment_seed_at_join` is the signed 32-bit seed reported when the bot joined
the active session, including zero and negative values. It returns `None`
outside an active session. The snapshot stays fixed if the server changes its
seed after enchanting.

Open a reachable enchanting table. Stage an enchantable item from the main
inventory, then wait for the server to confirm the move before requesting
choices. Slot `14` is the input and slot `15` is lapis.

```python
staged = await bot.stage_enchanting_item(source_slot, table_slot=14)
if staged.result.name == "SUCCESS":
    choices = await bot.wait_for_enchant_choices(timeout=15)
    for button, choice in enumerate(choices):
        if choice.cost > 0 and choice.net_id > 0:
            print(button, choice.cost, choice.name, choice.enchants)
```

`wait_for_enchant_choices()` returns usable choices already observed for
the current input or waits for the first usable list. The tuple preserves
server button order, including unavailable entries. At least one option has
a positive cost and positive network ID. Complete each input change through
its server verdict before starting another. A window change invalidates an
in-progress wait.

`bot.enchant_options` exposes the latest observed list, or `None` before a
list arrives. `wait_for_enchant_options()` can return an explicitly empty
list as `()`. Use the zero-based button index from the current choices
with `await bot.enchant(button)`. In server-confirmed Creative mode, stage
the item alone. The request leaves any staged lapis in place. Stage the
required lapis in other modes and when the mode is still unknown.
The SDK rechecks the observed mode, window, choices, and required inputs
before sending. Use the verdict and subsequent item observations to
confirm the result. The verdict can omit item metadata.

## Preview anvil costs

Open an anvil in Survival on protocol 2225. Stage the item in slot `1` and
any material in slot `2` with `put_into_container`, then wait for the server's
verdicts. `bot.preview_anvil(new_name=None)` uses observed item metadata in
slot order for supported vanilla equipment, repairs and enchanted books.
An `AnvilPreview` gives the level cost as `level_cost` and the number of items
consumed from slot `2` as `material_count`.

The preview sends no packets and requires neither a known experience level
nor a destination slot. It returns `None` when an input slot or required item
metadata is unknown, the anvil operation falls outside the supported rules,
or the current window, protocol, or game mode is unsupported. If a successful
request leaves a slot unconfirmed, subsequent previews return `None` until
a new slot observation or a valid server correction confirms the slot.

Use the same `new_name` for the preview and `apply_anvil`, either `None` or a
nonempty string. For an unnamed diamond sword, supplying only `Diamond Sword`
returns `None`. The comparison uses the vanilla default label across login
locales. During a repair, omitting `new_name` can restore the default name and
add a level to the cost. Pass the exact existing custom name to preserve it.

Pass a positive `material_count` from the preview to `apply_anvil`. Omit
that argument for a rename with an empty material slot. The commit requires a
confirmed empty main inventory slot.

```python
preview = bot.preview_anvil()
if preview is not None and preview.material_count > 0:
    print(preview.level_cost)
    verdict = await bot.apply_anvil(destination_slot, material_count=preview.material_count)
    print(verdict.result.name)
```

BDS determines whether the operation succeeds, what it costs, and what items
remain. Use its verdict and subsequent inventory observations to confirm the
result.

## Read, edit and sign books

`bot.get_book(slot)` reads the latest observed writable or signed book in a main
inventory slot (`0`–`35`). A book snapshot includes its pages, title, author,
generation when present, and the original user data bytes. An unobserved slot
or a slot with a different item returns `None`.

`await bot.edit_book(slot, "add_page", 0, "First page")` submits a page operation.
The other operations are `replace_page`, `delete_page`, and `swap_pages`.
Page indexes start at zero. Pass `other_index` to `swap_pages`.
`await bot.sign_book(slot, "Tales", author="Pen Name")` submits a signing
request. The player name supplies the default book author. The author is book
metadata, and BDS owns the resulting title, author, and pages. Editing and
signing require an observed writable book in the selected slot.

Start `wait_for_book_update(slot)` before submitting when the caller needs
to observe an effect. It returns a changed book snapshot from that inventory
slot, or `None` when the book was removed. Updates to other slots and repeated
copies of the same book leave the wait pending. Compare the observed contents
to the expected result. A quiet or unchanged slot can cause `TimeoutError`.

## Use the creative inventory

`bot.game_mode` is the server-reported mode of this player. `bot.creative_catalog`
is the latest immutable catalog, or `None` before the server sends one. Each
entry has a creative `net_id`, a group index, and an item instance with its
numeric ID, registry name, count, aux value, block runtime ID, and original
user-data bytes. `entry.item.nbt` returns a fresh decoded dictionary.

After the server grants creative mode, open the player inventory screen. Main
inventory operations use slots 0–35, including hotbar slots 0–8.
Choose entries from the current catalog for each operation.

```python
catalog = bot.creative_catalog
if bot.game_mode is not None and bot.game_mode.name == "CREATIVE" and catalog:
    diamond = next(entry for entry in catalog.entries if entry.item.name == "minecraft:diamond")
    await bot.open_inventory_screen()
    try:
        verdict = await bot.take_creative_item(diamond, 0, amount=3)
        print(verdict.result.name, bot.inventory_slots.get(0))
    finally:
        await bot.close_container()
```

`take_creative_item()` and `destroy_creative_item()` return the server's
item-stack verdict. `clear_creative_slot()` destroys the observed stack and
returns `None` for a confirmed empty slot. A slot missing from `bot.inventory_slots`
is unknown. Single-slot creative operations require an observed slot.
`fill_creative_slot()` adjusts the selected slot to the requested count and
returns the ordered verdicts. A replacement can clear the existing stack
before requesting a different one. Check all returned verdicts and the final
observed slot. The server determines item stack limits and acceptance.

`await bot.equip_creative_armor(helmet, 0)` takes one iron armor item from
the current catalog directly into its observed empty armor slot, including
when all 36 main inventory slots are full. Use 0 for head, 1 for chest, 2 for
legs, or 3 for feet. The method checks the current inventory window, creative
mode, catalog entry and target slot again before sending. It returns the
matching server verdict. Check `bot.armor_slots` for the resulting slot
observation. When a creative equip overlaps an armor transfer to or from the
same slot, the second write is rejected while the first is in flight. If the
catalog description changes during a request, a successful receipt can leave
the slot unknown until the server supplies a full slot description.

`await bot.clear_creative_inventory([0, 8, 17])` clears selected slots in
order. Omit the list to check all 36 main inventory slots. Each result includes
its slot, status and any server verdict. The status records an observed clear,
an already empty slot, an unknown slot, a rejection, or an unconfirmed result.
`result.complete` requires each selected slot to be observed empty at return.

Pass `include_armor=True` to clear head, chest, legs and feet after the selected
main inventory slots. Pass `[]` to select armor only. Each equipped piece is
moved to a confirmed empty main inventory slot, which may be outside the
selection. The server's receipt must identify that moved stack before the SDK
destroys it. The operation leaves the offhand untouched.

With this option, `result.armor` reports the four armor outcomes in order.
`result.slots` still reports the selected main inventory slots. Each armor
outcome has a `staging_slot` and `unequip` and `destroy` request progress when
those steps were prepared. `staged` is `"confirmed"` when the moved stack is
still observed there, `"unknown"` when its presence cannot be confirmed, and
`None` when there is no outstanding staged stack to report. An unobserved armor
slot is `"unknown"`. A rejection or an unconfirmed change stops further armor
requests and leaves the remaining outcomes `"pending"`. If no empty staging
slot is confirmed for an observed piece, the operation raises `RuntimeError`.
`result.complete` also requires all four armor slots to be observed empty.

Pass `include_crafting_grid=True` to clear the personal 2×2 crafting grid
after the selected main inventory and armor slots. For example,
`await bot.clear_creative_inventory([], include_crafting_grid=True)` selects
only the grid. Its `CreativeCraftingGridClearResult.crafting_grid` contains
four outcomes in native slot order (28–31). `result.slots` still reports
the main inventory selection, and `result.armor` is empty when armor clearing
was omitted. Each known nonempty grid stack is destroyed in place. Unknown
and already empty grid slots require no request. `result.complete` also
requires each selected grid slot to be observed empty. If a planned grid
destroy reaches the write with a changed slot or a request already in flight
for that slot, it raises `RuntimeError` before sending.

Timeout, disconnect and other interruptions of a started fill or bulk clear
preserve the original exception. Read `error.creative_progress.requests` for
the slot, action, request ID, submission and any confirmed verdict of each
prepared request. `submitted` is `True` after a transport send or server
verdict, `False` before any transport attempt, or `None` when a transport
attempt began without confirmation. Grid destroy requests use the
`"destroy_grid"` action to distinguish them from main inventory slots with
the same number. A step absent from the list was never
prepared. An interrupted bulk clear also includes `creative_progress.clear_result`
with partial slot results and pending slots. Use these results and reconcile
the current inventory before retrying an interrupted operation.

These actions require creative mode and an open player inventory window.
Outside creative mode they raise `PermissionError` before sending an item
request. A retained entry from a replaced catalog or a previous session cannot
be used for a new request.
