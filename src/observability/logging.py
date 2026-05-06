"""Structured JSON logging with per-request correlation context.

Why a ContextVar instead of stuffing everything through `extra=`?

Every log line in this pipeline needs the same five identifiers
(`correlation_id`, `interaction_id`, `customer_id`, `campaign_id`,
`step`). Threading them through every function signature is noise
that the inline comments explicitly call out as a problem. A
ContextVar lets the endpoint set the context once and every nested
log line inherit it, including from inside Celery worker tasks that
re-enter the event loop.

The README requires that an on-call engineer be able to grep all
logs for a single interaction three days after the fact. JSON output
+ a stable schema (every event carries the same fields) is what
makes that grep cheap.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from typing import Any, Mapping, MutableMapping, Optional

# Context variables — set by the endpoint or worker entrypoint and
# inherited by every log line emitted within that request/task.
_correlation_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "correlation_id", default=None
)
_interaction_context: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "interaction_context", default={}
)


# Standard `LogRecord` attributes we should not duplicate into the
# JSON body. Anything outside this set that ends up on the record
# (because the caller passed `extra={...}`) gets serialised.
_STANDARD_LOG_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


class StructuredJsonFormatter(logging.Formatter):
    """Emit one JSON object per log record.

    Includes the standard log fields plus any context bound via
    `bind_correlation_id` / `bind_interaction_context` and any
    free-form fields passed through `extra=`.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: MutableMapping[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }

        cid = _correlation_id.get()
        if cid:
            payload["correlation_id"] = cid

        ctx = _interaction_context.get()
        if ctx:
            payload.update(ctx)

        for key, value in record.__dict__.items():
            if key in _STANDARD_LOG_ATTRS or key.startswith("_"):
                continue
            payload[key] = _make_serialisable(value)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=_make_serialisable, ensure_ascii=False)


def _make_serialisable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_make_serialisable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _make_serialisable(v) for k, v in value.items()}
    return repr(value)


_configured = False


def configure_structured_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger.

    Idempotent: safe to call from every entrypoint (FastAPI startup,
    Celery worker init, test setup) without stacking handlers.
    """
    global _configured
    if _configured:
        return

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(StructuredJsonFormatter())

    root = logging.getLogger()
    # Replace any preexisting handlers so we get exactly one JSON
    # output stream — important so `pytest -s` and Celery's default
    # logger don't double-emit.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger that will emit through the structured formatter.

    Calling configure_structured_logging() before first use is
    recommended; not doing so falls back to Python's default
    formatter which is still functional but loses the JSON structure.
    """
    return logging.getLogger(name)


def bind_correlation_id(correlation_id: Optional[str] = None) -> str:
    """Set or generate a correlation_id for the current async context.

    Returns the id so callers (e.g., the FastAPI endpoint) can echo
    it back in the response payload.
    """
    cid = correlation_id or uuid.uuid4().hex
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> Optional[str]:
    return _correlation_id.get()


def bind_interaction_context(**fields: Any) -> None:
    """Merge the given fields into the inherited interaction context.

    Typical use: `bind_interaction_context(interaction_id=..., customer_id=...)`
    at the start of a worker task. Subsequent log calls within the
    same context will carry these fields automatically.
    """
    current = dict(_interaction_context.get() or {})
    current.update(fields)
    _interaction_context.set(current)


def clear_context() -> None:
    """Reset both correlation_id and interaction context.

    Workers that pool tasks should call this between tasks so context
    from a finished task does not leak into the next one.
    """
    _correlation_id.set(None)
    _interaction_context.set({})
