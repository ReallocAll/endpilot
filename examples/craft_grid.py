"""Craft a server-advertised recipe using the player 2x2 grid, not recipe-book auto-craft."""

import argparse
import asyncio
import json
import sys

from endpilot import Bot


async def run(url: str) -> None:
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
        preferred = ("minecraft:sugar", "minecraft:bone_meal", "minecraft:oak_planks")
        available = [
            recipe for recipe in recipes
            if recipe.kind in ("shapeless_recipes", "user_data_shapeless_recipes")
            and len(recipe.ingredients) == 1 and len(recipe.results) == 1
            and recipe.ingredients[0].name is not None
            and recipe.ingredients[0].count == 1
            and recipe.results[0].name in preferred
            and 1 <= recipe.results[0].count <= 64
        ]
        if not available:
            raise RuntimeError("BDS sent no suitable one-input 2x2 grid recipe")
        recipe = min(available, key=lambda r: preferred.index(r.results[0].name))
        ingredient, output = recipe.ingredients[0], recipe.results[0]
        print("recipe_selected=" + json.dumps({
            "id": recipe.recipe_id, "net_id": recipe.net_id,
            "ingredient": ingredient.name, "ingredient_count": ingredient.count,
            "output": output.name, "output_count": output.count,
            "output_id": output.item_id,
        }), flush=True)

        await asyncio.to_thread(sys.stdin.readline)  # Testbed gives the input.
        deadline = asyncio.get_running_loop().time() + 15
        while True:
            occupied = [(slot, item) for slot, item in bot.inventory_slots.items()
                        if int(item.id) != 0]
            if occupied:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("BDS did not deliver the crafting material")
            await asyncio.sleep(0.1)
        source_slot, source = occupied[0]
        destination_slot = 8 if source_slot != 8 else 7
        before = int(source.stack_size)
        window = await bot.open_inventory_screen()
        print("crafting_window=" + json.dumps({
            "id": window.container_id, "type": window.container_type,
        }), flush=True)
        staged = await bot.stage_crafting_ingredient(source_slot, 28)
        source_after_stage = bot.inventory_slots[source_slot]
        record = {
            "result": staged.result.name,
            "request_id": int(staged.client_request_id.raw_id),
            "source_slot": source_slot, "source_before": before,
            "source_after": int(source_after_stage.stack_size),
            "grid_slot": 28,
        }
        print("grid_stage=" + json.dumps(record), flush=True)
        if record["result"] != "SUCCESS" or record["source_after"] != before - 1:
            raise AssertionError(f"BDS rejected crafting input: {record}")

        reply = await bot.craft_recipe_grid(recipe.net_id, destination_slot)
        final_source = bot.inventory_slots[source_slot]
        destination = bot.inventory_slots.get(destination_slot)
        record = {
            "result": reply.result.name,
            "request_id": int(reply.client_request_id.raw_id),
            "source_slot": source_slot, "source_before": before,
            "source_after": int(final_source.stack_size),
            "destination_slot": destination_slot,
            "output_id": None if destination is None else int(destination.id),
            "output_count": None if destination is None else int(destination.stack_size),
        }
        print("craft_result=" + json.dumps(record), flush=True)
        if record["result"] != "SUCCESS":
            raise AssertionError(f"BDS rejected grid craft: {record}")
        if record["output_id"] != output.item_id or record["output_count"] != output.count:
            raise AssertionError(f"output correction did not match: {record}")
        await bot.close_container()
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    asyncio.run(run(parser.parse_args().url))
