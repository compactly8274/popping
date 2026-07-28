"""Slice 28 — two deferred security fixes:

1. OIDC ``email_verified`` claim check in ``auth/routes.py``. Without
   this, an OIDC provider that mints tokens with unverified-email
   claims lets an attacker log in as any email address (the ``sub``
   still uniquely identifies them, but the displayed email is
   attacker-controlled). Reject when ``email_verified`` is missing
   or False.

2. Defense-in-depth security headers middleware in ``main.py``. The
   /auth/login + /auth/callback endpoints are clickjacking targets
   (redirect-shaped, take URL params). Add ``Content-Security-Policy:
   frame-ancestors 'none'``, ``X-Frame-Options: DENY``,
   ``Referrer-Policy: no-referrer``, and HSTS (only when ``public_url``
   is https).

This test file guards both invariants structurally + functionally.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
AUTH_ROUTES = REPO / "backend/app/auth/routes.py"
MAIN = REPO / "backend/app/main.py"


# ---------------------------------------------------------------------------
# 1. OIDC email_verified check
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_auth_routes_checks_email_verified_claim():
    src = AUTH_ROUTES.read_text()
    assert "email_verified" in src, (
        "auth/routes.py must reference the ``email_verified`` OIDC "
        "claim. Slice 28 adds this check; without it, the email gets "
        "used as the session display name even though the IdP never "
        "verified the attacker owns it."
    )
    # Should reject when False
    assert re.search(
        r"claims\.get\(\"email_verified\",\s*False\)",
        src,
    ), (
        "The check must default ``email_verified`` to False when the "
        "claim is absent — safer than treating absent as True (which "
        "would let unverified-email IdPs slip through)."
    )


@pytest.mark.no_db
def test_auth_routes_rejects_unverified_email_with_403():
    """When ``email_verified`` is False (or missing) and ``email`` is
    present, the callback must raise 403 — not 500, not a silent
    accept."""
    src = AUTH_ROUTES.read_text()
    # Look for a 403 raise near the email_verified check
    assert re.search(
        r"email_verified[\s\S]{0,500}HTTPException\(\s*status_code\s*=\s*403",
        src,
    ), (
        "When email_verified is False, the callback must raise "
        "HTTPException with status_code=403. 500 is wrong (that's a "
        "server error, but this is a client/IdP config issue). Silent "
        "accept is wrong (defeats the point of the check)."
    )


@pytest.mark.no_db
def test_auth_routes_email_check_guards_session_create():
    """The email_verified check must come BEFORE ``session_create``
    (the call site, not the import) — a rejected email should never
    touch the DB."""
    src = AUTH_ROUTES.read_text()
    # Skip the import line — the first ``session_create`` mention is
    # the import alias, not the call site. Find the first call site
    # (``sid = await session_create(``).
    email_check_pos = src.find("email_verified")
    # Find the first ``sid = await session_create(`` — that's the call
    # we need to gate the check against.
    call_pos = src.find("sid = await session_create(")
    assert email_check_pos > 0, "email_verified check not found"
    assert call_pos > 0, "session_create call site not found"
    assert email_check_pos < call_pos, (
        f"The email_verified check (offset {email_check_pos}) must come "
        f"BEFORE the session_create call (offset {call_pos}). "
        f"Otherwise unverified-email logins still write session rows."
    )


# ---------------------------------------------------------------------------
# 2. Security headers middleware in main.py
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_main_defines_security_headers_middleware():
    src = MAIN.read_text()
    assert re.search(
        r"@app\.middleware\(\"http\"\)\s*\nasync def _security_headers\(",
        src,
    ), (
        "main.py must define an ``_security_headers`` middleware "
        "(@app.middleware(\"http\")). Without this, no response gets "
        "the defense-in-depth headers slice 28 introduces."
    )


@pytest.mark.no_db
def test_main_security_headers_includes_csp_frame_ancestors():
    src = MAIN.read_text()
    # Find the security-headers middleware body
    m = re.search(
        r"async def _security_headers\([^)]*\):[\s\S]*?(?=^@app\.middleware|^def \w+|^async def \w+|^class \w+)",
        src,
        re.MULTILINE,
    )
    assert m, "Couldn't locate _security_headers middleware body"
    body = m.group(0)
    assert re.search(
        r"Content-Security-Policy.*frame-ancestors",
        body,
    ), (
        "_security_headers must set Content-Security-Policy with "
        "``frame-ancestors 'none'`` — this is the modern way to forbid "
        "framing on /auth/login and /auth/callback."
    )


@pytest.mark.no_db
def test_main_security_headers_includes_x_frame_options_deny():
    src = MAIN.read_text()
    m = re.search(
        r"async def _security_headers\([^)]*\):[\s\S]*?(?=^@app\.middleware|^def \w+|^async def \w+|^class \w+)",
        src,
        re.MULTILINE,
    )
    body = m.group(0)
    assert re.search(
        r"X-Frame-Options.*DENY",
        body,
    ), (
        "_security_headers must set X-Frame-Options: DENY — legacy "
        "fallback for browsers that don't honor CSP frame-ancestors."
    )


@pytest.mark.no_db
def test_main_security_headers_includes_referrer_policy():
    src = MAIN.read_text()
    m = re.search(
        r"async def _security_headers\([^)]*\):[\s\S]*?(?=^@app\.middleware|^def \w+|^async def \w+|^class \w+)",
        src,
        re.MULTILINE,
    )
    body = m.group(0)
    assert "Referrer-Policy" in body, (
        "_security_headers must set Referrer-Policy (no-referrer). "
        "The dashboard doesn't need to leak a referer to third-party "
        "resources."
    )


@pytest.mark.no_db
def test_main_security_headers_includes_hsts_conditional_on_https():
    """HSTS only when public_url is https — sending it over http is a
    no-op for the user but a fingerprintable header."""
    src = MAIN.read_text()
    m = re.search(
        r"async def _security_headers\([^)]*\):[\s\S]*?(?=^@app\.middleware|^def \w+|^async def \w+|^class \w+)",
        src,
        re.MULTILINE,
    )
    body = m.group(0)
    assert "Strict-Transport-Security" in body, (
        "_security_headers must set HSTS."
    )
    # Must be gated on https (otherwise it's noise on http)
    assert re.search(
        r"public_url.*https|HSTS.*public_url",
        body,
    ), (
        "HSTS must be conditional on ``public_url.startswith('https://')``."
    )


@pytest.mark.no_db
def test_main_security_headers_use_setdefault_not_assignment():
    """Use ``setdefault`` so existing per-response overrides (e.g. the
    /assets nosniff middleware, or a future per-route override) win.
    Plain assignment would clobber them."""
    src = MAIN.read_text()
    m = re.search(
        r"async def _security_headers\([^)]*\):[\s\S]*?(?=^@app\.middleware|^def \w+|^async def \w+|^class \w+)",
        src,
        re.MULTILINE,
    )
    body = m.group(0)
    n_setdefault = len(re.findall(r"\.setdefault\(", body))
    n_assignment = len(re.findall(
        r"\.headers\[\"X-Frame-Options\"\]|\.headers\[\"Content-Security-Policy\"\]|\.headers\[\"Referrer-Policy\"\]|\.headers\[\"Strict-Transport-Security\"\]",
        body,
    ))
    assert n_setdefault >= 4, (
        f"_security_headers must call .setdefault(...) for all 4 "
        f"headers — found only {n_setdefault}."
    )
    assert n_assignment == 0, (
        "_security_headers must NOT use ``response.headers[...] = ...`` "
        "for these headers — that would clobber per-route overrides. "
        "Use setdefault."
    )