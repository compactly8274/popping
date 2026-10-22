"""``fetch_rss`` says when an address isn't a feed.

A feed URL that answers 200 with a web page, a JSON API or an empty
body used to be a silent success when the page didn't trip feedparser's
``bozo`` flag (an ordinary ``text/html`` page doesn't): zero items, no
error, and a feed that looked quiet forever. It now raises
``NotAFeedError``, which the scheduler records as ``[not a feed]`` and
the Test route still reports as ``parse_error``. A well-formed feed
with no items is still a success, and a feed whose entries are broken
keeps its old "malformed entries" error.
"""

from __future__ import annotations

import httpx
import pytest

from app import scheduler
from app.routes import sources as sources_routes
from app.schemas import SourceTestRequest
from app.sources import rss
from app.sources.base import NotAFeedError

pytestmark = pytest.mark.no_db

URL = "https://feed.test/feed"


@pytest.fixture
def serve(monkeypatch):
    """Answer every request ``fetch_rss`` makes with the body and
    content type the test sets."""
    real_client = httpx.AsyncClient
    response: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=response["body"].encode(),
            headers={"content-type": response["ctype"]},
        )

    def client(**kwargs):
        # No DNS in tests: drop the SSRF hook (pinned elsewhere) and
        # answer from the handler.
        kwargs.pop("event_hooks", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(rss.httpx, "AsyncClient", client)

    def _serve(body: str, ctype: str) -> None:
        response["body"] = body
        response["ctype"] = ctype

    return _serve


HTML_PAGE = (
    "<!doctype html><html><head><title>Example</title></head>"
    "<body><h1>Welcome</h1><p>Our latest news.</p></body></html>"
)
# No doctype: feedparser parses it without setting ``bozo``.
CHALLENGE_PAGE = "<html><head><title>Just a moment...</title></head><body><div>Checking</div></body></html>"
XHTML_PAGE = (
    '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
    "<head><title>x</title></head><body/></html>"
)


@pytest.mark.parametrize(
    "body,ctype",
    [
        (HTML_PAGE, "text/html; charset=utf-8"),
        (CHALLENGE_PAGE, "text/html"),
        (XHTML_PAGE, "application/xhtml+xml"),
        ('{"items": []}', "application/json"),
        ("", "text/html"),
    ],
    ids=["html", "html-no-bozo", "xhtml", "json", "empty"],
)
async def test_a_body_that_is_not_a_feed_raises_not_a_feed(serve, body, ctype):
    serve(body, ctype)
    with pytest.raises(NotAFeedError) as excinfo:
        await rss.fetch_rss(URL)
    # A ValueError that says "parse", so the Test route keeps calling it
    # a parse error.
    assert isinstance(excinfo.value, ValueError)
    assert "parse" in str(excinfo.value)
    assert scheduler._classify_error(excinfo.value) == "not a feed"


@pytest.mark.parametrize(
    "body",
    [
        '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>',
        "<rss><channel><title>x</title></channel></rss>",
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title></feed>',
        '<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"'
        ' xmlns="http://purl.org/rss/1.0/"><channel><title>x</title></channel></rdf:RDF>',
    ],
    ids=["rss20", "rss-no-version", "atom", "rdf"],
)
async def test_a_valid_empty_feed_is_still_a_success(serve, body):
    serve(body, "application/xml")
    assert await rss.fetch_rss(URL) == []


async def test_a_feed_with_items_is_unchanged(serve):
    serve(
        '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>'
        "<item><title>One</title><link>https://feed.test/1</link></item>"
        "<item><title>Two</title><link>https://feed.test/2</link></item>"
        "</channel></rss>",
        "application/rss+xml",
    )
    items = await rss.fetch_rss(URL)
    assert [(i["title"], i["url"]) for i in items] == [
        ("One", "https://feed.test/1"),
        ("Two", "https://feed.test/2"),
    ]


async def test_a_truncated_feed_keeps_the_malformed_error(serve):
    # A feed (feedparser recognised RSS 2.0) whose body was cut off: not
    # "not a feed", whose fix is a new address, but a broken read.
    serve('<?xml version="1.0"?><rss version="2.0"><channel><title>x', "application/rss+xml")
    with pytest.raises(ValueError) as excinfo:
        await rss.fetch_rss(URL)
    assert not isinstance(excinfo.value, NotAFeedError)
    assert "malformed entries" in str(excinfo.value)
    assert scheduler._classify_error(excinfo.value) == "error"


async def test_the_test_route_reports_a_web_page_as_a_parse_error(serve, monkeypatch):
    monkeypatch.setattr(sources_routes, "check_url_safe", lambda url: (True, None))
    serve(CHALLENGE_PAGE, "text/html")
    result = await sources_routes._test_source_impl(
        SourceTestRequest(type="rss", url=URL), session=None
    )
    assert result.ok is False
    assert result.error_kind == "parse_error"
