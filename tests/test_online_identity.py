import base64
import json
import struct
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch
from uuid import UUID

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

from endpilot import BedrockCredentials
from endpilot.authentication import acquire_online_identity
from endpilot.protocol import Protocol


NOW = 1_800_000_000
NAME = "Example Player"
XUID = "2535417953249031"
LEGACY_UUID = UUID("d9172ef4-9d79-454b-a0cd-920cb9adc7e5")
MODERN_UUID = UUID("ed2685f5-6fca-324b-818c-51cb290f1641")
ISSUER = "https://example.invalid/authorization/"


def _public_key(key):
    return base64.b64encode(key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )).decode("ascii")


def _segment(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode(segment):
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _issue(key, header, claims):
    data = ".".join(_segment(json.dumps(part).encode("utf-8")) for part in (header, claims))
    if isinstance(key, ec.EllipticCurvePrivateKey):
        left, right = decode_dss_signature(key.sign(data.encode("ascii"), ec.ECDSA(hashes.SHA384())))
        signature = left.to_bytes(48, "big") + right.to_bytes(48, "big")
    else:
        signature = key.sign(data.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return data + "." + _segment(signature)


def _verify_client_jwt(token, key):
    header, body, signature = token.split(".")
    raw = _decode(signature)
    assert len(raw) == 96
    der = encode_dss_signature(int.from_bytes(raw[:48], "big"), int.from_bytes(raw[48:], "big"))
    key.verify(der, f"{header}.{body}".encode("ascii"), ec.ECDSA(hashes.SHA384()))
    return json.loads(_decode(header)), json.loads(_decode(body))


class OnlineIdentityTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.rsa_signer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.chain_root = ec.generate_private_key(ec.SECP384R1())
        cls.chain_delegate = ec.generate_private_key(ec.SECP384R1())

    def setUp(self):
        self.clock = self.enterContext(patch("endpilot.authentication.time.time", return_value=NOW))
        self.calls = []

    def _credentials(self, spki, *, token=None, first=None, leaf=None, profile=None, ec_token=False):
        common = {"nbf": NOW - 60, "iat": NOW - 30, "exp": NOW + 300}
        root_header = {"alg": "ES384", "x5u": _public_key(self.chain_root.public_key())}
        delegate_spki = _public_key(self.chain_delegate.public_key())
        first_certificate = _issue(self.chain_root, root_header, {
            **common, "iss": "Mojang", "certificateAuthority": True,
            "identityPublicKey": delegate_spki, **(first or {}),
        })
        # The previous certificate supplies the leaf's signing key.
        leaf_certificate = _issue(self.chain_delegate, {"alg": "ES384"}, {
            **common, "iss": "Mojang", "identityPublicKey": spki,
            "extraData": {"displayName": NAME, "XUID": XUID, "identity": str(LEGACY_UUID), **(profile or {})},
            **(leaf or {}),
        })
        token_key = self.chain_root if ec_token else self.rsa_signer
        multiplayer_token = _issue(token_key, {"alg": "ES384" if ec_token else "RS256", "kid": "fixture"}, {
            **common, "iss": ISSUER, "aud": "api://auth-minecraft-services/multiplayer",
            "cpk": spki, "xname": NAME, "xid": XUID, **(token or {}),
        })
        return BedrockCredentials((first_certificate, leaf_certificate), multiplayer_token)

    def _provider(self, transform=None, **values):
        async def provide(spki):
            public = serialization.load_der_public_key(base64.b64decode(spki, validate=True))
            self.assertIsInstance(public, ec.EllipticCurvePublicKey)
            self.assertIsInstance(public.curve, ec.SECP384R1)
            self.assertEqual(_public_key(public), spki)
            credentials = self._credentials(spki, **values)
            if transform is not None:
                credentials = transform(credentials)
            self.calls.append((spki, public, credentials))
            return credentials
        return provide

    async def test_online_login_preserves_issued_tokens_and_signs_both_client_claims(self):
        provider = self._provider()
        identity = await acquire_online_identity(provider)
        spki, public, credentials = self.calls[0]
        self.assertEqual((identity.name, identity.uuid, identity.issuer), (NAME, MODERN_UUID, ISSUER))
        self.assertEqual(identity.key.public_key().public_numbers(), public.public_numbers())
        self.assertTrue(identity.multiplayer_token == credentials.multiplayer_token)

        address = "example.invalid:19132"
        request = identity.login_request(address, chunk_radius=7, locale="zh_TW")
        auth_length, = struct.unpack_from("<i", request)
        auth_end = 4 + auth_length
        auth = json.loads(request[4:auth_end])
        client_length, = struct.unpack_from("<i", request, auth_end)
        self.assertEqual(len(request), auth_end + 4 + client_length)
        self.assertEqual(set(auth), {"Certificate", "AuthenticationType", "Token"})
        self.assertEqual(auth["AuthenticationType"], 0)
        self.assertTrue(auth["Token"] == credentials.multiplayer_token)
        self.assertIsInstance(auth["Certificate"], str)
        bridge, *chain = json.loads(auth["Certificate"])["chain"]
        self.assertTrue(tuple(chain) == credentials.certificate_chain)
        header, claims = _verify_client_jwt(bridge, public)
        self.assertEqual((header["alg"], header["x5u"]), ("ES384", spki))
        self.assertEqual(claims, {
            "identityPublicKey": _public_key(self.chain_root.public_key()), "certificateAuthority": True,
            "nbf": NOW - 21600, "exp": NOW + 21600,
        })
        header, claims = _verify_client_jwt(request[auth_end + 4:].decode("ascii"), public)
        self.assertEqual((header["alg"], header["x5u"]), ("ES384", spki))
        platform = Protocol(2225).binding.BuildPlatform
        self.assertEqual(platform(claims["DeviceOS"]), platform.WIN32)
        self.assertEqual(
            (claims["ThirdPartyName"], claims["ServerAddress"], claims["GameVersion"],
             claims["LanguageCode"], claims["MaxViewDistance"]),
            (NAME, address, "1.26.66", "zh_TW", 7),
        )
        with self.assertRaisesRegex(ValueError, "requires protocol 2225"):
            identity.login_request(address, protocol=2193)
        second = await acquire_online_identity(provider)
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(self.calls[1][0], spki)
        self.assertEqual(second.uuid, identity.uuid)

    async def test_explicit_uuid_matches_signed_leaf_and_es384_multiplayer_is_supported(self):
        identity = await acquire_online_identity(self._provider(token={"leguuid": str(LEGACY_UUID)}, ec_token=True))
        self.assertEqual(identity.uuid, LEGACY_UUID)

    async def test_rejects_each_credential_bound_to_a_different_key(self):
        other = _public_key(self.chain_root.public_key())
        with self.assertRaisesRegex(ValueError, "cpk does not match"):
            await acquire_online_identity(self._provider(token={"cpk": other}))
        with self.assertRaisesRegex(ValueError, "leaf certificate public key does not match"):
            await acquire_online_identity(self._provider(leaf={"identityPublicKey": other}))

    async def test_verifies_both_chain_signatures_and_the_delegation(self):
        for index, label in ((0, "first"), (1, "leaf")):
            with self.subTest(certificate=label):
                def tamper(credentials):
                    chain = list(credentials.certificate_chain)
                    header, claims, signature = chain[index].split(".")
                    raw = bytearray(_decode(signature))
                    raw[-1] ^= 1
                    chain[index] = ".".join((header, claims, _segment(raw)))
                    return BedrockCredentials(tuple(chain), credentials.multiplayer_token)

                with self.assertRaisesRegex(ValueError, f"{label} certificate signature"):
                    await acquire_online_identity(self._provider(tamper))
        with self.assertRaisesRegex(ValueError, "leaf certificate signature"):
            await acquire_online_identity(self._provider(first={
                "identityPublicKey": _public_key(self.chain_root.public_key()),
            }))

    async def test_all_issuers_have_sixty_seconds_of_leeway_for_future_timestamps(self):
        for part in ("token", "first", "leaf"):
            for skew in (3, 60):
                with self.subTest(part=part, skew=skew):
                    identity = await acquire_online_identity(self._provider(**{
                        part: {"iat": NOW + skew, "nbf": NOW + skew},
                    }))
                    self.assertEqual(identity.uuid, MODERN_UUID)
            for field in ("iat", "nbf"):
                with self.subTest(part=part, field=field):
                    with self.assertRaisesRegex(ValueError, f"invalid {field} time"):
                        await acquire_online_identity(self._provider(**{part: {field: NOW + 61}}))

    async def test_rejects_expired_or_malformed_times_independently(self):
        for part, label in (("token", "multiplayer token"), ("first", "first certificate"), ("leaf", "leaf certificate")):
            with self.subTest(part=part):
                with self.assertRaisesRegex(ValueError, f"{label} has expired"):
                    await acquire_online_identity(self._provider(**{part: {"exp": NOW}}))
        with self.assertRaisesRegex(ValueError, "invalid nbf"):
            await acquire_online_identity(self._provider(token={"nbf": str(NOW)}))
        with self.assertRaisesRegex(ValueError, "invalid expiry"):
            await acquire_online_identity(self._provider(leaf={"exp": True}))

        identity = await acquire_online_identity(self._provider(leaf={"exp": NOW + 1}))
        self.clock.return_value = NOW + 1
        with self.assertRaisesRegex(ValueError, "credentials have expired"):
            identity.login_request("example.invalid")

    async def test_rejects_inconsistent_or_invalid_identity_claims(self):
        for values, error in (
            ({"token": {"xname": "Other Player"}}, "name or XUID does not match"),
            ({"token": {"xid": "2535417953249032"}}, "name or XUID does not match"),
            ({"token": {"xid": int(XUID)}}, "invalid XUID"),
            ({"token": {"leguuid": str(MODERN_UUID)}}, "UUID does not match"),
            ({"token": {"leguuid": None}}, "invalid UUID"),
            ({"profile": {"identity": str(UUID(int=0))}}, "invalid UUID"),
            ({"token": {"iss": "relative-issuer"}}, "absolute issuer URL"),
        ):
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error):
                    await acquire_online_identity(self._provider(**values))

    async def test_rejects_invalid_multiplayer_jwt_format_and_signature_encoding(self):
        for signature, error in (("", "compact signed JWT"), (_segment(bytes(256)), "signature encoding")):
            with self.subTest(error=error):
                def replace_signature(credentials):
                    signing_input, _ = credentials.multiplayer_token.rsplit(".", 1)
                    return BedrockCredentials(credentials.certificate_chain, signing_input + "." + signature)

                with self.assertRaisesRegex(ValueError, error):
                    await acquire_online_identity(self._provider(replace_signature))

        def replace_algorithm(credentials):
            _, body, signature = credentials.multiplayer_token.split(".")
            unsigned = ".".join((_segment(b'{"alg":"none"}'), body, signature))
            return BedrockCredentials(credentials.certificate_chain, unsigned)

        with self.assertRaisesRegex(ValueError, "invalid JWT header"):
            await acquire_online_identity(self._provider(replace_algorithm))

    def test_credentials_are_immutable_and_repr_redacts_both_secrets(self):
        credentials = BedrockCredentials(("test-certificate",), "test-token")
        self.assertEqual(repr(credentials), "BedrockCredentials()")
        with self.assertRaises(FrozenInstanceError):
            credentials.multiplayer_token = "different-token"
        with self.assertRaisesRegex(TypeError, "tuple of strings"):
            BedrockCredentials(["test-certificate"], "test-token")
