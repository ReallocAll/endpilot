import asyncio
from dataclasses import FrozenInstanceError
from hashlib import sha256
from itertools import product
from pathlib import Path
from types import MappingProxyType
import unittest
from unittest.mock import patch
import zlib

from bedrock_protocol import nbt

from endpilot import BlockObservation, BlockStateObservation, Bot, ColumnCoverageInvalidated, DimensionBounds
from endpilot.compression import PacketConnection
from endpilot.login import JoinSession
from endpilot.protocol import RawPacket, read_batch, write_batch


class Transport:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []

    async def recv(self):
        frame = await self.incoming.get()
        if isinstance(frame, Exception):
            raise frame
        return frame

    async def send(self, frame):
        self.sent.append(frame)

    async def close(self):
        pass


class WorldObservationTests(unittest.IsolatedAsyncioTestCase):
    async def connect(self, *, auto=False, hashed_runtime_ids=False, protocol=2225):
        self.bot = Bot("Coverage", protocol=protocol)
        self.addAsyncCleanup(self.bot.close)
        self.b = self.bot.protocol.binding
        self.transport = Transport()
        settings = self.b.NetworkSettingsPacket(
            compression_algorithm=self.b.PacketCompressionAlgorithm.ZLIB, compression_threshold=0,
        )
        packets = PacketConnection(self.transport, self.bot.protocol, settings)
        session = JoinSession(packets, self.bot.identity, "http://127.0.0.1:19132")
        session.player_id, session.player_unique_id = 42, 7
        session.block_network_ids_are_hashes = hashed_runtime_ids
        with patch.object(JoinSession, "connect_nethernet", return_value=session) as join:
            await self.bot.join_nethernet("http://127.0.0.1:19132", load_announced_terrain=auto)
            self.assertFalse(join.await_args.kwargs["retain_initial_chunks"])

    async def deliver(self, *packets):
        self.transport.incoming.put_nowait(b"\xff" + write_batch(map(self.bot.protocol.encode, packets)))
        await asyncio.sleep(0)

    def published(self, x):
        return self.b.NetworkChunkPublisherUpdatePacket(position=self.b.BlockPos(x=x, y=0, z=0), radius=32)

    def air(self, *sections, column=(0, 0), dimension=0):
        b = self.b
        return b.SubChunkPacket(
            dimension_type=dimension, center_pos=b.SubChunkPos(x=column[0], y=0, z=column[1]),
            sub_chunk_data=[b.SubChunkPacket.SubChunkPacketData(
                sub_chunk_pos_offset=b.SubChunkPacket.SubChunkPosOffset(x=0, y=y, z=0),
                result=b.SubChunkPacket.SubChunkRequestResult.SUCCESS_ALL_AIR,
            ) for y in sections],
        )

    def inline_column(self):
        return self.b.LevelChunkPacket(
            pos=self.b.ChunkPos(x=0, z=0), dimension_id=0, sub_chunks_count=2,
            serialized_chunk=b"\x08\x01\x01\x54\x09\x01\x00\x01\x56",
        )

    def dimension_data(self, dimension=0, minimum=-32, height=64):
        return self.b.DimensionDataPacket(dimension_definitions={
            "test:bounds": self.b.DimensionDataPacket.DimensionDefinition(
                dimension_type=dimension, min_y=minimum, height_range=height,
            ),
        })

    async def join_dimension_session(self, dimension, *terrain, retain=False):
        b = self.b
        self.transport = Transport()
        for packet in (
            b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
            b.ResourcePacksInfoPacket(), b.ResourcePackStackPacket(),
            b.StartGamePacket(runtime_id=4, entity_id=17,
                              settings=b.LevelSettings(spawn_settings=b.SpawnSettings(dimension=dimension))),
            *terrain, b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        ):
            self.transport.incoming.put_nowait(write_batch((self.bot.protocol.encode(packet),)))
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132", retain_initial_chunks=retain)

    async def observe_bounds_during_vehicle_teardown(self):
        b = self.b
        await self.deliver(b.SetActorLinkPacket(link=b.ActorLink(a=9, b=7, type=b.ActorLinkType.RIDING)))
        self.assertIsNotNone(self.bot.vehicle)
        seen = []

        def observe(change):
            end = self.bot.session_end
            seen.append((change.reason, None if end is None else end.kind, self.bot.dimension_bounds))

        self.bot.subscribe_vehicle_links(observe)
        return seen

    def entity(self, x, name=None):
        tag = nbt.CompoundTag()
        if name is not None:
            tag = nbt.CompoundTag({
                "id": nbt.StringTag("Chest"), "x": nbt.IntTag(x), "y": nbt.IntTag(20), "z": nbt.IntTag(9),
                "BlockEntityVersion": nbt.IntTag(0), "CustomName": nbt.StringTag(name),
                "Items": nbt.ListTag([nbt.CompoundTag({"Slot": nbt.ByteTag(0), "Count": nbt.ByteTag(2)})]),
                "Extra": nbt.CompoundTag({"Long": nbt.Int64Tag(1 << 40), "Array": nbt.IntArrayTag([0, 3])}),
            })
        return self.b.BlockActorDataPacket(pos=self.b.BlockPos(x=x, y=20, z=9), data=tag)

    def entity_section(self, *packets, tail=b""):
        b = self.b
        roots = b"".join(self.bot.protocol.encode(b.BlockActorDataPacket(
            pos=b.BlockPos(x=0, y=0, z=0), data=packet.data,
        )).body[3:] for packet in packets)
        data = bytes((9, 1, 1, 1, 2)) + roots + tail
        return b.SubChunkPacket(center_pos=b.SubChunkPos(x=0, y=1, z=0), dimension_type=0, sub_chunk_data=[
            b.SubChunkPacket.SubChunkPacketData(result=b.SubChunkPacket.SubChunkRequestResult.SUCCESS,
                                               serialized_sub_chunk=data),
        ])

    async def test_observed_block_state_decodes_each_layer_and_retains_its_properties(self):
        position = (3, 64, 7)
        properties = {"minecraft:corner": "outer_left", "upside_down_bit": 1, "weirdo_direction": 2}
        for hashed, stairs_id, replacement_id, water_id in (
            (True, 3671572261, 3688141530, 1334811448),
            (False, 1888, 1870, 14124),
        ):
            with self.subTest(hashed=hashed):
                await self.connect(hashed_runtime_ids=hashed)
                b = self.b
                pos = b.BlockPos(x=3, y=64, z=7)
                await self.deliver(b.UpdateBlockPacket(pos=pos, runtime_id=stairs_id),
                                   b.UpdateBlockPacket(pos=pos, runtime_id=water_id, layer=1))

                snapshot = self.bot.observed_block_state(position)
                self.assertEqual(snapshot, BlockStateObservation(
                    position, stairs_id, 0, "minecraft:oak_stairs", properties,
                ))
                for key, value in properties.items():
                    self.assertIs(type(snapshot.properties[key]), type(value))
                self.assertEqual(self.bot.observed_block_state(position, layer=1),
                                 BlockStateObservation(position, water_id, 1, "minecraft:water", {"liquid_depth": 2}))
                with self.assertRaises(TypeError):
                    snapshot.properties["weirdo_direction"] = 0
                with self.assertRaises(FrozenInstanceError):
                    snapshot.name = "minecraft:air"

                await self.deliver(b.UpdateBlockPacket(pos=pos, runtime_id=replacement_id))
                current = self.bot.observed_block_state(position)
                self.assertEqual(current, BlockStateObservation(position, replacement_id, 0, "minecraft:oak_stairs", {
                    "minecraft:corner": "inner_left", "upside_down_bit": 1, "weirdo_direction": 0,
                }))
                self.assertEqual(snapshot.properties, properties)
                self.assertEqual(self.transport.sent, [])
                await self.bot.close()

    def test_block_state_observation_copies_input_properties(self):
        properties = {"facing_direction": 2}
        snapshot = BlockStateObservation((3, 64, 7), 1838280610, 0, "minecraft:piston", properties)
        properties["facing_direction"] = 4
        self.assertEqual(snapshot.properties, {"facing_direction": 2})

    async def test_observed_block_state_distinguishes_known_air_from_unknown_cells_and_ids(self):
        position = (3, 64, 7)
        for hashed, air_id, unknown_id in ((True, 0xDBF44120, 0xFFFFFFFE), (False, 18894, 11244)):
            with self.subTest(hashed=hashed):
                await self.connect(hashed_runtime_ids=hashed)
                self.assertIsNone(self.bot.observed_block_state(position))
                self.assertIsNone(self.bot.observed_block_state(position, layer=1))
                await self.deliver(self.air(4))
                self.assertEqual(self.bot.observed_block_state(position), BlockStateObservation(
                    position, air_id if hashed else None, 0, "minecraft:air", {},
                ))
                self.assertEqual(self.bot.observed_block_state(position, layer=1),
                                 BlockStateObservation(position, None, 1, "minecraft:air", {}))

                b = self.b
                pos = b.BlockPos(x=3, y=64, z=7)
                await self.deliver(b.UpdateBlockPacket(pos=pos, runtime_id=air_id))
                self.assertEqual(self.bot.observed_block_state(position),
                                 BlockStateObservation(position, air_id, 0, "minecraft:air", {}))
                for runtime_id in (unknown_id, 0xFFFFFFFF):
                    await self.deliver(b.UpdateBlockPacket(pos=pos, runtime_id=runtime_id))
                    self.assertEqual(self.bot.observed_block(position).runtime_id, runtime_id)
                    self.assertIsNone(self.bot.observed_block_state(position))
                self.assertEqual(self.transport.sent, [])
                await self.bot.close()

    async def test_observed_block_state_rejects_unverified_sequential_ids(self):
        self.bot = Bot("Coverage")
        self.addAsyncCleanup(self.bot.close)
        b = self.b = self.bot.protocol.binding
        await self.join_dimension_session(0)
        sent_at_join = len(self.transport.sent)
        position = (3, 64, 7)
        pos = b.BlockPos(x=3, y=64, z=7)
        incoming = self.air(4), b.UpdateBlockPacket(pos=pos, runtime_id=1888)
        self.transport.incoming.put_nowait(write_batch(map(self.bot.protocol.encode, incoming)))
        await asyncio.sleep(0)
        self.assertEqual(self.bot.observed_block(position), BlockObservation(position, 1888, 0, None))
        self.assertIsNone(self.bot.observed_block_state(position))
        self.assertEqual(self.bot.observed_block_state(position, layer=1),
                         BlockStateObservation(position, None, 1, "minecraft:air", {}))
        self.assertEqual(len(self.transport.sent), sent_at_join)

    async def test_observed_block_state_requires_protocol_2225_even_for_known_air(self):
        await self.connect(hashed_runtime_ids=True, protocol=2193)
        b = self.b
        position = (3, 64, 7)
        pos = b.BlockPos(x=3, y=64, z=7)
        await self.deliver(self.air(4), b.UpdateBlockPacket(pos=pos, runtime_id=3671572261))
        self.assertEqual(self.bot.observed_block(position).runtime_id, 3671572261)
        self.assertTrue(self.bot.observed_block(position, layer=1).is_air)
        self.assertIsNone(self.bot.observed_block_state(position))
        self.assertIsNone(self.bot.observed_block_state(position, layer=1))
        self.assertEqual(self.transport.sent, [])

    async def test_block_entity_query_distinguishes_empty_and_unknown_and_copies_native_values(self):
        await self.connect()
        first, second = (8, 20, 9), (9, 20, 9)
        await self.deliver(self.entity(8, "first"), self.entity(9))
        self.assertIsNone(self.bot.observed_block_entity((10, 20, 9)))
        empty = self.bot.observed_block_entity(second)
        self.assertIsInstance(empty, nbt.CompoundTag)
        self.assertEqual(list(empty.keys()), [])
        source = self.bot.observed_block_entity(first)
        self.assertIsInstance(source["BlockEntityVersion"], nbt.IntTag)
        self.assertEqual(source["BlockEntityVersion"].value, 0)
        self.assertIsInstance(source["Extra"]["Long"], nbt.Int64Tag)
        self.assertEqual(source["Extra"]["Long"].value, 1 << 40)
        self.assertIsInstance(source["Extra"]["Array"], nbt.IntArrayTag)
        self.assertIsInstance(source["Items"][0]["Slot"], nbt.ByteTag)

        extra, item = source["Extra"], source["Items"][0]
        extra["Long"] = nbt.Int64Tag(88)
        item["Count"] = nbt.ByteTag(4)
        source["Extra"], source["Items"] = extra, nbt.ListTag([item])
        source["CustomName"] = nbt.StringTag("local")
        fresh = self.bot.observed_block_entity(first)
        self.assertEqual(fresh["Extra"]["Long"].value, 1 << 40)
        self.assertEqual(fresh["Items"][0]["Count"].value, 2)
        self.assertEqual(fresh["CustomName"].value, "first")
        self.assertEqual(self.transport.sent, [])

    async def test_block_entity_invalidates_on_main_layer_replacement_unload_dimension_and_disconnect(self):
        await self.connect()
        b = self.b
        one, two = (8, 20, 9), (9, 20, 9)
        pos = b.BlockPos(x=8, y=20, z=9)
        main = b.UpdateBlockPacket(pos=pos, runtime_id=83, layer=0)
        extra = b.UpdateSubChunkNetworkBlockInfo(pos=pos, runtime_id=83)

        def changes(standards=(), extras=()):
            return b.UpdateSubChunkBlocksPacket(sub_chunk_block_position=pos,
                blocks_changed=b.UpdateSubChunkBlocksChangedInfo(standards=standards, extras=extras))

        await self.deliver(main, self.entity(8, "before"), self.entity(9, "neighbor"))
        await self.deliver(b.UpdateBlockPacket(pos=pos, runtime_id=83, layer=1), changes(extras=[extra]))
        self.assertEqual(self.bot.observed_block_entity(one)["CustomName"].value, "before")
        await self.deliver(main)
        self.assertIsNone(self.bot.observed_block_entity(one))
        self.assertEqual(self.bot.observed_block_entity(two)["CustomName"].value, "neighbor")
        await self.deliver(self.entity(8, "replacement"), changes(standards=[extra]))
        self.assertIsNone(self.bot.observed_block_entity(one))

        await self.deliver(self.published(1000), self.entity(9, "unpublished"), self.published(0))
        self.assertIsNone(self.bot.observed_block_entity(two))
        await self.deliver(self.entity(9, "current"), b.ChangeDimensionPacket(dimension_id=1))
        self.assertIsNone(self.bot.observed_block_entity(two))
        await self.deliver(self.entity(9, "new dimension"))
        self.assertEqual(self.bot.observed_block_entity(two)["CustomName"].value, "new dimension")
        await self.deliver(b.DisconnectPacket(), self.entity(9, "after disconnect"))
        self.assertIsNone(self.bot.observed_block_entity(two))

    async def test_block_entity_section_without_cursor_clears_old_root_but_announcement_does_not(self):
        await self.connect()
        b = self.b
        position = (8, 20, 9)
        await self.deliver(self.entity(8, "before"), b.LevelChunkPacket(
            pos=b.ChunkPos(x=0, z=0), dimension_id=0, client_request_sub_chunk_limit=18,
        ))
        self.assertEqual(self.bot.observed_block_entity(position)["CustomName"].value, "before")
        with patch.object(self.bot._blocks, "_decode_block_entity", None):
            await self.deliver(self.entity_section(self.entity(8, "cannot decode")))
        self.assertIsNone(self.bot.observed_block_entity(position))
        self.assertEqual(self.bot.observed_block(position).runtime_id, 1)

    @unittest.skipUnless(hasattr(nbt, "decode_network_compound"), "native chunk NBT cursor unavailable")
    async def test_block_entity_section_replaces_only_after_all_roots_are_decoded(self):
        await self.connect()
        one, two = (8, 20, 9), (9, 20, 9)
        neighbor = (24, 20, 9)
        await self.deliver(self.entity(24, "neighbor"))
        seen = []

        def observed(event):
            roots = (self.bot.observed_block_entity(pos) for pos in (one, two))
            seen.append(tuple(None if root is None else root["CustomName"].value for root in roots))

        unsubscribe = self.bot.subscribe_column_coverage(observed)
        try:
            await self.deliver(self.entity_section(
                self.entity(8, "one"), self.entity(24, "outside replacement"),
                self.entity(9, "two"), self.entity(25, "outside addition"),
            ))
        finally:
            unsubscribe()
        self.assertEqual(seen, [("one", "two")])
        self.assertEqual(self.bot.observed_block_entity(neighbor)["CustomName"].value, "neighbor")
        self.assertIsNone(self.bot.observed_block_entity((25, 20, 9)))
        await self.deliver(self.entity_section(self.entity(8, "updated")))
        self.assertEqual(self.bot.observed_block_entity(one)["CustomName"].value, "updated")
        self.assertIsNone(self.bot.observed_block_entity(two))
        await self.deliver(self.entity_section(self.entity(8, "incomplete"), tail=b"\x0a\x00"))
        self.assertIsNone(self.bot.observed_block_entity(one))
        self.assertEqual(self.bot.observed_block_entity(neighbor)["CustomName"].value, "neighbor")
        await self.deliver(self.entity(9, "put back"), self.air(1))
        self.assertIsNone(self.bot.observed_block_entity(two))

    async def test_block_entity_login_keeps_main_layer_and_actor_data_in_wire_order(self):
        for retain in (False, True):
            with self.subTest(retain=retain):
                self.bot = Bot("EarlyActors", protocol=2225)
                self.addAsyncCleanup(self.bot.close)
                self.b = b = self.bot.protocol.binding
                self.transport = Transport()
                pos = b.BlockPos(x=8, y=20, z=9)
                for packet in (
                    b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
                    b.ResourcePacksInfoPacket(), b.ResourcePackStackPacket(),
                    b.StartGamePacket(runtime_id=4, entity_id=17), self.published(0),
                    self.entity(8, "old"), b.UpdateBlockPacket(pos=pos, runtime_id=83, layer=0),
                    b.UpdateBlockPacket(pos=b.BlockPos(x=9, y=20, z=9), runtime_id=83, layer=0),
                    self.entity(9, "current"), b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
                ):
                    self.transport.incoming.put_nowait(write_batch((self.bot.protocol.encode(packet),)))
                with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
                    await self.bot.join_nethernet("http://127.0.0.1:19132", retain_initial_chunks=retain)
                marker = asyncio.create_task(self.bot.packets.wait_for_packet(b.SetTimePacket.ID, timeout=1))
                await asyncio.sleep(0)
                self.transport.incoming.put_nowait(write_batch((self.bot.protocol.encode(b.SetTimePacket()),)))
                await marker
                self.assertIsNone(self.bot.observed_block_entity((8, 20, 9)))
                self.assertEqual(self.bot.observed_block_entity((9, 20, 9))["CustomName"].value, "current")

    async def test_stock_2225_subchunk_makes_chests_known_without_extending_coverage(self):
        # Vanilla BDS 1.26.66.1, build 53696234. Capture: double-lid-90c4752-02,
        # verification.json, before_open.LidA.raw_packets[0], timeline index 656.
        fixture = Path(__file__).with_name("fixtures") / "stock-subchunk-2225.hex"
        body = bytes.fromhex(fixture.read_text(encoding="ascii"))
        self.assertEqual(sha256(body).hexdigest(),
                         "a6501bc0ff651f8de62b081507d9b06c261f79357d851ab81d53335d6ce6e88e")

        await self.connect(hashed_runtime_ids=True)
        chests = {(5, 220, 10): 741882976, (6, 220, 10): 741882976, (6, 220, 9): 2001328343}
        for position in chests:
            self.assertIsNone(self.bot.observed_block(position))

        # The capture retained the game packet header and body after decompression.
        # Rebuild a single-packet batch and its raw DEFLATE frame for the shared reader.
        raw = RawPacket(174, body)
        batch = write_batch((raw,))
        self.transport.incoming.put_nowait(b"\x00" + zlib.compress(batch, wbits=-15))
        await self.bot.packets.wait_for_packet(self.b.SubChunkPacket.ID, timeout=1)

        for position, runtime_id in chests.items():
            with self.subTest(position=position):
                self.assertEqual(self.bot.observed_block(position),
                                 BlockObservation(position, runtime_id, 0, False))
        self.assertIsNone(self.bot.observed_block((5, 224, 10)))
        self.assertIsNone(self.bot.observed_block((16, 220, 10)))

    async def test_dimension_bounds_are_ready_at_join_and_keep_terrain_replay_in_wire_order(self):
        self.bot = Bot("DimensionBounds")
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding

        def column(x):
            return b.LevelChunkPacket(pos=b.ChunkPos(x=x, z=0), dimension_id=1, sub_chunks_count=1,
                                      serialized_chunk=b"\x08\x01\x01\x54")

        for retain in (False, True):
            with self.subTest(retain=retain):
                self.assertIsNone(self.bot.dimension_bounds)
                first = self.dimension_data(1, 16, 64)
                second = self.dimension_data(1, 48, 32)
                await self.join_dimension_session(1, first, column(0), second, column(1), retain=retain)

                sent = list(self.transport.sent)
                snapshot = self.bot.dimension_bounds
                self.assertEqual(snapshot, DimensionBounds(1, 48, 80, "dimension_data"))
                with self.assertRaises(FrozenInstanceError):
                    snapshot.min_y = 0
                self.assertEqual(self.transport.sent, sent)

                replayed = []

                def observe_replayed_bounds(raw):
                    if raw.packet_id == b.DimensionDataPacket.ID:
                        replayed.append(self.bot.dimension_bounds)

                self.bot.packets.subscribe_raw(observe_replayed_bounds)
                marker = b.SetTimePacket()
                self.transport.incoming.put_nowait(write_batch((self.bot.protocol.encode(marker),)))
                await self.bot.packets.wait_for_packet(type(marker).ID, timeout=1)
                self.assertEqual(replayed, [snapshot, snapshot] if retain else [])
                self.assertEqual(self.bot.observed_block((0, 16, 0)).runtime_id, 42)
                self.assertIsNone(self.bot.observed_block((0, 48, 0)))
                self.assertEqual(self.bot.observed_block((16, 48, 0)).runtime_id, 42)
                await self.bot.close()
                self.assertIsNone(self.bot.dimension_bounds)

        # The same Bot gets a fresh set of dimension definitions on rejoin.
        await self.join_dimension_session(1)
        self.assertEqual(self.bot.dimension_bounds, DimensionBounds(1, 0, 128, "vanilla_default"))

    async def test_dimension_bounds_follow_terrain_and_ignore_invalid_definitions(self):
        await self.connect()
        b = self.b
        initial = self.bot.dimension_bounds
        self.assertEqual(initial, DimensionBounds(0, -64, 320, "vanilla_default"))
        await self.deliver(self.dimension_data(0, -64, 384))
        self.assertEqual(self.bot.dimension_bounds, DimensionBounds(0, -64, 320, "dimension_data"))
        self.assertEqual(initial, DimensionBounds(0, -64, 320, "vanilla_default"))

        for dimension, upper in ((1, 128), (2, 256)):
            await self.deliver(b.ChangeDimensionPacket(dimension_id=dimension))
            self.assertEqual(self.bot.dimension_bounds, DimensionBounds(dimension, 0, upper, "vanilla_default"))

        await self.deliver(b.ChangeDimensionPacket(dimension_id=3), self.dimension_data(3, -31, 64))
        self.assertIsNone(self.bot.dimension_bounds)
        await self.deliver(self.dimension_data(3, -2048, 4096))
        custom = self.bot.dimension_bounds
        self.assertEqual(custom, DimensionBounds(3, -2048, 2048, "dimension_data"))
        for minimum, height in ((-2049, 4096), (-2064, 16), (0, 2064), (0, 0), (16, -16), (0, 17)):
            with self.subTest(minimum=minimum, height=height):
                await self.deliver(self.dimension_data(3, minimum, height))
                self.assertEqual(self.bot.dimension_bounds, custom)

        await self.deliver(b.ChangeDimensionPacket(dimension_id=0))
        self.assertEqual(self.bot.dimension_bounds, DimensionBounds(0, -64, 320, "dimension_data"))
        seen = await self.observe_bounds_during_vehicle_teardown()
        await self.deliver(b.DisconnectPacket(), self.dimension_data(0))
        self.assertEqual(seen, [("unobserved", "server_disconnect", None)])
        self.assertIsNone(self.bot.dimension_bounds)
        self.assertEqual(self.transport.sent, [])
        await self.bot.close()
        await self.join_dimension_session(0)
        self.assertIsNone(self.bot.session_end)
        self.assertEqual(self.bot.dimension_bounds, initial)

    async def test_inline_sections_are_passive_in_both_public_loading_modes(self):
        for auto in (False, True):
            with self.subTest(auto=auto):
                await self.connect(auto=auto)
                coverage, changes = [], []
                self.bot.subscribe_column_coverage(coverage.append)
                self.bot.subscribe_block_changes(changes.append)
                waiting = asyncio.create_task(self.bot.wait_for_column_coverage((0, 0), range(-2, 1)))
                await asyncio.sleep(0)
                self.addCleanup(waiting.cancel)
                b = self.b
                inline = self.inline_column()
                await self.deliver(self.dimension_data(), self.published(0), b.LevelChunkPacket(
                    pos=b.ChunkPos(x=0, z=0), dimension_id=0, client_request_sub_chunk_limit=1,
                ), inline, inline, self.air(-1))
                snapshot = await asyncio.wait_for(waiting, 1)
                self.assertEqual(self.bot.column_coverage((0, 0)), snapshot)
                self.assertTrue(snapshot.announced)
                self.assertEqual(snapshot.received_section_ys, (-2, -1, 0))
                self.assertEqual(snapshot.requested_section_ys, ())
                self.assertEqual(self.bot.observed_block((1, -32, 1)).runtime_id, 42)
                self.assertEqual(self.bot.observed_block((1, 0, 1)).runtime_id, 43)
                self.assertTrue(self.bot.observed_block((1, -16, 1)).is_air)
                self.assertEqual([e.kind for e in coverage], ["loaded", "coverage", "coverage"])
                self.assertEqual(changes, [])
                self.assertEqual(self.transport.sent, [])

    async def test_inline_login_packets_prime_both_public_loading_modes_without_raw_replay(self):
        for transport, auto in product(("nethernet", "raknet"), (False, True)):
            with self.subTest(transport=transport, auto=auto):
                self.bot = Bot("Startup", protocol=2225)
                self.addAsyncCleanup(self.bot.close)
                self.b = b = self.bot.protocol.binding
                self.transport = Transport()
                for packet in (
                    b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
                    b.ResourcePacksInfoPacket(), b.ResourcePackStackPacket(),
                    b.StartGamePacket(runtime_id=4, entity_id=17), self.dimension_data(), self.published(0),
                    self.inline_column(), self.air(-1),
                    b.LevelChunkPacket(pos=b.ChunkPos(x=1, z=0), dimension_id=0, client_request_sub_chunk_limit=1),
                    b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
                ):
                    self.transport.incoming.put_nowait(write_batch((self.bot.protocol.encode(packet),)))
                with patch(f"endpilot.transport.{transport}.connect", return_value=self.transport):
                    if transport == "nethernet":
                        await self.bot.join_nethernet("http://127.0.0.1:19132", load_announced_terrain=auto)
                    else:
                        await self.bot.join_raknet("127.0.0.1", load_announced_terrain=auto)
                self.assertEqual(self.bot.resource_pack_index.packs, ())
                self.assertEqual(self.bot.column_coverage((0, 0)).received_section_ys, (-2, -1, 0))
                self.assertEqual(self.bot.observed_block((1, -32, 1)).runtime_id, 42)
                self.assertTrue(self.bot.observed_block((1, -16, 1)).is_air)
                seen = []
                self.bot.packets.subscribe_raw(seen.append)
                await asyncio.sleep(0)
                self.assertEqual(seen, [])
                requests = [self.bot.protocol.decode(raw) for frame in self.transport.sent for raw in read_batch(frame)
                            if raw.packet_id == b.SubChunkRequestPacket.ID]
                self.assertEqual([(request.center_pos.x, request.center_pos.z) for request in requests],
                                 [(1, 0)] if auto else [])
                self.assertEqual(self.bot.column_coverage((1, 0)).requested_section_ys, (-2,) if auto else ())
                await self.bot.close()
                self.assertIsNone(self.bot.resource_pack_index)

    async def test_subscriber_failures_preserve_reader_and_external_cancellation(self):
        await self.connect()
        errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, details: errors.append(details))
        self.addCleanup(loop.set_exception_handler, previous)

        def cancel(event):
            raise asyncio.CancelledError("subscriber cancelled")

        def fail(event):
            raise ValueError("subscriber failure")

        columns, blocks = [], []
        for subscribe, records in ((self.bot.subscribe_column_coverage, columns),
                                   (self.bot.subscribe_block_changes, blocks)):
            subscribe(cancel)
            subscribe(fail)
            subscribe(records.append)
        await self.deliver(self.air(4), self.b.UpdateBlockPacket(
            pos=self.b.BlockPos(x=1, y=64, z=1), runtime_id=42,
        ))
        await self.deliver(self.air(5))
        self.assertEqual([e.kind for e in columns], ["loaded", "coverage"])
        self.assertEqual([e.after.runtime_id for e in blocks], [42])
        waiting = asyncio.create_task(self.bot.wait_for_column_coverage((0, 0), range(6, 7)))
        await asyncio.sleep(0)
        reader = self.bot.packets.start_receiving()
        reader.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reader
        with self.assertRaisesRegex(ConnectionError, "world observation ended"):
            await asyncio.wait_for(waiting, 1)
        self.assertEqual([e.kind for e in columns], ["loaded", "coverage", "unloaded"])
        self.assertEqual([type(report["exception"]) for report in errors],
                         [asyncio.CancelledError, ValueError] * 4)
        self.assertEqual(self.bot.column_coverage((0, 0)).received_section_ys, ())
        self.assertEqual(self.transport.sent, [])
        await self.bot.close()

    async def test_column_coverage_waiters_and_unload_reentry_without_network(self):
        await self.connect()
        column = (0, 0)
        position = (1, 64, 1)
        events, unload_observations = [], []

        def observer(event):
            events.append(event)
            if event.kind == "unloaded":
                unload_observations.append((self.bot.observed_block(position),
                                            self.bot.column_coverage(column).received_section_ys))

        unsubscribe = self.bot.subscribe_column_coverage(observer)
        block_events = []
        self.bot.subscribe_block_changes(block_events.append)
        await self.deliver(
            self.published(0),
            self.b.LevelChunkPacket(pos=self.b.ChunkPos(x=0, z=0), dimension_id=0,
                                    client_request_sub_chunk_limit=-1),
            self.b.UpdateBlockPacket(pos=self.b.BlockPos(x=1, y=64, z=1), runtime_id=42),
        )
        observed = self.bot.column_coverage(column)
        self.assertTrue(observed.announced)
        self.assertEqual((observed.requested_section_ys, observed.received_section_ys), ((), ()))
        self.assertEqual(len(block_events), 1)
        self.assertEqual(events, [])

        cancelled = asyncio.create_task(self.bot.wait_for_column_coverage(column, range(4, 6)))
        complete = asyncio.create_task(self.bot.wait_for_column_coverage(column, range(4, 6)))
        await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        await self.deliver(self.air(4))
        self.assertFalse(complete.done())
        self.assertTrue(self.bot.observed_block(position).is_air)
        await self.deliver(self.air(5, 5))
        snapshot = await complete
        self.assertEqual(snapshot.received_section_ys, (4, 5))
        self.assertEqual(snapshot.requested_section_ys, ())
        self.assertEqual(await self.bot.wait_for_column_coverage(column, range(4, 6), timeout=0), snapshot)
        self.assertEqual([(e.kind, e.coverage.received_section_ys) for e in events],
                         [("loaded", (4,)), ("coverage", (4, 5))])
        self.assertEqual(len(block_events), 1)

        leaving = asyncio.create_task(self.bot.wait_for_column_coverage(column, range(4, 7)))
        await asyncio.sleep(0)
        await self.deliver(self.published(1024), self.air(4))
        with self.assertRaisesRegex(ColumnCoverageInvalidated, "unloaded"):
            await leaving
        self.assertEqual(unload_observations, [(None, ())])
        self.assertIsNone(self.bot.observed_block(position))
        self.assertEqual(snapshot.received_section_ys, (4, 5))
        await self.deliver(self.published(0), self.air(4))
        self.assertEqual([e.kind for e in events], ["loaded", "coverage", "unloaded", "loaded"])
        self.assertEqual(self.bot.column_coverage(column).received_section_ys, (4,))
        self.assertEqual(self.transport.sent, [])
        unsubscribe()

    async def test_terrain_coverage_returns_ordered_fresh_snapshots(self):
        await self.connect()
        b = self.b
        first, second = (0, 0), (1, 0)
        selection = {first: range(0, 1)}
        wait = self.bot.wait_for_terrain_coverage(MappingProxyType(selection))
        selection.clear()
        expected = {second: range(4, 6), first: range(6, 7)}
        selection.update(expected)
        waiting = asyncio.create_task(wait)
        self.addCleanup(waiting.cancel)
        await asyncio.sleep(0)
        selection.clear()

        await self.deliver(
            self.published(0),
            b.LevelChunkPacket(pos=b.ChunkPos(x=1, z=0), dimension_id=0, client_request_sub_chunk_limit=-1),
            b.UpdateBlockPacket(pos=b.BlockPos(x=17, y=80, z=1), runtime_id=42),
            self.air(5, column=second, dimension=1),
        )
        self.assertEqual(self.transport.sent, [])
        await self.bot.request_sub_chunk((17, 80, 1))
        announced = self.bot.column_coverage(second)
        self.assertTrue(announced.announced)
        self.assertEqual(announced.requested_section_ys, (5,))
        self.assertEqual(announced.received_section_ys, ())
        self.assertEqual(self.bot.observed_block((17, 80, 1)).runtime_id, 42)
        await self.deliver(self.air(6))
        await asyncio.sleep(0)
        self.assertEqual(self.bot.column_coverage(first).received_section_ys, (6,))
        self.assertFalse(waiting.done())
        await self.deliver(self.air(7), self.air(4, column=second))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())

        await self.deliver(self.air(5, column=second))
        snapshots = await asyncio.wait_for(waiting, 1)
        self.assertIsInstance(snapshots, tuple)
        self.assertEqual(tuple(snapshot.column for snapshot in snapshots), tuple(expected))
        self.assertEqual(tuple(snapshot.dimension_id for snapshot in snapshots), (0, 0))
        self.assertEqual(tuple(snapshot.received_section_ys for snapshot in snapshots), ((4, 5), (6, 7)))
        self.assertEqual(snapshots[0].requested_section_ys, (5,))
        self.assertTrue(self.bot.observed_block((17, 80, 1)).is_air)
        self.assertEqual(await self.bot.wait_for_terrain_coverage(expected, timeout=0), snapshots)
        with self.assertRaises(FrozenInstanceError):
            snapshots[1].announced = True
        await self.deliver(self.air(8))
        self.assertEqual(snapshots[1].received_section_ys, (6, 7))
        self.assertEqual(self.bot.column_coverage(first).received_section_ys, (6, 7, 8))
        self.assertEqual(len(self.transport.sent), 1)

    async def test_terrain_coverage_invalidates_when_a_ready_column_is_evicted(self):
        await self.connect()
        first, second = (-2, 0), (0, 0)
        selected = {first: range(4, 5), second: range(6, 7)}
        await self.deliver(self.published(0))
        waiting = asyncio.create_task(self.bot.wait_for_terrain_coverage(selected))
        unaffected = asyncio.create_task(self.bot.wait_for_column_coverage(second, selected[second]))
        self.addCleanup(waiting.cancel)
        self.addCleanup(unaffected.cancel)
        await asyncio.sleep(0)

        await self.deliver(self.air(4, column=first))
        await asyncio.sleep(0)
        self.assertEqual(self.bot.column_coverage(first).received_section_ys, (4,))
        self.assertFalse(waiting.done())
        await self.deliver(self.published(32), self.air(6))
        with self.assertRaises(ColumnCoverageInvalidated) as invalidated:
            await asyncio.wait_for(waiting, 1)
        self.assertEqual(invalidated.exception.reason, "unloaded")
        self.assertEqual((await asyncio.wait_for(unaffected, 1)).received_section_ys, (6,))
        self.assertEqual(self.bot.column_coverage(first).received_section_ys, ())
        with self.assertRaises(ColumnCoverageInvalidated) as outside:
            await self.bot.wait_for_terrain_coverage({second: selected[second], first: selected[first]}, timeout=0)
        self.assertEqual(outside.exception.reason, "unloaded")

        await self.deliver(self.published(0))
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_terrain_coverage(selected, timeout=0)
        await self.deliver(self.air(4, column=first))
        snapshots = await self.bot.wait_for_terrain_coverage(selected, timeout=0)
        self.assertEqual(tuple(snapshot.received_section_ys for snapshot in snapshots), ((4,), (6,)))
        self.assertEqual(self.transport.sent, [])

    async def test_terrain_coverage_rejects_invalid_selections(self):
        await self.connect()
        with self.assertRaises(TypeError):
            await self.bot.wait_for_terrain_coverage([((0, 0), range(0, 1))])
        for selected in (
            {}, {(0, 0): range(0, 1), (True, 1): range(0, 1)}, {(0,): range(0, 1)}, {"0,0": range(0, 1)},
            {(0, 0): [0]}, {(0, 0): range(0)}, {(0, 0): range(0, 3, 2)}, {(0, 0): range(1, 0, -1)},
            {(0, 0): range(-129, 1)}, {(0, 0): range(0, 129)},
        ):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                await self.bot.wait_for_terrain_coverage(selected, timeout=0)
        self.assertEqual(self.transport.sent, [])

    async def test_terrain_coverage_timeout_and_cancellation_leave_other_waits_active(self):
        await self.connect()
        selected = {(0, 0): range(4, 5), (1, 0): range(6, 7)}
        waiting = asyncio.create_task(self.bot.wait_for_terrain_coverage(selected))
        cancelled = asyncio.create_task(self.bot.wait_for_terrain_coverage(selected))
        self.addCleanup(waiting.cancel)
        self.addCleanup(cancelled.cancel)
        await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_terrain_coverage(selected, timeout=0)
        await self.deliver(self.air(6, column=(1, 0)), self.air(4))
        snapshots = await asyncio.wait_for(waiting, 1)
        self.assertEqual(tuple(snapshot.column for snapshot in snapshots), tuple(selected))
        self.assertEqual(self.transport.sent, [])

    async def test_terrain_coverage_ends_on_dimension_packet_or_reader_failure(self):
        await self.connect()
        selected = {(0, 0): range(4, 5), (1, 0): range(6, 7)}
        waiting = asyncio.create_task(self.bot.wait_for_terrain_coverage(selected))
        self.addCleanup(waiting.cancel)
        await asyncio.sleep(0)
        await self.deliver(self.air(4), self.b.ChangeDimensionPacket(dimension_id=0))
        with self.assertRaises(ColumnCoverageInvalidated) as invalidated:
            await asyncio.wait_for(waiting, 1)
        self.assertEqual(invalidated.exception.reason, "dimension_changed")

        ended = asyncio.create_task(self.bot.wait_for_terrain_coverage(selected))
        self.addCleanup(ended.cancel)
        await asyncio.sleep(0)
        failure = EOFError("reader ended")
        self.transport.incoming.put_nowait(failure)
        with self.assertRaises(ConnectionError) as disconnected:
            await asyncio.wait_for(ended, 1)
        self.assertIs(disconnected.exception.__cause__, failure)
        with self.assertRaises(ConnectionError):
            await self.bot.wait_for_terrain_coverage(selected)
        self.assertEqual(self.transport.sent, [])
        with self.assertRaisesRegex(EOFError, "reader ended"):
            await self.bot.close()

    async def test_block_changes_capture_packet_order_and_filter_dimension_and_layer(self):
        await self.connect()
        b = self.b
        position = (1, 64, 1)
        pos = b.BlockPos(x=1, y=64, z=1)
        all_events, matching = [], []
        self.bot.subscribe_block_changes(all_events.append)
        unsubscribe = self.bot.subscribe_block_changes(matching.append, dimension_id=0, position=position, layer=0)
        await self.deliver(self.air(4))

        def change(runtime_id):
            return b.UpdateSubChunkNetworkBlockInfo(pos=pos, runtime_id=runtime_id)

        await self.deliver(b.UpdateSubChunkBlocksPacket(
            sub_chunk_block_position=pos,
            blocks_changed=b.UpdateSubChunkBlocksChangedInfo(
                standards=[change(21), change(22)], extras=[change(31), change(32)],
            ),
        ))
        self.assertEqual([e.after.runtime_id for e in all_events], [21, 22, 31, 32])
        self.assertEqual([e.after.runtime_id for e in matching], [21, 22])
        self.assertTrue(all_events[0].before.is_air)
        self.assertTrue(all_events[2].before.is_air)
        self.assertEqual((all_events[1].before.runtime_id, all_events[3].before.runtime_id), (21, 31))
        unsubscribe()
        await self.deliver(b.ChangeDimensionPacket(dimension_id=1), b.UpdateBlockPacket(pos=pos, runtime_id=33))
        self.assertEqual([e.after.runtime_id for e in matching], [21, 22])
        self.assertEqual(all_events[-1].dimension_id, 1)
        self.assertIsNone(all_events[-1].before)
        self.assertEqual(self.transport.sent, [])

    async def test_dimension_and_reader_end_invalidate_independent_waits(self):
        await self.connect()
        column = (0, 0)
        wait = asyncio.create_task(self.bot.wait_for_column_coverage(column, range(4, 5)))
        await asyncio.sleep(0)
        await self.deliver(self.b.ChangeDimensionPacket(dimension_id=1))
        with self.assertRaisesRegex(ColumnCoverageInvalidated, "dimension_changed"):
            await wait
        seen = await self.observe_bounds_during_vehicle_teardown()
        ended = asyncio.create_task(self.bot.wait_for_column_coverage(column, range(4, 5)))
        await asyncio.sleep(0)
        failure = EOFError("reader ended")
        self.transport.incoming.put_nowait(failure)
        with self.assertRaises(ConnectionError) as error:
            await ended
        self.assertIs(error.exception.__cause__, failure)
        self.assertEqual(self.bot.column_coverage(column).dimension_id, 1)
        self.assertIsNone(self.bot.dimension_bounds)
        self.assertEqual(self.transport.sent, [])
        with self.assertRaisesRegex(EOFError, "reader ended"):
            await self.bot.close()
        self.assertEqual(seen, [("unobserved", "reader_end", None)])
        await self.join_dimension_session(1, self.dimension_data(1, 16, 64))
        self.assertIsNone(self.bot.session_end)
        self.assertEqual(self.bot.dimension_bounds, DimensionBounds(1, 16, 80, "dimension_data"))

    async def test_auto_loader_and_manual_request_share_real_request_accounting(self):
        await self.connect(auto=True)
        b = self.b
        await self.deliver(self.published(0), b.LevelChunkPacket(
            pos=b.ChunkPos(x=0, z=0), dimension_id=0, client_request_sub_chunk_limit=2,
        ))
        await asyncio.sleep(0)
        observed = self.bot.column_coverage((0, 0))
        self.assertTrue(observed.announced)
        self.assertEqual(observed.requested_section_ys, (-4, -3))
        self.assertEqual(observed.received_section_ys, ())
        await self.bot.request_sub_chunk((1, 64, 1))
        requests = [self.bot.protocol.decode(next(read_batch(frame[1:]))) for frame in self.transport.sent]
        self.assertTrue(all(isinstance(packet, b.SubChunkRequestPacket) for packet in requests))
        self.assertEqual([[offset.y for offset in packet.sub_chunk_pos_offsets] for packet in requests], [[-4, -3], [4]])
        self.assertEqual(self.bot.column_coverage((0, 0)).requested_section_ys, (-4, -3, 4))
