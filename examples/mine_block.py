"""Mine a nearby server block and report the observed UpdateBlock packet."""
import argparse
import asyncio
import json
import sys

from endpilot import Bot


async def run(url: str, position: tuple[int, int, int], face: int):
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
        update = await bot.mine_block(position, face=face, timeout=15)
        print("mining_update=" + json.dumps({
            "position": update.position, "runtime_id": update.runtime_id,
            "layer": update.layer,
        }), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("x", type=int)
    parser.add_argument("y", type=int)
    parser.add_argument("z", type=int)
    parser.add_argument("--face", type=int, default=1)
    args = parser.parse_args()
    asyncio.run(run(args.url, (args.x, args.y, args.z), args.face))
