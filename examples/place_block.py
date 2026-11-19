"""Place a supplied block against two server-observed supports, one input per prompt."""

import argparse
import asyncio
import json
import sys

from endpilot import Bot


async def run(url, name):
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

        await asyncio.to_thread(sys.stdin.readline)
        pending = [asyncio.create_task(bot.wait_for_block_update((x, 99, 0))) for x in (2, 3)]
        await asyncio.sleep(0)
        print("support_watchers_ready", flush=True)
        against = await asyncio.gather(*pending)
        print("supports_observed=" + json.dumps([u.runtime_id for u in against]), flush=True)

        for x, update in zip((2, 3), against):
            await asyncio.to_thread(sys.stdin.readline)
            watch = asyncio.create_task(bot.wait_for_block_update((x, 100, 0), timeout=3))
            await asyncio.sleep(0)
            tick = await bot.place_against(update, face=1, slot=0)
            print("placement_submitted=" + json.dumps({"x": x, "tick": tick}), flush=True)
            try:
                observed = await watch
                print("placement_update=" + json.dumps({
                    "x": x, "runtime_id": observed.runtime_id, "layer": observed.layer,
                }), flush=True)
            except TimeoutError:
                print(f"placement_no_update={x}", flush=True)
    finally:
        await bot.close()
    print(f"disconnected {name}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--name", default="EndPilotTest")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.name))
