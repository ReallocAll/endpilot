"""Follow a server-spawned cow on flat ground using the public SDK."""

import argparse
import asyncio
import json

from endpilot import Bot


async def main(url: str, *, stop: bool = False, switch: bool = False,
               close_active: bool = False) -> None:
    bot = Bot("Knight")
    try:
        if url.startswith("raknet://"):
            host, sep, port = url.removeprefix("raknet://").rpartition(":")
            if not sep:
                host, port = port, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        print("Joined as Knight.", flush=True)
        await asyncio.to_thread(input, "Move Knight to clear, level ground, then press Enter. ")
        print("Spawn a cow within Knight's view now.", flush=True)
        spawn = await bot.wait_for_entity_spawn(actor_type="minecraft:cow", timeout=60)
        print("follow_spawn=" + json.dumps({
            "runtime_id": spawn.runtime_id, "position": spawn.position,
        }), flush=True)

        ticks = 400 if stop or switch or close_active else 80
        task = asyncio.create_task(bot.follow_entity(spawn.runtime_id, ticks=ticks))
        try:
            if switch:
                print("Spawn a second cow nearby to switch targets.", flush=True)
                actor = await bot.wait_for_entity_spawn(actor_type="minecraft:cow", timeout=15)
                bot.switch_follow_target(actor.runtime_id)
                print(f"Switched to cow {actor.runtime_id}.", flush=True)
            if stop or switch:
                await asyncio.to_thread(input, "Press Enter to stop following. ")
                await bot.stop_moving()
            if close_active:
                await asyncio.to_thread(input, "Press Enter to close the connection. ")
                position = bot.position
                await bot.close()
                submitted = await task
            else:
                submitted = await task
                position = bot.position
            print("follow_input_submitted=" + json.dumps({
                "frames": submitted, "position": position,
            }), flush=True)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        await bot.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--stop", action="store_true", help="stop active following on Enter")
    modes.add_argument("--switch", action="store_true", help="switch to a newly spawned cow, then stop on Enter")
    modes.add_argument("--close-active", action="store_true", help="close the Bot during a follow")
    args = parser.parse_args()
    asyncio.run(main(args.url, stop=args.stop, switch=args.switch, close_active=args.close_active))
