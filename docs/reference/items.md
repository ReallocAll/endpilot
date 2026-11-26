# Inventory and items

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

For supported vanilla protocol 2225 recipe tags, automatic crafting
can assign different matching materials to the same tag. For example,
planks of multiple wood types can contribute to a crafting table.
An unknown tag or an inventory that cannot supply the requested ingredients
raises before sending an item request.
[Recipe-book example](../../examples/craft_recipe.py)

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
list as `()`. To enchant, stage the necessary lapis and use the zero-based
button index from the current choices with `await bot.enchant(button)`. Use
the verdict and subsequent item observations to confirm the result. The
verdict can omit item metadata.

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

After the server grants creative mode, open the player inventory screen. The
operations use Bedrock main inventory slots 0–35, including hotbar slots 0–8.
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

`await bot.clear_creative_inventory([0, 8, 17])` clears selected slots in
order. Omit the list to check all 36 main inventory slots. Each result includes
its slot, status and any server verdict. The status records an observed clear,
an already empty slot, an unknown slot, a rejection, or an unconfirmed result.
`result.complete` requires each selected slot to be observed empty at return.

Timeout, disconnect and other interruptions of a started fill or bulk clear
preserve the original exception. Read `error.creative_progress.requests` for
the slot, action, request ID, submission and any confirmed verdict of each
prepared request. `submitted` is `True` after a transport send or server
verdict, `False` before any transport attempt, or `None` when a transport
attempt began without confirmation. A step absent from the list was never
prepared. An interrupted bulk clear also includes `creative_progress.clear_result`
with partial slot results and pending slots. Use these results and reconcile
the current inventory before retrying an interrupted operation.

These actions require creative mode and an open player inventory window.
Outside creative mode they raise `PermissionError` before sending an item
request. A retained entry from a replaced catalog or a previous session cannot
be used for a new request.
