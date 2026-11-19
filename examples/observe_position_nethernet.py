"""Join an offline-mode NetherNet server and observe a server position update."""

import argparse
import asyncio
import json

from endpilot import Bot


async def main(url: str, name: str, hold: float):
    bot = Bot(name)
    try:
        await bot.join_nethernet(url)
        print(f"spawned {name} runtime_id={bot.player_id}", flush=True)
        pos = await bot.wait_for_position_sync()
        print("server_position_update=" + json.dumps(dict(zip(("x", "y", "z"), pos))), flush=True)
        await asyncio.sleep(hold)
    finally:
        await bot.close()
    print(f"disconnected {name}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP signaling URL")
    parser.add_argument("--name", default="EndPilotTest")
    parser.add_argument("--hold", type=float, default=2)
    args = parser.parse_args()
    asyncio.run(main(args.url, args.name, args.hold))
