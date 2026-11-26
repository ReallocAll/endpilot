"""Verify player-to-player chat through two real BDS connections."""

import argparse
import asyncio
import json
import sys

from endpilot import Bot

SENDER_NAME = "Knight"


async def join(bot, url):
    if url.startswith("raknet://"):
        host, separator, port = url.removeprefix("raknet://").rpartition(":")
        if not separator:
            host, port = port, "19132"
        await bot.join_raknet(host, int(port))
    else:
        await bot.join_nethernet(url)


async def run(url, message, *, name=SENDER_NAME):
    sender = Bot(name)
    observer = Bot()
    if observer.identity.name.casefold() == sender.identity.name.casefold():
        observer = Bot(SENDER_NAME)
    try:
        await join(sender, url)
        await join(observer, url)
        print("both_spawned", flush=True)
        await asyncio.to_thread(sys.stdin.readline)

        observed = asyncio.create_task(observer.wait_for_chat(author=sender.identity.name, timeout=15))
        await asyncio.sleep(0)
        await sender.send_chat(message)
        received = await observed
        if received.message != message:
            raise AssertionError(f"Unexpected server chat: {received!r}")
        print("chat_received=" + json.dumps({
            "author": received.author, "message": received.message,
        }), flush=True)
    finally:
        await observer.close()
        await sender.close()
    print("both_disconnected", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--name", default=SENDER_NAME, help="Sender name (default: %(default)s)")
    parser.add_argument("--message", default="endpilot-chat-roundtrip")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.message, name=args.name))
