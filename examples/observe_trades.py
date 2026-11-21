"""Observe a villager quote, stage its ingredient, and inspect the server response."""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict

from endpilot import Bot


async def run_two_input(bot: Bot, offers) -> None:
    """Complete a server-quoted two-input trade through public SDK calls."""
    selected = next((
        offer for offer in offers.offers
        if offer.buy_a is not None and offer.buy_b is not None
        and offer.sell is not None and offer.recipe_net_id is not None
        and offer.uses is not None and offer.max_uses is not None
        and offer.uses < offer.max_uses
    ), None)
    if selected is None:
        raise AssertionError("villager has no available two-input server quote")
    print("two_input_offer=" + json.dumps(asdict(selected)), flush=True)

    previous_request = 0
    for number, item in enumerate((selected.buy_a, selected.buy_b), start=1):
        await asyncio.to_thread(sys.stdin.readline)  # Testbed supplies this input.
        deadline = asyncio.get_running_loop().time() + 15
        while True:
            filled = [
                (slot, stack) for slot, stack in bot.inventory_slots.items()
                if int(stack.id) != 0
            ]
            if filled:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"BDS did not supply trade ingredient {number}")
            await asyncio.sleep(0.1)
        source_slot, source = filled[0]
        before = int(source.stack_size)
        reply = await bot.stage_trade_ingredient(
            source_slot, item.count, ingredient=number
        )
        corrected = bot.inventory_slots.get(source_slot)
        after = 0 if corrected is None else int(corrected.stack_size)
        record = {
            "ingredient": number, "name": item.name, "amount": item.count,
            "result": reply.result.name,
            "request_id": int(reply.client_request_id.raw_id),
            "source_slot": source_slot, "before": before, "after": after,
            "input_containers": [
                {
                    "name": c.full_container_name.name.name,
                    "slots": [{"slot": int(p.slot), "amount": int(p.amount)}
                              for p in c.slots],
                } for c in reply.containers or ()
            ],
        }
        print(f"trade_input_{number}_result=" + json.dumps(record), flush=True)
        if (reply.result.name != "SUCCESS" or after != before - item.count
                or record["request_id"] >= previous_request):
            raise AssertionError(f"BDS rejected trade input {number}: {record}")
        previous_request = record["request_id"]

    output_slot = 9
    exchanged = await bot.execute_trade(selected.recipe_net_id, output_slot)
    output = bot.inventory_slots.get(output_slot)
    record = {
        "result": exchanged.result.name,
        "request_id": int(exchanged.client_request_id.raw_id),
        "output_slot": output_slot,
        "output_id": None if output is None else int(output.id),
        "output_count": None if output is None else int(output.stack_size),
        "containers": [
            {
                "name": c.full_container_name.name.name,
                "slots": [{"slot": int(p.slot), "amount": int(p.amount)}
                          for p in c.slots],
            } for c in exchanged.containers or ()
        ],
    }
    print("trade_exchange_result=" + json.dumps(record), flush=True)
    if (record["result"] != "SUCCESS" or record["request_id"] >= previous_request
            or output is None or int(output.id) == 0
            or int(output.stack_size) != selected.sell.count):
        raise AssertionError(f"BDS rejected two-input exchange: {record}")
    closed = await bot.close_container()
    if closed is None or closed.container_id != offers.window_id:
        raise AssertionError("BDS did not acknowledge the two-input trade close")
    print("trade_window_closed=" + str(closed.container_id), flush=True)


async def run(url: str, *, interrupt: bool = False, two_input: bool = False,
              server_interrupt: bool = False) -> None:
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
        if two_input:
            await run_two_input(bot, offers)
            await asyncio.to_thread(sys.stdin.readline)
            return
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
        if server_interrupt:
            # The server interrupts the session after BDS has accepted staging.
            # A DisconnectPacket is not an exchange verdict or an item refund.
            pending = asyncio.create_task(bot.wait_for_server_disconnect(timeout=15))
            await asyncio.sleep(0)
            print("trade_server_interrupt_ready", flush=True)
            disconnected = await pending
            print("trade_server_interrupted=" + json.dumps({
                "packet": type(disconnected).__name__,
                "window_id": offers.window_id,
                "staging_request_id": int(reply.client_request_id.raw_id),
                "staging_result": reply.result.name,
                "exchange_submitted": False,
            }), flush=True)
        elif interrupt:
            # A villager's death does not necessarily close the trade UI on BDS.
            # Explicitly cancel this staged exchange and await the server close reply.
            print("trade_abort_ready", flush=True)
            await asyncio.to_thread(sys.stdin.readline)
            returned = await bot.unstage_trade_ingredient(source_slot)
            print("trade_unstage_result=" + json.dumps({
                "result": returned.result.name,
                "request_id": int(returned.client_request_id.raw_id),
                "observed_count": bot.inventory_count(int(source.id)),
            }), flush=True)
            if returned.result.name != "SUCCESS":
                raise AssertionError(f"BDS rejected trade input return: {returned.result.name}")
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
            # Reopen from a fresh server quote after Testbed checks the refund.
            await asyncio.to_thread(sys.stdin.readline)
            reopened = await bot.open_trade(villager.runtime_id)
            retry_offer = next((
                offer for offer in reopened.offers
                if offer.buy_a == selected.buy_a and offer.buy_b is None
                and offer.sell == selected.sell and offer.recipe_net_id is not None
                and offer.uses is not None and offer.max_uses is not None
                and offer.uses < offer.max_uses
            ), None)
            if retry_offer is None or retry_offer.uses != selected.uses:
                raise AssertionError("cancelled trade has no fresh, unchanged server quote")
            if bot.open_window is None or bot.open_window.container_id != reopened.window_id:
                raise AssertionError("retry did not open a matching trade window")
            if bot.inventory_count(int(source.id)) < retry_offer.buy_a.count:
                raise AssertionError("returned ingredients are unavailable for retry")

            retry_stage = await bot.stage_trade_ingredient(source_slot, retry_offer.buy_a.count)
            if retry_stage.result.name != "SUCCESS":
                raise AssertionError(f"BDS rejected retry staging: {retry_stage.result.name}")
            retry_reply = await bot.execute_trade(retry_offer.recipe_net_id, 9)
            retry_output = bot.inventory_slots.get(9)
            if (retry_reply.result.name != "SUCCESS" or retry_output is None
                    or int(retry_output.id) == 0
                    or int(retry_output.stack_size) != retry_offer.sell.count):
                raise AssertionError(f"BDS rejected retried trade: {retry_reply.result.name}")
            retry_closed = await bot.close_container()
            if retry_closed is None or retry_closed.container_id != reopened.window_id:
                raise AssertionError("BDS did not acknowledge the retried trade close")
            print("trade_retry_result=" + json.dumps({
                "reopened_window": reopened.window_id,
                "recipe_net_id": retry_offer.recipe_net_id,
                "uses_before": selected.uses,
                "uses_after_reopen": retry_offer.uses,
                "stage_result": retry_stage.result.name,
                "stage_request_id": int(retry_stage.client_request_id.raw_id),
                "result": retry_reply.result.name,
                "request_id": int(retry_reply.client_request_id.raw_id),
                "output_slot": 9,
                "output_id": int(retry_output.id),
                "output_count": int(retry_output.stack_size),
                "ingredient_after": bot.inventory_count(int(source.id)),
                "closed_window": retry_closed.container_id,
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

        if not server_interrupt:
            await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
        print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--interrupt", action="store_true", help="cancel a staged trade and inspect returned inventory")
    parser.add_argument("--two-input", action="store_true", help="exchange a quote requiring both villager inputs")
    parser.add_argument("--server-interrupt", action="store_true",
                        help="observe a BDS kick after confirmed trade ingredient staging")
    args = parser.parse_args()
    asyncio.run(run(args.url, interrupt=args.interrupt, two_input=args.two_input,
                    server_interrupt=args.server_interrupt))
