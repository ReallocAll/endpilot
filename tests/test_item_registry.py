import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from endpilot import Bot, ItemRegistrySnapshot
from endpilot.protocol import write_batch

from test_scheduled_input import Wire


class ItemRegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Bot("ItemRegistry", protocol=2225)
        self.b = self.bot.protocol.binding
        self.addAsyncCleanup(self.bot.close)

    def registry(self, *entries):
        return self.b.ItemRegistryPacket(items=[self.b.ItemData(id=key, name=name) for key, name in entries])

    def drop(self, runtime_id):
        b = self.b
        return b.AddItemActorPacket(
            id=runtime_id + 1000, runtime_id=runtime_id, pos=b.Vec3(x=5, y=70, z=4),
            item=b.SerializedNetworkItemStackDescriptor(id=31, stack_size=1),
        )

    def push_batch(self, *packets):
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in packets))

    async def join(self, *early, after_spawn=(), transport="nethernet"):
        self.wire = Wire(self.bot.protocol)
        b = self.b
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.push_batch(b.StartGamePacket(runtime_id=13, entity_id=27), *early,
                        b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN), *after_spawn)
        with patch(f"endpilot.transport.{transport}.connect", return_value=self.wire):
            if transport == "nethernet":
                await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)
            else:
                await self.bot.join_raknet("127.0.0.1", timeout=1)

    async def deliver(self, *packets):
        barrier = self.b.SetTimePacket(time=71)
        pending = asyncio.create_task(self.bot.packets.wait_for_packet(barrier.ID, timeout=1))
        await asyncio.sleep(0)
        self.push_batch(*packets, barrier)
        await pending

    async def test_exact_ids_names_and_immutable_snapshot(self):
        self.assertIsNone(self.bot.item_registry)
        initial = {6: "custom:initial"}
        independent = ItemRegistrySnapshot(initial)
        initial[6] = "custom:changed"
        self.assertEqual(independent.by_id, {6: "custom:initial"})

        await self.join(
            self.registry((7, "custom:removed")),
            self.registry((9, "custom:old"), (-23, "custom:Shared"), (9, "custom:Shared")),
        )
        registry = self.bot.item_registry
        self.assertEqual(list(registry.by_id.items()), [(9, "custom:Shared"), (-23, "custom:Shared")])
        self.assertIsNone(registry.name_for_id(7))
        self.assertIsNone(registry.name_for_id("-23"))
        self.assertEqual(registry.name_for_id(-23), "custom:Shared")
        self.assertEqual(registry.id_for_name("custom:Shared"), 9)
        self.assertIsNone(registry.id_for_name("custom:shared"))
        self.assertIsNone(registry.id_for_name("Shared"))
        with self.assertRaises(TypeError):
            registry.by_id[9] = "custom:changed"
        with self.assertRaises(FrozenInstanceError):
            registry.by_id = {}

    async def test_batches_preserve_registry_order_and_derived_results(self):
        b = self.b
        item = b.SerializedNetworkItemInstanceDescriptor(id=31, stack_size=1)
        content = b.CreativeContentPacket(entries=[
            b.CreativeItemEntryPayload(creative_item_net_id=8, group_index=0, item_descriptor=item)
        ])
        recipe = b.CraftingDataPacket(shapeless_recipes=[b.ShapelessRecipePayload(
            recipe_id="custom:recipe", net_id=7, tag="crafting_table", results=[item],
            ingredients=[b.SerializedRecipeIngredient(descriptor={"name": "custom:input"}, stack_size=1)],
        )])
        await self.join(content, recipe, self.registry((31, "custom:one")),
                        after_spawn=(self.registry((31, "custom:two")),))
        first = self.bot.item_registry
        first_creative = self.bot.creative_catalog
        first_recipe = self.bot.get_recipe("custom:recipe")
        self.assertEqual(first.name_for_id(31), "custom:one")
        self.assertEqual(first_creative.entries[0].item.name, "custom:one")
        self.assertEqual(first_recipe.results[0].name, "custom:one")

        observed = []
        def record(raw):
            if raw.packet_id == b.ItemRegistryPacket.ID:
                observed.append((self.bot.item_registry, self.bot.creative_catalog,
                                 self.bot.get_recipe("custom:recipe"), self.bot.get_recipe(7)))
        unsubscribe = self.bot.packets.subscribe_raw(record)
        try:
            await self.deliver(self.registry(), self.registry((31, "custom:three")))
        finally:
            unsubscribe()
        self.assertEqual(len(observed), 3)
        for (snapshot, catalog, named, network), expected in zip(observed, ("custom:two", None, "custom:three")):
            self.assertEqual(snapshot.name_for_id(31), expected)
            self.assertEqual(catalog.entries[0].item.name, expected)
            self.assertEqual(named.results[0].name, expected)
            self.assertIs(named, network)
        self.assertEqual(dict(observed[1][0].by_id), {})
        self.assertEqual(self.bot.recipes_all("custom:three"), (observed[-1][2],))
        self.assertEqual(self.bot.recipes_all("custom:one"), ())
        self.assertEqual(first.name_for_id(31), "custom:one")
        self.assertEqual(first_creative.entries[0].item.name, "custom:one")
        self.assertEqual(first_recipe.results[0].name, "custom:one")

    async def test_dimension_change_and_disconnect_rejoin(self):
        b = self.b
        await self.join()
        self.assertIsNone(self.bot.item_registry)
        await self.deliver(self.registry((2, "custom:original")))
        registry = self.bot.item_registry
        await self.deliver(b.ChangeDimensionPacket(dimension_id=1, pos=b.Vec3(x=1, y=70, z=2)))
        self.assertIs(self.bot.item_registry, registry)
        await self.deliver(b.DisconnectPacket(), self.registry((4, "custom:discarded")))
        self.assertIsNone(self.bot.item_registry)
        await self.bot.close()
        self.assertEqual(registry.by_id, {2: "custom:original"})
        await self.join()
        self.assertIsNone(self.bot.item_registry)
        await self.deliver(self.registry())
        self.assertEqual(self.bot.item_registry.by_id, {})
        await self.bot.close()
        self.assertIsNone(self.bot.item_registry)

    async def test_transport_failure_clears_registry_before_reader_ends(self):
        await self.join(self.registry((2, "custom:observed")))
        registry = self.bot.item_registry
        ended = asyncio.Event()
        self.bot.packets.subscribe_reader_end(lambda _cause: ended.set())
        self.wire.incoming.put_nowait(ConnectionError("transport lost"))
        await asyncio.wait_for(ended.wait(), timeout=1)
        self.assertIsNone(self.bot.item_registry)
        self.assertEqual(registry.by_id, {2: "custom:observed"})
        with self.assertRaisesRegex(ConnectionError, "transport lost"):
            await self.bot.close()

    async def test_startup_drops_use_the_registry_at_their_wire_position(self):
        for transport in ("nethernet", "raknet"):
            with self.subTest(transport=transport):
                try:
                    await self.join(
                        self.drop(110), self.registry((31, "custom:early")), self.drop(111),
                        self.registry((31, "custom:late")), self.drop(112), transport=transport,
                    )
                    self.assertEqual(self.bot.item_registry.by_id, {31: "custom:late"})
                    names = {drop.runtime_id: drop.item_id for drop in self.bot.observed_drops}
                    self.assertEqual(names, {110: "#31", 111: "custom:early", 112: "custom:late"})
                finally:
                    await self.bot.close()

    async def test_live_registry_replacement_does_not_relabel_drops_in_the_same_batch(self):
        await self.join()
        history = []

        def remember(raw):
            if raw.packet_id == self.b.AddItemActorPacket.ID:
                history.append((self.bot.item_registry, self.bot.observed_drops[-1]))

        unsubscribe = self.bot.packets.subscribe_raw(remember)
        try:
            await self.deliver(
                self.registry((31, "custom:early")), self.drop(111),
                self.registry((31, "custom:late")), self.drop(112),
            )
        finally:
            unsubscribe()
        self.assertEqual(
            [(snapshot.name_for_id(31), drop.runtime_id, drop.item_id) for snapshot, drop in history],
            [("custom:early", 111, "custom:early"), ("custom:late", 112, "custom:late")],
        )
        self.assertEqual(self.bot.observed_drops, tuple(drop for _, drop in history))

    async def test_disconnect_ignores_late_registry_and_drop_from_the_same_batch(self):
        await self.join(self.registry((31, "custom:early")), self.drop(114))
        snapshot = self.bot.item_registry
        await self.deliver(self.b.DisconnectPacket(), self.registry((31, "custom:late")), self.drop(115))
        self.assertIsNone(self.bot.item_registry)
        self.assertEqual(snapshot.by_id, {31: "custom:early"})
        self.assertEqual(
            [(drop.runtime_id, drop.item_id) for drop in self.bot.observed_drops],
            [(114, "custom:early")],
        )
