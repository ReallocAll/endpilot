"""Observe a fresh mob spawn and removal from a real server, using public Bot APIs."""

import argparse
import asyncio
import json
import sys

from endpilot import Bot


async def run(url: str):
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
        print("entity_spawn_watcher_ready", flush=True)
        spawn = await pending
        tracked = next((entity for entity in bot.observed_entities
                        if entity.runtime_id == spawn.runtime_id), None)
        if tracked is None or tracked.entity_id != spawn.entity_id:
            raise AssertionError("spawn missing from observed entity snapshot")
        print("entity_spawn=" + json.dumps({
            "entity_id": spawn.entity_id, "runtime_id": spawn.runtime_id,
            "actor_type": spawn.actor_type, "position": spawn.position,
        }), flush=True)

        # Read the expected X coordinate so unrelated mob movement does not pass.
        target_x = float((await asyncio.to_thread(sys.stdin.readline)).strip())

        async def await_target():
            async with asyncio.timeout(15):
                while True:
                    move = await bot.wait_for_entity_movement(spawn.runtime_id)
                    if move.x is not None and abs(move.x - target_x) < 1:
                        return move

        pending = asyncio.create_task(await_target())
        await asyncio.sleep(0)
        print("entity_movement_watcher_ready", flush=True)
        move = await pending
        tracked = next((entity for entity in bot.observed_entities
                        if entity.runtime_id == spawn.runtime_id), None)
        if tracked is None or abs(tracked.position[0] - target_x) >= 1:
            raise AssertionError(f"server movement absent from entity snapshot: {tracked}")
        print("entity_state_after_move=" + json.dumps({
            "runtime_id": tracked.runtime_id, "position": tracked.position,
        }), flush=True)
        print("entity_movement=" + json.dumps({
            "runtime_id": move.runtime_id, "x": move.x, "y": move.y, "z": move.z,
        }), flush=True)

        await asyncio.to_thread(sys.stdin.readline)
        pending_drop = asyncio.create_task(bot.wait_for_item_drop())
        pending = asyncio.create_task(bot.wait_for_entity_removal(spawn.entity_id))
        await asyncio.sleep(0)
        print("entity_removal_watcher_ready", flush=True)
        await pending
        drop = await pending_drop
        if drop.item_id not in ("minecraft:beef", "minecraft:leather") or drop.item_count < 1:
            raise AssertionError(f"Unexpected cow loot from BDS: {drop}")
        if not any(entity.runtime_id == drop.runtime_id for entity in bot.observed_drops):
            raise AssertionError("BDS item drop missing from observed snapshot")
        print("item_drop=" + json.dumps({
            "entity_id": drop.entity_id, "runtime_id": drop.runtime_id,
            "item_id": drop.item_id, "item_count": drop.item_count,
            "position": drop.position,
        }), flush=True)
        if any(entity.entity_id == spawn.entity_id for entity in bot.observed_entities):
            raise AssertionError("removed actor remained in observed entity snapshot")
        print("entity_removed=" + json.dumps({"entity_id": spawn.entity_id}), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    args = parser.parse_args()
    asyncio.run(run(args.url))
