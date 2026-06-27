"""CISA Known Exploited Vulnerabilities (KEV) catalog.

A single JSON file maintained by CISA, refreshed daily. Each entry has
``cveID``, ``vulnerabilityName``, ``shortDescription``, ``dateAdded``.

We use ``dateAdded`` as ``published_at`` because that's when the
vulnerability was added to the actively-exploited list — the
operationally meaningful timestamp for "what should I patch today?".
Using the original CVE published date would make every KEV entry
months or years old at the moment it appears, which is the wrong
signal for the recency scorer.

The catalog is small (~1000 entries) so we fetch it whole.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import httpx

from app.sources import register_source
from app.sources.base import SourcePlugin

logger = logging.getLogger("popping.sources.cisa_kev")

_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
_TIMEOUT = 30.0
_DEFAULT_HEADERS = {
    "User-Agent": "Popping/0.2 (+https://github.com/compactly8274/popping)",
}


def _parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


@register_source
class CisaKev(SourcePlugin):
    name = "cisa_kev"
    type = "api"
    category = "vulns"
    url = _URL
    refresh_interval_seconds = 21600  # 6 h (catalog updates daily)

    async def fetch(self) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_DEFAULT_HEADERS) as client:
                resp = await client.get(self.url)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("cisa_kev: fetch failed: %s", exc)
            return []
        try:
            data = resp.json()
        except ValueError:
            logger.warning("cisa_kev: non-JSON response")
            return []
        items: list[dict] = []
        for entry in (data.get("vulnerabilities") or []):
            cve_id = entry.get("cveID") or ""
            name = entry.get("vulnerabilityName") or ""
            desc = entry.get("shortDescription") or ""
            added = _parse_iso(entry.get("dateAdded"))
            if not cve_id:
                continue
            # Title format: "CVE-YYYY-NNNN: vendor/product — vulnerability name".
            # The name field is usually already "vendor/product — issue" so we
            # just prepend the CVE id for grepability.
            items.append(
                {
                    "title": f"{cve_id}: {name}".strip(),
                    "url": f"https://nvd.nist.gov/vuln/detail/{cve_id}",
                    "published_at": added,
                    "summary": desc,
                    "meta": {
                        "cve_id": cve_id,
                        "vendor": entry.get("product"),
                        "required_action": entry.get("requiredAction"),
                        "due_date": entry.get("dueDate"),
                        "known_ransomware": entry.get("knownRansomwareCampaignUse") == "Known",
                    },
                }
            )
        return items