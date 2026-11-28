"""Dial NetherNet peers using HTTP signaling."""

# NetherNet HTTP dialing includes code adapted from nethernet-python b51ab0f.
# MIT License
#
# Copyright (c) 2026, Vincent (Zhengyu) Wu
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import errno
import inspect
import ssl
from collections.abc import Awaitable, Callable

from nethernet import (
    Connection, ConnectionClosed, ConnectionFailed, InvalidIdentity, NetworkID, SessionError, new_connection_id,
)
from nethernet.api import _make_http_session
from nethernet.constants import DEFAULT_NEGOTIATION_TIMEOUT
from nethernet.identity import strip_identity, verify_server_identity
from nethernet.signaling.http import SDP_CONTENT_TYPE, _parse_answer, _require_aiohttp

from .._join_failure import (
    _cleanup_failed_candidate, _nethernet_transport, _os_error_code, _record_connection_failure, _record_join_failure,
    _set_join_stage, _symbol_code,
)


class _NetherNetUnavailable(Exception):
    """A candidate failed before its NetherNet transport opened."""

    def __init__(self, error: Exception):
        self.error = error
        super().__init__(str(error))


def _connection_unavailable(error: BaseException) -> bool:
    import aiohttp

    if isinstance(error, (aiohttp.ServerDisconnectedError, TimeoutError)):
        return True
    if isinstance(error, aiohttp.ClientConnectorSSLError):
        # OpenSSL reports this when the listener answers a TLS hello in plaintext.
        return getattr(error.os_error, "reason", None) == "WRONG_VERSION_NUMBER"
    if isinstance(error, aiohttp.ClientSSLError):
        return False
    if isinstance(error, aiohttp.ClientConnectorError):
        return isinstance(error.os_error, (ConnectionError, TimeoutError)) or error.os_error.errno in (
            errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ETIMEDOUT,
        )
    return False


def _protocol_number(advertisement) -> int | None:
    try:
        number = int(str(advertisement["protocol"]))
    except (KeyError, TypeError, ValueError):
        return None
    return number if number > 0 else None


def _record_signaling_failure(error, url: str, stage: str) -> None:
    import aiohttp

    transport = _nethernet_transport(url)
    if isinstance(error, (aiohttp.ClientConnectorCertificateError, ssl.SSLCertVerificationError)):
        certificate_error = getattr(error, "certificate_error", error)
        error_code = getattr(certificate_error, "verify_code", None)
        _record_join_failure(error, transport, stage, "tls_certificate_verification", "verify_code",
                             error_code if type(error_code) is int else None)
    elif isinstance(error, (aiohttp.ClientSSLError, ssl.SSLError)):
        tls_error = error.os_error if isinstance(error, aiohttp.ClientConnectorSSLError) else error
        code = _symbol_code(getattr(tls_error, "reason", None))
        _record_join_failure(error, transport, stage, "tls_handshake", "openssl_reason", code)
    elif isinstance(error, aiohttp.ClientResponseError):
        status = error.status
        _record_join_failure(error, transport, stage, "http_error", "http_status",
                             status if type(status) is int and 100 <= status <= 599 else None)
    elif isinstance(error, aiohttp.ClientConnectorError):
        os_error = error.os_error
        reason = "timeout" if isinstance(os_error, TimeoutError) else "os_error"
        _record_join_failure(error, transport, stage, reason, *_os_error_code(os_error))
    else:
        _record_connection_failure(error, transport, stage)


def _signaling_connection_failed(reason: SessionError, cause: Exception) -> ConnectionFailed:
    error = ConnectionFailed(reason)
    if details := getattr(cause, "join_failure", None):
        error.join_failure = details
    return error


def _record_nethernet_failure(error: ConnectionClosed | ConnectionFailed, transport: str | None, stage: str) -> None:
    code = error.error
    reason = "transport_error"
    if code == SessionError.IDENTITY_NOT_ALLOWED:
        reason = "identity_rejected"
    elif code in (
        SessionError.NEGOTIATION_TIMEOUT, SessionError.NEGOTIATION_TIMEOUT_WAITING_FOR_RESPONSE,
        SessionError.NEGOTIATION_TIMEOUT_WAITING_FOR_ACCEPT, SessionError.INACTIVITY_TIMEOUT,
    ):
        reason = "timeout"
    name = code.name if isinstance(code, SessionError) else None
    _record_join_failure(error, transport, stage, reason, "nethernet_session_error", name)


async def _request(client, method: str, url: str, *, read_protocol: bool = False, **options) -> str | int | None:
    import aiohttp

    stage = "signaling_discovery" if method == "GET" else "signaling_connect"
    _set_join_stage(stage)
    try:
        async with client.request(method, url, allow_redirects=False, **options) as response:
            if response.status // 100 != 2:
                raise aiohttp.ClientResponseError(
                    response.request_info, response.history, status=response.status,
                    message=response.reason, headers=response.headers,
                )
            if method == "POST":
                return await response.text()
            if read_protocol:
                try:
                    advertisement = await response.json(content_type=None)
                except (ValueError, LookupError):
                    return None
                return _protocol_number(advertisement)
            return ""
    except (aiohttp.ClientError, OSError) as error:
        _record_signaling_failure(error, url, stage)
        raise


async def _check_signaling_endpoint(url: str, *, read_protocol: bool = False) -> int | None:
    _require_aiohttp()
    import aiohttp

    try:
        async with aiohttp.ClientSession() as client:
            number = await _request(client, "GET", f"{url.rstrip('/')}/v1/join", read_protocol=read_protocol)
    except (aiohttp.ClientError, OSError) as error:
        missing = isinstance(error, aiohttp.ClientResponseError) and error.status in (404, 405)
        if missing or _connection_unavailable(error):
            raise _NetherNetUnavailable(error) from error
        raise
    return number if read_protocol else None


async def _request_answer(
    url: str, local_id: NetworkID, offer: str, *, capability_check: bool,
    on_protocol: Callable[[int | None], Awaitable[None]] | None = None,
) -> str:
    _require_aiohttp()
    import aiohttp

    endpoint = f"{url.rstrip('/')}/v1/join"
    timeout = aiohttp.ClientTimeout(total=DEFAULT_NEGOTIATION_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        if capability_check:
            try:
                number = await _request(client, "GET", endpoint, read_protocol=on_protocol is not None)
            except (aiohttp.ClientError, OSError) as error:
                raise _signaling_connection_failed(SessionError.NO_SIGNALING_CHANNEL, error) from error
            if on_protocol is not None:
                await on_protocol(number)
        try:
            body = await _request(
                client, "POST", f"{endpoint}/{local_id}",
                data=offer.encode(), headers={"Content-Type": SDP_CONTENT_TYPE},
            )
        except (aiohttp.ClientError, OSError) as error:
            if not capability_check and _connection_unavailable(error):
                raise _NetherNetUnavailable(error) from error
            raise _signaling_connection_failed(SessionError.SIGNALING_FAILED_TO_SEND, error) from error
    try:
        return _parse_answer(body)
    except ConnectionFailed as error:
        _record_nethernet_failure(error, _nethernet_transport(url), "signaling_connect")
        raise


async def connect_http(
    url: str, *, identity_signer, on_server_identity, capability_check: bool,
    _on_protocol: Callable[[int | None], Awaitable[None]] | None = None,
) -> Connection:
    """Dial one URL. The selector has already probed it when capability_check is false."""
    local = NetworkID.p2p(new_connection_id())
    conn = _make_http_session(
        local_id=local, remote_id=NetworkID.unset(), is_dialer=True,
        on_open=Connection._mark_connected, on_close=Connection._mark_closed,
        ice_servers=None, relay_only=False, negotiation_timeout=DEFAULT_NEGOTIATION_TIMEOUT,
    )
    session = conn._session
    try:
        # b51ab0f creates this dialer's reliable channel without a session close handler.
        @session._pc._reliable.on("close")
        def _on_reliable_close() -> None:
            session.close(SessionError.DATA_CHANNEL_CLOSED)

        offer = identity_signer.sign(await session.offer())
        answer = await _request_answer(
            url, local, offer, capability_check=capability_check, on_protocol=_on_protocol,
        )
        _set_join_stage("transport_connect")
        if on_server_identity is not None:
            answer, server_identity = verify_server_identity(answer)
            result = on_server_identity(server_identity)
            if inspect.isawaitable(result):
                await result
        else:
            answer = strip_identity(answer)
        await session.accept_answer(answer)
        try:
            await conn._await_connected(None)
        except ConnectionFailed as error:
            _record_nethernet_failure(error, _nethernet_transport(url), "transport_connect")
            # The SDP answer has been accepted. Only the local session can
            # report these HTTP-dial negotiation timeouts at this point.
            if not capability_check and error.error in (
                SessionError.NEGOTIATION_TIMEOUT_WAITING_FOR_RESPONSE,
                SessionError.NEGOTIATION_TIMEOUT_WAITING_FOR_ACCEPT,
            ):
                raise _NetherNetUnavailable(error) from error
            raise
        return conn
    except BaseException as error:
        if isinstance(error, InvalidIdentity):
            _record_join_failure(error, _nethernet_transport(url), "transport_connect", "identity_verification")
        primary = error.error if isinstance(error, _NetherNetUnavailable) else error
        cleanup_error = await _cleanup_failed_candidate(session.aclose(SessionError.GENERIC_FAILURE), primary,
                                                        "NetherNet cleanup after failed dial")
        if cleanup_error is None or primary is error:
            raise
    raise primary
