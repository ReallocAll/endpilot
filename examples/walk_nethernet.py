"""Submit player input to offline-mode NetherNet BDS; show correction evidence."""

import argparse
import asyncio
from collections import Counter

from endpilot import Bot


async def main(url: str, name: str, frames: int, before: float, after: float):
    bot = Bot(name)
    corrections = []

    try:
        await bot.join_nethernet(url)
        b = bot.protocol.binding

        def observe(raw):
            if raw.packet_id in (b.MovePlayerPacket.ID, b.CorrectPlayerMovePredictionPacket.ID):
                corrections.append(repr(bot.protocol.decode(raw))[:260])

        unsubscribe = bot.packets.subscribe_raw(observe)
        print(f"spawned {name} runtime_id={bot.player_id}", flush=True)
        print(f"initial_predicted={bot._movement.position}", flush=True)
        await asyncio.sleep(before)
        sent = await bot.walk_forward(frames)
        print(f"submitted {sent} input frames; predicted={bot._movement.position}", flush=True)
        await asyncio.sleep(after)
        print(f"movement_correlations={corrections[-5:]}", flush=True)
        unsubscribe()
    finally:
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
