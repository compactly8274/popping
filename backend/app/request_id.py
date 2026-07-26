"""Per-request id propagation.

A small module that:

  - Exposes a ``contextvars.ContextVar`` named ``request_id`` so any
    code running inside a request handler (route function,
    dependency, ``Depends``, background task launched via
    ``BackgroundTasks``) can read it without having to thread the
    value through every call signature.

  - Provides a :func:`bind_request_id` helper that sets the var
    and returns the value actually bound (the caller-supplied
    value if non-empty, otherwise a freshly-generated 12-char hex).

The contextvar approach is intentional. ``logging`` already
supports per-thread and per-task context via :class:`contextvars`
naturally — ``asyncio`` tasks each get their own copy of the
context, so a request handler running on a worker task sees the
value the middleware bound, and a background task that was
launched *before* the request middleware fired (rare, but
possible — e.g. a startup cron) sees ``None``. The standard
``logging`` formatter can pull the current value via
:func:`contextvars.copy_context().get`, but we instead attach the
value at log-call time via a small :class:`logging.Filter` (see
``app.main._RequestIdFilter``), which is the cleanest integration
point — no formatter changes required.
"""

from __future__ import annotations

import secrets
from contextvars import ContextVar

# ``ContextVar[str | None]`` rather than ``ContextVar[str]`` so the
# pre-request path (background tasks launched outside a request
# context) sees ``None`` rather than raising ``LookupError`` —
# the formatter uses that to omit the ``[req=...]`` segment.
_request_id_var: ContextVar[str | None] = ContextVar("popping_request_id", default=None)


def bind_request_id(supplied: str | None) -> str:
    """Bind ``supplied`` as the current request's id, returning the
    value actually bound.

    If ``supplied`` is non-empty (and short enough to be safe to
    echo back in a log line — capped at 128 chars), it's used
    verbatim. Otherwise a 12-char hex token is generated.

    The value is also returned so the caller (the middleware)
    can put it on the response header without having to re-read
    the contextvar.
    """
    if supplied:
        # Defensive cap. A legitimate request-id from the frontend
        # is short; a malicious or buggy client could send
        # megabytes, which would balloon every log line that
        # includes it. 128 chars is more than enough for any real
        # use (UUID4 hex is 32; a 96-char free-form identifier is
        # already plenty).
        chosen = supplied[:128]
    else:
        # ``secrets.token_hex(6)`` is 12 hex chars — short enough
        # to read back over the phone ("check for the line with
        # req=ab12cd34ef56"), large enough to be unique within a
        # day's traffic without coordination. ``token_hex`` uses
        # the OS RNG so a request-id can't be predicted from
        # the previous one.
        chosen = secrets.token_hex(6)
    _request_id_var.set(chosen)
    return chosen


def current_request_id() -> str | None:
    """Return the request id bound for the current task, or ``None``
    if no request is in flight (e.g. a scheduler tick, a startup
    hook, a background task launched outside any request).
    """
    return _request_id_var.get()


def clear_request_id() -> None:
    """Unbind the current request's id.

    Called by the request-id middleware on the way out so a
    follow-up task scheduled by a route handler (a
    ``BackgroundTasks`` dependency, an
    ``asyncio.create_task`` fire-and-forget) starts with a
    clean context rather than inheriting the request id of
    the now-completed request. Without this, a background
    task that runs *after* the request handler returns
    would log lines tagged with a request id that no longer
    matches any in-flight request, which is misleading.
    """
    _request_id_var.set(None)