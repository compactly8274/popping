"""With OIDC on, every /api route except /api/health must require a user.

Gating is decided at import time from settings, so the check runs in a
fresh interpreter with OIDC_ENABLED set, calling every route without a
session cookie. The router-level ``require_user`` dependency rejects the
request before the handler (or the database) is touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

_PROBE = """
import asyncio, json, re
import httpx
from app.main import app

async def main():
    out = {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        for path, ops in app.openapi()["paths"].items():
            if not path.startswith("/api") or path == "/api/health":
                continue
            url = re.sub(r"{[^}]+}", "1", path)
            for method in ops:
                r = await c.request(method.upper(), url)
                out[method.upper() + " " + path] = r.status_code
    print(json.dumps(out))

asyncio.run(main())
"""


@pytest.mark.no_db
def test_all_api_routes_except_health_require_user_when_oidc_on(tmp_path):
    env = {
        **os.environ,
        "OIDC_ENABLED": "true",
        "SESSION_SECRET": "x" * 32,
        "ASSETS_DIR": str(tmp_path),
        "LOCAL_AUTH_BYPASS": "false",
    }
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    statuses = json.loads(result.stdout.strip().splitlines()[-1])
    assert len(statuses) > 20
    not_gated = {k: v for k, v in statuses.items() if v != 401}
    assert not_gated == {}


@pytest.mark.no_db
def test_email_allowlist():
    from app.auth.routes import _email_allowed

    assert _email_allowed("anyone@example.com", "")
    assert _email_allowed("Me@Example.com", "me@example.com, other@x.ca")
    assert not _email_allowed("stranger@example.com", "me@example.com")
    assert not _email_allowed(None, "me@example.com")
