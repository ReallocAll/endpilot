"""Observe the next server-reported health update on a live Bedrock player."""

import argparse
import asyncio
import sys

from endpilot import Bot


async def main(url: str):
    bot = Bot("EndPilotTest")
    try:
        if url.startswith("raknet://"):
            address = url.removeprefix("raknet://")
            host, sep, port = address.rpartition(":")
            if not sep:
                host, port = port, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        print(f"spawned EndPilotTest runtime_id={bot.player_id}", flush=True)
        print(f"initial_health={bot.health}", flush=True)

        while True:
            command = (await asyncio.to_thread(sys.stdin.readline)).strip()
            if command == "close" or not command:
                break
            if command == "watch":
                pending = asyncio.create_task(bot.wait_for_health_update(timeout=20))
                await asyncio.sleep(0)
                print("health_watcher_ready", flush=True)
                print(f"health_update={await pending}", flush=True)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    args = parser.parse_args()
    asyncio.run(main(args.url))
