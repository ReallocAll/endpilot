"""Drive the real Xbox provider through mocked HTTP and both genuine game transports."""

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from importlib.util import find_spec
import json
import os
import struct
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from endpilot import Bot, LoginRefused
from endpilot.qa import PacketTimeline

import test_online_join as wire


# Check the application sent on the wire independently of the provider default.
CLIENT_ID = "e7cfcfa2-c0bb-419c-bf4a-fd966b7fe716"
MS_TENANT = "https://login.microsoftonline.com/consumers"
OAUTH_ENDPOINT = MS_TENANT + "/oauth2/v2.0"
RETAIL_ISSUER = "https://authorization.franchise.minecraft-services.net/"
DISCOVERY = RETAIL_ISSUER + ".well-known/openid-configuration"
JWKS = RETAIL_ISSUER + "local-jwks-test"
AUDIENCE = "api://auth-minecraft-services/multiplayer"
USER_TOKEN = "local-xbox-user"
MINECRAFT_XSTS = "local-xsts-minecraft"
PLAYFAB_XSTS = "local-xsts-playfab"
USER_HASH = "12345"
PLAYFAB_SESSION = "local-playfab-session"
SERVICE_HEADER = "MCToken local-service-token"
DEVICE_CODE = "local-device-grant"
USER_CODE = "F1XT-URE4"
VERIFICATION_URI = "https://www.microsoft.com/devicelogin"
XSTS_ROLES = {
    "https://multiplayer.minecraft.net/": ("minecraft", MINECRAFT_XSTS),
    "https://b980a380.minecraft.playfabapi.com/": ("playfab", PLAYFAB_XSTS),
}


def xbl_header(token):
    return f"XBL3.0 x={USER_HASH};{token}"


class HttpIssuers(wire.LocalAuthority):
    """Test-only services, with signed material keyed from the received HTTP body."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.sessions = {}
        self.prompts = []
        self.fault = None
        self.rogue = None
        self.exchange_reached = asyncio.Event()
        self.exchange_closed = asyncio.Event()
        self.token_requests = []
        self.chain_requests = []
        self.xbox_proofs = []

    async def provide(self, _spki):
        raise AssertionError("the dummy credential callback must not run")

    def credentials_for(self, spki):
        saved = self.sessions.get(spki)
        if saved is not None:
            return saved
        public = serialization.load_der_public_key(base64.b64decode(spki, validate=True))
        assert isinstance(public, ec.EllipticCurvePublicKey) and isinstance(public.curve, ec.SECP384R1)
        assert wire.der64(public) == spki
        now = int(time.time())
        dates = {"iat": now, "nbf": now - 10, "exp": now + 300}
        chain = (
            wire.ec_token(self.root, {**dates, "iss": "Mojang", "certificateAuthority": True,
                                      "identityPublicKey": wire.der64(self.delegate.public_key())}),
            wire.ec_token(self.delegate, {**dates, "iss": "Mojang", "identityPublicKey": spki,
                                          "extraData": {"displayName": wire.PROFILE,
                                                        "identity": str(wire.PLAYER_UUID), "XUID": wire.XUID}}),
        )
        signer = self.rogue or self.multiplayer
        token_public = wire.der64(self.root.public_key()) if self.fault == "wrong-key" else spki
        claims = {**dates, "iss": RETAIL_ISSUER, "aud": AUDIENCE, "cpk": token_public, "xid": wire.XUID,
                  "xname": wire.PROFILE, "ipt": "PlayFab", "mid": "12abcdef", "tid": "20CA2"}
        if self.fault == "expired":
            claims.update(iat=now - 20, nbf=now - 20, exp=now - 10)
        token = wire.issue(signer, {"alg": "RS256", "kid": "local-authority"}, claims)
        issued = wire.Issued(public, spki, chain, token)
        self.sessions[spki] = issued
        self.issued.append(issued)
        return issued

    def jwks(self):
        numbers = self.multiplayer.public_key().public_numbers()

        def integer(value):
            return wire.url64(value.to_bytes((value.bit_length() + 7) // 8, "big"))

        return {"keys": [{"kty": "RSA", "alg": "RS256", "use": "sig", "kid": "local-authority",
                          "n": integer(numbers.n), "e": integer(numbers.e)}]}

    def oauth(self, request):
        """Feed the synchronous HTTP client used by the actual MSAL grant."""
        import httpx

        path = request.url.path
        assert request.url.host == "login.microsoftonline.com"
        if path.endswith("/.well-known/openid-configuration"):
            assert request.method == "GET"
            name = "msal-discovery"
            reply = {"issuer": MS_TENANT + "/v2.0", "authorization_endpoint": OAUTH_ENDPOINT + "/authorize",
                     "token_endpoint": OAUTH_ENDPOINT + "/token", "device_authorization_endpoint": OAUTH_ENDPOINT + "/devicecode"}
        elif path.endswith("/discovery/instance"):
            name = "msal-instance"
            reply = {"tenant_discovery_endpoint": MS_TENANT + "/v2.0/.well-known/openid-configuration",
                     "metadata": [{"preferred_network": "login.microsoftonline.com",
                                   "preferred_cache": "login.microsoftonline.com",
                                   "aliases": ["login.microsoftonline.com"]}]}
        else:
            assert request.method == "POST"
            form = parse_qs(request.content.decode())
            assert form["client_id"] == [CLIENT_ID]
            if path.endswith("/devicecode"):
                name = "device-grant"
                assert "xboxlive.signin" in form["scope"][0].lower().split()
                reply = {"device_code": DEVICE_CODE, "user_code": USER_CODE,
                         "verification_uri": VERIFICATION_URI,
                         "expires_in": 300, "interval": 1,
                         "message": "local fixture device prompt"}
            elif path.endswith("/token"):
                name = "device-redemption"
                assert form["device_code"] == [DEVICE_CODE]
                assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:device_code"]
                reply = {"token_type": "Bearer", "access_token": "local-msal-access",
                         "expires_in": 600, "scope": "XboxLive.signin"}
            else:
                raise AssertionError(f"unexpected MSAL route {path}")
        self.calls.append(name)
        return httpx.Response(200, json=reply)

    def verify_xbox_proof(self, request, jwk):
        assert (jwk["kty"], jwk["crv"], jwk["alg"]) == ("EC", "P-256", "ES256")
        x, y = (int.from_bytes(wire.unurl(jwk[coordinate]), "big") for coordinate in ("x", "y"))
        public = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
        signature = base64.b64decode(request.headers["Signature"], validate=True)
        assert len(signature) == 76 and request.headers["x-xbl-contract-version"] == "1"
        version, filetime = struct.unpack_from(">IQ", signature)
        assert version == 1 and abs((filetime - 116444736000000000) / 10**7 - time.time()) < 60
        signing_input = (signature[:4] + b"\0" + signature[4:12] + b"\0POST\0"
                         + request.url.raw_path + b"\0\0" + request.content + b"\0")
        r, s = (int.from_bytes(part, "big") for part in (signature[12:44], signature[44:76]))
        public.verify(encode_dss_signature(r, s), signing_input, ec.ECDSA(hashes.SHA256()))
        self.xbox_proofs.append(wire.der64(public))

    async def exchange(self, request):
        """Receive normal httpx requests for Xbox, PlayFab, Bedrock and JWKS."""
        import httpx

        url = request.url
        path = url.path
        if request.method == "GET":
            assert not request.headers.get("Authorization"), "credentials must not reach key discovery"
            if str(url) == DISCOVERY:
                self.calls.append("bedrock-discovery")
                reply = {"issuer": RETAIL_ISSUER, "jwks_uri": JWKS}
            elif str(url) == JWKS:
                self.calls.append("bedrock-jwks")
                reply = self.jwks()
            else:
                raise AssertionError(f"unexpected discovery route {url.host}{path}")
            return httpx.Response(200, json=reply)

        assert request.method == "POST"
        data = json.loads(request.content)
        expiry = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        if url.host == "user.auth.xboxlive.com" and path == "/user/authenticate":
            self.calls.append("xbox-user")
            assert data["Properties"]["RpsTicket"] == "d=local-msal-access"
            self.verify_xbox_proof(request, data["Properties"]["ProofKey"])
            reply = {"Token": USER_TOKEN, "NotAfter": expiry, "DisplayClaims": {"xui": [{"uhs": USER_HASH}]}}
        elif url.host == "xsts.auth.xboxlive.com" and path == "/xsts/authorize":
            assert data["Properties"]["UserTokens"] == [USER_TOKEN]
            assert data["Properties"]["SandboxId"] == "RETAIL"
            role, token = XSTS_ROLES[data["RelyingParty"]]
            self.verify_xbox_proof(request, data["Properties"]["ProofKey"])
            self.calls.append("xsts-" + role)
            reply = {"Token": token, "NotAfter": expiry, "DisplayClaims": {"xui": [{"uhs": USER_HASH, "xid": wire.XUID}]}}
        elif url.host == "20ca2.playfabapi.com" and path == "/Client/LoginWithXbox":
            self.calls.append("playfab")
            assert data["XboxToken"] == xbl_header(PLAYFAB_XSTS)
            assert data["TitleId"].lower() == "20ca2"
            reply = {"code": 200, "status": "OK", "data": {"SessionTicket": PLAYFAB_SESSION}}
        elif url.host == "multiplayer.minecraft.net" and path == "/authentication":
            self.calls.append("certificate")
            assert request.headers["Authorization"] == xbl_header(MINECRAFT_XSTS)
            assert request.headers["Client-Version"] == "1.26.66"
            issued = self.credentials_for(data["identityPublicKey"])
            self.chain_requests.append(issued.spki)
            reply = {"chain": list(issued.chain)}
        elif url.host == urlsplit(RETAIL_ISSUER).hostname and path == "/api/v1.0/session/start":
            self.calls.append("bedrock-session")
            assert (data["user"]["token"], data["user"]["tokenType"]) == (PLAYFAB_SESSION, "PlayFab")
            assert data["device"]["gameVersion"] == "1.26.66"
            reply = {"result": {"authorizationHeader": SERVICE_HEADER, "validUntil": expiry}}
        elif url.host == urlsplit(RETAIL_ISSUER).hostname and path == "/api/v1.0/multiplayer/session/start":
            self.calls.append("multiplayer")
            assert request.headers["Authorization"] == SERVICE_HEADER
            issued = self.credentials_for(data["publicKey"])
            self.token_requests.append(issued.spki)
            if self.fault == "pause":
                self.exchange_reached.set()
                try:
                    await asyncio.Future()
                finally:
                    self.exchange_closed.set()
            reply = {"result": {"signedToken": issued.token}}
        else:
            raise AssertionError(f"unexpected exchange route {url.host}{path}")
        return httpx.Response(200, json=reply)


@unittest.skipUnless(
    all(find_spec(name) for name in ("msal", "httpx", "jwt", "raknet", "nethernet", "aiohttp")),
    "install the xbox, raknet and nethernet HTTP extras",
)
class XboxProviderJoinTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import httpx
        from endpilot.xbox import XboxCredentialProvider, _verify

        home = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "HOME": home, "USERPROFILE": home, "LOCALAPPDATA": home, "XDG_STATE_HOME": home,
        }))
        self.services = HttpIssuers()
        oauth_mock = httpx.MockTransport(self.services.oauth)
        exchange_mock = httpx.MockTransport(self.services.exchange)

        def oauth(_transport, request):
            return oauth_mock.handle_request(request)

        async def exchange(_transport, request):
            return await exchange_mock.handle_async_request(request)

        self.enterContext(patch.object(httpx.HTTPTransport, "handle_request", new=oauth))
        self.enterContext(patch.object(httpx.AsyncHTTPTransport, "handle_async_request", new=exchange))
        self.enterContext(patch.object(_verify, "_MOJANG_ROOT", wire.der64(self.services.root.public_key())))
        # The server-side oracle verifies the same retail issuer independently.
        self.enterContext(patch.object(wire, "ISSUER", RETAIL_ISSUER))
        self.provider = XboxCredentialProvider(
            on_device_code=lambda uri, code: self.services.prompts.append((uri, code)),
        )
        self.bot = Bot("LocalFallback")
        self.addAsyncCleanup(self.bot.close)

    async def spawn(self, peer):
        timeline = PacketTimeline()
        try:
            await peer.join(self.bot, timeline, provider=self.provider)
            await peer.wait(peer.initialized)
            self.assertEqual(await self.bot.wait_for_position_sync(timeout=3), wire.SPAWN)
            self.assertEqual((self.bot.identity.name, self.bot.identity.uuid), (wire.PROFILE, wire.PLAYER_UUID))
            self.assertEqual(timeline.sent_count("LoginPacket"), 1)
            self.assertEqual(timeline.sent_count("SetLocalPlayerAsInitializedPacket"), 1)
            await self.bot.send_command("list")
            await self.bot.change_skin("provider-fixture")
            await peer.wait(peer.post_spawn_checked)
        except Exception:
            if peer.failure is not None:
                raise peer.failure
            raise
        finally:
            await self.bot.close()
        await peer.wait(peer.disconnected)
        self.assertEqual(peer.login_keys, [self.services.issued[-1].spki])
        if peer.transport == "nethernet":
            self.assertEqual(peer.offer_keys, peer.login_keys)
        else:
            self.assertTrue(peer.verified_ciphertext)

    async def test_real_device_flow_drives_both_public_transports(self):
        for transport in ("raknet", "nethernet"):
            with self.subTest(transport=transport):
                start = len(self.services.calls)
                async with wire.LocalServer(self.services, transport).listen() as peer:
                    await self.spawn(peer)
                calls = self.services.calls[start:]
                self.assertEqual(set(calls) - {"msal-instance"}, {
                    "msal-discovery", "device-grant", "device-redemption", "xbox-user", "xsts-minecraft", "xsts-playfab",
                    "certificate", "playfab", "bedrock-session", "multiplayer", "bedrock-discovery", "bedrock-jwks",
                })
                for previous, following in (("device-grant", "device-redemption"), ("device-redemption", "xbox-user"),
                                            ("xsts-minecraft", "certificate"), ("xsts-playfab", "playfab"),
                                            ("playfab", "bedrock-session"), ("bedrock-session", "multiplayer"),
                                            ("bedrock-discovery", "bedrock-jwks")):
                    self.assertLess(calls.index(previous), calls.index(following))
        self.assertEqual(self.services.prompts, [(VERIFICATION_URI, USER_CODE)] * 2)
        keys = self.services.token_requests
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2)
        self.assertEqual(keys, self.services.chain_requests)
        first, second = self.services.xbox_proofs[:3], self.services.xbox_proofs[3:]
        self.assertEqual((len(first), len(second)), (3, 3))
        self.assertEqual((len(set(first)), len(set(second))), (1, 1))
        self.assertNotEqual(first, second)
        self.assertTrue(set(keys).isdisjoint(self.services.xbox_proofs))

    async def test_server_refusal_closes_then_rejoins_with_new_issued_key(self):
        async with wire.LocalServer(self.services, "nethernet", reject=True).listen() as denied:
            with self.assertRaisesRegex(LoginRefused, "local test refusal"):
                await denied.join(self.bot, provider=self.provider)
            await denied.wait(denied.disconnected)
            self.assertFalse(denied.initialized.is_set())
            self.assertEqual(denied.login_keys, denied.offer_keys)
        async with wire.LocalServer(self.services, "nethernet").listen() as accepted:
            await self.spawn(accepted)
            self.assertNotEqual(denied.login_keys, accepted.login_keys)
        self.assertEqual(self.services.chain_requests, self.services.token_requests)

    async def test_invalid_issued_claims_are_rejected_before_join_and_retry_gets_new_key(self):
        async with wire.LocalServer(self.services, "raknet").listen() as peer:
            for fault in ("wrong-signer", "expired", "wrong-key"):
                with self.subTest(fault=fault):
                    start = len(self.services.calls)
                    self.services.fault = fault
                    if fault == "wrong-signer":
                        self.services.rogue = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                    with self.assertRaisesRegex(ValueError, "credential verification failed"):
                        await peer.join(self.bot, provider=self.provider)
                    self.assertEqual((peer.connections, peer.login_keys), (0, []))
                    self.assertIn("bedrock-jwks", self.services.calls[start:])
                    self.services.rogue = None
            self.services.fault = None
            await self.spawn(peer)
        self.assertEqual(len(set(self.services.token_requests)), 4)
        self.assertEqual(self.services.token_requests, self.services.chain_requests)

    async def test_cancel_inflight_credential_exchange_then_rejoin_with_new_key(self):
        self.services.fault = "pause"
        async with wire.LocalServer(self.services, "nethernet").listen() as peer:
            joining = asyncio.create_task(peer.join(self.bot, provider=self.provider))
            try:
                await asyncio.wait_for(self.services.exchange_reached.wait(), 4)
                joining.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await joining
                await asyncio.wait_for(self.services.exchange_closed.wait(), 4)
                self.assertEqual((peer.connections, peer.offer_keys), (0, []))
                self.services.fault = None
                await self.spawn(peer)
            finally:
                if not joining.done():
                    joining.cancel()
                await asyncio.gather(joining, return_exceptions=True)
        self.assertEqual(len(self.services.token_requests), 2)
        self.assertEqual(len(set(self.services.token_requests)), 2)
