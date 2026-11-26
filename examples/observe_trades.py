"""Read a villager quote and perform one trade using its server-provided inputs."""

import argparse
import asyncio
import json
from dataclasses import asdict

from endpilot import Bot


async def run(url: str, *, two_input: bool = False, return_input: bool = False) -> None:
    bot = Bot("Knight")
    try:
        if url.startswith("raknet://"):
            host, sep, port = url.removeprefix("raknet://").rpartition(":")
            if not sep:
                host, port = port, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        await asyncio.to_thread(
            input, "Move Knight to level ground with an empty inventory, then press Enter. ",
        )
        print("Spawn a villager with a profession within reach of Knight now.", flush=True)
        villager = await bot.wait_for_entity_spawn(actor_type="minecraft:villager_v2", timeout=60)
        await bot.look_at((villager.position[0], villager.position[1] + 1, villager.position[2]))
        await asyncio.sleep(0.3)
        offers = await bot.open_trade(villager.runtime_id)
        try:
            print("villager_quotes=" + json.dumps(asdict(offers)), flush=True)
            selected = next((
                offer for offer in offers.offers
                if offer.buy_a is not None and (offer.buy_b is not None) == two_input
                and offer.sell is not None and offer.recipe_net_id is not None
                and offer.uses is not None and offer.max_uses is not None
                and offer.uses < offer.max_uses
            ), None)
            if selected is None:
                raise RuntimeError("The villager has no available offer with the requested number of inputs.")
            print("selected_quote=" + json.dumps(asdict(selected)), flush=True)

            for slot, item in enumerate((selected.buy_a, selected.buy_b)):
                if item is None:
                    break
                await asyncio.to_thread(
                    input, f"Put {item.count} {item.name} in Knight's hotbar slot {slot + 1}, then press Enter. ",
                )
                async with asyncio.timeout(5):
                    while True:
                        stack = bot.inventory_slots.get(slot)
                        if stack is not None and int(stack.id) != 0 and int(stack.stack_size) >= item.count:
                            break
                        await asyncio.sleep(0.05)
                response = await bot.stage_trade_ingredient(slot, item.count, ingredient=slot + 1)
                print(f"Input {slot + 1}: {response.result.name}", flush=True)
                if response.result.name != "SUCCESS":
                    raise RuntimeError(f"The server rejected input {slot + 1}.")

            if return_input:
                for slot in range(2 if two_input else 1):
                    response = await bot.unstage_trade_ingredient(slot, ingredient=slot + 1)
                    print(f"Returned input {slot + 1}: {response.result.name}", flush=True)
                    if response.result.name != "SUCCESS":
                        raise RuntimeError(f"The server refused to return input {slot + 1}.")
            else:
                output_slot = 9
                response = await bot.execute_trade(selected.recipe_net_id, output_slot)
                print(f"Trade: {response.result.name}", flush=True)
                if response.result.name != "SUCCESS":
                    raise RuntimeError("The server rejected the trade.")
                output = bot.inventory_slots[output_slot]
                print(f"Inventory slot {output_slot}: item {output.id}, count {output.stack_size}.", flush=True)
        finally:
            await bot.close_container()
    finally:
        await bot.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--two-input", action="store_true", help="choose an offer requiring both input slots")
    parser.add_argument("--return-input", action="store_true", help="return the staged inputs without exchanging")
    args = parser.parse_args()
    asyncio.run(run(args.url, two_input=args.two_input, return_input=args.return_input))
