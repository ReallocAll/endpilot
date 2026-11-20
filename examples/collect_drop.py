"""Mine a distant dirt block, approach its drop and confirm server pickup."""
import argparse
import asyncio
import json
import sys

from endpilot import Bot


async def run(url: str, position: tuple[int, int, int], face: int) -> None:
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
        await asyncio.to_thread(sys.stdin.readline)
        await bot.look_at(tuple(v + 0.5 for v in position))
        pending_drop = asyncio.create_task(bot.wait_for_item_drop(timeout=15))
        await asyncio.sleep(0)
        try:
            update = await bot.mine_block(position, face=face, timeout=15)
            drop = await pending_drop
        finally:
            if not pending_drop.done():
                pending_drop.cancel()
            await asyncio.gather(pending_drop, return_exceptions=True)
        print("mining_update=" + json.dumps({
            "position": update.position, "runtime_id": update.runtime_id,
        }), flush=True)
        print("item_drop=" + json.dumps({
            "runtime_id": drop.runtime_id, "item_id": drop.item_id,
            "network_id": drop.item_network_id, "count": drop.item_count,
            "position": drop.position,
        }), flush=True)
        before = bot.inventory_count(drop.item_network_id)
        picked = await bot.collect_drop(drop.runtime_id, ticks=120, timeout=15)
        after = await bot.wait_for_inventory_item_count(
            drop.item_network_id, before + 1, timeout=5
        )
        print("item_collected=" + json.dumps({
            "runtime_id": picked.runtime_id, "item_id": picked.item_id,
            "inventory_before": before, "inventory_after": after,
        }), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("x", type=int)
    parser.add_argument("y", type=int)
    parser.add_argument("z", type=int)
    parser.add_argument("--face", type=int, default=1)
    args = parser.parse_args()
    asyncio.run(run(args.url, (args.x, args.y, args.z), args.face))
