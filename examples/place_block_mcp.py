"""Exercise the same Endstone placement scenario through MCP stdio tools."""

import argparse
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(url: str, name: str):
    params = StdioServerParameters(command=sys.executable, args=["-m", "endpilot.mcp_server"])
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()

            async def call(tool: str, **arguments):
                result = await session.call_tool(tool, arguments=arguments)
                if result.isError:
                    raise RuntimeError(f"MCP {tool}: {result.content}")
                return result.structured_content or json.loads(result.content[0].text)

            try:
                player = await call("connect", url=url, name=name)
                print(f"spawned {name} runtime_id={player['player_id']}", flush=True)
                await asyncio.to_thread(sys.stdin.readline)
                for x in (2, 3):
                    await call("watch_block", x=x, y=99, z=0)
                print("support_watchers_ready", flush=True)
                supports = [await call("read_block", x=x, y=99, z=0) for x in (2, 3)]
                print("supports_observed=" + json.dumps([s["runtime_id"] for s in supports]), flush=True)

                for x in (2, 3):
                    await asyncio.to_thread(sys.stdin.readline)
                    await call("watch_block", x=x, y=100, z=0, timeout=3)
                    submission = await call("place_against", x=x, y=99, z=0, face=1, slot=0)
                    print("placement_submitted=" + json.dumps({"x": x, "tick": submission["submitted_tick"]}), flush=True)
                    update = await call("read_block", x=x, y=100, z=0)
                    print("placement_update=" + json.dumps({"x": x, **update}), flush=True)

                print("placement_checks_ready", flush=True)
                await asyncio.to_thread(sys.stdin.readline)
            finally:
                await call("disconnect")
    print(f"disconnected {name}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--name", default="EndPilotTest")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.name))
