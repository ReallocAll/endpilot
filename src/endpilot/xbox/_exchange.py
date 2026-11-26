"""Exchange an authorized Microsoft account token for Bedrock credentials."""

import base64
import os
import platform
import struct
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from ..authentication import BedrockCredentials, _read_public_key


_XBOX_USER = "https://user.auth.xboxlive.com/user/authenticate"
_XSTS = "https://xsts.auth.xboxlive.com/xsts/authorize"
_BEDROCK_PARTY = "https://multiplayer.minecraft.net/"
_PLAYFAB_PARTY = "https://b980a380.minecraft.playfabapi.com/"
_PLAYFAB_TITLE = "20CA2"
_PLAYFAB_LOGIN = "https://20ca2.playfabapi.com/Client/LoginWithXbox"
_MINECRAFT_AUTH = "https://authorization.franchise.minecraft-services.net"
_BEDROCK_CHAIN = "https://multiplayer.minecraft.net/authentication"
_GAME_VERSION = "1.26.66"
_FILETIME_UNIX_EPOCH = 116444736000000000
_MINECRAFT_UWP_FAMILY = "Microsoft.MinecraftUWP_8wekyb3d8bbwe"
_ERROR_INSUFFICIENT_BUFFER = 122
_PACKAGE_ORIGIN_STORE = 3


class ExchangeError(RuntimeError):
    """A credential exchange failure with a fixed stage and optional HTTP status."""

    def __init__(self, stage: str, reason: str, *, status_code: int | None = None):
        self.stage = stage
        self.status_code = status_code
        status = "" if status_code is None else f" (HTTP {status_code})"
        super().__init__(f"{stage}: {reason}{status}")


def _jwk(key: ec.EllipticCurvePrivateKey) -> dict[str, str]:
    public = key.public_key().public_numbers()
    return {
        "alg": "ES256", "kty": "EC", "use": "sig", "crv": "P-256",
        "x": base64.urlsafe_b64encode(public.x.to_bytes(32, "big")).rstrip(b"=").decode("ascii"),
        "y": base64.urlsafe_b64encode(public.y.to_bytes(32, "big")).rstrip(b"=").decode("ascii"),
    }


def _sign_xbox_request(request: httpx.Request, key: ec.EllipticCurvePrivateKey) -> None:
    # Xbox v1 signs the exact encoded body and includes the Windows FILETIME.
    header = struct.pack(">IQ", 1, time.time_ns() // 100 + _FILETIME_UNIX_EPOCH)
    data = b"\0".join((header[:4], header[4:], b"POST", request.url.raw_path, b"", request.content, b""))
    r, s = decode_dss_signature(key.sign(data, ec.ECDSA(hashes.SHA256())))
    request.headers["x-xbl-contract-version"] = "1"
    request.headers["Signature"] = base64.b64encode(
        header + r.to_bytes(32, "big") + s.to_bytes(32, "big"),
    ).decode("ascii")


async def _post(
    client: httpx.AsyncClient, stage: str, url: str, body: dict, *,
    headers: dict[str, str] | None = None, proof: ec.EllipticCurvePrivateKey | None = None,
) -> dict:
    # Build the request without client-wide credentials, headers or cookies.
    request_headers = {"User-Agent": "EndPilot", "Accept": "application/json"}
    if headers is not None:
        request_headers.update(headers)
    request = httpx.Request("POST", url, json=body, headers=request_headers)
    if proof is not None:
        _sign_xbox_request(request, proof)
    try:
        response = await client.send(request, auth=None, follow_redirects=False)
    except httpx.HTTPError:
        raise ExchangeError(stage, "request failed") from None
    if response.status_code != 200:
        raise ExchangeError(stage, "service rejected the request", status_code=response.status_code)
    try:
        payload = response.json()
    except (ValueError, RecursionError):
        raise ExchangeError(stage, "invalid JSON response") from None
    if not isinstance(payload, dict):
        raise ExchangeError(stage, "invalid JSON response")
    return payload


def _field(payload: object, name: str) -> object:
    return payload.get(name) if isinstance(payload, dict) else None


def _text(value: object, stage: str, *, allow_spaces: bool = False) -> str:
    if (not isinstance(value, str) or not value or not value.isascii() or not value.isprintable()
            or value != value.strip() or (" " in value and not allow_spaces)):
        raise ExchangeError(stage, "invalid credential field")
    return value


def _require_unexpired(value: object, stage: str) -> None:
    if not isinstance(value, str):
        raise ExchangeError(stage, "invalid credential expiry")
    try:
        expiry = datetime.fromisoformat(value)
    except ValueError:
        raise ExchangeError(stage, "invalid credential expiry") from None
    if expiry.tzinfo is None:
        raise ExchangeError(stage, "invalid credential expiry")
    if expiry <= datetime.now(UTC):
        raise ExchangeError(stage, "credential expired")


async def _xsts_authorization(
    client: httpx.AsyncClient, stage: str, relying_party: str, user_token: str,
    proof: ec.EllipticCurvePrivateKey, proof_jwk: dict[str, str],
) -> str:
    payload = await _post(client, stage, _XSTS, {
        "RelyingParty": relying_party,
        "TokenType": "JWT",
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [user_token], "ProofKey": proof_jwk},
    }, proof=proof)
    token = _text(payload.get("Token"), stage)
    _require_unexpired(payload.get("NotAfter"), stage)
    users = _field(payload.get("DisplayClaims"), "xui")
    if not isinstance(users, list) or len(users) != 1:
        raise ExchangeError(stage, "invalid Xbox user claims")
    user_hash = _text(_field(users[0], "uhs"), stage)
    if ";" in user_hash:
        raise ExchangeError(stage, "invalid Xbox user hash")
    return f"XBL3.0 x={user_hash};{token}"


def _physical_memory_bytes(system: str) -> int | None:
    try:
        if system == "Windows":
            import win32api

            try:
                size = win32api.GlobalMemoryStatusEx().get("TotalPhys", 0)
            except win32api.error:
                return None
        elif system == "Linux":
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            if pages <= 0 or page_size <= 0:
                return None
            size = pages * page_size
        elif system == "Darwin":
            import ctypes

            memory = ctypes.c_uint64()
            length = ctypes.c_size_t(ctypes.sizeof(memory))
            sysctl = ctypes.CDLL(None).sysctlbyname
            sysctl.argtypes = (ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                               ctypes.c_void_p, ctypes.c_size_t)
            if sysctl(b"hw.memsize", ctypes.byref(memory), ctypes.byref(length), None, 0) != 0:
                return None
            if length.value != ctypes.sizeof(memory):
                return None
            size = memory.value
        else:
            return None
    except (AttributeError, ImportError, OSError, ValueError):
        return None
    return size if size > 0 else None


def _has_windows_minecraft_store_package() -> bool:
    import ctypes

    try:
        get_packages = ctypes.windll.kernel32.GetPackagesByPackageFamily
        get_origin = ctypes.windll.kernelbase.GetStagedPackageOrigin
    except (AttributeError, OSError):
        return False

    uint32_pointer = ctypes.POINTER(ctypes.c_uint32)
    get_packages.argtypes = (
        ctypes.c_wchar_p, uint32_pointer, ctypes.POINTER(ctypes.c_wchar_p), uint32_pointer,
        ctypes.POINTER(ctypes.c_wchar),
    )
    get_packages.restype = ctypes.c_long
    get_origin.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int))
    get_origin.restype = ctypes.c_long

    count = ctypes.c_uint32()
    length = ctypes.c_uint32()
    status = get_packages(_MINECRAFT_UWP_FAMILY, ctypes.byref(count), None, ctypes.byref(length), None)
    if status != _ERROR_INSUFFICIENT_BUFFER or not count.value or not length.value:
        return False
    names = (ctypes.c_wchar_p * count.value)()
    buffer = ctypes.create_unicode_buffer(length.value)
    if get_packages(_MINECRAFT_UWP_FAMILY, ctypes.byref(count), names, ctypes.byref(length), buffer) != 0:
        return False

    for name in names[:count.value]:
        origin = ctypes.c_int()
        if name and get_origin(name, ctypes.byref(origin)) == 0 and origin.value == _PACKAGE_ORIGIN_STORE:
            return True
    return False


async def exchange(client: httpx.AsyncClient, access_token: str, public_key: str) -> BedrockCredentials:
    """Use a personal-account user token to acquire both key-bound credentials.

    The Xbox proof key is local to this exchange. The Minecraft public key is
    the SPKI provided by the caller. PlayFab can create the retail title account
    on the first authorized sign-in.
    """
    access_token = _text(access_token, "input")
    try:
        _read_public_key(public_key, "session public key")
    except ValueError:
        raise ExchangeError("input", "expected a base64 DER P-384 session public key") from None

    proof = ec.generate_private_key(ec.SECP256R1())
    proof_jwk = _jwk(proof)
    user = await _post(client, "xbox-user", _XBOX_USER, {
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
        "Properties": {
            "AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": f"d={access_token}", "ProofKey": proof_jwk,
        },
    }, proof=proof)
    user_token = _text(user.get("Token"), "xbox-user")
    _require_unexpired(user.get("NotAfter"), "xbox-user")
    bedrock = await _xsts_authorization(client, "xsts-bedrock", _BEDROCK_PARTY, user_token, proof, proof_jwk)
    playfab = await _xsts_authorization(client, "xsts-playfab", _PLAYFAB_PARTY, user_token, proof, proof_jwk)

    playfab_login = await _post(client, "playfab-login", _PLAYFAB_LOGIN, {
        "TitleId": _PLAYFAB_TITLE, "XboxToken": playfab, "CreateAccount": True,
    })
    ticket = _text(_field(playfab_login.get("data"), "SessionTicket"), "playfab-login")
    device = {
        "applicationType": "MinecraftPE", "capabilities": [], "gameVersion": _GAME_VERSION,
        "id": str(uuid4()), "playFabTitleId": _PLAYFAB_TITLE,
    }
    system = platform.system()
    memory = _physical_memory_bytes(system)
    if memory is not None:
        device["memory"] = str(memory)
    # The Bedrock service calls its Windows 10 and 11 category "Windows10".
    if system == "Windows" and platform.release() in ("10", "11"):
        device.update(platform="Windows10", type="Windows10")
        if _has_windows_minecraft_store_package():
            device["storePlatform"] = "uwp.store"
    session = await _post(client, "minecraft-session", f"{_MINECRAFT_AUTH}/api/v1.0/session/start", {
        "user": {
            "tokenType": "PlayFab", "token": ticket, "language": "en", "languageCode": "en-US", "regionCode": "US",
        },
        "device": device,
    })
    result = session.get("result")
    session_auth = _text(_field(result, "authorizationHeader"), "minecraft-session", allow_spaces=True)
    _require_unexpired(_field(result, "validUntil"), "minecraft-session")

    issued = await _post(client, "bedrock-chain", _BEDROCK_CHAIN, {"identityPublicKey": public_key},
                         headers={"Authorization": bedrock, "Client-Version": _GAME_VERSION})
    chain = issued.get("chain")
    if not isinstance(chain, list) or len(chain) != 2:
        raise ExchangeError("bedrock-chain", "expected two issued certificates")
    certificates = tuple(_text(certificate, "bedrock-chain") for certificate in chain)

    multiplayer = await _post(client, "minecraft-multiplayer-token",
                              f"{_MINECRAFT_AUTH}/api/v1.0/multiplayer/session/start", {"publicKey": public_key},
                              headers={"Authorization": session_auth})
    signed_token = _text(_field(multiplayer.get("result"), "signedToken"), "minecraft-multiplayer-token")
    return BedrockCredentials(certificates, signed_token)
