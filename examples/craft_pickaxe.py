"""Craft a wooden pickaxe using a BDS workbench recipe, optionally equipping or storing it."""

import argparse
import asyncio
import json
import sys

from endpilot import Bot


async def run(url: str, *, store: bool = False, equip: bool = False) -> None:
    bot = Bot("EndPilotTest")
    try:
        if url.startswith("raknet://"):
            host, sep, port = url.removeprefix("raknet://").rpartition(":")
            if not sep:
                host, port = port, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        print("spawned EndPilotTest", flush=True)

        recipes = await bot.wait_for_recipes()
        candidates = [
            recipe for recipe in recipes
            if recipe.kind == "shaped_recipes"
            and len(recipe.ingredients) == 5
            and all(ingredient.count == 1 for ingredient in recipe.ingredients)
            and len(recipe.results) == 1
            and recipe.results[0].name == "minecraft:wooden_pickaxe"
            and recipe.results[0].count == 1
        ]
        if not candidates:
            raise RuntimeError("BDS sent no five-input wooden-pickaxe recipe")
        recipe = candidates[0]
        output = recipe.results[0]
        print("recipe_selected=" + json.dumps({
            "id": recipe.recipe_id, "net_id": recipe.net_id,
            "ingredients": [
                {"name": "minecraft:oak_planks", "count": 3},
                {"name": "minecraft:stick", "count": 2},
            ],
            "output": output.name, "output_count": output.count,
            "output_id": output.item_id,
        }), flush=True)

        await asyncio.to_thread(sys.stdin.readline)  # Testbed grants materials.
        deadline = asyncio.get_running_loop().time() + 15
        while True:
            stacks = [(slot, item) for slot, item in bot.inventory_slots.items()
                      if int(item.id) != 0]
            planks = [(slot, item) for slot, item in stacks if int(item.stack_size) == 3]
            sticks = [(slot, item) for slot, item in stacks if int(item.stack_size) == 2]
            if len(stacks) == 2 and len(planks) == 1 and len(sticks) == 1:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("BDS did not deliver planks x3 and sticks x2")
            await asyncio.sleep(0.1)
        plank_slot, _ = planks[0]
        stick_slot, _ = sticks[0]
        destination_slot = next(slot for slot in (8, 7, 6) if slot not in (plank_slot, stick_slot))

        window = await bot.open_container((1, 100, 0))
        print("workbench_window=" + json.dumps({
            "id": window.container_id, "type": window.container_type,
            "position": window.position,
        }), flush=True)
        if window.container_type != "WORKBENCH":
            raise AssertionError(f"expected WORKBENCH, got {window.container_type}")

        stages = []
        for grid_slot, source_slot in (
            (32, plank_slot), (33, plank_slot), (34, plank_slot),
            (36, stick_slot), (39, stick_slot),
        ):
            before = int(bot.inventory_slots[source_slot].stack_size)
            reply = await bot.stage_crafting_ingredient(source_slot, grid_slot)
            after = int(bot.inventory_slots[source_slot].stack_size)
            stage = {
                "grid_slot": grid_slot, "result": reply.result.name,
                "request_id": int(reply.client_request_id.raw_id),
                "source_after": after,
            }
            stages.append(stage)
            if stage["result"] != "SUCCESS" or after != before - 1:
                raise AssertionError(f"BDS rejected pickaxe ingredient: {stage}")
        print("grid_stages=" + json.dumps(stages), flush=True)

        reply = await bot.craft_recipe_grid(recipe.net_id, destination_slot)
        destination = bot.inventory_slots.get(destination_slot)
        record = {
            "result": reply.result.name,
            "request_id": int(reply.client_request_id.raw_id),
            "source_before": 5,
            "source_after": (
                int(bot.inventory_slots[plank_slot].stack_size)
                + int(bot.inventory_slots[stick_slot].stack_size)
            ),
            "destination_slot": destination_slot,
            "output_id": None if destination is None else int(destination.id),
            "output_count": None if destination is None else int(destination.stack_size),
        }
        print("craft_result=" + json.dumps(record), flush=True)
        if (record["result"] != "SUCCESS" or record["source_after"] != 0
                or record["output_id"] != output.item_id
                or record["output_count"] != output.count):
            raise AssertionError(f"unexpected pickaxe craft response: {record}")

        await bot.close_container()
        if equip:
            # Selecting is only an outgoing request. The Testbed checks BDS's
            # actual mainhand independently before allowing storage to proceed.
            held = bot.inventory_slots[destination_slot]
            if int(held.id) != output.item_id or int(held.stack_size) != output.count:
                raise AssertionError("crafted pickaxe is missing from the destination slot")
            await bot.select_hotbar(destination_slot)
            print("equipped_selection=" + json.dumps({
                "slot": destination_slot, "item_id": int(held.id),
                "count": int(held.stack_size),
            }), flush=True)
        if store:
            # Reuse the normal server-confirmed container transfer; no crafting
            # special case is needed for items once they reach the inventory.
            chest_pos = (1, 100, 0)
            pending = asyncio.create_task(bot.wait_for_block_update(chest_pos))
            await asyncio.sleep(0)
            print("storage_chest_watch_ready", flush=True)
            await pending
            opened = await bot.open_container(chest_pos, face=4)
            original_slots = await bot.wait_for_container_slots()
            if int(original_slots[0].id) != 0:
                raise AssertionError("expected an empty destination chest slot")
            stored = await bot.put_into_container(destination_slot, 0, output.count)
            if stored.result.name != "SUCCESS":
                raise AssertionError(f"BDS rejected crafted-output storage: {stored.result.name}")
            corrected = bot.container_slots[0]
            after_store = bot.inventory_count(output.item_id)
            await bot.close_container()

            # A fresh server InventoryContent after reopening verifies the
            # destination independently of the earlier ItemStackResponse.
            reopened = await bot.open_container(chest_pos, face=4)
            verified = (await bot.wait_for_container_slots())[0]
            await bot.close_container()
            record = {
                "request_id": int(stored.client_request_id.raw_id),
                "result": stored.result.name,
                "first_window": opened.container_id,
                "reopened_window": reopened.container_id,
                "response_chest_id": int(corrected.id),
                "response_chest_count": int(corrected.stack_size),
                "reopened_chest_id": int(verified.id),
                "reopened_chest_count": int(verified.stack_size),
                "inventory_output_count": after_store,
                "open_window": bot.open_window is not None,
            }
            print("storage_result=" + json.dumps(record), flush=True)
            if (record["response_chest_id"] != output.item_id
                    or record["response_chest_count"] != output.count
                    or record["reopened_chest_id"] != output.item_id
                    or record["reopened_chest_count"] != output.count
                    or record["inventory_output_count"] != 0):
                raise AssertionError(f"storage did not reconcile: {record}")
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--store", action="store_true", help="store the crafted pickaxe in a chest")
    parser.add_argument("--equip", action="store_true", help="select the crafted pickaxe as the mainhand item")
    args = parser.parse_args()
    asyncio.run(run(args.url, store=args.store, equip=args.equip))
