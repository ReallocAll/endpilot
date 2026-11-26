"""Read the server player list and observe its next update."""

import argparse
import asyncio
from dataclasses import asdict
import json
from uuid import UUID

from endpilot import Bot


def _player_list_json(entries):
    def without_rgba(fields):
        return {name: value for name, value in fields if name != "rgba"}

    return json.dumps([asdict(entry, dict_factory=without_rgba) for entry in entries], default=str)


async def main(url, name, action, player_uuid, timeout):
    bot = Bot(name)
    try:
        if url.startswith("raknet://"):
            address = url.removeprefix("raknet://")
            host, separator, port = address.rpartition(":")
            if not separator:
                host, port = address, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        pending = asyncio.create_task(bot.wait_for_player_list_update(
            action=action, player_uuid=player_uuid, timeout=timeout,
        ))
        await asyncio.sleep(0)
        print("player_list=" + _player_list_json(bot.observed_players), flush=True)
        print("player_list_watcher_ready", flush=True)
        changes = await pending
        print("player_list_update=" + _player_list_json(changes), flush=True)
    finally:
        await bot.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--name", default="Knight")
    parser.add_argument("--action", choices=("add", "remove"))
    parser.add_argument("--player-uuid", type=UUID)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    asyncio.run(main(args.url, args.name, args.action, args.player_uuid, args.timeout))
