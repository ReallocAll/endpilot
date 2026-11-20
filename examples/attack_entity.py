"""Attack a server-spawned cow through Bot.attack_entity; await server removal.

The Testbed summons the cow after receiving the ready marker. Attacks only
submit packets; removal of the actor is separate server evidence of its death.
"""
import argparse
import asyncio
import json
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
        waiting = asyncio.create_task(bot.wait_for_entity_spawn(actor_type="minecraft:cow"))
        await asyncio.sleep(0)
        print("entity_spawn_watcher_ready", flush=True)
        spawned = await waiting
        print("entity_spawn=" + json.dumps({
            "entity_id": spawned.entity_id, "runtime_id": spawned.runtime_id,
            "actor_type": spawned.actor_type, "position": spawned.position,
        }), flush=True)

        await asyncio.to_thread(sys.stdin.readline)
        removal = asyncio.create_task(bot.wait_for_entity_removal(spawned.entity_id, timeout=30))
        await asyncio.sleep(0)
        print("entity_removal_watcher_ready", flush=True)
        attacks = 0
        try:
            for _ in range(35):
                if removal.done():
                    break
                target = next((actor for actor in bot.observed_entities
                               if actor.runtime_id == spawned.runtime_id), None)
                if target is None:
                    break
                await bot.look_at((target.position[0], target.position[1] + 0.75,
                                   target.position[2]))
                await asyncio.sleep(0.06)
                await bot.attack_entity(spawned.runtime_id)
                attacks += 1
                await asyncio.sleep(0.55)
            print("attacks_submitted=" + str(attacks), flush=True)
            await removal
            print("entity_removed=" + json.dumps({
                "entity_id": spawned.entity_id, "runtime_id": spawned.runtime_id,
            }), flush=True)
        finally:
            if not removal.done():
                removal.cancel()
            await asyncio.gather(removal, return_exceptions=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    args = parser.parse_args()
    asyncio.run(run(args.url))
