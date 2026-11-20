"""Navigate around a Testbed obstacle on a known, level stone floor."""
import argparse
import asyncio
import json
import sys

from endpilot import Bot


async def main(url: str):
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
        await bot.wait_for_position_sync()
        await bot.request_sub_chunk((1, 100, 1))
        expected = ((0, 99, 0), (0, 100, 0), (0, 101, 0),
                    (1, 100, 0), (1, 101, 0), (1, 100, 1),
                    (3, 100, 0))
        for _ in range(200):
            if all(bot.observed_block(pos) is not None for pos in expected):
                break
            await asyncio.sleep(0.05)
        else:
            raise TimeoutError("expected BDS sub-chunk observations did not arrive")
        print("navigation_terrain_ready=" + json.dumps({
            "obstacle": [bot.observed_block((1, 100, 0)).is_air,
                         bot.observed_block((1, 101, 0)).is_air],
            "floor": bot.observed_block((0, 99, 0)).is_air,
        }), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        frames = await bot.navigate_to((3, 100, 0), ticks=120)
        print("navigation_submitted=" + json.dumps({
            "frames": frames, "last_observed_position": bot.position
        }), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    asyncio.run(main(parser.parse_args().url))
