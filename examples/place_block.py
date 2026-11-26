"""Place a stone block and read back the target's server update."""

import argparse
import asyncio
import json

from endpilot import Bot


async def run(url: str, name: str, support: tuple[int, int, int]) -> None:
    x, y, z = support
    target = (x, y + 1, z)
    bot = Bot(name)
    try:
        if url.startswith("raknet://"):
            host, sep, port = url.removeprefix("raknet://").rpartition(":")
            if not sep:
                host, port = port, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        print(f"Joined as {name}.", flush=True)
        await asyncio.to_thread(
            input,
            f"Put stone in the bot's first hotbar slot and move it within reach of {support}. "
            f"Clear {support} and {target}, then press Enter. ",
        )
        await bot.wait_for_position_sync()

        watching = asyncio.create_task(bot.wait_for_block_update(support, timeout=60))
        await asyncio.sleep(0)
        print(f"Place stone at {support} now. The bot will place another stone block on top.", flush=True)
        against = await watching

        watching = asyncio.create_task(bot.wait_for_block_update(target))
        try:
            await asyncio.sleep(0)
            tick = await bot.place_against(against, face=1, slot=0)
            print(f"Placement input submitted at tick {tick}.", flush=True)
            update = await watching
        finally:
            watching.cancel()
            await asyncio.gather(watching, return_exceptions=True)

        print(json.dumps({
            "position": update.position, "runtime_id": update.runtime_id, "layer": update.layer,
        }), flush=True)
        if update.runtime_id != against.runtime_id:
            raise RuntimeError(f"The target at {target} did not receive the stone block.")
    finally:
        await bot.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--name", default="Knight")
    parser.add_argument("--support", nargs=3, type=int, metavar=("X", "Y", "Z"), default=(2, 99, 0),
                        help="support block coordinates (default: 2 99 0)")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.name, tuple(args.support)))
