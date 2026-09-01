"""Issue #99 contract tests: dwell as a value-scaled preference-vector signal.

Pins the contract the sweep2 PR documents:

- ``settings.pref_vector_weight_dwell`` exists with the documented
  default (0.3) -- the contribution of a fully-read (10-second
  reference) dwell interaction.
- The per-row dwell contribution follows
  ``PREF_VECTOR_WEIGHT_DWELL * min(dwell_seconds / 10, 1.0)``: a
  2-second skim contributes 0.06, a fully-read 10s+ article 0.3,
  and nothing past the cap grows further (a tab left open
  overnight cannot out-weigh a deliberate deep read).
- The knob is read from settings at CALL time -- an env override
  actually changes the weights the recompute uses, so the knob is
  real, not a decorative constant.
- The issue's acceptance ordering holds at any positive weight: a
  30s read always outweighs a 2s skim, because the weight scales
  the whole dwell layer uniformly (5x ratio, weight-invariant).

Pure-function / settings tests -- no DB session needed.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.scheduler import _interaction_row_weight


# ---------------------------------------------------------------------------
# 1. The knob exists, with the documented default
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_pref_vector_weight_dwell_default():
    assert settings.pref_vector_weight_dwell == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# 2. Value-scaled contribution formula
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_dwell_skim_contribution():
    # 2s skim: 0.3 * (2 / 10) = 0.06
    assert _interaction_row_weight("dwell", 2.0) == pytest.approx(0.06)


@pytest.mark.no_db
def test_dwell_full_read_contribution():
    # 10s full read: 0.3 * min(10 / 10, 1.0) = 0.3
    assert _interaction_row_weight("dwell", 10.0) == pytest.approx(0.3)


@pytest.mark.no_db
def test_dwell_caps_at_reference_read():
    # Past the 10s reference the contribution stops growing.
    assert _interaction_row_weight("dwell", 3600.0) == pytest.approx(0.3)


@pytest.mark.no_db
def test_dwell_zero_or_missing_value_contributes_nothing():
    assert _interaction_row_weight("dwell", 0.0) == pytest.approx(0.0)
    assert _interaction_row_weight("dwell", None) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 3. Fixed-weight types are unchanged
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_fixed_types_keep_their_weights():
    assert _interaction_row_weight("click", None) == pytest.approx(1.0)
    assert _interaction_row_weight("thumb_up", None) == pytest.approx(4.0)
    assert _interaction_row_weight("bookmark", None) == pytest.approx(4.0)
    assert _interaction_row_weight("thumb_down", None) == pytest.approx(-4.0)
    assert _interaction_row_weight("never", None) == pytest.approx(-4.0)
    assert _interaction_row_weight("view", None) == pytest.approx(0.2)
    assert _interaction_row_weight("share", None) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 4. The knob is real: settings overrides change the actual weight
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_weights_read_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "pref_vector_weight_dwell", 1.0)
    # Click-parity for a fully-read article; the skim scales too.
    assert _interaction_row_weight("dwell", 10.0) == pytest.approx(1.0)
    assert _interaction_row_weight("dwell", 2.0) == pytest.approx(0.2)
    monkeypatch.setattr(settings, "pref_vector_weight_dwell", 0.0)
    assert _interaction_row_weight("dwell", 30.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 5. Issue acceptance ordering: deep read outweighs skim at any weight
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_deep_read_outranks_skim_at_any_positive_weight(monkeypatch):
    for knob in (0.1, 0.3, 0.5, 1.0):
        monkeypatch.setattr(settings, "pref_vector_weight_dwell", knob)
        deep = _interaction_row_weight("dwell", 30.0)
        skim = _interaction_row_weight("dwell", 2.0)
        assert deep > skim
        # The ratio is weight-invariant: 5x at any knob (30s caps at
        # the 10s norm; 2s is 20% of it).
        assert deep / skim == pytest.approx(5.0)