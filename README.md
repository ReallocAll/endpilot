# EndPilot

EndPilot is a Python SDK for Minecraft Bedrock bots. The current development build supports NetherNet network negotiation and offline-mode player join/leave on BDS 1.26.52 (protocol 2193). Gameplay actions are still under development.

## Try it offline

Clone the repository and activate a Python 3.12+ virtual environment. Download the `bedrock_protocol-` runtime wheel for your platform from the [upstream build](https://github.com/EndstoneMC/bedrock-protocol/actions/runs/43980162669). From the repository root, install it using its local path:

```sh
python -m pip install /path/to/downloaded.whl
python -m pip install -e .
python examples/network_settings.py
```

If the wheel has expired, build the [same revision from source](https://github.com/EndstoneMC/bedrock-protocol/tree/1aa8ae119ccb0a4e60f693217d6dcdde00238d13#python).

## Probe or join a server

With the SDK installed, pass an offline-mode NetherNet HTTP signaling URL:

```sh
python -m pip install -e ".[nethernet]"
python examples/probe_nethernet.py http://localhost:19132
python examples/join_nethernet.py http://localhost:19132 --name EndPilotTest --hold 10
```

The settings probe prints the server's compression algorithm and threshold. The join example reports spawn and closes the connection after the requested hold time. Server-required resource-pack downloads and online authentication are not yet supported.

[Contributing](CONTRIBUTING.md) · [Roadmap](ROADMAP.md) · [Apache-2.0](LICENSE)
