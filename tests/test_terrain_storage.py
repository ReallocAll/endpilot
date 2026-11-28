import asyncio
import sqlite3
import struct
import unittest
from contextlib import closing
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from endpilot import Bot, ColumnCoverageInvalidated, TerrainSectionData, TerrainStorage, TerrainStorageError
from endpilot.protocol import _write_uint, write_batch
from test_scheduled_input import ManualClock, Wire


STONE = 0x80310E21
WATER = 0x483B8B2A
LADDER_NORTH = 0x547A1F1A
UNKNOWN = 0xFFFFFFFE
POSITION = (4, 100, 7)
COLUMN = (0, 0)


def _signed_varint(runtime_id):
    signed = runtime_id if runtime_id < 2**31 else runtime_id - 2**32
    return _write_uint(((signed << 1) ^ (signed >> 31)) & 0xffffffff)


class MemoryBackend:
    def __init__(self):
        self.rows = {}
        self.commits = []
        self.write_started = asyncio.Event()
        self.allow_write = asyncio.Event()
        self.allow_write.set()
        self.read_started = asyncio.Event()
        self.allow_read = asyncio.Event()
        self.allow_read.set()
        self.writer_closed = asyncio.Event()

    def open(self, world, protocol, dimension):
        backend = self
        scope = (world, protocol, dimension)

        class Handle:
            wrote = False

            async def commit(self, session_id, through, sections, *, finished=False):
                self.wrote = True
                backend.write_started.set()
                await backend.allow_write.wait()
                backend.commits.append((scope, session_id, through, dict(sections), finished))
                for key, data in sections.items():
                    older = backend.rows.get((scope, key), TerrainSectionData())
                    backend.rows[scope, key] = older.merged_with(TerrainSectionData.from_bytes(data.to_bytes()))

            async def load_column(self, column):
                backend.read_started.set()
                await backend.allow_read.wait()
                return {y: data for (key, (x, y, z)), data in backend.rows.items()
                        if key == scope and column == (x, z)}

            async def aclose(self):
                if self.wrote:
                    backend.writer_closed.set()

        return Handle()


class TerrainStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Bot("TerrainStorage")
        self.b = self.bot.protocol.binding
        self.path = Path(self.enterContext(TemporaryDirectory())) / "terrain.sqlite"
        self.storage = TerrainStorage.sqlite(self.path, world_key="first")
        self.addAsyncCleanup(self.bot.close)

    def section(self, *runtime_ids, x=0, y=6, z=0, dimension=0, all_air=False, payload=None):
        packet = self.b.SubChunkPacket
        result = packet.SubChunkRequestResult
        if not all_air and payload is None:
            payload = bytes((9, len(runtime_ids), y & 255)) + b"".join(
                b"\x01" + _signed_varint(runtime_id) for runtime_id in runtime_ids
            )
        return packet(dimension_type=dimension, center_pos=self.b.SubChunkPos(x=x, y=y, z=z), sub_chunk_data=[
            packet.SubChunkPacketData(result=result.SUCCESS_ALL_AIR if all_air else result.SUCCESS,
                                      serialized_sub_chunk=payload),
        ])

    def change(self, runtime_id, *, position=POSITION, layer=0):
        x, y, z = position
        return self.b.UpdateBlockPacket(pos=self.b.BlockPos(x=x, y=y, z=z), runtime_id=runtime_id, layer=layer)

    async def join(self, *early, hashed=True, retain=False, via="raknet", storage=True):
        b = self.b
        self.wire = Wire(self.bot.protocol)
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in (
            b.StartGamePacket(runtime_id=7, entity_id=7, block_network_ids_are_hashes=hashed),
            *early, b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        )))
        options = dict(terrain_storage=self.storage if storage else None, retain_initial_chunks=retain, timeout=2)
        if via == "address":
            join, endpoint = self.bot.join_address, "raknet://127.0.0.1:19132"
        elif via == "nethernet":
            join, endpoint = self.bot.join_nethernet, "http://127.0.0.1:19132"
        else:
            join, endpoint = self.bot.join_raknet, "127.0.0.1"
        with patch(f"endpilot.transport.{'nethernet' if via == 'nethernet' else 'raknet'}.connect",
                   return_value=self.wire):
            await join(endpoint, **options)

    async def feed(self, *packets):
        # The sentinel follows the entire terrain batch on the public reader.
        b = self.b
        batch = write_batch(self.bot.protocol.encode(packet) for packet in (
            *packets, b.ChunkRadiusUpdatedPacket(chunk_radius=4),
        ))
        waiting = asyncio.create_task(self.bot.packets.wait_for_packet(b.ChunkRadiusUpdatedPacket.ID, timeout=2))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(batch)
        await waiting

    async def archived(self, column=COLUMN, *, protocol=2225, dimension=0, world="first"):
        fresh = TerrainStorage.sqlite(self.path, world_key=world)
        return await fresh.load_column(column, protocol=protocol, dimension=dimension)

    def boundaries(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute("SELECT dimension, session, through, finished FROM terrain_sessions "
                                      "ORDER BY rowid").fetchall()

    async def test_join_storage_captures_both_login_retention_policies_and_all_join_entry_points(self):
        sessions = set()
        for retain, via in ((False, "raknet"), (True, "nethernet"), (False, "address")):
            with self.subTest(retain=retain, via=via):
                await self.join(self.section(STONE, WATER), self.change(LADDER_NORTH), retain=retain, via=via)
                await self.feed()
                column = await self.bot.terrain_column(COLUMN)
                primary, extra = column.block(POSITION), column.block(POSITION, layer=1)
                self.assertEqual((column.current_sections, column.stored_sections), ((6,), ()))
                self.assertEqual((primary.name, primary.properties, primary.source),
                                 ("minecraft:ladder", {"facing_direction": 2}, "current"))
                self.assertEqual((extra.name, extra.runtime_id, extra.source), ("minecraft:water", WATER, "current"))
                self.assertEqual(primary.session_id, extra.session_id)
                with self.assertRaises(TypeError):
                    primary.properties["facing_direction"] = 4
                with self.assertRaises(FrozenInstanceError):
                    column.current_sections = ()
                await self.bot.close()
                self.assertTrue(self.wire.closed)
                stored = await self.archived()
                self.assertEqual((stored.current_sections, stored.stored_sections), ((), (6,)))
                self.assertEqual(stored.block(POSITION).source, "stored")
                sessions.add(stored.block(POSITION).session_id)
        self.assertEqual(len(sessions), 3)
        self.assertEqual([(dim, through, done) for dim, _, through, done in self.boundaries()], [(0, 2, 1)] * 3)

    async def test_complete_replacement_clears_sparse_layers_before_unload(self):
        await self.join()
        await self.feed(self.section(STONE, WATER), self.change(WATER, layer=2), self.change(UNKNOWN))
        old = await self.bot.terrain_column(COLUMN)
        self.assertIsNone(old.block(POSITION).name)
        self.assertIsNone(old.block(POSITION).is_air)
        self.assertEqual(old.block(POSITION).runtime_id, UNKNOWN)
        self.assertEqual(old.block(POSITION, layer=2).name, "minecraft:water")
        unload = self.b.NetworkChunkPublisherUpdatePacket(position=self.b.BlockPos(x=256, z=256), radius=10)
        await self.feed(self.section(all_air=True), unload)
        self.assertIsNone(self.bot.observed_block(POSITION))
        latest = await self.bot.terrain_column(COLUMN)
        self.assertEqual((latest.current_sections, latest.stored_sections), ((), (6,)))
        self.assertTrue(latest.block(POSITION).is_air)
        self.assertIsNone(latest.block(POSITION).runtime_id)
        self.assertTrue(latest.block(POSITION, layer=1).is_air)
        self.assertIsNone(latest.block(POSITION, layer=1).runtime_id)
        self.assertIsNone(latest.block(POSITION, layer=2))
        self.assertEqual(old.block(POSITION, layer=2).source, "current")

    async def test_packed_palette_preserves_cell_order_at_negative_coordinates(self):
        position = (-12, -13, -9)
        index = (4 << 8) | (7 << 4) | 3
        words = bytearray(512)
        struct.pack_into("<I", words, (index // 32) * 4, 1 << (index % 32))
        payload = (b"\x09\x02\xff\x03" + words + _signed_varint(2) + _signed_varint(STONE)
                   + _signed_varint(LADDER_NORTH) + b"\x01" + _signed_varint(WATER))
        await self.join(self.section(x=-1, y=-1, z=-1, payload=payload))
        online = await self.bot.terrain_column((-1, -1))
        await self.bot.close()
        offline = await self.archived((-1, -1))
        for column in (online, offline):
            self.assertEqual(column.block(position).name, "minecraft:ladder")
            self.assertEqual(column.block((-12, -12, -9)).name, "minecraft:stone")
            self.assertEqual(column.block((-12, -13, -10)).name, "minecraft:stone")
            self.assertEqual(column.block(position, layer=1).name, "minecraft:water")

    async def test_reconnect_keeps_old_id_context_with_sparsely_current_unknown(self):
        await self.join()
        await self.feed(self.section(STONE))
        await self.bot.close()
        old = await self.archived()
        await self.join(hashed=False)
        await self.feed(self.change(STONE))
        current = await self.bot.terrain_column(COLUMN)
        self.assertEqual((current.current_sections, current.stored_sections), ((), (6,)))
        self.assertEqual(current.block(POSITION).source, "current")
        self.assertIsNone(current.block(POSITION).name)
        self.assertIsNone(current.block(POSITION).is_air)
        neighbor = current.block((5, 100, 7))
        self.assertEqual((neighbor.runtime_id, neighbor.name, neighbor.source), (STONE, "minecraft:stone", "stored"))
        self.assertEqual(neighbor.session_id, old.block(POSITION).session_id)
        self.assertNotEqual(neighbor.session_id, current.block(POSITION).session_id)
        await self.bot.close()
        archived = await self.archived()
        self.assertEqual(archived.block(POSITION).session_id, current.block(POSITION).session_id)
        self.assertEqual(archived.block((5, 100, 7)).session_id, neighbor.session_id)

    async def test_scope_isolation_and_sparse_or_announced_columns(self):
        b = self.b
        await self.join()
        await self.feed(self.section(STONE), b.ChangeDimensionPacket(dimension_id=1), self.section(WATER, dimension=1),
                        b.LevelChunkPacket(pos=b.ChunkPos(x=2, z=0), dimension_id=1),
                        self.section(x=2, dimension=1, payload=b"\x01"),
                        self.change(WATER, position=(48, 100, 0), layer=1))
        self.assertIsNone(await self.bot.terrain_column((2, 0)))
        sparse = await self.bot.terrain_column((3, 0))
        self.assertEqual((sparse.current_sections, sparse.stored_sections), ((), ()))
        self.assertIsNone(sparse.block((48, 100, 0)))
        self.assertEqual(sparse.block((48, 100, 0), layer=1).source, "current")
        await self.bot.close()
        self.assertEqual((await self.archived()).block(POSITION).name, "minecraft:stone")
        self.assertEqual((await self.archived(dimension=1)).block(POSITION).name, "minecraft:water")
        self.assertIsNone(await self.archived(dimension=2))
        self.assertIsNone(await self.archived(protocol=1000))
        self.assertIsNone(await self.archived(world="second"))

    async def test_unconfigured_join_leaves_live_queries_available(self):
        await self.join(self.section(STONE), storage=False)
        self.assertEqual(self.bot.observed_block_state(POSITION).name, "minecraft:stone")
        with self.assertRaisesRegex(RuntimeError, "terrain storage"):
            await self.bot.terrain_column(COLUMN)
        self.assertFalse(self.path.exists())

    async def test_queue_drains_prefix_after_saturation_while_clock_and_reader_keep_working(self):
        backend = MemoryBackend()
        backend.allow_write.clear()
        self.addCleanup(backend.allow_write.set)
        self.storage = TerrainStorage("first", backend.open, max_pending_bytes=180)
        clock = ManualClock()
        self.enterContext(clock.patch_scheduler())
        await self.join(self.section(all_air=True))
        await asyncio.wait_for(backend.write_started.wait(), 1)
        query = asyncio.create_task(self.bot.terrain_column(COLUMN))
        await asyncio.sleep(0)
        self.assertFalse(backend.read_started.is_set())
        await self.feed(self.b.MovePlayerPacket(player_id=7, pos=self.b.Vec3(x=4, y=102, z=7), reset_position=2))
        await clock.wake_at(50)
        self.assertTrue(any(isinstance(packet, self.b.PlayerAuthInputPacket) for packet in self.wire.sent))
        await self.feed(self.section(all_air=True, x=1), self.section(STONE, x=1), self.section(all_air=True, x=2),
                        self.section(WATER, x=3))
        error = self.storage.error
        self.assertIsInstance(error, TerrainStorageError)
        self.assertEqual(error.sequence, 4)
        self.assertEqual(self.bot.observed_block_state((48, 100, 0)).name, "minecraft:water")
        flush = asyncio.create_task(self.storage.flush())
        close = asyncio.create_task(self.bot.close())
        async with asyncio.timeout(2):
            while not self.wire.closed:
                await asyncio.sleep(0)
        self.assertFalse(query.done())
        self.assertFalse(flush.done())
        self.assertFalse(close.done())
        backend.allow_write.set()
        with self.assertRaises(ConnectionError):
            await query
        for operation in (flush, close):
            with self.assertRaises(TerrainStorageError) as caught:
                await operation
            self.assertIs(caught.exception, error)
        self.assertTrue(backend.writer_closed.is_set())
        self.assertEqual([(through, tuple(sections), finished) for _, _, through, sections, finished in backend.commits],
                         [(1, ((0, 6, 0),), False), (3, ((1, 6, 0),), False)])
        saved = await self.storage.load_column((1, 0), protocol=2225, dimension=0)
        self.assertEqual(saved.block((16, 100, 0)).name, "minecraft:stone")
        await self.join(self.section(WATER))
        self.assertIsNone(self.storage.error)
        await self.bot.close()
        _, session, through, _, finished = backend.commits[-1]
        self.assertTrue(finished)
        self.assertEqual(through, 1)
        self.assertNotEqual(session, error.session_id)

    async def test_delayed_read_is_invalidated_by_dimension_round_trip_and_rejoin(self):
        backend = MemoryBackend()
        self.storage = TerrainStorage("first", backend.open)
        self.addCleanup(backend.allow_read.set)
        await self.join(self.section(STONE))
        await self.storage.flush()
        for rejoin in (False, True):
            with self.subTest(rejoin=rejoin):
                backend.read_started.clear()
                backend.allow_read.clear()
                reading = asyncio.create_task(self.bot.terrain_column(COLUMN))
                await asyncio.wait_for(backend.read_started.wait(), 1)
                if rejoin:
                    await self.bot.close()
                    await self.join(self.section(WATER))
                    expected = ConnectionError
                else:
                    await self.feed(self.b.ChangeDimensionPacket(dimension_id=1),
                                    self.b.ChangeDimensionPacket(dimension_id=0))
                    expected = ColumnCoverageInvalidated
                backend.allow_read.set()
                with self.assertRaises(expected):
                    await reading
        self.assertEqual((await self.bot.terrain_column(COLUMN)).block(POSITION).name, "minecraft:water")

    async def test_remote_end_drains_and_closes_without_finished_marker(self):
        for kind in ("server_disconnect", "reader_end"):
            with self.subTest(kind=kind):
                backend = MemoryBackend()
                self.storage = TerrainStorage("first", backend.open)
                await self.join(self.section(STONE))
                if kind == "server_disconnect":
                    self.wire.push(self.b.DisconnectPacket())
                else:
                    self.wire.incoming.put_nowait(ConnectionError("test transport lost"))
                self.assertEqual((await self.bot.wait_for_session_end()).kind, kind)
                await asyncio.wait_for(backend.writer_closed.wait(), 1)
                self.assertEqual([(through, finished) for _, _, through, _, finished in backend.commits], [(1, False)])
                if kind == "reader_end":
                    with self.assertRaisesRegex(ConnectionError, "test transport lost"):
                        await self.bot.close()
                else:
                    await self.bot.close()
                self.assertTrue(self.wire.closed)

    async def test_sqlite_rolls_back_a_batch_and_its_boundary_when_a_section_fails(self):
        await self.join(self.section(STONE))
        await self.storage.flush()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""CREATE TRIGGER reject_test_section BEFORE INSERT ON terrain_sections
                WHEN NEW.x=2 BEGIN SELECT RAISE(ABORT, 'test fault'); END""")
        await self.feed(self.section(WATER), self.section(WATER, x=2))
        with self.assertRaises(TerrainStorageError) as caught:
            await self.storage.flush()
        self.assertIsInstance(caught.exception.__cause__, sqlite3.IntegrityError)
        self.assertIs(caught.exception, self.storage.error)
        self.assertEqual(caught.exception.sequence, 2)
        self.assertEqual((await self.archived()).block(POSITION).name, "minecraft:stone")
        self.assertIsNone(await self.archived((2, 0)))
        self.assertEqual([(dim, through, done) for dim, _, through, done in self.boundaries()], [(0, 1, 0)])
        with self.assertRaises(TerrainStorageError):
            await self.bot.close()
        self.assertTrue(self.wire.closed)
