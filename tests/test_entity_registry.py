import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from bedrock_protocol import nbt

from endpilot import Bot, EntityRegistryEntry, EntityRegistrySnapshot, VanillaEntityDimensions, vanilla_entity_dimensions
from endpilot.protocol import RawPacket, write_batch

from test_scheduled_input import Wire


class VanillaEntityDimensionsTests(unittest.TestCase):
    def test_exact_names_and_read_only_dimensions(self):
        dimensions = vanilla_entity_dimensions()
        self.assertEqual(dimensions["minecraft:pig"], VanillaEntityDimensions(width=.9, height=.9))
        self.assertEqual(dimensions["minecraft:sheep"], VanillaEntityDimensions(width=.9, height=1.3))
        self.assertEqual(vanilla_entity_dimensions(2225), dimensions)
        for name in ("sheep", "Minecraft:sheep", "custom:sheep", "minecraft:ender_dragon", "minecraft:rabbit"):
            with self.subTest(name=name):
                self.assertIsNone(dimensions.get(name))
        with self.assertRaises(TypeError):
            dimensions["minecraft:sheep"] = VanillaEntityDimensions(1, 1)
        with self.assertRaises(FrozenInstanceError):
            dimensions["minecraft:sheep"].width = 1

    def test_other_protocols_and_nonintegers_are_rejected(self):
        for protocol in (2168, 2224, 2226, "2225", 2225.0, True, None):
            with self.subTest(protocol=protocol), self.assertRaisesRegex(ValueError, "unsupported vanilla entity dimensions"):
                vanilla_entity_dimensions(protocol)


class EntityRegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Bot("ActorRegistry", protocol=2225)
        self.b = self.bot.protocol.binding
        self.addAsyncCleanup(self.bot.close)

    @staticmethod
    def row(name, type_id, **fields):
        return nbt.CompoundTag({"id": nbt.StringTag(name), "rid": nbt.IntTag(type_id), **fields})

    def registry(self, *rows):
        id_list = nbt.ListTag(rows, nbt.Type.COMPOUND if rows else nbt.Type.END)
        return self.b.AvailableActorIdentifiersPacket(identifier_list=nbt.CompoundTag({"idlist": id_list}))

    def push(self, *packets):
        self.wire.incoming.put_nowait(write_batch(
            packet if isinstance(packet, RawPacket) else self.bot.protocol.encode(packet) for packet in packets
        ))

    async def join(self, *before_start_game, after_spawn=()):
        self.wire = Wire(self.bot.protocol)
        b = self.b
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.push(*before_start_game, b.StartGamePacket(runtime_id=13, entity_id=27),
                  b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN), *after_spawn)
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    async def deliver(self, *packets):
        barrier = self.b.SetTimePacket(time=71)
        pending = asyncio.create_task(self.bot.packets.wait_for_packet(barrier.ID, timeout=1))
        await asyncio.sleep(0)
        self.push(*packets, barrier)
        await pending

    async def test_base_dimensions_are_independent_of_the_joined_registry_and_send_nothing(self):
        dims = vanilla_entity_dimensions()
        await self.join(self.registry(self.row("test:sheep", 58)))
        sent = list(self.wire.sent)
        self.assertIsNone(self.bot.entity_registry.entry_for_name("minecraft:sheep"))
        self.assertIsNone(vanilla_entity_dimensions().get("test:sheep"))
        self.assertEqual(vanilla_entity_dimensions()["minecraft:sheep"], dims["minecraft:sheep"])
        self.assertEqual(self.bot.observed_entities, ())
        self.assertEqual(self.wire.sent, sent)

    async def test_exact_lookups_and_independent_immutable_entries_in_wire_order(self):
        self.assertIsNone(self.bot.entity_registry)
        await self.join(self.registry(
            self.row("test:Wing", 1301, bid=nbt.StringTag(""), hasspawnegg=nbt.ByteTag(0), summonable=nbt.ByteTag(1)),
            self.row("test:wing", 1301),
            self.row("test:Wing", -42, bid=nbt.StringTag("test:asset")),
        ))
        registry = self.bot.entity_registry
        expected = (
            EntityRegistryEntry("test:Wing", 1301, "", False, True),
            EntityRegistryEntry("test:wing", 1301),
            EntityRegistryEntry("test:Wing", -42, "test:asset"),
        )
        self.assertEqual(registry.entries, expected)
        self.assertIs(registry.entry_for_name("test:Wing"), registry.entries[0])
        self.assertIs(registry.entry_for_type_id(1301), registry.entries[0])
        self.assertIs(registry.entry_for_name("test:wing"), registry.entries[1])
        self.assertIs(registry.entry_for_type_id(-42), registry.entries[2])
        self.assertIsNone(registry.entry_for_name("Wing"))
        self.assertIsNone(registry.entry_for_type_id("1301"))
        self.assertEqual(self.bot.observed_entities, ())
        with self.assertRaises(FrozenInstanceError):
            registry.entries = ()
        with self.assertRaises(FrozenInstanceError):
            registry.entries[0].name = "test:other"
        source = list(expected)
        independent = EntityRegistrySnapshot(source)
        source.clear()
        self.assertEqual(independent.entries, expected)

    async def test_latest_valid_advertisement_and_explicit_empty_registry(self):
        invalid_body = RawPacket(self.b.AvailableActorIdentifiersPacket.ID, b"")
        no_list = self.b.AvailableActorIdentifiersPacket(identifier_list=nbt.CompoundTag())
        malformed_row = self.registry(self.row("test:valid", 1), nbt.CompoundTag({"id": nbt.StringTag("test:no_id")}))
        await self.join(invalid_body, no_list, malformed_row, after_spawn=(self.registry(self.row("test:late", 9)),))
        self.assertIsNone(self.bot.entity_registry)
        await self.deliver()
        first = self.bot.entity_registry
        self.assertEqual(first.entries, (EntityRegistryEntry("test:late", 9),))
        await self.deliver(invalid_body, no_list, malformed_row)
        self.assertIs(self.bot.entity_registry, first)
        await self.deliver(self.registry())
        empty = self.bot.entity_registry
        self.assertEqual(empty.entries, ())
        await self.deliver(self.registry(self.row("test:replacement", 10)))
        self.assertEqual(self.bot.entity_registry.entries, (EntityRegistryEntry("test:replacement", 10),))
        self.assertEqual(first.entries, (EntityRegistryEntry("test:late", 9),))
        self.assertEqual(empty.entries, ())

    async def test_disconnect_rejects_trailing_announcement_and_rejoin_starts_empty(self):
        await self.join(self.registry(self.row("test:old", 9)))
        snapshot = self.bot.entity_registry
        await self.deliver(self.b.DisconnectPacket(), self.registry(self.row("test:discarded", 10)))
        self.assertIsNone(self.bot.entity_registry)
        await self.bot.close()
        await self.join()
        self.assertIsNone(self.bot.entity_registry)
        await self.deliver(self.registry(self.row("test:new", 11)))
        observed_at_close = []
        self.bot.subscribe_session_end(lambda _end: observed_at_close.append(self.bot.entity_registry))
        await self.bot.close()
        self.assertEqual(observed_at_close, [None])
        self.assertIsNone(self.bot.entity_registry)
        self.assertEqual(snapshot.entries, (EntityRegistryEntry("test:old", 9),))

    async def test_transport_failure_clears_registry(self):
        await self.join(self.registry(self.row("test:old", 9)))
        snapshot = self.bot.entity_registry
        self.wire.incoming.put_nowait(ConnectionError("transport lost"))
        end = await self.bot.wait_for_session_end(timeout=1)
        self.assertEqual(end.kind, "reader_end")
        self.assertIsNone(self.bot.entity_registry)
        self.assertEqual(snapshot.entries, (EntityRegistryEntry("test:old", 9),))
        with self.assertRaisesRegex(ConnectionError, "transport lost"):
            await self.bot.close()
