"""Open a server-created chest; the server's ContainerOpenPacket confirms the window."""
import argparse
import asyncio
import json
import sys

from endpilot import Bot


async def run(url: str) -> None:
    bot = Bot("EndPilotTest")
    target = (1, 100, 0)
    try:
        if url.startswith("raknet://"):
            host, sep, port = url.removeprefix("raknet://").rpartition(":")
            if not sep:
                host, port = port, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        print("spawned EndPilotTest", flush=True)
        # Testbed places the chest after the watch is armed.
        await asyncio.to_thread(sys.stdin.readline)
        pending = asyncio.create_task(bot.wait_for_block_update(target))
        await asyncio.sleep(0)
        print("chest_watch_ready", flush=True)
        observed = await pending
        print(f"chest_runtime_id={observed.runtime_id}", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        window = await bot.open_container(target, face=4)
        print("container_opened=" + json.dumps({
            "id": window.container_id, "type": window.container_type,
            "position": window.position, "entity_unique_id": window.entity_unique_id,
        }), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    args = parser.parse_args()
    asyncio.run(run(args.url))
