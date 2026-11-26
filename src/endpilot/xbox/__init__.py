"""Optional Xbox credentials from a Microsoft personal-account device flow."""

import asyncio
from asyncio import sleep
import base64
from collections.abc import Callable
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
import json
import logging
from pathlib import Path
import re
from uuid import UUID

from ..authentication import BedrockCredentials, _json_object, _read_public_key


__all__ = [
    "DEFAULT_CLIENT_ID", "XboxAccountCacheError", "XboxAuthorizationUnavailable", "XboxCredentialProvider",
    "XboxReauthenticationRequired", "account_cache_path", "forget_account",
]

DEFAULT_CLIENT_ID = "e7cfcfa2-c0bb-419c-bf4a-fd966b7fe716"

_AUTHORITY = "https://login.microsoftonline.com/consumers"
_AUTHORITY_HOST = "login.microsoftonline.com"
_SCOPES = ("XboxLive.signin",)
_REQUEST_TIMEOUT = 10.0
_VERIFICATION_URIS = {
    "https://microsoft.com/devicelogin", "https://www.microsoft.com/devicelogin", "https://www.microsoft.com/link",
}
_APPLICATION_ID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
_REAUTH_ERRORS = ("invalid_grant", "interaction_required", "login_required", "consent_required")
_TEMPORARY_ERRORS = ("server_error", "temporarily_unavailable")
_UNAVAILABLE = "Microsoft authorization service is temporarily unavailable"

# The CLI can tie a nonblocking prompt to this device round without extending
# the public callback contract.
_device_prompt_scope: ContextVar[tuple[ExitStack, float] | None] = ContextVar("device_prompt_scope", default=None)


class XboxAccountCacheError(RuntimeError):
    """The local Microsoft account cache could not be accessed safely."""


class XboxAuthorizationUnavailable(RuntimeError):
    """The Microsoft authorization service is temporarily unavailable."""


class XboxReauthenticationRequired(RuntimeError):
    """Authorize this Microsoft account interactively before renewing its cache."""


def _application_id(client_id: str) -> str:
    if not isinstance(client_id, str) or not _APPLICATION_ID.fullmatch(client_id) or not UUID(client_id).int:
        raise ValueError("client_id must be a Microsoft application UUID")
    return client_id.lower()


def _cache_operation(operation, *args):
    try:
        return operation(*args)
    except Exception:
        raise XboxAccountCacheError("Xbox account cache could not be accessed") from None


@asynccontextmanager
async def _locked_cache(path: Path):
    from . import _cache

    async with AsyncExitStack() as entered:
        try:
            await entered.enter_async_context(_cache.locked_cache(path))
        except Exception:
            raise XboxAccountCacheError("Xbox account cache could not be locked") from None
        yield


def account_cache_path(client_id: str = DEFAULT_CLIENT_ID) -> Path:
    """Return the current user's account cache path for this Microsoft application."""
    from . import _cache

    return _cache_operation(_cache.cache_path, _application_id(client_id))


async def forget_account(client_id: str = DEFAULT_CLIENT_ID) -> bool:
    """Delete the saved local account for this application and report whether it existed."""
    from . import _cache

    path = account_cache_path(client_id)
    async with _locked_cache(path):
        return _cache_operation(_cache.delete_cache, path)


@contextmanager
def _private_library_logs():
    # HTTP debug logs can include response headers. A context-local filter
    # leaves other tasks' and threads' logging intact.
    private = ContextVar("xbox_auth_logs", default=False)

    def allow_record(_record):
        return not private.get()

    children = [logging.getLogger(name) for name in ("httpx", "httpcore", "msal")]
    loggers = []
    while children:
        logger = children.pop()
        loggers.append(logger)
        children.extend(logger.getChildren())
    token = private.set(True)
    try:
        for logger in loggers:
            logger.addFilter(allow_record)
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(allow_record)
        private.reset(token)


def _require_json(response) -> None:
    # MSAL logs the raw token body when JSON decoding fails.
    if response.status_code == 429 or response.status_code >= 500:
        raise XboxAuthorizationUnavailable(_UNAVAILABLE)
    response.read()
    if not isinstance(response.json(), dict):
        raise ValueError("Microsoft response was not a JSON object")


async def _run_msal(operation, failure: str, *args, **kwargs):
    attempt = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(attempt)
    except asyncio.CancelledError:
        # A worker may be inside a bounded HTTP call. Drain it before closing
        # its client, even if the caller requests cancellation again.
        while not attempt.done():
            try:
                await asyncio.wait((attempt,))
            except asyncio.CancelledError:
                continue
        if not attempt.cancelled():
            attempt.exception()
        raise
    except XboxAuthorizationUnavailable:
        raise
    except Exception as error:
        from httpx import RequestError

        if isinstance(error, RequestError):
            raise XboxAuthorizationUnavailable(_UNAVAILABLE) from None
    raise RuntimeError(failure)


@contextmanager
def _microsoft_client(httpx):
    try:
        client = httpx.Client(
            timeout=_REQUEST_TIMEOUT, follow_redirects=False, event_hooks={"response": [_require_json]},
        )
    except Exception:
        raise RuntimeError("Microsoft authorization client could not start") from None
    with _private_library_logs(), client:
        yield client


async def _microsoft_app(msal, client, client_id, cache, *, renewable):
    return await _run_msal(
        msal.PublicClientApplication, "Microsoft authority discovery failed", client_id,
        authority=_AUTHORITY, token_cache=cache, http_client=client, instance_discovery=False,
        exclude_scopes=["profile"] if renewable else ["profile", "offline_access"], enable_pii_log=False,
    )


_CACHE_FIELDS = {
    "Account": ("home_account_id", "environment", "realm", "authority_type"),
    "AccessToken": (
        "credential_type", "home_account_id", "environment", "client_id", "realm", "target", "secret",
        "token_type", "cached_at", "expires_on", "extended_expires_on", "refresh_on",
    ),
    "RefreshToken": (
        "credential_type", "home_account_id", "environment", "client_id", "target", "secret",
        "last_modification_time",
    ),
}
_REQUIRED_CACHE_FIELDS = {
    "Account": {"realm", "authority_type"},
    "AccessToken": {"secret", "realm", "target", "expires_on"},
    "RefreshToken": {"secret"},
}


def _selected_cache(serialized: str, client_id: str, account_id: str) -> str:
    """Keep the MSAL fields used to renew only the selected account for this app."""
    try:
        state = json.loads(serialized)
        selected = {}
        for kind, fields in _CACHE_FIELDS.items():
            entries = {}
            for key, entry in state.get(kind, {}).items():
                if entry.get("home_account_id") != account_id or entry.get("environment") != _AUTHORITY_HOST:
                    continue
                if kind != "Account" and entry.get("client_id") != client_id:
                    continue
                known = {field: entry[field] for field in fields if isinstance(entry.get(field), str)}
                if not _REQUIRED_CACHE_FIELDS[kind].issubset(known):
                    raise ValueError
                for timestamp in ("expires_on", "refresh_on", "last_modification_time"):
                    if timestamp in known:
                        int(known[timestamp])
                entries[key] = known
            if entries:
                selected[kind] = entries
        return json.dumps(selected)
    except (AttributeError, TypeError, ValueError, RecursionError):
        raise XboxAccountCacheError("Xbox account cache is invalid") from None


def _trusted_accounts(app):
    return [account for account in app.get_accounts()
            if account.get("environment") == _AUTHORITY_HOST and account.get("realm") == app.authority.tenant
            and account.get("authority_type") == "MSSTS" and account.get("home_account_id")]


def _has_refresh_token(cache, client_id, account_id):
    tokens = list(cache.search(cache.CredentialType.REFRESH_TOKEN, query={
        "home_account_id": account_id, "environment": _AUTHORITY_HOST, "client_id": client_id,
    }))
    return any(isinstance(entry.get("secret"), str) and entry["secret"] for entry in tokens)


def _device_cache_state(cache, app, client_id, result, expected_account_id) -> tuple[str, str] | None:
    reported_account_id = _client_info_account_id(result)
    if expected_account_id is not None and reported_account_id not in (None, expected_account_id):
        raise RuntimeError("Microsoft device authorization returned a different account")

    account_id = None
    accounts = _trusted_accounts(app)
    if len(accounts) == 1:
        candidate = accounts[0]["home_account_id"]
        # Match the entry from this device response. MSAL checks expiry when reusing it.
        access_tokens = list(cache.search(cache.CredentialType.ACCESS_TOKEN, query={
            "home_account_id": candidate, "environment": _AUTHORITY_HOST,
            "client_id": client_id, "realm": app.authority.tenant,
        }, now=0))
        if (reported_account_id in (None, candidate)
                and any(entry.get("secret") == result["access_token"] for entry in access_tokens)):
            account_id = candidate
    if expected_account_id is not None and (
        reported_account_id != expected_account_id or account_id != expected_account_id
    ):
        raise XboxReauthenticationRequired("Microsoft device authorization could not confirm the selected account")
    if account_id is not None and _has_refresh_token(cache, client_id, account_id):
        return account_id, _selected_cache(cache.serialize(), client_id, account_id)
    return None


async def _device_access_token(msal, httpx, client_id, on_device_code, *, remember=False, expected_account_id=None):
    if on_device_code is None:
        raise XboxReauthenticationRequired("Microsoft account requires interactive authorization")

    class AttemptCache(msal.TokenCache):
        # A one-time flow has no later use for MSAL's account or token entries.
        def add(self, event, now=None):
            pass

    cache = msal.SerializableTokenCache() if remember else AttemptCache()
    with _microsoft_client(httpx) as client, ExitStack() as prompt:
        app = await _microsoft_app(msal, client, client_id, cache, renewable=remember)
        flow = await _run_msal(
            app.initiate_device_flow, "Microsoft device authorization could not start", scopes=_SCOPES,
        )
        if flow.get("error") in _TEMPORARY_ERRORS:
            raise XboxAuthorizationUnavailable(_UNAVAILABLE)
        uri, code, device_code = (flow.get(key) for key in ("verification_uri", "user_code", "device_code"))
        interval, lifetime = flow.get("interval"), flow.get("expires_in")
        if (
            "error" in flow or not isinstance(uri, str) or uri not in _VERIFICATION_URIS
            or not isinstance(code, str) or re.fullmatch(r"[A-Z0-9-]{4,20}", code) is None
            or not isinstance(device_code, str) or not device_code
            or type(interval) is not int or interval <= 0 or type(lifetime) is not int
        ):
            raise RuntimeError("Microsoft device authorization returned an invalid prompt")

        clock = asyncio.get_running_loop().time
        expires = clock() + lifetime
        if expires > clock():
            scope = _device_prompt_scope.set((prompt, expires))
            try:
                on_device_code(uri, code)
            except Exception:
                raise RuntimeError("Microsoft device authorization callback failed") from None
            finally:
                _device_prompt_scope.reset(scope)

        while clock() < expires:
            result = await _run_msal(
                app.acquire_token_by_device_flow, "Microsoft device authorization polling failed",
                flow, exit_condition=lambda _: True,
            )
            if clock() >= expires or result.get("error") == "expired_token":
                break
            if result.get("error") in _TEMPORARY_ERRORS:
                raise XboxAuthorizationUnavailable(_UNAVAILABLE)
            token = result.get("access_token")
            if isinstance(token, str) and token and "error" not in result:
                state = _device_cache_state(cache, app, client_id, result, expected_account_id) if remember else None
                return token, state
            if result.get("error") not in ("authorization_pending", "slow_down"):
                raise RuntimeError("Microsoft device authorization was denied")
            await sleep(min(flow["interval"], max(0, expires - clock())))
        raise TimeoutError("Microsoft device authorization expired")


def _client_info_account_id(result) -> str | None:
    encoded = result.get("client_info")
    if not isinstance(encoded, str):
        return None
    try:
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        info = json.loads(decoded, object_pairs_hook=_json_object)
    except (ValueError, RecursionError):
        return None
    if not isinstance(info, dict):
        return None
    uid, utid = info.get("uid"), info.get("utid")
    return f"{uid}.{utid}" if isinstance(uid, str) and uid and isinstance(utid, str) and utid else None


async def _silent_access_token(
    msal, httpx, client_id, account_id, serialized, *, save_rotated=None, require_account_proof=False,
):
    cache = msal.SerializableTokenCache()
    cache.deserialize(_selected_cache(serialized, client_id, account_id))
    needs_authorization = "Microsoft account requires interactive reauthorization"
    # Exhaust the search before giving the cache to the worker. MSAL holds its
    # cache lock for the lifetime of the search iterator.
    accounts = list(cache.search(cache.CredentialType.ACCOUNT, query={"home_account_id": account_id}))
    if not accounts or not _has_refresh_token(cache, client_id, account_id):
        raise XboxReauthenticationRequired(needs_authorization)
    with _microsoft_client(httpx) as client:
        app = await _microsoft_app(msal, client, client_id, cache, renewable=True)
        account = next((account for account in _trusted_accounts(app) if account["home_account_id"] == account_id), None)
        if account is None:
            raise XboxReauthenticationRequired(needs_authorization)

        def refresh():
            result = app.acquire_token_silent_with_error(list(_SCOPES), account)
            token = result.get("access_token") if result else None
            rotated = None
            if isinstance(token, str) and token and "error" not in result:
                reported_account_id = _client_info_account_id(result)
                check_reported_account = reported_account_id is not None or (
                    "client_info" in result and not require_account_proof
                )
                if check_reported_account and reported_account_id != account_id:
                    return None, None, RuntimeError("Microsoft silent authorization returned a different account")
                if require_account_proof:
                    source = result.get("token_source")
                    if source != "cache" and not (source == "identity_provider" and reported_account_id == account_id):
                        return None, None, XboxReauthenticationRequired(
                            "Microsoft account requires interactive confirmation before linking its Xbox profile",
                        )
                if cache.has_state_changed:
                    try:
                        rotated = _selected_cache(cache.serialize(), client_id, account_id)
                        if save_rotated is not None:
                            # The legacy cache records only Microsoft authorization.
                            # Retain a completed rotation if its caller cancels.
                            save_rotated(rotated)
                    except Exception as error:
                        return result, None, error
            return result, rotated, None

        result, rotated, write_failure = await _run_msal(refresh, "Microsoft silent authorization failed")
        if write_failure is not None:
            raise write_failure
        if result and result.get("error") in _TEMPORARY_ERRORS:
            raise XboxAuthorizationUnavailable(_UNAVAILABLE)
        if not result or result.get("error") in _REAUTH_ERRORS:
            raise XboxReauthenticationRequired(needs_authorization)
        token = result.get("access_token")
        if isinstance(token, str) and token and "error" not in result:
            return token, rotated
        raise RuntimeError("Microsoft silent authorization failed")


def _cache_file_present(path: Path) -> bool:
    try:
        path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        raise XboxAccountCacheError("Xbox account cache could not be checked") from None
    return True


def _select_account(accounts, xuid=None, *, legacy_account_id=None):
    if xuid is not None:
        for account in accounts:
            if account.xuid == xuid:
                return account
        raise ValueError("no saved Xbox account matches the selected XUID")
    if legacy_account_id is not None:
        for account in accounts:
            if account.xuid is None and account.home_account_id == legacy_account_id:
                return account
        raise ValueError("the selected legacy Xbox account is no longer listed")
    if len(accounts) > 1:
        raise XboxAccountCacheError("Xbox account cache contains multiple accounts and requires a selection")
    return accounts[0] if accounts else None


async def _legacy_access_token(msal, httpx, client_id, path, saved, on_device_code, remember_account):
    from . import _cache

    if saved is not None:
        def save_rotated(serialized):
            _cache_operation(_cache.save_cache, path, saved.home_account_id, serialized)

        try:
            token, _ = await _silent_access_token(
                msal, httpx, client_id, saved.home_account_id, saved.serialized_cache, save_rotated=save_rotated,
            )
            return token, False
        except XboxReauthenticationRequired:
            if not remember_account or on_device_code is None:
                raise
    elif not remember_account:
        raise XboxReauthenticationRequired("Microsoft account requires interactive reauthorization")

    token, state = await _device_access_token(msal, httpx, client_id, on_device_code, remember=True)
    if state is not None:
        _cache_operation(_cache.save_cache, path, *state)
    return token, state is None


def _authorization_dependencies():
    try:
        import httpx
        import msal
        from . import _exchange, _verify
    except ImportError:
        raise RuntimeError("Xbox authorization requires the EndPilot xbox extra") from None
    return msal, httpx, _exchange.exchange, _verify


async def _exchange_credentials(httpx, exchange, verify, access_token, public_key):
    try:
        client = httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, follow_redirects=False)
    except Exception:
        raise RuntimeError("Xbox credential exchange could not start") from None
    with _private_library_logs():
        async with client:
            credentials = await exchange(client, access_token, public_key)
            verified = await verify(client, credentials, public_key)
            return credentials, verified


async def _verified_account_credentials(dependencies, client_id, path, public_key, saved, on_device_code):
    from . import _cache

    msal, httpx, exchange, verify = dependencies
    expected_account_id = saved.home_account_id if saved is not None and saved.xuid is None else None
    if saved is not None:
        try:
            token, rotated = await _silent_access_token(
                msal, httpx, client_id, saved.home_account_id, saved.serialized_cache,
                require_account_proof=expected_account_id is not None,
            )
        except XboxReauthenticationRequired:
            if on_device_code is None:
                raise
        else:
            credentials, profile = await _exchange_credentials(httpx, exchange, verify.verify_account, token, public_key)
            xuid, gamertag = profile
            if saved.xuid is not None and saved.xuid != xuid:
                raise ValueError("verified Xbox XUID does not match the selected account")
            if rotated is not None or (xuid, gamertag) != (saved.xuid, saved.gamertag):
                record = _cache.AccountRecord(saved.home_account_id, rotated or saved.serialized_cache, xuid, gamertag)
                _cache_operation(_cache.upsert_account, path, record)
            return credentials, profile, False

    token, state = await _device_access_token(
        msal, httpx, client_id, on_device_code, remember=True, expected_account_id=expected_account_id,
    )
    credentials, profile = await _exchange_credentials(httpx, exchange, verify.verify_account, token, public_key)
    if state is not None:
        record = _cache.AccountRecord(*state, *profile)
        _cache_operation(_cache.upsert_account, path, record)
    return credentials, profile, state is None


class XboxCredentialProvider:
    """Acquire Bedrock credentials using EndPilot's Microsoft application by default.

    Pass ``client_id`` to use another authorized Microsoft application.
    Pass a synchronous callback to enable device authorization. The callback
    receives the Microsoft verification URI and the player's short code.
    ``remember_account`` allows saving an account's Microsoft authorization.
    """

    def __init__(
        self, client_id: str = DEFAULT_CLIENT_ID, *, on_device_code: Callable[[str, str], None] | None = None,
        remember_account: bool = False,
    ):
        self._client_id = _application_id(client_id)
        if on_device_code is not None and not callable(on_device_code):
            raise TypeError("on_device_code must be callable or None")
        if type(remember_account) is not bool:
            raise TypeError("remember_account must be a bool")
        self._on_device_code = on_device_code
        self._remember_account = remember_account

    async def __call__(self, public_key: str) -> BedrockCredentials:
        """Authorize the player and verify the credentials for this P-384 public key."""
        credentials, _ = await self._credentials_with_remember_status(public_key)
        return credentials

    async def _credentials_with_remember_status(self, public_key: str) -> tuple[BedrockCredentials, bool]:
        """Return verified credentials and whether this device authorization could not be remembered."""
        _read_public_key(public_key, "session public key")
        dependencies = _authorization_dependencies()
        from . import _cache

        msal, httpx, exchange, verify = dependencies
        path = account_cache_path(self._client_id)
        if self._remember_account or _cache_file_present(path):
            async with _locked_cache(path):
                saved = _select_account(_cache_operation(_cache.load_accounts, path))
                if saved is not None and saved.xuid is not None:
                    credentials, _, remember_skipped = await _verified_account_credentials(
                        dependencies, self._client_id, path, public_key, saved,
                        self._on_device_code if self._remember_account else None,
                    )
                    return credentials, remember_skipped
                access_token, remember_skipped = await _legacy_access_token(
                    msal, httpx, self._client_id, path, saved, self._on_device_code, self._remember_account,
                )
        else:
            access_token, _ = await _device_access_token(msal, httpx, self._client_id, self._on_device_code)
            remember_skipped = False

        credentials, _ = await _exchange_credentials(
            httpx, exchange, verify.verify_credentials, access_token, public_key,
        )
        return credentials, remember_skipped

    async def _credentials_for_account(
        self, public_key: str, *, xuid: str | None = None, add: bool = False, legacy_account_id: str | None = None,
    ) -> tuple[BedrockCredentials, tuple[str, str], bool]:
        """Acquire a verified Xbox profile and credentials, saving renewable authorization."""
        _read_public_key(public_key, "session public key")
        dependencies = _authorization_dependencies()
        from . import _cache

        if xuid is not None and (add or not _cache._valid_xuid(xuid)):
            raise ValueError("an Xbox account selection requires a single valid XUID")
        if legacy_account_id is not None and (
            add or xuid is not None or not isinstance(legacy_account_id, str) or not legacy_account_id
        ):
            raise ValueError("a legacy Xbox account selection requires a single saved record")
        path = account_cache_path(self._client_id)
        async with _locked_cache(path):
            accounts = _cache_operation(_cache.load_accounts, path)
            saved = None if add else _select_account(accounts, xuid, legacy_account_id=legacy_account_id)
            return await _verified_account_credentials(
                dependencies, self._client_id, path, public_key, saved, None if xuid is not None else self._on_device_code,
            )
