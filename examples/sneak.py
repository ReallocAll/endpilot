"""Hold and release sneaking through public EndPilot input on a live server."""

import asyncio
import sys

from endpilot import Bot


async def run(url: str) -> None:
    bot = Bot("EndPilotTest")
    try:
        if url.startswith("raknet://"):
            host, sep, port = url.removeprefix("raknet://").rpartition(":")
            if not sep:
                host, port = port, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        print("spawned EndPilotTest", flush=True)
        # Testbed teleports after this signal; that supplies the first live position.
        await bot.wait_for_position_sync()

        await asyncio.to_thread(sys.stdin.readline)
        print(f"sneak_on_tick={await bot.set_sneaking(True)}", flush=True)
        await asyncio.sleep(0.35)
        await bot.send_chat("endpilot-sneak-on")
        print("sneak_on_probe_sent", flush=True)

        await asyncio.to_thread(sys.stdin.readline)
        print(f"sneak_off_tick={await bot.set_sneaking(False)}", flush=True)
        await asyncio.sleep(0.35)
        await bot.send_chat("endpilot-sneak-off")
        print("sneak_off_probe_sent", flush=True)

        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python examples/sneak.py <nethernet-url|raknet://host:port>")
    asyncio.run(run(sys.argv[1]))
