# Working on EndPilot

- Start with the player action or contributor task. Keep its state, packet handling and completion logic together. A convenient `Bot` entry point can delegate to gameplay modules.
- Write gameplay in Python. Use the upstream nanobind types and codecs directly. Move a hot path to C++ when profiling a representative workload identifies it; include conversion costs in the comparison.
- Give common actions a high-level SDK and extensions access to typed and raw packets. Build MCP tools on the same SDK. Keep AI planning and test-server orchestration in their callers.
- Define what each action completes: input queued, server response received, or world change observed. Scope inventory prediction and waits to affected slots. Capture historical state only when a caller needs it.
- Add a guard for a concrete protocol constraint or demonstrated failure. Check which valid actions it would reject. Server identity pinning is opt-in. Permit callers to try installed protocol bindings beyond the tested combinations.
- Before the first release, change an awkward API and migrate known callers together. Add compatibility aliases or API versions only for an actual compatibility commitment.
- Test observable behavior and credible regressions. Reuse an existing scenario when it covers the change. Use real BDS for server behavior; a client disconnect alone cannot establish a server crash.
- Keep runnable examples, reusable fixtures and current instructions in Git. Put decisions specific to a change in its issue or PR; attach logs, profiles and run output there. Write each document for the person who will use it.
