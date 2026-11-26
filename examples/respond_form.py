"""Watch for a server action form and answer it through the Bot API.

Once the watcher is ready, use a server feature that sends the form to your
player. If that feature responds to chat, pass its message with --message.
"""

import argparse
import asyncio
import json

from endpilot import Bot


async def answer_next_action_form(bot: Bot, button_index: int, *, timeout: float = 15) -> dict:
    """Wait for the next action form and submit one button choice.

    Returning confirms submission of the response packet. Observe the server
    callback or a game state change to confirm the effect.
    """
    binding = bot.protocol.binding
    request = await bot.packets.wait_for_packet(
        binding.ModalFormRequestPacket.ID, timeout=timeout,
    )
    pending = bot.get_pending_form(request)
    if pending is None:
        raise RuntimeError("form is no longer pending")
    form = pending["form"]
    if not isinstance(form, dict) or form.get("type") != "form":
        raise ValueError("expected a server action form")
    await bot.answer_form(button_index, request)
    return {"form_id": pending["form_id"], "title": form.get("title"),
            "button_index": button_index}


async def run(
    url: str, button_index: int = 1, *, name: str = "EndPilot",
    message: str | None = None, timeout: float = 15,
) -> None:
    bot = Bot(name)
    try:
        if url.startswith("raknet://"):
            host, sep, port = url.removeprefix("raknet://").rpartition(":")
            if not sep:
                host, port = port, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        pending = asyncio.create_task(answer_next_action_form(bot, button_index, timeout=timeout))
        try:
            # Give the packet waiter a turn to subscribe before any trigger.
            await asyncio.sleep(0)
            print(f"Watching for an action form for {name}.", flush=True)
            if message is not None:
                await bot.send_chat(message)
            answer = await pending
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        print("Submitted action-form answer " + json.dumps(answer), flush=True)
    finally:
        await bot.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet signaling URL or raknet://host:port")
    parser.add_argument("--name", default="EndPilot", help="Player name (default: %(default)s)")
    parser.add_argument("--button-index", type=int, required=True, help="Zero-based button index")
    parser.add_argument("--message", help="Optional chat message that opens the form on your server")
    parser.add_argument("--timeout", type=float, default=15, help="Seconds to wait (default: %(default)s)")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.button_index, name=args.name, message=args.message, timeout=args.timeout))
