"""Hand a live player from an MCP controller to direct Python SDK control.

The controller and script run in one process. MCP stdio cannot transfer a live
Bot object between processes; this example tests the local ownership boundary.
"""
import argparse
import asyncio
import json
import sys

from endpilot.mcp_server import MCPPlayer


async def main(url: str):
    controller = MCPPlayer()
    bot = None
    try:
        await controller.connect(url, name="EndPilotTest")
        print("spawned EndPilotTest", flush=True)
        print("mcp_walk_ready", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        await controller.start_walk(ticks=160)
        await asyncio.sleep(0.1)
        status = await controller.observe_walk()
        assert status["state"] == "running"
        print("mcp_walk_running=" + json.dumps(status), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        stopped = await controller.stop_walk()
        assert stopped["state"] == "stopped" and 0 < stopped["submitted_frames"] < 160
        print("mcp_walk_stopped=" + json.dumps(stopped), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        bot = await controller.handoff()
        assert controller.bot is None
        frames = await bot.walk_forward(12)
        assert frames == 12
        print("mcp_walk_resumed=" + json.dumps({
            "state": "finished", "submitted_frames": frames,
            "controller": "python_sdk", "server_position": bot.position,
        }), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        if bot is None:
            await controller.disconnect()
        else:
            await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet signaling URL or raknet://host:port")
    asyncio.run(main(parser.parse_args().url))
