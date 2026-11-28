import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from endpilot import BiomeDefinition, Bot
from endpilot.protocol import RawPacket, write_batch

from test_scheduled_input import ManualClock, Wire


class SessionBiomeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("SessionBiomes", protocol=2225)
        self.b = self.bot.protocol.binding
        self.addAsyncCleanup(self.bot.close)

    def packet(self, strings, entries):
        return self.b.BiomeDefinitionListPacket(
            string_list=self.b.BiomeStringList(strings=strings), biome_data=entries,
        )

    def row(self, *, tag_indices=None, **fields):
        tags = None if tag_indices is None else self.b.BiomeTagsData(tags=tag_indices)
        return self.b.BiomeDefinitionData(tags=tags, **fields)

    def push(self, *packets):
        protocol = self.bot.protocol
        self.wire.incoming.put_nowait(write_batch(
            packet if isinstance(packet, RawPacket) else protocol.encode(packet) for packet in packets
        ))

    async def join(self, *before_spawn):
        self.wire = Wire(self.bot.protocol)
        self.wire.push(self.b.NetworkSettingsPacket(compression_algorithm=self.b.PacketCompressionAlgorithm.NONE))
        self.push(self.b.StartGamePacket(runtime_id=13, entity_id=27), *before_spawn,
                  self.b.PlayStatusPacket(status=self.b.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    async def deliver(self, *packets):
        barrier = self.b.SetTimePacket(time=71)
        async with asyncio.TaskGroup() as group:
            group.create_task(self.bot.packets.wait_for_packet(barrier.ID, timeout=1))
            await asyncio.sleep(0)
            self.push(*packets, barrier)

    async def test_login_snapshot_uses_exact_names_and_tag_order_without_numeric_ids(self):
        self.assertIsNone(self.bot.biome_definitions)
        await self.join(self.packet(("cold", "test:Blue", "wet", "test:blue"), {
            1: self.row(id=65535, temperature=-0.25, downfall=0.75, rain=True, tag_indices=(2, 0, 2)),
            3: self.row(id=65535, temperature=0.25, downfall=0.0, rain=False),
        }))
        sent = list(self.wire.sent)
        snapshot = self.bot.biome_definitions
        self.assertEqual(snapshot, {
            "test:Blue": BiomeDefinition("test:Blue", -0.25, 0.75, True, ("wet", "cold", "wet")),
            "test:blue": BiomeDefinition("test:blue", 0.25, 0.0, False, ()),
        })
        for missing in ("test:BLUE", "minecraft:ocean", 1, 65535):
            self.assertIsNone(snapshot.get(missing))
        self.assertFalse(self.bot.weather.raining)
        with self.assertRaises(TypeError):
            snapshot["test:other"] = snapshot["test:Blue"]
        with self.assertRaises(FrozenInstanceError):
            snapshot["test:Blue"].rain = False
        self.assertIs(self.bot.biome_definitions, snapshot)
        self.assertEqual(self.wire.sent, sent)

    async def test_invalid_advertisements_keep_snapshot_and_valid_replacements_are_complete(self):
        invalid = (
            RawPacket(self.b.BiomeDefinitionListPacket.ID, b""),
            self.packet(("test:valid",), {0: self.row(), 1: self.row()}),
            self.packet(("test:bad_tag",), {0: self.row(tag_indices=(1,))}),
            self.packet(("test:duplicate", "test:duplicate"), {0: self.row(), 1: self.row()}),
        )
        await self.join(*invalid)
        self.assertIsNone(self.bot.biome_definitions)
        sent = list(self.wire.sent)
        await self.deliver(self.packet(("test:first",), {0: self.row(temperature=1.0)}))
        first = self.bot.biome_definitions
        await self.deliver(*invalid)
        self.assertIs(self.bot.biome_definitions, first)

        await self.deliver(self.packet((), {}))
        empty = self.bot.biome_definitions
        self.assertIsNotNone(empty)
        self.assertEqual(empty, {})
        await self.deliver(self.packet(("test:replacement",), {0: self.row(temperature=0.5)}))
        self.assertEqual(tuple(self.bot.biome_definitions), ("test:replacement",))
        self.assertEqual(first["test:first"].temperature, 1.0)
        self.assertEqual(empty, {})
        self.assertEqual(self.wire.sent, sent)

    async def test_disconnect_and_reader_failure_end_snapshot_without_changing_saved_values(self):
        original = self.packet(("test:old",), {0: self.row(temperature=1.0)})
        fresh = self.packet(("test:new",), {0: self.row(temperature=0.0)})
        await self.join(original)
        before = self.bot.biome_definitions
        await self.deliver(self.b.DisconnectPacket(), fresh)
        self.assertIsNone(self.bot.biome_definitions)
        await self.bot.close()
        await self.join()
        self.assertIsNone(self.bot.biome_definitions)
        await self.deliver(fresh)
        current = self.bot.biome_definitions
        self.assertEqual(tuple(current), ("test:new",))
        self.wire.incoming.put_nowait(ConnectionError("transport lost"))
        self.assertEqual((await self.bot.wait_for_session_end(timeout=1)).kind, "reader_end")
        self.assertIsNone(self.bot.biome_definitions)
        self.assertEqual((before["test:old"].temperature, current["test:new"].temperature), (1.0, 0.0))
        with self.assertRaisesRegex(ConnectionError, "transport lost"):
            await self.bot.close()
