# Road to the first public release

EndPilot targets Bedrock players controlled by Python scripts or AI. Today the SDK handles typed and raw packets, uncompressed batches and the opening NetherNet settings exchange. The milestones below describe planned work.

## 1. Connect a player

- Target BDS 1.26.52 (protocol 2193). Complete negotiation, login, resource-pack handling, spawn, periodic input, position correction and disconnect.
- Run the same walk example on NetherNet and RakNet. Change only connection settings and verify the final position on BDS. Prioritize NetherNet for capacity work.
- Support optional server identity pinning. A supplied pin must match; ordinary connections need no preconfigured server key.

## 2. Put EndPilot to work on Endstone

- Verify a plugin that cancels one block placement and allows the next. Check the server event, final block and client view. Use Testbed for world setup, process status and logs.
- Expose the same SDK action through MCP. Give an agent a real Endstone change to explore and save its shortest reproducible scenario as a standalone Python script.
- Distinguish client disconnect, server refusal, timeout and confirmed server crash. Reuse the scenario for subsequent Endstone version upgrades.

## 3. Build the player SDK through ordinary survival tasks

| Capability | Representative workflow |
| --- | --- |
| World and entities | Inspect loaded blocks, nearby entities, drops and player state. Distinguish unloaded terrain from air. |
| Movement and navigation | Walk, look, jump, navigate around an obstacle, follow a moving target and stop or switch targets. Handle server corrections. |
| Gathering and building | Equip a tool, mine a block, collect its drop and place blocks from inventory. |
| Inventory and containers | Open a chest, split or transfer a stack, equip a tool and close the window. Reconcile prediction with server responses while unrelated actions continue. |
| Crafting and smelting | Gather wood, craft tools, obtain fuel, smelt materials and store the output. |
| Entities and survival | Interact, attack, use food, handle death and resume after respawn. Approach a villager, read offers, trade and check inventory. |
| Communication | Receive and send chat, run an allowed command and observe the result. |

Exercise these through public Python calls on an ordinary server. Use Mineflayer's basic player workflows to track remaining gaps. Prefer native Python integrations; adopt a cross-language dependency only for a concrete benefit.

## 4. Exercise extension, AI and compatibility paths

- From an external package, subscribe by packet ID and exchange typed or raw data for an unwrapped feature such as server forms. Use the normal connection and expose the resulting Python operation to MCP.
- Let AI inspect nearby state, start a long action, observe progress, cancel it and hand control back to a script. Keep planning outside the client. After an interrupted trade, inspect current state before deciding whether to retry.
- Once inventory works on 2193, repeat chest → tool → equip on an adjacent protocol. Keep adapters local to actual differences. Allow low-level use of installed upstream bindings and document the gameplay and transport combinations verified on BDS.

## 5. Measure capacity as features land

Record single-bot timing at the first walk. After the first Endstone regression, compare 1, 10 and 50 NetherNet bots on the same local BDS workload. Measure server-received inputs, per-bot tail latency, queue growth, CPU and memory. Repeat with entity and chunk tracking enabled. Profile a reproducible bottleneck before moving work into a native kernel. Explore larger populations as actual workloads justify them.

## 6. Publish the first release

- Review coverage against most basic Mineflayer workflows. Link working examples and document remaining gaps, supported BDS versions and measured capacity.
- Use the SDK for real Endstone regressions and ordinary gameplay before committing to API compatibility. Exercise a version upgrade and an external extension.
- Have a new contributor install from a clean environment, run an example and verify a small change using the contribution guide.
- Build and install the release distribution on supported platforms, verify its native dependency is obtainable, and rerun representative BDS scenarios from the installed package. Publish the package, tag the tested commit and publish user-facing release notes.
