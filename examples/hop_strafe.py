"""Repeat sideways jumps through the public EndPilot SDK."""

import asyncio
import sys

from endpilot import Bot


async def run(url: str, direction: str) -> None:
    if direction not in ("left", "right"):
        raise ValueError("direction must be left or right")
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
        # Set up solid, level ground around the bot before starting.
        await asyncio.to_thread(sys.stdin.readline)
        print(f"hop_{direction}_started", flush=True)
        print(f"hop_{direction}_ticks={await getattr(bot, f'hop_{direction}')(32)}", flush=True)
        await asyncio.sleep(0.4)
        print("jump_finished", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected Knight", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python examples/hop_strafe.py <nethernet-url|raknet://host:port> <left|right>")
    asyncio.run(run(sys.argv[1], sys.argv[2]))
