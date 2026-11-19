"""Join a Bedrock server, wait for a teleport, then submit forward walking input."""

import argparse
import asyncio
import json
import sys
import time

from endpilot import Bot


async def main(url: str, name: str, ticks: int, hold: float):
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
        b = bot.packets.protocol.binding
        observed = {"movement_modes": [], "corrections": 0, "input_locks": []}
        def on_packet(raw):
            if raw.packet_id == b.MovePlayerPacket.ID:
                move = bot.packets.protocol.decode(raw)
                if int(move.player_id) == bot.player_id:
                    observed["movement_modes"].append(int(move.reset_position))
            elif raw.packet_id == b.CorrectPlayerMovePredictionPacket.ID:
                observed["corrections"] += 1
            elif raw.packet_id == b.UpdateClientInputLocksPacket.ID:
                observed["input_locks"].append(int(bot.packets.protocol.decode(raw).input_lock_component_data))
        unsubscribe = bot.packets.subscribe_raw(on_packet)
        # The testbed creates flat ground and teleports the player before sending GO.
        await asyncio.to_thread(sys.stdin.readline)
        before = bot.position or await bot.wait_for_position_sync()
        start = time.monotonic()
        submitted = await bot.walk_forward(ticks)
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)
        print("walk_input_submitted=" + json.dumps({
            "frames": submitted, "elapsed_ms": elapsed_ms,
            "start": dict(zip(("x", "y", "z"), before)),
        }), flush=True)
        await asyncio.sleep(hold)
        print("walk_server_packets=" + json.dumps(observed), flush=True)
        unsubscribe()
    finally:
        await bot.close()
    print(f"disconnected {name}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--name", default="EndPilotTest")
    parser.add_argument("--ticks", type=int, default=24)
    parser.add_argument("--hold", type=float, default=4)
    args = parser.parse_args()
    asyncio.run(main(args.url, args.name, args.ticks, args.hold))
