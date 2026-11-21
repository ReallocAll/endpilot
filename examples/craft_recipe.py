"""Craft a server-advertised inventory recipe, optionally storing its output in a chest."""

import argparse
import asyncio
import json
import sys

from endpilot import Bot


async def run(url: str, *, store: bool = False) -> None:
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
        # Single-item shapeless recipes need no crafting table or grid transfer.
        preferred = ("minecraft:sugar", "minecraft:bone_meal", "minecraft:oak_planks")
        available = [
            recipe for recipe in recipes
            if recipe.kind in ("shapeless_recipes", "user_data_shapeless_recipes")
            and len(recipe.ingredients) == 1 and len(recipe.results) == 1
            and recipe.ingredients[0].name is not None
            and recipe.ingredients[0].tag is None
            and recipe.results[0].name in preferred
            and 1 <= recipe.ingredients[0].count <= 64
            and 1 <= recipe.results[0].count <= 64
        ]
        if not available:
            raise RuntimeError("BDS sent no supported one-input inventory recipe")
        recipe = min(available, key=lambda r: preferred.index(r.results[0].name))
        ingredient, output = recipe.ingredients[0], recipe.results[0]
        print("recipe_selected=" + json.dumps({
            "id": recipe.recipe_id, "net_id": recipe.net_id,
            "ingredient": ingredient.name, "ingredient_count": ingredient.count,
            "output": output.name, "output_count": output.count,
            "output_id": output.item_id,
        }), flush=True)

        await asyncio.to_thread(sys.stdin.readline)  # Testbed gives the material.
        deadline = asyncio.get_running_loop().time() + 15
        while True:
            filled = [(slot, item) for slot, item in bot.inventory_slots.items()
                      if int(item.id) != 0]
            if filled:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("BDS did not deliver the crafting material")
            await asyncio.sleep(0.1)
        source_slot, source = filled[0]
        destination_slot = 8 if source_slot != 8 else 7
        before = int(source.stack_size)
        window = await bot.open_inventory_screen()
        print("crafting_window=" + json.dumps({
            "id": window.container_id, "type": window.container_type,
        }), flush=True)
        response = await bot.craft_recipe_auto(recipe.net_id, destination_slot)
        after = bot.inventory_slots.get(source_slot)
        destination = bot.inventory_slots.get(destination_slot)
        record = {
            "result": response.result.name,
            "request_id": int(response.client_request_id.raw_id),
            "source_slot": source_slot, "source_before": before,
            "source_after": None if after is None else int(after.stack_size),
            "destination_slot": destination_slot,
            "output_id": None if destination is None else int(destination.id),
            "output_count": None if destination is None else int(destination.stack_size),
        }
        print("craft_result=" + json.dumps(record), flush=True)
        if response.result.name != "SUCCESS":
            raise AssertionError(f"BDS rejected auto-craft: {response.result.name}")
        if record["source_after"] != before - ingredient.count:
            raise AssertionError(f"source correction did not match: {record}")
        if record["output_id"] != output.item_id or record["output_count"] != output.count:
            raise AssertionError(f"output correction did not match: {record}")
        await bot.close_container()
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
    parser.add_argument("--store", action="store_true", help="store the crafted output in a chest")
    args = parser.parse_args()
    asyncio.run(run(args.url, store=args.store))
