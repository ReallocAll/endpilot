"""Craft an iron block using nine observed workbench 3x3 input slots."""

import argparse
import asyncio
import json
import sys

from endpilot import Bot


async def run(url: str, *, times: int = 1) -> None:
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
            and len(recipe.ingredients) == 9
            and all(i.name == "minecraft:iron_ingot" and i.count == 1
                    for i in recipe.ingredients)
            and len(recipe.results) == 1
            and recipe.results[0].name == "minecraft:iron_block"
            and recipe.results[0].count == 1
        ]
        if not candidates:
            raise RuntimeError("BDS sent no nine-ingot iron-block workbench recipe")
        recipe = candidates[0]
        output = recipe.results[0]
        print("recipe_selected=" + json.dumps({
            "id": recipe.recipe_id, "net_id": recipe.net_id,
            "ingredient": "minecraft:iron_ingot", "ingredient_count": 9,
            "crafts": times,
            "output": output.name, "output_count": output.count,
            "output_id": output.item_id,
        }), flush=True)

        await asyncio.to_thread(sys.stdin.readline)
        deadline = asyncio.get_running_loop().time() + 15
        while True:
            occupied = [(slot, item) for slot, item in bot.inventory_slots.items()
                        if int(item.id) != 0 and int(item.stack_size) >= 9 * times]
            if occupied:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("BDS did not deliver enough iron ingots for the requested crafts")
            await asyncio.sleep(0.1)
        source_slot, source = occupied[0]
        before = int(source.stack_size)
        destination_slot = 8 if source_slot != 8 else 7

        window = await bot.open_container((1, 100, 0))
        print("workbench_window=" + json.dumps({
            "id": window.container_id, "type": window.container_type,
            "position": window.position,
        }), flush=True)
        if window.container_type != "WORKBENCH":
            raise AssertionError(f"expected WORKBENCH, got {window.container_type}")

        stages = []
        for slot in range(32, 41):
            reply = await bot.stage_crafting_ingredient(source_slot, slot, amount=times)
            remaining = int(bot.inventory_slots[source_slot].stack_size)
            stage = {
                "grid_slot": slot, "result": reply.result.name,
                "request_id": int(reply.client_request_id.raw_id),
                "source_after": remaining,
            }
            stages.append(stage)
            if stage["result"] != "SUCCESS" or remaining != before - (slot - 31) * times:
                raise AssertionError(f"BDS rejected workbench staging: {stage}")
        print("grid_stages=" + json.dumps(stages), flush=True)

        reply = await bot.craft_recipe_grid(recipe.net_id, destination_slot, times=times)
        destination = bot.inventory_slots.get(destination_slot)
        record = {
            "result": reply.result.name,
            "request_id": int(reply.client_request_id.raw_id),
            "source_slot": source_slot, "source_before": before,
            "source_after": int(bot.inventory_slots[source_slot].stack_size),
            "destination_slot": destination_slot,
            "output_id": None if destination is None else int(destination.id),
            "output_count": None if destination is None else int(destination.stack_size),
        }
        print("craft_result=" + json.dumps(record), flush=True)
        if record["result"] != "SUCCESS":
            raise AssertionError(f"BDS rejected workbench craft: {record}")
        if record["output_id"] != output.item_id or record["output_count"] != output.count * times:
            raise AssertionError(f"unexpected workbench output: {record}")

        await bot.close_container()
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--times", type=int, default=1, help="craft this many iron blocks in one request")
    args = parser.parse_args()
    asyncio.run(run(args.url, times=args.times))
