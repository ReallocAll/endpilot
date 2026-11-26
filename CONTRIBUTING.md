# Contributing

Follow the [installation guide](README.md#install) to set up Python and install
the native codec. From the repository root, install both transport extras and
the MCP extra used in CI.

```sh
python -m pip install -e ".[nethernet,raknet,mcp]"
```

On Windows, append `"aiortc==1.16.0" "aioice==0.14.1"` to the install command
to use the NetherNet dependency pins from the installation guide.

For additional PNG skin and Xbox sign-in and account storage coverage, install
the optional extras before running the suite.

```sh
python -m pip install -e ".[skin,xbox]"
```

Run the tests and the offline codec example from the checkout.

```sh
python -m unittest discover -s tests -v
python examples/network_settings.py
```

For a first packet change, follow the [offline example](examples/network_settings.py) into [protocol.py](src/endpilot/protocol.py) and run the [framing tests](tests/test_protocol.py). A small reproduction or a correction to an example is welcome.

In your PR, describe what the user can do and how you checked it. For server behavior, include the BDS version, transport and observed result.
