"""Slice 22 — ``Drawer.tsx`` aliveRef useEffect deps fix.

The previous code:

    const aliveRef = useRef(true)
    useEffect(() => {
      aliveRef.current = true
      return () => {
        aliveRef.current = false
      }
    })

— had NO deps array, which means the effect ran on every render. The
commit sequence per re-render was:

    1. aliveRef.current = true       (effect body)
    2. aliveRef.current = false      (cleanup)
    3. aliveRef.current = true       (effect body, re-entered)

An async ``.then`` callback that landed during step 2 read stale
``false`` and silently dropped its ``setState``. The drawer's source
list would stay stale until the next user action triggered a re-fetch.

Fix: add ``[]`` deps so the effect runs once on mount. Drawer is
mounted unconditionally by App (visibility is gated by the ``open``
prop, not by mount/unmount), so the original "alive = true on every
(re)open" intent was never what the code did.

This test file is frontend-shape only — Python regex checks on the
source. The real verification is the TypeScript build in CI.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

DRAWER = Path("/tmp/popping-review/frontend/src/components/Drawer.tsx")


@pytest.mark.no_db
def test_drawer_alive_ref_effect_has_empty_deps():
    """The aliveRef useEffect MUST have ``[]`` deps.

    Without this, the effect re-runs on every render and the
    cleanup→rerun sequence sets aliveRef.current = false between
    commits. Async ``.then`` callbacks that land during that window
    read false and drop their setState.
    """
    src = DRAWER.read_text()
    # Find the aliveRef block
    m = re.search(
        r"const aliveRef = useRef\(true\).*?const refetchSources",
        src,
        re.DOTALL,
    )
    assert m, "Couldn't locate aliveRef declaration in Drawer.tsx"
    block = m.group(0)
    assert "useEffect" in block, "Expected a useEffect for the aliveRef lifecycle"
    # Must end with `}, [])` not `})` (no-deps form)
    assert re.search(r"\},\s*\[\]\)", block), (
        "The aliveRef useEffect must have ``[]`` deps so it runs only on "
        "mount. A missing deps array means the effect runs on every "
        "render and the ref flickers false between commits, dropping "
        "async .then setState calls."
    )


@pytest.mark.no_db
def test_drawer_alive_ref_still_set_true_on_mount():
    """Sanity: the fix must preserve the on-mount set-true behavior."""
    src = DRAWER.read_text()
    m = re.search(
        r"const aliveRef = useRef\(true\).*?const refetchSources",
        src,
        re.DOTALL,
    )
    block = m.group(0)
    # The mount-time set-true must still be there
    assert "aliveRef.current = true" in block, (
        "Mount-time aliveRef.current = true must be preserved"
    )
    # And the unmount cleanup must still set false
    assert "aliveRef.current = false" in block, (
        "Unmount cleanup must still set aliveRef.current = false"
    )


@pytest.mark.no_db
def test_drawer_alive_ref_no_reopen_intent_comment():
    """The misleading "(re)open as a fresh alive generation" comment is gone.

    The original comment claimed the effect ran on every (re)open, but
    Drawer is mounted unconditionally by App.tsx — there is no
    mount/unmount on open/close. The comment misled future readers into
    thinking the no-deps effect was intentional. Slice 22 replaces the
    comment with the actual correct invariant (mount/unmount only).
    """
    src = DRAWER.read_text()
    assert "Mark every (re)open as a fresh" not in src, (
        "Old comment claiming 'every (re)open' should be removed — "
        "the effect no longer runs on every render."
    )
    # New comment should mention mount/unmount explicitly (in the
    # preceding comment block, which lives BEFORE the useRef line
    # that anchors our regex above)
    m = re.search(
        r"// Slice 22 fix:.*?(?=const aliveRef = useRef\(true\))",
        src,
        re.DOTALL,
    )
    assert m, "Couldn't find the new 'Slice 22 fix' comment block"
    block = m.group(0)
    assert re.search(r"mount|unmount", block, re.IGNORECASE), (
        "The replacement comment should mention mount/unmount semantics "
        "so future readers understand why ``[]`` is correct."
    )


@pytest.mark.no_db
def test_other_drawer_effects_keep_their_deps():
    """The other Drawer useEffects (Esc, swipe, focus-trap) should still
    have their ``[open, ...]`` deps. We don't want slice 22 to have
    broken them by accident.
    """
    src = DRAWER.read_text()
    # Sample check: the refetchSources trigger should still gate on [open]
    assert re.search(r"useEffect\(\(\) => \{[^}]*refetchSources\(\)[^}]*\}, \[open\]\)", src, re.DOTALL), (
        "The refetchSources trigger must still gate on ``[open]`` so the "
        "drawer fetches its sources list every time the user opens it."
    )
    # Esc-key effect should still gate on [open, onClose]
    assert re.search(r"addEventListener\('keydown', onKey", src), "Esc handler missing"
    assert re.search(r"window\.addEventListener\('keydown', onKey, true\)", src), "Esc useCapture flag missing"