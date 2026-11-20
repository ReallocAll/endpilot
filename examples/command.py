"""Run a permitted command and observe real BDS text feedback."""

import argparse
import asyncio
import json
import sys

from endpilot import Bot


async def run(url: str, command: str):
    bot = Bot("EndPilotTest")
    try:
        if url.startswith("raknet://"):
            host, separator, port = url.removeprefix("raknet://").rpartition(":")
            if not separator:
                host, port = port, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        print("spawned EndPilotTest", flush=True)
        entered = (await asyncio.to_thread(sys.stdin.readline)).strip()
        if entered:
            command = entered
        packet = await bot.execute_command(command)
        print("command_feedback=" + json.dumps({
            "type": int(packet.body.type), "message": packet.body.message,
        }), flush=True)
        # Keep the player online while Testbed checks the server-side effect.
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--command", default="/setblock ~1 ~ ~ gold_block")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.command))
