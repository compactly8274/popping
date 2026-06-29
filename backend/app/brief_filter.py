"""Brief-only filters.

Pure helpers consumed by ``app.brief`` to clean the candidate set
before it reaches the LLM. Two pieces:

  1. ``is_clickbait(title)`` — surface-level patterns that mark
     editorial-quality titles. Returns True for ALL-CAPS headlines,
     listicle patterns ("7 things you won't believe…"), excessive
     punctuation ("!!!"), sensational adjectives ("SHOCKING",
     "UNBELIEVABLE"), and emoji-heavy noise. False positives are
     acceptable here — the LLM is given the title anyway as a
     fallback, so a tight filter that drops a legitimate headline
     only costs us one bullet. Cheap deterministic matchers; no ML.

  2. ``extract_summary(meta, *, max_chars)`` — pick the best long-form
     description from the source-specific ``Entry.meta`` blob.
     ``_format_entries`` previously read only ``meta["summary"]``;
     sources differ on which key they emit (RSS uses ``summary``,
     NVD uses ``description`` via ``meta`` lifting, HN uses ``text``,
     Wikipedia uses ``extract``, CISA/KEV uses ``shortDescription``).
     Centralizing the priority order here keeps the prompt fed with
     the most informative field per source.

These are deliberately scoped to the brief pipeline — the dashboard
feed keeps clickbait intact, so the user still sees those items
if they want to (and the convergence system can still detect them).
The brief is an editorial product; filtering is the editor's job.
"""

from __future__ import annotations

import re
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Clickbait detection
# ---------------------------------------------------------------------------

# ALL-CAPS headlines: >50% alphabetic characters are uppercase, and
# the title has at least 4 letters (so "USA" doesn't trip it).
# A short all-caps acronym like "FBI" or "AI" is fine; a title where
# the bulk of words are capped is not.
def _is_allcaps(title: str) -> bool:
    letters = [c for c in title if c.isalpha()]
    if len(letters) < 4:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) > 0.5


# Excessive punctuation runs: "!!!", "?!!", "?!?!" etc. A single
# "!" or "?" is normal; three or more in a row is shouting.
_EXCESSIVE_PUNCT_RE = re.compile(r"[!?]{3,}")


# Listicles / how-to / "you won't believe" templates. Case-insensitive.
# Each pattern is a substring match — cheap, no tokenization.
_LISTICLE_PATTERNS: tuple[str, ...] = (
    r"\b\d+\s+(things|ways|reasons|tips|hacks|tricks|secrets|signs)\b",
    r"\byou (won't|wont|will never|should never)\s+(believe|guess|imagine|expect)\b",
    r"\bthis (one )?(weird|crazy|simple|genius)\s+(trick|secret|hack|method)\b",
    r"\bwhat (happened|they did) next\b",
    r"\bthe truth about\b",
    r"\bdoctors (hate|don't want|won't tell)\b",
)


# Sensational adjectives in caps or title case. Matched as whole
# words so "TERRIBLE accident" trips but "TERRIBLY" doesn't.
_SENSATIONAL_WORDS: frozenset[str] = frozenset({
    "shocking",
    "unbelievable",
    "insane",
    "mind-blowing",
    "mindblowing",
    "outrageous",
    "horrifying",
    "incredible",
    "stunning",
    "jaw-dropping",
    "jawdropping",
    "epic",
    "viral",
    "breaking",
})


# Emojis — broad Unicode emoji range. Title is "mostly emoji" if
# 30%+ of its characters fall in this range.
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F300, 0x1F5FF),  # symbols & pictographs
    (0x1F600, 0x1F64F),  # emoticons
    (0x1F680, 0x1F6FF),  # transport & map
    (0x1F700, 0x1F77F),  # alchemical
    (0x1F900, 0x1F9FF),  # supplemental
    (0x2600, 0x26FF),    # misc symbols
    (0x2700, 0x27BF),    # dingbats
)


def _emoji_ratio(title: str) -> float:
    if not title:
        return 0.0
    n_emoji = 0
    for ch in title:
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES):
            n_emoji += 1
    return n_emoji / len(title)


def is_clickbait(title: Optional[str]) -> bool:
    """True if ``title`` matches any clickbait surface pattern.

    The listicle patterns are pre-compiled at module load; everything
    else is per-call but cheap (linear scan). A title that's empty,
    None, or just whitespace isn't clickbait — it's a bad row, but
    that's a separate problem (the normalizer already rejects empty
    titles so this branch is just defensive).

    This is intentionally a SHALLOW filter. We don't try to detect
    "subtle" clickbait ("You won't guess what happened next…") —
    that's the LLM's job, and the prompt now nudges it to ignore
    sensational framing. We catch the loud, deterministic patterns
    that an editor would obviously flag, and leave the rest to the
    model.
    """
    if not title:
        return False
    t = title.strip()
    if not t:
        return False

    if _is_allcaps(t):
        return True

    if _EXCESSIVE_PUNCT_RE.search(t):
        return True

    tl = t.lower()
    for pat in _LISTICLE_PATTERNS:
        if re.search(pat, tl):
            return True

    # Sensational words — match as whole words so "unbelievable" trips
    # but "unbelievably" doesn't (we're being conservative on the
    # latter; the prompt handles "epic launch" naturally).
    tokens = re.findall(r"[a-zA-Z][a-zA-Z\-']*", tl)
    if any(tok in _SENSATIONAL_WORDS for tok in tokens):
        return True

    if _emoji_ratio(t) >= 0.3:
        return True

    return False


# ---------------------------------------------------------------------------
# Summary extraction
# ---------------------------------------------------------------------------

# Priority order for "what's the long-form description of this entry?"
# Different sources emit different keys; we read them in this order
# and return the first non-empty string. New sources should add their
# preferred key near the top.
#
# Note: ``Entry.body_text`` is a column (populated by a separate
# pipeline) and not in ``meta``, so it doesn't appear here. RSS
# feeds ship the description in ``summary``; that's where this
# helper looks first.
_SUMMARY_KEYS: tuple[str, ...] = (
    "summary",             # RSS feeds (BBC, Verge, etc.)
    "description",         # generic / Wikipedia extracts
    "shortDescription",    # CISA KEV
    "vulnerabilityName",   # NVD sometimes carries a short title
    "text",                # HN Ask HN body
    "extract",             # Wikipedia "on this day" extracts
)


def extract_summary(meta: Optional[dict[str, Any]], *, max_chars: int = 600) -> str:
    """Best-effort long-form description from ``Entry.meta``.

    Returns an empty string when no usable field is present — the
    caller (the brief prompt formatter) already handles "no summary"
    by emitting just the title line. ``max_chars`` defaults to 600
    (up from the previous 240) so the LLM has real context to
    summarize from rather than just a fragment.
    """
    if not meta or not isinstance(meta, dict):
        return ""
    for key in _SUMMARY_KEYS:
        val = meta.get(key)
        if not val:
            continue
        s = str(val).strip()
        if not s:
            continue
        # Collapse internal whitespace so a multi-line HTML summary
        # doesn't waste tokens on indentation.
        s = re.sub(r"\s+", " ", s)
        if len(s) > max_chars:
            s = s[:max_chars].rstrip()
            # Trim trailing partial word so the cut doesn't end mid-
            # token ("…the president announc").
            cut = s.rfind(" ")
            if cut > max_chars - 60:
                s = s[:cut]
            s = s.rstrip(" ,;:.-") + "…"
        return s
    return ""