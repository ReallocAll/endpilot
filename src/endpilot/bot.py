"""Public player entry point; game state delegates to the relevant feature."""

from .authentication import OfflineIdentity
from .blocks import Blocks, BlockObservation, BlockUpdate
from .chat import Chat, ChatMessage
from .commands import Commands
from .containers import Containers, ContainerWindow
from .entities import Entities, EntityMovement, EntitySpawn, EntityState
from .inventory import Inventory
from .login import JoinSession
from .movement import Movement
from .player_state import PlayerState
from .recipes import Recipe, Recipes
from .trading import Trading, TradeOffers
from .protocol import Protocol


class Bot:
    def __init__(self, name: str, protocol: int = 2193):
        self.identity = OfflineIdentity(name)
        self.protocol = Protocol(protocol)
        self._session: JoinSession | None = None
        self._movement: Movement | None = None
        self._inventory: Inventory | None = None
        self._containers: Containers | None = None
        self._blocks: Blocks | None = None
        self._chat: Chat | None = None
        self._commands: Commands | None = None
        self._entities: Entities | None = None
        self._player_state: PlayerState | None = None
        self._trading: Trading | None = None
        self._recipes: Recipes | None = None

    async def join_nethernet(self, url: str, *, timeout: float = 30, server_key_sha256: str | None = None) -> None:
        if self._session is not None:
            raise RuntimeError("player is already connected")
        session = await JoinSession.connect_nethernet(
            url, self.identity, self.protocol, timeout, server_key_sha256=server_key_sha256
        )
        self._start_session(session)

    async def join_raknet(self, host: str, port: int = 19132, *, timeout: float = 30) -> None:
        if self._session is not None:
            raise RuntimeError("player is already connected")
        session = await JoinSession.connect_raknet(host, port, self.identity, self.protocol, timeout)
        self._start_session(session)

    def _start_session(self, session: JoinSession) -> None:
        self._movement = Movement(session.packets, session.player_id)
        self._inventory = Inventory(session.packets, session.player_id)
        for raw in session.inventory_packets:
            self._inventory._on_packet(raw)
        session.inventory_packets.clear()
        self._containers = Containers(session.packets, self._movement, self._inventory)
        self._blocks = Blocks(
            session.packets, self._movement, self._inventory,
            hashed_runtime_ids=session.block_network_ids_are_hashes,
        )
        self._chat = Chat(session.packets, self.identity.name)
        self._commands = Commands(session.packets, self.identity.uuid, session.player_unique_id)
        self._entities = Entities(session.packets, item_names=session.item_names)
        self._player_state = PlayerState(session.packets, session.player_id)
        self._recipes = Recipes(
            session.packets, session.item_names, session.crafting_data_packets,
            inventory=self._inventory, containers=self._containers,
        )
        session.crafting_data_packets.clear()
        self._trading = Trading(
            session.packets, self._entities, self._movement, self._inventory, self._containers,
            item_names=session.item_names,
        )
        self._session = session
        self._entities.start()
        for raw in session.entity_packets:
            self._entities._on_packet(raw)
        session.entity_packets.clear()
        self._blocks.start()
        self._movement.start()

    @property
    def recipes(self) -> tuple[Recipe, ...]:
        """Current server-issued recipes, not locally assumed vanilla recipes."""
        if self._recipes is None:
            raise RuntimeError("player has not joined")
        return self._recipes.snapshot()

    def get_recipe(self, recipe_id: str | int) -> Recipe | None:
        """Find a server-issued recipe by string ID or network ID."""
        if self._recipes is None:
            raise RuntimeError("player has not joined")
        return self._recipes.get(recipe_id)

    async def wait_for_recipes(self, *, timeout: float = 15) -> tuple[Recipe, ...]:
        """Wait for the first CraftingData from BDS, which may be an empty catalog."""
        if self._recipes is None:
            raise RuntimeError("player has not joined")
        return await self._recipes.wait_loaded(timeout=timeout)

    async def craft_recipe_auto(
        self, recipe_id: str | int, destination_slot: int, *,
        times: int = 1, timeout: float = 15,
    ):
        """Craft using the open player-inventory screen; return the BDS verdict.

        Open the inventory screen first with open_inventory_screen(). BDS 1.26.52
        rejects a recipe-book request without that active window.
        """
        if self._recipes is None:
            raise RuntimeError("player has not joined")
        return await self._recipes.craft_auto(
            recipe_id, destination_slot, times=times, timeout=timeout,
        )

    async def stage_crafting_ingredient(self, source_slot: int, grid_slot: int = 28,
                                        amount: int = 1, *, timeout: float = 15):
        """Place inventory material in the active 2x2 or workbench 3x3 grid."""
        if self._recipes is None:
            raise RuntimeError("player has not joined")
        return await self._recipes.stage_grid(source_slot, grid_slot, amount, timeout=timeout)

    async def craft_recipe_grid(self, recipe_id: str | int, destination_slot: int, *,
                                times: int = 1, timeout: float = 15):
        """Craft from server-observed grid inputs; return BDS's item-stack verdict."""
        if self._recipes is None:
            raise RuntimeError("player has not joined")
        return await self._recipes.craft_grid(recipe_id, destination_slot,
                                              times=times, timeout=timeout)

    @property
    def player_id(self) -> int:
        if self._session is None or self._session.player_id is None:
            raise RuntimeError("player has not joined")
        return self._session.player_id

    @property
    def position(self) -> tuple[float, float, float] | None:
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return self._movement.position

    async def wait_for_position_sync(self, timeout: float = 15) -> tuple[float, float, float]:
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.wait_for_position_sync(timeout)

    async def look_at(self, target: tuple[float, float, float]) -> tuple[float, float]:
        """Aim toward a world-space eye-height target on subsequent input frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.look_at(target)

    async def walk_forward(self, ticks: int = 24) -> int:
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.walk_forward(ticks)

    async def walk_backward(self, ticks: int = 24) -> int:
        """Walk opposite the facing direction; return submitted frames, not BDS acceptance."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.walk_backward(ticks)

    async def strafe_left(self, ticks: int = 24) -> int:
        """Move left without turning; return submitted frames, not BDS acceptance."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.strafe_left(ticks)

    async def strafe_right(self, ticks: int = 24) -> int:
        """Move right without turning; return submitted frames, not BDS acceptance."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.strafe_right(ticks)

    async def walk_forward_left(self, ticks: int = 24) -> int:
        """Move diagonally forward-left; return submitted frames, not BDS acceptance."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.walk_forward_left(ticks)

    async def walk_forward_right(self, ticks: int = 24) -> int:
        """Move diagonally forward-right; return submitted frames, not BDS acceptance."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.walk_forward_right(ticks)

    async def walk_backward_left(self, ticks: int = 24) -> int:
        """Move diagonally backward-left; return submitted frames, not BDS acceptance."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.walk_backward_left(ticks)

    async def walk_backward_right(self, ticks: int = 24) -> int:
        """Move diagonally backward-right; return submitted frames, not BDS acceptance."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.walk_backward_right(ticks)

    async def set_sneaking(self, sneaking: bool) -> int:
        """Submit the held sneak state and return the client tick, not BDS acceptance."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.set_sneaking(sneaking)

    async def set_sprinting(self, sprinting: bool) -> int:
        """Hold the sprint key for later forward input; return a submitted tick."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.set_sprinting(sprinting)

    async def jump(self) -> int:
        """Submit a jump press; return its tick, not confirmed server movement."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.jump()

    async def jump_forward(self, ticks: int = 24) -> int:
        """Jump once during forward walking; return submitted moving frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.jump_forward(ticks)

    async def jump_backward(self, ticks: int = 24) -> int:
        """Jump once during backward walking; return submitted moving frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.jump_backward(ticks)

    async def jump_left(self, ticks: int = 24) -> int:
        """Jump once while strafing left; return submitted moving frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.jump_left(ticks)

    async def jump_right(self, ticks: int = 24) -> int:
        """Jump once while strafing right; return submitted moving frames."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._movement.jump_right(ticks)

    async def stop_moving(self) -> None:
        """Stop an active walk/follow, waiting for its input to be released."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        await self._movement.stop_moving()

    async def follow_entity(self, runtime_id: int, *, ticks: int = 80,
                            stop_distance: float = 1.5) -> int:
        """Follow the latest server-observed actor position for a bounded time."""
        if self._movement is None or self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._movement.follow_entity(
            self._entities, runtime_id, ticks=ticks, stop_distance=stop_distance
        )

    def switch_follow_target(self, runtime_id: int) -> None:
        """Redirect an active follow to another actor on the next input frame."""
        if self._movement is None:
            raise RuntimeError("player has not joined")
        self._movement.switch_follow_target(runtime_id)

    async def navigate_to(self, target: tuple[float, float, float], *,
                          ticks: int = 160, search_radius: int = 8) -> int:
        """Submit a flat route through observed blocks; return moving frames."""
        if self._movement is None or self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._movement.navigate_to(
            self._blocks, target, ticks=ticks, search_radius=search_radius
        )

    async def wait_for_block_update(
        self, position: tuple[int, int, int], *, layer: int = 0, timeout: float = 15
    ) -> BlockUpdate:
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._blocks.wait_for_update(position, layer=layer, timeout=timeout)

    def observed_block(
        self, position: tuple[int, int, int], *, layer: int = 0
    ) -> BlockObservation | None:
        """Last server-supplied block, or None until that position is known."""
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return self._blocks.observed_block(position, layer=layer)

    async def request_sub_chunk(self, position: tuple[int, int, int]) -> None:
        """Ask BDS for the containing section; inspect observed_block afterwards."""
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        await self._blocks.request_sub_chunk(position)

    def inventory_count(self, item_id: int) -> int:
        """Current server-described count of a numeric inventory item ID."""
        if self._inventory is None:
            raise RuntimeError("player has not joined")
        return self._inventory.count_item(item_id)

    async def wait_for_inventory_item_count(
        self, item_id: int, minimum: int, *, timeout: float = 15
    ) -> int:
        if self._inventory is None:
            raise RuntimeError("player has not joined")
        return await self._inventory.wait_for_item_count(
            item_id, minimum, timeout=timeout
        )

    async def select_hotbar(self, slot: int) -> None:
        """Request a hotbar slot; observe BDS to confirm the equipped item."""
        if self._inventory is None:
            raise RuntimeError("player has not joined")
        await self._inventory.select_hotbar(slot)


    @property
    def inventory_slots(self) -> dict[int, object]:
        """Latest server-described player inventory slots."""
        if self._inventory is None:
            raise RuntimeError("bot is not joined")
        return self._inventory.slots

    @property
    def armor_slots(self) -> dict[int, object]:
        """Latest server-described armor slots; absent slots are not confirmed empty."""
        if self._inventory is None:
            raise RuntimeError("bot is not joined")
        return self._inventory.armor_slots

    async def equip_armor(self, source_slot: int, armor_slot: int, *, timeout: float = 15):
        """Return the server's item-stack verdict for an inventory/armor swap."""
        if self._inventory is None:
            raise RuntimeError("bot is not joined")
        return await self._inventory.equip_armor(source_slot, armor_slot, timeout=timeout)


    async def unequip_armor(self, armor_slot: int, destination_slot: int, *, timeout: float = 15):
        """Take equipped armor back into inventory; return BDS's stack verdict."""
        if self._inventory is None:
            raise RuntimeError("bot is not joined")
        return await self._inventory.unequip_armor(
            armor_slot, destination_slot, timeout=timeout,
        )


    async def drop_inventory_item(self, slot: int, amount: int = 1, *, timeout: float = 15):
        """Return BDS's verdict for dropping an observed inventory stack."""
        if self._inventory is None:
            raise RuntimeError("player has not joined")
        return await self._inventory.drop_item(slot, amount, timeout=timeout)

    async def use_item(self) -> None:
        """Use the selected item in air; await BDS observations to confirm its effect."""
        if self._inventory is None or self._movement is None:
            raise RuntimeError("player has not joined")
        await self._inventory.use_item(self._movement)

    @property
    def open_window(self) -> ContainerWindow | None:
        """Latest server-opened window, or None if closed or not yet opened."""
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return self._containers.window

    @property
    def container_slots(self) -> dict[int, object] | None:
        """Current window slots from BDS; None until initial contents arrive."""
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return self._containers.slots

    async def wait_for_container_slots(self, *, timeout: float = 15) -> dict[int, object]:
        """Wait for server-reported slots of the currently open container."""
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return await self._containers.wait_for_slots(timeout=timeout)

    async def take_from_container(
        self, source_slot: int, destination_slot: int, amount: int = 1, *, timeout: float = 15
    ):
        """Return the server's item-stack verdict; response slots update observed state."""
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return await self._containers.take_from_container(
            source_slot, destination_slot, amount, timeout=timeout
        )

    async def move_inventory_item(
        self, source_slot: int, destination_slot: int, amount: int = 1, *, timeout: float = 15
    ):
        """Split or merge a player stack and return the matching BDS verdict."""
        if self._inventory is None:
            raise RuntimeError("player has not joined")
        return await self._inventory.move_item(
            source_slot, destination_slot, amount, timeout=timeout
        )

    async def swap_inventory_items(
        self, source_slot: int, destination_slot: int, *, timeout: float = 15
    ):
        """Swap player slots; return the matching BDS item-stack verdict."""
        if self._inventory is None:
            raise RuntimeError("player has not joined")
        return await self._inventory.swap_items(
            source_slot, destination_slot, timeout=timeout
        )

    async def put_into_container(
        self, source_slot: int, destination_slot: int, amount: int = 1, *, timeout: float = 15
    ):
        """Return the server's verdict for inventory-to-container transfer."""
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return await self._containers.put_into_container(
            source_slot, destination_slot, amount, timeout=timeout
        )

    async def open_inventory_screen(self, *, timeout: float = 15) -> ContainerWindow:
        """Open the player inventory UI and await its server-created window."""
        if self._containers is None:
            raise RuntimeError("bot is not joined")
        return await self._containers.open_inventory_screen(timeout=timeout)

    async def close_container(self, *, timeout: float = 15) -> ContainerWindow | None:
        """Close the active window; wait for the matching server close packet."""
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return await self._containers.close_window(timeout=timeout)

    async def wait_for_container_close(self, *, timeout: float = 15):
        """Observe the active window closing; return BDS's typed close packet.

        Does not send a close request. Arm before a possible server-initiated close.
        """
        if self._containers is None:
            raise RuntimeError("player has not joined")
        return await self._containers.wait_for_close(timeout=timeout)

    async def open_container(
        self, position: tuple[int, int, int], *, face: int = 1, timeout: float = 15
    ) -> ContainerWindow:
        """Use a block and wait for the server's container-open response."""
        if self._containers is None or self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._containers.open(position, self._blocks, face=face, timeout=timeout)

    async def mine_block(
        self, position: tuple[int, int, int], *, face: int = 1, timeout: float = 15
    ) -> BlockUpdate:
        """Mine until a server block update, not merely input submission."""
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._blocks.mine_block(position, face=face, timeout=timeout)

    async def place_against(self, against: BlockUpdate, *, face: int = 1, slot: int = 0) -> int:
        if self._blocks is None:
            raise RuntimeError("player has not joined")
        return await self._blocks.place_against(against, face=face, slot=slot)

    async def send_chat(self, message: str) -> None:
        """Submit a chat packet; server delivery is not implied."""
        if self._chat is None:
            raise RuntimeError("player has not joined")
        await self._chat.send(message)

    async def wait_for_chat(self, *, author: str | None = None, timeout: float = 15) -> ChatMessage:
        """Wait for a new player chat relayed by the server."""
        if self._chat is None:
            raise RuntimeError("player has not joined")
        return await self._chat.wait_for_message(author=author, timeout=timeout)

    async def execute_command(self, command: str, *, timeout: float = 15):
        """Submit a player command and observe the next command-style server text.

        The text has no request ID and does not prove the command took effect.
        """
        if self._commands is None:
            raise RuntimeError("player has not joined")
        return await self._commands.execute(command, timeout=timeout)

    @property
    def health(self) -> float | None:
        """Latest server-reported health; None until a health packet arrives."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return self._player_state.health

    async def wait_for_health_update(self, timeout: float = 15) -> float:
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return await self._player_state.wait_for_health_update(timeout)

    @property
    def hunger(self) -> float | None:
        """Latest server-reported food level, or None until observed."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return self._player_state.hunger

    async def wait_for_hunger_update(self, timeout: float = 15) -> float:
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return await self._player_state.wait_for_hunger_update(timeout)

    async def respawn(self, *, timeout: float = 15) -> tuple[float, float, float]:
        """Request respawn and acknowledge the server-provided spawn position."""
        if self._player_state is None:
            raise RuntimeError("player has not joined")
        return await self._player_state.respawn(timeout=timeout)

    @property
    def observed_entities(self) -> tuple[EntityState, ...]:
        """Last observed AddActor states, not a complete scan of loaded entities."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.snapshot()

    @property
    def observed_drops(self) -> tuple[EntityState, ...]:
        """Dropped items visible on this player stream, not all world drops."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return self._entities.drops()

    async def attack_entity(self, runtime_id: int, *, hit_height: float = 0.5) -> None:
        """Submit a held-item attack; server damage or removal must be observed separately."""
        if self._entities is None or self._movement is None or self._inventory is None:
            raise RuntimeError("player has not joined")
        await self._entities.attack(self._movement, self._inventory, runtime_id,
                                    hit_height=hit_height)

    async def interact_entity(self, runtime_id: int, *, hit_height: float = 0.5) -> None:
        """Submit a right-click on an actor; observe server events to confirm its effect."""
        if self._entities is None or self._movement is None or self._inventory is None:
            raise RuntimeError("player has not joined")
        await self._entities.interact(self._movement, self._inventory, runtime_id,
                                      hit_height=hit_height)

    async def open_trade(self, runtime_id: int, *, timeout: float = 15) -> TradeOffers:
        """Interact with an observed actor and return its server-issued trade offers."""
        if self._trading is None:
            raise RuntimeError("player has not joined")
        return await self._trading.open(runtime_id, timeout=timeout)

    async def execute_trade(self, recipe_net_id: int, destination_slot: int, *, timeout: float = 15):
        """Submit a quoted villager exchange and await the matching server response."""
        if self._trading is None:
            raise RuntimeError("player has not joined")
        return await self._trading.exchange(recipe_net_id, destination_slot, timeout=timeout)

    async def stage_trade_ingredient(
        self, source_slot: int, amount: int, *, ingredient: int = 1,
        timeout: float = 15,
    ):
        """Place an inventory stack into an open trade input; await BDS's response."""
        if self._trading is None:
            raise RuntimeError("player has not joined")
        return await self._trading.stage_ingredient(
            source_slot, amount, ingredient=ingredient, timeout=timeout
        )

    async def unstage_trade_ingredient(
        self, destination_slot: int, *, ingredient: int = 1, timeout: float = 15
    ):
        """Return a staged trade input to inventory; await the BDS stack response."""
        if self._trading is None:
            raise RuntimeError("player has not joined")
        return await self._trading.unstage_ingredient(
            destination_slot, ingredient=ingredient, timeout=timeout
        )

    async def collect_drop(
        self, runtime_id: int, *, ticks: int = 120, timeout: float = 15
    ) -> EntityState:
        """Approach an observed dropped item; confirm this player picked it up."""
        if self._entities is None or self._movement is None:
            raise RuntimeError("player has not joined")
        return await self._entities.collect_drop(
            self._movement, runtime_id, self.player_id, ticks=ticks, timeout=timeout
        )

    async def wait_for_item_drop(self, timeout: float = 15) -> EntityState:
        """Observe the next item actor spawn sent by the server."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_item_drop(timeout)

    async def wait_for_entity_spawn(
        self, *, actor_type: str | None = None, timeout: float = 15
    ) -> EntitySpawn:
        """Observe the next server actor spawn; not a scan of all world entities."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_spawn(actor_type=actor_type, timeout=timeout)

    async def wait_for_entity_removal(self, entity_id: int, *, timeout: float = 15) -> None:
        """Observe the server removing the actor with this unique ID from view."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        await self._entities.wait_for_removal(entity_id, timeout=timeout)

    async def wait_for_entity_movement(
        self, runtime_id: int, *, timeout: float = 15
    ) -> EntityMovement:
        """Observe one actor movement event, not a continuously tracked position."""
        if self._entities is None:
            raise RuntimeError("player has not joined")
        return await self._entities.wait_for_movement(runtime_id, timeout=timeout)

    async def wait_for_server_disconnect(self, timeout: float = 15):
        if self._session is None:
            raise RuntimeError("player has not joined")
        return await self._session.wait_for_server_disconnect(timeout)

    @property
    def packets(self):
        """Expose typed and raw packet I/O for external extensions."""
        if self._session is None:
            raise RuntimeError("player has not joined")
        return self._session.packets

    async def close(self) -> None:
        session, self._session = self._session, None
        movement, self._movement = self._movement, None
        inventory, self._inventory = self._inventory, None
        containers, self._containers = self._containers, None
        blocks, self._blocks = self._blocks, None
        if blocks is not None:
            blocks.close()
        if containers is not None:
            containers.close()
        self._chat = None
        self._commands = None
        entities, self._entities = self._entities, None
        player_state, self._player_state = self._player_state, None
        self._trading = None
        recipes, self._recipes = self._recipes, None
        if recipes is not None:
            recipes.close()
        if player_state is not None:
            player_state.close()
        if entities is not None:
            entities.close()
        if movement is not None:
            movement.close()
        if inventory is not None:
            inventory.close()
        if session is not None:
            await session.close()
