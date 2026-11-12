from __future__ import annotations

from typing import Any, Dict, Type

from .base import Backend, BackendError, ProgressCallback, RunResult
from .claude import ClaudeBackend
from .codex import CodexBackend
from .live import LiveEvents, LiveSession

BACKENDS: Dict[str, Type[Backend]] = {
    ClaudeBackend.name: ClaudeBackend,
    CodexBackend.name: CodexBackend,
}


def create_backend(name: str, config: Any) -> Backend:
    try:
        return BACKENDS[name](config)
    except KeyError:
        raise BackendError("unknown backend %r (available: %s)" % (name, ", ".join(sorted(BACKENDS)))) from None


__all__ = [
    "BACKENDS", "Backend", "BackendError", "LiveEvents", "LiveSession", "ProgressCallback", "RunResult", "create_backend",
]
