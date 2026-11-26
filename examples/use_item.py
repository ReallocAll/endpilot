"""Switch hotbar slots and use a snowball from the first slot."""
import argparse
import asyncio
import sys

from endpilot import Bot


async def run(url: str) -> None:
    bot = Bot("Knight")
    try:
        if url.startswith("raknet://"):
            host, sep, port = url.removeprefix("raknet://").rpartition(":")
            if not sep:
                host, port = port, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        print("spawned Knight", flush=True)
        # Put a snowball in the first slot before starting.
        for slot in (0, 2, 0):
            await asyncio.to_thread(sys.stdin.readline)
            await bot.select_hotbar(slot)
            print(f"hotbar_selection_submitted={slot}", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        await bot.use_item()
        print("item_use_submitted=1", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected Knight", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    args = parser.parse_args()
    asyncio.run(run(args.url))
