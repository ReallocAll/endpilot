"""Observe real BDS villager quotes without submitting an exchange."""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict

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
        await asyncio.to_thread(sys.stdin.readline)  # Wait for Testbed world setup.
        waiting = asyncio.create_task(bot.wait_for_entity_spawn(actor_type="minecraft:villager_v2"))
        await asyncio.sleep(0)
        print("villager_watch_ready", flush=True)
        villager = await waiting
        print("villager_spawn=" + json.dumps(asdict(villager)), flush=True)

        await asyncio.to_thread(sys.stdin.readline)
        await bot.look_at((villager.position[0], villager.position[1] + 1, villager.position[2]))
        await asyncio.sleep(0.3)
        offers = await bot.open_trade(villager.runtime_id)
        print("villager_quotes=" + json.dumps(asdict(offers)), flush=True)
        window = bot.open_window
        if window is None or window.container_id != offers.window_id:
            raise AssertionError("trade response did not open the matching window")
        closed = await bot.close_container()
        if closed is None or closed.container_id != offers.window_id:
            raise AssertionError("BDS did not acknowledge the trade window close")
        print("trade_window_closed=" + str(closed.container_id), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    asyncio.run(run(parser.parse_args().url))
