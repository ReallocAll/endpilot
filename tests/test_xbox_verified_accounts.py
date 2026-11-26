"""Exercise the shared account path with MSAL and locally signed Bedrock credentials."""

import asyncio
import base64
from importlib import import_module
from importlib.util import find_spec
import json
import os
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import parse_qs

from endpilot import BedrockCredentials, xbox
from endpilot.authentication import acquire_online_identity
from test_xbox_device_flow import CLIENT_ID, DEVICE, TOKEN, _MsalTestCase, public_key
from test_xbox_provider_join import HttpIssuers, wire


_HAS_XBOX = all(find_spec(name) is not None for name in ("httpx", "jwt", "msal")) and (
    os.name != "nt" or find_spec("win32security") is not None
)
if _HAS_XBOX:
    from msal import PublicClientApplication

    from endpilot.xbox import _cache


ALPHA = (wire.XUID, wire.PROFILE)
BETA = ("987654321", "Second Local Player")
ALPHA_ACCESS = "local-msal-access"
BETA_ACCESS = "local-msal-beta"
ALPHA_ACCOUNT = "first-user.fixture-tenant"
BETA_ACCOUNT = "second-user.fixture-tenant"


@skipUnless(_HAS_XBOX, "requires the Xbox extra")
class VerifiedXboxAccountTests(_MsalTestCase):
    def setUp(self):
        super().setUp()
        self.services = HttpIssuers()
        self.profiles = {ALPHA_ACCESS: ALPHA, BETA_ACCESS: BETA}
        self.verify_module = import_module("endpilot.xbox._verify")
        self.enterContext(patch.object(xbox, "_verify", self.verify_module))
        self.enterContext(patch.object(self.verify_module, "_MOJANG_ROOT", wire.der64(self.services.root.public_key())))
        construct = self.httpx.AsyncClient
        self.enterContext(patch.object(self.httpx, "AsyncClient", side_effect=lambda **options: construct(
            transport=self.httpx.MockTransport(self.services.exchange), **options,
        )))
        self.exchange.side_effect = self.issue_credentials
        self.path = xbox.account_cache_path(CLIENT_ID)
        self.authorize = self.provider._credentials_for_account

    async def issue_credentials(self, _client, token, key):
        xuid, name = self.profiles[token]
        with patch.object(wire, "XUID", xuid), patch.object(wire, "PROFILE", name):
            issued = self.services.credentials_for(key)
        return BedrockCredentials(issued.chain, issued.token)

    def token(self, *, uid="first-user", access=ALPHA_ACCESS, refresh="first-refresh", expires=3600):
        body = {
            "access_token": access, "expires_in": expires, "token_type": "Bearer", "scope": "XboxLive.signin",
        }
        if uid is not None:
            identity = json.dumps({"uid": uid, "utid": "fixture-tenant"}).encode()
            body["client_info"] = base64.urlsafe_b64encode(identity).decode()
        if refresh is not None:
            body["refresh_token"] = refresh
        return self.httpx.Response(200, json=body)

    def accounts(self):
        return {record.xuid: record for record in _cache.load_accounts(self.path)}

    async def remember_legacy_account(self):
        legacy = xbox.XboxCredentialProvider(CLIENT_ID, on_device_code=self.display, remember_account=True)
        await legacy(public_key())
        self.assertEqual(json.loads(self.path.read_bytes())["version"], 1)
        self.display.reset_mock()
        self.exchange.reset_mock()
        return self.path.read_bytes()

    async def test_standalone_add_and_silent_join_share_the_service_exchange(self):
        requests, _, _ = self.authority(self.token())
        exchange = import_module("endpilot.xbox._exchange")
        self.enterContext(patch.object(xbox, "_exchange", exchange))
        key = public_key()
        credentials, profile, skipped = await self.authorize(key, add=True)
        self.assertEqual(profile, ALPHA)
        self.assertFalse(skipped)
        self.assertEqual(self.accounts()[ALPHA[0]].home_account_id, ALPHA_ACCOUNT)
        before = self.path.read_bytes()
        for secret in (key, *credentials.certificate_chain, credentials.multiplayer_token):
            self.assertNotIn(secret.encode(), before)

        async def provider(join_key):
            supplied, identity, skipped = await self.authorize(join_key)
            self.assertEqual(identity, ALPHA)
            self.assertFalse(skipped)
            return supplied

        identity = await acquire_online_identity(provider)
        self.assertEqual(identity.name, ALPHA[1])
        join_key = wire.der64(identity.key.public_key())
        self.assertNotEqual(key, join_key)
        self.assertEqual(self.services.chain_requests, [key, join_key])
        self.assertEqual(self.services.token_requests, [key, join_key])
        self.assertEqual(self.services.calls.count("bedrock-jwks"), 2)
        self.assertEqual(sum(str(request.url) == TOKEN for request in requests), 1)
        self.display.assert_called_once()
        self.assertEqual(self.path.read_bytes(), before)

    async def test_only_selected_account_is_refreshed_and_renamed(self):
        requests, _, _ = self.authority(
            self.token(expires=0),
            self.token(uid="second-user", access=BETA_ACCESS, refresh="second-refresh", expires=0),
            self.token(uid="second-user", access=BETA_ACCESS, refresh="second-rotated"),
        )
        await self.authorize(public_key())
        await self.authorize(public_key(), add=True)
        original = self.accounts()
        self.assertEqual(original[ALPHA[0]].home_account_id, ALPHA_ACCOUNT)
        self.assertEqual(original[BETA[0]].home_account_id, BETA_ACCOUNT)

        before_requests = len(requests)
        for xuid in ("001", "9912345"):
            with self.subTest(xuid=xuid), self.assertRaises(ValueError):
                await self.authorize(public_key(), xuid=xuid)
        with self.assertRaisesRegex(xbox.XboxAccountCacheError, "multiple accounts"):
            await self.authorize(public_key())
        self.assertEqual(len(requests), before_requests)

        renamed = (BETA[0], "Renamed Second Player")
        self.profiles[BETA_ACCESS] = renamed
        credentials, profile, skipped = await self.authorize(public_key(), xuid=BETA[0])
        self.assertEqual(profile, renamed)
        self.assertFalse(skipped)
        self.assertIsInstance(credentials, BedrockCredentials)
        saved = self.accounts()
        self.assertEqual(saved[ALPHA[0]], original[ALPHA[0]])
        self.assertEqual(saved[BETA[0]].gamertag, renamed[1])
        refresh, = json.loads(saved[BETA[0]].serialized_cache)["RefreshToken"].values()
        self.assertEqual(refresh["secret"], "second-rotated")
        forms = [parse_qs(request.content.decode()) for request in requests if str(request.url) == TOKEN]
        self.assertEqual([form.get("refresh_token") for form in forms], [None, None, ["second-refresh"]])
        self.assertEqual(self.display.call_count, 2)

    async def test_selected_account_rejects_foreign_microsoft_and_signed_xbox_identities(self):
        requests, _, _ = self.authority(
            self.token(expires=0),
            self.token(uid="second-user", access=BETA_ACCESS, refresh="wrong-microsoft-refresh"),
            self.token(uid=None, access=BETA_ACCESS, refresh="wrong-xbox-refresh"),
        )
        await self.authorize(public_key())
        original = self.path.read_bytes()
        self.exchange.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "different account"):
            await self.authorize(public_key(), xuid=ALPHA[0])
        self.exchange.assert_not_awaited()
        self.assertEqual(self.path.read_bytes(), original)
        with self.assertRaisesRegex(ValueError, "does not match the selected account"):
            await self.authorize(public_key(), xuid=ALPHA[0])
        self.exchange.assert_awaited_once()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(sum(str(request.url) == DEVICE for request in requests), 1)

    async def test_device_authorization_saves_only_after_proof_and_can_continue_once(self):
        self.authority(self.token(), self.token(refresh=None))
        self.services.fault = "wrong-key"
        with self.assertRaisesRegex(ValueError, "Bedrock credential verification failed"):
            await self.authorize(public_key(), add=True)
        self.assertFalse(self.path.exists())
        self.services.fault = None
        credentials, profile, skipped = await self.authorize(public_key())
        self.assertIsInstance(credentials, BedrockCredentials)
        self.assertEqual(profile, ALPHA)
        self.assertTrue(skipped)
        self.assertFalse(self.path.exists())

    async def test_device_token_must_match_the_selected_ms_account(self):
        self.authority(self.token())
        redeem = PublicClientApplication.acquire_token_by_device_flow

        def different_token(app, *args, **kwargs):
            return dict(redeem(app, *args, **kwargs), access_token=BETA_ACCESS)

        with patch.object(PublicClientApplication, "acquire_token_by_device_flow", new=different_token):
            _, profile, skipped = await self.authorize(public_key(), add=True)
        self.assertEqual(profile, BETA)
        self.assertTrue(skipped)
        self.assertFalse(self.path.exists())

    async def test_legacy_account_is_upgraded_after_its_first_verified_selection(self):
        requests, _, _ = self.authority(self.token())
        legacy = xbox.XboxCredentialProvider(CLIENT_ID, on_device_code=self.display, remember_account=True)
        await legacy(public_key())
        self.assertEqual(json.loads(self.path.read_bytes())["version"], 1)
        self.services.fault = "wrong-key"
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Bedrock credential verification failed"):
            await self.authorize(public_key())
        self.assertEqual(self.path.read_bytes(), before)
        self.services.fault = None
        _, profile, skipped = await self.authorize(public_key())
        self.assertEqual(profile, ALPHA)
        self.assertFalse(skipped)
        self.assertEqual(json.loads(self.path.read_bytes())["version"], 2)
        self.assertEqual(self.accounts()[ALPHA[0]].home_account_id, ALPHA_ACCOUNT)
        self.assertEqual(sum(str(request.url) == TOKEN for request in requests), 1)
        self.display.assert_called_once()

    async def test_explicitly_selected_legacy_row_upgrades_without_refreshing_the_other_account(self):
        requests, _, _ = self.authority(
            self.token(expires=0),
            self.token(uid="second-user", access=BETA_ACCESS, refresh="second-refresh", expires=0),
            self.token(refresh="first-rotated"),
        )
        legacy = xbox.XboxCredentialProvider(CLIENT_ID, on_device_code=self.display, remember_account=True)
        await legacy(public_key())
        await self.authorize(public_key(), add=True)
        original = self.accounts()
        self.assertEqual(original[None].home_account_id, ALPHA_ACCOUNT)
        self.assertEqual(original[BETA[0]].home_account_id, BETA_ACCOUNT)

        before_requests = len(requests)
        for stale_id in ("removed-user.fixture-tenant", BETA_ACCOUNT):
            with self.subTest(stale_id=stale_id), self.assertRaisesRegex(ValueError, "legacy Xbox account"):
                await self.authorize(public_key(), legacy_account_id=stale_id)
        self.assertEqual(len(requests), before_requests)

        _, profile, skipped = await self.authorize(public_key(), legacy_account_id=original[None].home_account_id)
        self.assertEqual(profile, ALPHA)
        self.assertFalse(skipped)
        saved = self.accounts()
        self.assertEqual(set(saved), {ALPHA[0], BETA[0]})
        self.assertEqual(saved[ALPHA[0]].home_account_id, ALPHA_ACCOUNT)
        self.assertEqual(saved[BETA[0]], original[BETA[0]])
        forms = [parse_qs(request.content.decode()) for request in requests if str(request.url) == TOKEN]
        self.assertEqual([form.get("refresh_token") for form in forms], [None, None, ["first-refresh"]])
        self.assertEqual(self.display.call_count, 2)

    async def test_legacy_refresh_without_identity_uses_one_device_round_for_the_same_account(self):
        def confirm():
            self.assertEqual(self.path.read_bytes(), before)
            return self.token(refresh="confirmed-refresh")

        requests, _, _ = self.authority(
            self.token(expires=0), self.token(uid=None, access=BETA_ACCESS, refresh="unconfirmed-refresh"), confirm,
        )
        before = await self.remember_legacy_account()
        key = public_key()
        _, profile, skipped = await self.authorize(key, legacy_account_id=ALPHA_ACCOUNT)
        self.assertEqual(profile, ALPHA)
        self.assertFalse(skipped)
        self.exchange.assert_awaited_once()
        self.assertEqual(self.exchange.await_args.args[1:], (ALPHA_ACCESS, key))
        self.display.assert_called_once()
        saved = self.accounts()[ALPHA[0]]
        self.assertEqual(saved.home_account_id, ALPHA_ACCOUNT)
        refresh, = json.loads(saved.serialized_cache)["RefreshToken"].values()
        self.assertEqual(refresh["secret"], "confirmed-refresh")
        forms = [parse_qs(request.content.decode()) for request in requests if str(request.url) == TOKEN]
        self.assertEqual([form.get("refresh_token") for form in forms], [None, ["first-refresh"], None])

    async def test_legacy_refresh_without_identity_needs_reauthorization_without_a_callback(self):
        requests, _, _ = self.authority(self.token(expires=0), self.token(uid=None, access=BETA_ACCESS))
        before = await self.remember_legacy_account()
        no_prompt = xbox.XboxCredentialProvider(CLIENT_ID)
        with self.assertRaises(xbox.XboxReauthenticationRequired):
            await no_prompt._credentials_for_account(public_key())
        self.assertEqual(sum(str(request.url) == DEVICE for request in requests), 1)
        self.exchange.assert_not_awaited()
        self.assertEqual(self.path.read_bytes(), before)

    async def test_expired_selected_legacy_grant_reauthorizes_once_with_a_callback(self):
        requests, _, _ = self.authority(
            self.token(expires=0),
            self.httpx.Response(503, json={"error": "server_error"}),
            self.httpx.Response(400, json={"error": "invalid_grant"}),
            self.httpx.Response(400, json={"error": "invalid_grant"}),
            self.token(refresh="replacement-refresh"),
        )
        before = await self.remember_legacy_account()
        no_prompt = xbox.XboxCredentialProvider(CLIENT_ID)
        with self.assertRaises(xbox.XboxAuthorizationUnavailable):
            await self.authorize(public_key(), legacy_account_id=ALPHA_ACCOUNT)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(xbox.XboxReauthenticationRequired):
            await no_prompt._credentials_for_account(public_key(), legacy_account_id=ALPHA_ACCOUNT)
        self.assertEqual(self.path.read_bytes(), before)
        self.display.assert_not_called()
        self.exchange.assert_not_awaited()

        key = public_key()
        _, profile, skipped = await self.authorize(key, legacy_account_id=ALPHA_ACCOUNT)
        self.assertEqual(profile, ALPHA)
        self.assertFalse(skipped)
        self.display.assert_called_once()
        self.exchange.assert_awaited_once()
        self.assertEqual(self.exchange.await_args.args[1:], (ALPHA_ACCESS, key))
        saved = self.accounts()[ALPHA[0]]
        self.assertEqual(saved.home_account_id, ALPHA_ACCOUNT)
        refresh, = json.loads(saved.serialized_cache)["RefreshToken"].values()
        self.assertEqual(refresh["secret"], "replacement-refresh")
        self.assertEqual(sum(str(request.url) == DEVICE for request in requests), 2)

    async def test_legacy_refresh_unknown_source_uses_the_confirmed_one_time_device_credentials(self):
        self.authority(
            self.token(expires=0), self.token(access=BETA_ACCESS, refresh="unconfirmed-refresh"), self.token(refresh=None),
        )
        before = await self.remember_legacy_account()
        silent = PublicClientApplication.acquire_token_silent_with_error

        def unknown_source(app, *args, **kwargs):
            return dict(silent(app, *args, **kwargs), token_source="unclassified")

        key = public_key()
        with patch.object(PublicClientApplication, "acquire_token_silent_with_error", new=unknown_source):
            credentials, profile, skipped = await self.authorize(key)
        self.assertTrue(skipped)
        self.assertEqual(profile, ALPHA)
        self.assertIsInstance(credentials, BedrockCredentials)
        self.display.assert_called_once()
        self.exchange.assert_awaited_once()
        self.assertEqual(self.exchange.await_args.args[1:], (ALPHA_ACCESS, key))
        self.assertEqual(self.path.read_bytes(), before)

    async def test_legacy_refresh_for_another_microsoft_account_does_not_open_device_authorization(self):
        self.authority(self.token(expires=0), self.token(uid="second-user", access=BETA_ACCESS))
        before = await self.remember_legacy_account()
        with self.assertRaisesRegex(RuntimeError, "silent authorization returned a different account"):
            await self.authorize(public_key())
        self.display.assert_not_called()
        self.exchange.assert_not_awaited()
        self.assertEqual(self.path.read_bytes(), before)

    async def test_legacy_device_reauthorization_must_confirm_the_original_microsoft_account(self):
        self.authority(
            self.token(expires=0),
            self.token(uid=None, access=BETA_ACCESS), self.token(uid="second-user", access=BETA_ACCESS),
            self.token(uid=None, access=BETA_ACCESS), self.token(uid=None),
        )
        before = await self.remember_legacy_account()
        with self.assertRaisesRegex(RuntimeError, "device authorization returned a different account"):
            await self.authorize(public_key())
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaisesRegex(xbox.XboxReauthenticationRequired, "could not confirm the selected account"):
            await self.authorize(public_key())
        self.assertEqual(self.path.read_bytes(), before)
        self.exchange.assert_not_awaited()
        self.assertEqual(self.display.call_count, 2)

    async def test_cancelled_device_reauthorization_keeps_the_original_legacy_record(self):
        self.authority(
            self.token(expires=0), self.token(uid=None, access=BETA_ACCESS),
            self.httpx.Response(400, json={"error": "authorization_pending"}),
        )
        before = await self.remember_legacy_account()
        entered = asyncio.Event()

        async def wait_for_device(_delay):
            entered.set()
            await asyncio.Future()

        with patch.object(xbox, "sleep", new=wait_for_device):
            pending = asyncio.create_task(self.authorize(public_key()))
            try:
                await asyncio.wait_for(entered.wait(), 2)
            finally:
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
        self.exchange.assert_not_awaited()
        self.display.assert_called_once()
        self.assertEqual(self.path.read_bytes(), before)

    async def test_selected_unavailable_or_revoked_accounts_do_not_start_device_authorization(self):
        requests, _, _ = self.authority(
            self.token(expires=0),
            self.httpx.Response(503, json={"error": "server_error"}),
            self.httpx.Response(400, json={"error": "invalid_grant"}),
        )
        await self.authorize(public_key())
        original = self.path.read_bytes()
        for error in (xbox.XboxAuthorizationUnavailable, xbox.XboxReauthenticationRequired):
            with self.assertRaises(error):
                await self.authorize(public_key(), xuid=ALPHA[0])
            self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(sum(str(request.url) == DEVICE for request in requests), 1)

    async def test_implicit_join_reauthorizes_only_after_the_saved_grant_is_revoked(self):
        requests, _, _ = self.authority(
            self.token(expires=0),
            self.httpx.Response(400, json={"error": "invalid_grant"}),
            self.token(refresh="replacement-refresh"),
        )
        await self.authorize(public_key())
        _, profile, skipped = await self.authorize(public_key())
        self.assertEqual(profile, ALPHA)
        self.assertFalse(skipped)
        saved, = _cache.load_accounts(self.path)
        self.assertEqual(saved.home_account_id, ALPHA_ACCOUNT)
        refresh, = json.loads(saved.serialized_cache)["RefreshToken"].values()
        self.assertEqual(refresh["secret"], "replacement-refresh")
        self.assertEqual(sum(str(request.url) == DEVICE for request in requests), 2)

    async def test_cancellation_during_verified_refresh_keeps_the_committed_record(self):
        self.authority(self.token(expires=0), self.token(refresh="not-yet-verified-refresh"))
        await self.authorize(public_key())
        original = self.path.read_bytes()
        entered = asyncio.Event()

        async def wait_for_proof(_client, _token, _key):
            entered.set()
            await asyncio.Future()

        self.exchange.side_effect = wait_for_proof
        pending = asyncio.create_task(self.authorize(public_key(), xuid=ALPHA[0]))
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assertEqual(self.path.read_bytes(), original)

    async def test_genuine_proof_cannot_link_one_xuid_to_two_microsoft_accounts(self):
        self.authority(self.token(), self.token(uid="second-user", refresh="second-refresh"))
        await self.authorize(public_key())
        original = self.path.read_bytes()
        with self.assertRaises(xbox.XboxAccountCacheError):
            await self.authorize(public_key(), add=True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.display.call_count, 2)
