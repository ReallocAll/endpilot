"""Self-signed Bedrock identity for offline-mode servers."""

import base64
import json
import secrets
import struct
import time
import uuid

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _url64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign(claims: dict, key: ec.EllipticCurvePrivateKey) -> str:
    public = _b64(key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ))
    header = {"alg": "ES384", "typ": "JWT", "x5u": public}
    body = _url64(json.dumps(header, separators=(",", ":")).encode()) + "." + _url64(
        json.dumps(claims, separators=(",", ":")).encode()
    )
    r, s = decode_dss_signature(key.sign(body.encode(), ec.ECDSA(hashes.SHA384())))
    return body + "." + _url64(r.to_bytes(48, "big") + s.to_bytes(48, "big"))


class OfflineIdentity:
    """One name and key shared by the NetherNet transport and Bedrock login."""

    def __init__(self, name: str):
        self.name = name
        self.uuid = uuid.uuid4()
        self.key = ec.generate_private_key(ec.SECP384R1())
        self.self_signed_id = uuid.uuid4()
        self.random_id = secrets.randbits(63)

    def login_request(self, address: str) -> bytes:
        now = int(time.time())
        public = _b64(self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        ))
        token = _sign({
            "cpk": public, "xname": self.name, "leguuid": str(self.uuid),
            "iat": now, "nbf": now - 60, "exp": now + 21600,
        }, self.key)
        auth = {"AuthenticationType": 2, "Token": token}
        skin = _b64(bytes((0, 0, 0, 255)) * (64 * 32))
        client_data = {
            "AnimatedImageData": [], "ArmSize": "wide", "CapeData": "",
            "CapeId": "", "CapeImageHeight": 0, "CapeImageWidth": 0,
            "CapeOnClassicSkin": False, "ClientRandomId": self.random_id,
            "CompatibleWithClientSideChunkGen": False,
            "CurrentInputMode": 1, "DefaultInputMode": 1,
            "DeviceId": self.self_signed_id.hex, "DeviceModel": "EndPilot",
            "DeviceOS": 8, "GameVersion": "1.26.52",
            "GraphicsMode": 0, "GuiScale": 0, "IsEditorMode": False,
            "LanguageCode": "en_US", "MaxViewDistance": 8,
            "MemoryTier": 0, "OverrideSkin": False,
            "PersonaPieces": [], "PersonaSkin": False, "PieceTintColors": [],
            "PlatformOfflineId": "", "PlatformOnlineId": "", "PlatformType": 0,
            "PlayFabId": self.uuid.hex[:16], "PremiumSkin": False,
            "SelfSignedId": str(self.self_signed_id), "ServerAddress": address,
            "SkinAnimationData": "", "SkinColor": "#0", "SkinData": skin,
            "SkinGeometryData": "", "SkinGeometryDataEngineVersion": _b64(b"0.0.0"),
            "SkinId": f"endpilot.{self.uuid}",
            "SkinImageHeight": 32, "SkinImageWidth": 64,
            "SkinResourcePatch": _b64(b'{"geometry":{"default":"geometry.humanoid.custom"}}'),
            "ThirdPartyName": self.name, "TrustedSkin": False, "UIProfile": 0,
        }
        parts = (json.dumps(auth, separators=(",", ":")).encode(), _sign(client_data, self.key).encode())
        return b"".join(struct.pack("<i", len(part)) + part for part in parts)
