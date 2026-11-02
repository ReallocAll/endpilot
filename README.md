# Endpilot

Endpilot is an asynchronous Python bot framework for Minecraft: Bedrock Edition.
It manages connections, movement, world observations, inventories and trading over
NetherNet by default, with an opt-in RakNet transport. Gameplay support covers
protocols **1001** and **2193** over NetherNet and RakNet.

## Quick start

Use Python 3.12 or newer with the matching native protocol runtime installed.
The server must provide a NetherNet HTTP signaling endpoint.

Save this as `connect.py`, then pass the endpoint:

```python
import asyncio
import sys

from endpilot import BotOptions, Runtime


async def main():
    async with Runtime() as runtime:
        bot = runtime.add_bot(BotOptions(
            server_url=sys.argv[1],
            # Optional identity-key pin:
            # server_key_sha256="VERIFIED_64_HEX_DIGIT_SHA256",
            name="ExampleBot",
        ))
        await bot.wait_ready(60)
        print(bot.get_status().position)
        await bot.disconnect()


asyncio.run(
    main(),
    loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None,
)
```

```shell
python connect.py http://server.example:19672
```

With or without a key pin, NetherNet's library verifies the server identity
statement signature and SDP fingerprint signature and establishes DTLS. The
optional `server_key_sha256` pin strictly checks the expected identity key digest
when provided. RakNet setup and its optional game-handshake key pin are described
below. Xbox account authentication is not provided; an offline XUID is only a
declared identity field.

## Installation

The SDK depends on `bedrock-protocol-native` and the NetherNet implementation
specified in [pyproject.toml](pyproject.toml). Use native wheels compatible with
your Python interpreter and platform. See the protocol project's
[Python guide](https://github.com/EndstoneMC/bedrock-protocol/blob/main/docs/python.md)
for its compiler and native runtime packages.

To use RakNet, install its optional transport dependency with
`python -m pip install "endpilot[raknet]"` (use `.[raknet]` when installing
from a checkout).

From this checkout or an extracted SDK source distribution, install into a virtual
environment. Point `--find-links` at the matching compiler and native wheels:

```shell
python -m pip install --find-links ../bedrock-protocol/dist .
```

Replace that wheel-directory path for your layout. Other dependencies are resolved
from their declared sources; the NetherNet Git dependency requires network access.

## RakNet

NetherNet remains the default transport; its optional `server_key_sha256` pin
checks the expected server identity key. For RakNet, set `transport="raknet"`
and pass an explicit `raknet://host:port` endpoint in `server_url`. The URI
cannot contain user information, a path, a query or a fragment. RakNet supports
protocols 1001 and 2193.
See the runnable [RakNet connection example](examples/raknet_connect.py).

On RakNet, `server_key_sha256` is an optional SHA-256 pin of the DER-encoded
SubjectPublicKeyInfo for the server's game-handshake key. When supplied, the
server must perform that handshake before login succeeds, and its key is checked
against the pin. Without a pin, the handshake's self-signature is still checked,
but the server identity is unauthenticated and a man-in-the-middle attack is
possible. When a server sends the signed game handshake, RakNet enables native
game encryption and uses the native batch codec over reliable ordered FE framing;
it does not use DTLS. Without a pin, a server may omit the handshake and still
report login success. This transport does not add Xbox account authentication or
shared-peer support.

## Using the API

- **Loop ownership:** enter and use `Runtime` on one asyncio loop. Only
  `Runtime.request_stop()` is thread-safe. Context exit awaits connection cleanup.
- **Windows:** use `asyncio.SelectorEventLoop` as shown above. Proactor UDP cleanup
  is unsupported; Selector has a 512-socket limit and no asyncio subprocess pipes.
- **Observations:** gameplay captures, events and results are immutable. Retained values
  remain valid after updates, reconnect or runtime destruction. `copy()` detaches
  a subtree; `to_dict()` and `to_list()` materialize it.
- **Inventory:** readiness does not mean every slot is known. Operations use
  observed items and current references. A timed-out or cancelled waiter does not
  cancel a submitted transaction; never retry an indeterminate result.
- **Transport statistics:** application packet and batch counters are available;
  connected-byte counters are unknown (`None`) on both transports. Application
  limits do not bound transport backlog or reassembly memory.

The [API guide](docs/api.md) covers observations, operations, events,
statistics and error handling. The runnable examples demonstrate a
[RakNet connection](examples/raknet_connect.py),
[behavior composition](examples/behavior_composition.py), an
[observed trade](examples/first_trade.py) and a
[trade/restock cycle](examples/villager_cycle.py). The trade examples require a
prepared server and explicit target identifiers.

## Contributing

Changes to the Python API should preserve loop ownership, immutable captures and
transaction outcomes. See the [API guide](docs/api.md) for these contracts.

Build the wheel and source distribution from the repository root:

```shell
python -m pip install build
python -m build --outdir dist
```

In an isolated test environment, install the built SDK wheel with test dependencies
and access to the compatible protocol wheels. The filenames below follow the
current package version; use the filenames produced by your build:

```shell
python -m pip install --find-links ../bedrock-protocol/dist "dist/endpilot-0.1.0-py3-none-any.whl[test,raknet]"
mkdir ../endpilot-test-source
tar -xf dist/endpilot-0.1.0.tar.gz -C ../endpilot-test-source
cd ../endpilot-test-source/endpilot-0.1.0
python -I -m pytest tests/python tests/integration/nethernet/test_transport.py tests/integration/raknet
```

Choose a new extraction directory outside the checkout. Run pytest with the
interpreter where the wheels are installed; isolated mode prevents importing the
SDK from the checkout. These tests include controlled native transport checks;
real-server gameplay needs a separately prepared server.

## License

[Apache-2.0](LICENSE).
