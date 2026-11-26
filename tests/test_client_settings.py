import asyncio
import base64
import json
import struct
import unittest
from unittest.mock import patch

from endpilot import Bot
from endpilot.protocol import read_batch, write_batch

from test_join import FakeTransport


class ClientSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def joined(self, *, transport="nethernet", **options):
        bot = Bot("ClientSettings", protocol=2225)
        self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding
        wire = FakeTransport()

        def deliver(*packets):
            wire.received.put_nowait(write_batch(map(bot.protocol.encode, packets)))

        deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        deliver(
            b.StartGamePacket(runtime_id=42, entity_id=7, block_network_ids_are_hashes=True),
            b.ChunkRadiusUpdatedPacket(chunk_radius=5),
            b.NetworkChunkPublisherUpdatePacket(position=b.BlockPos(x=32, y=5, z=-32), radius=80),
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        )
        with patch(f"endpilot.transport.{transport}.connect", return_value=wire):
            if transport == "nethernet":
                await bot.join_nethernet("http://127.0.0.1:19132", **options)
            else:
                await bot.join_raknet("127.0.0.1", **options)
        await asyncio.sleep(0)
        return bot, wire, deliver

    def sent(self, bot, wire, packet_type):
        return [bot.protocol.decode(raw) for frame in wire.sent for raw in read_batch(frame)
                if raw.packet_id == packet_type.ID]

    async def test_join_preserves_defaults_and_signs_explicit_locale_for_both_transports(self):
        for transport, options, expected in (
            ("nethernet", {}, ("en_US", 8)),
            ("nethernet", {"locale": "zh_TW", "chunk_radius": 7}, ("zh_TW", 7)),
            ("raknet", {"locale": "fr_FR", "chunk_radius": 9}, ("fr_FR", 9)),
        ):
            with self.subTest(transport=transport, options=options):
                bot, wire, _ = await self.joined(transport=transport, **options)
                b = bot.protocol.binding
                login, = self.sent(bot, wire, b.LoginPacket)
                request = login.connection_request
                auth_size, = struct.unpack_from("<i", request)
                claims_segment = request[auth_size + 8:].decode().split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(claims_segment + "=="))
                self.assertEqual((claims["LanguageCode"], claims["MaxViewDistance"]), expected)
                self.assertEqual((claims["CurrentInputMode"], claims["DefaultInputMode"]), (1, 1))
                radius, = self.sent(bot, wire, b.RequestChunkRadiusPacket)
                self.assertEqual((radius.chunk_radius, radius.max_chunk_radius), (expected[1], expected[1]))
                self.assertEqual(bot.requested_chunk_radius, expected[1])
                self.assertEqual(bot.spawn_chunk_radius, 5)
                self.assertEqual(bot.accepted_chunk_radius, 5)
                await bot.close()

    async def test_live_request_observation_and_published_bounds_are_distinct(self):
        bot, wire, deliver = await self.joined(chunk_radius=8, retain_initial_chunks=True)
        b = bot.protocol.binding
        self.assertEqual(bot.published_chunk_bounds, (-3, 7, -7, 3))
        pending = asyncio.create_task(bot.wait_for_chunk_radius_update(timeout=1))
        await asyncio.sleep(0)
        await bot.request_chunk_radius(11)
        self.assertEqual((bot.requested_chunk_radius, bot.accepted_chunk_radius, bot.spawn_chunk_radius), (11, 5, 5))
        self.assertEqual(bot.published_chunk_bounds, (-3, 7, -7, 3))
        deliver(b.ChunkRadiusUpdatedPacket(chunk_radius=7))
        self.assertEqual(await pending, 7)
        self.assertEqual(bot.accepted_chunk_radius, 7)
        self.assertEqual(bot.spawn_chunk_radius, 5)
        self.assertEqual(bot.published_chunk_bounds, (-3, 7, -7, 3))
        deliver(b.NetworkChunkPublisherUpdatePacket(position=b.BlockPos(x=64, y=5, z=-48), radius=112))
        await asyncio.sleep(0)
        self.assertEqual(bot.published_chunk_bounds, (-3, 11, -10, 4))

        with patch.object(wire, "send", side_effect=ConnectionError("send failed")):
            with self.assertRaisesRegex(ConnectionError, "send failed"):
                await bot.request_chunk_radius(12)
        self.assertEqual((bot.requested_chunk_radius, bot.accepted_chunk_radius), (11, 7))
        with self.assertRaisesRegex(ValueError, "1..96"):
            await bot.request_chunk_radius(97)
        radii = self.sent(bot, wire, b.RequestChunkRadiusPacket)
        self.assertEqual([(p.chunk_radius, p.max_chunk_radius) for p in radii], [(8, 8), (11, 11)])
        pending = asyncio.create_task(bot.wait_for_chunk_radius_update(timeout=1))
        await asyncio.sleep(0)
        deliver(b.DisconnectPacket())
        with self.assertRaises(ConnectionError):
            await pending
        session = bot._session
        await bot.close()
        self.assertIsNone(session._unsubscribe_chunk_radius)

    async def test_recipe_book_settings_are_encoded_on_every_call(self):
        bot, wire, _ = await self.joined()
        b = bot.protocol.binding
        await bot.set_recipe_book_settings(False)
        for _ in range(2):
            await bot.set_recipe_book_settings(
                filtering=True, inventory_layout="RECIPE_BOOK_ONLY", crafting_layout="INVENTORY_ONLY",
                left_tab="RECIPE_SEARCH", right_tab="CRAFTING",
            )
        with self.assertRaisesRegex(ValueError, "InventoryLayout"):
            await bot.set_recipe_book_settings(False, inventory_layout="COUNT")
        packets = self.sent(bot, wire, b.SetPlayerInventoryOptionsPacket)
        actual = []
        for packet in packets:
            options = packet.inventory_options
            actual.append((options.filtering, options.layout_inv.name, options.layout_craft.name,
                           options.left_inventory_tab.name, options.right_inventory_tab.name))
        self.assertEqual(actual, [
            (False, "DEFAULT", "DEFAULT", "NONE", "NONE"),
            (True, "RECIPE_BOOK_ONLY", "INVENTORY_ONLY", "RECIPE_SEARCH", "CRAFTING"),
            (True, "RECIPE_BOOK_ONLY", "INVENTORY_ONLY", "RECIPE_SEARCH", "CRAFTING"),
        ])

    async def test_optional_client_options_preserve_false_and_omissions(self):
        bot, wire, _ = await self.joined()
        b = bot.protocol.binding
        await bot.update_client_options(graphics_mode="FANCY", filter_profanity=True)
        await bot.update_client_options(filter_profanity=False)
        await bot.update_client_options(graphics_mode="SIMPLE")
        with self.assertRaisesRegex(ValueError, "provide"):
            await bot.update_client_options()
        with self.assertRaisesRegex(ValueError, "GraphicsMode"):
            await bot.update_client_options(graphics_mode="UNAVAILABLE")
        packets = self.sent(bot, wire, b.UpdateClientOptionsPacket)
        self.assertEqual([(p.graphics_mode, p.filter_profanity) for p in packets], [
            (b.GraphicsMode.FANCY, True), (None, False), (b.GraphicsMode.SIMPLE, None),
        ])
