"""Hold and release sneak input while another player watches the bot."""

import argparse
import asyncio

from endpilot import Bot


async def run(url: str, name: str) -> None:
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
            input, "Move the bot to level ground within view, then press Enter to sneak. ",
        )
        await bot.wait_for_position_sync()
        print(f"Sneak input submitted at tick {await bot.set_sneaking(True)}.", flush=True)
        await asyncio.to_thread(input, "Press Enter to stand. ")
        print(f"Stand input submitted at tick {await bot.set_sneaking(False)}.", flush=True)
        await asyncio.to_thread(input, "Press Enter to disconnect. ")
    finally:
        await bot.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--name", default="Knight")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.name))
