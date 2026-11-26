"""Join a Bedrock server or manage local Xbox accounts."""

import argparse
import asyncio
from contextlib import ExitStack
import json
from math import isfinite
from pathlib import Path
import re
import sys

from . import LoginRefused, Skin
from ._cli_terminal import (
    _can_read_terminal, _device_terminal, _print_remember_notice, _print_status, _show_device_code, _write_notice,
)
from ._join_address import _parse_join_address
from .skin import _SkinModel


class _Arguments(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.exit(2, "Invalid or unrecognized arguments. See --help.\n")

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if message:
            _write_notice(message.removesuffix("\n"), sys.stderr)
        super().exit(status)


class _CommandError(Exception):
    """A command failure with a message suitable for the terminal."""


def _login_refusal_message(error: LoginRefused) -> str:
    reason = error.reason_name
    if reason in (
        "OUTDATED_CLIENT", "OUTDATED_SERVER", "VERSION_MISMATCH", "EDITION_VERSION_MISMATCH", "EDITION_MISMATCH",
        "NETHER_NET_PROTOCOL_VERSION", "LOGIN_FAILED_CLIENT_OLD", "LOGIN_FAILED_SERVER_OLD",
        "LOGIN_FAILED_EDITION_MISMATCH_EDU_TO_VANILLA", "LOGIN_FAILED_EDITION_MISMATCH_VANILLA_TO_EDU",
    ):
        return "Login refused. Client and server versions are incompatible."
    if reason in (
        "INVALID_PLAYER", "NOT_AUTHENTICATED", "NOT_AUTHENTICATED_FAST_FAIL", "LOGIN_PACKET_NO_CERT",
        "NETHER_NET_IDENTITY_NOT_ALLOWED", "EXPIRED_TOKEN", "HOST_ACCEPTS_NO_TYPE_OF_AUTH", "INVALID_TENANT",
        "LOGIN_FAILED_INVALID_TENANT",
    ):
        return "Login refused. The server rejected the player's identity."
    message = error.server_message
    if reason in ("NOT_ALLOWED", "KICKED") and message and len(message) <= 256:
        if re.search(
            r"\b(?:allowlist denied|not (?:allow|white)-?listed|not on (?:the|this server's) (?:allow|white)list|"
            r"disconnect\.not(?:allow|white)listed)\b", message, re.IGNORECASE,
        ):
            return "Login refused. The server allowlist denied this player."
    return "Login refused by the server."


def _nethernet_failure_message(error: BaseException) -> str | None:
    # A failure from another transport does not require importing NetherNet.
    nethernet = sys.modules.get("nethernet")
    if nethernet is None:
        return None
    if isinstance(error, nethernet.InvalidIdentity):
        return "NetherNet identity verification failed."
    if isinstance(error, nethernet.ConnectionFailed):
        reason = nethernet.SessionError
        if error.error == reason.IDENTITY_NOT_ALLOWED:
            return "NetherNet connection refused. The player identity was rejected."
        if error.error in (reason.NEGOTIATION_TIMEOUT, reason.NEGOTIATION_TIMEOUT_WAITING_FOR_RESPONSE,
                           reason.NEGOTIATION_TIMEOUT_WAITING_FOR_ACCEPT):
            return "NetherNet connection timed out during negotiation."
    return None


def _choose_account(labels: list[str], owned: ExitStack, *, allow_name: bool) -> int:
    selector = "--name or --xuid" if allow_name else "--xuid"
    if not _can_read_terminal(sys.stdin):
        raise _CommandError(f"Multiple Xbox accounts are remembered. Select one with {selector}.")
    terminal = _device_terminal(owned)
    rows = [f"  {index}. {label}" for index, label in enumerate(labels, 1)]
    prompt = "\n".join(["Choose an Xbox account", *rows, "Enter a number, or press Enter to cancel."])
    if not _write_notice(prompt, terminal):
        raise _CommandError(f"Could not display the Xbox account chooser. Select an account with {selector}.")
    try:
        answer = sys.stdin.readline().strip()
    except (OSError, ValueError):
        raise _CommandError(f"Could not read the Xbox account choice. Select an account with {selector}.") from None
    if not answer:
        raise asyncio.CancelledError
    for index in range(len(labels)):
        if answer == str(index + 1):
            return index
    guidance = f"select an account with {selector}" if allow_name else "find the XUID for --xuid"
    raise _CommandError(f"Invalid Xbox account choice. Run account list to {guidance}.")


def _account_label(account) -> str:
    if account.xuid is None:
        return "Legacy Xbox account (XUID unavailable)"
    return f"{account.gamertag} ({account.xuid})"


def _select_account(accounts, xuid: str | None, *, name: str | None = None, allow_name: bool = True):
    if xuid is not None:
        for account in accounts:
            if account.xuid == xuid:
                return account
        raise _CommandError("No remembered Xbox account matches --xuid. Run account list to see saved accounts.")
    if name is not None:
        name = name.casefold()
        matches = [account for account in accounts if account.xuid is not None and account.gamertag.casefold() == name]
        if not matches:
            raise _CommandError("No remembered Xbox account matches --name. Run account list to see saved accounts.")
        if len(matches) > 1:
            raise _CommandError("Multiple remembered Xbox accounts match --name. Select one with --xuid.")
        return matches[0]
    if len(accounts) < 2:
        return accounts[0] if accounts else None
    with ExitStack() as owned:
        index = _choose_account([_account_label(account) for account in accounts], owned, allow_name=allow_name)
    return accounts[index]


def _add_account_selector(parser: argparse.ArgumentParser, *, requires_xbox: bool = False) -> None:
    selector = parser.add_mutually_exclusive_group()
    suffix = " (requires --xbox)" if requires_xbox else ""
    selector.add_argument("--xuid", metavar="XUID", help=f"select a remembered Xbox account by XUID{suffix}")
    selector.add_argument("--name", metavar="NAME", help=f"select a remembered Xbox account by gamertag{suffix}")


def _parser() -> argparse.ArgumentParser:
    parser = _Arguments(prog="python -m endpilot", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    join = commands.add_parser("join", help="connect to a Bedrock server")
    join.add_argument("address", metavar="ADDRESS",
                      help="host:port or a raknet://, https:// or http:// server URL")
    join.add_argument("--xbox", action="store_true", help="sign in with an Xbox account")
    _add_account_selector(join, requires_xbox=True)
    join.add_argument("--skin", type=Path, metavar="PNG", help="use a custom skin PNG for this join")
    join.add_argument("--skin-model", choices=("wide", "slim"), help="arm model for --skin (default: wide)")
    join.add_argument("--server-key-sha256", metavar="HEX", help="trusted NetherNet server key digest")
    join.add_argument("--hold", type=float, default=2, metavar="SECONDS",
                      help="time to stay connected after spawning (default: 2)")
    join.add_argument("--accept-resource-packs", action="store_true",
                      help="accept supported server resource packs for this join")
    account = commands.add_parser("account", help="manage locally remembered Xbox accounts")
    accounts = account.add_subparsers(dest="account_command", required=True)
    accounts.add_parser("add", help="authorize and remember an Xbox account")
    accounts.add_parser("list", help="list remembered Xbox accounts")
    forget = accounts.add_parser("forget", help="remove a remembered Xbox account")
    forget.add_argument("--xuid", metavar="XUID", help="select a remembered Xbox account")
    skin = accounts.add_parser("skin", help="manage a remembered Xbox account's skin")
    skin_commands = skin.add_subparsers(dest="skin_command", required=True)
    set_skin = skin_commands.add_parser("set", help="bind a skin PNG to a remembered Xbox account")
    set_skin.add_argument("skin", type=Path, metavar="PNG", help="path to a custom skin PNG")
    set_skin.add_argument("--model", choices=("wide", "slim"), default="wide", help="arm model (default: wide)")
    clear_skin = skin_commands.add_parser("clear", help="clear a remembered Xbox account's skin")
    _add_account_selector(set_skin)
    _add_account_selector(clear_skin)
    return parser


def _load_skin(path: str | Path, model: _SkinModel, *, saved: bool = False) -> Skin:
    try:
        return Skin.from_png(path, model=model)
    except ImportError:
        raise _CommandError("PNG skins require Pillow. Install endpilot[skin].") from None
    except (OSError, ValueError):
        if saved:
            raise _CommandError("Could not load the saved Xbox skin. Use join --skin to override it, "
                                "or run account skin set or account skin clear.") from None
        raise _CommandError("Could not load the skin PNG. Use a readable 64x32, 64x64, or 128x128 static PNG.") from None


async def _join(args, authorize_account=None, *, selected_account=None) -> None:
    from . import Bot, ResourcePackDecision

    if args.skin is None:
        bot = Bot("EndPilot")
    else:
        bot = Bot("EndPilot", skin=_load_skin(args.skin, args.skin_model or "wide"))

    hold_failure = None
    try:
        credential_provider = None
        if authorize_account is not None:
            binding = selected_account.skin if args.skin is None and selected_account is not None else None

            async def credentials(public_key: str):
                authorized, (xuid, _), remember_skipped = await authorize_account(public_key)
                if remember_skipped:
                    _write_notice("Xbox account was not remembered. Continuing with this sign-in only.", sys.stderr)
                if binding is not None and xuid == selected_account.xuid:
                    # The fresh CLI bot reads _skin after acquiring identity and before opening the connection.
                    bot._skin = _load_skin(binding.path, binding.model, saved=True)
                return authorized

            credential_provider = credentials
        on_resource_packs = None
        if args.accept_resource_packs:
            on_resource_packs = lambda _offer: ResourcePackDecision.ACCEPT
        await bot.join_address(
            args.address, credential_provider=credential_provider, server_key_sha256=args.server_key_sha256,
            on_resource_packs=on_resource_packs,
        )
        _print_status("Joined.", flush=True)
        if args.hold:
            try:
                end = await bot.wait_for_session_end(timeout=args.hold)
            except TimeoutError:
                end = bot.session_end
            if end is not None:
                message = ("The server disconnected the player before the hold ended."
                           if end.kind == "server_disconnect" else "The connection ended before the hold ended.")
                hold_failure = _CommandError(message)
    finally:
        try:
            await bot.close()
        except Exception as error:
            if hold_failure is not None:
                raise hold_failure from error
            raise
    if hold_failure is not None:
        raise hold_failure
    _print_status("Disconnected.", flush=True)


async def _authorize_account(public_key: str, path: Path, *, xuid: str | None = None, add: bool = False,
                             legacy_account_id: str | None = None):
    from . import xbox
    from .xbox import _cache

    with ExitStack() as owned:
        terminal = _device_terminal(owned) if xuid is None else None
        # The first sign-in can hold this application's lock before publishing a cache.
        if terminal is None and (add or not (
            xbox._cache_file_present(path) or xbox._cache_operation(_cache.cache_exists, path.with_suffix(".lock"))
        )):
            raise xbox.XboxReauthenticationRequired

        def show_code(verification_uri: str, code: str) -> None:
            if not _print_remember_notice(json.dumps(str(path), ensure_ascii=False), terminal=terminal):
                raise OSError("could not display the account cache notice")
            _show_device_code(terminal, verification_uri, code)

        provider = xbox.XboxCredentialProvider(on_device_code=show_code if terminal is not None else None)
        if legacy_account_id is not None:
            return await provider._credentials_for_account(public_key, legacy_account_id=legacy_account_id)
        return await provider._credentials_for_account(public_key, xuid=xuid, add=add)


async def _run_xbox(args) -> None:
    from . import xbox
    from .xbox import _cache

    try:
        path = xbox.account_cache_path()
        if args.command == "account" and args.account_command == "add":
            from cryptography.hazmat.primitives.asymmetric import ec

            from .authentication import _public_key_b64

            key = ec.generate_private_key(ec.SECP384R1())
            _, (xuid, gamertag), remember_skipped = await _authorize_account(
                _public_key_b64(key.public_key()), path, add=True,
            )
            if remember_skipped:
                raise _CommandError("Xbox sign-in succeeded, but the account could not be remembered.")
            _print_status(f"Remembered Xbox account {gamertag} ({xuid}).", flush=True)
            return

        accounts = ()
        if xbox._cache_file_present(path):
            async with xbox._locked_cache(path):
                accounts = xbox._cache_operation(_cache.load_accounts, path)
        if args.command == "account" and args.account_command == "list":
            if not accounts:
                _print_status("No remembered Xbox accounts.", flush=True)
            for account in accounts:
                line = _account_label(account) if account.xuid is None else f"{account.xuid}\t{account.gamertag}"
                _print_status(line, flush=True)
            return

        if args.command == "join":
            selected = _select_account(accounts, args.xuid, name=args.name)
            xuid = None
            legacy_account_id = None
            if args.xuid is not None or args.name is not None or len(accounts) > 1:
                xuid = selected.xuid
                if xuid is None:
                    legacy_account_id = selected.home_account_id

            async def authorize(public_key: str):
                return await _authorize_account(
                    public_key, path, xuid=xuid, legacy_account_id=legacy_account_id,
                )

            await _join(args, authorize, selected_account=selected)
            return

        if args.account_command == "skin":
            verified = tuple(account for account in accounts if account.xuid is not None)
            if not verified:
                raise _CommandError("No verified Xbox accounts are remembered. Run python -m endpilot account add.")
            selected = _select_account(verified, args.xuid, name=args.name)
            binding = None
            if args.skin_command == "set":
                try:
                    skin_path = args.skin.resolve()
                except (OSError, ValueError, RuntimeError):
                    raise _CommandError("Could not resolve the skin PNG path. Check the path and try again.") from None
                _load_skin(skin_path, args.model)
                binding = _cache.SkinBinding(str(skin_path), args.model)
            async with xbox._locked_cache(path):
                updated = xbox._cache_operation(
                    _cache.set_account_skin, path, selected.home_account_id, selected.xuid, binding,
                )
            if not updated:
                raise _CommandError("The selected Xbox account is no longer remembered. Run account list and try again.")
            action = "Set" if binding is not None else "Cleared"
            _print_status(f"{action} the skin for {_account_label(selected)}.", flush=True)
            return

        selected = _select_account(accounts, args.xuid, allow_name=False)
        if selected is None:
            _print_status("No remembered Xbox accounts.", flush=True)
            return
        async with xbox._locked_cache(path):
            current = xbox._cache_operation(_cache.load_accounts, path)
            for account in current:
                if account.home_account_id == selected.home_account_id and account.xuid == selected.xuid:
                    xbox._cache_operation(_cache.delete_account, path, account.home_account_id)
                    _print_status(f"Forgot {_account_label(account)}.", flush=True)
                    return
        _print_status("The selected Xbox account is no longer remembered.", flush=True)
    except xbox.XboxReauthenticationRequired:
        raise _CommandError("Xbox authorization is required. Run python -m endpilot account add in an interactive "
                            "terminal.") from None
    except xbox.XboxAccountCacheError:
        raise _CommandError("The local Xbox account cache is unreadable, invalid, or inaccessible. "
                            "Check the cache and its permissions.") from None
    except xbox.XboxAuthorizationUnavailable:
        raise _CommandError("Xbox authorization is temporarily unavailable. Check your network and try again.") from None


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "join":
        try:
            address = _parse_join_address(args.address)
        except ValueError:
            parser.exit(2, "Invalid server address. See join --help.\n")
        if not args.xbox:
            if args.xuid is not None:
                parser.exit(2, "--xuid requires --xbox for a join.\n")
            if args.name is not None:
                parser.exit(2, "--name requires --xbox for a join.\n")
        if args.skin_model is not None and args.skin is None:
            parser.exit(2, "--skin-model requires --skin.\n")
        if not isfinite(args.hold) or args.hold < 0:
            parser.exit(2, "--hold requires a finite, nonnegative duration.\n")
        if args.server_key_sha256 is not None:
            if address.scheme == "raknet":
                parser.exit(2, "--server-key-sha256 requires a NetherNet address.\n")
            if not re.fullmatch(r"[0-9a-f]{64}", args.server_key_sha256):
                parser.exit(2, "--server-key-sha256 requires a lowercase SHA-256 digest.\n")
    xbox_command = args.command == "account" or args.xbox
    try:
        asyncio.run(_run_xbox(args) if xbox_command else _join(args), debug=False)
    except (KeyboardInterrupt, asyncio.CancelledError):
        _print_status("Stopped.", file=sys.stderr)
        return 130
    except LoginRefused as error:
        _print_status(_login_refusal_message(error), file=sys.stderr)
        return 1
    except _CommandError as error:
        _print_status(str(error), file=sys.stderr)
        return 1
    except (Exception, SystemExit) as error:
        if isinstance(error, TimeoutError):
            message = "Join timed out." if args.command == "join" else "Xbox account authorization timed out."
        elif args.command == "join" and (nethernet_message := _nethernet_failure_message(error)) is not None:
            message = nethernet_message
        elif not xbox_command:
            message = "Connection failed. Check the server address, settings and transport dependencies."
        elif args.command == "join":
            message = "Xbox sign-in or connection failed. Check the Xbox extra and server settings."
        else:
            message = "Xbox account command failed. Check the Xbox extra and local account cache."
        _print_status(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
