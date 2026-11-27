"""Diagnostics for inbound data the SDK could recover from."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecoverableError:
    """A failed parse handled by an SDK component, such as ``blocks`` or ``titles``."""

    source: str
    packet_id: int
    error_type: str
    message: str
