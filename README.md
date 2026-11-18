# EndPilot

EndPilot is a Python SDK in development for headless Minecraft Bedrock players.
The first target is Bedrock Dedicated Server 1.26.52 (protocol 2193), with
NetherNet as the primary transport and RakNet as a second connection path.

The current checkout can encode typed packets with the upstream native binding
and read or write uncompressed game batches, including raw extension packets.
Login and movement are upcoming. The example runs offline.

## Try it from a checkout

Use Python 3.12+ in a virtual environment. Install the native
`bedrock-protocol` wheel for your platform from the
[tested upstream build](https://github.com/EndstoneMC/bedrock-protocol/actions/runs/43980162669).
The runtime wheel starts with `bedrock_protocol-`; the compiler wheel is a
different package. If the build artifact has expired, build the
[pinned upstream source](https://github.com/EndstoneMC/bedrock-protocol/tree/1aa8ae119ccb0a4e60f693217d6dcdde00238d13)
using its Python installation instructions.

```sh
python -m pip install /path/to/downloaded-wheel.whl
python -m pip install -e .
python examples/network_settings.py
```

The example prints the decoded network settings request, protocol number 2193
and the framed bytes. See [Contributing](CONTRIBUTING.md) to run the checks.

To probe a NetherNet HTTP endpoint, install the transport extra and run the
opening network settings exchange against your server:

```sh
python -m pip install -e '.[nethernet]'
python examples/probe_nethernet.py http://localhost:8080
```

The probe closes after the server advertises its compression settings. Player
authentication, spawning, movement and RakNet are future work.

## Development direction

1. Connect, spawn and move on a real BDS through both transports.
2. Exercise Endstone features through Python scenarios and expose the same actions to AI through MCP.
3. Build out ordinary player workflows, including navigation, gathering, inventory and trading. Measure real multiplayer workloads as these features land.

The SDK may change during development. The first public release is planned
after broad coverage of Mineflayer's basic player workflows.

Licensed under [Apache-2.0](LICENSE).
