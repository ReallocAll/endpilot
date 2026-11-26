"""Private, application-scoped storage for remembered Xbox accounts."""

import asyncio
from collections.abc import Iterable
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, field, replace
import errno
import json
import os
from pathlib import Path
import re
import stat
import sys
from uuid import UUID, uuid4

if os.name == "nt":
    import msvcrt

    from . import _windows_cache
else:
    import fcntl


def cache_path(client_id: str) -> Path:
    """Return the current user's cache path without contacting the filesystem."""
    try:
        app = str(UUID(client_id))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Xbox cache requires a valid application UUID") from None
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", ""))
        if not base.is_absolute():
            raise ValueError("LOCALAPPDATA must identify the current user's absolute local application data path")
        base = base / "EndPilot"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "EndPilot"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", ""))
        if not base.is_absolute():
            base = Path.home() / ".local" / "state"
        base = base / "endpilot"
    return base / "auth" / f"{app}.json"


@contextmanager
def _directory(path: Path, *, create: bool = False):
    path = path.absolute()
    if os.name == "nt":
        with _windows_cache.open_directory(path, create=create):
            yield None
        return

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as opened:
        directory = os.open(path.anchor, flags)
        opened.callback(os.close, directory)
        for name in path.parts[1:]:
            try:
                child = os.open(name, flags, dir_fd=directory)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(name, 0o700, dir_fd=directory)
                except FileExistsError:
                    pass  # A concurrent user created it first.
                child = os.open(name, flags, dir_fd=directory)
            opened.callback(os.close, child)
            directory = child
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("Xbox cache directory must be private to the current user")
        yield directory


def _open_file(path: Path, directory: int | None, flags: int = os.O_RDONLY) -> int:
    if os.name == "nt":
        return _windows_cache.open_file(path, flags)
    descriptor = os.open(path.name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise PermissionError("Xbox cache file must be an ordinary file private to the current user")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _leaf(path: Path, directory: int | None) -> str | Path:
    return path if directory is None else path.name


def _file_exists(path: Path, directory: int | None) -> bool:
    try:
        descriptor = _open_file(path, directory)
    except FileNotFoundError:
        return False
    os.close(descriptor)
    return True


def cache_exists(path: Path) -> bool:
    """Check for a private cache file. Only a missing file or directory is absent."""
    try:
        with _directory(path.parent) as directory:
            return _file_exists(path, directory)
    except FileNotFoundError:
        return False


def _try_lock(descriptor: int) -> bool:
    try:
        if os.name == "nt":
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise
    return True


@asynccontextmanager
async def locked_cache(path: Path):
    """Hold the application's stable lock through a complete account operation."""
    lock_path = path.with_suffix(".lock")
    with _directory(path.parent, create=True) as directory:
        descriptor = _open_file(lock_path, directory, os.O_CREAT | os.O_RDWR)
        try:
            while not _try_lock(descriptor):
                await asyncio.sleep(0.05)
            yield
        finally:
            # Closing releases the OS lock. Keeping the file avoids split locks.
            os.close(descriptor)


def _valid_xuid(xuid: object) -> bool:
    return isinstance(xuid, str) and re.fullmatch(r"[1-9][0-9]{0,18}", xuid) is not None and int(xuid) < 2**63


def _validate_profile(xuid: object, gamertag: object) -> None:
    if (not _valid_xuid(xuid) or not isinstance(gamertag, str) or not gamertag
            or gamertag != gamertag.strip() or not gamertag.isprintable()):
        raise ValueError("Xbox account requires a valid XUID and gamertag")


@dataclass(frozen=True, slots=True)
class SkinBinding:
    """The absolute local PNG path and classic skin model saved for an account."""

    path: str
    model: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or "\0" in self.path or not Path(self.path).is_absolute():
            raise ValueError("Xbox account skin requires an absolute path")
        if not isinstance(self.model, str) or self.model not in ("wide", "slim"):
            raise ValueError("Xbox account skin model must be 'wide' or 'slim'")


@dataclass(frozen=True, slots=True)
class AccountRecord:
    """One Microsoft account and its last verified Xbox profile, if available."""

    home_account_id: str
    serialized_cache: str = field(repr=False)
    xuid: str | None = None
    gamertag: str | None = None
    skin: SkinBinding | None = None

    def __post_init__(self):
        if (not isinstance(self.home_account_id, str) or not self.home_account_id
                or not isinstance(self.serialized_cache, str) or not self.serialized_cache):
            raise ValueError("Xbox cache requires an account ID and serialized cache")
        if self.skin is not None and (self.xuid is None or not isinstance(self.skin, SkinBinding)):
            raise ValueError("Xbox account skin requires a verified account and skin binding")
        if self.xuid is None and self.gamertag is None:
            return
        _validate_profile(self.xuid, self.gamertag)


def _read_cache(path: Path) -> tuple[int | None, dict[str, AccountRecord]]:
    try:
        with _directory(path.parent) as directory:
            with os.fdopen(_open_file(path, directory), encoding="utf-8") as source:
                data = json.load(source)

        if not isinstance(data, dict) or type(data.get("version")) is not int:
            raise ValueError
        version = data["version"]
        if version == 1:
            account = AccountRecord(data.get("home_account_id"), data.get("serialized_cache"))
            return version, {account.home_account_id: account}
        if version != 2 or not isinstance(data.get("accounts"), list) or not data["accounts"]:
            raise ValueError

        accounts = {}
        xuids = set()
        for entry in data["accounts"]:
            if not isinstance(entry, dict):
                raise ValueError
            skin = None
            if "skin" in entry:
                binding = entry["skin"]
                if not isinstance(binding, dict) or binding.keys() != {"path", "model"}:
                    raise ValueError
                skin = SkinBinding(binding["path"], binding["model"])
            account = AccountRecord(entry["home_account_id"], entry["serialized_cache"],
                                    entry["xuid"], entry["gamertag"], skin)
            if account.home_account_id in accounts or account.xuid in xuids:
                raise ValueError
            accounts[account.home_account_id] = account
            if account.xuid is not None:
                xuids.add(account.xuid)
        return version, accounts
    except FileNotFoundError:
        return None, {}
    except (KeyError, ValueError, RecursionError):
        raise ValueError("Xbox cache file is invalid") from None


def load_accounts(path: Path) -> tuple[AccountRecord, ...]:
    """Read a snapshot without creating or upgrading the cache.

    Use the application lock when coordinating with other cache operations.
    """
    _, accounts = _read_cache(path)
    return tuple(accounts.values())


def load_cache(path: Path) -> tuple[str, str] | None:
    """Read the single MSAL account under the lock for the legacy SDK path."""
    accounts = load_accounts(path)
    if len(accounts) > 1:
        raise ValueError("Xbox cache contains multiple accounts and requires an account selection")
    if accounts:
        account, = accounts
        return account.home_account_id, account.serialized_cache
    return None


def _verified_cache(path: Path, account: AccountRecord) -> AccountRecord:
    from . import _selected_cache

    client_id = str(UUID(path.stem))
    serialized = _selected_cache(account.serialized_cache, client_id, account.home_account_id)
    state = json.loads(serialized)
    if not state.get("Account") or not any(entry["secret"] for entry in state.get("RefreshToken", {}).values()):
        raise ValueError("Xbox cache requires a renewable MSAL cache for this account")
    return replace(account, serialized_cache=serialized)


def upsert_account(path: Path, account: AccountRecord) -> None:
    """Store a service-verified account using the caller's application lock."""
    if account.xuid is None:
        raise ValueError("Xbox cache requires a verified Xbox profile")
    _, accounts = _read_cache(path)
    previous = accounts.get(account.home_account_id)
    if previous is not None and previous.xuid is not None:
        if previous.xuid != account.xuid:
            raise ValueError("Xbox cache account already has a different XUID")
        account = replace(account, skin=previous.skin)
    if any(saved.xuid == account.xuid and saved.home_account_id != account.home_account_id
           for saved in accounts.values()):
        raise ValueError("Xbox cache XUID already belongs to a different account")
    accounts[account.home_account_id] = _verified_cache(path, account)
    _write_accounts(path, accounts.values())


def set_account_skin(path: Path, home_account_id: str, xuid: str, skin: SkinBinding | None) -> bool:
    """Set or clear a skin under the caller's lock. Return whether both IDs matched."""
    _, accounts = _read_cache(path)
    account = accounts.get(home_account_id)
    if account is None or account.xuid is None or account.xuid != xuid:
        return False
    if account.skin != skin:
        accounts[home_account_id] = replace(account, skin=skin)
        _write_accounts(path, accounts.values())
    return True


def save_cache(path: Path, home_account_id: str, serialized_cache: str) -> None:
    """Save legacy data or renew an existing V2 account under the lock."""
    account = AccountRecord(home_account_id, serialized_cache)
    version, accounts = _read_cache(path)
    if version == 2:
        previous = accounts.get(home_account_id)
        if previous is None:
            raise ValueError("Xbox cache update requires an existing account")
        if previous.xuid is not None:
            account = _verified_cache(path, replace(previous, serialized_cache=serialized_cache))
        accounts[home_account_id] = account
        _write_accounts(path, accounts.values())
        return
    data = {"version": 1, "home_account_id": home_account_id, "serialized_cache": serialized_cache}
    _write_cache(path, data)


def _write_accounts(path: Path, accounts: Iterable[AccountRecord]) -> None:
    entries = []
    for account in accounts:
        entry = asdict(account)
        if account.skin is None:
            del entry["skin"]
        entries.append(entry)
    _write_cache(path, {"version": 2, "accounts": entries})


def _write_cache(path: Path, data: dict) -> None:
    """Atomically replace the account data with a fully written private file."""
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with _directory(path.parent) as directory:
        _file_exists(path, directory)  # Refuse an unsafe existing target before writing.
        descriptor = _open_file(temporary, directory, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(data, output, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(_leaf(temporary, directory), _leaf(path, directory), src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(_leaf(temporary, directory), dir_fd=directory)
            except FileNotFoundError:
                pass


def delete_account(path: Path, home_account_id: str) -> bool:
    """Forget one Microsoft account under the caller's application lock."""
    _, accounts = _read_cache(path)
    if accounts.pop(home_account_id, None) is None:
        return False
    with _directory(path.parent) as directory:
        # Keep the committed record available for retry if an orphan cannot be removed.
        for target in _orphaned_writes(path, directory):
            _unlink_if_present(target, directory)
        if not accounts:
            _unlink_if_present(path, directory)
            return True
    _write_accounts(path, accounts.values())
    return True


def delete_cache(path: Path) -> bool:
    """Forget this application's cache and orphaned writes under its lock."""
    removed = False
    try:
        with _directory(path.parent) as directory:
            for target in (path, *_orphaned_writes(path, directory)):
                removed |= _unlink_if_present(target, directory)
    except FileNotFoundError:
        pass
    return removed


def _orphaned_writes(path: Path, directory: int | None) -> list[Path]:
    prefix = re.compile(rf"\.{re.escape(path.name)}\.[0-9a-f]{{32}}\.tmp")
    names = os.listdir(path.parent if directory is None else directory)
    return [path.with_name(name) for name in names if prefix.fullmatch(name)]


def _unlink_if_present(path: Path, directory: int | None) -> bool:
    if not _file_exists(path, directory):
        return False
    os.unlink(_leaf(path, directory), dir_fd=directory)
    return True
