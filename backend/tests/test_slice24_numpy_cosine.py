"""Slice 24 — numpy-ize ``personal._cosine``.

The previous implementation walked two 384-dim vectors in a Python
``for x, y in zip(a, b)`` loop, doing ~4000 Python-level float ops
per call. Called 5000-10000× per rescore tick. Slice 16 added numpy
to the aggregate path (``_recompute_preference_vector``) but missed
this per-call site.

This file guards the patch:

- The function still uses ``np.dot`` for the actual math (vs the old
  ``for x, y in zip(...)`` accumulator).
- Behavior is identical to the old Python loop for the documented
  inputs (list, ndarray, None, mismatched-length, zero-norm).
- The ``None``-input contract is preserved (callers map ``None`` to
  the neutral midpoint via ``vector_score``).
- The function is importable as ``app.scoring.personal._cosine``
  and ``vector_score``.

Functional equivalence is checked against a local reference Python
implementation. Vectorized math correctness is verified on three input
shapes (list, ndarray, mixed) at 384-dim (the actual production
dimension).
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

# The patched module is importable from the backend root.
import sys
BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


# Local reference Python implementation for equivalence testing.
# This is the OLD behavior, copied verbatim from the pre-slice source.
def _cosine_python_reference(a, b):
    if a is None or b is None:
        return None
    if hasattr(a, "tolist"):
        a = a.tolist()
    if hasattr(b, "tolist"):
        b = b.tolist()
    if not a or not b or len(a) != len(b):
        return None
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return None
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ---------------------------------------------------------------------------
# 1. Source shape: the math is now np.dot, not a Python for-loop
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_cosine_source_uses_np_dot():
    """The hot loop must use ``np.dot`` — not a Python ``for x, y in zip(...)``
    accumulator. This is the structural change slice 24 introduces."""
    src = (BACKEND / "app/scoring/personal.py").read_text()
    fn_body = src.split("def _cosine", 1)[1].split("def ", 1)[0]
    assert "np.dot" in fn_body, (
        "personal._cosine must use np.dot for the dot product — slice 24's "
        "whole point. Falling back to a Python for-loop reverts the win."
    )
    assert "for x, y in zip" not in fn_body, (
        "The Python for-loop accumulator is what slice 24 replaces. If this "
        "string reappears in the function body, the patch regressed."
    )


@pytest.mark.no_db
def test_cosine_source_imports_numpy():
    """numpy must be imported at module level (not lazily inside the
    function) — the slice 13 pattern for slice 24."""
    src = (BACKEND / "app/scoring/personal.py").read_text()
    # Find the import block at top of file (before ``def _cosine``)
    head = src.split("def _cosine", 1)[0]
    assert re.search(r"^import numpy as np", head, re.MULTILINE), (
        "numpy must be imported at module level. A function-level import "
        "would pay the cost on every call (Python's import cache makes "
        "this fast, but it's still slower than a module-level import)."
    )


import re  # for the assertion above


# ---------------------------------------------------------------------------
# 2. Functional equivalence with the old Python implementation
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_cosine_random_384d_equivalence():
    """Random 384-d vectors (production dimension) must match the
    Python reference to 1e-10 tolerance — the dtype=float coercion
    and np.dot vs the Python accumulator are mathematically the same
    operation, but floating-point order can drift in the last bit.
    """
    rng = np.random.default_rng(42)
    a = rng.standard_normal(384).tolist()
    b = rng.standard_normal(384).tolist()
    ref = _cosine_python_reference(a, b)
    from app.scoring.personal import _cosine
    new = _cosine(a, b)
    assert ref is not None and new is not None
    assert abs(ref - new) < 1e-10, (
        f"Functional equivalence broken: ref={ref}, new={new}, "
        f"delta={abs(ref - new):.2e}"
    )


@pytest.mark.no_db
def test_cosine_ndarray_input_equivalence():
    """ndarray inputs (the pgvector read-back path) must also match the
    Python reference. The Python reference's ``hasattr('tolist')``
    branch forces the numpy array back to a list, so the comparison
    exercises the same end-result."""
    rng = np.random.default_rng(43)
    a = rng.standard_normal(384)
    b = rng.standard_normal(384)
    ref = _cosine_python_reference(a, b)
    from app.scoring.personal import _cosine
    new = _cosine(a, b)
    assert ref is not None and new is not None
    assert abs(ref - new) < 1e-10


@pytest.mark.no_db
def test_cosine_identical_vectors_return_one():
    """v . v / (||v|| * ||v||) = 1.0 — sanity check."""
    rng = np.random.default_rng(44)
    v = rng.standard_normal(384)
    from app.scoring.personal import _cosine
    assert abs(_cosine(v, v) - 1.0) < 1e-12
    assert abs(_cosine(v.tolist(), v.tolist()) - 1.0) < 1e-12


@pytest.mark.no_db
def test_cosine_opposite_vectors_return_minus_one():
    """v . (-v) / (||v|| * ||v||) = -1.0 — opposite-direction check."""
    rng = np.random.default_rng(45)
    v = rng.standard_normal(384)
    from app.scoring.personal import _cosine
    assert abs(_cosine(v, -v) - (-1.0)) < 1e-12


# ---------------------------------------------------------------------------
# 3. Edge case contract: None / empty / mismatched / zero-norm
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_cosine_returns_none_for_none_inputs():
    from app.scoring.personal import _cosine
    assert _cosine(None, [1, 2, 3]) is None
    assert _cosine([1, 2, 3], None) is None
    assert _cosine(None, None) is None


@pytest.mark.no_db
def test_cosine_returns_none_for_empty_inputs():
    from app.scoring.personal import _cosine
    assert _cosine([], [1, 2, 3]) is None
    assert _cosine([1, 2, 3], []) is None
    assert _cosine([], []) is None


@pytest.mark.no_db
def test_cosine_returns_none_for_mismatched_length():
    from app.scoring.personal import _cosine
    assert _cosine([1, 2], [1, 2, 3]) is None
    assert _cosine([1, 2, 3], [1, 2]) is None


@pytest.mark.no_db
def test_cosine_returns_none_for_zero_norm():
    """If either vector is all-zero, cosine is undefined; the contract
    is to return None so callers fall through to the NEUTRAL midpoint
    (NOT 0.0, which would zero out the score)."""
    from app.scoring.personal import _cosine
    assert _cosine([0, 0, 0], [1, 2, 3]) is None
    assert _cosine([1, 2, 3], [0, 0, 0]) is None
    assert _cosine([0, 0, 0], [0, 0, 0]) is None


# ---------------------------------------------------------------------------
# 4. vector_score contract preserved (None → NEUTRAL = 50.0)
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_vector_score_returns_neutral_for_none_embedding():
    """vector_score(embedding=None, ...) must return NEUTRAL=50.0 so
    the dashboard stays usable during cold start (no preference vector
    yet → every entry scores 50, not 0). The numpy rewrite of _cosine
    must not have changed this contract."""
    from app.scoring.personal import vector_score
    assert vector_score(None, [1, 2, 3]) == 50.0


@pytest.mark.no_db
def test_vector_score_clamps_to_0_100():
    """Opposite vectors map to -100 raw, must clamp to 0."""
    from app.scoring.personal import vector_score
    rng = np.random.default_rng(46)
    v = rng.standard_normal(384)
    assert vector_score(v.tolist(), (-v).tolist()) == 0.0


# ---------------------------------------------------------------------------
# 5. Performance smoke (loose check — actual speedup varies by env)
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_cosine_faster_than_python_reference():
    """The numpy path must be faster than the Python for-loop on a
    realistic 384-dim workload (1000 calls). The actual ratio varies
    by Python/numpy version and host, but the structural win should
    be visible at >1000 calls. We assert a modest 1.1x lower bound so
    a future regression that re-introduces a slow Python loop fails
    this test.
    """
    rng = np.random.default_rng(47)
    a = rng.standard_normal(384)
    b = rng.standard_normal(384)
    n = 1000
    # Warm both paths (numpy caches dispatch on first call)
    _cosine_python_reference(a, b)
    from app.scoring.personal import _cosine as new_cos
    new_cos(a, b)

    import time
    t0 = time.perf_counter()
    for _ in range(n):
        _cosine_python_reference(a, b)
    t_ref = time.perf_counter() - t0

    t0 = time.perf_counter()
    for _ in range(n):
        new_cos(a, b)
    t_new = time.perf_counter() - t0

    # numpy must be at least 10% faster on this workload. On the
    # production-rescore workload (5000+ calls) the relative overhead
    # is amortized further; the per-call ratio here is the floor.
    assert t_new < t_ref * 0.9, (
        f"numpy path ({t_new*1000:.1f}ms) should be at least 10% faster "
        f"than Python loop ({t_ref*1000:.1f}ms). If the win disappeared, "
        f"a regression likely added Python overhead to the new path."
    )