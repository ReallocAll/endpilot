# EndPilot

EndPilot is a Python SDK for Minecraft Bedrock bots. The current development build supports offline-mode join/leave and forward walking on NetherNet and RakNet against BDS 1.26.52 (protocol 2193). Other gameplay actions are still under development.

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

The settings probe prints the server's compression algorithm and threshold. The join example reports spawn and closes the connection after the requested hold time. To require a trusted NetherNet server identity, pass `--server-key-sha256 HEX` or `server_key_sha256=HEX` to `Bot.join_nethernet()`. The pin is the lowercase SHA-256 hex digest of the server operator public key in DER SPKI form; obtain it from a trusted source. A missing, invalid or mismatched identity fails the join. Without a pin, the server identity is not verified. Server-required resource-pack downloads and online authentication are not yet supported.

## Walk on NetherNet or RakNet

The same script uses either transport; press Enter to start walking after joining:

```sh
python -m pip install -e ".[nethernet,raknet]"
python examples/walk.py http://localhost:19132
python examples/walk.py raknet://127.0.0.1:19132
```

`walk_forward()` returns submitted input frames, not confirmed server displacement. Use BDS `querytarget` to verify actual movement. The example's wait for Enter also supports Testbed world setup.

A running player can await a server-initiated kick with
`await bot.wait_for_server_disconnect(timeout=10)`. This returns the upstream
typed `DisconnectPacket` if BDS sent one, raises `ConnectionError` if the
packet stream ends first, or `TimeoutError` if neither occurs by the deadline.
Closing the bot locally is separate. Confirm a server crash from BDS process
status or logs rather than inferring it from a missing client packet.

A server that explicitly rejects a player before spawn raises `LoginRefused`
(a `ConnectionError` subclass), distinct from a join timeout or transport loss.
To test a full-server login refusal with either transport, run
`python examples/refused_login.py URL` against a server whose player limit is already occupied.

## Place a block through MCP

Install the optional MCP tools and register `python -m endpilot.mcp_server` as a local **stdio** MCP server:

```sh
python -m pip install -e ".[nethernet,raknet,mcp]"
python examples/place_block_mcp.py http://localhost:19132
```

The MCP tools `connect`, `watch_block`, `read_block`, `place_against`, and `disconnect` use the ordinary Bot SDK. First watch and read an actual server block update for the support, then submit `place_against` against that observed runtime ID. The returned tick only confirms **input submission**, not that Endstone accepted placement. Observe the target block and use the server state to establish the outcome. The example mirrors Testbed's cancel/allow placement scenario; it expects the Testbed to set up its world and prompt it.

[Contributing](CONTRIBUTING.md) · [Roadmap](ROADMAP.md) · [Apache-2.0](LICENSE)
