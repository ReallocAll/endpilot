"""Observe another real player joining, moving and leaving on one BDS."""

import argparse
import asyncio
import json
import math

from endpilot import Bot

MOVER_NAME = "Knight"


async def join(bot, url):
    if url.startswith("raknet://"):
        host, separator, port = url.removeprefix("raknet://").rpartition(":")
        if not separator:
            host, port = port, "19132"
        await bot.join_raknet(host, int(port))
    else:
        await bot.join_nethernet(url)


def player_state(bot, name):
    return next(
        (actor for actor in bot.observed_entities
         if actor.actor_type == "minecraft:player" and actor.name == name),
        None,
    )


async def until_player(bot, name, *, present, timeout=15):
    async with asyncio.timeout(timeout):
        while True:
            state = player_state(bot, name)
            if (state is not None) == present:
                return state
            await asyncio.sleep(0.05)


async def run(url, *, name=MOVER_NAME):
    observer = Bot()
    mover = Bot(name)
    if observer.identity.name.casefold() == mover.identity.name.casefold():
        observer = Bot(MOVER_NAME)
    try:
        await join(observer, url)
        await join(mover, url)
        observer_name = observer.identity.name
        mover_name = mover.identity.name
        print(f"Joined as {observer_name} and {mover_name}.", flush=True)
        await asyncio.to_thread(
            input,
            f"Move both players to a flat area within view of each other. "
            f"Face {mover_name} along a clear path, then press Enter. ",
        )
        original = await until_player(observer, mover.identity.name, present=True)
        print("player_spawn_observed=" + json.dumps({
            "name": original.name,
            "unique_id": original.entity_id,
            "runtime_id": original.runtime_id,
            "position": original.position,
        }), flush=True)
        await asyncio.to_thread(input, f"Press Enter to walk forward as {mover_name}. ")

        submitted = await mover.walk_forward(24)
        async with asyncio.timeout(12):
            while True:
                state = player_state(observer, mover.identity.name)
                if state is not None and math.hypot(
                    state.position[0] - original.position[0],
                    state.position[2] - original.position[2],
                ) > 0.4:
                    break
                await asyncio.sleep(0.05)
        print("player_move_observed=" + json.dumps({
            "runtime_id": state.runtime_id,
            "position": state.position,
            "submitted_frames": submitted,
        }), flush=True)
        await asyncio.to_thread(input, f"Press Enter to disconnect {mover_name} and observe its removal. ")

        await mover.close()
        await until_player(observer, mover.identity.name, present=False)
        print("player_removed", flush=True)
    finally:
        await mover.close()
        await observer.close()
    print("both_disconnected", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--name", default=MOVER_NAME, help="Mover name (default: %(default)s)")
    args = parser.parse_args()
    asyncio.run(run(args.url, name=args.name))
