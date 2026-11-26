"""Compare normal forward jump with a held-sprint forward jump on the same Bot."""

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
        print("jump_forward_started", flush=True)
        print(f"jump_forward_ticks={await bot.jump_forward(18)}", flush=True)
        await asyncio.sleep(0.4)
        print("jump_finished", flush=True)

        await asyncio.to_thread(sys.stdin.readline)
        print(f"sprint_key_tick={await bot.set_sprinting(True)}", flush=True)
        try:
            print("sprint_jump_started", flush=True)
            print(f"sprint_jump_ticks={await bot.jump_forward(18)}", flush=True)
        finally:
            print(f"sprint_release_tick={await bot.set_sprinting(False)}", flush=True)
        await asyncio.sleep(0.4)
        print("sprint_jump_finished", flush=True)

        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected Knight", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python examples/sprint_jump.py <nethernet-url|raknet://host:port>")
    asyncio.run(run(sys.argv[1]))
