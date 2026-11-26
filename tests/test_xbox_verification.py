"""Verify the Xbox provider's trust boundary without a real issuer or account."""

import asyncio
import base64
from importlib.util import find_spec
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from endpilot import BedrockCredentials

_HAS_XBOX = find_spec("httpx") is not None and find_spec("jwt") is not None
if _HAS_XBOX:
    import httpx
    import jwt

    from endpilot.xbox import _verify


_ISSUER = "https://authorization.franchise.minecraft-services.net/"
_DISCOVERY = _ISSUER + ".well-known/openid-configuration"
_AUDIENCE = "api://auth-minecraft-services/multiplayer"
_JWKS = "https://keys.example.invalid/fixtures/current"
_KEY_ID = "local-signing-key"
_PRIVATE = "fixture-data-must-not-appear-in-errors"


def _spki(public_key):
    data = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(data).decode("ascii")


@unittest.skipUnless(_HAS_XBOX, "install the Xbox extra to exercise credential verification")
class XboxCredentialVerificationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ec.generate_private_key(ec.SECP384R1())
        cls.delegate = ec.generate_private_key(ec.SECP384R1())
        cls.session_key = ec.generate_private_key(ec.SECP384R1()).public_key()
        cls.other_session_key = ec.generate_private_key(ec.SECP384R1()).public_key()
        cls.rsa_authority = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_rsa_authority = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.enterContext(patch.object(_verify, "_MOJANG_ROOT", _spki(self.root.public_key())))

    def credentials(self, *, claims=None, dates=None, leaf_key=None, root=None, token_signer=None, algorithm="RS256"):
        now = int(time.time())
        root = self.root if root is None else root
        dates = {"exp": now + 300, "iat": now - 1} | (dates or {})
        profile = {"XUID": "1234567890", "displayName": "Local Test Fixture"}
        first = jwt.encode({"iss": "Mojang", "identityPublicKey": _spki(self.delegate.public_key()), **dates},
                           root, algorithm="ES384", headers={"x5u": _spki(root.public_key())})
        leaf = jwt.encode({"iss": "Mojang", "identityPublicKey": _spki(leaf_key or self.session_key),
                           "extraData": profile, **dates}, self.delegate, algorithm="ES384")
        token_signer = token_signer or (self.rsa_authority if algorithm == "RS256" else self.root)
        token = jwt.encode({
            "iss": _ISSUER, "aud": _AUDIENCE, "exp": now + 300, "iat": now - 1,
            "cpk": _spki(self.session_key), "xid": profile["XUID"], "xname": profile["displayName"],
        } | (claims or {}), token_signer, algorithm=algorithm,
            headers={"kid": _KEY_ID, "jku": "https://attacker.example.invalid/never-fetch"})
        return BedrockCredentials((first, leaf), token)

    def signing_jwk(self, algorithm="RS256"):
        if algorithm == "RS256":
            key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.rsa_authority.public_key()))
        else:
            key = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.root.public_key()))
        return {**key, "kid": _KEY_ID, "alg": algorithm, "use": "sig", "key_ops": ["verify"]}

    async def verify(self, credentials, *, metadata=None, keys=None, discovery_status=200, jwks_status=200):
        self.requests = []
        metadata = {"issuer": _ISSUER, "jwks_uri": _JWKS} if metadata is None else metadata
        keys = [self.signing_jwk()] if keys is None else keys

        def serve(request):
            url = str(request.url)
            self.requests.append(url)
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
            if url == _DISCOVERY:
                return httpx.Response(discovery_status, json=metadata, headers={"Location": "https://redirect.invalid/"})
            if url == _JWKS:
                return httpx.Response(jwks_status, json={"keys": keys, "private": _PRIVATE},
                                      headers={"Location": "https://redirect.invalid/"})
            self.fail("unexpected network URL")

        async with httpx.AsyncClient(transport=httpx.MockTransport(serve), follow_redirects=True,
                                     auth=("default-user", _PRIVATE), cookies={"secret": _PRIVATE},
                                     headers={"Authorization": f"Bearer {_PRIVATE}"}) as client:
            return await _verify.verify_credentials(client, credentials, _spki(self.session_key))

    async def assert_rejected(self, credentials, **options):
        with self.assertRaisesRegex(ValueError, "^Bedrock credential verification failed$") as rejected:
            await self.verify(credentials, **options)
        self.assertTrue(rejected.exception.__suppress_context__)

    async def test_actual_discovery_url_accepts_rsa_and_p384_keys_without_forwarding_auth(self):
        for algorithm in ("RS256", "ES384"):
            with self.subTest(algorithm=algorithm):
                self.assertIsNone(await self.verify(self.credentials(algorithm=algorithm), keys=[self.signing_jwk(algorithm)]))
                self.assertEqual(self.requests, [_DISCOVERY, _JWKS])

    async def test_rejects_untrusted_root_and_wrong_leaf_before_network(self):
        for credentials in (self.credentials(root=self.delegate), self.credentials(leaf_key=self.other_session_key)):
            await self.assert_rejected(credentials)
            self.assertEqual(self.requests, [])

    async def test_rejects_wrong_signature_issuer_audience_and_binding(self):
        options = (
            {"token_signer": self.other_rsa_authority},
            {"claims": {"iss": _ISSUER.rstrip("/")}},
            {"claims": {"aud": "wrong-audience"}},
            {"claims": {"cpk": _spki(self.other_session_key)}},
            {"claims": {"xid": "9999999999"}},
        )
        for params in options:
            await self.assert_rejected(self.credentials(**params))
            self.assertEqual(self.requests, [_DISCOVERY, _JWKS])

    async def test_time_allows_small_future_skew_and_never_expiry_grace(self):
        now = int(time.time())
        future = {"iat": now + 60, "nbf": now + 60}
        with patch.object(_verify, "time", SimpleNamespace(time=lambda: now)):
            await self.verify(self.credentials(claims=future, dates=future))
            for claims in ({"iat": now + 61}, {"nbf": now + 61}, {"exp": now}, {"exp": now - 1}):
                await self.assert_rejected(self.credentials(claims=claims))
                await self.assert_rejected(self.credentials(dates=claims))

    async def test_rejects_unexpected_algorithm_and_invalid_or_ambiguous_jwks_key(self):
        await self.assert_rejected(self.credentials(algorithm="HS256", token_signer="fixture" * 5))
        self.assertEqual(self.requests, [])
        rsa_jwk = self.signing_jwk()
        for keys in ([], [rsa_jwk, rsa_jwk], [self.signing_jwk("ES384")],
                     [{**rsa_jwk, "key_ops": ["encrypt"]}], [{**rsa_jwk, "use": "enc"}]):
            await self.assert_rejected(self.credentials(), keys=keys)

    async def test_rejects_unsafe_metadata_refusals_and_redirects(self):
        for metadata in (
            {"issuer": _ISSUER + "wrong", "jwks_uri": _JWKS},
            *({"issuer": _ISSUER, "jwks_uri": url} for url in (
                "/relative", _JWKS.replace("https:", "http:"), _JWKS + "#", _JWKS.replace("https://", "https://user@"),
            )),
        ):
            await self.assert_rejected(self.credentials(), metadata=metadata)
            self.assertEqual(self.requests, [_DISCOVERY])
        for status in (302, 403):
            await self.assert_rejected(self.credentials(), discovery_status=status)
            self.assertEqual(self.requests, [_DISCOVERY])
            await self.assert_rejected(self.credentials(), jwks_status=status)
            self.assertEqual(self.requests, [_DISCOVERY, _JWKS])

    async def test_cancellation_is_preserved_during_metadata_fetch(self):
        requested = asyncio.Event()

        async def wait_for_cancel(_request):
            requested.set()
            await asyncio.Future()

        async with httpx.AsyncClient(transport=httpx.MockTransport(wait_for_cancel)) as client:
            task = asyncio.create_task(_verify.verify_credentials(client, self.credentials(), _spki(self.session_key)))
            try:
                await asyncio.wait_for(requested.wait(), 1)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
