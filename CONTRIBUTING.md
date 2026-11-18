# Contributing

Follow the checkout installation in the [README](README.md), then run:

```sh
python -m unittest discover -s tests
python examples/network_settings.py
```

For a first change, follow the example into `src/endpilot/protocol.py`. The
native dependency owns packet fields and serialization. EndPilot handles game
packet headers and batch boundaries. If a change affects actual server behavior,
include the BDS version and what you observed in the pull request. A small
reproduction or a correction to an example is also welcome.

The SDK is unreleased. Update affected callers together when an interface changes.
Keep repeatable examples and focused regression fixtures with the code. Put
investigation notes in the issue or PR and attach supporting run output there.
