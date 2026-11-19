"""Observe a live server block change near the player (not an initial chunk snapshot)."""

import argparse
import asyncio
import json
import sys

from endpilot import Bot


async def main(url: str, name: str, position: tuple[int, int, int]):
    bot = Bot(name)
    try:
        if url.startswith("raknet://"):
            host, sep, port = url.removeprefix("raknet://").rpartition(":")
            if not sep:
                host, port = port, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        print(f"spawned {name} runtime_id={bot.player_id}", flush=True)

        # Testbed prepares the world before it tells the client to watch.
        await asyncio.to_thread(sys.stdin.readline)
        pending = asyncio.create_task(bot.wait_for_block_update(position, timeout=20))
        await asyncio.sleep(0)
        print("block_watcher_ready", flush=True)
        update = await pending
        print("block_update=" + json.dumps({
            "position": update.position,
            "runtime_id": update.runtime_id,
            "layer": update.layer,
        }), flush=True)
    finally:
        await bot.close()
    print(f"disconnected {name}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--name", default="EndPilotTest")
    parser.add_argument("--x", type=int, default=2)
    parser.add_argument("--y", type=int, default=100)
    parser.add_argument("--z", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(main(args.url, args.name, (args.x, args.y, args.z)))
