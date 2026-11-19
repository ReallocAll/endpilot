"""Join NetherNet, wait for a server teleport, then submit forward walking input."""

import argparse
import asyncio
import json
import sys
import time

from endpilot import Bot


async def main(url: str, name: str, ticks: int, hold: float):
    bot = Bot(name)
    try:
        await bot.join_nethernet(url)
        print(f"spawned {name} runtime_id={bot.player_id}", flush=True)
        # The testbed creates flat ground and teleports the player before sending GO.
        await asyncio.to_thread(sys.stdin.readline)
        before = bot.position or await bot.wait_for_position_sync()
        start = time.monotonic()
        submitted = await bot.walk_forward(ticks)
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)
        print("walk_input_submitted=" + json.dumps({
            "frames": submitted, "elapsed_ms": elapsed_ms,
            "start": dict(zip(("x", "y", "z"), before)),
        }), flush=True)
        await asyncio.sleep(hold)
    finally:
        await bot.close()
    print(f"disconnected {name}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP signaling URL")
    parser.add_argument("--name", default="EndPilotTest")
    parser.add_argument("--ticks", type=int, default=24)
    parser.add_argument("--hold", type=float, default=4)
    args = parser.parse_args()
    asyncio.run(main(args.url, args.name, args.ticks, args.hold))
