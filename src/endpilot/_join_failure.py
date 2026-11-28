"""Safe diagnostics for a failed candidate without replacing its exception."""

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


_join_stage: ContextVar[str | None] = ContextVar("join_stage", default=None)


def _set_join_stage(stage: str) -> None:
    if _join_stage.get() is not None:
        _join_stage.set(stage)


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
