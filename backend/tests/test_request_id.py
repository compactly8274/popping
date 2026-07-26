"""Unit tests for ``app.request_id`` (the per-request id propagation
module added in slice 9e — request-id middleware).

These are intentionally pure unit tests: no DB, no HTTP, no
asyncio. The module is 95 lines and contains the entire
trace-correlation contract — if any of these regress, the
operator loses the ability to correlate a user-reported
issue with the server logs. Cheap to run, easy to reason
about, and acts as a regression guard for the cap, the RNG
shape, and the clear semantics.

The integration story is covered by ``app/main.py``'s
``_request_id`` middleware (not unit-tested here because
end-to-end middleware tests would require a TestClient and
the full app stack; the unit tests below verify the contract
that the middleware depends on).

Marked ``@pytest.mark.no_db`` so the conftest's session-scope
``_schema`` fixture skips the Postgres connect — these tests
don't need a DB and the conftest's autouse fixtures would
otherwise force a DB round-trip on every run. See
``_NO_DB_MARKERS`` in ``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio
import secrets

import pytest

from app import request_id
from app.request_id import (
    bind_request_id,
    clear_request_id,
    current_request_id,
)

pytestmark = pytest.mark.no_db


@pytest.fixture(autouse=True)
def _reset_request_id():
    """Each test starts with no bound request id, even if a
    prior test left one bound (e.g. on a test that deliberately
    exercises the bind/current/clear round-trip).
    """
    clear_request_id()
    yield
    clear_request_id()


def test_current_request_id_returns_none_when_unbound():
    """Outside any request (startup, scheduler tick, background
    task), ``current_request_id()`` returns ``None`` rather than
    raising ``LookupError``. The middleware depends on this —
    a ContextVar that raises on lookup would crash the logging
    filter for every scheduler tick.
    """
    assert current_request_id() is None


def test_bind_request_id_echoes_non_empty_string():
    """When the client supplies an ``X-Request-Id``, ``bind_request_id``
    echoes it verbatim. The middleware reads this from the
    request header; the unit test exercises the helper directly.
    """
    rid = bind_request_id("client-supplied-trace-id")
    assert rid == "client-supplied-trace-id"
    assert current_request_id() == "client-supplied-trace-id"


def test_bind_request_id_generates_12_hex_chars_when_none():
    """When the client doesn't supply an id, ``bind_request_id``
    generates a 12-char hex token via ``secrets.token_hex(6)``.

    Shape matters here: 12 chars is short enough to read back
    over the phone and long enough to be unique within a day's
    traffic without coordination. All hex (``0-9a-f``) means it
    survives a copy/paste through anything that mangles URLs.
    """
    rid = bind_request_id(None)
    assert len(rid) == 12
    assert all(c in "0123456789abcdef" for c in rid), \
        f"generated id must be hex: {rid!r}"


def test_bind_request_id_generated_ids_are_unique():
    """Two consecutive ``bind_request_id(None)`` calls produce
    different values. The OS RNG (``secrets.token_hex``) is the
    source — if someone replaces it with a non-RNG source (e.g.
    ``random.randint``), the OS-RNG guarantee is lost and an
    attacker could predict the next request's id from the
    previous one. This test catches that regression.
    """
    a = bind_request_id(None)
    b = bind_request_id(None)
    assert a != b
    # Sample size 50 to detect patterns in the random source.
    seen = {bind_request_id(None) for _ in range(50)}
    assert len(seen) == 50, f"expected 50 unique ids, got {len(seen)}"


def test_bind_request_id_uses_secrets_token_hex():
    """Pinned: ``bind_request_id`` uses ``secrets.token_hex(6)``
    (the OS-RNG-backed hex generator), not ``random.hex`` or
    similar. If a future refactor swaps the source, this test
    fails and forces a review of the security implication
    (predictability of the next request id).

    Implemented by patching the module attribute the function
    would call and checking it gets invoked. Doesn't pin the
    *exact* call (we don't care whether it's token_hex(6) or
    token_bytes(6).hex()), just that the secrets module is the
    source.
    """
    bound = []

    def _spy_token_hex(n: int) -> str:
        bound.append(n)
        return "f" * (n * 2)

    original = request_id.secrets.token_hex
    request_id.secrets.token_hex = _spy_token_hex
    try:
        rid = bind_request_id(None)
    finally:
        request_id.secrets.token_hex = original
    assert rid == "f" * 12
    assert bound == [6], f"expected token_hex(6) call, got {bound!r}"


def test_bind_request_id_truncates_oversized_client_supplied():
    """A client-supplied ``X-Request-Id`` longer than 128 chars
    is truncated to 128 chars. Defensive cap — a malicious or
    buggy client could send megabytes, which would balloon
    every log line that includes the field (the log format
    string is ``%(name)s[%(request_id)s]``). 128 chars is more
    than enough for any legitimate id (UUID4 hex is 32; a
    96-char free-form identifier is already plenty).
    """
    big = "x" * 500
    rid = bind_request_id(big)
    assert len(rid) == 128
    assert rid == "x" * 128


def test_bind_request_id_accepts_exactly_128_char_string():
    """Boundary: a 128-char string is preserved verbatim (cap is
    inclusive, not exclusive). Documents the contract so a
    future "off by one" in the cap doesn't silently change
    behavior for legitimate users.
    """
    exact = "a" * 128
    rid = bind_request_id(exact)
    assert rid == exact


def test_bind_request_id_treats_empty_string_as_no_supplied():
    """An empty-string ``X-Request-Id`` is treated as 'no supplied id'
    and a fresh 12-char token is generated. This matches how
    ``request.headers.get`` returns an empty string (not ``None``)
    for an explicitly-empty header, and prevents a regression
    where an empty id would be bound and propagated through the
    logs (giving the operator no id to grep for).
    """
    rid = bind_request_id("")
    assert len(rid) == 12
    assert all(c in "0123456789abcdef" for c in rid)


def test_clear_request_id_unbinds_for_follow_up_task():
    """After ``clear_request_id``, ``current_request_id`` returns
    ``None``. The middleware depends on this in its ``finally``
    block so a background task scheduled by a request handler
    (``BackgroundTasks`` dep, fire-and-forget
    ``asyncio.create_task``) doesn't inherit the request id of
    the now-completed request. Without the clear, log lines
    from a follow-up task would be tagged with a stale id that
    no longer matches any in-flight request — misleading for
    the operator.
    """
    bind_request_id("abc123")
    assert current_request_id() == "abc123"
    clear_request_id()
    assert current_request_id() is None


def test_bind_request_id_after_clear_binds_new_value():
    """``bind_request_id`` after a ``clear_request_id`` binds a
    fresh value (the new request's id), not the old one. The
    sequence bind → clear → bind should reflect the new bind.
    """
    bind_request_id("first-request")
    clear_request_id()
    assert current_request_id() is None
    bind_request_id("second-request")
    assert current_request_id() == "second-request"


def test_current_request_id_isolated_between_asyncio_tasks():
    """``asyncio`` task isolation: each task gets its own copy of
    the ContextVar. ``bind_request_id`` in task A doesn't leak
    into task B's ``current_request_id()``. The middleware
    depends on this — a request handler running on a worker
    task sees the id the middleware bound; a background task
    spawned by a different request sees its own id (or ``None``
    if no request is in flight for that task).
    """
    captured_a = []
    captured_b = []

    async def task_a():
        bind_request_id("id-for-task-a")
        captured_a.append(current_request_id())

    async def task_b():
        # Run before task A — task B sees no bound id
        captured_b.append(current_request_id())

    async def main():
        await asyncio.gather(task_a(), task_b())

    asyncio.run(main())

    # Task A bound its own id; task B saw None (its own copy).
    assert captured_a == ["id-for-task-a"]
    assert captured_b == [None]


def test_module_does_not_import_heavy_dependencies():
    """``app.request_id`` is imported by ``app.main`` at module
    load time (the middleware binds via ``bind_request_id``).
    If a future refactor accidentally pulls in a heavy
    dependency (torch, sentence-transformers, etc.) at this
    module's top level, the FastAPI app's import cost would
    skyrocket. This test asserts the module's top-level imports
    are minimal — only ``secrets``, the stdlib ``contextvars``,
    and ``typing``.

    Pinned by inspecting ``__dict__``'s import-related entries.
    Anything beyond ``secrets``, ``contextvars``, ``typing``,
    ``__future__``, and stdlib is a regression.
    """
    module = request_id
    # The module-level names that come from explicit imports:
    # ``secrets`` (the function-name reference) and the
    # ContextVar class instance. Anything else suggests
    # someone added a heavy import.
    # The simplest check: the module file's top-level imports.
    import ast
    with open(module.__file__) as f:
        tree = ast.parse(f.read())
    top_imports = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            top_imports.add(node.module.split(".")[0])
    # The whitelist: stdlib + nothing project-specific.
    # ``secrets``, ``contextvars``, ``__future__``, ``typing``.
    allowed = {"secrets", "contextvars", "__future__", "typing"}
    extras = top_imports - allowed
    assert not extras, \
        f"app.request_id should only import stdlib; found extras: {extras}"


# Sanity check that ``secrets.token_hex(6)`` is the documented
# shape (12 hex chars). Pins the generated id's length across
# pytest collection — if someone changes ``secrets.token_hex(6)``
# to ``secrets.token_hex(8)``, the tests that depend on
# ``len(generated) == 12`` will start failing and force a
# review of any docs that say "12-char request id".
def test_secrets_token_hex_6_is_12_chars():
    """Sanity: ``secrets.token_hex(6)`` returns 12 hex chars.
    This is what the production log format expects when an
    unbranded id is generated.
    """
    sample = secrets.token_hex(6)
    assert len(sample) == 12
    assert all(c in "0123456789abcdef" for c in sample)