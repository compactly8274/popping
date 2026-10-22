"""Route-level contract for the feed editor: the header, category,
name and refresh rules that the frontend mirrors, and the scheduler
skip for category-only edits.

Every test goes through the real routes with ``app_client``. URLs use
example.com / example.org, answered by ``fake_dns`` so the SSRF guard
runs without network.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import scheduler
from app.models import Source
from app.sources import list_sources

_CBC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}


async def _create(app_client, name="contract_feed", **extra):
    payload = {
        "name": name,
        "type": "rss",
        "category": "news",
        "url": "https://example.com/feed.xml",
        **extra,
    }
    return await app_client.post("/api/sources", json=payload)


async def _row(db_session, source_id):
    db_session.expire_all()
    return await db_session.scalar(select(Source).where(Source.id == source_id))


# --- headers ---------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Bad Header", "", "Coo kie"])
async def test_invalid_header_names_are_refused_as_invalid(app_client, fake_dns, name):
    resp = await _create(app_client, custom_headers={name: "x"})
    assert resp.status_code == 422
    assert "not a valid HTTP header name" in resp.json()["detail"]

    created = await _create(app_client, name="patch_target")
    assert created.status_code == 200
    resp = await app_client.patch(f"/api/sources/{created.json()['id']}", json={"custom_headers": {name: "x"}})
    assert resp.status_code == 422
    assert "not a valid HTTP header name" in resp.json()["detail"]


@pytest.mark.parametrize("name", ["Cookie", "Content-Length", "Proxy-Authorization"])
async def test_denied_header_names_are_refused(app_client, fake_dns, name):
    resp = await _create(app_client, custom_headers={name: "x"})
    assert resp.status_code == 422
    assert "cannot be overridden" in resp.json()["detail"]

    created = await _create(app_client, name="patch_target")
    resp = await app_client.patch(f"/api/sources/{created.json()['id']}", json={"custom_headers": {name: "x"}})
    assert resp.status_code == 422
    assert "cannot be overridden" in resp.json()["detail"]


async def test_ordinary_header_is_accepted(app_client, fake_dns):
    resp = await _create(app_client, custom_headers={"X-Ok": "1"})
    assert resp.status_code == 200
    assert resp.json()["custom_headers"] == {"X-Ok": "1"}


@pytest.mark.parametrize(
    "headers",
    [
        {"X-A": "1", "x-a": "2"},
        {"X" * 101: "1"},
        {"X-Ok": "a\r\nInjected: 1"},
        {f"X-H{i}": "v" for i in range(21)},
        {"X-Ok": "v" * 513},
    ],
    ids=["duplicate", "long-name", "crlf", "too-many", "long-value"],
)
async def test_bad_header_maps_are_refused(app_client, fake_dns, headers):
    resp = await _create(app_client, custom_headers=headers)
    assert resp.status_code == 422


@pytest.mark.parametrize("clear", [None, {}])
async def test_null_and_empty_both_clear(app_client, fake_dns, db_session, clear):
    created = await _create(app_client, custom_headers={"X-Ok": "1"})
    source_id = created.json()["id"]
    resp = await app_client.patch(f"/api/sources/{source_id}", json={"custom_headers": clear})
    assert resp.status_code == 200
    assert resp.json()["custom_headers"] is None
    assert (await _row(db_session, source_id)).custom_headers is None


async def test_lower_case_user_agent_is_stored_canonical(app_client, fake_dns):
    resp = await _create(app_client, custom_headers={"user-agent": "X"})
    assert resp.status_code == 200
    assert resp.json()["custom_headers"] == {"User-Agent": "X"}


async def test_url_patch_leaves_headers_alone(app_client, fake_dns):
    created = await _create(app_client, custom_headers={"X-Ok": "1"})
    source_id = created.json()["id"]
    resp = await app_client.patch(f"/api/sources/{source_id}", json={"url": "https://example.org/other.xml"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["custom_headers"] == {"X-Ok": "1"}


async def test_recommendation_default_headers_still_add(app_client, fake_dns):
    resp = await _create(app_client, name="cbc_top", custom_headers=_CBC_HEADERS)
    assert resp.status_code == 200, resp.text


# --- categories --------------------------------------------------------------------


async def test_category_is_stored_stripped(app_client, fake_dns):
    resp = await _create(app_client, category=" Tech ")
    assert resp.status_code == 200
    assert resp.json()["category"] == "Tech"


async def test_forty_characters_with_spaces_is_stored_stripped(app_client, fake_dns):
    resp = await _create(app_client, category="  " + "c" * 40 + "  ")
    assert resp.status_code == 200, resp.text
    assert resp.json()["category"] == "c" * 40

    created = await _create(app_client, name="patch_target")
    resp = await app_client.patch(
        f"/api/sources/{created.json()['id']}", json={"category": " " + "d" * 40 + " "}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["category"] == "d" * 40


@pytest.mark.parametrize("category", ["", "   ", "c" * 41, "For You", "for_you", "saved_", "For-You"])
async def test_bad_categories_are_refused(app_client, fake_dns, category):
    resp = await _create(app_client, category=category)
    assert resp.status_code == 422


async def test_auto_scrape_branch_stores_category_stripped(app_client, fake_dns, monkeypatch):
    async def no_feed(url):
        return None

    async def one_page(url, limit=1):
        return ["https://example.com/article"]

    monkeypatch.setattr("app.routes.sources.discover_feed_url", no_feed)
    monkeypatch.setattr("app.routes.sources.discover_sitemap_urls", one_page)
    resp = await app_client.post("/api/sources/auto", json={"url": "https://example.com/", "category": " Tech "})
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "generic_scrape"
    assert resp.json()["source"]["category"] == "Tech"


async def test_discover_route_uses_the_stripped_category(app_client, monkeypatch):
    seen = {}

    async def fake_discover(session, *, category, context, **kwargs):
        seen["category"] = category
        return [], None

    monkeypatch.setattr("app.routes.sources.discover_candidates", fake_discover)
    resp = await app_client.post("/api/feed-recommendations/discover", json={"category": " Tech "})
    assert resp.status_code == 200, resp.text
    assert resp.json()["category"] == "Tech"
    assert seen["category"] == "Tech"


# --- names ---------------------------------------------------------------------------


async def test_built_in_rows_cannot_be_renamed_or_re_urled(app_client, db_session):
    registry = list_sources()
    assert "rfd_hot_deals" in registry
    assert len(registry) == 7
    for plugin in registry.values():
        row = await scheduler._upsert_source(db_session, plugin)
        for change in ({"name": "renamed_builtin"}, {"url": "https://example.com/x.xml"}):
            resp = await app_client.patch(f"/api/sources/{row.id}", json=change)
            assert resp.status_code == 400, (row.name, change, resp.text)


async def test_name_collision_is_409(app_client, fake_dns):
    await _create(app_client, name="first_feed")
    second = await _create(app_client, name="second_feed")
    resp = await app_client.patch(f"/api/sources/{second.json()['id']}", json={"name": "first_feed"})
    assert resp.status_code == 409


@pytest.mark.parametrize("name", ["Bad Name", "abc\n"])
async def test_bad_names_are_refused(app_client, fake_dns, name):
    resp = await _create(app_client, name=name)
    assert resp.status_code == 422
    created = await _create(app_client, name="patch_target")
    resp = await app_client.patch(f"/api/sources/{created.json()['id']}", json={"name": name})
    assert resp.status_code == 422


# --- refresh ----------------------------------------------------------------------------


async def test_refresh_is_clamped_to_a_minute(app_client, fake_dns):
    created = await _create(app_client)
    resp = await app_client.patch(f"/api/sources/{created.json()['id']}", json={"refresh_interval_seconds": 30})
    assert resp.status_code == 200
    assert resp.json()["refresh_interval_seconds"] == 60


# --- scheduler: category-only edits do not rebuild the job --------------------------------


class _FakeScheduler:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.removed: list[str] = []

    def add_job(self, *args, **kwargs):
        self.added.append(kwargs.get("id", ""))

    def remove_job(self, job_id, *args, **kwargs):
        self.removed.append(job_id)

    def get_job(self, *args, **kwargs):
        return None


async def test_category_only_patch_does_not_rebuild_the_job(app_client, fake_dns, monkeypatch):
    created = await _create(app_client)
    source_id = created.json()["id"]
    fake = _FakeScheduler()
    monkeypatch.setattr(scheduler, "_scheduler", fake)

    resp = await app_client.patch(f"/api/sources/{source_id}", json={"category": "tech"})
    assert resp.status_code == 200
    assert fake.added == []

    resp = await app_client.patch(f"/api/sources/{source_id}", json={"custom_headers": {"X-Ok": "1"}})
    assert resp.status_code == 200
    assert len(fake.added) == 1


# --- display name ------------------------------------------------------------------


async def test_display_name_keeps_what_the_user_typed(app_client, fake_dns, db_session):
    created = await _create(app_client, name="cpabc_newsroom", display_name="  CPABC   Newsroom ")
    assert created.status_code == 200
    assert created.json()["display_name"] == "CPABC Newsroom"
    assert created.json()["name"] == "cpabc_newsroom"

    sid = created.json()["id"]
    resp = await app_client.patch(f"/api/sources/{sid}", json={"display_name": "B.C. Gov – Finance"})
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "B.C. Gov – Finance"
    # The internal slug never changes with the label.
    assert resp.json()["name"] == "cpabc_newsroom"
    assert (await _row(db_session, sid)).display_name == "B.C. Gov – Finance"

    resp = await app_client.patch(f"/api/sources/{sid}", json={"display_name": "   "})
    assert resp.status_code == 200
    assert resp.json()["display_name"] is None


async def test_display_name_too_long_is_refused(app_client, fake_dns):
    created = await _create(app_client, name="long_label")
    resp = await app_client.patch(f"/api/sources/{created.json()['id']}", json={"display_name": "x" * 121})
    assert resp.status_code == 422
