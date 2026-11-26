# EndPilot

EndPilot is a Python SDK for running headless Minecraft Bedrock players. Use `Bot`
to join servers over NetherNet or RakNet, observe world and player state, and
perform player actions from an async Python script. The SDK defaults to Bedrock
protocol 2225 (Minecraft Bedrock 1.26.6x).

## Try it offline

### Install

From the SDK checkout, create a virtual environment with Python 3.12 or later.
Run `python -m venv .venv`, then activate it with `source .venv/bin/activate`
on Linux or `.\.venv\Scripts\Activate.ps1` in PowerShell.

Install the separate native codec, `bedrock-protocol==0.5.0.dev0`, before installing
the SDK. Sign in to GitHub and download the `bedrock_protocol-*.whl` file for
your platform. The wheels target standard CPython on Windows x86-64 and Linux
x86-64. [Upstream build artifacts](https://github.com/EndstoneMC/bedrock-protocol/actions/runs/43980162669)

Install the `.whl` file directly. Substitute the path to the wheel you downloaded.

```sh
python -m pip install "./bedrock_protocol-0.5.0.dev0-cp312-abi3-win_amd64.whl"
```

If the artifact is unavailable, install from the same source revision with Git,
CMake, and a C++23 compiler.
[Codec build instructions](https://github.com/EndstoneMC/bedrock-protocol/blob/1aa8ae119ccb0a4e60f693217d6dcdde00238d13/README.md#python)

```sh
python -m pip install "git+https://github.com/EndstoneMC/bedrock-protocol.git@1aa8ae119ccb0a4e60f693217d6dcdde00238d13"
```

The NetherNet extra uses a pinned Git dependency, so Git must be available on
your `PATH`.

On Windows, install the NetherNet extra with these dependency pins.

```powershell
python -m pip install -e ".[nethernet]" "aiortc==1.16.0" "aioice==0.14.1"
```

On Linux, install the NetherNet extra.

```sh
python -m pip install -e ".[nethernet]"
```

You can check the codec without a server by running `python examples/network_settings.py`.
It prints `RequestNetworkSettingsPacket`, the protocol number, and the encoded batch.

### Run your first bot

Start a Bedrock Dedicated Server in offline mode (`online-mode=false`) with a
NetherNet HTTP signaling endpoint and support for protocol 2225. Replace the URL
below if your server listens on a different address or port.

```sh
python -m endpilot join http://127.0.0.1:19132
```

The CLI joins as `EndPilot`, prints `Joined.`, and disconnects after two seconds.
To run your own bot as `Knight`, save this as `first_bot.py`.

```python
import asyncio

from endpilot import Bot


async def main():
    bot = Bot("Knight")
    try:
        await bot.join_nethernet("http://127.0.0.1:19132")
        print(f"Spawned with runtime ID {bot.player_id}")
    finally:
        await bot.close()


asyncio.run(main())
```

Run `python first_bot.py`. It prints the bot's runtime ID after spawn and then
disconnects. For RakNet, Xbox sign-in, or a longer CLI session, see the
[login tutorial](docs/tutorials/login.md).

## Tutorials

- [Connect to a server and sign in to Xbox](docs/tutorials/login.md)
- [Use and change a custom PNG skin](docs/tutorials/skins.md)
- [Plan and execute a route](docs/tutorials/navigation.md)
- [Open containers and transfer items](docs/tutorials/containers.md)

## Reference

- [Sessions and resource packs](docs/reference/session.md) — client ticks, disconnection, pack offers, and subpacks
- [Players and entities](docs/reference/players.md) — walking, flight, spawns, the player list, nearby entities, and swings
- [Chat, display, and forms](docs/reference/display.md) — chat patterns, private messages, titles, boss bars, scoreboards, and forms
- [World and terrain](docs/reference/world.md) — block search, dimensions, biome IDs, weather, and sign editing
- [Mining and gathering](docs/reference/gathering.md) — veins, tool selection, block and drop collection, and fishing
- [Inventory and items](docs/reference/items.md) — crafting, enchanting, books, and creative inventory
- [Testing and MCP](docs/reference/testing.md) — packet timelines, command observations, placement, and forms
- [Bedrock data tables](docs/reference/block-data.md) — regenerate the protocol 2225 data

[Contributing](CONTRIBUTING.md) · [Apache-2.0](LICENSE)
