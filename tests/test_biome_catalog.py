"""Resolve vanilla biome IDs without joining the catalog to server state."""

import asyncio
from dataclasses import FrozenInstanceError
from importlib.resources import files
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from endpilot import Bot, VanillaBiome, vanilla_biomes
from endpilot.protocol import write_batch

from test_biome_observation import constant_column
from test_scheduled_input import ManualClock, Wire
from test_subchunks import signed_varint


# These names and IDs were independently matched to the protocol 2225
# columns and Endstone Block.biome observations in three dimensions.
EXPECTED_NAMES = {
    0: "minecraft:ocean",
    8: "minecraft:hell",
    9: "minecraft:the_end",
    178: "minecraft:soulsand_valley",
    181: "minecraft:basalt_deltas",
    187: "minecraft:lush_caves",
}


class BiomeCatalogTests(unittest.TestCase):
    def test_names_properties_and_sorted_unique_ids(self):
        catalog = vanilla_biomes(protocol=2225)
        self.assertIs(catalog, vanilla_biomes())
        self.assertEqual(len(catalog), 90)
        self.assertEqual(list(catalog), sorted(catalog))
        self.assertEqual(len({biome.name for biome in catalog.values()}), 90)
        self.assertTrue(all(biome.id == biome_id for biome_id, biome in catalog.items()))
        self.assertEqual({key: catalog[key].name for key in EXPECTED_NAMES}, EXPECTED_NAMES)
        self.assertEqual(catalog[0], VanillaBiome(0, "minecraft:ocean", 0.5, 0.5, True,
                                                ("monster", "ocean", "overworld", "fast_fishing", "high_seas")))
        self.assertEqual((catalog[8].temperature, catalog[8].downfall, catalog[8].rain), (2.0, 0.0, False))
        self.assertEqual((type(catalog[8].temperature), type(catalog[8].downfall)), (float, float))
        self.assertEqual((catalog[187].temperature, catalog[187].rain), (0.9, False))
        self.assertIn("lush_caves", catalog[187].tags)

    def test_catalog_and_its_values_are_immutable_and_unknown_ids_are_absent(self):
        catalog = vanilla_biomes()
        with self.assertRaises(TypeError):
            catalog[0] = catalog[8]
        with self.assertRaises(FrozenInstanceError):
            catalog[0].name = "test:changed"
        with self.assertRaises(TypeError):
            catalog[0].tags[0] = "test:changed"
        self.assertIsNone(catalog.get(None))
        self.assertIsNone(catalog.get(50))  # The numeric ID space has gaps.
        self.assertIsNone(catalog.get(500))
        with self.assertRaisesRegex(ValueError, "unsupported vanilla biome protocol 2193"):
            vanilla_biomes(protocol=2193)

    def test_packaged_catalog_records_its_source_and_generator_rejects_unpinned_bytes(self):
        data = json.loads(files("endpilot").joinpath("data", "biomes-2225.json").read_bytes())
        self.assertEqual(data["source"], "EndstoneMC/bedrock-data@40570f03ef64e37c71a81388b52fd55da5d73521:biomes.json")
        self.assertEqual(data["protocol"], 2225)
        generator = Path(__file__).resolve().parents[1] / "scripts/generate_biome_data.py"
        with TemporaryDirectory() as checkout:
            (Path(checkout) / "biomes.json").write_bytes(b'{"biomes":[]}')
            result = subprocess.run([sys.executable, "-B", str(generator), checkout, "--check"],
                                    capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match bedrock-data 40570f03", result.stderr)
        self.assertIn("99a7cccdfc78462b9efe6dc9f18510469bf0849d8005d01920e45f39ef57c654", result.stderr)


class BiomeCatalogObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("BiomeCatalog", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.wire.push(b.StartGamePacket(runtime_id=7, entity_id=17))
        self.wire.push(b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        received = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in (*packets, marker)))
        await received

    def column(self, payload, dimension):
        return self.b.LevelChunkPacket(pos=self.b.ChunkPos(x=-1, z=-1), dimension_id=dimension, sub_chunks_count=0,
                                       client_request_sub_chunk_limit=8, serialized_chunk=payload)

    async def test_public_numeric_observations_can_be_resolved_in_any_vanilla_dimension(self):
        def lookup(y, expected):
            sent = list(self.wire.sent)
            observed = self.bot.observed_biome((-1, y, -1))
            self.assertEqual(observed, expected)
            biome = vanilla_biomes().get(observed)
            self.assertEqual(None if biome is None else biome.name, EXPECTED_NAMES.get(expected))
            self.assertEqual(self.wire.sent, sent)

        b = self.b
        overworld = (b"\x01\x00" + b"\xff" * 5 + b"\x01" + signed_varint(187)
                     + b"\x01\x00" + b"\xff" * 16 + b"\x00")
        await self.deliver(self.column(overworld, 0))
        lookup(32, 187)
        lookup(63, 0)

        await self.deliver(b.ChangeDimensionPacket(dimension_id=1))
        lookup(32, None)
        nether = b"\x01" + signed_varint(8) + b"\x01" + signed_varint(178) + constant_column(181, 6)
        await self.deliver(self.column(nether, 1))
        lookup(0, 8)
        lookup(16, 178)
        lookup(127, 181)
        lookup(128, None)

        await self.deliver(b.ChangeDimensionPacket(dimension_id=2), self.column(constant_column(9, 16), 2))
        lookup(255, 9)
        await self.deliver(self.column(constant_column(500, 16), 2))
        lookup(255, 500)
