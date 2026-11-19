"""Submit player input to offline-mode NetherNet BDS; inspect incoming corrections."""

import argparse
import asyncio
from collections import Counter

from endpilot import Bot


async def main(url: str, name: str, frames: int, before: float, after: float):
    bot = Bot(name)
    counts = Counter()
    corrected = []
    reader = None

    async def read_packets():
        while True:
            raw = await bot.packets.recv_raw()
            counts[raw.packet_id] += 1
            if raw.packet_id in (19, 161, 16):
                decoded = bot.protocol.decode(raw)
                corrected.append(repr(decoded)[:220])

    try:
        await bot.join_nethernet(url)
        print(f"spawned {name} runtime_id={bot.player_id}", flush=True)
        print(f"initial_predicted={bot._movement.position}", flush=True)
        reader = asyncio.create_task(read_packets())
        await asyncio.sleep(before)
        sent = await bot.walk_forward(frames)
        print(f"submitted {sent} input frames; predicted={bot._movement.position}", flush=True)
        await asyncio.sleep(after)
    finally:
        if reader is not None:
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
        print(f"received_packet_ids={dict(counts)} corrections={corrected[-6:]}", flush=True)
        await bot.close()
    print(f"disconnected {name}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP signaling URL")
    parser.add_argument("--name", default="EndPilotTest")
    parser.add_argument("--frames", type=int, default=34)
    parser.add_argument("--before", type=float, default=5)
    parser.add_argument("--after", type=float, default=6)
    args = parser.parse_args()
    asyncio.run(main(args.url, args.name, args.frames, args.before, args.after))
