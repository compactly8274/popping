"""NVD recent CVEs (NATIONAL VULNERABILITY DATABASE).

Fetches a rolling 7-day window of recently published CVEs via the
NVD CVE 2.0 API. Output is plain JSON, no auth, no rate limit (NVD
asks for a 6-second cadence between requests — we refresh every 6 h
so this is comfortable).

The ``pubStartDate`` parameter is RFC 3339 UTC; the response carries
``published`` for each CVE. We map the CVE description into
``summary`` so the embedder has real text to vector against.

Phase 3 only reads recently-published CVEs. Modified/older CVEs would
require a different query and aren't on the roadmap.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import httpx

from app.sources import register_source
from app.sources.base import SourcePlugin

logger = logging.getLogger("popping.sources.nvd")

_BASE = "https://services.nvd.nist.gov/rest/json/cves/2.0"
_WINDOW_DAYS = 7
_TIMEOUT = 30.0  # NVD can be slow
_DEFAULT_HEADERS = {
    "User-Agent": "Popping/0.2 (+https://github.com/compactly8274/popping)",
}


def _rfc3339(dt_obj: dt.datetime) -> str:
    # NVD expects "YYYY-MM-DDTHH:MM:SS.sss" without timezone marker (UTC assumed).
    return dt_obj.strftime("%Y-%m-%dT%H:%M:%S.000")


def _parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _first_english_description(cve: dict) -> str:
    for desc in (cve.get("descriptions") or []):
        if (desc.get("lang") or "").lower().startswith("en"):
            return desc.get("value", "") or ""
    return ""


@register_source
class NvdRecent(SourcePlugin):
    name = "nvd_recent"
    type = "api"
    category = "vulns"
    url = _BASE
    refresh_interval_seconds = 21600  # 6 h

    async def fetch(self) -> list[dict]:
        # Rolling 7-day window. NVD rejects very recent pubStartDate if
        # the lastModifiedDate is too close to now (sometimes returns
        # 404), so we use UTC 0:00 of the day 8 days ago to be safe.
        start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=_WINDOW_DAYS + 1)
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        params = {
            "pubStartDate": _rfc3339(start),
            "resultsPerPage": 50,
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_DEFAULT_HEADERS) as client:
                resp = await client.get(self.url, params=params)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("nvd: fetch failed: %s", exc)
            return []
        try:
            data = resp.json()
        except ValueError:
            logger.warning("nvd: non-JSON response (likely 404 / captcha)")
            return []
        items: list[dict] = []
        for cve in (data.get("vulnerabilities") or []):
            cve_inner = cve.get("cve") or {}
            cve_id = cve_inner.get("id") or ""
            desc = _first_english_description(cve_inner)
            published = _parse_iso(cve_inner.get("published"))
            metrics = cve_inner.get("metrics") or {}
            # Best-effort CVSS score — prefer v3.1, fall back to v3.0,
            # then v2.0. We don't surface severity text; the UI can pull
            # it from the ``vector`` meta if it wants.
            score = None
            severity = None
            vector = None
            for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
                bucket = metrics.get(key) or []
                if bucket:
                    primary = bucket[0].get("cvssData") or {}
                    score = primary.get("baseScore")
                    severity = primary.get("baseSeverity")
                    vector = primary.get("vectorString")
                    break
            if not cve_id:
                continue
            items.append(
                {
                    "title": f"{cve_id}: {desc[:140] + ('…' if len(desc) > 140 else '')}".strip(),
                    "url": f"https://nvd.nist.gov/vuln/detail/{cve_id}",
                    "published_at": published,
                    "summary": desc,
                    "meta": {
                        "cve_id": cve_id,
                        "cvss_score": score,
                        "cvss_severity": severity,
                        "cvss_vector": vector,
                    },
                }
            )
        return items