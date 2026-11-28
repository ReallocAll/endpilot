import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import errno
from ipaddress import IPv4Address
from pathlib import Path
import socket
import ssl
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from aiohttp import ClientConnectorCertificateError, ClientConnectorSSLError, ClientResponseError, web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from nethernet import ConnectionFailed, InvalidIdentity, SessionError

from endpilot import Bot, JoinFailureDetails
from endpilot._join_address import _parse_join_address
from endpilot.protocol import Protocol
from endpilot.transport._nethernet_http import _NetherNetUnavailable, _request, connect_http
from endpilot.transport.nethernet import _check_signaling_endpoint
from test_join import _online_identity, _spawn_transport


@asynccontextmanager
async def _listen(routes, *, ssl_context=None):
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_context).start()
        yield f"127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


def _untrusted_tls(directory: Path) -> ssl.SSLContext:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "untrusted join fixture")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(IPv4Address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "certificate.pem", directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


class JoinAddressTests(unittest.IsolatedAsyncioTestCase):
    def test_offline_default_and_explicit_player_names(self):
        self.assertEqual(Bot().identity.name, "EndPilot")
        self.assertEqual(Bot("Knight").identity.name, "Knight")

    def test_bare_ipv6_and_full_signaling_path(self):
        target = _parse_join_address("[2001:0DB8::1]:19132")
        self.assertIsNone(target.scheme)
        self.assertEqual((target.host, target.port, target.authority), ("2001:db8::1", 19132, "[2001:db8::1]:19132"))
        url = "https://[::1]:443/path/%2flobby"
        self.assertEqual(_parse_join_address(url).value, url)
        self.assertEqual(_parse_join_address("raknet://[::1]").port, 19132)

    async def test_invalid_address_or_pin_is_rejected_before_authorization(self):
        provider = AsyncMock()
        for address, pin in (
            ("localhost", None), ("localhost:0", None), ("localhost:65536", None), ("::1:19132", None),
            ("[::1]bad:19132", None), ("localhost:19132/path", None), ("https://user@localhost:19132", None),
            ("http://localhost:19132/path?", None), ("http://localhost:19132/path#fragment", None),
            ("ftp://localhost:19132", None), ("localhost:19132", "bad"), ("raknet://localhost", "a" * 64),
        ):
            with self.subTest(address=address, pin=pin), self.assertRaises(ValueError):
                await Bot("Knight").join_address(address, server_key_sha256=pin, credential_provider=provider)
        provider.assert_not_awaited()

    async def test_probe_preserves_terminal_status_and_accepts_a_2xx_without_a_protocol(self):
        async def capability(request):
            return web.Response(status=int(request.match_info["status"]))

        async with _listen([web.get("/{status}/v1/join", capability)]) as authority:
            for status in (401, 403, 503, 307):
                with self.subTest(status=status), self.assertRaises(ClientResponseError) as caught:
                    await _check_signaling_endpoint(f"http://{authority}/{status}")
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(caught.exception.join_failure, JoinFailureDetails(
                    "nethernet_http", "signaling_discovery", "http_error", "http_status", status,
                ))
            await _check_signaling_endpoint(f"http://{authority}/204")
            with self.assertRaises(_NetherNetUnavailable) as caught:
                await _check_signaling_endpoint(f"http://{authority}/404")
            self.assertEqual(caught.exception.error.status, 404)
            self.assertIs(caught.exception.error, caught.exception.__cause__)

    async def test_explicit_signaling_get_keeps_connection_error_and_http_cause(self):
        async def deny(_request):
            return web.Response(status=403, reason="custom assertion from a private server")

        bot = Bot("Knight")
        self.addAsyncCleanup(bot.close)
        connection = self._mock_connection()
        cleanup = OSError(errno.EBADF, "failed to close the NetherNet session")
        connection._session.aclose.side_effect = cleanup
        async with _listen([web.get("/v1/join", deny)]) as authority:
            with (
                patch("endpilot.transport._nethernet_http._make_http_session", return_value=connection),
                self.assertRaises(ConnectionFailed) as caught,
            ):
                await bot.join_nethernet(f"http://{authority}", timeout=1)
        error = caught.exception
        self.assertIs(error.error, SessionError.NO_SIGNALING_CHANNEL)
        self.assertEqual(error.args, ConnectionFailed(SessionError.NO_SIGNALING_CHANNEL).args)
        self.assertIsInstance(error.__cause__, ClientResponseError)
        self.assertEqual(error.__cause__.status, 403)
        self.assertIs(error.join_failure, error.__cause__.join_failure)
        self.assertEqual(error.join_failure, JoinFailureDetails(
            "nethernet_http", "signaling_discovery", "http_error", "http_status", 403,
        ))
        connection._session.aclose.assert_awaited_once_with(SessionError.GENERIC_FAILURE)
        self.assertIn(repr(cleanup), " ".join(error.__notes__))

    async def test_signaling_requests_disable_redirects(self):
        url = "http://127.0.0.1:19132/v1/join"
        for method, options in (("GET", {}), ("POST", {"data": b"v=0", "headers": {"Content-Type": "application/sdp"}})):
            with self.subTest(method=method):
                client = MagicMock()
                client.request.return_value.__aenter__.return_value = SimpleNamespace(
                    status=200, text=AsyncMock(return_value="v=0"),
                )
                await _request(client, method, url, **options)
                client.request.assert_called_once_with(method, url, allow_redirects=False, **options)

    async def test_peer_reported_timeout_and_http_denials_stop_before_accepting_an_answer(self):
        async def capability(_request):
            return web.Response()

        async def signal(request):
            status = int(request.match_info["status"])
            return web.Response(status=status, text=str(int(SessionError.NEGOTIATION_TIMEOUT_WAITING_FOR_ACCEPT)))

        async with _listen([web.get("/{status}/v1/join", capability),
                            web.post("/{status}/v1/join/{id}", signal)]) as authority:
            for status in (200, 307, 401, 403, 404):
                for explicit in (False, True):
                    with self.subTest(status=status, explicit=explicit):
                        connection = self._mock_connection()
                        with (
                            patch("endpilot.transport._nethernet_http._make_http_session", return_value=connection),
                            self.assertRaises(ConnectionFailed) as caught,
                        ):
                            await connect_http(
                                f"http://{authority}/{status}", identity_signer=Mock(sign=str),
                                on_server_identity=None, capability_check=explicit,
                            )
                        if status == 200:
                            self.assertIs(caught.exception.error, SessionError.NEGOTIATION_TIMEOUT_WAITING_FOR_ACCEPT)
                            expected = JoinFailureDetails(
                                "nethernet_http", "signaling_connect", "timeout", "nethernet_session_error",
                                "NEGOTIATION_TIMEOUT_WAITING_FOR_ACCEPT",
                            )
                        else:
                            self.assertIs(caught.exception.error, SessionError.SIGNALING_FAILED_TO_SEND)
                            self.assertEqual(caught.exception.__cause__.status, status)
                            self.assertIs(caught.exception.join_failure, caught.exception.__cause__.join_failure)
                            expected = JoinFailureDetails(
                                "nethernet_http", "signaling_connect", "http_error", "http_status", status,
                            )
                        self.assertEqual(caught.exception.join_failure, expected)
                        connection._session.accept_answer.assert_not_awaited()
                        connection._session.aclose.assert_awaited_once_with(SessionError.GENERIC_FAILURE)

    async def test_only_automatic_join_can_continue_after_a_local_post_answer_timeout(self):
        async def capability(_request):
            return web.Response()

        async def answer(_request):
            return web.Response(text="v=0")

        async with _listen([web.get("/v1/join", capability), web.post("/v1/join/{id}", answer)]) as authority:
            for explicit in (False, True):
                with self.subTest(explicit=explicit):
                    connection = self._mock_connection()
                    timeout = ConnectionFailed(SessionError.NEGOTIATION_TIMEOUT_WAITING_FOR_ACCEPT)
                    connection._await_connected.side_effect = timeout
                    cleanup = OSError(errno.EBADF, "failed to close the unavailable NetherNet candidate")
                    if not explicit:
                        connection._session.aclose.side_effect = cleanup
                    with (
                        patch("endpilot.transport._nethernet_http._make_http_session", return_value=connection),
                        self.assertRaises(ConnectionFailed if explicit else _NetherNetUnavailable) as caught,
                    ):
                        await connect_http(
                            f"http://{authority}", identity_signer=Mock(sign=str),
                            on_server_identity=None, capability_check=explicit,
                        )
                    error = caught.exception if explicit else caught.exception.error
                    self.assertIs(error, timeout)
                    if not explicit:
                        self.assertIn(repr(cleanup), " ".join(error.__notes__))
                    connection._session.accept_answer.assert_awaited_once_with("v=0")
                    connection._session.aclose.assert_awaited_once_with(SessionError.GENERIC_FAILURE)

    @staticmethod
    def _mock_connection():
        reliable = SimpleNamespace(on=Mock(return_value=lambda callback: callback))
        session = SimpleNamespace(
            _pc=SimpleNamespace(_reliable=reliable),
            offer=AsyncMock(return_value="v=0"), accept_answer=AsyncMock(), aclose=AsyncMock(),
        )
        return SimpleNamespace(_session=session, _await_connected=AsyncMock())

    async def test_untrusted_certificate_stops_before_offer_or_other_transport(self):
        bot = Bot("Knight")
        with TemporaryDirectory() as directory:
            context = _untrusted_tls(Path(directory))
            async with _listen([], ssl_context=context) as authority:
                with (
                    patch("endpilot.transport.nethernet._check_signaling_endpoint", wraps=_check_signaling_endpoint) as probe,
                    patch("endpilot.transport.nethernet.connect_http") as offer,
                    patch("endpilot.transport.raknet.connect") as raknet,
                ):
                    with self.assertRaises(ClientConnectorCertificateError) as caught:
                        await bot.join_address(authority, timeout=2)
                    error = caught.exception
                    self.assertIs(error.certificate_error, error.__cause__)
                    self.assertIsInstance(error.certificate_error, ssl.SSLCertVerificationError)
                    self.assertEqual(error.join_failure, JoinFailureDetails(
                        "nethernet_https", "signaling_discovery", "tls_certificate_verification", "verify_code",
                        error.certificate_error.verify_code,
                    ))
                    probe.assert_awaited_once_with(f"https://{authority}")
                    offer.assert_not_awaited()
                    raknet.assert_not_awaited()
        await bot.close()

    async def test_tls_handshake_alert_stops_before_offer_or_other_transport(self):
        bot = Bot("Knight")
        self.addAsyncCleanup(bot.close)
        with TemporaryDirectory() as directory, socket.create_server(("127.0.0.1", 0)) as listener:
            context = _untrusted_tls(Path(directory))
            context.maximum_version = ssl.TLSVersion.TLSv1_2
            # The ECDSA server certificate cannot use this RSA-only cipher.
            context.set_ciphers("ECDHE-RSA-AES256-GCM-SHA384")
            listener.settimeout(3)
            authority = f"127.0.0.1:{listener.getsockname()[1]}"

            def serve():
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(3)
                    with self.assertRaises(ssl.SSLError) as rejected:
                        with context.wrap_socket(connection, server_side=True):
                            self.fail("TLS fixture unexpectedly completed a handshake")
                    return rejected.exception.reason

            server = asyncio.create_task(asyncio.to_thread(serve))
            try:
                with (
                    patch("endpilot.transport.nethernet._check_signaling_endpoint", wraps=_check_signaling_endpoint) as probe,
                    patch("endpilot.transport.nethernet.connect_http") as offer,
                    patch("endpilot.transport.raknet.connect") as raknet,
                    self.assertRaises(ClientConnectorSSLError) as caught,
                ):
                    await bot.join_address(authority, timeout=2)
                error = caught.exception
                self.assertIs(error.os_error, error.__cause__)
                self.assertEqual(error.os_error.reason, "SSLV3_ALERT_HANDSHAKE_FAILURE")
                self.assertEqual(error.join_failure, JoinFailureDetails(
                    "nethernet_https", "signaling_discovery", "tls_handshake", "openssl_reason",
                    "SSLV3_ALERT_HANDSHAKE_FAILURE",
                ))
                probe.assert_awaited_once_with(f"https://{authority}")
                offer.assert_not_awaited()
                raknet.assert_not_awaited()
            finally:
                server_result = await server
            self.assertEqual(server_result, "NO_SHARED_CIPHER")

    async def test_pin_mismatch_stops_selection(self):
        async def wrong_pin(_url, *, on_server_identity, **_kwargs):
            on_server_identity(SimpleNamespace(key_digest="0" * 64))

        with (
            patch("endpilot.transport.nethernet._check_signaling_endpoint") as probe,
            patch("endpilot.transport.nethernet.connect_http", side_effect=wrong_pin) as offer,
            patch("endpilot.transport.raknet.connect") as raknet,
        ):
            with self.assertRaisesRegex(InvalidIdentity, "does not match server_key_sha256"):
                await Bot("Knight").join_address("localhost:19132", server_key_sha256="1" * 64)
            probe.assert_awaited_once_with("https://localhost:19132")
            self.assertFalse(offer.await_args.kwargs["capability_check"])
            raknet.assert_not_awaited()

    async def test_pin_stops_before_raknet_if_neither_nethernet_endpoint_exists(self):
        last_error = ConnectionRefusedError("HTTP refused")
        with (
            patch("endpilot.transport.nethernet._check_signaling_endpoint", side_effect=[
                _NetherNetUnavailable(ConnectionRefusedError("HTTPS refused")), _NetherNetUnavailable(last_error),
            ]) as probe,
            patch("endpilot.transport.raknet.connect") as raknet,
        ):
            with self.assertRaises(ConnectionRefusedError) as caught:
                await Bot("Knight").join_address("localhost:19132", server_key_sha256="1" * 64)
            self.assertIs(caught.exception, last_error)
            self.assertEqual([call.args for call in probe.await_args_list], [
                ("https://localhost:19132",), ("http://localhost:19132",),
            ])
            raknet.assert_not_awaited()

    async def test_bare_address_uses_raknet_when_either_nethernet_http_dependency_is_absent(self):
        for missing in ("nethernet", "aiohttp"):
            with self.subTest(missing=missing):
                bot = Bot()
                wire = _spawn_transport(bot.protocol, 42)
                with (
                    patch.dict(sys.modules, {missing: None}),
                    patch("endpilot.login.JoinSession.connect_nethernet") as nethernet,
                    patch("endpilot.transport.raknet.connect", return_value=wire) as raknet,
                ):
                    try:
                        await bot.join_address("127.0.0.1:19132")
                        self.assertEqual(bot.player_id, 42)
                        nethernet.assert_not_awaited()
                        raknet.assert_awaited_once_with("127.0.0.1", 19132)
                    finally:
                        await bot.close()

    async def test_missing_optional_dependency_does_not_change_a_required_nethernet_route(self):
        for address, options in (
            ("127.0.0.1:19132", {"server_key_sha256": "a" * 64}),
            ("https://127.0.0.1:19132/lobby", {}),
        ):
            with self.subTest(address=address):
                with (
                    patch.dict(sys.modules, {"nethernet": None}),
                    patch("endpilot.transport.raknet.connect") as raknet,
                    self.assertRaises(ModuleNotFoundError) as caught,
                ):
                    await Bot().join_address(address, **options)
                self.assertEqual(caught.exception.name, "nethernet")
                raknet.assert_not_awaited()

    async def test_internal_import_failure_is_preserved_when_nethernet_http_is_present(self):
        error = ModuleNotFoundError("No module named 'aiortc'", name="aiortc")
        with (
            patch("endpilot.transport.nethernet._check_signaling_endpoint", side_effect=error),
            patch("endpilot.transport.raknet.connect") as raknet,
            self.assertRaises(ModuleNotFoundError) as caught,
        ):
            await Bot().join_address("127.0.0.1:19132")
        self.assertIs(caught.exception, error)
        raknet.assert_not_awaited()

    async def test_ambiguous_post_error_is_preserved_without_fallback(self):
        error = ConnectionFailed(SessionError.SIGNALING_FAILED_TO_SEND)
        with (
            patch("endpilot.transport.nethernet._check_signaling_endpoint") as probe,
            patch("endpilot.transport.nethernet.connect_http", side_effect=error) as offer,
            patch("endpilot.transport.raknet.connect") as raknet,
        ):
            with self.assertRaises(ConnectionFailed) as caught:
                await Bot("Knight").join_address("localhost:19132")
            self.assertIs(caught.exception, error)
            self.assertEqual(error.__notes__, ["NetherNet HTTP offer exchange failed before the transport opened"])
            probe.assert_awaited_once_with("https://localhost:19132")
            self.assertFalse(offer.await_args.kwargs["capability_check"])
            raknet.assert_not_awaited()

    async def test_timeout_while_unwinding_an_identity_rejection_does_not_fall_back(self):
        rejection = InvalidIdentity("test identity rejection")

        async def reject_and_clean_up(*_args, **_kwargs):
            try:
                raise rejection
            except InvalidIdentity:
                await asyncio.Future()

        with (
            patch("endpilot.transport.nethernet._check_signaling_endpoint") as probe,
            patch("endpilot.transport.nethernet.connect_http", side_effect=reject_and_clean_up),
            patch("endpilot.transport.raknet.connect") as raknet,
        ):
            with self.assertRaises(TimeoutError) as caught:
                await Bot("Knight").join_address("localhost:19132", timeout=0.05)
            self.assertIs(caught.exception.__cause__.__context__, rejection)
            probe.assert_awaited_once_with("https://localhost:19132")
            raknet.assert_not_awaited()

    async def test_close_during_offline_selection_drains_the_probe(self):
        entered, exited = asyncio.Event(), asyncio.Event()

        async def blocked(_url):
            try:
                entered.set()
                await asyncio.Future()
            finally:
                exited.set()

        bot = Bot("Knight")
        with patch("endpilot.transport.nethernet._check_signaling_endpoint", side_effect=blocked) as probe:
            async with asyncio.timeout(2):
                joining = asyncio.create_task(bot.join_address("localhost:19132"))
                await entered.wait()
                await bot.close()
                self.assertTrue(exited.is_set())
                with self.assertRaises(asyncio.CancelledError) as cancelled:
                    await joining
                self.assertIsNone(getattr(cancelled.exception, "join_failure", None))
            probe.assert_awaited_once_with("https://localhost:19132")

    async def test_protocol_announcement_is_scoped_to_one_nethernet_candidate(self):
        bot = Bot(protocol=None)
        self.addAsyncCleanup(bot.close)
        wire = _spawn_transport(Protocol(2225), 42)
        failure = _NetherNetUnavailable(ConnectionError("fixture transport did not open"))
        with (
            patch("endpilot.transport.nethernet._check_signaling_endpoint", side_effect=[2193, None]) as get,
            patch("endpilot.transport.nethernet.connect_http", side_effect=[failure, wire]) as dial,
            patch("endpilot.transport.raknet.connect") as raknet,
        ):
            await bot.join_address("127.0.0.1:19132")
        self.assertEqual([call.args for call in get.await_args_list], [
            ("https://127.0.0.1:19132",), ("http://127.0.0.1:19132",),
        ])
        self.assertEqual([call.kwargs for call in get.await_args_list], [{"read_protocol": True}] * 2)
        self.assertEqual(dial.await_count, 2)
        raknet.assert_not_awaited()
        self.assertEqual(failure.error.join_failure.transport, "nethernet_https")
        self.assertIs(bot.protocol, bot.packets.protocol)
        self.assertEqual((bot.protocol.number, bot.player_id), (2225, 42))

    async def test_nethernet_rejects_unsupported_announcements_without_posting_an_offer(self):
        from aiortc import RTCIceGatherer

        self.enterContext(patch.object(RTCIceGatherer, "getDefaultIceServers", return_value=[]))
        self.enterContext(patch("aioice.ice.get_host_addresses", return_value=["127.0.0.1"]))
        for number, online, message in (
            (99999, False, "unsupported Bedrock protocol 99999"),
            (2193, True, "online login requires protocol 2225, server advertised 2193"),
        ):
            with self.subTest(number=number, online=online):
                requests = []

                async def announce(request):
                    requests.append((request.method, request.headers.get("Authorization")))
                    return web.json_response({"protocol": number})

                async def offer(request):
                    requests.append((request.method, request.headers.get("Authorization")))
                    return web.Response(status=403)

                bot = Bot(protocol=None)
                self.addAsyncCleanup(bot.close)
                initial = bot.protocol
                provider = object() if online else None
                async with _listen([web.get("/v1/join", announce), web.post("/v1/join/{id}", offer)]) as authority:
                    with (
                        patch("endpilot.bot.acquire_online_identity", return_value=_online_identity()) as acquire,
                        patch("endpilot.transport.raknet.connect") as raknet,
                        self.assertRaisesRegex(ValueError, message),
                    ):
                        if online:
                            await bot.join_address(authority, credential_provider=provider)
                        else:
                            await bot.join_nethernet(f"http://{authority}")
                self.assertEqual(requests, [("GET", None)])
                self.assertEqual(acquire.await_count, int(online))
                raknet.assert_not_awaited()
                self.assertIs(bot.protocol, initial)

    async def test_closing_automatic_nethernet_join_cancels_the_get_and_closes_the_session(self):
        entered, released = asyncio.Event(), asyncio.Event()

        async def announce(_request):
            entered.set()
            await released.wait()
            return web.json_response({"protocol": 2193})

        connection = self._mock_connection()
        cleanup = OSError(errno.EBADF, "failed to close the cancelled NetherNet session")
        connection._session.aclose.side_effect = cleanup
        bot = Bot(protocol=None)
        self.addAsyncCleanup(bot.close)
        async with _listen([web.get("/v1/join", announce)]) as authority:
            with (
                patch("endpilot.transport._nethernet_http._make_http_session", return_value=connection),
                patch("endpilot.transport.nethernet.PlayerSigner", return_value=Mock(sign=str)),
            ):
                joining = asyncio.create_task(bot.join_nethernet(f"http://{authority}"))
                try:
                    async with asyncio.timeout(3):
                        await entered.wait()
                        await bot.close()
                        with self.assertRaises(asyncio.CancelledError) as caught:
                            await joining
                        self.assertIsNone(getattr(caught.exception, "join_failure", None))
                        self.assertIn(repr(cleanup), " ".join(caught.exception.__notes__))
                finally:
                    released.set()
                    joining.cancel()
                    await asyncio.gather(joining, return_exceptions=True)
        connection._session.accept_answer.assert_not_awaited()
        connection._session.aclose.assert_awaited_once_with(SessionError.GENERIC_FAILURE)
        self.assertEqual(bot.protocol.number, 2225)
