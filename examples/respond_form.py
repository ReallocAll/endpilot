"""Answer a server action form from an external script using public packet I/O.

Run against a Testbed server with the form probe plugin enabled.
The server must issue a form in response to the player's chat trigger.
"""

import argparse
import asyncio
import json
import sys

from endpilot import Bot


async def answer_next_action_form(bot: Bot, button_index: int, *, timeout: float = 15) -> dict:
    """Wait for the next action form and submit one button choice.

    Returning means the response packet was submitted, not that the server
    accepted the selection. Observe the plugin callback to verify the effect.
    """
    binding = bot.protocol.binding
    request = bot.protocol.decode(await bot.packets.wait_for_packet(
        binding.ModalFormRequestPacket.ID, timeout=timeout,
    ))
    form = json.loads(request.form_json)
    if form.get("type") != "form":
        raise ValueError("expected a server action form")
    buttons = form.get("buttons", [])
    if (isinstance(button_index, bool) or not isinstance(button_index, int)
            or not 0 <= button_index < len(buttons)):
        raise ValueError("button index is outside the server's action form")

    await bot.packets.send(binding.ModalFormResponsePacket(
        form_id=request.form_id,
        json_response=json.dumps(button_index),
        form_cancel_reason=None,
    ))
    return {"form_id": int(request.form_id), "title": form.get("title"),
            "button_index": button_index}


async def run(url: str):
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

        # The packet waiter is registered before the server is asked to send
        # the form, including on a low-latency local BDS.
        pending = asyncio.create_task(answer_next_action_form(bot, 1))
        await asyncio.sleep(0)
        print("form_watch_ready", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        await bot.send_chat("endpilot-form-probe")
        print("action_form_answer_sent=" + json.dumps(await pending), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet signaling URL or raknet://host:port")
    asyncio.run(run(parser.parse_args().url))
