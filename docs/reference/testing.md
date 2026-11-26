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
server text. A server disconnect received before that text raises
`ConnectionError`. The text carries no request ID. Verify the intended effect
from a separate world, inventory, or server observation.

The synchronous `bot.complete_command(text)` reads the current command
advertisement and live soft-enum updates without sending packets or executing
a command. Pass the text with an optional leading slash. Completion matches
the current token by case-sensitive prefix. Trailing whitespace starts a new
token.

The result is a tuple of advertised command names or aliases for the first
token. For arguments, completion covers unquoted, whitespace-separated values
from positional hard or soft enums, including optional enum parameters. Every
earlier position in the same overload must contain a matching enum value. The
method returns `None` before the first advertisement and an empty tuple when
there are no supported matches.
Use `bot.complete_command_name(prefix)` to complete just the first command word.

```python
bot.complete_command('/packstack cl')
bot.complete_command('/scoreboard objectives remove ep_')
```

The first call can return `("client",)` when the server advertises that choice.
The second can include `"ep_alpha"` while that objective is advertised.

[Timeline tests](../../tests/test_qa.py) · [Command tests](../../tests/test_commands.py) · [Session tests](../../tests/test_session_termination.py)

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
Use `BlockPlacementGoal(destination)` to find a stance and get the support,
face, and click point for an adjacent placement. [Navigation tutorial](../tutorials/navigation.md#navigate-to-place-a-block)

By default, the target is the observed air cell adjacent to the clicked face.
Pass `replace=True` to target the clicked cell when its main layer is known
to contain vanilla `minecraft:short_grass`.

```python
result = await bot.place_block(
    grass_position, face=1, expected_block_name="minecraft:stone", replace=True,
)
```

A replacement request without that observation or a default request on known
short grass returns `precondition_failed` before sending a click.

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

## Place an oak boat

In Survival, select a hotbar slot with a server-observed oak boat stack and
move within reach of a known source water block. Pass the water block's
coordinates to `await bot.place_oak_boat(position, timeout=15)`. The call
checks the held item against the current session's item registry and requires
protocol 2225's vanilla `minecraft:water` with `liquid_depth=0` in the main
layer. It synchronizes the player's position and submits one top-face click
at the block-local point `(0.5, 0.875, 0.5)`.

`BoatPlacementResult` records the original dimension, water block, selected
slot, and local submission state. `submitted` is `False` before the write,
`None` if the write began without local confirmation, and `True` after the
transport confirms the send. `observations_complete` means the SDK received
both an `AddActorPacket` for a new `minecraft:boat` at the exact click
position and a one-item decrease in the selected slot after the send. These
are independent server observations with no request ID linking them to the
click.

The timeout covers position synchronization, submission, and observations.
The result preserves either observation on timeout, dimension change, or
disconnect. If the caller cancels after the action starts, the original
`CancelledError` carries the partial result in `placement_progress`.
[Placement tests](../../tests/test_placement.py)

## Observe and answer forms

Use a server that sends the player a form through a plugin. Wait for
`ModalFormRequestPacket` through `bot.packets`, pass that packet to
`bot.get_pending_form()`, then answer or close the pending form. Check the
server's form callback to confirm it handled the response.
[Form reference](display.md#answer-player-forms) · [Form tests](../../tests/test_forms.py)
