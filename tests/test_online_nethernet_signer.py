import asyncio
import base64
import hashlib
import json
import unittest
from importlib.util import find_spec
from unittest.mock import AsyncMock, patch

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature


def encode_base64url(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def decode_base64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def public_der(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


@unittest.skipUnless(find_spec("nethernet"), "install the nethernet extra")
class OnlineNetherNetSignerTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_offer_carries_issued_token_over_http(self):
        from aiortc import RTCIceGatherer
        from nethernet import IdentitySigner, serve_http

        from endpilot.transport.nethernet import connect

        self.enterContext(patch.object(RTCIceGatherer, "getDefaultIceServers", return_value=[]))
        key = ec.generate_private_key(ec.SECP384R1())
        issuer_key = ec.generate_private_key(ec.SECP384R1())
        server_key = ec.generate_private_key(ec.SECP384R1())
        server_pin = hashlib.sha256(public_der(server_key)).hexdigest()
        received_offers = []

        async def peer(connection):
            async for _ in connection:
                pass

        for issuer, domain in (
            ("https://issuer.example", "https://issuer.example/"),
            ("https://issuer.example/identity/v1", "https://issuer.example/identity/v1"),
        ):
            with self.subTest(issuer=issuer):
                claims = {"iss": domain, "cpk": base64.b64encode(public_der(key)).decode()}
                signed = encode_base64url(b'{"alg":"ES384"}') + b"." + encode_base64url(json.dumps(claims).encode())
                r, s = decode_dss_signature(issuer_key.sign(signed, ec.ECDSA(hashes.SHA384())))
                issued_token = (signed + b"." + encode_base64url(r.to_bytes(48, "big") + s.to_bytes(48, "big"))).decode()

                async with asyncio.timeout(20), serve_http(
                    peer, host="127.0.0.1", identity_signer=IdentitySigner(server_key), validate_offer=received_offers.append,
                ) as server:
                    connection = await connect(
                        f"http://127.0.0.1:{server.bound_port}", private_key=key,
                        multiplayer_token=issued_token, issuer=issuer, server_key_sha256=server_pin,
                    )
                    await connection.close()

                offer = received_offers.pop()
                self.assertFalse(received_offers)
                self.assertIsNotNone(offer.identity)
                self.assertEqual(offer.identity.domain, domain)
                self.assertEqual(offer.identity.protocol, "default")
                self.assertEqual(offer.identity.token, issued_token)

                fingerprints = []
                for line in offer.sdp.splitlines():
                    if line.startswith("a=fingerprint:"):
                        algorithm, digest = line.removeprefix("a=fingerprint:").split(" ", 1)
                        fingerprints.append({"algorithm": algorithm, "digest": digest})
                self.assertTrue(fingerprints)
                header, empty_payload, sdp_signature = offer.identity.fingerprints.split(".")
                self.assertEqual(empty_payload, "")
                self.assertEqual(json.loads(decode_base64url(header)), {"alg": "ES384"})
                payload = json.dumps({"fingerprint": fingerprints}, sort_keys=True, separators=(",", ":")).encode()
                signed_sdp = header.encode() + b"." + encode_base64url(payload)
                raw_signature = decode_base64url(sdp_signature)
                self.assertEqual(len(raw_signature), 96)
                der_signature = encode_dss_signature(
                    int.from_bytes(raw_signature[:48], "big"), int.from_bytes(raw_signature[48:], "big"),
                )
                credential_claims = json.loads(decode_base64url(offer.identity.token.split(".")[1]))
                bound_key = serialization.load_der_public_key(base64.b64decode(credential_claims["cpk"]))
                bound_key.verify(der_signature, signed_sdp, ec.ECDSA(hashes.SHA384()))
                with self.assertRaises(InvalidSignature):
                    issuer_key.public_key().verify(der_signature, signed_sdp, ec.ECDSA(hashes.SHA384()))

    async def test_incomplete_online_identity_fails_before_transport_opens(self):
        from endpilot.transport.nethernet import connect

        key = ec.generate_private_key(ec.SECP384R1())
        token = "test-multiplayer-token"
        issuer = "https://issuer.example/"
        with patch("endpilot.transport.nethernet.connect_http", new_callable=AsyncMock) as dial:
            for invalid in (
                {"private_key": key, "issuer": issuer},
                {"private_key": key, "multiplayer_token": token},
                {"multiplayer_token": token, "issuer": issuer},
                {"private_key": key, "multiplayer_token": "", "issuer": issuer},
                {"private_key": key, "multiplayer_token": token, "issuer": ""},
            ):
                with self.assertRaisesRegex(ValueError, "requires private_key, multiplayer_token, and issuer"):
                    await connect("http://127.0.0.1", **invalid)
            with self.assertRaisesRegex(ValueError, "issuer must be an absolute URL"):
                await connect("http://127.0.0.1", private_key=key, multiplayer_token=token, issuer="relative/path")
        dial.assert_not_called()
