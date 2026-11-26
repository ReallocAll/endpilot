"""Mine a nearby server block and report the observed UpdateBlock packet."""
import argparse
import asyncio
import json
import sys
from dataclasses import asdict

from endpilot import Bot


async def run(
    url: str, position: tuple[int, int, int], face: int, *, equip: bool = False, protocol: int | None = None,
):
    bot = Bot("Knight") if protocol is None else Bot("Knight", protocol=protocol)
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
        if equip:
            if bot.observed_block(position) is None:
                await bot.request_sub_chunk(position)
                async with asyncio.timeout(15):
                    while bot.observed_block(position) is None:
                        await asyncio.sleep(0.05)
            tool = await bot.equip_harvest_tool(position)
            if tool is None:
                print("harvest_equipment_requested=null", flush=True)
                return
            print("harvest_equipment_requested=" + json.dumps(asdict(tool)), flush=True)
        await bot.look_at((position[0] + 0.5, position[1] + 0.5, position[2] + 0.5))
        update = await bot.mine_block(position, face=face, timeout=15)
        print("mining_update=" + json.dumps({
            "position": update.position, "runtime_id": update.runtime_id,
            "layer": update.layer,
        }), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected Knight", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("x", type=int)
    parser.add_argument("y", type=int)
    parser.add_argument("z", type=int)
    parser.add_argument("--face", type=int, default=1)
    parser.add_argument("--equip", action="store_true", help="choose and equip a suitable tool before mining")
    parser.add_argument("--protocol", type=int, help="override the SDK's default protocol")
    args = parser.parse_args()
    asyncio.run(run(args.url, (args.x, args.y, args.z), args.face,
                    equip=args.equip, protocol=args.protocol))
