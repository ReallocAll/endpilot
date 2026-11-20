"""Use a server-equipped item in air; Testbed checks the actual BDS inventory result."""
import argparse
import asyncio
import sys

from endpilot import Bot


async def run(url: str) -> None:
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
        # Exercise normal hotbar switching before using a server-equipped item.
        # The Testbed verifies each selection and the snowball decrement.
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
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    args = parser.parse_args()
    asyncio.run(run(args.url))
