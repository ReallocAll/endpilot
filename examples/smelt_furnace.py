"""Smelt raw iron, take its ingot and optionally store it in a chest.

The companion Testbed controls staging through stdin. BDS responses and fresh
window contents distinguish accepted transfers from observed storage.
"""
import argparse
import asyncio
import json
import sys

from endpilot import Bot


def report(marker, **values):
    print(marker + json.dumps(values), flush=True)


async def run(url: str, *, store: bool = False):
    bot = Bot("EndPilotTest")
    target = (1, 100, 0)
    try:
        if url.startswith("raknet://"):
            host, _, port = url.removeprefix("raknet://").rpartition(":")
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        print("spawned EndPilotTest", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        pending = asyncio.create_task(bot.wait_for_block_update(target))
        await asyncio.sleep(0)
        print("furnace_watch_ready", flush=True)
        observed = await pending
        report("furnace_block=", runtime_id=observed.runtime_id)
        await asyncio.to_thread(sys.stdin.readline)

        async with asyncio.timeout(15):
            while any(
                slot not in bot.inventory_slots or int(bot.inventory_slots[slot].id) == 0
                for slot in (0, 1)
            ):
                await asyncio.sleep(0.1)
        initial = bot.inventory_slots
        report("furnace_inputs=", ingredient_id=int(initial[0].id),
               fuel_id=int(initial[1].id),
               ingredient_count=int(initial[0].stack_size),
               fuel_count=int(initial[1].stack_size))
        window = await bot.open_container(target, face=4)
        report("furnace_window=", id=window.container_id,
               type=window.container_type, position=window.position)
        slots = await bot.wait_for_container_slots()
        report("furnace_initial_slots=", count=len(slots),
               nonempty=[i for i, v in slots.items() if int(v.id) != 0])

        for name, inventory_slot, furnace_slot in (
            ("ingredient", 0, 0), ("fuel", 1, 1),
        ):
            response = await bot.put_into_container(inventory_slot, furnace_slot)
            now = bot.container_slots
            report("furnace_stage=", kind=name,
                   request_id=int(response.client_request_id.raw_id),
                   result=response.result.name,
                   furnace_count=int(now[furnace_slot].stack_size),
                   inventory_count=int(bot.inventory_slots[inventory_slot].stack_size))
            if response.result.name != "SUCCESS":
                raise AssertionError("BDS rejected furnace " + name + " stage")

        async with asyncio.timeout(30):
            while True:
                output = (bot.container_slots or {}).get(2)
                if output is not None and int(output.id) != 0 and int(output.stack_size) > 0:
                    break
                await asyncio.sleep(0.2)
        report("furnace_output=", item_id=int(output.id),
               count=int(output.stack_size))
        receipt = await bot.take_from_container(2, 2)
        carried = bot.inventory_slots[2]
        report("furnace_take=", request_id=int(receipt.client_request_id.raw_id),
               result=receipt.result.name,
               item_id=int(carried.id), inventory_count=int(carried.stack_size),
               furnace_count=int(bot.container_slots[2].stack_size))
        closed = await bot.close_container()
        report("furnace_closed=", id=closed.container_id,
               open_window=bot.open_window is not None)
        if store:
            # The Testbed replaces the furnace with a chest only after the
            # client has subscribed for this block update.
            pending = asyncio.create_task(bot.wait_for_block_update(target))
            await asyncio.sleep(0)
            print("furnace_storage_watch_ready", flush=True)
            await pending
            chest = await bot.open_container(target, face=4)
            if chest.container_type != "CONTAINER":
                raise AssertionError(f"unexpected storage window: {chest}")
            chest_slots = await bot.wait_for_container_slots()
            if int(chest_slots[0].id) != 0:
                raise AssertionError("expected an empty chest destination")
            response = await bot.put_into_container(2, 0)
            if response.result.name != "SUCCESS":
                raise AssertionError(f"BDS rejected smelted-output storage: {response.result.name}")
            confirmed = bot.container_slots[0]
            remaining = bot.inventory_count(int(carried.id))
            await bot.close_container()

            # Reopen to observe a fresh server InventoryContent, not just the
            # ItemStackResponse correction from the earlier request.
            reopened = await bot.open_container(target, face=4)
            observed = (await bot.wait_for_container_slots())[0]
            await bot.close_container()
            stored = {
                "request_id": int(response.client_request_id.raw_id),
                "result": response.result.name,
                "first_window": chest.container_id,
                "reopened_window": reopened.container_id,
                "response_chest_id": int(confirmed.id),
                "response_chest_count": int(confirmed.stack_size),
                "reopened_chest_id": int(observed.id),
                "reopened_chest_count": int(observed.stack_size),
                "inventory_count": remaining,
                "open_window": bot.open_window is not None,
            }
            report("furnace_storage=", **stored)
            if (stored["response_chest_id"] != int(carried.id)
                    or stored["response_chest_count"] != 1
                    or stored["reopened_chest_id"] != int(carried.id)
                    or stored["reopened_chest_count"] != 1
                    or stored["inventory_count"] != 0
                    or stored["open_window"]):
                raise AssertionError(f"furnace output was not stored: {stored}")
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--store", action="store_true",
                        help="store the resulting iron ingot in a chest")
    args = parser.parse_args()
    asyncio.run(run(args.url, store=args.store))
