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
`ConnectionError`. The text carries no request ID.

On protocol 2225, `await bot.execute_command_output(command, timeout=...)`
returns the native `CommandOutputPacket`. It matches a reply with the
`PLAYER` origin and the unique request UUID echoed by BDS. The timeout
covers waiting to submit the request, sending it, and waiting for the reply.
The SDK makes at most one send attempt per call. Replies reporting syntax
errors or rejections are returned as packets, including when the success
count is zero.

On servers that support `/packstack client`, read the success count and
messages from the reply.

```python
reply = await bot.execute_command_output('/packstack client')
print(reply.output.success_count)
print(reply.output.messages)
```

Verify the intended effect of either command call from a separate world,
inventory, or server observation.

The synchronous `bot.complete_command(text)` reads the current command
advertisement, live soft enums and player list. It sends no packets. Pass
the text with an optional leading slash. Completion matches the current
token by case-sensitive prefix.

The result is a tuple of advertised command names or aliases for the first
token. For arguments, completion covers values from positional hard or soft
enums, including optional enum parameters. Earlier positions must match their
advertised parameter in the same overload. Trailing whitespace starts a new
token outside an open player target quote.

When the catalog advertises the standard `/tell` target, the first argument
also completes ordinary names from the current server player list. The same
applies to `/w` and `/msg` when they are advertised as aliases of `/tell`.
Names made of ASCII letters, digits and underscores are returned literally.
Names with internal spaces return one double-quoted token. An open double
quote lets the prefix include spaces, as in `/tell "Dm P`. Player suggestions
end when the target is closed or the input moves to the message.

When the catalog advertises the player target for `/scoreboard players set`
or `reset`, an open double quote completes names from the current player list.
For example, `/scoreboard players set "Dm P` can return `"Dm Peer"`. After a
closed quoted name that matches an online player and a separating space,
completion uses the objective enum advertised for that overload. Soft enum
updates become visible in subsequent queries. Objective completion also supports
unquoted, single-word player names.

`bot.complete_command_with_span(text)` returns an immutable
`endpilot.commands.CommandCompletion` with `start`, `end`, and `tokens`.
Use `text[:result.start] + token + text[result.end:]` to apply a candidate.
The indices are Python string indices and preserve any leading slash on a
command name. For `/tell Dm` and `/tell "Dm P`, the spans are `[6, 8)` and
`[6, 11)`, and both can return the token `"Dm Peer"`.
For `/scoreboard players set "Dm P`, the span is `[24, 29)`. For
`/scoreboard players set "Dm Peer" ep_`, the span covers only `ep_`.

Both methods return `None` when the catalog is unavailable. An empty tuple
of tokens means the catalog is known and there are no supported matches.
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
name is `EndPilot` and the default locale is `en_US`. Call `disconnect` when
finished.

To render translated chat entries, add a local Bedrock `.lang` file to the MCP
server's launch arguments. Repeat `--chat-language` to provide other locales.

```sh
python -m endpilot.mcp_server --chat-language zh_CN /path/to/zh_CN.lang
```

Pass `locale="zh_CN"` to `connect` to use that language for the player.
`reconnect` and `switch_account` retain the connection locale. File paths are
configured in the host launch arguments. `subscribe_chat` keeps ordinary text
and the original translation keys and parameters when no translation is
available. Inspect compound or preformatted text through `raw_message`. Player
command output is delivered one record at a time in the order received,
including repeated records and errors. Formatted messages retain Bedrock
formatting codes.
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

## Place an entity

In Survival on protocol 2225, select a server-observed stack of a supported
item and move within reach of the required block. Pass that block's
coordinates to `await bot.place_entity(position, face=1, timeout=15)`. The SDK
resolves the selected item name through the current session's registry and
sends one click when the terrain and held item still match. Face `1` selects
the top. Cow, pig, zombie, and villager spawn eggs also accept horizontal
faces `2` through `5` on stone. An unsupported face or surface fails the
precondition before submission. The face must be an integer from `0` through
`5`. Booleans, floats, and out-of-range values raise `ValueError`.

| Held item | Observed block | Local click point | Matching `AddActorPacket` |
| --- | --- | --- | --- |
| Plain watercraft below | Source water | `(0.5, 0.875, 0.5)` | `minecraft:boat` with the hull's integer `VARIANT` |
| Chest watercraft below | Source water | `(0.5, 0.875, 0.5)` | `minecraft:chest_boat` with the hull's integer `VARIANT` |
| `minecraft:minecart` | Ordinary, powered, activator, or inactive detector rail | `(0.5, 0.125, 0.5)` | `minecraft:minecart` |
| `minecraft:chest_minecart` | Ordinary, powered, activator, or inactive detector rail | `(0.5, 0.125, 0.5)` | `minecraft:chest_minecart` |
| `minecraft:hopper_minecart` | Ordinary, powered, activator, or inactive detector rail | `(0.5, 0.125, 0.5)` | `minecraft:hopper_minecart` |
| `minecraft:armor_stand` | Stone with clearance | `(0.5, 1.0, 0.5)` | `minecraft:armor_stand` |
| `minecraft:end_crystal` | Obsidian or bedrock with clearance | `(0.5, 1.0, 0.5)` | `minecraft:ender_crystal` |
| `minecraft:cow_spawn_egg` | Spawn egg surfaces below | Face center | `minecraft:cow` |
| `minecraft:pig_spawn_egg` | Spawn egg surfaces below | Face center | `minecraft:pig` |
| `minecraft:zombie_spawn_egg` | Spawn egg surfaces below | Face center | `minecraft:zombie` |
| `minecraft:villager_spawn_egg` | Spawn egg surfaces below | Face center | `minecraft:villager_v2` |
| Additional spawn eggs below | Stone top with clearance | `(0.5, 1.0, 0.5)` | The corresponding vanilla actor below |

The 22 watercraft use the `minecraft:` namespace and the item names in this
table.

| Plain item | Chest item | `VARIANT` |
| --- | --- | ---: |
| `oak_boat` | `oak_chest_boat` | 0 |
| `spruce_boat` | `spruce_chest_boat` | 1 |
| `birch_boat` | `birch_chest_boat` | 2 |
| `jungle_boat` | `jungle_chest_boat` | 3 |
| `acacia_boat` | `acacia_chest_boat` | 4 |
| `dark_oak_boat` | `dark_oak_chest_boat` | 5 |
| `mangrove_boat` | `mangrove_chest_boat` | 6 |
| `bamboo_raft` | `bamboo_chest_raft` | 7 |
| `cherry_boat` | `cherry_chest_boat` | 8 |
| `pale_oak_boat` | `pale_oak_chest_boat` | 9 |
| `poplar_boat` | `poplar_chest_boat` | 10 |

Source water is vanilla `minecraft:water` with `liquid_depth=0`. Ordinary
`minecraft:rail` uses `rail_direction` of `0` through `9`, including flat,
ascending, and curved shapes. Powered (`minecraft:golden_rail`), activator,
and detector rails use `rail_direction` of `0` through `5`. Powered and
activator rails accept `rail_data_bit` of `0` or `1`. A detector rail requires
`rail_data_bit=0` before sending and can activate after a minecart spawns.
The four listed spawn eggs share the same placement rules. Top faces support
stone, grass blocks, dirt, and oak planks. Vanilla Survival checks exercised
cow eggs on all four top surfaces and all four stone sides, pig eggs on an
oak plank top and the east face of stone, zombie eggs on the east face of
stone, and villager eggs on the south face of stone.

The additional spawn eggs support face `1` on `minecraft:stone` in protocol
2225. Use the `minecraft:` namespace and append `_spawn_egg` to the following
names. These eggs spawn the same namespaced actor without the suffix.

`armadillo`, `bogged`, `breeze`, `cat`, `cave_spider`, `chicken`,
`copper_golem`, `creeper`, `donkey`, `endermite`, `fox`, `goat`, `hoglin`,
`horse`, `husk`, `llama`, `mooshroom`, `mule`, `ocelot`, `panda`, `parched`,
`piglin_brute`, `piglin`, `pillager`, `polar_bear`, `rabbit`, `sheep`,
`silverfish`, `skeleton_horse`, `skeleton`, `sniffer`, `snow_golem`,
`spider`, `stray`, `trader_llama`, `vindicator`, `witch`, `wolf`, `zoglin`,
`zombie_horse`, `zombie_pigman`.

The stone-top catalog also includes `minecraft:evoker_spawn_egg`, which
spawns `minecraft:evocation_illager`, and `minecraft:zombie_villager_spawn_egg`,
which spawns `minecraft:zombie_villager_v2`.

For egg, armor stand, and end crystal placement, the SDK checks the cell adjacent to the
clicked face and the cell above it for observed air. It also checks layer 1
of those two cells and the clicked block. A horizontal egg placement can have
air below the adjacent cell. It matches the spawn at that cell's horizontal
center and base height. The initial `AddActorPacket` matches the world click
coordinates for boats and other land placements, or the rail block center, offset
`(0.5, 0.5, 0.5)`, for minecarts. Coordinates are compared at the wire's
float32 precision, with a tolerance of `0.001` block per axis for minecarts.
A minecart's position can change after it spawns.

`EntityPlacementResult` records the original dimension, clicked block,
selected slot, and local submission state. `submitted` is `False` before
the write, `None` if the write began without local confirmation, and `True`
after the transport confirms the send. `observations_complete` means the SDK
observed both a matching new spawn and a one-item decrease in the original
selected slot after sending. These are independent server observations with
no request ID linking them to the click.

The timeout covers position synchronization, submission, and observations.
The result preserves either observation on timeout, dimension change, or
disconnect. If the caller cancels after the action starts, the original
`CancelledError` carries the partial result in `placement_progress`.
[Placement tests](../../tests/test_placement.py)

Call `bot.subscribe_entity_placement_completions(callback)` in a joined
session to observe complete local `place_entity` and `place_oak_boat` calls.
The synchronous callback receives the same immutable result once, immediately
before the call returns. `place_oak_boat` still delivers a `BoatPlacementResult`.
Call the returned function to unsubscribe. Registrations also expire when the
session ends. Partial, failed, and cancelled calls produce no completion.
Use `subscribe_entity_lifecycle` for other observed spawns. Concurrent calls
can match the same server observations, which carry no actor attribution to
the local placement request.

## Place an oak boat

`await bot.place_oak_boat(position, timeout=15)` uses the same source-water
requirements and click point. It returns `BoatPlacementResult` and matches a
new `minecraft:boat` at the exact spawn coordinates using its actor type and
position. Use `place_entity` to require the oak `VARIANT` on that spawn.

### Place entities from MCP scripts

Connect a resident player over stdio and start an async `main(bot, ...)` with
`run_script`. The worker's `bot` supports `await bot.place_entity(position)`,
`await bot.place_oak_boat(position)`, and
`bot.subscribe_entity_placement_completions(callback)`. Use `observe_script`
to read the script result. The action reply and subscription callback receive
separate snapshots with the placement reason, spawn and inventory observations.
Snapshot fields are accessible by attribute. Byte fields such as
`user_data_buffer` are represented as hex strings.

Worker notifications are queued. Keep the script running for callbacks it
needs to receive. A synchronous callback observes only completed local action
calls. Use `subscribe_entity_lifecycle` to observe other spawns. Call the
returned function to unsubscribe. Script stop and player reconnect also
release the script's subscriptions.

## Observe and answer forms

Use a server that sends the player a form through a plugin. Wait for
`ModalFormRequestPacket` through `bot.packets`, pass that packet to
`bot.get_pending_form()`, then answer or close the pending form. Check the
server's form callback to confirm it handled the response.
[Form reference](display.md#answer-player-forms) · [Form tests](../../tests/test_forms.py)
