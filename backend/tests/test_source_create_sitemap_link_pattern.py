"""Slice-20 wire tests: SourceCreate accepts sitemap_url + link_pattern.

Slice 17 added ``Source.sitemap_url`` (override the sitemap URL
for the generic_scrape plugin). Slice 18 added
``Source.link_pattern`` (page_links fallback filter). Both were
PATCH-only — users had to create the row first and then PATCH
the new field. Slice 20 lets the user set them at creation
time so they don't have to PATCH after creating.

Backend changes:
- ``SourceCreate`` schema adds two optional fields with the
  same semantics as the PATCH body
- ``create_source_endpoint`` validates them (sitemap_url as
  http(s) URL; link_pattern as leading-slash path prefix)
- ``scheduler.add_source`` accepts them and writes them to
  the row

Frontend changes:
- ``api.createSource`` body type union is widened to include
  the new optional fields
- ``FeedManager.tsx`` adds two optional inputs to the
  AddCustomTab form (visible when type=generic_scrape)

These are wire tests; they parse the source as text and verify
the new fields are present with the right shape. They don't
need a DB.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCHEMAS = REPO / "backend/app/schemas.py"
SOURCES_ROUTE = REPO / "backend/app/routes/sources.py"
SCHEDULER = REPO / "backend/app/scheduler.py"
API_TS = REPO / "frontend/src/api.ts"
FEEDMANAGER = REPO / "frontend/src/components/FeedManager.tsx"


def _read(path: Path) -> str:
    return path.read_text()


# ---------------------------------------------------------------------------
# 1. SourceCreate schema has the two new optional fields
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_source_create_has_sitemap_url_and_link_pattern():
    src = _read(SCHEMAS)
    # Both fields should be in SourceCreate.model_fields. We
    # look for the field declarations within the SourceCreate
    # block specifically.
    sc_block = re.search(
        r"class SourceCreate\(BaseModel\):.*?(?=\nclass\s|\Z)",
        src,
        re.DOTALL,
    )
    assert sc_block is not None, "couldn't locate SourceCreate block"
    block = sc_block.group(0)
    assert "sitemap_url: Optional[str] = None" in block, (
        "SourceCreate should have sitemap_url: Optional[str] = None"
    )
    assert "link_pattern: Optional[str] = None" in block, (
        "SourceCreate should have link_pattern: Optional[str] = None"
    )


# ---------------------------------------------------------------------------
# 2. add_source accepts the new kwargs
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_scheduler_add_source_signature_includes_new_kwargs():
    src = _read(SCHEDULER)
    # Look at the add_source function signature.
    sig_match = re.search(
        r"async def add_source\(.*?\):",
        src,
        re.DOTALL,
    )
    assert sig_match is not None, "couldn't find add_source signature"
    sig = sig_match.group(0)
    assert "sitemap_url:" in sig, "add_source should accept sitemap_url"
    assert "link_pattern:" in sig, "add_source should accept link_pattern"


# ---------------------------------------------------------------------------
# 3. add_source writes the new fields to the row
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_add_source_assigns_new_fields_to_source_row():
    """The Source() constructor call inside add_source must
    include sitemap_url= and link_pattern= so the values land
    in the DB on first INSERT.
    """
    src = _read(SCHEDULER)
    add_block = re.search(
        r"async def add_source\(.*?(?=\nasync def\s|\nclass\s|\Z)",
        src,
        re.DOTALL,
    )
    assert add_block is not None, "couldn't find add_source function"
    block = add_block.group(0)
    # The Source(...) constructor call.
    assert "row = Source(" in block, "add_source should construct a Source row"
    source_ctor = re.search(r"row = Source\((.*?)\)", block, re.DOTALL)
    assert source_ctor is not None, "couldn't find Source(...) constructor"
    ctor = source_ctor.group(1)
    assert "sitemap_url=sitemap_url" in ctor, (
        "Source() constructor should forward sitemap_url"
    )
    assert "link_pattern=link_pattern" in ctor, (
        "Source() constructor should forward link_pattern"
    )


# ---------------------------------------------------------------------------
# 4. create_source_endpoint validates sitemap_url as http(s)
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_create_source_validates_sitemap_url():
    src = _read(SOURCES_ROUTE)
    # The create_source_endpoint function should call _validate_url
    # on body.sitemap_url when it's present.
    create_block = re.search(
        r"async def create_source_endpoint\(.*?(?=\nasync def\s|\n@router\.)",
        src,
        re.DOTALL,
    )
    assert create_block is not None, "couldn't find create_source_endpoint"
    block = create_block.group(0)
    assert "_validate_url(body.sitemap_url)" in block, (
        "create_source_endpoint should validate sitemap_url as a URL"
    )


# ---------------------------------------------------------------------------
# 5. create_source_endpoint rejects bad link_pattern
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_create_source_rejects_bad_link_pattern():
    """link_pattern must start with '/' and must not be a full URL.
    Two distinct 422 messages for the two failure modes — same as
    the PATCH route's behavior.
    """
    src = _read(SOURCES_ROUTE)
    create_block = re.search(
        r"async def create_source_endpoint\(.*?(?=\nasync def\s|\n@router\.)",
        src,
        re.DOTALL,
    )
    assert create_block is not None
    block = create_block.group(0)
    # Must raise 422 with the "must start with '/'" message.
    assert "must start with '/'" in block, (
        "create_source_endpoint should reject link_pattern without leading slash"
    )
    # Must raise 422 with the "not a full URL" message.
    assert "not a full URL" in block, (
        "create_source_endpoint should reject full URLs as link_pattern"
    )


# ---------------------------------------------------------------------------
# 6. Frontend API: createSource body type includes the new optional fields
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_api_createSource_body_type_includes_new_optional_fields():
    src = _read(API_TS)
    # Find the createSource function definition.
    cs = re.search(r"createSource:\s*\(body:\s*\{[^}]+\}\)\s*=>", src, re.DOTALL)
    assert cs is not None, "couldn't find createSource body type"
    body = cs.group(0)
    assert "sitemap_url" in body, "createSource body should accept sitemap_url"
    assert "link_pattern" in body, "createSource body should accept link_pattern"


# ---------------------------------------------------------------------------
# 7. Frontend AddCustomTab: the two new optional inputs are present
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_feedmanager_add_custom_form_has_new_inputs():
    src = _read(FEEDMANAGER)
    # The AddCustomTab function should have inputs for sitemap_url
    # and link_pattern. We look for the state declarations + the
    # input fields.
    assert "setSitemapUrl" in src, (
        "AddCustomTab should have a setSitemapUrl state setter"
    )
    assert "setLinkPattern" in src, (
        "AddCustomTab should have a setLinkPattern state setter"
    )
    # The inputs themselves should reference these state setters.
    assert "onChange={(e) => setSitemapUrl" in src or \
           "onChange={e => setSitemapUrl" in src, (
        "AddCustomTab should have a Sitemap URL input"
    )
    assert "onChange={(e) => setLinkPattern" in src or \
           "onChange={e => setLinkPattern" in src, (
        "AddCustomTab should have a Link pattern input"
    )


# ---------------------------------------------------------------------------
# 8. Frontend AddCustomTab: inputs are visible only when type=generic_scrape
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_feedmanager_inputs_conditional_on_generic_scrape():
    src = _read(FEEDMANAGER)
    # The new inputs should be conditional on sourceType ===
    # 'generic_scrape' — for RSS / Reddit / Podcast / YouTube
    # types they're irrelevant (those plugins don't read these
    # fields). We look for a guard around the inputs.
    # The simplest check: sitemap_url's input field should be
    # gated by a sourceType === 'generic_scrape' check.
    assert "{sourceType === 'generic_scrape' && (" in src or \
           "{sourceType === \"generic_scrape\" && (" in src, (
        "New AddCustomTab inputs should be conditional on type=generic_scrape"
    )


# ---------------------------------------------------------------------------
# 9. Frontend AddCustomTab: submit() includes new fields in the API call
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_feedmanager_submit_includes_new_fields():
    src = _read(FEEDMANAGER)
    # The submit() function calls api.createSource. The body
    # object should include the new optional fields so they're
    # sent to the backend. The implementation uses a conditional
    # spread (``...(condition && { field: value })``) so the
    # field is only sent when the type is generic_scrape AND
    # the input is non-empty. We accept either shape:
    #   - direct key: ``sitemap_url: ...``
    #   - conditional spread: ``...{ sitemap_url: ... }``
    has_sitemap_url_send = (
        "sitemap_url: " in src
        or "{ sitemap_url: " in src
    )
    assert has_sitemap_url_send, (
        "submit() should include sitemap_url in the createSource body"
    )
    has_link_pattern_send = (
        "link_pattern: " in src
        or "{ link_pattern: " in src
    )
    assert has_link_pattern_send, (
        "submit() should include link_pattern in the createSource body"
    )