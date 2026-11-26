"""Observe a shield granted to the offhand by a live Bedrock server."""

import asyncio
import json
import sys

from endpilot import Bot


async def run(url: str) -> None:
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
        await bot.wait_for_position_sync()

        print("offhand_grant_ready", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        deadline = asyncio.get_running_loop().time() + 12
        while True:
            observed = bot.offhand_item
            if observed is not None and int(observed.id) != 0:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("no server-described offhand item")
            await asyncio.sleep(0.1)
        print("offhand_observed=" + json.dumps({
            "item_id": int(observed.id),
            "count": int(observed.stack_size),
            "net_id": (None if observed.net_id_variant is None
                       else int(observed.net_id_variant)),
        }), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected Knight", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python examples/observe_offhand.py <nethernet-url|raknet://host:port>")
    asyncio.run(run(sys.argv[1]))
