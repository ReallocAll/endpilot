# Testing and MCP

## Record a packet timeline

Pass a new `endpilot.qa.PacketTimeline` as `timeline=` to `join_address()`,
`join_nethernet()`, or `join_raknet()` to record incoming packets starting with
network settings. Read the timeline after join, after a login refusal, or
after closing the bot. Each entry contains the raw packet, a packet name or
numeric ID, and a monotonic receipt time. `received_count(name)` includes
entries evicted from the retained history.

```python
from endpilot import Bot
from endpilot.qa import PacketTimeline


async def inspect_join(address):
    bot = Bot("Knight")
    timeline = PacketTimeline()
    try:
        await bot.join_address(address, timeline=timeline)
    finally:
        await bot.close()
        print(timeline.received_count("NetworkSettingsPacket"))
        print(timeline.disconnect_reason())
```

Save `timeline.next_index` before triggering an action and pass it as the
inclusive cursor to `timeline.received(name, since=cursor)`. For commands,
`await bot.send_command(command)` completes after submission.
`await bot.execute_command(command)` also waits for the next command-style
server text. That text carries no request ID. Verify the intended effect from a
separate world, inventory, or server observation.
[Timeline tests](../../tests/test_qa.py) · [Command tests](../../tests/test_commands.py)

## Run the MCP server

Follow the [SDK installation guide](../../README.md#install) for the codec and
transport requirements, then install the MCP extra from the checkout.

```sh
python -m pip install -e ".[mcp]"
```

Configure your MCP client to launch `python -m endpilot.mcp_server` over stdio
using the Python environment where you installed the SDK. Call `connect` with a
NetherNet signaling URL or a `raknet://host:port` address. The default player
name is `EndPilot`. Call `disconnect` when finished.
[MCP tests](../../tests/test_mcp.py)

## Place a block

For a survival placement, select a hotbar slot containing the block item and
move within reach of a support block with an observed empty target next to it.
In Python, pass the support coordinates and face to `await bot.place_block()`.
Supply either `expected_runtime_id` from the current session or a fully
qualified vanilla `expected_block_name` such as `"minecraft:stone"`.

The MCP tool `observed_block(x, y, z, layer=0)` reads the cache without
requesting terrain. The result shows `observed: false` for unknown blocks and
`is_air: true` for observed air. Use the MCP tool
`place_block(x, y, z, expected_runtime_id, face=1, click_pos=None, timeout=15)`
to attempt placement. Its coordinates identify the support block.

The Python and MCP results include the local submission state and reason.
`world_change` and `inventory_change` report independent observations without
attributing the changes to a player. The MCP inventory snapshots include
`user_data_hex`. A client that abandons an in-flight call may miss its progress.
[Placement tests](../../tests/test_placement.py)

For lower-level MCP input, use `watch_block` before the server updates the
support, then `read_block` to record that update. Call `place_against` with
the support coordinates. The returned tick confirms input submission. Check
the target block or server state to verify placement.

## Observe and answer forms

Use a server that sends the player a form through a plugin. Wait for
`ModalFormRequestPacket` through `bot.packets`, pass that packet to
`bot.get_pending_form()`, then answer or close the pending form. Check the
server's form callback to confirm it handled the response.
[Form reference](display.md#answer-player-forms) · [Form tests](../../tests/test_forms.py)
