"""Submit one right-click on a server-spawned cow through Bot.interact_entity.

Summon a cow nearby after the watcher is ready. Check server-side interaction separately
from the submitted interaction.
"""
import argparse
import asyncio
import json
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
        await bot.look_at((
            spawned.position[0], spawned.position[1] + 0.75,
            spawned.position[2],
        ))
        await asyncio.sleep(0.25)
        await bot.interact_entity(spawned.runtime_id)
        # Packet submission is separate from the server's interaction event.
        print("interactions_submitted=1", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected Knight", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    args = parser.parse_args()
    asyncio.run(run(args.url))
