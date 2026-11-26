"""Bedrock login identities and connection request encoding."""

import base64
import hashlib
import json
import re
import secrets
import struct
import time
import uuid
from collections.abc import Awaitable, Callable
from copy import copy
from dataclasses import dataclass, field
from typing import Self
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

from .skin import Skin, _BLACK_RGBA, _SKIN_WIDTH, _SOLID_SKIN_HEIGHT, _resource_patch


DEFAULT_LOCALE = "en_US"
_ISSUANCE_CLOCK_LEEWAY_SECONDS = 60


@dataclass(frozen=True, slots=True)
class BedrockCredentials:
    """Authorized Bedrock credentials issued for the supplied session public key.

    Pass the two issued certificates in signing order, before the client's
    delegation certificate is added. The provider verifies the issuing service,
    certificate authority, and multiplayer token (including its audience).
    """

    certificate_chain: tuple[str, ...] = field(repr=False)
    multiplayer_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.certificate_chain, tuple) or not all(
            isinstance(certificate, str) for certificate in self.certificate_chain
        ):
            raise TypeError("certificate_chain must be a tuple of strings")
        if not isinstance(self.multiplayer_token, str):
            raise TypeError("multiplayer_token must be a string")


BedrockCredentialProvider = Callable[[str], Awaitable[BedrockCredentials]]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _url64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _public_key_b64(key: ec.EllipticCurvePublicKey) -> str:
    return _b64(key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ))


def _sign(claims: dict, key: ec.EllipticCurvePrivateKey) -> str:
    header = {"alg": "ES384", "typ": "JWT", "x5u": _public_key_b64(key.public_key())}
    body = _url64(json.dumps(header, separators=(",", ":")).encode()) + "." + _url64(
        json.dumps(claims, separators=(",", ":")).encode()
    )
    r, s = decode_dss_signature(key.sign(body.encode(), ec.ECDSA(hashes.SHA384())))
    return body + "." + _url64(r.to_bytes(48, "big") + s.to_bytes(48, "big"))


class _ClientIdentity:
    def __init__(self, name: str, player_uuid: uuid.UUID, key: ec.EllipticCurvePrivateKey):
        self.name = name
        self.uuid = player_uuid
        self.key = key
        self.self_signed_id = uuid.uuid4()
        self.random_id = secrets.randbits(63)
        self._skin: Skin | None = None

    def _with_skin(self, skin: Skin) -> Self:
        # The configured offline identity can be shared. Bind the skin to this join.
        identity = copy(self)
        identity._skin = skin
        return identity

    def _encode_login(
        self, auth: dict, address: str, *, protocol: int, chunk_radius: int, locale: str,
    ) -> bytes:
        skin = self._skin
        if skin is None:
            model = "wide"
            width, height = _SKIN_WIDTH, _SOLID_SKIN_HEIGHT
            pixels = _BLACK_RGBA * (width * height)
            skin_id = f"endpilot.{self.uuid}"
        else:
            model = skin.model
            width, height = skin.width, skin.height
            pixels = skin.rgba
            skin_id = skin._id_for(self.uuid)
        client_data = {
            "AnimatedImageData": [], "ArmSize": model, "CapeData": "",
            "CapeId": "", "CapeImageHeight": 0, "CapeImageWidth": 0,
            "CapeOnClassicSkin": False, "ClientRandomId": self.random_id,
            "CompatibleWithClientSideChunkGen": False,
            "CurrentInputMode": 1, "DefaultInputMode": 1,
            "DeviceId": self.self_signed_id.hex, "DeviceModel": "EndPilot",
            "DeviceOS": 8, "GameVersion": "1.26.66" if protocol == 2225 else "1.26.52",
            "GraphicsMode": 0, "GuiScale": 0, "IsEditorMode": False,
            "LanguageCode": locale, "MaxViewDistance": chunk_radius,
            "MemoryTier": 0, "OverrideSkin": False,
            "PersonaPieces": [], "PersonaSkin": False, "PieceTintColors": [],
            "PlatformOfflineId": "", "PlatformOnlineId": "", "PlatformType": 0,
            "PlayFabId": self.uuid.hex[:16], "PremiumSkin": False,
            "SelfSignedId": str(self.self_signed_id), "ServerAddress": address,
            "SkinAnimationData": "", "SkinColor": "#0", "SkinData": _b64(pixels),
            "SkinGeometryData": "", "SkinGeometryDataEngineVersion": _b64(b"0.0.0"),
            "SkinId": skin_id,
            "SkinImageHeight": height, "SkinImageWidth": width,
            "SkinResourcePatch": _b64(_resource_patch(model).encode("ascii")),
            "ThirdPartyName": self.name, "TrustedSkin": False, "UIProfile": 0,
        }
        parts = (json.dumps(auth, separators=(",", ":")).encode(), _sign(client_data, self.key).encode())
        return b"".join(struct.pack("<i", len(part)) + part for part in parts)


def _validate_locale(locale: str) -> None:
    if not isinstance(locale, str) or not re.fullmatch("[a-z]{2,3}_[A-Z]{2}", locale):
        raise ValueError("locale must be a Bedrock language code such as en_US")


class OfflineIdentity(_ClientIdentity):
    """One self-signed name and key shared by the transport and Bedrock login."""

    def __init__(self, name: str):
        super().__init__(name, uuid.uuid4(), ec.generate_private_key(ec.SECP384R1()))

    def login_request(
        self, address: str, *, protocol: int = 2193, chunk_radius: int = 8, locale: str = DEFAULT_LOCALE,
    ) -> bytes:
        _validate_locale(locale)
        now = int(time.time())
        token = _sign({
            "cpk": _public_key_b64(self.key.public_key()), "xname": self.name, "leguuid": str(self.uuid),
            "iat": now, "nbf": now - 60, "exp": now + 21600,
        }, self.key)
        return self._encode_login(
            {"AuthenticationType": 2, "Token": token},
            address, protocol=protocol, chunk_radius=chunk_radius, locale=locale,
        )


class _OnlineIdentity(_ClientIdentity):
    def __init__(
        self, name: str, player_uuid: uuid.UUID, key: ec.EllipticCurvePrivateKey,
        credentials: BedrockCredentials, issuer: str, first_signer: str, expires_at: int | float,
    ):
        super().__init__(name, player_uuid, key)
        self.multiplayer_token = credentials.multiplayer_token
        self.issuer = issuer
        self._certificate_chain = credentials.certificate_chain
        self._first_signer = first_signer
        self._expires_at = expires_at

    def login_request(
        self, address: str, *, protocol: int = 2225, chunk_radius: int = 8, locale: str = DEFAULT_LOCALE,
    ) -> bytes:
        if protocol != 2225:
            raise ValueError("online login requires protocol 2225")
        _validate_locale(locale)
        now = time.time()
        if now >= self._expires_at:
            raise ValueError("online credentials have expired")
        # Prepend a delegation from the session key to the first issued certificate's signer.
        delegation = _sign({
            "identityPublicKey": self._first_signer, "certificateAuthority": True,
            "nbf": int(now) - 21600, "exp": int(now) + 21600,
        }, self.key)
        auth = {
            "Certificate": json.dumps({"chain": (delegation, *self._certificate_chain)}, separators=(",", ":")),
            "AuthenticationType": 0,
            "Token": self.multiplayer_token,
        }
        return self._encode_login(
            auth, address, protocol=protocol, chunk_radius=chunk_radius, locale=locale,
        )


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate claim")
    return obj


def _reject_json_constant(value: str) -> None:
    raise ValueError("invalid JSON number")


def _decode_url64(segment: str) -> bytes:
    decoded = base64.b64decode(segment + "=" * (-len(segment) % 4), altchars=b"-_", validate=True)
    if not segment or _url64(decoded) != segment:
        raise ValueError("invalid compact JWT encoding")
    return decoded


def _read_jwt(token: str, algorithms: tuple[str, ...], label: str) -> tuple[dict, dict, bytes, bytes]:
    try:
        head, body, signature_segment = token.split(".")
        header = json.loads(
            _decode_url64(head).decode("utf-8"), object_pairs_hook=_json_object, parse_constant=_reject_json_constant,
        )
        claims = json.loads(
            _decode_url64(body).decode("utf-8"), object_pairs_hook=_json_object, parse_constant=_reject_json_constant,
        )
        signature = _decode_url64(signature_segment)
    except (ValueError, RecursionError):
        raise ValueError(f"{label} must be a compact signed JWT") from None
    if not isinstance(header, dict) or not isinstance(claims, dict) or header.get("alg") not in algorithms:
        raise ValueError(f"{label} has an invalid JWT header or claims")
    if "crit" in header or header.get("b64", True) is not True:
        raise ValueError(f"{label} uses unsupported JWT header parameters")
    if header["alg"] == "ES384":
        valid_signature = len(signature) == 96 and any(signature[:48]) and any(signature[48:])
    else:
        valid_signature = len(signature) >= 256 and any(signature)
    if not valid_signature:
        raise ValueError(f"{label} has an invalid signature encoding")
    return header, claims, signature, f"{head}.{body}".encode("ascii")


def _read_public_key(value: object, label: str) -> ec.EllipticCurvePublicKey:
    if isinstance(value, str):
        try:
            key = serialization.load_der_public_key(base64.b64decode(value, validate=True))
        except (ValueError, UnsupportedAlgorithm):
            pass
        else:
            if isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP384R1):
                if _public_key_b64(key) == value:
                    return key
    raise ValueError(f"{label} must be a base64 DER P-384 public key")


def _expiry(claims: dict, now: float, label: str) -> int | float:
    def numeric_date(value: object) -> bool:
        return type(value) in (int, float) and 0 <= value < 2**63

    expiry = claims.get("exp")
    if not numeric_date(expiry):
        raise ValueError(f"{label} has an invalid expiry")
    if expiry <= now:
        raise ValueError(f"{label} has expired")
    latest_issuance = now + _ISSUANCE_CLOCK_LEEWAY_SECONDS
    for field_name in ("nbf", "iat"):
        if field_name in claims:
            date = claims[field_name]
            if not numeric_date(date) or date > latest_issuance:
                raise ValueError(f"{label} has an invalid {field_name} time")
    return expiry


def _read_certificate_chain(
    chain: tuple[str, ...], public: ec.EllipticCurvePublicKey, now: float,
) -> tuple[str, dict, int | float]:
    if len(chain) != 2:
        raise ValueError("online login requires two issued certificates")
    first_header, delegation, signature, signed = _read_jwt(chain[0], ("ES384",), "first certificate")
    first_signer = first_header.get("x5u")
    signer = _read_public_key(first_signer, "first certificate signer")
    _verify_certificate(signer, signature, signed, "first certificate")
    first_expiry = _expiry(delegation, now, "first certificate")
    if "certificateAuthority" in delegation and type(delegation["certificateAuthority"]) is not bool:
        raise ValueError("first certificate has an invalid certificateAuthority")
    delegate = _read_public_key(delegation.get("identityPublicKey"), "certificate delegation key")

    leaf_header, leaf, signature, signed = _read_jwt(chain[1], ("ES384",), "leaf certificate")
    if "x5u" in leaf_header:
        leaf_signer = _read_public_key(leaf_header["x5u"], "leaf certificate signer")
        if leaf_signer.public_numbers() != delegate.public_numbers():
            raise ValueError("leaf certificate signer does not match certificate delegation")
    _verify_certificate(delegate, signature, signed, "leaf certificate")
    leaf_expiry = _expiry(leaf, now, "leaf certificate")
    if delegation.get("iss") != "Mojang" or leaf.get("iss") != "Mojang":
        raise ValueError("issued certificates must declare the Mojang issuer")
    leaf_public = _read_public_key(leaf.get("identityPublicKey"), "leaf certificate public key")
    if leaf_public.public_numbers() != public.public_numbers():
        raise ValueError("leaf certificate public key does not match the session public key")
    profile = leaf.get("extraData")
    if not isinstance(profile, dict):
        raise ValueError("leaf certificate is missing its identity data")
    return first_signer, profile, min(first_expiry, leaf_expiry)


def _verify_certificate(
    key: ec.EllipticCurvePublicKey, signature: bytes, signed: bytes, label: str,
) -> None:
    der_signature = encode_dss_signature(int.from_bytes(signature[:48], "big"), int.from_bytes(signature[48:], "big"))
    try:
        key.verify(der_signature, signed, ec.ECDSA(hashes.SHA384()))
    except InvalidSignature:
        raise ValueError(f"{label} signature does not match its signing key") from None


def _identity_uuid(value: object, label: str) -> uuid.UUID:
    if isinstance(value, str):
        try:
            player_uuid = uuid.UUID(value)
        except ValueError:
            pass
        else:
            if player_uuid.int != 0:
                return player_uuid
    raise ValueError(f"{label} has an invalid UUID")


async def acquire_online_identity(provider: BedrockCredentialProvider) -> _OnlineIdentity:
    """Bind provider-verified credentials to a fresh local P-384 session key.

    Only the encoded public key goes to the provider. The certificate signatures
    are checked for continuity through the supplied signing keys. Issuer trust and
    multiplayer token signature verification belong to the authorized provider.
    """
    key = ec.generate_private_key(ec.SECP384R1())
    public = key.public_key()
    credentials = await provider(_public_key_b64(public))
    if not isinstance(credentials, BedrockCredentials):
        raise TypeError("credential provider must return BedrockCredentials")

    now = time.time()
    _, claims, _, _ = _read_jwt(credentials.multiplayer_token, ("ES384", "RS256"), "multiplayer token")
    token_expiry = _expiry(claims, now, "multiplayer token")
    token_public = _read_public_key(claims.get("cpk"), "multiplayer token cpk")
    if token_public.public_numbers() != public.public_numbers():
        raise ValueError("multiplayer token cpk does not match the session public key")
    first_signer, profile, chain_expiry = _read_certificate_chain(credentials.certificate_chain, public, now)

    issuer = claims.get("iss")
    try:
        issuer_url = urlsplit(issuer) if isinstance(issuer, str) else None
    except ValueError:
        raise ValueError("multiplayer token must declare an absolute issuer URL") from None
    if issuer_url is None or not issuer_url.scheme or not issuer_url.netloc or issuer != issuer.strip():
        raise ValueError("multiplayer token must declare an absolute issuer URL")
    name, xuid = claims.get("xname"), claims.get("xid")
    if not isinstance(name, str) or not name or name != name.strip() or not name.isprintable():
        raise ValueError("multiplayer token has an invalid player name")
    if not isinstance(xuid, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", xuid) or int(xuid) >= 2**63:
        raise ValueError("multiplayer token has an invalid XUID")
    if name != profile.get("displayName") or xuid != profile.get("XUID"):
        raise ValueError("multiplayer token name or XUID does not match the leaf certificate")
    legacy_uuid = _identity_uuid(profile.get("identity"), "leaf certificate")
    explicit_uuid = claims.get("leguuid", "")
    if explicit_uuid == "":
        # The modern multiplayer identity derives its UUID from the XUID.
        digest = hashlib.md5(b"pocket-auth-1-xuid:" + xuid.encode("ascii"), usedforsecurity=False).digest()
        player_uuid = uuid.UUID(bytes=digest, version=3)
    else:
        player_uuid = _identity_uuid(explicit_uuid, "multiplayer token")
        if player_uuid != legacy_uuid:
            raise ValueError("multiplayer token UUID does not match the leaf certificate")
    return _OnlineIdentity(name, player_uuid, key, credentials, issuer, first_signer, min(token_expiry, chain_expiry))
