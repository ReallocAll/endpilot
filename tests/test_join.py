import asyncio
import base64
import json
import struct
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from endpilot import BedrockCredentials, ResourcePackDecision
from endpilot.authentication import OfflineIdentity, _OnlineIdentity
from endpilot.blocks import Blocks
from endpilot.bot import Bot
from endpilot.compression import PacketConnection
from endpilot.login import JoinSession, LoginRefused
from endpilot.movement import Movement
from endpilot.player_state import PlayerState
from endpilot.protocol import Protocol, RawPacket, read_batch, write_batch
from endpilot.signs import Signs


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.received = asyncio.Queue()

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        return await self.received.get()

    async def close(self):
        self.closed = True


def _spawn_transport(protocol: Protocol, player_id: int, *initial_packets) -> FakeTransport:
    transport = FakeTransport()
    binding = protocol.binding
    settings = binding.NetworkSettingsPacket(compression_algorithm=binding.PacketCompressionAlgorithm.NONE)
    start = binding.StartGamePacket(runtime_id=player_id, entity_id=player_id)
    spawn = binding.PlayStatusPacket(status=binding.PlayStatus.PLAYER_SPAWN)
    transport.received.put_nowait(write_batch([protocol.encode(settings)]))
    transport.received.put_nowait(write_batch(protocol.encode(packet) for packet in (start, *initial_packets, spawn)))
    return transport


def _online_identity() -> _OnlineIdentity:
    return _OnlineIdentity(
        "VerifiedName", UUID(int=7), ec.generate_private_key(ec.SECP384R1()),
        BedrockCredentials(("local-test-certificate",), "local-test-token"),
        "http://127.0.0.1/test-issuer/", "local-test-signer", time.time() + 60, xuid="2535417953249031",
    )


class JoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_acquired_identity_drives_transport_and_player_packets(self):
        bot = Bot("OfflineName")
        offline = bot.identity
        self.addAsyncCleanup(bot.close)
        provider = object()
        identity = _online_identity()
        protocol = bot.protocol
        binding = protocol.binding
        book = binding.SerializedNetworkItemStackDescriptor(id=199, stack_size=1)
        initial = (
            binding.ItemRegistryPacket(items=[binding.ItemData(name="minecraft:writable_book", id=199)]),
            binding.InventorySlotPacket(inventory_id=binding.ContainerID.INVENTORY, slot=4, item=book),
        )

        for transport, endpoint in (("nethernet", "http://127.0.0.1:19132"), ("raknet", "127.0.0.1")):
            with self.subTest(transport=transport):
                wire = _spawn_transport(protocol, 41, *initial)
                with (
                    patch("endpilot.bot.acquire_online_identity", return_value=identity) as acquire,
                    patch(f"endpilot.transport.{transport}.connect", return_value=wire) as connect,
                ):
                    join = getattr(bot, f"join_{transport}")
                    await join(endpoint, credential_provider=provider, timeout=1)

                acquire.assert_awaited_once_with(provider)
                if transport == "nethernet":
                    connect.assert_awaited_once_with(
                        endpoint, private_key=identity.key, server_key_sha256=None,
                        multiplayer_token=identity.multiplayer_token, issuer=identity.issuer,
                    )
                else:
                    connect.assert_awaited_once_with(endpoint, 19132)
                self.assertIs(bot.identity, identity)
                first = len(wire.sent)
                await bot.send_chat("hello")
                await bot.sign_book(4, "Draft")
                await bot.send_command("list")
                await bot.send_whisper("OtherPlayer", "hello")
                chat, book_edit, command, whisper = [
                    protocol.decode(raw) for batch in wire.sent[first:] for raw in read_batch(batch)
                ]
                self.assertEqual(chat.body.author, "VerifiedName")
                self.assertEqual(book_edit.operation.author, "VerifiedName")
                self.assertEqual((command.origin.uuid, command.origin.player_id), (identity.uuid, 41))
                self.assertEqual((whisper.origin.uuid, whisper.origin.player_id), (identity.uuid, 41))
                session = bot._session
                await bot.close()
                self.assertIs(bot.identity, offline)
                self.assertIsNone(session.identity)
                self.assertTrue(wire.closed)

    async def test_close_during_network_settings_reclaims_online_transport_before_returning(self):
        for transport, endpoint, late_settings, fail_close in (
            ("nethernet", "http://127.0.0.1:19132", False, False),
            ("raknet", "127.0.0.1", False, False),
            ("raknet", "127.0.0.1", True, False),
            ("nethernet", "http://127.0.0.1:19132", False, True),
            ("raknet", "127.0.0.1", False, True),
        ):
            with self.subTest(transport=transport, late_settings=late_settings, fail_close=fail_close):
                bot = Bot("Retry")
                self.addAsyncCleanup(bot.close)
                protocol = bot.protocol
                binding = protocol.binding
                settings = binding.NetworkSettingsPacket(compression_algorithm=binding.PacketCompressionAlgorithm.NONE)
                response = write_batch([protocol.encode(settings)])
                waiting = asyncio.Event()
                close_error = OSError("local transport cleanup failed")

                class WaitingWire(FakeTransport):
                    async def recv(self):
                        waiting.set()
                        try:
                            return await super().recv()
                        except asyncio.CancelledError:
                            if late_settings:
                                return response
                            raise

                    async def close(self):
                        await super().close()
                        if fail_close:
                            raise close_error

                wire = WaitingWire()
                replacement = _spawn_transport(protocol, 42)
                join = getattr(bot, f"join_{transport}")
                with (
                    patch("endpilot.bot.acquire_online_identity", return_value=_online_identity()),
                    patch(f"endpilot.transport.{transport}.connect", side_effect=[wire, replacement]),
                    patch.object(wire, "close", wraps=wire.close) as close,
                ):
                    attempt = asyncio.create_task(join(endpoint, credential_provider=object()))
                    try:
                        await asyncio.wait_for(waiting.wait(), 1)
                        if fail_close:
                            with self.assertRaises(OSError) as closed:
                                await asyncio.wait_for(bot.close(), 1)
                            self.assertIs(closed.exception, close_error)
                        else:
                            await asyncio.wait_for(bot.close(), 1)
                        wire.received.put_nowait(response)
                        await asyncio.sleep(0)
                        sent = [packet.packet_id for batch in wire.sent for packet in read_batch(batch)]
                        self.assertEqual(sent, [binding.RequestNetworkSettingsPacket.ID])
                        close.assert_awaited_once()
                        with self.assertRaises(ConnectionError if late_settings else asyncio.CancelledError) as stopped:
                            await attempt
                        if fail_close:
                            self.assertIn(repr(close_error), " ".join(stopped.exception.__notes__))
                        await join(endpoint, timeout=1)
                        self.assertIs(bot.packets.transport, replacement)
                    finally:
                        if not attempt.done():
                            attempt.cancel()
                        await asyncio.gather(attempt, return_exceptions=True)

    async def test_known_invalid_online_arguments_fail_before_requesting_credentials(self):
        for transport, endpoint in (("nethernet", "http://127.0.0.1:19132"), ("raknet", "127.0.0.1")):
            for protocol, options, error, message in (
                (2193, {}, ValueError, "protocol 2225"),
                (2225, {"chunk_radius": 0}, ValueError, "chunk_radius must be in 1\\.\\.96"),
                (2225, {"locale": "en"}, ValueError, "locale must be a Bedrock language code"),
                (2225, {"timeout": "invalid"}, TypeError, "unsupported operand"),
            ):
                with self.subTest(transport=transport, protocol=protocol, options=options):
                    bot = Bot("Retry", protocol=protocol)
                    self.addAsyncCleanup(bot.close)
                    self.assertIs(bot.protocol.supports_feature("xbox_login"), protocol == 2225)
                    join = getattr(bot, f"join_{transport}")
                    with (
                        patch("endpilot.bot.acquire_online_identity", return_value=_online_identity()) as acquire,
                        patch(f"endpilot.transport.{transport}.connect", return_value=_spawn_transport(bot.protocol, 42)) as connect,
                    ):
                        with self.assertRaisesRegex(error, message):
                            await join(endpoint, credential_provider=object(), **options)
                        acquire.assert_not_awaited()
                        connect.assert_not_awaited()

    async def test_online_pack_callback_can_close_the_pending_transport_itself(self):
        bot = Bot("Retry")
        self.addAsyncCleanup(bot.close)
        protocol = bot.protocol
        binding = protocol.binding

        class ClosingWire(FakeTransport):
            async def send(self, data):
                if getattr(self, "closed", False):
                    raise ConnectionError("transport closed")
                await super().send(data)

        wire = ClosingWire()
        settings = binding.NetworkSettingsPacket(compression_algorithm=binding.PacketCompressionAlgorithm.NONE)
        pack = binding.PackInfoData(
            pack_id_version=binding.PackIdVersion(id=UUID(int=1), version=binding.SemVersion(version="1.0.0")),
            pack_size=50,
        )
        offer = binding.ResourcePacksInfoPacket(resource_packs=[pack])
        for packet in (settings, offer):
            wire.received.put_nowait(write_batch([protocol.encode(packet)]))

        async def decide(_offer):
            await bot.close()
            self.assertTrue(wire.closed)
            return ResourcePackDecision.REFUSE

        with (
            patch("endpilot.bot.acquire_online_identity", return_value=_online_identity()),
            patch("endpilot.transport.raknet.connect", return_value=wire),
            patch.object(wire, "close", wraps=wire.close) as close,
        ):
            with self.assertRaisesRegex(ConnectionError, "transport closed"):
                await bot.join_raknet("127.0.0.1", credential_provider=object(), on_resource_packs=decide, timeout=1)
            close.assert_awaited_once()

    async def test_close_during_authorization_prevents_a_late_dial(self):
        bot = Bot("Retry")
        self.addAsyncCleanup(bot.close)
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        release_cleanup = asyncio.Event()
        cleaned = asyncio.Event()

        async def authorize(_provider):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release_cleanup.wait()
                return OfflineIdentity("LateCredentials")
            finally:
                cleaned.set()

        wire = _spawn_transport(bot.protocol, 42)
        with (
            patch("endpilot.bot.acquire_online_identity", side_effect=authorize),
            patch("endpilot.transport.raknet.connect", return_value=wire) as connect,
        ):
            old_join = asyncio.create_task(bot.join_raknet("127.0.0.1", credential_provider=object()))
            closing = None
            try:
                await asyncio.wait_for(entered.wait(), 1)
                with self.assertRaisesRegex(RuntimeError, "already connecting or connected"):
                    await bot.join_raknet("127.0.0.1", timeout=1)
                connect.assert_not_awaited()
                closing = asyncio.create_task(bot.close())
                await asyncio.wait_for(cancelled.wait(), 1)
                await bot.join_raknet("127.0.0.1", timeout=1)
                self.assertFalse(closing.done())
                release_cleanup.set()
                await asyncio.wait_for(closing, 1)
                self.assertTrue(cleaned.is_set())
                with self.assertRaisesRegex(ConnectionError, "player closed while joining"):
                    await asyncio.wait_for(old_join, 1)
                connect.assert_awaited_once_with("127.0.0.1", 19132)
                self.assertEqual(bot.player_id, 42)
                self.assertIs(bot.packets.transport, wire)
                self.assertFalse(getattr(wire, "closed", False))
            finally:
                release_cleanup.set()
                old_join.cancel()
                await asyncio.gather(old_join, return_exceptions=True)
                if closing is not None:
                    await asyncio.gather(closing, return_exceptions=True)

    async def test_provider_can_close_its_bot_without_waiting_on_itself(self):
        bot = Bot("Retry")
        self.addAsyncCleanup(bot.close)

        async def authorize(_provider):
            await bot.close()
            return OfflineIdentity("LateCredentials")

        with (
            patch("endpilot.bot.acquire_online_identity", side_effect=authorize),
            patch("endpilot.transport.raknet.connect") as connect,
        ):
            with self.assertRaisesRegex(ConnectionError, "player closed while joining"):
                await asyncio.wait_for(bot.join_raknet("127.0.0.1", credential_provider=object()), 1)
            connect.assert_not_awaited()

    async def test_authorization_preserves_errors_and_outlasts_the_network_timeout(self):
        bot = Bot("Retry")
        self.addAsyncCleanup(bot.close)
        endpoint = "http://127.0.0.1:19132"
        problem = ValueError("local provider failed")

        async def fail(_public_key):
            raise problem

        with patch("endpilot.transport.nethernet.connect") as connect:
            with self.assertRaises(ValueError) as captured:
                await bot.join_nethernet(endpoint, credential_provider=fail)
            self.assertIs(captured.exception, problem)

            authorized = asyncio.Event()
            cleaning = asyncio.Event()
            release_cleanup = asyncio.Event()
            cleaned = asyncio.Event()

            async def authorize(_public_key):
                authorized.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release_cleanup.wait()
                    cleaned.set()

            pending = asyncio.create_task(bot.join_nethernet(endpoint, credential_provider=authorize, timeout=0.01))
            closing = None
            try:
                await asyncio.wait_for(authorized.wait(), 1)
                await asyncio.sleep(0.03)
                self.assertFalse(pending.done())
                pending.cancel("player cancelled authorization")
                await asyncio.wait_for(cleaning.wait(), 1)
                closing = asyncio.create_task(bot.close())
                await asyncio.sleep(0)
                self.assertFalse(closing.done())
                release_cleanup.set()
                await asyncio.wait_for(closing, 1)
                with self.assertRaisesRegex(asyncio.CancelledError, "player cancelled authorization"):
                    await pending
                self.assertTrue(cleaned.is_set())
            finally:
                release_cleanup.set()
                if not pending.done() and not pending.cancelling():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                if closing is not None:
                    await asyncio.gather(closing, return_exceptions=True)
            connect.assert_not_awaited()
            connect.return_value = _spawn_transport(bot.protocol, 43)
            await bot.join_nethernet(endpoint, timeout=1)
            self.assertEqual(bot.player_id, 43)

    async def test_login_refusal_remains_primary_when_transport_close_raises(self):
        bot = Bot("Retry")
        self.addAsyncCleanup(bot.close)
        protocol = bot.protocol
        binding = protocol.binding
        cleanup = OSError("local transport close failed")
        for transport, endpoint in (("nethernet", "http://127.0.0.1:19132"), ("raknet", "127.0.0.1")):
            with self.subTest(transport=transport):
                wire = FakeTransport()
                for packet in (
                    binding.NetworkSettingsPacket(compression_algorithm=binding.PacketCompressionAlgorithm.NONE),
                    binding.DisconnectPacket(),
                ):
                    wire.received.put_nowait(write_batch([protocol.encode(packet)]))
                with (
                    patch(f"endpilot.transport.{transport}.connect", return_value=wire),
                    patch.object(wire, "close", side_effect=cleanup) as close,
                ):
                    join = getattr(bot, f"join_{transport}")
                    with self.assertRaises(LoginRefused) as captured:
                        await join(endpoint, timeout=1)
                close.assert_awaited_once()
                self.assertIn(repr(cleanup), " ".join(captured.exception.__notes__))

    async def test_assembly_failure_closes_transport_and_allows_retry(self):
        for transport, endpoint in (("nethernet", "http://127.0.0.1:19132"), ("raknet", "127.0.0.1")):
            with self.subTest(transport=transport):
                bot = Bot("AssemblyTest")
                self.addAsyncCleanup(bot.close)
                failed = _spawn_transport(bot.protocol, 41)
                retry = _spawn_transport(bot.protocol, 42)
                failure = RuntimeError("scoreboard assembly failed")
                join = getattr(bot, f"join_{transport}")
                with (
                    patch(f"endpilot.transport.{transport}.connect", side_effect=[failed, retry]) as connect,
                    patch.object(failed, "close", wraps=failed.close) as close,
                ):
                    with patch("endpilot.bot.Scoreboard", side_effect=failure):
                        with self.assertRaises(RuntimeError) as caught:
                            await join(endpoint, timeout=1)
                    self.assertIs(caught.exception, failure)
                    close.assert_awaited_once()
                    self.assertTrue(failed.closed)
                    with self.assertRaisesRegex(RuntimeError, "^player has not joined$"):
                        _ = bot.packets

                    await join(endpoint, timeout=1)
                    self.assertEqual(connect.await_count, 2)
                    self.assertIs(bot.packets.transport, retry)
                    self.assertEqual(bot.player_id, 42)
                    await bot.close()
                    self.assertTrue(retry.closed)
                    close.assert_awaited_once()

    async def test_assembly_failure_remains_primary_when_transport_close_raises(self):
        bot = Bot("AssemblyTest")
        self.addAsyncCleanup(bot.close)
        transport = _spawn_transport(bot.protocol, 41)
        failure = RuntimeError("scoreboard assembly failed")
        close_failure = ConnectionError("transport close failed")
        with (
            patch("endpilot.transport.nethernet.connect", return_value=transport),
            patch("endpilot.bot.Scoreboard", side_effect=failure),
            patch.object(transport, "close", side_effect=close_failure) as close,
        ):
            with self.assertRaises(RuntimeError) as caught:
                await bot.join_nethernet("http://127.0.0.1:19132", timeout=1)
            close.assert_awaited_once()
        self.assertIs(caught.exception, failure)
        self.assertIn(repr(close_failure), " ".join(failure.__notes__))
        with self.assertRaisesRegex(RuntimeError, "^player has not joined$"):
            _ = bot.packets

    async def test_late_assembly_failure_drains_reader_and_ticker_after_component_close_error(self):
        for transport, endpoint in (("nethernet", "http://127.0.0.1:19132"), ("raknet", "127.0.0.1")):
            with self.subTest(transport=transport):
                bot = Bot("AssemblyTest")
                empty = {name for name, value in vars(bot).items() if value is None}
                failed = _spawn_transport(bot.protocol, 41)
                retry = _spawn_transport(bot.protocol, 42)
                failure = RuntimeError("assembly failed after movement started")
                cleanup_failure = RuntimeError("signs cleanup failed after unsubscribing")
                drain_failure = RuntimeError("player state drain failed after completion")
                movement = reader = ticker = None
                start_movement = Movement.start
                close_signs = Signs.close
                close_player_state = PlayerState.aclose

                def fail_after_start(component, **kwargs):
                    nonlocal movement, reader, ticker
                    start_movement(component, **kwargs)
                    movement = component
                    reader = movement.packets._reader_task
                    ticker = movement._ticker
                    self.assertFalse(reader.done())
                    self.assertFalse(ticker.done())
                    raise failure

                def fail_after_close(component):
                    close_signs(component)
                    raise cleanup_failure

                async def fail_after_drain(component):
                    await close_player_state(component)
                    raise drain_failure

                join = getattr(bot, f"join_{transport}")
                try:
                    with (
                        patch(f"endpilot.transport.{transport}.connect", side_effect=[failed, retry]),
                        patch.object(failed, "close", wraps=failed.close) as close,
                    ):
                        with (
                            patch.object(Movement, "start", fail_after_start),
                            patch.object(Signs, "close", fail_after_close),
                            patch.object(PlayerState, "aclose", fail_after_drain),
                        ):
                            with self.assertRaises(RuntimeError) as caught:
                                await join(endpoint, timeout=1)
                        self.assertIs(caught.exception, failure)
                        self.assertIn(repr(cleanup_failure), " ".join(failure.__notes__))
                        self.assertTrue(reader.done())
                        self.assertTrue(ticker.done())
                        close.assert_awaited_once()
                        self.assertTrue(failed.closed)
                        self.assertIn(repr(drain_failure), " ".join(failure.__notes__))
                        self.assertEqual({name for name in empty if getattr(bot, name) is not None}, set())
                        with self.assertRaisesRegex(RuntimeError, "^player has not joined$"):
                            _ = bot.packets

                        await join(endpoint, timeout=1)
                        self.assertEqual(bot.player_id, 42)
                        self.assertIs(bot.packets.transport, retry)
                        await bot.close()
                        self.assertTrue(retry.closed)
                        close.assert_awaited_once()
                finally:
                    await bot.close()
                    if movement is not None:
                        await movement.aclose()
                        if not getattr(failed, "closed", False):
                            await movement.packets.close()

    async def test_close_while_connecting_leaves_the_replacement_join_intact(self):
        bot = Bot("AssemblyTest")
        first = _spawn_transport(bot.protocol, 41)
        replacement = _spawn_transport(bot.protocol, 42)
        connecting = asyncio.Event()
        release = asyncio.Event()

        async def connect(*args, **kwargs):
            if connecting.is_set():
                return replacement
            connecting.set()
            await release.wait()
            return first

        endpoint = "http://127.0.0.1:19132"
        with (
            patch("endpilot.transport.nethernet.connect", side_effect=connect) as factory,
            patch.object(first, "close", wraps=first.close) as close,
        ):
            joining = asyncio.create_task(bot.join_nethernet(endpoint))
            try:
                await asyncio.wait_for(connecting.wait(), 1)
                with self.assertRaisesRegex(RuntimeError, "already connecting or connected"):
                    await bot.join_nethernet(endpoint, timeout=1)
                factory.assert_awaited_once()

                await bot.close()
                await bot.join_nethernet(endpoint, timeout=1)
                release.set()
                with self.assertRaisesRegex(ConnectionError, "player closed while joining"):
                    await asyncio.wait_for(joining, 1)
                close.assert_awaited_once()
                self.assertIs(bot.packets.transport, replacement)
                self.assertEqual(bot.player_id, 42)
            finally:
                joining.cancel()
                await asyncio.gather(joining, return_exceptions=True)
                await bot.close()

    async def test_offline_login_signs_game_version_for_selected_protocol(self):
        for options, number, game_version in (({}, 2225, "1.26.66"), ({"protocol": 2193}, 2193, "1.26.52")):
            with self.subTest(protocol=number):
                bot = Bot("EndPilotTest", **options)
                protocol = bot.protocol
                binding = protocol.binding
                settings = binding.NetworkSettingsPacket()
                settings.compression_algorithm = binding.PacketCompressionAlgorithm.ZLIB
                settings.compression_threshold = 0
                transport = FakeTransport()
                await transport.received.put(write_batch([protocol.encode(settings)]))
                await transport.received.put(b"")
                with patch("endpilot.transport.nethernet.connect", return_value=transport):
                    with self.assertRaisesRegex(ConnectionError, "empty compressed batch"):
                        await bot.join_nethernet("http://127.0.0.1:19132")

                _, frame = transport.sent
                self.assertEqual(frame[:1], b"\xff")
                login = protocol.decode(next(read_batch(frame[1:])))
                self.assertIsInstance(login, binding.LoginPacket)
                self.assertEqual(login.client_network_version, number)
                request = login.connection_request
                auth_length = struct.unpack_from("<i", request)[0]
                auth_end = 4 + auth_length
                auth = json.loads(request[4:auth_end])
                data_length = struct.unpack_from("<i", request, auth_end)[0]
                data = request[auth_end + 4:]
                self.assertEqual(len(data), data_length)

                key = bot.identity.key.public_key()
                public_der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
                for token, field, expected in (
                    (auth["Token"], "xname", "EndPilotTest"),
                    (data.decode(), "GameVersion", game_version),
                ):
                    header, payload, signature = token.split(".")
                    claims = json.loads(base64.urlsafe_b64decode(header + "=="))
                    self.assertEqual(claims["alg"], "ES384")
                    self.assertEqual(base64.b64decode(claims["x5u"]), public_der)
                    sig = base64.urlsafe_b64decode(signature + "==")
                    key.verify(
                        encode_dss_signature(int.from_bytes(sig[:48], "big"), int.from_bytes(sig[48:], "big")),
                        f"{header}.{payload}".encode(), ec.ECDSA(hashes.SHA384()),
                    )
                    self.assertEqual(json.loads(base64.urlsafe_b64decode(payload + "=="))[field], expected)

    async def test_join_timeout_includes_signaling_connection(self):
        async def never_connect(*args, **kwargs):
            await asyncio.Event().wait()

        with patch("endpilot.transport.nethernet.connect", side_effect=never_connect):
            with self.assertRaises(TimeoutError):
                await JoinSession.connect_nethernet(
                    "http://127.0.0.1:19132", OfflineIdentity("EndPilotTest"), object(), timeout=0.01
                )

    async def test_explicit_login_refusal_has_its_own_error(self):
        protocol = Protocol(2193)
        binding = protocol.binding

        class RejectedPackets:
            def __init__(self, packet_id):
                self.protocol = protocol
                self.packet_id = packet_id

            async def send(self, packet):
                pass

            async def recv_raw(self):
                return RawPacket(self.packet_id, b"")

        for packet_id, decoded in (
            (binding.DisconnectPacket.ID, SimpleNamespace(message="not allowed")),
            (binding.PlayStatusPacket.ID, SimpleNamespace(status=1)),
        ):
            with self.subTest(packet_id=packet_id), patch.object(protocol, "decode", return_value=decoded):
                session = JoinSession(RejectedPackets(packet_id), OfflineIdentity("DeniedPlayer"), "")
                with self.assertRaises(LoginRefused) as rejected:
                    await session.join()
                self.assertIsInstance(rejected.exception, ConnectionError)
                self.assertFalse(session.spawned)

    async def test_player_spawn_finishes_loading_screen_before_initialization(self):
        protocol = Protocol(2193)
        binding = protocol.binding

        class SpawnPackets:
            def __init__(self):
                self.protocol = protocol
                self.sent = []
                self.received = iter((
                    RawPacket(binding.StartGamePacket.ID, b"start"),
                    RawPacket(binding.PlayStatusPacket.ID, b"spawn"),
                ))

            async def recv_raw(self):
                return next(self.received)

            async def send(self, packet):
                self.sent.append(packet)

        def decode(raw):
            if raw.packet_id == binding.StartGamePacket.ID:
                return SimpleNamespace(runtime_id=42, entity_id=7, block_network_ids_are_hashes=False,
                                       block_properties=[object()], settings=SimpleNamespace(
                                           spawn_settings=binding.SpawnSettings(dimension=1),
                                       ))
            return SimpleNamespace(status=3)

        packets = SpawnPackets()
        with patch.object(protocol, "decode", side_effect=decode):
            session = JoinSession(packets, OfflineIdentity("EndPilotTest"), "")
            await session.join()

        self.assertTrue(session.spawned)
        self.assertEqual(session.player_id, 42)
        self.assertEqual(session.dimension_id, 1)
        self.assertTrue(session.has_custom_blocks)
        self.assertEqual(len(packets.sent), 5)
        self.assertIsInstance(packets.sent[0], binding.LoginPacket)
        self.assertIsInstance(packets.sent[1], binding.RequestChunkRadiusPacket)
        self.assertEqual(
            [int(packet.serverbound_loading_screen_packet_type) for packet in packets.sent[2:4]],
            [int(binding.ServerboundLoadingScreenPacketType.START_LOADING_SCREEN),
             int(binding.ServerboundLoadingScreenPacketType.END_LOADING_SCREEN)],
        )
        self.assertIsInstance(packets.sent[4], binding.SetLocalPlayerAsInitializedPacket)
        self.assertEqual(int(packets.sent[4].player_id), 42)

    async def test_pre_spawn_inventory_packets_survive_login(self):
        protocol = Protocol(2193)
        binding = protocol.binding
        initial = [
            RawPacket(binding.InventoryContentPacket.ID, b"inventory"),
            RawPacket(binding.InventorySlotPacket.ID, b"updated-slot"),
        ]

        class PreSpawnPackets:
            def __init__(self):
                self.protocol = protocol
                self.received = iter(initial + [
                    RawPacket(binding.StartGamePacket.ID, b"start"),
                    RawPacket(binding.PlayStatusPacket.ID, b"spawn"),
                ])

            async def recv_raw(self):
                return next(self.received)

            async def send(self, packet):
                pass

        def decode(raw):
            if raw.packet_id == binding.StartGamePacket.ID:
                return SimpleNamespace(runtime_id=42, entity_id=7, block_network_ids_are_hashes=False,
                                       block_properties=[], settings=binding.StartGamePacket().settings)
            return SimpleNamespace(status=3)

        with patch.object(protocol, "decode", side_effect=decode):
            session = JoinSession(PreSpawnPackets(), OfflineIdentity("EndPilotTest"), "")
            await session.join()
        self.assertTrue(session.spawned)
        self.assertEqual(session.inventory_packets, initial)

    async def test_pre_spawn_actor_notifications_survive_login(self):
        protocol = Protocol(2193)
        binding = protocol.binding
        remote_metadata = protocol.encode(binding.SetActorDataPacket(id=43))
        own_metadata = protocol.encode(binding.SetActorDataPacket(id=42))
        initial = [
            RawPacket(binding.PlayerListPacket.ID, b"player-list"),
            RawPacket(binding.AddPlayerPacket.ID, b"player-spawn"),
            remote_metadata,
            RawPacket(binding.MovePlayerPacket.ID, b"player-move"),
            RawPacket(binding.AddActorPacket.ID, b"spawn"),
            RawPacket(binding.MoveActorAbsolutePacket.ID, b"move"),
            RawPacket(binding.RemoveActorPacket.ID, b"remove"),
        ]

        class PreSpawnPackets:
            def __init__(self):
                self.protocol = protocol
                self.received = iter(initial[:3] + [RawPacket(binding.StartGamePacket.ID, b"start"), own_metadata]
                                     + initial[3:] + [
                    RawPacket(binding.PlayStatusPacket.ID, b"ready"),
                ])

            async def recv_raw(self):
                return next(self.received)

            async def send(self, packet):
                pass

        def decode(raw):
            if raw.packet_id == binding.StartGamePacket.ID:
                return SimpleNamespace(runtime_id=42, entity_id=7, block_network_ids_are_hashes=False,
                                       block_properties=[], settings=binding.StartGamePacket().settings)
            if raw.packet_id == binding.SetActorDataPacket.ID:
                return binding.decode(binding.SetActorDataPacket, raw.body)
            return SimpleNamespace(status=3)

        with patch.object(protocol, "decode", side_effect=decode):
            session = JoinSession(PreSpawnPackets(), OfflineIdentity("EndPilotTest"), "")
            await session.join()
        self.assertTrue(session.spawned)
        self.assertEqual(session.entity_packets, initial[:3] + [own_metadata] + initial[3:])
        self.assertIn(own_metadata, session.movement_packets)

    async def test_compressed_typed_raw_roundtrip(self):
        protocol = Protocol(2193)
        settings = protocol.binding.NetworkSettingsPacket()
        settings.compression_algorithm = protocol.binding.PacketCompressionAlgorithm.ZLIB
        settings.compression_threshold = 1
        fake = FakeTransport()
        stream = PacketConnection(fake, protocol, settings)
        packet = protocol.binding.RequestNetworkSettingsPacket()
        packet.client_network_version = protocol.number
        await stream.send(packet)
        self.assertIn(fake.sent[0][0], (0, 255))
        await fake.received.put(fake.sent[0])
        raw = await stream.recv_raw()
        self.assertEqual(protocol.decode(raw).client_network_version, protocol.number)
        await stream.send(RawPacket(header=5, body=b"x" * 1024))
        self.assertEqual(fake.sent[-1][0], 0)  # Repetitive payload is worth compressing.
        # A batch can carry multiple packets without dropping the next one.
        payload = write_batch([raw, raw])
        await fake.received.put(b"\xff" + payload)
        self.assertEqual((await stream.recv_raw()).packet_id, raw.packet_id)
        self.assertEqual((await stream.recv_raw()).packet_id, raw.packet_id)

    async def test_sent_observers_count_only_successful_game_packets(self):
        protocol = Protocol(2225)
        b = protocol.binding
        fake = FakeTransport()
        stream = PacketConnection(fake, protocol, SimpleNamespace(compression_algorithm=65535,
                                                                 compression_threshold=0))
        sent = []
        unsubscribe = stream.subscribe_sent(sent.append)
        packet = b.RequestChunkRadiusPacket(chunk_radius=16, max_chunk_radius=16)

        def cancelled(_packet_id):
            raise asyncio.CancelledError("sent observer failed")

        stop = stream.subscribe_sent(cancelled)
        with patch.object(asyncio.get_running_loop(), "call_exception_handler") as report:
            await stream.send(packet)
        self.assertIsInstance(report.call_args.args[0]["exception"], asyncio.CancelledError)
        stop()
        with patch.object(fake, "send", side_effect=ConnectionError("transport refused")):
            with self.assertRaisesRegex(ConnectionError, "transport refused"):
                await stream.send(packet)
        unsubscribe()
        await stream.send(packet)
        self.assertEqual(sent, [b.RequestChunkRadiusPacket.ID])
        self.assertEqual(len(fake.sent), 2)

    async def test_failed_observer_does_not_stop_live_packets(self):
        protocol = Protocol(2193)
        settings = protocol.binding.NetworkSettingsPacket()
        settings.compression_algorithm = protocol.binding.PacketCompressionAlgorithm.ZLIB
        settings.compression_threshold = 1
        fake = FakeTransport()
        stream = PacketConnection(fake, protocol, settings)
        observed = []

        def broken(raw):
            if raw.body == b"second":
                raise asyncio.CancelledError("extension callback cancelled")
            raise ValueError("extension callback failed")

        stream.subscribe_raw(broken)
        stream.subscribe_raw(observed.append)
        with patch.object(asyncio.get_running_loop(), "call_exception_handler") as report:
            stream.start_receiving()
            for raw in (RawPacket(5, b"first"), RawPacket(5, b"second")):
                receiving = asyncio.create_task(stream.recv_raw())
                await asyncio.sleep(0)  # Register the live waiter before the packet arrives.
                await fake.received.put(b"\xff" + write_batch([raw]))
                self.assertEqual(await asyncio.wait_for(receiving, 1), raw)

            self.assertEqual([packet.body for packet in observed], [b"first", b"second"])
            self.assertEqual([type(call.args[0]["exception"]) for call in report.call_args_list],
                             [ValueError, asyncio.CancelledError])

        await stream.close()
        self.assertTrue(fake.closed)

    async def test_cancelled_match_ends_only_its_own_packet_wait(self):
        fake = FakeTransport()
        stream = PacketConnection(fake, None, SimpleNamespace(compression_algorithm=65535, compression_threshold=0))
        self.addAsyncCleanup(stream.close)
        stream.start_receiving()

        def cancelled(raw):
            raise asyncio.CancelledError("predicate cancelled")

        raw = RawPacket(4, b"marker")
        matching = asyncio.create_task(stream.wait_for_packet(raw.packet_id, match=cancelled, timeout=1))
        await asyncio.sleep(0)
        fake.received.put_nowait(write_batch([raw]))
        with self.assertRaisesRegex(asyncio.CancelledError, "predicate cancelled"):
            await matching
        fake.received.put_nowait(write_batch([raw]))
        async with asyncio.timeout(1):
            self.assertEqual(await stream.recv_raw(), raw)

    async def test_caller_and_reader_cancellations_settle_only_their_raw_reads(self):
        fake = FakeTransport()
        stream = PacketConnection(fake, None, SimpleNamespace(compression_algorithm=65535, compression_threshold=0))
        self.addAsyncCleanup(stream.close)
        reader = stream.start_receiving()
        first, abandoned, last = (asyncio.create_task(stream.recv_raw()) for _ in range(3))
        await asyncio.sleep(0)
        abandoned.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await abandoned
        retained = len(stream._waiters)  # No incoming traffic to release abandoned readers.
        raw = RawPacket(4, b"first")
        fake.received.put_nowait(write_batch([raw]))
        self.assertEqual(await asyncio.wait_for(first, 1), raw)
        reader.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reader
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(last, 1)
        with self.assertRaises(ConnectionError):
            await stream.recv_raw()
        self.assertEqual(retained, 2)

    async def test_reader_end_callback_cancellation_keeps_eof_and_notifies_each_subscriber_once(self):
        source = EOFError("transport EOF")
        callback_error = asyncio.CancelledError("end subscriber cancelled itself")

        class EOFTransport(FakeTransport):
            async def recv(self):
                await super().recv()
                raise source

        protocol = Protocol(2225)
        transport = EOFTransport()
        stream = PacketConnection(transport, protocol, protocol.binding.NetworkSettingsPacket())
        observed = []

        def broken(cause):
            observed.append(("broken", cause))
            raise callback_error

        def later(cause):
            observed.append(("later", cause))

        stream.subscribe_reader_end(broken)
        stream.subscribe_reader_end(later)
        loop = asyncio.get_running_loop()
        with patch.object(loop, "call_exception_handler") as report:
            reader = stream.start_receiving()
            try:
                transport.received.put_nowait(b"")
                async with asyncio.timeout(1):
                    with self.assertRaises(EOFError) as raw_error:
                        await stream.recv_raw()
                self.assertIs(raw_error.exception, source)
                self.assertFalse(reader.cancelled())
                self.assertIs(reader.exception(), source)
            finally:
                await asyncio.gather(stream.close(), return_exceptions=True)
            self.assertEqual(observed, [("broken", source), ("later", source)])
            self.assertEqual(stream._reader_end_subscribers, [])
            report.assert_called_once()
            self.assertIs(report.call_args.args[0]["exception"], callback_error)
        self.assertTrue(transport.closed)

    async def test_reader_end_callback_cancellation_preserves_external_reader_cancellation(self):
        protocol = Protocol(2225)
        transport = FakeTransport()
        stream = PacketConnection(transport, protocol, protocol.binding.NetworkSettingsPacket())
        callback_error = asyncio.CancelledError("end subscriber cancelled itself")
        observed = []

        def broken(cause):
            observed.append(("broken", cause))
            raise callback_error

        stream.subscribe_reader_end(broken)
        stream.subscribe_reader_end(lambda cause: observed.append(("later", cause)))
        with patch.object(asyncio.get_running_loop(), "call_exception_handler") as report:
            reader = stream.start_receiving()
            reader.cancel()  # Also cover cancellation before the reader coroutine starts.
            with self.assertRaises(asyncio.CancelledError):
                await reader
            self.assertTrue(reader.cancelled())
            await stream.close()
            self.assertEqual(observed, [("broken", None), ("later", None)])
            self.assertEqual(stream._reader_end_subscribers, [])
            report.assert_called_once()
            self.assertIs(report.call_args.args[0]["exception"], callback_error)
        self.assertTrue(transport.closed)

    async def test_wait_for_server_disconnect_returns_upstream_packet(self):
        protocol = Protocol(2193)
        settings = protocol.binding.NetworkSettingsPacket()
        settings.compression_algorithm = protocol.binding.PacketCompressionAlgorithm.ZLIB
        settings.compression_threshold = 1
        fake = FakeTransport()
        stream = PacketConnection(fake, protocol, settings)
        session = JoinSession(stream, OfflineIdentity("EndPilotTest"), "")
        stream.start_receiving()
        waiting = asyncio.create_task(session.wait_for_server_disconnect(timeout=1))
        await asyncio.sleep(0)
        packet = RawPacket(protocol.binding.DisconnectPacket.ID, b"kick")
        observed = SimpleNamespace(reason="kicked")
        with patch.object(protocol, "decode", return_value=observed):
            await fake.received.put(b"\xff" + write_batch([packet]))
            self.assertIs(await waiting, observed)
        self.assertEqual(stream._subscribers, [])
        await stream.close()

    async def test_wait_for_server_disconnect_distinguishes_drop_and_timeout(self):
        protocol = Protocol(2193)
        settings = protocol.binding.NetworkSettingsPacket()
        settings.compression_algorithm = protocol.binding.PacketCompressionAlgorithm.ZLIB
        settings.compression_threshold = 1
        fake = FakeTransport()
        stream = PacketConnection(fake, protocol, settings)
        session = JoinSession(stream, OfflineIdentity("EndPilotTest"), "")
        stream.start_receiving()
        with self.assertRaises(TimeoutError):
            await session.wait_for_server_disconnect(timeout=0.01)
        self.assertEqual(stream._subscribers, [])
        waiting = asyncio.create_task(session.wait_for_server_disconnect(timeout=1))
        await asyncio.sleep(0)
        await fake.received.put(b"")
        with self.assertRaises(ConnectionError) as failure:
            await waiting
        self.assertIsInstance(failure.exception.__cause__, ConnectionError)
        self.assertEqual(stream._subscribers, [])
        with self.assertRaises(ConnectionError):
            await stream.close()
        self.assertTrue(fake.closed)

    async def test_block_wait_differentiates_live_timeout_from_stream_loss(self):
        protocol = Protocol(2193)
        settings = protocol.binding.NetworkSettingsPacket()
        settings.compression_algorithm = protocol.binding.PacketCompressionAlgorithm.ZLIB
        settings.compression_threshold = 1
        fake = FakeTransport()
        stream = PacketConnection(fake, protocol, settings)
        stream.start_receiving()
        blocks = Blocks(stream, None, None)

        with self.assertRaises(TimeoutError):
            await blocks.wait_for_update((2, 100, 0), timeout=0.01)
        self.assertEqual(stream._subscribers, [])

        waiting = asyncio.create_task(blocks.wait_for_update((2, 100, 0), timeout=1))
        await asyncio.sleep(0)
        await fake.received.put(b"")
        with self.assertRaises(ConnectionError) as failure:
            await waiting
        self.assertIsInstance(failure.exception.__cause__, ConnectionError)
        self.assertEqual(stream._subscribers, [])
        with self.assertRaises(ConnectionError):
            await stream.close()

    async def test_reader_failure_still_closes_transport(self):
        protocol = Protocol(2193)
        settings = protocol.binding.NetworkSettingsPacket()
        settings.compression_algorithm = protocol.binding.PacketCompressionAlgorithm.ZLIB
        settings.compression_threshold = 1
        fake = FakeTransport()
        stream = PacketConnection(fake, protocol, settings)
        stream.start_receiving()
        await fake.received.put(b"\x01invalid-compression-header")
        await asyncio.sleep(0)

        with self.assertRaises(ValueError):
            await stream.close()
        self.assertTrue(fake.closed)

    async def test_public_walk_stops_when_receive_stream_fails(self):
        protocol = Protocol(2193)
        settings = protocol.binding.NetworkSettingsPacket()
        settings.compression_algorithm = protocol.binding.PacketCompressionAlgorithm.ZLIB
        settings.compression_threshold = 1
        class DroppingTransport(FakeTransport):
            async def recv(self):
                raw = await super().recv()
                if raw is None:
                    raise ConnectionError("peer disconnected")
                return raw

        transport = DroppingTransport()
        packets = PacketConnection(transport, protocol, settings)
        session = JoinSession(packets, OfflineIdentity("EndPilotTest"), "http://127.0.0.1:19132")
        session.player_id = session.player_unique_id = 1

        bot = Bot("EndPilotTest", protocol=protocol.number)
        with patch.object(JoinSession, "connect_nethernet", return_value=session):
            await bot.join_nethernet("http://127.0.0.1:19132")
        bot._movement.position = bot._movement._input_position = (0.5, 101.62, 0.5)
        walking = asyncio.create_task(bot.walk_forward(100))
        try:
            async with asyncio.timeout(1):
                while not transport.sent:
                    await asyncio.sleep(0.001)
            # The receiver fails, while the mock transport can still accept writes.
            await transport.received.put(None)
            with self.assertRaisesRegex(ConnectionError, "peer disconnected"):
                await asyncio.wait_for(walking, 1)
            self.assertLess(len(transport.sent), 100)
        finally:
            if not walking.done():
                walking.cancel()
                await asyncio.gather(walking, return_exceptions=True)
            with self.assertRaisesRegex(ConnectionError, "peer disconnected"):
                await bot.close()
        self.assertTrue(transport.closed)
        self.assertIsNone(bot._movement)

    async def test_public_walk_stops_on_kick_even_if_transport_stays_open(self):
        protocol = Protocol(2193)
        binding = protocol.binding
        settings = binding.NetworkSettingsPacket()
        settings.compression_algorithm = binding.PacketCompressionAlgorithm.ZLIB
        settings.compression_threshold = 1
        transport = FakeTransport()
        packets = PacketConnection(transport, protocol, settings)
        session = JoinSession(packets, OfflineIdentity("EndPilotTest"), "http://127.0.0.1:19132")
        session.player_id = session.player_unique_id = 1

        bot = Bot("EndPilotTest", protocol=protocol.number)
        with patch.object(JoinSession, "connect_nethernet", return_value=session):
            await bot.join_nethernet("http://127.0.0.1:19132")
        movement = bot._movement
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        ticker = movement._ticker
        walking = asyncio.create_task(bot.walk_forward(100))
        try:
            await asyncio.sleep(0.06)
            self.assertFalse(walking.done())
            self.assertGreater(len(transport.sent), 0)
            waiting = asyncio.create_task(bot.wait_for_server_disconnect(timeout=1))
            await asyncio.sleep(0)
            observed = SimpleNamespace(reason="kicked")
            with patch.object(protocol, "decode", return_value=observed):
                await transport.received.put(b"\xff" + write_batch([
                    RawPacket(binding.DisconnectPacket.ID, b"kick")
                ]))
                self.assertIs(await waiting, observed)

            # No synthetic socket failure: the packet receiver is still alive.
            self.assertFalse(packets._reader_task.done())
            with self.assertRaisesRegex(ConnectionError, "server disconnected"):
                await asyncio.wait_for(walking, 1)
            count = len(transport.sent)
            await asyncio.sleep(0.1)
            self.assertEqual(len(transport.sent), count)
            self.assertTrue(ticker.done())
        finally:
            if not walking.done():
                walking.cancel()
                await asyncio.gather(walking, return_exceptions=True)
            await bot.close()
        self.assertTrue(transport.closed)

    async def test_public_position_wait_fails_on_receive_stream_loss(self):
        protocol = Protocol(2193)
        settings = protocol.binding.NetworkSettingsPacket()
        settings.compression_algorithm = protocol.binding.PacketCompressionAlgorithm.ZLIB
        settings.compression_threshold = 1
        transport = FakeTransport()
        packets = PacketConnection(transport, protocol, settings)
        session = JoinSession(packets, OfflineIdentity("EndPilotTest"), "http://127.0.0.1:19132")
        session.player_id = session.player_unique_id = 1

        bot = Bot("EndPilotTest", protocol=protocol.number)
        with patch.object(JoinSession, "connect_nethernet", return_value=session):
            await bot.join_nethernet("http://127.0.0.1:19132")
        waiting = asyncio.create_task(bot.wait_for_position_sync(timeout=5))
        try:
            await asyncio.sleep(0)
            await transport.received.put(b"")  # Remote stream ends before the first position.
            with self.assertRaisesRegex(ConnectionError, "packet stream lost") as failure:
                await asyncio.wait_for(waiting, 1)
            self.assertIsInstance(failure.exception.__cause__, ConnectionError)
        finally:
            if not waiting.done():
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            with self.assertRaisesRegex(ConnectionError, "empty compressed batch"):
                await bot.close()
        self.assertTrue(transport.closed)
        self.assertIsNone(bot._movement)
