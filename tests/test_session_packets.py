import asyncio
from importlib.util import find_spec
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from bedrock_protocol import EncodeError, TextPacketType, nbt

from endpilot import Bot
from endpilot.blocks import Blocks
from endpilot.compression import PacketConnection
from endpilot.protocol import Protocol, RawPacket, read_batch
from endpilot.qa import PacketTimeline
from endpilot.qa_terrain import _TerrainObserver
from test_scheduled_input import ManualClock, Wire


class SessionProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_protocol_is_sent_on_both_transports_without_discovery(self):
        for transport, connect, address, version in (
            ("nethernet", Bot.join_nethernet, ("http://127.0.0.1:20232",), 2193),
            ("raknet", Bot.join_raknet, ("127.0.0.1", 20232), 2225),
        ):
            with self.subTest(transport=transport):
                bot = Bot("Probe", protocol=version)
                self.addAsyncCleanup(bot.close)
                wire = Wire(bot.protocol)
                b = wire.protocol.binding
                for packet in (
                    b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
                    b.StartGamePacket(runtime_id=13, entity_id=27),
                    b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
                ):
                    wire.push(packet)
                with (
                    patch(f"endpilot.transport.{transport}.advertised_protocol", side_effect=TimeoutError) as advertise,
                    patch(f"endpilot.transport.{transport}.connect", return_value=wire),
                ):
                    await connect(bot, *address)
                advertise.assert_not_awaited()
                request, = (packet for packet in wire.sent if isinstance(packet, b.RequestNetworkSettingsPacket))
                self.assertEqual(request.client_network_version, version)
                await bot.close()
                self.assertTrue(wire.closed)

    async def test_raknet_protocol_discovery_reads_the_pong_and_propagates_timeout(self):
        from endpilot.transport.raknet import advertised_protocol

        host, port = "127.0.0.1", 20232
        pong = SimpleNamespace(data=b"MCPE;Local world;2193;1.26.0")
        with patch("raknet.asyncio.ping", return_value=pong) as ping:
            self.assertEqual(await advertised_protocol(host, port), 2193)
            ping.assert_awaited_once_with((host, port))
        with patch("raknet.asyncio.ping", side_effect=TimeoutError) as ping:
            with self.assertRaisesRegex(TimeoutError, "RakNet protocol discovery timed out"):
                await advertised_protocol(host, port)
            ping.assert_awaited_once_with((host, port))

    @unittest.skipUnless(find_spec("aiohttp"), "install the nethernet extra")
    async def test_nethernet_protocol_discovery_rejects_missing_metadata(self):
        from aiohttp import web

        from endpilot.transport.nethernet import advertised_protocol

        async def advertisement(_request):
            return web.json_response({})

        app = web.Application()
        app.router.add_get("/v1/join", advertisement)
        server = web.AppRunner(app)
        await server.setup()
        self.addAsyncCleanup(server.cleanup)
        await web.TCPSite(server, "127.0.0.1", 0).start()
        port = server.addresses[0][1]
        with self.assertRaisesRegex(ValueError, "has no valid protocol"):
            await advertised_protocol(f"http://127.0.0.1:{port}")


class SessionExpressionTests(unittest.IsolatedAsyncioTestCase):
    async def _connect(self, version):
        protocol = Protocol(version)
        b = protocol.binding
        wire = Wire(protocol)
        bot = Bot("Expression", protocol=version)
        timeline = PacketTimeline()
        self.addAsyncCleanup(bot.close)
        for packet in (
            b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
            b.StartGamePacket(runtime_id=13, entity_id=27),
            b.PlayStatusPacket(status=3),
        ):
            wire.push(packet)
        with patch("endpilot.transport.nethernet.connect", new=AsyncMock(return_value=wire)):
            await bot.join_nethernet("http://127.0.0.1:20232", timeline=timeline)
        return bot, wire, timeline

    async def test_expression_actions_encode_and_use_the_logged_in_player(self):
        bot, wire, timeline = await self._connect(2225)
        b = wire.protocol.binding
        own_uuid = bot.identity.uuid
        bot.identity.uuid = uuid4()
        beginning = len(wire.sent)
        self.assertIsNone(await bot.animate("WAKE_UP", 1.25))
        self.assertIsNone(await bot.swing("MINE"))
        for quiet in (False, True):
            self.assertIsNone(await bot.emote("4c8ae710-df2e-47cd-814d-cc7bf21a3d67", quiet))
        self.assertIsNone(await bot.change_skin("fixture-color", [7, 13, 23, 255]))

        alert, swing, speaking, muted, skin = wire.sent[beginning:]
        self.assertEqual((alert.action, alert.data, alert.runtime_id), (b.AnimatePacket.Action.WAKE_UP, 1.25, 13))
        self.assertEqual((swing.action, swing.swing_source, swing.hand_slot),
                         (b.AnimatePacket.Action.SWING, b.ActorSwingSource.MINE, b.HandSlot.MAINHAND))
        self.assertEqual((speaking.runtime_id, muted.runtime_id), (13, 13))
        self.assertEqual((speaking.flags, muted.flags), (0, 2))
        self.assertEqual(skin.uuid, own_uuid)
        self.assertEqual((skin.skin.skin_image.width, skin.skin.skin_image.height), (64, 32))
        self.assertEqual(skin.skin.skin_image.image_bytes, b"\x07\x0d\x17\xff" * (64 * 32))

        since = timeline.next_index
        received = asyncio.create_task(bot.packets.wait_for_packet(b.PlayerSkinPacket.ID, timeout=1))
        await asyncio.sleep(0)
        wire.push(skin)
        await received
        observed, = timeline.received("PlayerSkinPacket", since=since)
        decoded = bot.protocol.decode(observed.raw)
        self.assertEqual(decoded.skin.skin_image.image_bytes, skin.skin.skin_image.image_bytes)

        before_invalid = len(wire.sent)
        with self.assertRaises(EncodeError):
            await bot.animate("ROW_RIGHT")
        self.assertEqual(len(wire.sent), before_invalid)

    async def test_skin_defaults_to_opaque_black_and_rejects_invalid_colors(self):
        black = b"\0\0\0\xff"
        color = b"\x07\x0d\x17\xff"
        for version in (2193, 2225):
            with self.subTest(protocol=version):
                bot, wire, _ = await self._connect(version)
                before = len(wire.sent)
                self.assertIsNone(await bot.change_skin("default"))
                self.assertIsNone(await bot.change_skin("color", list(color)))
                packets = wire.sent[before:]
                self.assertEqual([packet.skin.id for packet in packets], ["default", "color"])
                for packet, pixel in zip(packets, (black, color), strict=True):
                    image = packet.skin.skin_image
                    self.assertEqual((image.width, image.height), (64, 32))
                    self.assertEqual(image.image_bytes, pixel * (64 * 32))
                with self.assertRaises(ValueError):
                    await bot.change_skin("wrong-length", [1, 2, 3])
                with self.assertRaises(ValueError):
                    await bot.change_skin("empty", [])
                self.assertEqual(len(wire.sent), before + 2)

    async def test_synchronized_position_and_predicted_feet_height(self):
        bot, wire, _ = await self._connect(2225)
        b = wire.protocol.binding
        wire.push(b.MovePlayerPacket(player_id=13, pos=b.Vec3(x=12.5, y=-58.38, z=-3.5)))

        x, y, z = await bot.wait_for_position_sync(timeout=1)
        self.assertEqual((x, z), (12.5, -3.5))
        self.assertAlmostEqual(y, -58.38, places=4)
        x, y, z = bot.movement_state.feet_position
        self.assertEqual((x, z), (12.5, -3.5))
        self.assertAlmostEqual(y, -60, places=4)

    async def test_position_sync_times_out_without_a_position_packet(self):
        bot, _, _ = await self._connect(2225)
        with self.assertRaises(TimeoutError):
            await bot.wait_for_position_sync(timeout=0)

    async def test_missed_swing_returns_the_submitted_input_tick(self):
        bot, wire, _ = await self._connect(2225)
        b = wire.protocol.binding
        wire.push(b.MovePlayerPacket(player_id=13, pos=b.Vec3(x=12.5, y=81.62, z=-3.5)))
        await bot.wait_for_position_sync(timeout=1)
        start = len(wire.sent)

        tick = await bot.missed_swing()

        animation, frame = wire.sent[start:]
        self.assertEqual((animation.runtime_id, animation.swing_source), (13, b.ActorSwingSource.ATTACK))
        self.assertIn(b.PlayerAuthInputPacket.InputData.MISSED_SWING, frame.input_data)
        self.assertEqual(tick, frame.client_tick)


class SessionTerrainTests(unittest.IsolatedAsyncioTestCase):
    def _terrain_fixture(self, dimension_id):
        protocol = Protocol(2225)
        transport = SimpleNamespace(send=AsyncMock())
        packets = PacketConnection(transport, protocol, SimpleNamespace(compression_algorithm=65535, compression_threshold=0))
        blocks = Blocks(packets, None, None, dimension_id=dimension_id)
        blocks.start()
        self.addCleanup(blocks.close)
        terrain = _TerrainObserver(blocks)
        self.addAsyncCleanup(terrain.close)

        def deliver(packet):
            packets._notify(packets._subscribers, protocol.encode(packet))

        return protocol, transport, blocks, deliver

    async def test_login_dimension_change_keeps_global_metadata_and_current_terrain(self):
        protocol = Protocol(2225)
        b = protocol.binding
        wire = Wire(protocol)
        bot = Bot("LoginTerrain", protocol=2225)
        timeline = PacketTimeline()
        self.addAsyncCleanup(bot.close)
        requests = asyncio.Queue()

        async def send(batch):
            for raw in read_batch(batch):
                if raw.packet_id == b.SubChunkRequestPacket.ID:
                    requests.put_nowait(protocol.decode(raw))

        wire.send = send
        for packet in (
            b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
            b.StartGamePacket(runtime_id=4, entity_id=17),
            b.DimensionDataPacket(dimension_definitions={
                "test:height": b.DimensionDataPacket.DimensionDefinition(dimension_type=1, min_y=32, height_range=64),
            }),
            b.NetworkChunkPublisherUpdatePacket(position=b.BlockPos(x=0, y=0, z=0), radius=0),
            b.LevelChunkPacket(pos=b.ChunkPos(x=0, z=0), dimension_id=0, client_request_sub_chunk_limit=2),
            b.ChangeDimensionPacket(dimension_id=1),
            b.LevelChunkPacket(pos=b.ChunkPos(x=2, z=0), dimension_id=1, client_request_sub_chunk_limit=2),
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        ):
            wire.push(packet)

        with patch("endpilot.transport.nethernet.connect", return_value=wire):
            await bot.join_nethernet(
                "http://127.0.0.1:20232", timeline=timeline, retain_initial_chunks=True, load_announced_terrain=True,
            )
        request = await asyncio.wait_for(requests.get(), 1)
        self.assertEqual((request.dimension_type, request.center_pos.x, request.center_pos.z), (1, 2, 0))
        self.assertEqual([offset.y for offset in request.sub_chunk_pos_offsets], [2, 3])
        await asyncio.sleep(0)
        self.assertTrue(requests.empty())
        columns = timeline.received("LevelChunkPacket")
        self.assertEqual([protocol.decode(column.raw).dimension_id for column in columns], [0, 1])

    async def test_column_loads_once_and_concurrent_unknown_queries_send_nothing(self):
        protocol, transport, blocks, deliver = self._terrain_fixture(dimension_id=3)
        b = protocol.binding
        deliver(b.DimensionDataPacket(dimension_definitions={
            "test:height": b.DimensionDataPacket.DimensionDefinition(dimension_type=3, min_y=-32, height_range=64),
        }))
        column = b.LevelChunkPacket(
            pos=b.ChunkPos(x=0, z=-1), dimension_id=3, client_request_sub_chunk_limit=2,
        )
        deliver(column)
        deliver(column)
        await asyncio.sleep(0)
        sent, = transport.send.await_args_list
        request = protocol.decode(next(read_batch(sent.args[0])))
        self.assertIsInstance(request, b.SubChunkRequestPacket)
        self.assertEqual(request.dimension_type, 3)
        self.assertEqual((request.center_pos.x, request.center_pos.y, request.center_pos.z), (0, 0, -1))
        self.assertEqual([offset.y for offset in request.sub_chunk_pos_offsets], [-2, -1])

        async def read(pos):
            return blocks.observed_block(pos)

        unknown = await asyncio.gather(read((12, -31, -4)), read((13, -30, -6)))
        self.assertEqual(unknown, [None, None])
        transport.send.assert_awaited_once()

        deliver(b.UpdateBlockPacket(pos=b.BlockPos(x=12, y=-31, z=-4), layer=1, runtime_id=42))
        deliver(column)
        await asyncio.sleep(0)
        self.assertIsNone(blocks.observed_block((12, -31, -4)))
        self.assertEqual(blocks.observed_block((12, -31, -4), layer=1).runtime_id, 42)
        transport.send.assert_awaited_once()

        deliver(b.LevelChunkPacket(pos=b.ChunkPos(x=1, z=0), dimension_id=3, sub_chunks_count=1,
                                   serialized_chunk=b"\x09\x01\xfe\x01\x54"))
        self.assertEqual(blocks.observed_block((17, -32, 3)).runtime_id, 42)
        transport.send.assert_awaited_once()

        # Switch dimensions before the queued request for a second old column sends.
        deliver(b.LevelChunkPacket(pos=b.ChunkPos(x=1, z=-1), dimension_id=3,
                                   client_request_sub_chunk_limit=2))
        deliver(b.ChangeDimensionPacket(dimension_id=1))
        deliver(b.LevelChunkPacket(pos=b.ChunkPos(x=0, z=-1), dimension_id=1,
                                   client_request_sub_chunk_limit=-1))
        await asyncio.sleep(0)
        self.assertEqual(transport.send.await_count, 2)
        new_request = protocol.decode(next(read_batch(transport.send.await_args.args[0])))
        self.assertEqual(new_request.dimension_type, 1)
        self.assertEqual([offset.y for offset in new_request.sub_chunk_pos_offsets], list(range(8)))
        self.assertIsNone(blocks.observed_block((12, -31, -4), layer=1))

    async def test_published_column_reloads_on_return_and_only_adds_new_sections(self):
        protocol, transport, blocks, deliver = self._terrain_fixture(dimension_id=0)
        b = protocol.binding
        pos = (6, -63, 5)

        def publish(x, z):
            deliver(b.NetworkChunkPublisherUpdatePacket(position=b.BlockPos(x=x, y=0, z=z), radius=64))

        def announce(limit):
            deliver(b.LevelChunkPacket(pos=b.ChunkPos(x=0, z=0), dimension_id=0,
                                       client_request_sub_chunk_limit=limit))

        def receive_section(runtime_id):
            entry = b.SubChunkPacket.SubChunkPacketData(
                sub_chunk_pos_offset=b.SubChunkPacket.SubChunkPosOffset(x=0, y=-4, z=0),
                result=b.SubChunkPacket.SubChunkRequestResult.SUCCESS,
                serialized_sub_chunk=bytes((9, 1, 252, 1, runtime_id * 2)),
            )
            deliver(b.SubChunkPacket(dimension_type=0, center_pos=b.SubChunkPos(x=0, y=0, z=0),
                                     sub_chunk_data=[entry]))

        def offsets():
            requests = [protocol.decode(next(read_batch(call.args[0]))) for call in transport.send.await_args_list]
            return [[offset.y for offset in request.sub_chunk_pos_offsets] for request in requests]

        publish(0, 0)
        announce(1)
        await asyncio.sleep(0)
        self.assertEqual(offsets(), [[-4]])
        receive_section(42)
        deliver(b.UpdateBlockPacket(pos=b.BlockPos(x=6, y=-63, z=5), layer=1, runtime_id=43))
        self.assertEqual(blocks.observed_block(pos).runtime_id, 42)
        self.assertEqual(blocks.observed_block(pos, layer=1).runtime_id, 43)

        announce(1)
        announce(3)
        await asyncio.sleep(0)
        self.assertEqual(offsets(), [[-4], [-3, -2]])
        self.assertEqual(blocks.observed_block(pos).runtime_id, 42)

        announce(4)  # Queue the fourth section while the column is still published.
        publish(1000, 1000)
        self.assertIsNone(blocks.observed_block(pos))
        self.assertIsNone(blocks.observed_block(pos, layer=1))
        receive_section(51)
        deliver(b.UpdateBlockPacket(pos=b.BlockPos(x=6, y=-63, z=5), runtime_id=52))
        announce(3)
        publish(0, 0)
        self.assertIsNone(blocks.observed_block(pos))

        announce(1)
        await asyncio.sleep(0)
        self.assertEqual(offsets(), [[-4], [-3, -2], [-4]])
        receive_section(53)
        self.assertEqual(blocks.observed_block(pos).runtime_id, 53)
        layer = blocks.observed_block(pos, layer=1)
        self.assertTrue(layer.is_air)
        self.assertIsNone(layer.runtime_id)


class SessionRecipeTests(unittest.IsolatedAsyncioTestCase):
    async def test_replaced_login_recipe_keys_are_discarded_and_rejoin_gets_a_fresh_catalog(self):
        bot = Bot("RecipeRejoin", protocol=2225)
        self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding

        async def join(*numbers):
            wire = Wire(bot.protocol)
            entries = [b.ShapelessRecipePayload(recipe_id="test:one", net_id=number, tag="crafting_table")
                       for number in numbers]
            for packet in (
                b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
                b.StartGamePacket(runtime_id=1, entity_id=1),
                b.CraftingDataPacket(shapeless_recipes=entries),
                b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
            ):
                wire.push(packet)
            with patch("endpilot.transport.nethernet.connect", return_value=wire):
                await bot.join_nethernet("http://127.0.0.1:20232")
            return wire

        old = await join(10, 12)
        self.assertIsNone(bot.get_recipe(10))
        latest = bot.get_recipe("test:one")
        self.assertEqual((latest.net_id, latest.kind, latest.tag), (12, "shapeless_recipes", "crafting_table"))
        self.assertIs(bot.get_recipe(12), latest)
        self.assertIsNone(bot.get_recipe("12"))
        await bot.close()
        self.assertTrue(old.closed)

        await join(19)
        self.assertIsNone(bot.get_recipe(10))
        self.assertIsNone(bot.get_recipe(12))
        self.assertEqual(bot.get_recipe("test:one").net_id, 19)


class PacketTimelineTests(unittest.TestCase):
    def test_records_protocol_indices_ticks_and_native_payloads(self):
        timeline = PacketTimeline()
        legacy, stable = Protocol(2193), Protocol(2225)
        old, new = legacy.binding, stable.binding
        player_uuid = UUID(int=7)

        def player_list(binding, **fields):
            kind = binding.PlayerListPacket
            return kind(entries=[kind.AddEntry(
                action=binding.PlayerListPacketType.ADD, uuid=player_uuid,
                id=44, name="Peer", **fields,
            )])

        packets = (
            (legacy, player_list(old)),
            (stable, new.AddPlayerPacket(
                uuid=player_uuid, name="Peer", runtime_id=97, pos=new.Vec3(x=2, y=80, z=-3),
                player_game_type=new.GameType.CREATIVE,
            )),
            (stable, new.RemoveActorPacket(entity_id=44)),
            (stable, player_list(new, play_fab_id="stable-peer")),
            (stable, new.BlockActorDataPacket(
                pos=new.BlockPos(x=7, y=80, z=-2), data=nbt.CompoundTag({
                    "id": nbt.StringTag("Sign"), "raw": nbt.ByteArrayTag([1, 255]),
                    "rows": nbt.ListTag([nbt.CompoundTag({"text": nbt.StringTag("row")})]),
                }),
            )),
            (stable, new.LevelChunkPacket(serialized_chunk=b"\x00\x85\xff")),
            (stable, new.TextPacket(localize=True, body=new.TextPacket.MessageAndParams(
                type=new.TextPacketType.TRANSLATE, message="example.key", params=["Peer"],
            ))),
        )
        for tick, (protocol, packet) in enumerate(packets, start=10):
            timeline._record(protocol, protocol.encode(packet), tick=tick)

        observed = timeline.received()
        self.assertEqual([(entry.index, entry.name) for entry in observed], [
            (0, "PlayerListPacket"), (1, "AddPlayerPacket"), (2, "RemoveActorPacket"),
            (3, "PlayerListPacket"), (4, "BlockActorDataPacket"), (5, "LevelChunkPacket"), (6, "TextPacket"),
        ])
        self.assertEqual([entry.protocol for entry in observed], [2193] + [2225] * 6)
        self.assertEqual([entry.tick for entry in observed], list(range(10, 17)))
        self.assertEqual(timeline.received(since=2), observed[2:])
        self.assertEqual(timeline.received("PlayerListPacket", since=3), (observed[3],))
        native = [Protocol(entry.protocol).decode(entry.raw) for entry in observed]

        legacy_player, = native[0].entries
        self.assertEqual((legacy_player.uuid, legacy_player.id, legacy_player.action),
                         (player_uuid, 44, old.PlayerListPacketType.ADD))
        self.assertFalse(hasattr(legacy_player, "play_fab_id"))
        actor = native[1]
        self.assertEqual((actor.uuid, actor.runtime_id, actor.pos.x, actor.pos.y, actor.pos.z),
                         (player_uuid, 97, 2.0, 80.0, -3.0))
        self.assertEqual(actor.player_game_type, new.GameType.CREATIVE)
        self.assertEqual(native[2].entity_id, 44)
        self.assertEqual(native[3].entries[0].play_fab_id, "stable-peer")
        sign = native[4]
        self.assertEqual((sign.pos.x, sign.pos.y, sign.pos.z), (7, 80, -2))
        self.assertEqual(sign.data["id"].value, "Sign")
        self.assertEqual(list(sign.data["raw"].value), [1, 255])
        self.assertEqual(sign.data["rows"][0]["text"].value, "row")
        self.assertEqual(native[5].serialized_chunk, b"\x00\x85\xff")
        body = native[6].body
        self.assertEqual((body.type, body.message, list(body.params)),
                         (new.TextPacketType.TRANSLATE, "example.key", ["Peer"]))

    def test_opaque_packet_names_stay_consistent_through_queries_and_eviction(self):
        protocol = Protocol(2225)
        binding = protocol.binding
        timeline = PacketTimeline(max_kept=2)
        request = binding.ModalFormRequestPacket
        readable = protocol.encode(request(form_id=19, form_json='{"type":"modal"}'))
        unreadable = protocol.encode(request(form_id=20, form_json='{"type":"modal"}'))
        opaque = f"#{request.ID}"
        decode = Protocol.decode

        def decode_sample(codec, raw):
            if raw == unreadable:
                raise ValueError("controlled decode failure")
            return decode(codec, raw)

        with patch.object(Protocol, "decode", autospec=True, side_effect=decode_sample) as decoded:
            timeline._record(protocol, RawPacket(1023, b"\x01"), tick=22)
            timeline._record(protocol, unreadable, tick=24)
            self.assertEqual([(entry.name, entry.tick) for entry in timeline.received()],
                             [("#1023", 22), (opaque, 24)])
            self.assertEqual(timeline.received(opaque)[0].raw, unreadable)
            unknown, malformed = timeline.received()
            self.assertEqual(unknown.raw, RawPacket(1023, b"\x01"))
            self.assertEqual(unknown._decode_error, "no packet class for this id")
            self.assertEqual(malformed._decode_error, "ValueError: controlled decode failure")
            self.assertEqual(timeline.received(opaque), (malformed,))
            self.assertEqual(timeline.received(request.__name__), ())
            decoded.assert_called_once_with(protocol, unreadable)

            timeline._record(protocol, readable, tick=25)
            self.assertEqual(timeline.received_count(request.__name__), 1)
            observed, = timeline.received(request.__name__)
            self.assertEqual(observed._decoded.form_id, 19)
            self.assertEqual(timeline.received(opaque), (malformed,))

            timeline._record(protocol, protocol.encode(binding.PlayStatusPacket()))
            for name in ("#1023", opaque):
                self.assertEqual(timeline.received(name), ())
                self.assertEqual(timeline.received_count(name), 1)
            self.assertEqual(decoded.call_count, 3)


class JoinedSessionTests(unittest.IsolatedAsyncioTestCase):
    def deliver_spawn(self, wire, *before_spawn):
        b = wire.protocol.binding
        wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        wire.push(b.StartGamePacket(runtime_id=4, entity_id=17))
        for packet in before_spawn:
            wire.push(packet)
        wire.push(b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))

    async def connect(self, bot, wire, *, timeline=None):
        with patch("endpilot.transport.nethernet.connect", return_value=wire):
            await bot.join_address("http://127.0.0.1:20232", timeline=timeline)

    async def deliver_live(self, bot, wire, packet):
        waiter = asyncio.create_task(bot.packets.wait_for_packet(packet.ID, timeout=1))
        await asyncio.sleep(0)
        wire.push(packet)
        await waiter

    async def test_inventory_move_waits_for_the_matching_native_verdict(self):
        bot = Bot("Inventory", protocol=2225)
        self.addAsyncCleanup(bot.close)
        wire = Wire(bot.protocol)
        b = wire.protocol.binding
        carried = b.SerializedNetworkItemStackDescriptor(id=306, stack_size=3, net_id_variant=71)
        self.deliver_spawn(wire, b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=3, item=carried))
        await self.connect(bot, wire)
        await self.deliver_live(bot, wire, b.ContainerOpenPacket(container_id=0, type=b.ContainerType.INVENTORY))
        self.assertEqual(bot.open_window.container_type, "INVENTORY")

        start = len(wire.sent)
        moving = asyncio.create_task(bot.move_inventory_item(3, 5, 2, timeout=1))
        async with asyncio.timeout(1):
            while len(wire.sent) == start:
                await asyncio.sleep(0)
        request, = wire.sent[-1].requests
        action, = request.actions
        self.assertIsInstance(action, b.ItemStackRequestCereal.PlaceActionData)
        self.assertEqual((action.source.slot, action.source.net_id_variant, action.destination.slot, action.amount),
                         (3, 71, 5, 2))
        await self.deliver_live(bot, wire, b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=b.ItemStackRequestId(raw_id=-97), result=b.ItemStackNetResult.ERROR,
        )]))
        self.assertFalse(moving.done())
        self.assertEqual(bot.inventory_slots[3].stack_size, 3)
        self.assertNotIn(5, bot.inventory_slots)

        corrections = [b.ItemStackResponseSlotInfo(
            requested_slot=slot, slot=slot, amount=amount, item_stack_net_id=b.ItemStackNetId(raw_id=net_id),
            custom_name=b.RedactableString(unredacted_string=""), durability_correction=0,
        ) for slot, amount, net_id in ((3, 1, 71), (5, 2, 72))]
        wire.push(b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request.client_request_id, result=b.ItemStackNetResult.SUCCESS,
            containers=[b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName.COMBINED_HOTBAR_AND_INVENTORY_CONTAINER),
                slots=corrections,
            )],
        )]))
        verdict = await moving
        self.assertEqual(verdict.client_request_id.raw_id, request.client_request_id.raw_id)
        self.assertEqual(verdict.result, b.ItemStackNetResult.SUCCESS)
        self.assertEqual((bot.inventory_slots[3].id, bot.inventory_slots[3].stack_size,
                          bot.inventory_slots[5].id, bot.inventory_slots[5].stack_size,
                          bot.inventory_slots[5].net_id_variant), (306, 1, 306, 2, 72))

    async def test_container_window_and_slots_follow_open_close_and_reopen_packets(self):
        bot = Bot("Container", protocol=2225)
        wire = Wire(Protocol(2225))
        b = wire.protocol.binding
        self.addAsyncCleanup(bot.close)
        self.deliver_spawn(wire)
        await self.connect(bot, wire)

        container_id = 5
        position = b.BlockPos(x=7, y=100, z=9)
        item = b.SerializedNetworkItemStackDescriptor(id=19, stack_size=1)
        content = b.InventoryContentPacket(inventory_id=container_id, slots=[item])
        self.assertIsNone(bot.open_window)
        self.assertIsNone(bot.container_slots)
        for entity_unique_id in (-4032, 8125, -1):
            with self.subTest(entity_unique_id=entity_unique_id):
                await self.deliver_live(bot, wire, b.ContainerOpenPacket(
                    container_id=container_id, type=b.ContainerType.CONTAINER,
                    pos=position, entity_unique_id=entity_unique_id,
                ))
                window = bot.open_window
                self.assertEqual((window.container_id, window.container_type, window.position, window.entity_unique_id),
                                 (container_id, "CONTAINER", (7, 100, 9), entity_unique_id))
                self.assertIsNone(bot.container_slots)
                await self.deliver_live(bot, wire, content)
                self.assertIs(bot.open_window, window)
                slot = bot.container_slots[0]
                self.assertEqual((slot.id, slot.stack_size, slot.aux_value, slot.net_id_variant), (19, 1, 0, None))
                await self.deliver_live(bot, wire, b.ContainerClosePacket(
                    container_id=container_id, container_type=b.ContainerType.CONTAINER,
                ))
                self.assertIsNone(bot.open_window)
                self.assertIsNone(bot.container_slots)

    async def test_equipment_view_and_selection_follow_player_packet_order(self):
        bot = Bot("Equipment", protocol=2225)
        wire = Wire(Protocol(2225))
        b = wire.protocol.binding
        self.addAsyncCleanup(bot.close)
        stack = b.SerializedNetworkItemStackDescriptor(id=1, stack_size=2, net_id_variant=71)
        air = b.SerializedNetworkItemStackDescriptor()

        def equipment(actor, slot):
            return b.MobEquipmentPacket(runtime_id=actor, selected_slot=slot, slot=slot,
                                        container_id=b.ContainerID.INVENTORY, item=air)

        self.deliver_spawn(
            wire,
            b.ItemRegistryPacket(items=[b.ItemData(name="minecraft:stone", id=1)]),
            equipment(4, 2), equipment(33, 8),
            b.PlayerHotbarPacket(selected_slot=3, container_id=b.ContainerID.INVENTORY, should_select_slot=True),
            b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=5, item=stack),
            b.InventoryContentPacket(inventory_id=b.ContainerID.ARMOR, slots=[stack, air]),
            b.InventorySlotPacket(inventory_id=b.ContainerID.OFFHAND, slot=0, item=air),
        )
        await self.connect(bot, wire)

        self.assertEqual(bot._inventory.selected_slot, 3)
        self.assertEqual({idx: slot.id for idx, slot in bot.armor_slots.items()}, {0: 1, 1: 0})
        self.assertEqual(bot.offhand_item.stack_size, 0)
        self.assertNotIn(1, bot.inventory_slots)
        await self.deliver_live(bot, wire, equipment(4, 6))
        await self.deliver_live(bot, wire, equipment(33, 7))
        await self.deliver_live(bot, wire, b.PlayerHotbarPacket(
            selected_slot=8, container_id=b.ContainerID.INVENTORY, should_select_slot=False,
        ))
        self.assertEqual(bot._inventory.selected_slot, 6)

        async def cancel_selection():
            wire.before_send = None
            await self.deliver_live(bot, wire, equipment(4, 6))

        wire.before_send = cancel_selection
        self.assertIsNone(await bot.select_hotbar(5))
        sent, = (packet for packet in wire.sent if isinstance(packet, b.MobEquipmentPacket))
        self.assertEqual((sent.runtime_id, sent.selected_slot, sent.item.id, sent.item.stack_size,
                          sent.item.net_id_variant), (4, 5, 1, 2, 71))
        self.assertFalse(any(isinstance(packet, b.PlayerHotbarPacket) for packet in wire.sent))
        self.assertEqual(bot._inventory.selected_slot, 6)

    async def test_hotbar_writeback_uses_submission_order_for_the_next_drop(self):
        for outcome, expected_slot in (("success", 2), ("server correction", 3), ("failed", 1), ("cancelled", 1)):
            with self.subTest(outcome=outcome):
                bot = Bot("ConcurrentSelection", protocol=2225)
                wire = Wire(Protocol(2225))
                b = wire.protocol.binding
                self.deliver_spawn(wire, *(b.InventorySlotPacket(
                    inventory_id=b.ContainerID.INVENTORY, slot=slot,
                    item=b.SerializedNetworkItemStackDescriptor(id=slot, stack_size=2, net_id_variant=70 + slot),
                ) for slot in (1, 2, 3)))
                await self.connect(bot, wire)
                first_emitted = asyncio.Event()
                release_first = asyncio.Event()
                second_entered = asyncio.Event()
                send = wire.send

                async def controlled_send(batch):
                    packet = wire.protocol.decode(next(read_batch(batch)))
                    if isinstance(packet, b.MobEquipmentPacket) and packet.selected_slot == 2:
                        second_entered.set()
                        if outcome == "failed":
                            raise OSError("selection could not be submitted")
                        if outcome == "cancelled":
                            await asyncio.Future()
                    await send(batch)
                    if isinstance(packet, b.MobEquipmentPacket) and packet.selected_slot == 1:
                        first_emitted.set()
                        await release_first.wait()

                wire.send = controlled_send
                first = asyncio.create_task(bot.select_hotbar(1))
                second = None
                try:
                    await asyncio.wait_for(first_emitted.wait(), 1)
                    second = asyncio.create_task(bot.select_hotbar(2))
                    await asyncio.wait_for(second_entered.wait(), 1)
                    if outcome == "cancelled":
                        second.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await second
                    elif outcome == "failed":
                        with self.assertRaisesRegex(OSError, "could not be submitted"):
                            await second
                    else:
                        self.assertIsNone(await second)
                        self.assertEqual(bot._inventory.selected_slot, 2)
                    if outcome == "server correction":
                        await self.deliver_live(bot, wire, b.PlayerHotbarPacket(
                            selected_slot=3, container_id=b.ContainerID.INVENTORY, should_select_slot=True,
                        ))
                    release_first.set()
                    self.assertIsNone(await first)
                    await bot.drop_selected_item(False)
                    selections = [packet.selected_slot for packet in wire.sent
                                  if isinstance(packet, b.MobEquipmentPacket)]
                    self.assertEqual(selections, [1, 2] if outcome in ("success", "server correction") else [1])
                    tx, = (packet.variant_transaction for packet in wire.sent
                           if isinstance(packet, b.InventoryTransactionPacket))
                    _, carried = tx.transaction.actions
                    self.assertEqual((carried.slot, carried.from_item_descriptor.net_id_variant),
                                     (expected_slot, 70 + expected_slot))
                finally:
                    release_first.set()
                    if second is not None:
                        second.cancel()
                        await asyncio.gather(second, return_exceptions=True)
                    await asyncio.gather(first, return_exceptions=True)
                    await bot.close()

    async def test_fifth_executed_tick_keeps_cursor_after_late_announcement(self):
        clock = ManualClock()
        bot = Bot("ClientClock", protocol=2225)
        wire = Wire(Protocol(2225))
        b = wire.protocol.binding
        self.addAsyncCleanup(bot.close)
        timeline = PacketTimeline()
        with clock.patch_scheduler():
            joining = asyncio.create_task(self.connect(bot, wire, timeline=timeline))
            await clock.wake_at(170)  # The scheduled slot at 50 expired during the late wake.
            await clock.wake_at(200)
            await clock.wake_at(250)
            self.assertFalse(joining.done())
            self.deliver_spawn(wire)
            await joining
            self.assertEqual(await bot.wait_ticks(0), 2)

            ready = asyncio.create_task(bot.wait_ticks(5))
            await asyncio.sleep(0)
            for step in (300, 350, 400, 450):
                await clock.wake_at(step)
                self.assertFalse(ready.done())
            announcement = b.TextPacket(body=b.TextPacket.MessageOnly(
                type=TextPacketType.TEXT_OBJECT,
                message='{"rawtext":[{"translate":"%multiplayer.player.joined","with":["LatePlayer"]}]}',
            ))
            await self.deliver_live(bot, wire, announcement)
            self.assertFalse(ready.done())
            await clock.wake_at(500)
            await asyncio.wait_for(ready, timeout=1)
            self.assertEqual(await bot.wait_ticks(0), 7)
            cursor = timeline.next_index
            joined, = timeline.received("TextPacket")
            self.assertEqual((joined.index, joined.tick), (cursor - 1, 6))
            self.assertEqual(wire.protocol.decode(joined.raw).body.message, announcement.body.message)

            text = b.TextPacket(body=b.TextPacket.AuthorAndMessage(
                type=TextPacketType.CHAT, author="LatePlayer", message="after ready",
            ))
            await self.deliver_live(bot, wire, text)
            await clock.wake_at(550)
            self.assertEqual(bot.get_tick(), 8)
            observed, = timeline.received("TextPacket", since=cursor)
            self.assertEqual(observed.tick, 7)
            body = wire.protocol.decode(observed.raw).body
            self.assertEqual((body.type, body.author, body.message),
                             (TextPacketType.CHAT, "LatePlayer", "after ready"))
            self.assertFalse(any(isinstance(packet, b.PlayerAuthInputPacket) for packet in wire.sent))
            await bot.close()

    async def test_tick_wait_stops_on_close_disconnect_packet_and_reader_failure(self):
        for ended in ("close", "disconnect", "wire"):
            with self.subTest(ended=ended):
                clock = ManualClock()
                bot = Bot("ClientClock", protocol=2225)
                wire = Wire(Protocol(2225))
                self.deliver_spawn(wire)
                with clock.patch_scheduler():
                    await self.connect(bot, wire)
                    ready = asyncio.create_task(bot.wait_ticks(5))
                    await asyncio.sleep(0)
                    if ended == "close":
                        await bot.close()
                    elif ended == "disconnect":
                        wire.push(wire.protocol.binding.DisconnectPacket())
                    else:
                        wire.incoming.put_nowait(OSError("wire lost"))
                    with self.assertRaisesRegex(ConnectionError, "clock closed"):
                        await asyncio.wait_for(ready, timeout=1)
                    if ended == "wire":
                        with self.assertRaisesRegex(OSError, "wire lost"):
                            await bot.close()
                    else:
                        await bot.close()
                    self.assertTrue(wire.closed)

                fresh_clock = ManualClock()
                fresh_wire = Wire(wire.protocol)
                self.deliver_spawn(fresh_wire)
                with fresh_clock.patch_scheduler():
                    await self.connect(bot, fresh_wire)
                    self.assertEqual(await bot.wait_ticks(0), 0)
                    await fresh_clock.wake_at(50)
                    self.assertEqual(await bot.wait_ticks(0), 1)
                    await bot.close()

    async def test_leaving_during_loading_drains_the_old_connection_before_rejoining(self):
        clock = ManualClock()
        bot = Bot("Loading", protocol=2225)
        wire = Wire(Protocol(2225))
        fresh = Wire(wire.protocol)
        b = wire.protocol.binding
        wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        wire.push(b.StartGamePacket(runtime_id=4, entity_id=17))
        self.deliver_spawn(fresh)
        closing, allow_close = asyncio.Event(), asyncio.Event()
        original_close = wire.close

        async def delayed_close():
            closing.set()
            await allow_close.wait()
            await original_close()

        wire.close = delayed_close
        self.addAsyncCleanup(bot.close)
        self.addCleanup(allow_close.set)
        with clock.patch_scheduler():
            joining = asyncio.create_task(self.connect(bot, wire))
            await clock.wake_at(50)
            self.assertFalse(joining.done())
            with self.assertRaisesRegex(RuntimeError, "not joined"):
                _ = bot.packets
            self.assertEqual(bot.get_tick(), 1)
            self.assertEqual(await bot.wait_ticks(0), 1)
            loaded_ticks = asyncio.create_task(bot.wait_ticks(1))
            expired_ticks = asyncio.create_task(bot.wait_ticks(3))
            await asyncio.sleep(0)
            await clock.wake_at(100)
            self.assertEqual(await asyncio.wait_for(loaded_ticks, 1), 2)
            self.assertFalse(expired_ticks.done())

            leaving = asyncio.create_task(bot.close())
            await asyncio.wait_for(closing.wait(), 1)
            try:
                self.assertFalse(leaving.done())
                with self.assertRaisesRegex(ConnectionError, "clock closed"):
                    await asyncio.wait_for(expired_ticks, 1)
            finally:
                allow_close.set()
            await asyncio.wait_for(leaving, 1)
            with self.assertRaises(asyncio.CancelledError):
                await joining
            self.assertTrue(wire.closed)
            with self.assertRaisesRegex(RuntimeError, "no active connection"):
                bot.get_tick()

        fresh_clock = ManualClock()
        with fresh_clock.patch_scheduler():
            await self.connect(bot, fresh)
            self.assertEqual(bot.get_tick(), 0)
            await fresh_clock.wake_at(50)
            self.assertEqual(await bot.wait_ticks(0), 1)
            await bot.close()
        self.assertTrue(fresh.closed)
