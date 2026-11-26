"""Observe the next server-reported health update on a live Bedrock player."""

import argparse
import asyncio

from endpilot import Bot


async def main(url: str):
    bot = Bot("Knight")
    try:
        if url.startswith("raknet://"):
            address = url.removeprefix("raknet://")
            host, sep, port = address.rpartition(":")
            if not sep:
                host, port = port, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        print(f"spawned Knight runtime_id={bot.player_id}", flush=True)
        print(f"initial_health={bot.health}", flush=True)

        await asyncio.to_thread(input, "Put Knight in survival mode, then press Enter to watch for health changes. ")
        pending = asyncio.create_task(bot.wait_for_health_update(timeout=60))
        await asyncio.sleep(0)
        print("Change Knight's health now, for example with damage Knight 2 in the BDS console.", flush=True)
        print(f"health_update={await pending}", flush=True)
    finally:
        await bot.close()
    print("disconnected Knight", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    args = parser.parse_args()
    asyncio.run(main(args.url))
