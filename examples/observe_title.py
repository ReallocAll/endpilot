"""Observe the next title, subtitle, action bar, timing, clear, or reset event."""

import argparse
import asyncio
from dataclasses import asdict
import json

from endpilot import Bot


async def main(url, name, protocol, kind, timeout):
    bot = Bot(name, protocol=protocol)
    try:
        if url.startswith("raknet://"):
            address = url.removeprefix("raknet://")
            host, separator, port = address.rpartition(":")
            if not separator:
                host, port = address, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        print(f"spawned {name} runtime_id={bot.player_id}", flush=True)
        pending = asyncio.create_task(bot.wait_for_title(kind=kind, timeout=timeout))
        await asyncio.sleep(0)
        print("title_watcher_ready", flush=True)
        event = await pending
        print("title_event=" + json.dumps(asdict(event)), flush=True)
    finally:
        await bot.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--name", default="Knight")
    parser.add_argument("--protocol", type=int, default=2225)
    parser.add_argument("--kind", choices=("title", "subtitle", "actionbar", "times", "clear", "reset"))
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    asyncio.run(main(args.url, args.name, args.protocol, args.kind, args.timeout))
