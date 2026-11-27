"""MCP tools for the CLI's remembered Xbox accounts."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from functools import wraps
import hmac
import json
from pathlib import Path
from secrets import token_bytes
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric import ec

from . import xbox
from .authentication import BedrockCredentialProvider, _public_key_b64
from .skin import Skin, _SkinModel

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from .bot import Bot
    from .xbox._cache import AccountRecord, SkinBinding


class AccountError(RuntimeError):
    """An account operation failed with a message safe for MCP clients."""


class _Account(TypedDict):
    xuid: str
    gamertag: str


class _LegacyAccount(TypedDict):
    xuid: None
    gamertag: None
    legacy_selector: str


class _ForgetResult(TypedDict):
    forgotten: bool
    xuid: NotRequired[str | None]
    gamertag: NotRequired[str | None]
    legacy_selector: NotRequired[str]


class _DeviceChallenge(TypedDict):
    verification_uri: str
    user_code: str


class _AuthorizationStatus(TypedDict):
    authorization_id: str
    status: Literal["pending", "cancelling", "cancelled", "succeeded", "failed"]
    storage_notice: NotRequired[str]
    challenge: NotRequired[_DeviceChallenge]
    account: NotRequired[_Account]
    error: NotRequired[str]


@dataclass(slots=True)
class _Authorization:
    id: str
    storage_notice: str
    task: asyncio.Task[_AuthorizationStatus] = field(init=False, repr=False)
    challenge: _DeviceChallenge | None = field(default=None, repr=False)
    expires: float | None = None


@contextmanager
def _account_errors():
    try:
        yield
    except AccountError:
        raise
    except xbox.XboxReauthenticationRequired:
        raise AccountError("Xbox authorization is required. Use account_add to authorize the account.") from None
    except xbox.XboxAuthorizationUnavailable:
        raise AccountError("Xbox authorization is temporarily unavailable. Try again later.") from None
    except xbox.XboxAccountCacheError:
        raise AccountError("The local Xbox account cache is unreadable, invalid, or inaccessible.") from None
    except TimeoutError:
        raise AccountError("Xbox device authorization expired.") from None
    except ImportError:
        raise AccountError("Install endpilot[xbox] to manage Xbox accounts.") from None
    except Exception:
        raise AccountError("Xbox account operation failed. Check the Xbox extra and local account cache.") from None


@asynccontextmanager
async def _remembered_accounts():
    from .xbox import _cache

    path = xbox.account_cache_path()
    if not xbox._cache_file_present(path):
        yield path, ()
        return
    async with xbox._locked_cache(path):
        yield path, xbox._cache_operation(_cache.load_accounts, path)


def _select_verified_account(
    records: tuple["AccountRecord", ...], xuid: str | None, gamertag: str | None = None,
) -> "AccountRecord":
    if xuid is not None and gamertag is not None:
        raise AccountError("Specify a single Xbox account selector.")
    records = tuple(record for record in records if record.xuid is not None)
    if xuid is not None:
        matches = [record for record in records if record.xuid == xuid]
    elif gamertag is not None:
        matches = [record for record in records if record.gamertag.casefold() == gamertag.casefold()]
    else:
        matches = records
    if not matches:
        raise AccountError("No matching verified Xbox account. Use account_list or account_add.")
    if len(matches) != 1:
        raise AccountError("Multiple verified Xbox accounts match. Specify an XUID from account_list.")
    return matches[0]


def _load_skin(path: str | Path, model: _SkinModel, *, saved: bool = False) -> Skin:
    try:
        return Skin.from_png(path, model=model)
    except ImportError:
        raise AccountError("PNG skins require Pillow. Install endpilot[skin].") from None
    except (OSError, ValueError):
        if saved:
            raise AccountError("Could not load the saved Xbox skin. Use account_skin_set or account_skin_clear.") from None
        raise AccountError("Use a readable 64x32, 64x64, or 128x128 static skin PNG.") from None


class MCPAccounts:
    """Own at most one MCP device authorization for the host."""

    def __init__(self) -> None:
        self._authorization: _Authorization | None = None
        self._closed = False
        self._legacy_selector_key = token_bytes(32)

    def register(self, server: "MCPServer") -> None:
        from mcp.server.mcpserver.exceptions import ToolError

        def register_tool(tool):
            @wraps(tool)
            async def report_failure(*args, **kwargs):
                try:
                    return await tool(*args, **kwargs)
                except AccountError as error:
                    raise ToolError(str(error)) from None

            server.tool()(report_failure)

        register_tool(self.account_list)
        register_tool(self.account_add)
        register_tool(self.account_forget)
        register_tool(self.account_skin_set)
        register_tool(self.account_skin_clear)
        register_tool(self.account_authorization_status)
        register_tool(self.account_authorization_cancel)

    def _legacy_selector(self, record: "AccountRecord") -> str:
        # A host-local selector identifies a record without returning its Microsoft account ID.
        account_id = record.home_account_id.encode("utf-8", "surrogatepass")
        return hmac.digest(self._legacy_selector_key, account_id, "sha256").hex()

    def _account_summary(self, record: "AccountRecord") -> _Account | _LegacyAccount:
        if record.xuid is None:
            return {"xuid": None, "gamertag": None, "legacy_selector": self._legacy_selector(record)}
        return {"xuid": record.xuid, "gamertag": record.gamertag}

    async def account_list(self) -> list[_Account | _LegacyAccount]:
        """List remembered Xbox accounts, with a local selector for legacy records without an XUID."""
        with _account_errors():
            async with _remembered_accounts() as (_, records):
                return [self._account_summary(record) for record in records]

    async def account_forget(self, xuid: str | None = None, legacy_selector: str | None = None) -> _ForgetResult:
        """Forget a remembered account using its XUID or legacy selector, or forget the only saved account."""
        if xuid is not None and legacy_selector is not None:
            raise AccountError("Specify a single Xbox account selector.")
        with _account_errors():
            from .xbox import _cache

            async with _remembered_accounts() as (path, records):
                if xuid is not None:
                    selected = _select_verified_account(records, xuid)
                elif legacy_selector is not None:
                    selected = next((record for record in records if record.xuid is None
                                     and self._legacy_selector(record) == legacy_selector), None)
                    if selected is None:
                        raise AccountError("No matching legacy Xbox account. Use account_list.")
                elif not records:
                    return {"forgotten": False}
                elif len(records) > 1:
                    raise AccountError(
                        "Multiple Xbox accounts are remembered. Use an XUID or legacy selector from account_list.",
                    )
                else:
                    selected, = records
                forgotten = xbox._cache_operation(_cache.delete_account, path, selected.home_account_id)
                return {"forgotten": forgotten, **self._account_summary(selected)}

    async def account_skin_set(
        self, path: str, model: _SkinModel = "wide", xuid: str | None = None, gamertag: str | None = None,
    ) -> _Account:
        """Save a host-local skin PNG for a remembered Xbox account."""
        with _account_errors():
            from .xbox import _cache

            try:
                skin_path = Path(path).resolve()
            except (OSError, ValueError, RuntimeError):
                raise AccountError("Could not resolve the skin PNG path.") from None
            _load_skin(skin_path, model)
            return await self._set_skin(_cache.SkinBinding(str(skin_path), model), xuid, gamertag)

    async def account_skin_clear(self, xuid: str | None = None, gamertag: str | None = None) -> _Account:
        """Clear the saved skin for a remembered Xbox account."""
        with _account_errors():
            return await self._set_skin(None, xuid, gamertag)

    async def _set_skin(self, binding: "SkinBinding | None", xuid: str | None, gamertag: str | None) -> _Account:
        from .xbox import _cache

        async with _remembered_accounts() as (path, records):
            selected = _select_verified_account(records, xuid, gamertag)
            updated = xbox._cache_operation(
                _cache.set_account_skin, path, selected.home_account_id, selected.xuid, binding,
            )
            if not updated:
                raise AccountError("The selected Xbox account is no longer remembered.")
            return {"xuid": selected.xuid, "gamertag": selected.gamertag}

    def credential_provider(
        self, bot: "Bot", *, xuid: str | None = None, gamertag: str | None = None,
    ) -> BedrockCredentialProvider:
        """Bind a verified saved account to a fresh Bot's SDK join."""

        async def credentials(public_key: str):
            with _account_errors():
                async with _remembered_accounts() as (_, records):
                    selected = _select_verified_account(records, xuid, gamertag)
                provider = xbox.XboxCredentialProvider()
                result, (verified_xuid, _), _ = await provider._credentials_for_account(
                    public_key, xuid=selected.xuid,
                )
                if verified_xuid != selected.xuid:
                    raise AccountError("The signed-in Xbox profile differs from the selected account.")
                if selected.skin is not None and bot._skin is None:
                    # The SDK reads the skin after receiving the session's credentials.
                    bot._skin = _load_skin(selected.skin.path, selected.skin.model, saved=True)
                return result

        return credentials

    async def account_add(self) -> _AuthorizationStatus:
        """Start Xbox device authorization to remember an account locally."""
        if self._closed:
            raise AccountError("The Xbox authorization service is closed.")
        if self._authorization is not None and not self._authorization.task.done():
            raise AccountError("An Xbox authorization is already in progress.")
        with _account_errors():
            path = json.dumps(str(xbox.account_cache_path()), ensure_ascii=False)
        notice = (f"Remembering this account stores renewable sign-in credentials in plaintext at {path}. "
                  "Anyone who can read the file may reuse the authorization.")
        authorization = _Authorization(uuid4().hex, notice)
        authorization.task = asyncio.create_task(self._authorize(authorization))
        self._authorization = authorization
        return self._status(authorization)

    async def _authorize(self, authorization: _Authorization) -> _AuthorizationStatus:
        def clear_challenge() -> None:
            authorization.challenge = None

        def show_challenge(verification_uri: str, user_code: str) -> None:
            authorization.challenge = {"verification_uri": verification_uri, "user_code": user_code}
            scope = xbox._device_prompt_scope.get()
            if scope is not None:
                prompt, authorization.expires = scope
                prompt.callback(clear_challenge)

        try:
            with _account_errors():
                key = ec.generate_private_key(ec.SECP384R1())
                provider = xbox.XboxCredentialProvider(on_device_code=show_challenge)
                _, (xuid, gamertag), remember_skipped = await provider._credentials_for_account(
                    _public_key_b64(key.public_key()), add=True,
                )
                if remember_skipped:
                    raise AccountError("Xbox sign-in succeeded, but the account could not be remembered.")
        except AccountError as error:
            return {"authorization_id": authorization.id, "status": "failed", "error": str(error)}
        finally:
            clear_challenge()
        return {
            "authorization_id": authorization.id, "status": "succeeded",
            "account": {"xuid": xuid, "gamertag": gamertag},
        }

    def _find_authorization(self, authorization_id: str) -> _Authorization:
        authorization = self._authorization
        if authorization is None or authorization.id != authorization_id:
            raise AccountError("Unknown Xbox authorization ID. Use the ID returned by account_add.")
        return authorization

    @staticmethod
    def _status(authorization: _Authorization) -> _AuthorizationStatus:
        task = authorization.task
        if task.cancelled():
            return {"authorization_id": authorization.id, "status": "cancelled"}
        if task.done():
            return task.result()
        if task.cancelling():
            return {"authorization_id": authorization.id, "status": "cancelling"}
        status: _AuthorizationStatus = {
            "authorization_id": authorization.id, "status": "pending", "storage_notice": authorization.storage_notice,
        }
        if authorization.challenge is not None and (
            authorization.expires is None or asyncio.get_running_loop().time() < authorization.expires
        ):
            status["challenge"] = authorization.challenge.copy()
        return status

    async def account_authorization_status(self, authorization_id: str) -> _AuthorizationStatus:
        """Observe Xbox authorization. Show the storage notice before the verification link and user code."""
        return self._status(self._find_authorization(authorization_id))

    async def account_authorization_cancel(self, authorization_id: str) -> _AuthorizationStatus:
        """Cancel an Xbox device authorization and hide its code immediately."""
        authorization = self._find_authorization(authorization_id)
        task = authorization.task
        if not task.done() and not task.cancelling():
            task.cancel()
        return self._status(authorization)

    async def close(self) -> None:
        """Stop the device authorization and wait for the provider to close."""
        self._closed = True
        authorization = self._authorization
        if authorization is None or authorization.task.done():
            return
        await self.account_authorization_cancel(authorization.id)
        await asyncio.wait((authorization.task,))
