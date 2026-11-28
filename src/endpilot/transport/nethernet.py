"""NetherNet HTTP signaling for a Bedrock server."""

import asyncio
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from nethernet import ConnectionFailed, InvalidIdentity, PlayerSigner, SessionError
from nethernet.identity import build_envelope, insert_identity, sign_fingerprints

from .._join_failure import _set_join_stage
from ._nethernet_http import _check_signaling_endpoint, _protocol_number, connect_http


class _OnlinePlayerSigner:
    def __init__(self, private_key: ec.EllipticCurvePrivateKey, token: str, issuer: str) -> None:
        domain = urlsplit(issuer)
        if not domain.scheme or not domain.netloc:
            raise ValueError("NetherNet issuer must be an absolute URL")
        self._private_key = private_key
        self._token = token
        self._domain = domain._replace(path=domain.path or "/").geturl()

    def sign(self, sdp: str) -> str:
        fingerprints = sign_fingerprints(sdp, self._private_key)
        return insert_identity(sdp, build_envelope(self._domain, self._token, fingerprints))


async def advertised_protocol(url: str, *, timeout: float = 5) -> int:
    """Read the Bedrock protocol from the server's HTTP signaling advertisement."""
    import aiohttp

    endpoint = f"{url.rstrip('/')}/v1/join"
    try:
        async with asyncio.timeout(timeout), aiohttp.ClientSession() as client:
            async with client.get(endpoint, allow_redirects=False) as response:
                if not 200 <= response.status < 300:
                    raise ConnectionError(f"NetherNet protocol discovery returned HTTP {response.status} at {endpoint}")
                advertisement = await response.json(content_type=None)
    except TimeoutError as error:
        raise TimeoutError(f"NetherNet protocol discovery timed out after {timeout:g}s at {endpoint}") from error
    except aiohttp.ClientError as error:
        raise ConnectionError(f"NetherNet protocol discovery failed at {endpoint}") from error
    except ValueError as error:
        raise ValueError(f"NetherNet advertisement at {endpoint} is not JSON") from error

    protocol = _protocol_number(advertisement)
    if protocol is None:
        raise ValueError(f"NetherNet advertisement at {endpoint} has no valid protocol")
    return protocol


async def connect(
    url: str, *, private_key: ec.EllipticCurvePrivateKey | None = None,
    server_key_sha256: str | None = None,
    multiplayer_token: str | None = None, issuer: str | None = None,
    _check_endpoint: bool = False,
    _on_protocol: Callable[[int | None], Awaitable[None]] | None = None,
):
    if _check_endpoint:
        if _on_protocol is None:
            await _check_signaling_endpoint(url)
        else:
            await _on_protocol(await _check_signaling_endpoint(url, read_protocol=True))
            _on_protocol = None
    _set_join_stage("transport_connect")
    if multiplayer_token is None and issuer is None:
        key = private_key or ec.generate_private_key(ec.SECP384R1())
        signer = PlayerSigner(key, domain="self")
    else:
        if private_key is None or not multiplayer_token or not issuer:
            raise ValueError("online NetherNet requires private_key, multiplayer_token, and issuer")
        signer = _OnlinePlayerSigner(private_key, multiplayer_token, issuer)

    def verify_pin(server_identity):
        if server_identity.key_digest != server_key_sha256:
            raise InvalidIdentity("NetherNet server identity does not match server_key_sha256")

    try:
        return await connect_http(
            url, identity_signer=signer,
            on_server_identity=verify_pin if server_key_sha256 is not None else None,
            capability_check=not _check_endpoint, _on_protocol=_on_protocol,
        )
    except ConnectionFailed as error:
        if _check_endpoint and error.error == SessionError.SIGNALING_FAILED_TO_SEND:
            error.add_note("NetherNet HTTP offer exchange failed before the transport opened")
        raise
