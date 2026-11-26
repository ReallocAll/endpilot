"""Observe a cow spawning, moving and dropping loot on a Bedrock server."""

import argparse
import asyncio
import json

from endpilot import Bot


async def run(url: str, target_x: float | None = None) -> None:
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

        await asyncio.to_thread(input, "Move Knight to an open area, then press Enter. ")
        pending = asyncio.create_task(bot.wait_for_entity_spawn(actor_type="minecraft:cow", timeout=60))
        await asyncio.sleep(0)
        print("Summon a cow within view of Knight now.", flush=True)
        spawn = await pending
        tracked = next((entity for entity in bot.observed_entities
                        if entity.runtime_id == spawn.runtime_id), None)
        if tracked is None or tracked.entity_id != spawn.entity_id:
            raise AssertionError("spawn missing from observed entity snapshot")
        print("entity_spawn=" + json.dumps({
            "entity_id": spawn.entity_id, "runtime_id": spawn.runtime_id,
            "actor_type": spawn.actor_type, "position": spawn.position,
        }), flush=True)

        await asyncio.to_thread(input, "Press Enter to watch the cow move. ")

        async def await_movement():
            async with asyncio.timeout(60):
                while True:
                    move = await bot.wait_for_entity_movement(spawn.runtime_id, timeout=60)
                    if move.x is not None and (target_x is None or abs(move.x - target_x) < 1):
                        return move

        pending = asyncio.create_task(await_movement())
        await asyncio.sleep(0)
        destination = "a new X coordinate" if target_x is None else f"X={target_x:g}"
        print(f"Move or teleport the cow to {destination} within view of Knight.", flush=True)
        move = await pending
        tracked = next((entity for entity in bot.observed_entities
                        if entity.runtime_id == spawn.runtime_id), None)
        if tracked is None:
            raise AssertionError("cow missing from observed entity snapshot after moving")
        print("entity_state_after_move=" + json.dumps({
            "runtime_id": tracked.runtime_id, "position": tracked.position,
        }), flush=True)
        print("entity_movement=" + json.dumps({
            "runtime_id": move.runtime_id, "x": move.x, "y": move.y, "z": move.z,
        }), flush=True)

        await asyncio.to_thread(input, "Press Enter to watch for cow removal and loot. ")
        pending_drop = asyncio.create_task(bot.wait_for_item_drop(timeout=60))
        pending_removal = asyncio.create_task(bot.wait_for_entity_removal(spawn.entity_id, timeout=60))
        try:
            await asyncio.sleep(0)
            print("Kill the cow within view of Knight with mob loot enabled.", flush=True)
            _, drop = await asyncio.gather(pending_removal, pending_drop)
        finally:
            pending_removal.cancel()
            pending_drop.cancel()
            await asyncio.gather(pending_removal, pending_drop, return_exceptions=True)
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
    finally:
        await bot.close()
    print("disconnected Knight", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--target-x", type=float,
                        help="wait for a movement update within one block of this X coordinate")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.target_x))
