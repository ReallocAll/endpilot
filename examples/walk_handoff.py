"""Hand a live player from an MCP controller to direct Python SDK control.

The controller and script share one process, so the script receives the
live Bot and continues walking on the existing connection.
"""
import argparse
import asyncio
import json

from endpilot.mcp_server import MCPPlayer


async def main(url: str):
    controller = MCPPlayer()
    bot = None
    try:
        await controller.connect(url, name="Knight")
        print("Joined as Knight.", flush=True)
        await asyncio.to_thread(
            input, "Move Knight to level ground facing a clear path, then press Enter to walk. ",
        )
        await controller.start_walk(ticks=160)
        await asyncio.sleep(0.1)
        status = await controller.observe_walk()
        print("mcp_walk_status=" + json.dumps(status), flush=True)
        if status["state"] == "running":
            await asyncio.to_thread(input, "Press Enter to stop the walk early. ")
            stopped = await controller.stop_walk()
            print("mcp_walk_result=" + json.dumps(stopped), flush=True)
        await asyncio.to_thread(input, "Press Enter to hand off control and walk using the Python SDK. ")
        bot = await controller.handoff()
        assert controller.bot is None
        frames = await bot.walk_forward(12)
        assert frames == 12
        print("mcp_walk_resumed=" + json.dumps({
            "state": "finished", "submitted_frames": frames,
            "controller": "python_sdk", "server_position": bot.position,
        }), flush=True)
    finally:
        if bot is None:
            await controller.disconnect()
        else:
            await bot.close()
    print("disconnected Knight", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet signaling URL or raknet://host:port")
    asyncio.run(main(parser.parse_args().url))
