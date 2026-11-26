# Working on EndPilot

## Project

EndPilot is a Python SDK for headless Bedrock players. Scripts and MCP tools share the same player actions. The SDK targets protocol 2225 (Minecraft Bedrock 1.26.6x).

## Build and test

Use Python 3.12 or later. Follow the [installation guide](README.md#install) for the native codec and transport dependencies, then run these commands from the repository root.

```sh
python -m pip install -e ".[nethernet,raknet]"
python -m unittest discover -s tests -v
python examples/network_settings.py
```

The NetherNet test uses a loopback peer. Verify gameplay changes on a compatible Bedrock Dedicated Server (BDS) and report the version, transport and observed result. Confirm a server crash from the server process or its logs.

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
- Change unpublished APIs and migrate known callers together. Add tests for observable behavior and reproducible failures; reuse existing coverage when changing internals.

## Git and documentation

Base development branches on `main`. Use separate Git worktrees when tasks need isolated checkouts.

Use an imperative subject such as `feat(inventory): track item requests by slot` or `fix(login): apply compression after negotiation`. Omit the scope for project-wide changes. Preserve human attribution; omit AI co-author trailers.

Rebase a PR when its commits are independently useful. Squash a single change developed through successive fixes, using an imperative subject. Use one of these two methods for routine PRs.

Keep examples, fixtures and current instructions in Git. Put investigation notes in the issue or PR and attach raw test output there. Summarize user-visible changes in relevant PRs and examples.
