import asyncio
import base64
from importlib import import_module
from importlib.util import find_spec
import json
from pathlib import Path
import threading
from unittest import skipUnless
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

from endpilot import BedrockCredentials, xbox
from endpilot.xbox import (
    DEFAULT_CLIENT_ID, XboxAccountCacheError, XboxAuthorizationUnavailable, XboxCredentialProvider,
    XboxReauthenticationRequired, account_cache_path, forget_account,
)

from test_xbox_device_flow import ACCESS_TOKEN, CLIENT_ID, CODE, DEVICE, MACHINE_CODE, METADATA, TOKEN, _MsalTestCase, public_key


ACCOUNT = "fake-user.fake-tenant"


class XboxCachedFlowTests(_MsalTestCase):
    def remembered(self):
        return XboxCredentialProvider(CLIENT_ID, on_device_code=self.display, remember_account=True)

    def token(self, *, uid="fake-user", utid="fake-tenant", refresh="fake-refresh-one", expires=3600):
        reply = {
            "access_token": ACCESS_TOKEN, "expires_in": expires, "token_type": "Bearer",
            "scope": "XboxLive.signin", "device_code": MACHINE_CODE, "user_code": CODE,
        }
        if uid:
            reply["client_info"] = base64.urlsafe_b64encode(json.dumps({"uid": uid, "utid": utid}).encode()).decode()
        if refresh:
            reply["refresh_token"] = refresh
        return self.httpx.Response(200, json=reply)

    def saved_state(self):
        data = json.loads(account_cache_path(CLIENT_ID).read_bytes())
        self.assertEqual(data["home_account_id"], ACCOUNT)
        return json.loads(data["serialized_cache"])

    def device_result_with_client_info(self, client_info):
        from msal import PublicClientApplication

        redeem = PublicClientApplication.acquire_token_by_device_flow

        def report(app, *args, **kwargs):
            result = redeem(app, *args, **kwargs)
            return dict(result, client_info=client_info)

        return patch.object(PublicClientApplication, "acquire_token_by_device_flow", new=report)

    async def test_default_application_authorizes_and_forgets_only_its_separate_cache(self):
        requests, _, _ = self.authority(self.token(), self.token())
        await XboxCredentialProvider(on_device_code=self.display, remember_account=True)(public_key())
        await self.remembered()(public_key())

        forms = [parse_qs(request.content.decode()) for request in requests if str(request.url) in (DEVICE, TOKEN)]
        default = "e7cfcfa2-c0bb-419c-bf4a-fd966b7fe716"
        self.assertEqual([form["client_id"] for form in forms], [[default], [default], [CLIENT_ID], [CLIENT_ID]])
        default_path = account_cache_path()
        custom_path = account_cache_path(CLIENT_ID)
        self.assertEqual(default_path, account_cache_path(DEFAULT_CLIENT_ID))
        self.assertEqual(default_path.name, f"{default}.json")
        self.assertTrue(default_path.is_file())
        custom_cache = custom_path.read_bytes()
        self.assertTrue(await forget_account())
        self.assertFalse(default_path.exists())
        self.assertEqual(custom_path.read_bytes(), custom_cache)
        self.assertFalse(await forget_account())

    async def test_remembering_requires_an_explicit_bool(self):
        requests, _, factory = self.authority()
        with patch("endpilot.xbox.account_cache_path", side_effect=AssertionError("unexpected cache lookup")):
            for value in ("false", "true", 0, 1, None):
                with self.subTest(value=value), self.assertRaisesRegex(TypeError, "must be a bool"):
                    XboxCredentialProvider(CLIENT_ID, on_device_code=self.display, remember_account=value)
        factory.assert_not_called()
        self.assertFalse(requests)
        self.assertFalse(account_cache_path(CLIENT_ID).parent.exists())
        self.display.assert_not_called()

    async def test_opt_in_saves_the_account_and_later_uses_the_cached_access_token(self):
        requests, _, _ = self.authority(self.success(), self.token())
        path = account_cache_path(CLIENT_ID)
        headless = XboxCredentialProvider(CLIENT_ID)
        with self.assertRaises(XboxReauthenticationRequired):
            await headless(public_key())
        self.assertEqual(requests, [])

        once = public_key()
        await self.provider(once)
        self.assertFalse(path.parent.exists())
        remembered = public_key()
        await self.remembered()(remembered)
        self.assertTrue(path.exists())
        self.assertEqual(set(self.saved_state()), {"Account", "AccessToken", "RefreshToken"})
        before = path.read_bytes()
        for excluded in (MACHINE_CODE, CODE, once, remembered, self.credentials.multiplayer_token):
            self.assertNotIn(excluded.encode(), before)
        self.assertNotIn("offline_access", parse_qs(requests[1].content.decode())["scope"][0].split())
        self.assertIn("offline_access", parse_qs(requests[4].content.decode())["scope"][0].split())
        retry_key = public_key()
        self.assertIs(await headless(retry_key), self.credentials)
        self.assertEqual([str(request.url) for request in requests], [METADATA, DEVICE, TOKEN, METADATA, DEVICE, TOKEN, METADATA])
        self.assertEqual(self.display.call_count, 2)
        self.assertEqual(self.exchange.await_args.args[2], retry_key)
        self.assertEqual(path.read_bytes(), before)

        async def exchange_after_unlock(_client, _token, _key):
            self.assertTrue(await asyncio.wait_for(forget_account(CLIENT_ID), 2))
            return self.credentials

        self.exchange.side_effect = exchange_after_unlock
        await headless(public_key())
        self.assertFalse(path.exists())
        self.assertFalse(await forget_account(CLIENT_ID))

    async def test_each_silent_refresh_uses_and_replaces_the_current_refresh_token(self):
        requests, _, _ = self.authority(
            self.token(expires=0), self.token(refresh="fake-refresh-two", expires=0),
            self.token(refresh="fake-refresh-three"),
        )
        await self.remembered()(public_key())
        for expected in ("fake-refresh-two", "fake-refresh-three"):
            await XboxCredentialProvider(CLIENT_ID)(public_key())
            token, = self.saved_state()["RefreshToken"].values()
            self.assertEqual(token["secret"], expected)
        forms = [parse_qs(request.content.decode()) for request in requests if str(request.url) == TOKEN]
        self.assertEqual([form["refresh_token"] for form in forms[1:]], [["fake-refresh-one"], ["fake-refresh-two"]])
        for form in forms[1:]:
            self.assertEqual(form["grant_type"], ["refresh_token"])
            self.assertIn("offline_access", form["scope"][0].split())
        self.display.assert_called_once()

    async def test_unrenewable_or_unidentified_device_response_authorizes_once_without_saving(self):
        requests, _, _ = self.authority(self.token(refresh=None), self.token(uid=None))
        provider = self.remembered()
        for _ in range(2):
            key = public_key()
            self.assertEqual(await provider._credentials_with_remember_status(key), (self.credentials, True))
            self.assertFalse(account_cache_path(CLIENT_ID).exists())
            client, token, exchanged_key = self.exchange.await_args.args
            self.assertEqual((token, exchanged_key), (ACCESS_TOKEN, key))
            self.verify.assert_awaited_with(client, self.credentials, key)
        self.assertEqual([str(request.url) for request in requests], [METADATA, DEVICE, TOKEN] * 2)

    async def test_add_with_incomplete_client_info_saves_and_silently_reuses_verified_account(self):
        from endpilot.xbox import _cache

        requests, _, _ = self.authority(self.token(utid=""))
        profile = ("12345", "Local Player")
        verify_account = AsyncMock(return_value=profile)
        path = account_cache_path(CLIENT_ID)
        with patch.object(xbox._verify, "verify_account", new=verify_account, create=True):
            self.assertEqual(await self.provider._credentials_for_account(public_key(), add=True),
                             (self.credentials, profile, False))
            record, = _cache.load_accounts(path)
            self.assertEqual((record.home_account_id, record.xuid, record.gamertag), ("fake-user.", *profile))

            before = path.read_bytes()
            headless = XboxCredentialProvider(CLIENT_ID)
            self.assertEqual(await headless._credentials_for_account(public_key(), xuid=profile[0]),
                             (self.credentials, profile, False))
            self.assertEqual(path.read_bytes(), before)

        self.assertEqual([str(request.url) for request in requests], [METADATA, DEVICE, TOKEN, METADATA])
        self.display.assert_called_once()
        self.assertEqual(verify_account.await_count, 2)

    async def test_add_with_conflicting_client_info_verifies_without_saving(self):
        self.authority(self.token())
        profile = ("12345", "Local Player")
        verify_account = AsyncMock(return_value=profile)
        client_info = self.token(uid="other-user").json()["client_info"]
        with (self.device_result_with_client_info(client_info),
              patch.object(xbox._verify, "verify_account", new=verify_account, create=True)):
            self.assertEqual(await self.provider._credentials_for_account(public_key(), add=True),
                             (self.credentials, profile, True))

        verify_account.assert_awaited_once()
        self.assertFalse(account_cache_path(CLIENT_ID).exists())

    async def test_selected_legacy_account_requires_complete_device_client_info(self):
        denied = self.httpx.Response(400, json={"error": "invalid_grant"})
        self.authority(self.token(expires=0), denied, self.token())
        await self.remembered()(public_key())
        path = account_cache_path(CLIENT_ID)
        before = path.read_bytes()
        self.exchange.reset_mock()
        client_info = self.token(utid="").json()["client_info"]
        verify_account = AsyncMock()
        with (self.device_result_with_client_info(client_info),
              patch.object(xbox._verify, "verify_account", new=verify_account, create=True)):
            with self.assertRaisesRegex(XboxReauthenticationRequired, "could not confirm the selected account"):
                await self.provider._credentials_for_account(public_key(), legacy_account_id=ACCOUNT)

        self.exchange.assert_not_awaited()
        verify_account.assert_not_awaited()
        self.assertEqual(path.read_bytes(), before)

    async def test_ambiguous_account_authorizes_once_and_preserves_previous_cache(self):
        from msal import PublicClientApplication

        denied = self.httpx.Response(400, json={"error": "invalid_grant"})
        requests, _, _ = self.authority(self.token(expires=0), denied, self.token(uid="new-user"))
        provider = self.remembered()
        await provider(public_key())
        path = account_cache_path(CLIENT_ID)
        before = path.read_bytes()
        redeem = PublicClientApplication.acquire_token_by_device_flow

        def add_second_account(app, *args, **kwargs):
            result = redeem(app, *args, **kwargs)
            cache = app.token_cache
            kind = cache.CredentialType.ACCOUNT
            account, = cache.search(kind)
            other = dict(account, home_account_id="other-user.fake-tenant", local_account_id="other-user")
            cache.modify(kind, other, other)
            self.assertEqual(len(app.get_accounts()), 2)
            return result

        with patch.object(PublicClientApplication, "acquire_token_by_device_flow", new=add_second_account):
            key = public_key()
            self.assertEqual(await provider._credentials_with_remember_status(key), (self.credentials, True))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([str(request.url) for request in requests], [
            METADATA, DEVICE, TOKEN, METADATA, TOKEN, METADATA, DEVICE, TOKEN,
        ])
        client, token, exchanged_key = self.exchange.await_args.args
        self.assertEqual((token, exchanged_key), (ACCESS_TOKEN, key))
        self.verify.assert_awaited_with(client, self.credentials, key)

    async def test_overlapping_calls_report_their_own_remember_status(self):
        requests, _, _ = self.authority(self.token(refresh=None), self.token())
        provider = self.remembered()
        first_key = public_key()
        first_exchanging, finish_first = asyncio.Event(), asyncio.Event()

        async def exchange(_client, _token, key):
            if key == first_key:
                first_exchanging.set()
                await finish_first.wait()
            return self.credentials

        self.exchange.side_effect = exchange
        first = asyncio.create_task(provider._credentials_with_remember_status(first_key))
        try:
            async with asyncio.timeout(5):
                await first_exchanging.wait()
                self.assertEqual(await provider._credentials_with_remember_status(public_key()), (self.credentials, False))
                finish_first.set()
                self.assertEqual(await first, (self.credentials, True))
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)

        before = account_cache_path(CLIENT_ID).read_bytes()
        self.display.reset_mock()
        self.assertEqual(await provider._credentials_with_remember_status(public_key()), (self.credentials, False))
        self.assertEqual(account_cache_path(CLIENT_ID).read_bytes(), before)
        self.assertEqual([str(request.url) for request in requests], [METADATA, DEVICE, TOKEN] * 2 + [METADATA])
        self.display.assert_not_called()

    async def test_cache_write_failure_is_fatal_before_credential_exchange(self):
        self.authority(self.token())
        with patch("endpilot.xbox._cache.save_cache", side_effect=PermissionError(ACCESS_TOKEN)):
            with self.assertRaisesRegex(XboxAccountCacheError, "cache could not be accessed"):
                await self.remembered()(public_key())
        self.assertFalse(account_cache_path(CLIENT_ID).exists())
        self.exchange.assert_not_awaited()

    @skipUnless(find_spec("jwt"), "requires the Xbox extra")
    async def test_one_time_credentials_are_signed_and_bound_to_this_session_key(self):
        from test_xbox_provider_join import HttpIssuers, wire

        services = HttpIssuers()
        reply = self.token(refresh=None).json()
        reply["access_token"] = "local-msal-access"
        self.authority(*(self.httpx.Response(200, json=reply) for _ in range(2)))
        exchange = import_module("endpilot.xbox._exchange")
        verify = import_module("endpilot.xbox._verify")
        async_client = self.httpx.AsyncClient

        with (
            patch.object(xbox, "_exchange", exchange),
            patch.object(xbox, "_verify", verify),
            patch.object(verify, "_MOJANG_ROOT", wire.der64(services.root.public_key())),
            patch.object(self.httpx, "AsyncClient", side_effect=lambda **options: async_client(
                transport=self.httpx.MockTransport(services.exchange), **options,
            )),
        ):
            provider = self.remembered()
            accepted_key, rejected_key = public_key(), public_key()
            credentials = await provider(accepted_key)
            issued = services.sessions[accepted_key]
            self.assertEqual(credentials, BedrockCredentials(issued.chain, issued.token))
            services.fault = "wrong-key"
            with self.assertRaisesRegex(ValueError, "Bedrock credential verification failed"):
                await provider._credentials_with_remember_status(rejected_key)
        self.assertEqual(services.chain_requests, [accepted_key, rejected_key])
        self.assertEqual(services.token_requests, [accepted_key, rejected_key])
        self.assertEqual(services.calls.count("bedrock-jwks"), 2)
        self.assertFalse(account_cache_path(CLIENT_ID).exists())

    async def test_distinguishes_network_abort_from_reauthorization_and_preserves_the_cache(self):
        def unavailable():
            raise self.httpx.ConnectError(ACCESS_TOKEN)

        rejected = {"error": "invalid_grant", "error_description": ACCESS_TOKEN}
        requests, _, _ = self.authority(
            self.token(expires=0), unavailable, self.httpx.Response(429, json=rejected),
            self.httpx.Response(503, json=rejected), self.httpx.Response(400, json=rejected),
            self.httpx.Response(400, json=rejected), self.token(refresh="replacement-refresh"),
        )
        provider = self.remembered()
        await provider(public_key())
        path = account_cache_path(CLIENT_ID)
        before = path.read_bytes()
        for _ in range(3):
            with self.assertRaisesRegex(XboxAuthorizationUnavailable, "temporarily unavailable"):
                await provider(public_key())
            self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(XboxReauthenticationRequired):
            await self.provider(public_key())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sum(str(request.url) == DEVICE for request in requests), 1)
        self.display.assert_called_once()

        await provider(public_key())
        self.assertEqual(self.display.call_count, 2)
        entry, = self.saved_state()["RefreshToken"].values()
        self.assertEqual(entry["secret"], "replacement-refresh")

    async def test_foreign_refresh_identity_does_not_replace_the_saved_account(self):
        self.authority(self.token(expires=0), self.token(uid="different-account", refresh="foreign-refresh"))
        await self.remembered()(public_key())
        path = account_cache_path(CLIENT_ID)
        before = path.read_bytes()
        self.exchange.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "different account"):
            await XboxCredentialProvider(CLIENT_ID)(public_key())
        self.assertEqual(path.read_bytes(), before)
        self.exchange.assert_not_awaited()

    async def test_cancelled_refresh_finishes_rotation_without_exchanging_bedrock_credentials(self):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()

        def refreshing():
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise RuntimeError("test did not release refresh")
            return self.token(refresh="rotated-during-cancellation")

        requests, _, _ = self.authority(self.token(expires=0), refreshing)
        await self.remembered()(public_key())
        self.exchange.reset_mock()
        task = asyncio.create_task(XboxCredentialProvider(CLIENT_ID)(public_key()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
            if not task.done() and not task.cancelling():
                task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        entry, = self.saved_state()["RefreshToken"].values()
        self.assertEqual(entry["secret"], "rotated-during-cancellation")
        self.exchange.assert_not_awaited()
        await XboxCredentialProvider(CLIENT_ID)(public_key())
        self.assertEqual(sum(str(request.url) == TOKEN for request in requests), 2)

    async def test_cancelled_first_authorization_does_not_save_the_account(self):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()

        def first_token():
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise RuntimeError("test did not release the device poll")
            return self.token()

        self.authority(first_token)
        task = asyncio.create_task(self.remembered()(public_key()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
            if not task.done() and not task.cancelling():
                task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(account_cache_path(CLIENT_ID).exists())
        self.exchange.assert_not_awaited()

    async def test_malformed_cache_and_absent_selected_account_do_not_start_authorization(self):
        requests, _, _ = self.authority(self.token())
        await self.remembered()(public_key())
        path = account_cache_path(CLIENT_ID)
        before = json.loads(path.read_bytes())
        state = json.loads(before["serialized_cache"])
        entry, = state["Account"].values()
        del entry["authority_type"]
        path.write_text(json.dumps(dict(before, serialized_cache=json.dumps(state))), encoding="utf-8")
        with self.assertRaises(XboxAccountCacheError):
            await self.remembered()(public_key())

        path.write_text(json.dumps(dict(before, home_account_id="not-in-cache")), encoding="utf-8")
        with self.assertRaises(XboxReauthenticationRequired):
            await self.provider(public_key())
        self.assertEqual([str(request.url) for request in requests], [METADATA, DEVICE, TOKEN])
        self.display.assert_called_once()

    async def test_cache_discovery_permission_failure_does_not_start_device_authorization(self):
        requests, _, _ = self.authority()
        path = account_cache_path(CLIENT_ID)
        stat = Path.stat

        def denied(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError(ACCESS_TOKEN)
            return stat(candidate, *args, **kwargs)

        with patch.object(Path, "stat", new=denied):
            with self.assertRaisesRegex(XboxAccountCacheError, "cache could not be checked"):
                await self.provider(public_key())
        self.assertFalse(requests)
        self.display.assert_not_called()
