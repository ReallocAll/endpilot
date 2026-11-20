"""Assert that a server explicitly refuses a headless player before spawn."""

import argparse
import asyncio

from endpilot import Bot, LoginRefused


async def run(url: str, name: str) -> None:
    bot = Bot(name)
    try:
        try:
            if url.startswith("raknet://"):
                host, sep, port = url.removeprefix("raknet://").rpartition(":")
                if not sep:
                    host, port = port, "19132"
                await bot.join_raknet(host, int(port))
            else:
                await bot.join_nethernet(url)
        except LoginRefused as error:
            print("login_refused=LoginRefused", flush=True)
            print(f"server_refusal={error}", flush=True)
            return
        raise AssertionError("server accepted a player expected to be refused")
    finally:
        await bot.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--name", default="EndPilotDenied")
    arguments = parser.parse_args()
    asyncio.run(run(arguments.url, arguments.name))
