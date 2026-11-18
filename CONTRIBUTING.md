# Contributing

[Install the SDK](README.md#try-it-offline), then run:

```sh
python -m unittest discover -s tests -v
```

Run `python -m pip install -e ".[nethernet]"` to include the NetherNet loopback check.

For a first packet change, follow the [offline example](examples/network_settings.py) into [protocol.py](src/endpilot/protocol.py) and run the [framing tests](tests/test_protocol.py). A small reproduction or a correction to an example is welcome.

In your PR, describe what the user can do and how you checked it. For server behavior, include the BDS version, transport and observed result.
