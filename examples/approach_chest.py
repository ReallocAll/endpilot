"""Walk around obstacles to a prepared chest and open it from the chosen face."""

import argparse
import asyncio
from dataclasses import asdict
import json
import math

from endpilot import Bot


async def run(url: str, target: tuple[int, int, int], protocol: int, radius: int) -> None:
    bot = Bot("Knight", protocol=protocol)
    try:
        await bot.join_nethernet(url)
        print(f"spawned Knight runtime_id={bot.player_id}", flush=True)
        await asyncio.to_thread(input, "Press Enter after preparing and positioning the player: ")
        x, _, z = await bot.wait_for_position_sync()
        start_x, start_z = math.floor(x), math.floor(z)
        y = target[1]
        sections = [
            (cx << 4, cy << 4, cz << 4)
            for cx in range((start_x - radius) >> 4, ((start_x + radius) >> 4) + 1)
            for cz in range((start_z - radius) >> 4, ((start_z + radius) >> 4) + 1)
            for cy in range((y - 1) >> 4, ((y + 1) >> 4) + 1)
        ]
        for section in sections:
            await bot.request_sub_chunk(section)
        for _ in range(100):
            if all(bot.observed_block(section) is not None for section in sections):
                break
            await asyncio.sleep(0.05)

        approach = await bot.approach_block(target, search_radius=radius)
        print("chest_approach=" + json.dumps({
            **asdict(approach), "server_position": bot.position,
        }), flush=True)
        if approach.face is None:
            return
        window = await bot.open_container(target, face=approach.face)
        try:
            slots = await bot.wait_for_container_slots()
            print("chest_opened=" + json.dumps({
                "id": window.container_id, "position": window.position, "slots": len(slots),
            }), flush=True)
        finally:
            await bot.close_container()
    finally:
        await bot.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL")
    parser.add_argument("target", type=int, nargs=3, metavar=("X", "Y", "Z"))
    parser.add_argument("--protocol", type=int, default=2225)
    parser.add_argument("--search-radius", type=int, default=8)
    args = parser.parse_args()
    asyncio.run(run(args.url, tuple(args.target), args.protocol, args.search_radius))
