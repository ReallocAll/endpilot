"""Safe diagnostics for a failed candidate without replacing its exception."""

import asyncio
from collections.abc import Awaitable
from contextvars import ContextVar
from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class JoinFailureDetails:
    """The known stage, classification, and optional machine code of a failed join."""

    transport: str | None
    stage: str
    reason: str
    code_source: str | None = None
    code: str | int | None = None


@dataclass(slots=True)
class _JoinProgress:
    stage: str | None = "transport_connect"
    transport: object | None = None
    cleanup_failed: bool = False


_join_progress: ContextVar[_JoinProgress | None] = ContextVar("join_progress", default=None)


def _set_join_stage(stage: str) -> None:
    progress = _join_progress.get()
    if progress is not None and progress.stage is not None:
        progress.stage = stage


def _mark_candidate_cleanup_failed() -> None:
    progress = _join_progress.get()
    if progress is not None and progress.stage is not None:
        progress.cleanup_failed = True


def _note_join_cleanup_failure(primary: BaseException, cleanup_error: BaseException, activity: str) -> None:
    if cleanup_error is primary:
        return
    primary.add_note(f"{activity}: {cleanup_error!r}")
    for note in getattr(cleanup_error, "__notes__", ()):
        primary.add_note(note)


async def _cleanup_failed_candidate(cleanup: Awaitable[None], primary: BaseException,
                                    activity: str) -> BaseException | None:
    try:
        await cleanup
    except (Exception, asyncio.CancelledError) as cleanup_error:
        _mark_candidate_cleanup_failed()
        if isinstance(cleanup_error, asyncio.CancelledError) and not isinstance(primary, asyncio.CancelledError):
            raise
        _note_join_cleanup_failure(primary, cleanup_error, activity)
        return cleanup_error
    return None


def _nethernet_transport(url: str) -> str | None:
    scheme = url.partition(":")[0].lower() if isinstance(url, str) else None
    return {"https": "nethernet_https", "http": "nethernet_http"}.get(scheme)


def _symbol_code(value) -> str | None:
    if type(value) is str and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", value):
        return value
    return None


def _os_error_code(error: OSError) -> tuple[str | None, int | None]:
    for source in ("winerror", "errno"):
        code = getattr(error, source, None)
        if type(code) is int:
            return source, code
    return None, None


def _record_join_failure(
    error: Exception, transport: str | None, stage: str, reason: str,
    code_source: str | None = None, code: str | int | None = None,
) -> None:
    error.__dict__["join_failure"] = JoinFailureDetails(
        transport, stage, reason, code_source if code is not None else None, code,
    )


def _record_connection_failure(error: Exception, transport: str | None, stage: str) -> None:
    if isinstance(error, TimeoutError):
        _record_join_failure(error, transport, stage, "timeout", *_os_error_code(error))
    elif isinstance(error, OSError):
        _record_join_failure(error, transport, stage, "os_error", *_os_error_code(error))
    else:
        _record_join_failure(error, transport, stage, "connection_error")
