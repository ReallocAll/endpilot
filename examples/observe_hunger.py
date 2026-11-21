"""Observe BDS hunger changes and try eating one server-equipped food item.

Designed for the Testbed: it applies hunger and equips food from BDS commands.
"""
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
        await asyncio.to_thread(sys.stdin.readline)
        print("hunger_watcher_ready", flush=True)
        depleted = await bot.wait_for_hunger_update(timeout=20)
        print(f"hunger_after_effect={depleted}", flush=True)

        await asyncio.to_thread(sys.stdin.readline)

        async def wait_for_food_gain():
            async with asyncio.timeout(15):
                while True:
                    updated = await bot.wait_for_hunger_update(timeout=15)
                    if updated > depleted:
                        return updated

        pending = asyncio.create_task(wait_for_food_gain())
        await asyncio.sleep(0)
        try:
            await bot.use_item()
            print("food_use_submitted=1", flush=True)
            restored = await pending
            print(f"hunger_after_food={restored}", flush=True)
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    args = parser.parse_args()
    asyncio.run(run(args.url))
