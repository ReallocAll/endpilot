"""Compare forward, backward, lateral and sprint movement through the public SDK."""

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
        await bot.wait_for_position_sync()

        await asyncio.to_thread(sys.stdin.readline)
        print(f"walk_ticks={await bot.walk_forward(18)}", flush=True)

        await asyncio.to_thread(sys.stdin.readline)
        print(f"backward_ticks={await bot.walk_backward(18)}", flush=True)

        await asyncio.to_thread(sys.stdin.readline)
        print(f"left_ticks={await bot.strafe_left(18)}", flush=True)

        await asyncio.to_thread(sys.stdin.readline)
        print(f"right_ticks={await bot.strafe_right(18)}", flush=True)

        await asyncio.to_thread(sys.stdin.readline)
        print(f"sprint_key_tick={await bot.set_sprinting(True)}", flush=True)
        print(f"sprint_ticks={await bot.walk_forward(18)}", flush=True)
        print(f"sprint_release_tick={await bot.set_sprinting(False)}", flush=True)

        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python examples/sprint.py <nethernet-url|raknet://host:port>")
    asyncio.run(run(sys.argv[1]))
