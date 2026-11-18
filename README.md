# EndPilot

EndPilot is a Python SDK for Minecraft Bedrock bots. The current development build can read and write game packets for BDS 1.26.52 (protocol 2193) and query a NetherNet server's compression settings. Player login and movement are [in development](ROADMAP.md).

## Try it offline

Clone the repository and activate a Python 3.12+ virtual environment. Download the `bedrock_protocol-` runtime wheel for your platform from the [upstream build](https://github.com/EndstoneMC/bedrock-protocol/actions/runs/43980162669). From the repository root, install it using its local path:

```sh
python -m pip install /path/to/downloaded.whl
python -m pip install -e .
python examples/network_settings.py
```

If the wheel has expired, build the [same revision from source](https://github.com/EndstoneMC/bedrock-protocol/tree/1aa8ae119ccb0a4e60f693217d6dcdde00238d13#python). The offline example prints the packet type, protocol number and batch bytes:

```text
RequestNetworkSettingsPacket 2193 06c10100000891
```

## Probe a server

With the SDK installed, pass your NetherNet HTTP signaling URL:

```sh
python -m pip install -e ".[nethernet]"
python examples/probe_nethernet.py http://localhost:8080
```

The probe prints the server's compression algorithm and threshold, then disconnects.

[Contributing](CONTRIBUTING.md) · [Roadmap](ROADMAP.md) · [Apache-2.0](LICENSE)
