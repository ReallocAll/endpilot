"""Follow a server-summoned cow on flat ground using the public SDK."""

import argparse
import asyncio
import json
import sys

from endpilot import Bot


async def main(url: str, *, stop: bool = False):
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
        pending = asyncio.create_task(bot.wait_for_entity_spawn(actor_type="minecraft:cow"))
        await asyncio.sleep(0)
        print("follow_spawn_watcher_ready", flush=True)
        spawn = await pending
        print("follow_spawn=" + json.dumps({
            "runtime_id": spawn.runtime_id, "position": spawn.position,
        }), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        task = asyncio.create_task(bot.follow_entity(spawn.runtime_id, ticks=160 if stop else 80))
        await asyncio.sleep(0.1)
        print("follow_started", flush=True)
        if stop:
            await asyncio.to_thread(sys.stdin.readline)
            await bot.stop_moving()
        submitted = await task
        print(("follow_stopped=" if stop else "follow_input_submitted=") + json.dumps({
            "frames": submitted, "position": bot.position,
        }), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--stop", action="store_true", help="Stop active following on stdin")
    args = parser.parse_args()
    asyncio.run(main(args.url, stop=args.stop))
