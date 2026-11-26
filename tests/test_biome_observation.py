"""Biome queries over the same encoded packet reader as block observation."""

import asyncio
import struct
import unittest

from endpilot.blocks import Blocks
from endpilot.compression import PacketConnection
from endpilot.protocol import Protocol, write_batch
from endpilot.subchunks import parse_biome_sections
from test_subchunks import signed_varint
from test_world_observation import Transport


def constant_column(biome_id, sections=24):
    return b"\x01" + signed_varint(biome_id) + b"\xff" * (sections - 1) + b"\x00"


class BiomeObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.protocol = Protocol(2225)
        self.b = b = self.protocol.binding
        self.transport = Transport()
        self.packets = PacketConnection(self.transport, self.protocol, b.NetworkSettingsPacket(
            compression_algorithm=b.PacketCompressionAlgorithm.ZLIB, compression_threshold=0,
        ))
        self.reader_failure = None
        self.addAsyncCleanup(self.close_packets)
        self.blocks = Blocks(self.packets, None, None, hashed_runtime_ids=True)
        self.addCleanup(self.blocks.close)
        self.blocks.start()
        self.reader = self.packets.start_receiving()

    async def close_packets(self):
        try:
            await self.packets.close()
        except ConnectionError as error:
            if error is not self.reader_failure:
                raise

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        passed = asyncio.create_task(self.packets.wait_for_packet(marker.ID, timeout=2))
        await asyncio.sleep(0)
        frames = write_batch(self.protocol.encode(packet) for packet in (*packets, marker))
        self.transport.incoming.put_nowait(b"\xff" + frames)
        await passed

    def announced(self, payload, *, column=(-1, -1), dimension=0, **options):
        b = self.b
        return b.LevelChunkPacket(
            pos=b.ChunkPos(x=column[0], z=column[1]), dimension_id=dimension,
            sub_chunks_count=0, client_request_sub_chunk_limit=8, serialized_chunk=payload, **options,
        )

    def publisher(self, x, z):
        b = self.b
        return b.NetworkChunkPublisherUpdatePacket(position=b.BlockPos(x=x, y=0, z=z), radius=32)

    async def test_announced_biomes_cover_negative_cells_without_loading_block_sections(self):
        data = bytearray(512)
        # Local (15, 1, 1) is index 3857 in X/Z/Y order.
        data[482] = 2
        payload = b"\x01\x00\x03" + data + b"\x04\x00\xf6\x02" + b"\xff" * 22 + b"\x00"
        self.assertIsNone(self.blocks.observed_biome((-1, -47, -15)))
        await self.deliver(self.publisher(-8, -8), self.announced(payload))

        self.assertEqual(self.blocks.observed_biome((-1, -47, -15)), 187)
        self.assertEqual(self.blocks.observed_biome((-1, -31, -15)), 187)
        self.assertEqual(self.blocks.observed_biome((-1, -47, -14)), 0)
        self.assertEqual(self.blocks.observed_biome((-1, -46, -15)), 0)
        self.assertEqual(self.blocks.observed_biome((-1, -64, -15)), 0)
        self.assertEqual(self.blocks.observed_biome((-1, 319, -15)), 0)
        for position in ((-1, -65, -15), (-1, 320, -15), (0, -47, -15)):
            self.assertIsNone(self.blocks.observed_biome(position))
        await self.deliver(self.announced(constant_column(8), column=(0, -1)))
        self.assertEqual(self.blocks.observed_biome((0, -47, -15)), 8)
        self.assertEqual(self.blocks.observed_biome((-1, -47, -15)), 187)
        self.assertIsNone(self.blocks.observed_block((-1, -47, -15)))
        self.assertEqual(self.blocks.column_coverage((-1, -1)).received_section_ys, ())
        self.assertEqual(self.transport.sent, [])

    async def test_invalid_biome_data_preserves_block_observation_and_neighbor(self):
        b = self.b
        position = (-8, 0, -8)
        other = (0, 0, -8)
        # One ordinary inline block section with unknown data after its block prefix.
        inline = b.LevelChunkPacket(
            pos=b.ChunkPos(x=-1, z=-1), dimension_id=0, sub_chunks_count=1,
            serialized_chunk=b"\x09\x01\x00\x01\x54\xff",
        )
        await self.deliver(inline, self.announced(constant_column(0), column=(0, -1)))
        self.assertEqual(self.blocks.observed_block(position).runtime_id, 42)
        self.assertIsNone(self.blocks.observed_biome(position))

        # The first two-bit index is 3, but the palette only has entries 0 through 2.
        out_of_bounds = (
            b"\x05" + struct.pack("<I", 3) + bytes(1020)
            + b"\x06\x00\x10\x12" + b"\xff" * 23 + b"\x00"
        )
        with self.assertRaisesRegex(ValueError, "^biome palette index out of bounds$"):
            parse_biome_sections(out_of_bounds, 24)

        bad_payloads = {
            "truncated trailer": constant_column(17)[:-1],
            "missing predecessor": b"\xff" * 24 + b"\x00",
            "unsupported storage header": b"\x02" + b"\xff" * 23 + b"\x00",
            "negative ID": constant_column(-1),
            "packed index outside its palette": out_of_bounds,
        }
        for label, bad in bad_payloads.items():
            with self.subTest(label=label):
                valid = self.announced(constant_column(187))
                await self.deliver(valid)
                archived = self.blocks.observed_biome(position)
                self.assertEqual(archived, 187)
                await self.deliver(self.announced(bad))
                self.assertIsNone(self.blocks.observed_biome(position))
                self.assertEqual(archived, 187)
                self.assertEqual(self.blocks.observed_block(position).runtime_id, 42)
                self.assertEqual(self.blocks.observed_biome(other), 0)
        self.assertEqual(self.transport.sent, [])

    async def test_unsupported_forms_do_not_revive_a_known_biome(self):
        position = (-8, 0, -8)
        payload = constant_column(187)
        for fields in ({"cache_enabled": True}, {"is_client_biome_update": True}):
            with self.subTest(fields=fields):
                await self.deliver(self.announced(payload))
                self.assertEqual(self.blocks.observed_biome(position), 187)
                await self.deliver(self.announced(payload, **fields))
                self.assertIsNone(self.blocks.observed_biome(position))
                self.assertTrue(self.blocks.column_coverage((-1, -1)).announced)

    async def test_biome_lifetime_matches_publisher_dimension_and_disconnect(self):
        b = self.b
        position = (-8, 0, -8)
        packet = self.announced(constant_column(187))
        await self.deliver(self.publisher(-8, -8), packet)
        snapshot = self.blocks.observed_biome(position)
        self.assertEqual(snapshot, 187)

        await self.deliver(self.publisher(384, 384), packet)
        self.assertIsNone(self.blocks.observed_biome(position))
        await self.deliver(self.publisher(-8, -8))
        self.assertIsNone(self.blocks.observed_biome(position))
        await self.deliver(packet)
        self.assertEqual(self.blocks.observed_biome(position), 187)

        await self.deliver(b.ChangeDimensionPacket(dimension_id=1), packet)
        self.assertIsNone(self.blocks.observed_biome(position))
        await self.deliver(b.ChangeDimensionPacket(dimension_id=0))
        self.assertIsNone(self.blocks.observed_biome(position))
        await self.deliver(packet)
        self.assertEqual(self.blocks.observed_biome(position), 187)

        await self.deliver(b.DisconnectPacket(), packet)
        self.assertIsNone(self.blocks.observed_biome(position))
        self.assertEqual(snapshot, 187)
        self.assertEqual(self.transport.sent, [])

    async def test_changed_dimension_height_does_not_relabel_existing_biome_sections(self):
        b = self.b
        packet = self.announced(constant_column(187))
        lower = (-8, -64, -8)

        def bounds(min_y, height):
            return b.DimensionDataPacket(dimension_definitions={
                "test:bounds": b.DimensionDataPacket.DimensionDefinition(
                    dimension_type=0, min_y=min_y, height_range=height,
                ),
            })

        await self.deliver(packet)
        self.assertEqual(self.blocks.observed_biome(lower), 187)
        await self.deliver(bounds(-32, 384), packet)
        self.assertIsNone(self.blocks.observed_biome((-8, -32, -8)))
        await self.deliver(bounds(-64, 384))
        self.assertIsNone(self.blocks.observed_biome(lower))
        await self.deliver(packet)
        self.assertEqual(self.blocks.observed_biome(lower), 187)

    async def test_nether_and_end_use_their_own_observed_height(self):
        b = self.b
        for dimension, height, biome_id in ((1, 8, 178), (2, 16, 9)):
            with self.subTest(dimension=dimension):
                await self.deliver(b.ChangeDimensionPacket(dimension_id=dimension))
                self.assertIsNone(self.blocks.observed_biome((-8, 0, -8)))
                await self.deliver(self.announced(constant_column(biome_id, height), dimension=dimension))
                self.assertEqual(self.blocks.observed_biome((-8, 0, -8)), biome_id)
                self.assertEqual(self.blocks.observed_biome((-8, height * 16 - 1, -8)), biome_id)
                self.assertIsNone(self.blocks.observed_biome((-8, -1, -8)))
                self.assertIsNone(self.blocks.observed_biome((-8, height * 16, -8)))
        self.assertEqual(self.transport.sent, [])

    async def test_unverified_dimension_has_no_biome_observation(self):
        b = self.b
        await self.deliver(
            b.ChangeDimensionPacket(dimension_id=3),
            b.DimensionDataPacket(dimension_definitions={
                "test:dimension": b.DimensionDataPacket.DimensionDefinition(
                    dimension_type=3, min_y=0, height_range=128,
                ),
            }),
            self.announced(constant_column(178, 8), dimension=3),
        )
        self.assertTrue(self.blocks.column_coverage((-1, -1)).announced)
        self.assertIsNone(self.blocks.observed_biome((-8, 0, -8)))
        self.assertEqual(self.transport.sent, [])

    async def test_transport_loss_invalidates_live_biomes(self):
        position = (-8, 0, -8)
        await self.deliver(self.announced(constant_column(187)))
        self.assertEqual(self.blocks.observed_biome(position), 187)
        self.reader_failure = ConnectionError("reader lost")
        self.transport.incoming.put_nowait(self.reader_failure)
        with self.assertRaisesRegex(ConnectionError, "reader lost"):
            await self.reader
        self.assertIsNone(self.blocks.observed_biome(position))
