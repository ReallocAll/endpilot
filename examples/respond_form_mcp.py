"""Call the separately installed action-form extension over MCP stdio.

Install examples/form_extension and the SDK's optional MCP dependency.
Once the watcher is ready, use a server feature that sends the form to your
player. If that feature responds to chat, pass its message with --message.
"""

import argparse
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(
    url: str, button_index: int = 1, *, name: str = "EndPilot",
    message: str | None = None, timeout: float = 15,
) -> None:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "endpilot_form_extension.mcp_server"],
    )
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
                await call("watch_form", timeout=timeout)
                print(f"Watching for an action form for {name}.", flush=True)
                if message is not None:
                    await call("send_chat", message=message)
                form = await call("read_form")
                answer = await call("answer_form", form_id=form["form_id"], button_index=button_index)
                print("Submitted action-form answer " + json.dumps(answer), flush=True)
            finally:
                await call("disconnect")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet signaling URL or raknet://host:port")
    parser.add_argument("--name", default="EndPilot", help="Player name (default: %(default)s)")
    parser.add_argument("--button-index", type=int, required=True, help="Zero-based button index")
    parser.add_argument("--message", help="Optional chat message that opens the form on your server")
    parser.add_argument("--timeout", type=float, default=15, help="Seconds to wait (default: %(default)s)")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.button_index, name=args.name, message=args.message, timeout=args.timeout))
