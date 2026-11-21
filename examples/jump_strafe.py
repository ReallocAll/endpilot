"""Jump sideways once using the public EndPilot SDK."""

import asyncio
import sys

from endpilot import Bot


async def run(url: str, direction: str) -> None:
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
        # The Testbed prepares loaded ground before triggering the action.
        await asyncio.to_thread(sys.stdin.readline)
        print(f"jump_{direction}_started", flush=True)
        action = bot.jump_left if direction == "left" else bot.jump_right
        print(f"jump_{direction}_ticks={await action(18)}", flush=True)
        await asyncio.sleep(0.4)
        print("jump_finished", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[2] not in ("left", "right"):
        raise SystemExit("usage: python examples/jump_strafe.py <nethernet-url|raknet://host:port> <left|right>")
    asyncio.run(run(sys.argv[1], sys.argv[2]))
