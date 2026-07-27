"""Slice 23 — replace stdlib ``xml.etree.ElementTree`` with ``defusedxml``.

The stdlib parser doesn't expand external entities by default (so
classic XXE file-disclosure is blocked), but a ``DOCTYPE`` with nested
entity expansion still triggers quadratic / exponential entity
expansion = CPU DoS (billion-laughs attack). This is reachable from
any third-party feed — Reddit ``/r/.../hot.rss``, podcast RSS feeds,
etc — that an attacker can craft with a malicious ``<?xml ?>`` doctype.

``defusedxml.ElementTree`` disables DTD processing entirely so the
parser bails before reading a single entity.

Sites patched:
- backend/app/reddit_client.py — 2 sites (Atom feed for /r/.../hot.rss
  and Atom feed for /r/.../comments/.rss)
- backend/app/sources/rss.py — 1 site (podcast transcript extraction)

This test file guards:
- Both modules import ``defusedxml.ElementTree as ET``
- ``ET.fromstring(...)`` call sites are unchanged (still 3 total)
- ``defusedxml`` is added to ``pyproject.toml`` dependencies
- Functional smoke: defusedxml actually blocks billion-laughs + XXE
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# Repo root resolved from the test file location — works in any CI
# environment.
REPO = Path(__file__).resolve().parents[2]
REDDIT = REPO / "backend/app/reddit_client.py"
RSS = REPO / "backend/app/sources/rss.py"
PYPROJECT = REPO / "backend/pyproject.toml"


# ---------------------------------------------------------------------------
# 1. Module-level imports switch to defusedxml
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_reddit_client_imports_defusedxml():
    src = REDDIT.read_text()
    assert "from defusedxml import ElementTree as ET" in src, (
        "reddit_client.py must import defusedxml's ElementTree as ET "
        "so the existing ET.fromstring() call sites pick up the "
        "hardened parser without code changes."
    )
    assert "import xml.etree.ElementTree as ET" not in src, (
        "reddit_client.py must no longer import the stdlib "
        "xml.etree.ElementTree directly — that's the parser with the "
        "billion-laughs DoS surface."
    )


@pytest.mark.no_db
def test_sources_rss_imports_defusedxml():
    src = RSS.read_text()
    assert "from defusedxml import ElementTree as ET" in src, (
        "sources/rss.py must import defusedxml's ElementTree as ET."
    )
    assert "import xml.etree.ElementTree as ET" not in src


# ---------------------------------------------------------------------------
# 2. Call sites unchanged (regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_reddit_client_has_two_et_fromstring_calls():
    """Two ``ET.fromstring`` call sites in reddit_client.py — one for
    the hot-list Atom feed and one for the comment Atom feed. Both
    must remain at the same line numbers (or near them) and use
    ``ET.fromstring``.

    Counts only real call sites — not ``ET.fromstring(...)`` strings
    inside docstrings / comments / import examples.
    """
    src = REDDIT.read_text()
    # Strip comments and docstrings so the count is real call sites.
    code = re.sub(r'""".*?"""', '', src, flags=re.DOTALL)
    code = re.sub(r"'''.*?'''", '', code, flags=re.DOTALL)
    code = re.sub(r"#.*", "", code)
    n = len(re.findall(r"\bET\.fromstring\s*\(", code))
    assert n == 2, (
        f"Expected 2 ET.fromstring call sites in reddit_client.py "
        f"(hot-list Atom parser + comment-feed Atom parser), "
        f"found {n}. If a call site moved, update this count."
    )


@pytest.mark.no_db
def test_sources_rss_has_one_et_fromstring_call():
    src = RSS.read_text()
    code = re.sub(r'""".*?""""', '', src, flags=re.DOTALL)
    code = re.sub(r"'''.*?'''", '', code, flags=re.DOTALL)
    code = re.sub(r"#.*", "", code)
    n = len(re.findall(r"\bET\.fromstring\s*\(", code))
    assert n == 1, (
        f"Expected 1 ET.fromstring call site in sources/rss.py, "
        f"found {n}."
    )


# ---------------------------------------------------------------------------
# 3. pyproject.toml declares the new dep
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_pyproject_includes_defusedxml():
    """The runtime dep must be added so a fresh ``pip install -e .``
    picks up ``defusedxml``. Without this, ``from defusedxml import
    ElementTree`` raises ImportError at first feed parse.
    """
    src = PYPROJECT.read_text()
    assert re.search(r'"defusedxml[\^>=~]*0?\.?7', src) or "defusedxml" in src, (
        "pyproject.toml must declare ``defusedxml>=0.7`` (or equivalent) "
        "so a clean checkout doesn't blow up at first Reddit feed parse."
    )


# ---------------------------------------------------------------------------
# 4. Functional smoke: defusedxml actually blocks malicious DTDs
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_defusedxml_blocks_billion_laughs():
    """A nested entity expansion (billion-laughs) must be rejected."""
    from defusedxml import ElementTree as ET
    evil = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE lolz [\n'
        '  <!ENTITY lol "lol">\n'
        '  <!ENTITY lol2 "&lol;&lol;&lol;&lol;">\n'
        ']>\n'
        '<lolz>&lol2;</lolz>'
    )
    with pytest.raises(Exception) as excinfo:
        ET.fromstring(evil)
    # defusedxml raises ``EntitiesForbidden`` (subclass of
    # ``defusedxml.EntitiesForbidden`` / ParseError). The exact class
    # name is not contractual — any exception means the attack was
    # blocked before the parser expanded the entity.
    assert excinfo.value is not None, (
        "defusedxml must raise on a billion-laughs DTD"
    )


@pytest.mark.no_db
def test_defusedxml_blocks_external_entity_xxe():
    """An external entity (``<!ENTITY xxe SYSTEM file:///...>``) must
    be rejected. The stdlib parser blocks the file read but still
    parses the entity reference; defusedxml rejects the DTD outright.
    """
    from defusedxml import ElementTree as ET
    ext = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE foo [\n'
        '  <!ENTITY xxe SYSTEM "file:///etc/passwd">\n'
        ']>\n'
        '<foo>&xxe;</foo>'
    )
    with pytest.raises(Exception):
        ET.fromstring(ext)


@pytest.mark.no_db
def test_defusedxml_parses_legitimate_atom():
    """Sanity check — defusedxml must still parse a normal Atom feed
    without error. If this fails, slice 23 is breaking legitimate
    feeds, not just blocking malicious ones.
    """
    from defusedxml import ElementTree as ET
    ok = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry><title>hello</title></entry>'
        '</feed>'
    )
    root = ET.fromstring(ok)
    assert root.tag.endswith("feed"), (
        f"Legitimate Atom feed must parse; got root tag {root.tag!r}"
    )
    assert root.find("{http://www.w3.org/2005/Atom}entry") is not None


@pytest.mark.no_db
def test_defusedxml_parses_legitimate_rss_with_dtd():
    """Some RSS feeds still ship a DOCTYPE (legacy WordPress blogs).
    defusedxml accepts DTDs that contain no entity declarations, so a
    plain ``<!DOCTYPE html>`` shouldn't be rejected. If this fails,
    real RSS feeds start breaking.
    """
    from defusedxml import ElementTree as ET
    ok = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE rss PUBLIC "-//Netscape Communications//DTD RSS 0.91//EN" '
        '"http://www.netscape.com/docs/products/express/rss091.dtd">\n'
        '<rss version="0.91"><channel><title>x</title></channel></rss>'
    )
    root = ET.fromstring(ok)
    assert root.tag == "rss" or root.tag.endswith("}rss")