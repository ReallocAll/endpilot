"""Respawn a dead player, then submit fresh movement through the public SDK.

Run with Bedrock Testbed; it kills the joined player and verifies renewed
health and server-received movement independently from these submission markers.
"""
import argparse
import asyncio
import json
import sys

from endpilot import Bot


async def run(url: str) -> None:
    bot = Bot("EndPilotTest")
    try:
        if url.startswith("raknet://"):
            address = url.removeprefix("raknet://")
            host, sep, port = address.rpartition(":")
            if not sep:
                host, port = port, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        print("spawned EndPilotTest", flush=True)
        binding = bot.packets.protocol.binding

        def observe_respawn(raw):
            if raw.packet_id == binding.RespawnPacket.ID:
                packet = bot.packets.protocol.decode(raw)
                print("observed_respawn=" + json.dumps({
                    "state": packet.state.name,
                    "runtime_id": int(packet.runtime_id),
                    "own_runtime_id": bot.player_id,
                }), flush=True)

        unsubscribe = bot.packets.subscribe_raw(observe_respawn)
        while True:
            command = (await asyncio.to_thread(sys.stdin.readline)).strip()
            if not command or command == "close":
                break
            if command == "watch":
                waiting = asyncio.create_task(bot.wait_for_health_update(timeout=20))
                await asyncio.sleep(0)
                print("death_watcher_ready", flush=True)
                print(f"death_health={await waiting}", flush=True)
            if command == "respawn":
                spawn = await bot.respawn()
                print("respawn_ready=" + json.dumps(spawn), flush=True)
                for _ in range(60):
                    if bot.health is not None and bot.health > 0:
                        break
                    await asyncio.sleep(0.05)
                print(f"respawn_health={bot.health}", flush=True)
            if command == "go":
                print(f"walk_frames={await bot.walk_forward(24)}", flush=True)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    args = parser.parse_args()
    asyncio.run(run(args.url))
