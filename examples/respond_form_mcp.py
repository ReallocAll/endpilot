"""Call the separately installed action-form extension over MCP stdio.

Install examples/form_extension and the SDK's optional MCP dependency.
The Testbed form probe plugin sends a form after a chat trigger.
"""

import argparse
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(url: str):
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

            try:
                await call("connect", url=url, name="EndPilotTest")
                print("spawned EndPilotTest", flush=True)
                await call("watch_form")
                print("form_watch_ready", flush=True)
                await asyncio.to_thread(sys.stdin.readline)
                await call("send_chat", message="endpilot-form-probe")
                form = await call("read_form")
                answer = await call("answer_form", form_id=form["form_id"], button_index=1)
                assert answer["submitted"] and form["button_count"] == 2
                print("action_form_answer_sent=" + json.dumps({
                    "form_id": answer["form_id"],
                    "title": answer["title"],
                    "button_index": answer["button_index"],
                }), flush=True)
                await asyncio.to_thread(sys.stdin.readline)
            finally:
                await call("disconnect")
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet signaling URL or raknet://host:port")
    asyncio.run(run(parser.parse_args().url))
