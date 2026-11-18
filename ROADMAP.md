# Roadmap

## First usable client

1. **Walk on BDS 1.26.52.** One Python example connects, spawns, walks and exits. Verify the player's position on the server. Run it over NetherNet first, then RakNet with only the endpoint and transport changed.
2. **Test an Endstone change.** Have a plugin cancel a block placement, then allow it. Check the client view, server event and final block in Bedrock Testbed. Expose the same SDK action through MCP so an AI can explore a change and leave a Python reproduction that runs without a model.

## Before the first public release

- **Cover most basic Mineflayer use cases on Bedrock.** Track gaps by player task. Start with finding a tree, walking to it, gathering wood, crafting a tool and storing it. Extend to building, smelting, combat, chat and villager trading. Exercise cancellation and switching targets on an ordinary server.
- **Try an adjacent protocol early.** As soon as inventory works on 2193, repeat “open chest, take tool, equip it” on the next target version. Record which actions and transports passed for each tested version.
- **Measure real load.** Record single-bot input timing from the first walk. Compare 1, 10 and 50 bots over NetherNet on the same BDS workload, checking server-received input and the slowest bot. Repeat with world and entity tracking enabled.
- **Try an external extension.** Implement an unwrapped interaction, such as answering a server form, in a separate Python package using public packet APIs.

Use the SDK for Endstone regressions throughout development. Make the first release once most basic player workflows work through the public SDK and remaining gaps are documented.
