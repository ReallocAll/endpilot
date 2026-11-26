"""Repeatedly jump while moving backward through the public EndPilot SDK."""

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
        # Set up solid, level ground around the bot before starting.
        await asyncio.to_thread(sys.stdin.readline)
        print("hop_backward_started", flush=True)
        print(f"hop_backward_ticks={await bot.hop_backward(32)}", flush=True)
        await asyncio.sleep(0.4)
        print("jump_finished", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected Knight", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python examples/hop_backward.py <nethernet-url|raknet://host:port>")
    asyncio.run(run(sys.argv[1]))
