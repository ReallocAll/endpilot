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

            async def call(tool, **args):
                result = await session.call_tool(tool, arguments=args)
                if result.is_error:
                    raise RuntimeError(f"MCP {tool}: {result.content}")
                return result.structured_content or json.loads(result.content[0].text)

            await call("connect", url=url, name="Knight")
            try:
                print("Joined as Knight.", flush=True)
                await asyncio.to_thread(
                    input, "Move Knight to level ground facing a clear path, then press Enter to walk. ",
                )
                await call("start_walk", ticks=160)
                await asyncio.sleep(0.1)
                status = await call("observe_walk")
                print("mcp_walk_status=" + json.dumps(status), flush=True)
                if status["state"] == "running":
                    await asyncio.to_thread(input, "Press Enter to stop the walk early. ")
                    stopped = await call("stop_walk")
                    print("mcp_walk_result=" + json.dumps(stopped), flush=True)
                await asyncio.to_thread(input, "Press Enter to start a short walk with the same connection. ")
                await call("start_walk", ticks=12)
                while True:
                    resumed = await call("observe_walk")
                    if resumed["state"] == "finished":
                        break
                    await asyncio.sleep(0.05)
                assert resumed["submitted_frames"] == 12
                print("mcp_walk_resumed=" + json.dumps(resumed), flush=True)
            finally:
                await call("disconnect")
    print("disconnected Knight", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet signaling URL or raknet://host:port")
    asyncio.run(main(parser.parse_args().url))
