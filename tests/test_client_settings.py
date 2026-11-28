import asyncio
import base64
import json
import struct
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from endpilot import Bot, SubmittedClientSettings
from endpilot.authentication import OfflineIdentity, _sign
from endpilot.protocol import read_batch, write_batch

from test_join import FakeTransport


class CustomClientIdentity(OfflineIdentity):
    def __init__(self, claims):
        super().__init__("CustomClient")
        self.claims = claims

    def login_request(self, address, *, protocol, chunk_radius, locale):
        request = super().login_request(address, protocol=protocol, chunk_radius=chunk_radius, locale=locale)
        auth_size, = struct.unpack_from("<i", request)
        client = _sign(self.claims, self.key).encode("ascii")
        return request[:auth_size + 4] + struct.pack("<i", len(client)) + client


class ClientSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def joined(self, *, transport="nethernet", bot=None, **options):
        if bot is None:
            bot = Bot("ClientSettings", protocol=2225)
            self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding
        wire = FakeTransport()

        def deliver(*packets):
            wire.received.put_nowait(write_batch(map(bot.protocol.encode, packets)))

        deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        deliver(
            b.StartGamePacket(runtime_id=42, entity_id=7, block_network_ids_are_hashes=True, server_version="test-version"),
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
                self.assertEqual((claims["GraphicsMode"], claims.get("FilterProfanity")), (0, None))
                self.assertEqual(bot.submitted_client_settings, SubmittedClientSettings(expected[0], "SIMPLE", None))
                radius, = self.sent(bot, wire, b.RequestChunkRadiusPacket)
                self.assertEqual((radius.chunk_radius, radius.max_chunk_radius), (expected[1], expected[1]))
                self.assertEqual(bot.requested_chunk_radius, expected[1])
                self.assertEqual(bot.spawn_chunk_radius, 5)
                self.assertEqual(bot.accepted_chunk_radius, 5)
                await bot.close()

    async def test_snapshot_uses_custom_identity_claims_and_validates_each_field(self):
        for transport, claims, expected in (
            ("nethernet", {"LanguageCode": "de_DE", "GraphicsMode": 3, "FilterProfanity": False},
             SubmittedClientSettings("de_DE", "RAY_TRACED", False)),
            ("raknet", {"LanguageCode": "zh_TW", "GraphicsMode": 1, "FilterProfanity": True},
             SubmittedClientSettings("zh_TW", "FANCY", True)),
            ("raknet", {"LanguageCode": 9, "GraphicsMode": True, "FilterProfanity": 0}, SubmittedClientSettings()),
            ("nethernet", {"LanguageCode": "en_GB", "GraphicsMode": 256, "FilterProfanity": False},
             SubmittedClientSettings("en_GB", None, False)),
            ("nethernet", {"LanguageCode": "", "GraphicsMode": "FANCY", "FilterProfanity": "false"},
             SubmittedClientSettings()),
            ("raknet", {}, SubmittedClientSettings()),
        ):
            with self.subTest(transport=transport, claims=claims):
                bot = Bot("ClientSettings", protocol=2225)
                self.addAsyncCleanup(bot.close)
                bot.identity = CustomClientIdentity(claims)
                self.assertIsNone(bot.submitted_client_settings)
                await self.joined(bot=bot, transport=transport, locale="fr_FR")
                snapshot = bot.submitted_client_settings
                self.assertEqual(snapshot, expected)
                with self.assertRaises(FrozenInstanceError):
                    snapshot.locale = "fr_FR"
                await bot.close()
                self.assertIsNone(bot.submitted_client_settings)
                self.assertEqual(snapshot, expected)

    async def test_unreadable_client_declarations_do_not_change_login(self):
        for request in (b"\x04", struct.pack("<i", -1), struct.pack("<ii", 0, 2) + b"x",
                        struct.pack("<ii", 0, 1) + b"\xff", struct.pack("<ii", 0, 1) + b"?extra"):
            with self.subTest(request=request):
                bot = Bot("ClientSettings", protocol=2225)
                self.addAsyncCleanup(bot.close)
                with patch.object(bot.identity, "login_request", return_value=request):
                    _, wire, _ = await self.joined(bot=bot)
                login, = self.sent(bot, wire, bot.protocol.binding.LoginPacket)
                self.assertEqual(login.connection_request, request)
                self.assertEqual(bot.submitted_client_settings, SubmittedClientSettings())
                self.assertEqual(bot.reported_server_version, "test-version")
                await bot.update_client_options(filter_profanity=False)
                self.assertEqual(bot.submitted_client_settings, SubmittedClientSettings(filter_profanity=False))
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
        initial = bot.submitted_client_settings
        await bot.update_client_options(graphics_mode="FANCY", filter_profanity=True)
        first = bot.submitted_client_settings
        self.assertEqual(first, SubmittedClientSettings("en_US", "FANCY", True))
        await bot.update_client_options(filter_profanity=False)
        self.assertEqual(bot.submitted_client_settings, SubmittedClientSettings("en_US", "FANCY", False))
        await bot.update_client_options(graphics_mode="SIMPLE")
        current = bot.submitted_client_settings
        self.assertEqual(current, SubmittedClientSettings("en_US", "SIMPLE", False))
        with self.assertRaisesRegex(ValueError, "provide"):
            await bot.update_client_options()
        with self.assertRaisesRegex(ValueError, "GraphicsMode"):
            await bot.update_client_options(graphics_mode="UNAVAILABLE")
        with self.assertRaises(TypeError):
            await bot.update_client_options(filter_profanity=0)
        self.assertIs(bot.submitted_client_settings, current)
        self.assertEqual((initial, first), (SubmittedClientSettings("en_US", "SIMPLE", None),
                                            SubmittedClientSettings("en_US", "FANCY", True)))
        packets = self.sent(bot, wire, b.UpdateClientOptionsPacket)
        self.assertEqual([(p.graphics_mode, p.filter_profanity) for p in packets], [
            (b.GraphicsMode.FANCY, True), (None, False), (b.GraphicsMode.SIMPLE, None),
        ])
        self.assertEqual([raw.body.hex() for frame in wire.sent for raw in read_batch(frame)
                          if raw.packet_id == b.UpdateClientOptionsPacket.ID], ["01010101", "000100", "010000"])
        await bot.packets.send(b.UpdateClientOptionsPacket(graphics_mode=b.GraphicsMode.FANCY, filter_profanity=True))
        self.assertIs(bot.submitted_client_settings, current)

    async def test_failed_or_cancelled_updates_leave_snapshot_unchanged(self):
        bot, wire, _ = await self.joined()
        initial = bot.submitted_client_settings
        with patch.object(wire, "send", side_effect=ConnectionError("send failed")):
            with self.assertRaisesRegex(ConnectionError, "send failed"):
                await bot.update_client_options(graphics_mode="FANCY")
        self.assertIs(bot.submitted_client_settings, initial)

        transmitted = asyncio.Event()
        original_send = wire.send

        async def wait_after_send(data):
            await original_send(data)
            if any(raw.packet_id == bot.protocol.binding.UpdateClientOptionsPacket.ID for raw in read_batch(data)):
                transmitted.set()
                await asyncio.Future()

        with patch.object(wire, "send", new=wait_after_send):
            pending = asyncio.create_task(bot.update_client_options(filter_profanity=False))
            try:
                await asyncio.wait_for(transmitted.wait(), 1)
            finally:
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
        self.assertIs(bot.submitted_client_settings, initial)
        (sent,) = self.sent(bot, wire, bot.protocol.binding.UpdateClientOptionsPacket)
        self.assertIs(sent.filter_profanity, False)

    async def test_concurrent_updates_merge_in_successful_completion_order(self):
        bot, wire, _ = await self.joined()
        original_send = wire.send
        started = asyncio.Queue()

        async def wait_after_send(data):
            await original_send(data)
            if any(raw.packet_id == bot.protocol.binding.UpdateClientOptionsPacket.ID for raw in read_batch(data)):
                release = asyncio.Event()
                started.put_nowait(release)
                await release.wait()

        for first, second, second_snapshot, final_snapshot in (
            ({"graphics_mode": "ADVANCED"}, {"graphics_mode": "FANCY"},
             SubmittedClientSettings("en_US", "FANCY", True), SubmittedClientSettings("en_US", "ADVANCED", True)),
            ({"graphics_mode": "FANCY"}, {"filter_profanity": False},
             SubmittedClientSettings("en_US", "SIMPLE", False), SubmittedClientSettings("en_US", "FANCY", False)),
        ):
            with self.subTest(first=first, second=second):
                await bot.update_client_options(graphics_mode="SIMPLE", filter_profanity=True)
                with patch.object(wire, "send", new=wait_after_send):
                    async with asyncio.timeout(3), asyncio.TaskGroup() as tasks:
                        earlier = tasks.create_task(bot.update_client_options(**first))
                        release_earlier = await started.get()
                        newer = tasks.create_task(bot.update_client_options(**second))
                        release_newer = await started.get()
                        release_newer.set()
                        await newer
                        self.assertEqual(bot.submitted_client_settings, second_snapshot)
                        release_earlier.set()
                        await earlier
                self.assertEqual(bot.submitted_client_settings, final_snapshot)

    async def test_late_update_on_disconnected_transport_does_not_touch_rejoined_session(self):
        bot, wire, deliver = await self.joined()
        previous = bot.submitted_client_settings
        entered, release = asyncio.Event(), asyncio.Event()
        original_send = wire.send

        async def wait_after_send(data):
            await original_send(data)
            if any(raw.packet_id == bot.protocol.binding.UpdateClientOptionsPacket.ID for raw in read_batch(data)):
                entered.set()
                await release.wait()

        with patch.object(wire, "send", new=wait_after_send):
            async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
                updating = tasks.create_task(bot.update_client_options(graphics_mode="FANCY", filter_profanity=True))
                try:
                    await entered.wait()
                    deliver(bot.protocol.binding.DisconnectPacket())
                    await bot.wait_for_session_end(timeout=1)
                    self.assertIsNone(bot.submitted_client_settings)
                    self.assertIsNone(bot.reported_server_version)
                    await bot.close()
                    await self.joined(bot=bot, transport="raknet", locale="ja_JP")
                    current = bot.submitted_client_settings
                finally:
                    release.set()
                await updating
        self.assertEqual(previous, SubmittedClientSettings("en_US", "SIMPLE", None))
        self.assertEqual(current, SubmittedClientSettings("ja_JP", "SIMPLE", None))
        self.assertIs(bot.submitted_client_settings, current)
