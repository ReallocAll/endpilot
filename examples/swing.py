"""Submit an empty-hand arm swing through the public SDK.

Watch the bot from a second Minecraft client to see the swing.
"""

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
        await bot.wait_for_position_sync()
        await asyncio.to_thread(sys.stdin.readline)
        await bot.swing()
        print("swing_submitted", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        print(f"missed_swing_tick={await bot.missed_swing()}", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected Knight", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python examples/swing.py <nethernet-url|raknet://host:port>")
    asyncio.run(run(sys.argv[1]))
