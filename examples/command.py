"""Run a permitted player command and observe the correlated server output."""

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
        await asyncio.to_thread(sys.stdin.readline)
        packet = await bot.execute_command(command)
        output = packet.output
        print("command_result=" + json.dumps({
            "success_count": int(output.success_count),
            "messages": [
                {"id": msg.message_id, "successful": bool(msg.successful), "params": list(msg.params)}
                for msg in output.messages
            ],
        }), flush=True)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--command", default="/setblock 1 99 0 gold_block")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.command))
