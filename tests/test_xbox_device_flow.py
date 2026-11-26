import asyncio
import base64
from importlib.util import find_spec
import logging
import os
from tempfile import TemporaryDirectory
import threading
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from endpilot import BedrockCredentials
from endpilot import xbox
from endpilot.xbox import XboxCredentialProvider


CLIENT_ID = "00000000-0000-0000-0000-000000000001"
HOST = "https://login.microsoftonline.com"
TENANT = HOST + "/consumers"
DEVICE = TENANT + "/oauth2/v2.0/devicecode"
TOKEN = TENANT + "/oauth2/v2.0/token"
METADATA = TENANT + "/v2.0/.well-known/openid-configuration"
URI = "https://www.microsoft.com/devicelogin"
CODE = "ABCD-1234"
MACHINE_CODE = "local-machine-device-secret"
ACCESS_TOKEN = "local-access-token-secret"
SECRETS = (CODE, MACHINE_CODE, ACCESS_TOKEN)


def public_key() -> str:
    key = ec.generate_private_key(ec.SECP384R1()).public_key()
    return base64.b64encode(key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )).decode("ascii")


@unittest.skipUnless(find_spec("msal") and find_spec("httpx"), "requires the Xbox extra")
class _MsalTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import httpx

        self.httpx = httpx
        home = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "HOME": home, "USERPROFILE": home, "LOCALAPPDATA": home, "XDG_STATE_HOME": home,
        }))
        self.enterContext(patch("socket.getaddrinfo", side_effect=AssertionError("unexpected DNS")))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("unexpected socket connect")))
        self.enterContext(patch("socket.socket.connect_ex", side_effect=AssertionError("unexpected socket connect")))
        self.display = Mock()
        self.provider = XboxCredentialProvider(CLIENT_ID, on_device_code=self.display)
        self.credentials = BedrockCredentials(("test-certificate-one", "test-certificate-two"), "test-bedrock-token")
        self.exchange = AsyncMock(return_value=self.credentials)
        self.verify = AsyncMock()
        self.enterContext(patch.object(xbox, "_exchange", SimpleNamespace(exchange=self.exchange), create=True))
        self.enterContext(patch.object(xbox, "_verify", SimpleNamespace(verify_credentials=self.verify), create=True))

    def authority(self, *poll_responses, lifetime=60, on_discovery=None, verification_uri=URI):
        httpx = self.httpx
        construct = httpx.Client
        poll_responses = iter(poll_responses)
        requests, transports = [], []
        case = self

        def respond(request):
            requests.append(request)
            if str(request.url) == METADATA:
                if on_discovery is not None:
                    on_discovery()
                return httpx.Response(200, json={
                    "issuer": TENANT + "/v2.0", "authorization_endpoint": TENANT + "/oauth2/v2.0/authorize",
                    "device_authorization_endpoint": DEVICE, "token_endpoint": TOKEN,
                })
            if str(request.url) == DEVICE:
                uri = verification_uri() if callable(verification_uri) else verification_uri
                return httpx.Response(200, json={
                    "device_code": MACHINE_CODE, "user_code": CODE, "verification_uri": uri,
                    "interval": 1, "expires_in": lifetime, "message": ACCESS_TOKEN,
                })
            if str(request.url) == TOKEN:
                response = next(poll_responses)
                return response() if callable(response) else response
            raise AssertionError(f"unexpected local request: {request.method} {request.url}")

        class Transport(httpx.MockTransport):
            closed = False

            def close(self):
                self.closed = True
                super().close()

        def client(**kwargs):
            case.assertFalse(kwargs["follow_redirects"])
            transport = Transport(respond)
            transports.append(transport)
            return construct(transport=transport, **kwargs)

        factory = self.enterContext(patch.object(httpx, "Client", side_effect=client))
        return requests, transports, factory

    def success(self):
        return self.httpx.Response(200, json={
            "access_token": ACCESS_TOKEN, "expires_in": 3600, "token_type": "Bearer", "scope": "XboxLive.signin",
        })


class XboxDeviceFlowTests(_MsalTestCase):
    async def test_constructor_and_invalid_public_key_make_no_requests(self):
        requests, transports, factory = self.authority()
        with self.assertRaisesRegex(ValueError, "application UUID"):
            XboxCredentialProvider("00000000-0000-0000-0000-000000000000", on_device_code=self.display)
        with self.assertRaisesRegex(ValueError, "P-384 public key"):
            await self.provider("invalid")
        factory.assert_not_called()
        self.display.assert_not_called()
        self.assertEqual((requests, transports), ([], []))

    async def test_real_msal_uses_personal_device_flow_and_passes_the_same_public_key(self):
        requests, transports, _ = self.authority(
            self.httpx.Response(400, json={"error": "authorization_pending"}), self.success(),
        )
        key = public_key()
        credentials = await self.provider(key)
        self.assertIs(credentials, self.credentials)
        self.display.assert_called_once_with(URI, CODE)
        self.assertEqual([(request.method, str(request.url)) for request in requests], [
            ("GET", METADATA), ("POST", DEVICE), ("POST", TOKEN), ("POST", TOKEN),
        ])
        device = parse_qs(requests[1].content.decode())
        self.assertEqual(device["client_id"], [CLIENT_ID])
        self.assertEqual(set(device["scope"][0].split()), {"XboxLive.signin", "openid"})
        for request in requests:
            timeout = request.extensions["timeout"]
            self.assertTrue(all(0 < value < 60 for value in timeout.values()))
        for request in requests[2:]:
            form = parse_qs(request.content.decode())
            self.assertEqual(form["device_code"], [MACHINE_CODE])
            self.assertEqual(form["grant_type"], ["urn:ietf:params:oauth:grant-type:device_code"])
            self.assertNotIn("client_secret", form)
        client, token, presented_key = self.exchange.await_args.args
        self.assertEqual((token, presented_key), (ACCESS_TOKEN, key))
        self.assertFalse(client.follow_redirects)
        self.assertTrue(client.is_closed)
        self.assertTrue(transports[0].closed)
        self.verify.assert_awaited_once_with(client, credentials, key)

    async def test_exact_microsoft_verification_uris_are_passed_to_the_callback_without_logging_codes(self):
        accepted = ("https://www.microsoft.com/link", "https://microsoft.com/devicelogin", URI)
        uris = iter(accepted)
        requests, _, _ = self.authority(*(self.success() for _ in accepted), verification_uri=lambda: next(uris))
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("outside-authentication").debug("outside message")
            for uri in accepted:
                with self.subTest(uri=uri):
                    self.assertIs(await self.provider(public_key()), self.credentials)
        self.assertEqual([call.args for call in self.display.call_args_list], [(uri, CODE) for uri in accepted])
        self.assertEqual([str(request.url) for request in requests], [METADATA, DEVICE, TOKEN] * len(accepted))
        for secret in SECRETS:
            self.assertNotIn(secret, "\n".join(logs.output))

    async def test_similar_untrusted_verification_uris_are_rejected_before_prompt_or_poll(self):
        rejected = (
            "https://signin.microsoft.com/link", "https://www.microsoft.com.example.test/link",
            "https://www.microsoft.com/links", "https://www.microsoft.com/link?ref=app",
            "https://www.microsoft.com/link#prompt", "http://www.microsoft.com/link",
        )
        uris = iter(rejected)
        requests, _, _ = self.authority(verification_uri=lambda: next(uris))
        for uri in rejected:
            with self.subTest(uri=uri), self.assertRaisesRegex(RuntimeError, "invalid prompt"):
                await self.provider(public_key())
        self.assertEqual([str(request.url) for request in requests], [METADATA, DEVICE] * len(rejected))
        self.display.assert_not_called()
        self.exchange.assert_not_awaited()

    async def test_poll_failure_does_not_expose_non_json_credentials_in_exceptions_or_logs(self):
        self.authority(self.httpx.Response(400, content=" ".join(SECRETS)))
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("outside-authentication").debug("outside message")
            with self.assertRaisesRegex(RuntimeError, "Microsoft device authorization polling failed") as failed:
                await self.provider(public_key())
        displayed = "\n".join(logs.output + traceback.format_exception(failed.exception))
        for secret in SECRETS:
            self.assertNotIn(secret, displayed)
        self.assertIn("outside message", displayed)
        self.exchange.assert_not_awaited()
        self.verify.assert_not_awaited()

    async def test_denial_and_expiration_remain_distinct_from_exchange_errors(self):
        self.authority(
            self.httpx.Response(400, json={"error": "authorization_declined", "error_description": ACCESS_TOKEN}),
            self.httpx.Response(400, json={"error": "expired_token", "error_description": MACHINE_CODE}),
        )
        with self.assertRaisesRegex(RuntimeError, "authorization was denied"):
            await self.provider(public_key())
        with self.assertRaisesRegex(TimeoutError, "authorization expired"):
            await self.provider(public_key())
        self.assertEqual(self.display.call_count, 2)
        self.exchange.assert_not_awaited()

    async def test_callback_failure_does_not_poll_or_expose_the_short_code(self):
        requests, transports, _ = self.authority()
        self.display.side_effect = RuntimeError(CODE)
        with self.assertRaisesRegex(RuntimeError, "callback failed") as failed:
            await self.provider(public_key())
        self.assertNotIn(CODE, "".join(traceback.format_exception(failed.exception)))
        self.assertEqual([str(request.url) for request in requests], [METADATA, DEVICE])
        self.assertTrue(transports[0].closed)

    async def test_msal_slow_down_updates_the_async_wait_and_it_can_be_cancelled(self):
        requests, transports, _ = self.authority(self.httpx.Response(400, json={"error": "slow_down"}))
        waiting = asyncio.Event()

        async def wait(_delay):
            waiting.set()
            await asyncio.Event().wait()

        with patch.object(xbox, "sleep", side_effect=wait) as sleep:
            pending = asyncio.create_task(self.provider(public_key()))
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                sleep.assert_awaited_once_with(6)
            finally:
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
        self.assertEqual([str(request.url) for request in requests], [METADATA, DEVICE, TOKEN])
        self.assertTrue(transports[0].closed)
        self.exchange.assert_not_awaited()

    async def test_cancellation_during_msal_discovery_does_not_request_a_device_code(self):
        loop = asyncio.get_running_loop()
        discovered = asyncio.Event()
        released = threading.Event()

        def block_discovery():
            loop.call_soon_threadsafe(discovered.set)
            if not released.wait(3):
                raise RuntimeError("test did not release discovery")

        requests, transports, _ = self.authority(self.success(), on_discovery=block_discovery)
        abandoned = asyncio.create_task(self.provider(public_key()))
        try:
            await asyncio.wait_for(discovered.wait(), 1)
            abandoned.cancel("cancelled before device prompt")
            await asyncio.sleep(0)
            self.assertFalse(abandoned.done())
            self.assertFalse(transports[0].closed)
        finally:
            released.set()
            if not abandoned.done() and not abandoned.cancelling():
                abandoned.cancel()
            with self.assertRaisesRegex(asyncio.CancelledError, "cancelled before device prompt"):
                await abandoned

        self.assertEqual([(request.method, str(request.url)) for request in requests], [("GET", METADATA)])
        self.assertTrue(transports[0].closed)
        self.display.assert_not_called()
        self.exchange.assert_not_awaited()
        self.verify.assert_not_awaited()

        retry_key = public_key()
        self.assertIs(await self.provider(retry_key), self.credentials)
        self.assertEqual([str(request.url) for request in requests], [METADATA, METADATA, DEVICE, TOKEN])
        self.display.assert_called_once_with(URI, CODE)
        self.assertEqual(self.exchange.await_args.args[2], retry_key)

    async def test_cancellation_drains_the_msal_worker_and_same_provider_can_retry(self):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        released = threading.Event()
        logger = logging.getLogger("httpcore.http11")

        def block():
            logger.debug("provider header %s", MACHINE_CODE)
            loop.call_soon_threadsafe(entered.set)
            if not released.wait(3):
                raise RuntimeError("test did not release device poll")
            return self.httpx.Response(400, json={"error": "authorization_pending"})

        requests, transports, _ = self.authority(block, self.success())
        abandoned_key, retry_key = public_key(), public_key()
        with self.assertLogs(logger, level=logging.DEBUG) as logs:
            pending = asyncio.create_task(self.provider(abandoned_key))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                logger.debug("unrelated HTTP request")
                pending.cancel("caller cancelled authorization")
                await asyncio.sleep(0)
                pending.cancel()
                await asyncio.sleep(0)
                self.assertFalse(pending.done())
                self.assertFalse(transports[0].closed)
                self.exchange.assert_not_awaited()
            finally:
                released.set()
                if not pending.done() and not pending.cancelling():
                    pending.cancel()
                with self.assertRaisesRegex(asyncio.CancelledError, "caller cancelled authorization"):
                    await pending
        self.assertEqual(logs.output, ["DEBUG:httpcore.http11:unrelated HTTP request"])
        self.assertTrue(transports[0].closed)
        self.assertEqual([str(request.url) for request in requests], [METADATA, DEVICE, TOKEN])
        self.exchange.assert_not_awaited()
        self.assertIs(await self.provider(retry_key), self.credentials)
        self.assertEqual(self.exchange.await_args.args[2], retry_key)
        self.assertTrue(transports[-1].closed)
