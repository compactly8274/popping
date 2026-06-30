#!/usr/bin/env python3
"""One-shot Reddit reachability check for direct mode.

Run from the TrueNAS host (or any host that hosts popping-backend-1)
to confirm whether Reddit's public JSON endpoints will work from
this IP without a proxy.

Direct mode throttles to 2 req/s sustained / 4 burst. A persistent
403 here means Reddit has throttled this IP for the foreseeable
future and you should plan to deploy the proxy (see
``/opt/popping-proxy``) and set ``REDDIT_HYDRA_URL`` in the
backend container env.

Usage:
    docker exec popping-backend-1 python /app/scripts/reddit_reachability.py
    # or locally with the backend venv active:
    python scripts/reddit_reachability.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx

REDDIT_BASE = "https://www.reddit.com"
USER_AGENT = (
    "Popping/0.2 (+https://github.com/compactly8274/popping; "
    "contact: see /admin for operator email)"
)
TIMEOUT = 10.0


async def probe(client: httpx.AsyncClient, label: str, path: str) -> dict:
    """Make one request, return a small summary dict."""
    started = time.monotonic()
    try:
        resp = await client.get(path, headers={"User-Agent": USER_AGENT})
        elapsed = time.monotonic() - started
        return {
            "label": label,
            "path": path,
            "status": resp.status_code,
            "elapsed_s": round(elapsed, 2),
            "content_type": resp.headers.get("content-type", "?"),
            "bytes": len(resp.content),
            "ok": resp.status_code == 200
            and "json" in resp.headers.get("content-type", "").lower(),
        }
    except Exception as e:
        return {
            "label": label,
            "path": path,
            "error": f"{type(e).__name__}: {e}",
            "ok": False,
        }


async def main() -> int:
    print(f"Probing Reddit from this host. UA: {USER_AGENT!r}\n")
    async with httpx.AsyncClient(
        base_url=REDDIT_BASE,
        timeout=TIMEOUT,
        follow_redirects=True,
    ) as client:
        results = []
        for label, path in [
            ("subreddit listing", "/r/python/hot.json?limit=2"),
            ("search by URL", "/search.json?q=url%3Ahttps%3A%2F%2Fexample.com&limit=1"),
        ]:
            results.append(await probe(client, label, path))
            # Brief pause so we don't fire the second request in the
            # same millisecond as the first — gives the server a fair
            # shot at distinguishing the two.
            await asyncio.sleep(0.5)

    print(json.dumps(results, indent=2))
    print()

    all_ok = all(r.get("ok") for r in results)
    if all_ok:
        print("Direct mode will work from this IP. Reddit's anti-abuse")
        print("may still 429 / 403 you within hours of polling cadence,")
        print("but the contact-stamped UA above gives you a fighting")
        print("chance. If you see 429s in the scheduler log, deploy the")
        print("proxy and set REDDIT_HYDRA_URL.")
        return 0

    print("Direct mode will NOT work reliably from this IP. At least one")
    print("of the probes failed:")
    for r in results:
        if not r.get("ok"):
            print(f"  - {r['label']} ({r.get('path')}): "
                  f"status={r.get('status', '?')} "
                  f"ctype={r.get('content_type', '?')} "
                  f"error={r.get('error', '-')}")
    print()
    print("Recommendation: deploy the proxy (see /opt/popping-proxy),")
    print("set REDDIT_HYDRA_URL=https://your.proxy.host, restart the")
    print("backend, and the scheduler will route through the proxy")
    print("instead of direct to Reddit.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
