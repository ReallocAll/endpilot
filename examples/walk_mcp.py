"""Start, observe and stop a walk over MCP stdio, then reuse the same player."""
import argparse
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(url: str):
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "endpilot.mcp_server"])
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()

            async def call(name, **args):
                result = await session.call_tool(name, arguments=args)
                if result.is_error:
                    raise RuntimeError(f"MCP {name}: {result.content}")
                return result.structured_content or json.loads(result.content[0].text)

            try:
                await call("connect", url=url, name="EndPilotTest")
                print("spawned EndPilotTest", flush=True)
                print("mcp_walk_ready", flush=True)
                await asyncio.to_thread(sys.stdin.readline)
                await call("start_walk", ticks=160)
                await asyncio.sleep(0.1)
                status = await call("observe_walk")
                assert status["state"] == "running"
                print("mcp_walk_running=" + json.dumps(status), flush=True)
                await asyncio.to_thread(sys.stdin.readline)
                stopped = await call("stop_walk")
                assert stopped["state"] == "stopped" and 0 < stopped["submitted_frames"] < 160
                print("mcp_walk_stopped=" + json.dumps(stopped), flush=True)
                await asyncio.to_thread(sys.stdin.readline)
                await call("start_walk", ticks=12)
                while True:
                    resumed = await call("observe_walk")
                    if resumed["state"] == "finished":
                        break
                    await asyncio.sleep(0.05)
                assert resumed["submitted_frames"] == 12
                print("mcp_walk_resumed=" + json.dumps(resumed), flush=True)
                await asyncio.to_thread(sys.stdin.readline)
            finally:
                await call("disconnect")
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet signaling URL or raknet://host:port")
    asyncio.run(main(parser.parse_args().url))
