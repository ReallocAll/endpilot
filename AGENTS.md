# Working on EndPilot

## Project

EndPilot is a Python SDK for headless Bedrock players. Scripts and MCP tools share the same player actions. Start with BDS 1.26.52 (protocol 2193). See [ROADMAP.md](ROADMAP.md) for the release scope.

## Build and test

Use Python 3.12+ and install the native wheel in the [quickstart](README.md#try-it-offline). From the repository root:

```sh
python -m pip install -e ".[nethernet]"
python -m unittest discover -s tests -v
python examples/network_settings.py
```

The NetherNet test uses a loopback peer. Verify gameplay changes against BDS with Bedrock Testbed. Report the version, transport and observed result. Confirm a server crash from the server process or its logs.

## Source guide

- `src/endpilot/protocol.py` owns batch boundaries, packet headers and dispatch to the upstream nanobind codec. Preserve typed and raw access for extensions.
- `src/endpilot/transport/` opens message connections. Keep transport details out of gameplay.
- `src/endpilot/login.py` handles the opening game exchange. Keep later gameplay state with the feature that updates it.
- `examples/` contains runnable user scripts. Follow the closest example from its public entry point to the wire when changing behavior.

## Implementation

- Keep Python as the default. Reuse upstream packet types. Move measured hotspots to C++ only after accounting for copies, binding calls and event-loop time.
- Keep a player action, its state and its completion handling together. Delegate from the public Bot entry point. Use short comments for protocol details a reader cannot infer.
- Specify what an action returns: input accepted, server response received, or effect observed. Track item requests by ID and affected slots. Take historical snapshots on demand.
- Require only the state an operation uses. Keep server identity pinning optional. Before adding a guard, identify the failure it prevents and which valid calls it would reject.
- Before release, change APIs and migrate known callers together. Add tests for observable behavior and reproducible failures; reuse existing coverage when changing internals.

## Git and documentation

Use an imperative subject such as `feat(inventory): track item requests by slot` or `fix(login): apply compression after negotiation`. Omit the scope for project-wide changes. Preserve human attribution; omit AI co-author trailers.

Keep examples, fixtures and current instructions in Git. Put investigation notes in the issue or PR and attach raw test output there. At release, summarize user-visible changes.
