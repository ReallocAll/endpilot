"""Verify opening, taking and reopening a prepared chest on a local BDS."""

import argparse
import asyncio
import json

from endpilot import Bot


PLAYER_NAME = "Knight"
TARGET = (1, 125, 0)
SOURCE_SLOT = 0
DESTINATION_SLOT = 3
INITIAL_COUNT = 7
INITIAL_INVENTORY_COUNT = 2
TAKE_COUNT = 3


def check_item(slots, index, item_id, count):
    item = slots.get(index)
    if item is None:
        raise AssertionError(f"slot {index}: no observed item")
    observed = (int(item.id), int(item.stack_size))
    if observed != (item_id, count):
        raise AssertionError(f"slot {index}: expected {(item_id, count)}, got {observed}")


async def run(url: str, item_id: int) -> None:
    bot = Bot(PLAYER_NAME, protocol=2225)
    try:
        await bot.join_nethernet(url)
        print(f"spawned {PLAYER_NAME} runtime_id={bot.player_id}", flush=True)
        await asyncio.to_thread(
            input,
            f"Put {INITIAL_INVENTORY_COUNT} of item {item_id} in hotbar slot {DESTINATION_SLOT + 1}, "
            f"move {PLAYER_NAME} west of {TARGET} within reach, and clear {TARGET}. Press Enter. ",
        )
        position = await bot.wait_for_position_sync()

        pending = asyncio.create_task(bot.wait_for_block_update(TARGET, timeout=60))
        try:
            await asyncio.sleep(0)
            print(f"Place a chest at {TARGET}. Bot position: {position}.", flush=True)
            block = await pending
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        await asyncio.to_thread(
            input, f"Put {INITIAL_COUNT} of item {item_id} in chest slot 0 and press Enter. ",
        )

        window = await bot.open_container(TARGET, face=4)
        if window.position != TARGET or window.container_type != "CONTAINER" or bot.open_window != window:
            raise AssertionError(f"unexpected window: {window}")
        initial = await bot.wait_for_container_slots()
        if len(initial) != 27:
            raise AssertionError(f"expected 27 chest slots, got {len(initial)}")
        check_item(initial, SOURCE_SLOT, item_id, INITIAL_COUNT)
        check_item(bot.inventory_slots, DESTINATION_SLOT, item_id, INITIAL_INVENTORY_COUNT)
        print("chest_opened=" + json.dumps({
            "id": window.container_id, "position": window.position,
            "type": window.container_type, "runtime_id": block.runtime_id,
            "size": len(initial), "slot": SOURCE_SLOT, "item_id": item_id, "count": INITIAL_COUNT,
            "inventory_slot": DESTINATION_SLOT, "inventory_count": INITIAL_INVENTORY_COUNT,
        }), flush=True)

        response = await bot.take_from_container(SOURCE_SLOT, DESTINATION_SLOT, TAKE_COUNT)
        reply_id = int(response.client_request_id.raw_id)
        if response.result.name != "SUCCESS":
            raise RuntimeError(f"The server rejected the take request: {response.result.name}.")
        inventory_count = INITIAL_INVENTORY_COUNT + TAKE_COUNT
        check_item(bot.inventory_slots, DESTINATION_SLOT, item_id, inventory_count)
        print("chest_take=" + json.dumps({
            "request_id": reply_id, "result": response.result.name,
            "inventory_slot": DESTINATION_SLOT, "item_id": item_id, "count": inventory_count,
        }), flush=True)

        closed = await bot.close_container()
        if closed != window or bot.open_window is not None or bot.container_slots is not None:
            raise AssertionError(f"window {window.container_id} failed to clear")
        print(f"chest_closed id={closed.container_id} window=None slots=None", flush=True)

        reopened = await bot.open_container(TARGET, face=4)
        if reopened.position != TARGET or reopened.container_type != window.container_type:
            raise AssertionError(f"unexpected reopened window: {reopened}")
        remaining = await bot.wait_for_container_slots()
        check_item(remaining, SOURCE_SLOT, item_id, INITIAL_COUNT - TAKE_COUNT)
        check_item(bot.inventory_slots, DESTINATION_SLOT, item_id, inventory_count)
        print("chest_reopened=" + json.dumps({
            "id": reopened.container_id, "position": reopened.position,
            "slot": SOURCE_SLOT, "item_id": item_id, "count": INITIAL_COUNT - TAKE_COUNT,
        }), flush=True)
        await bot.close_container()
    finally:
        await bot.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL")
    parser.add_argument("item_id", type=int, help="network ID of the prepared item")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.item_id))
