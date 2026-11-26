"""Place a stone block and read its server update through MCP stdio tools."""

import argparse
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(url: str, name: str, support: tuple[int, int, int]) -> None:
    x, y, z = support
    target = (x, y + 1, z)
    params = StdioServerParameters(command=sys.executable, args=["-m", "endpilot.mcp_server"])
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()

            async def call(tool: str, **arguments):
                result = await session.call_tool(tool, arguments=arguments)
                if result.is_error:
                    raise RuntimeError(f"MCP {tool}: {result.content}")
                return result.structured_content or json.loads(result.content[0].text)

            await call("connect", url=url, name=name)
            try:
                print(f"Joined as {name}.", flush=True)
                await asyncio.to_thread(
                    input,
                    f"Put stone in the bot's first hotbar slot and move it within reach of {support}. "
                    f"Clear {support} and {target}, then press Enter. ",
                )
                await call("watch_block", x=x, y=y, z=z, timeout=60)
                print(f"Place stone at {support} now. The bot will place another stone block on top.", flush=True)
                against = await call("read_block", x=x, y=y, z=z)

                await call("watch_block", x=x, y=y + 1, z=z)
                submission = await call("place_against", x=x, y=y, z=z, face=1, slot=0)
                print(f"Placement input submitted at tick {submission['submitted_tick']}.", flush=True)
                update = await call("read_block", x=x, y=y + 1, z=z)
                print(json.dumps(update), flush=True)
                if update["runtime_id"] != against["runtime_id"]:
                    raise RuntimeError(f"The target at {target} did not receive the stone block.")
            finally:
                await call("disconnect")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--name", default="Knight")
    parser.add_argument("--support", nargs=3, type=int, metavar=("X", "Y", "Z"), default=(2, 99, 0),
                        help="support block coordinates (default: 2 99 0)")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.name, tuple(args.support)))
