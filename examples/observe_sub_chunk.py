"""Read server-confirmed sub-chunk blocks; unknown is never interpreted as air."""

import argparse
import asyncio
from dataclasses import asdict
import json
import sys

from endpilot import Bot


async def main(url: str, name: str) -> None:
    bot = Bot(name)
    try:
        if url.startswith("raknet://"):
            host, sep, port = url.removeprefix("raknet://").rpartition(":")
            if not sep:
                host, port = port, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        print(f"spawned {name} runtime_id={bot.player_id}", flush=True)

        # Prepare the blocks at the requested coordinates before the query.
        await asyncio.to_thread(sys.stdin.readline)
        unknown = bot.observed_block((2000, 100, 2000))
        assert unknown is None, f"distant terrain is unexpectedly known: {unknown}"

        binding = bot.packets.protocol.binding
        responses = []
        unsubscribe = bot.packets.subscribe_raw(
            lambda raw: responses.append(raw) if raw.packet_id == binding.SubChunkPacket.ID else None
        )
        try:
            await bot.request_sub_chunk((2, 100, 0))
            print("sub_chunk_request_submitted", flush=True)
            positions = ((2, 100, 0), (3, 100, 0), (2, 99, 0))
            deadline = asyncio.get_running_loop().time() + 20
            while asyncio.get_running_loop().time() < deadline:
                samples = [bot.observed_block(pos) for pos in positions]
                if responses and all(sample is not None for sample in samples):
                    break
                await asyncio.sleep(.05)
            else:
                raise TimeoutError(f"sub-chunk not observed: responses={len(responses)} samples={samples}")
            print("block_snapshots=" + json.dumps({
                "unloaded": None,
                "responses": len(responses),
                "samples": [asdict(sample) for sample in samples],
            }), flush=True)
        finally:
            unsubscribe()
    finally:
        await bot.close()
    print(f"disconnected {name}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--name", default="Knight")
    args = parser.parse_args()
    asyncio.run(main(args.url, args.name))
