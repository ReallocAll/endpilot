"""Observe a villager quote, stage its ingredient, and inspect the server response."""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict

from endpilot import Bot


async def run(url: str, *, interrupt: bool = False) -> None:
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
        sample = next(
            (recipe for recipe in recipes if recipe.results and recipe.ingredients), None
        )
        if sample is None or bot.get_recipe(sample.recipe_id) != sample or bot.get_recipe(sample.net_id) != sample:
            raise AssertionError("server recipes are missing or cannot be looked up by both IDs")
        print("server_recipe_catalog=" + json.dumps({
            "count": len(recipes), "recipe_id": sample.recipe_id, "net_id": sample.net_id,
            "kind": sample.kind,
            "first_result_id": sample.results[0].item_id,
            "first_result_count": sample.results[0].count,
            "first_ingredient_count": sample.ingredients[0].count,
        }), flush=True)
        await asyncio.to_thread(sys.stdin.readline)  # Wait for Testbed world setup.
        waiting = asyncio.create_task(bot.wait_for_entity_spawn(actor_type="minecraft:villager_v2"))
        await asyncio.sleep(0)
        print("villager_watch_ready", flush=True)
        villager = await waiting
        print("villager_spawn=" + json.dumps(asdict(villager)), flush=True)

        await asyncio.to_thread(sys.stdin.readline)
        await bot.look_at((villager.position[0], villager.position[1] + 1, villager.position[2]))
        await asyncio.sleep(0.3)
        offers = await bot.open_trade(villager.runtime_id)
        print("villager_quotes=" + json.dumps(asdict(offers)), flush=True)
        window = bot.open_window
        if window is None or window.container_id != offers.window_id:
            raise AssertionError("trade response did not open the matching window")
        await asyncio.to_thread(sys.stdin.readline)  # Testbed gives the quoted ingredient.
        # On a fresh test player, the given ingredient is the first occupied slot.
        selected = next(
            offer for offer in offers.offers
            if offer.buy_a is not None and offer.buy_b is None
            and offer.uses is not None and offer.max_uses is not None
            and offer.uses < offer.max_uses
        )
        deadline = asyncio.get_running_loop().time() + 15
        while True:
            filled = [
                (slot, item) for slot, item in bot.inventory_slots.items()
                if int(item.id) != 0
            ]
            if filled:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("BDS did not send given trade ingredient")
            await asyncio.sleep(0.1)
        source_slot, source = filled[0]
        before = int(source.stack_size)
        reply = await bot.stage_trade_ingredient(source_slot, selected.buy_a.count)
        corrected = bot.inventory_slots[source_slot]
        record = {
            "result": reply.result.name,
            "request_id": int(reply.client_request_id.raw_id),
            "source_slot": source_slot,
            "before": before,
            "after": int(corrected.stack_size),
            "amount": selected.buy_a.count,
            "input_containers": [
                {
                    "name": c.full_container_name.name.name,
                    "slots": [{"slot": int(p.slot), "amount": int(p.amount)} for p in c.slots],
                } for c in reply.containers or ()
            ],
        }
        print("trade_ingredient_result=" + json.dumps(record), flush=True)
        if reply.result.name != "SUCCESS":
            raise AssertionError(f"BDS rejected trade ingredient: {reply.result.name}")
        if record["after"] != before - selected.buy_a.count:
            raise AssertionError(f"BDS inventory count mismatch: {record}")
        if interrupt:
            # A villager's death does not necessarily close the trade UI on BDS.
            # Explicitly cancel this staged exchange and await the server close reply.
            print("trade_abort_ready", flush=True)
            await asyncio.to_thread(sys.stdin.readline)
            closed = await bot.close_container()
            if (closed is None or closed.container_id != offers.window_id
                    or bot.open_window is not None):
                raise AssertionError("BDS did not acknowledge the trade cancellation")

            # The closed window must reject stale offers without another request.
            try:
                await bot.execute_trade(selected.recipe_net_id, 9)
            except RuntimeError as error:
                if "no matching trade window" not in str(error):
                    raise
            else:
                raise AssertionError("the closed trade accepted another exchange")
            # Closing may not publish an inventory correction. Report the
            # observed state instead of assuming a refund from the close ack.
            await asyncio.sleep(0.5)
            observed_count = bot.inventory_count(int(source.id))
            print("trade_aborted=" + json.dumps({
                "window_id": closed.container_id,
                "close_acknowledged": True,
                "open_window": bot.open_window is not None,
                "item_id": int(source.id),
                "observed_count": observed_count,
                "original_count": before,
                "stale_exchange_blocked": True,
            }), flush=True)
        else:
            output_slot = 9
            existing = bot.inventory_slots.get(output_slot)
            if existing is not None and int(existing.id) != 0:
                raise AssertionError("trade output slot is not empty")
            exchanged = await bot.execute_trade(selected.recipe_net_id, output_slot)
            output = bot.inventory_slots.get(output_slot)
            exchange_record = {
                "result": exchanged.result.name,
                "request_id": int(exchanged.client_request_id.raw_id),
                "output_slot": output_slot,
                "output_id": None if output is None else int(output.id),
                "output_count": None if output is None else int(output.stack_size),
                "containers": [
                    {"name": c.full_container_name.name.name,
                     "slots": [{"slot": int(p.slot), "amount": int(p.amount)} for p in c.slots]}
                    for c in exchanged.containers or ()
                ],
            }
            print("trade_exchange_result=" + json.dumps(exchange_record), flush=True)
            if (exchange_record["result"] != "SUCCESS" or output is None
                    or int(output.id) == 0 or int(output.stack_size) != selected.sell.count):
                raise AssertionError(f"trade output not accepted: {exchange_record}")
            closed = await bot.close_container()
            if closed is None or closed.container_id != offers.window_id:
                raise AssertionError("BDS did not acknowledge the trade window close")
            print("trade_window_closed=" + str(closed.container_id), flush=True)

        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--interrupt", action="store_true", help="cancel a staged trade and inspect returned inventory")
    args = parser.parse_args()
    asyncio.run(run(args.url, interrupt=args.interrupt))
