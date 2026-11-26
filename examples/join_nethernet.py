"""Join an offline-mode NetherNet BDS, wait briefly and disconnect."""

import argparse
import asyncio

from endpilot import Bot


async def main(url: str, name: str, hold: float, server_key_sha256: str | None = None) -> None:
    bot = Bot(name)
    try:
        await bot.join_nethernet(url, server_key_sha256=server_key_sha256)
        print(f"spawned {name} runtime_id={bot.player_id}", flush=True)
        await asyncio.sleep(hold)
    finally:
        await bot.close()
    print(f"disconnected {name}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP signaling URL")
    parser.add_argument("--name", default="Knight")
    parser.add_argument("--hold", type=float, default=2)
    parser.add_argument("--server-key-sha256", help="Trusted SHA-256 hex digest of the server DER SPKI")
    args = parser.parse_args()
    asyncio.run(main(args.url, args.name, args.hold, args.server_key_sha256))
