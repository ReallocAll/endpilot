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

        # Capture each initially empty target from an actual server update.
        await asyncio.to_thread(sys.stdin.readline)
        pending = [asyncio.create_task(bot.wait_for_block_update((x, 100, 0))) for x in (2, 3)]
        await asyncio.sleep(0)
        print("target_watchers_ready", flush=True)
        empty = await asyncio.gather(*pending)
        print("targets_observed=" + json.dumps([u.runtime_id for u in empty]), flush=True)

        for x, update, baseline in zip((2, 3), against, empty):
            await asyncio.to_thread(sys.stdin.readline)
            watch = asyncio.create_task(bot.wait_for_block_update((x, 100, 0), timeout=3))
            await asyncio.sleep(0)
            tick = await bot.place_against(update, face=1, slot=0)
            print("placement_submitted=" + json.dumps({"x": x, "tick": tick}), flush=True)
            try:
                observed = await watch
                expected = baseline.runtime_id if x == 2 else update.runtime_id
                if observed.runtime_id != expected:
                    raise AssertionError(
                        f"target {x}: expected runtime ID {expected}, received {observed.runtime_id}"
                    )
                print("placement_update=" + json.dumps({
                    "x": x, "runtime_id": observed.runtime_id, "layer": observed.layer,
                    "effect": "unchanged" if x == 2 else "placed",
                }), flush=True)
            except TimeoutError as error:
                raise TimeoutError(f"no target update at {x} after input tick {tick}") from error

        print("placement_checks_ready", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print(f"disconnected {name}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--name", default="EndPilotTest")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.name))
