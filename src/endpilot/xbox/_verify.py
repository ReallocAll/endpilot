"""Verify retail Bedrock credentials before handing them to the SDK."""

import time

from cryptography.hazmat.primitives.asymmetric import ec, rsa
import httpx
import jwt

from ..authentication import BedrockCredentials, _expiry, _read_certificate_chain, _read_jwt, _read_public_key


_ISSUER = "https://authorization.franchise.minecraft-services.net/"
_DISCOVERY = _ISSUER + ".well-known/openid-configuration"
_AUDIENCE = "api://auth-minecraft-services/multiplayer"
_ALGORITHM_KEYS = {"RS256": "RSA", "ES384": "EC"}
# Retail legacy certificate authority in the fixed Bedrock protocol reference.
_MOJANG_ROOT = (
    "MHYwEAYHKoZIzj0CAQYFK4EEACIDYgAECRXueJeTDqNRRgJi/vlRufByu/2G0i2Ebt6YMar5QX/R0DIIyrJMcUpruK4QveTfJSTp3Shlq4Gk"
    "34cD/4GUWwkv0DVuzeuB+tXija7HBxii03NHDbPAD0AKnLr2wdAp"
)


async def _json(client: httpx.AsyncClient, url: str | httpx.URL) -> dict:
    # A bare request avoids client defaults such as cookies or Authorization.
    request = httpx.Request("GET", url, headers={"Accept": "application/json"})
    response = await client.send(request, auth=None, follow_redirects=False)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def _jwks_url(metadata: dict) -> httpx.URL:
    value = metadata.get("jwks_uri")
    if metadata.get("issuer") != _ISSUER or not isinstance(value, str) or "#" in value:
        raise ValueError("invalid issuer metadata")
    url = httpx.URL(value)
    if url.scheme != "https" or not url.host or url.userinfo:
        raise ValueError("invalid key set URL")
    return url


def _signing_key(key_set: dict, key_id: str, algorithm: str) -> rsa.RSAPublicKey | ec.EllipticCurvePublicKey:
    keys = key_set.get("keys")
    if not isinstance(keys, list):
        raise ValueError("invalid key set")
    matching = [entry for entry in keys if isinstance(entry, dict) and entry.get("kid") == key_id]
    if len(matching) != 1:
        raise ValueError("expected a single matching signing key")
    jwk = matching[0]
    operations = jwk.get("key_ops", ["verify"])
    if (jwk.get("kty") != _ALGORITHM_KEYS[algorithm] or jwk.get("alg", algorithm) != algorithm
            or jwk.get("use", "sig") != "sig" or not isinstance(operations, list) or "verify" not in operations):
        raise ValueError("unexpected signing key properties")
    key = jwt.PyJWK.from_dict(jwk, algorithm=algorithm).key
    if algorithm == "RS256" and isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048:
        return key
    if algorithm == "ES384" and isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP384R1):
        return key
    raise ValueError("unexpected signing key type")


async def verify_credentials(client: httpx.AsyncClient, credentials: BedrockCredentials, public_key: str) -> None:
    """Verify both issued credentials and bind them to the same session public key."""
    try:
        session_key = _read_public_key(public_key, "session public key")
        signer, profile, chain_expiry = _read_certificate_chain(credentials.certificate_chain, session_key, time.time())
        if signer != _MOJANG_ROOT:
            raise ValueError("untrusted certificate root")

        token = credentials.multiplayer_token
        header = jwt.get_unverified_header(token)
        algorithm, key_id = header.get("alg"), header.get("kid")
        if algorithm not in _ALGORITHM_KEYS or not isinstance(key_id, str) or not key_id:
            raise ValueError("invalid multiplayer signing header")
        metadata = await _json(client, _DISCOVERY)
        keys = await _json(client, _jwks_url(metadata))
        claims = jwt.decode(
            token, _signing_key(keys, key_id, algorithm), algorithms=tuple(_ALGORITHM_KEYS),
            issuer=_ISSUER, audience=_AUDIENCE,
            # The SDK enforces strict expiry with skew only for iat and nbf.
            options={"require": ["exp", "iat", "cpk", "xid", "xname"],
                     "verify_exp": False, "verify_iat": False, "verify_nbf": False},
        )
        now = time.time()
        _expiry(claims, now, "multiplayer token")
        token_key = _read_public_key(claims["cpk"], "multiplayer token public key")
        if (claims["iss"] != _ISSUER or chain_expiry <= now or token_key.public_numbers() != session_key.public_numbers()
                or (claims["xid"], claims["xname"]) != (profile.get("XUID"), profile.get("displayName"))):
            raise ValueError("credentials do not agree")
    except (httpx.HTTPError, httpx.InvalidURL, jwt.PyJWTError, KeyError, ValueError, TypeError):
        raise ValueError("Bedrock credential verification failed") from None


async def verify_account(
    client: httpx.AsyncClient, credentials: BedrockCredentials, public_key: str,
) -> tuple[str, str]:
    """Read the XUID and gamertag after checking both signatures and the session key."""
    from ._cache import _validate_profile

    await verify_credentials(client, credentials, public_key)
    try:
        # Use the same strict JWT parsing as Bedrock login, including duplicate claim rejection.
        _, claims, _, _ = _read_jwt(credentials.multiplayer_token, tuple(_ALGORITHM_KEYS), "multiplayer token")
        xuid, gamertag = claims["xid"], claims["xname"]
        _validate_profile(xuid, gamertag)
        return xuid, gamertag
    except (KeyError, ValueError):
        raise ValueError("Bedrock credential verification failed") from None
