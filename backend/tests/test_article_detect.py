"""``app.article_detect``: which URLs, sitemaps and pages are articles.

Pure library, no wiring yet (PR10b calls it from the generic_scrape
crawl). The fixtures in ``tests/fixtures/scrape/`` are hand-written
from public CMS layouts: WordPress core, Yoast SEO, Shopify, Google
News sitemaps, a WordPress listing page, and the page shapes that
produce junk stories today (About pages, a home-page redirect, a soft
404).
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from lxml import etree

from app.article_detect import (
    ACCEPT_SCORE,
    Candidate,
    classify_sitemap,
    listing_links,
    order_sitemaps,
    score_page,
    url_ok,
)

pytestmark = pytest.mark.no_db

FIXTURES = Path(__file__).parent / "fixtures" / "scrape"
SITE = "https://www.example.com/"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _locs(name: str) -> list[tuple[str, str | None]]:
    """(loc, lastmod) of every entry in a sitemap fixture."""
    root = etree.fromstring((FIXTURES / name).read_bytes())
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    out = []
    for entry in root:
        if not isinstance(entry.tag, str):
            continue
        loc = entry.findtext("sm:loc", namespaces=ns)
        out.append((loc.strip(), entry.findtext("sm:lastmod", namespaces=ns)))
    return out


def _accepted(html: str, url: str, **kwargs) -> bool:
    score, _ = score_page(html, url, **kwargs)
    return score >= ACCEPT_SCORE


STORY_URL = "https://www.example.com/news/council-approves-river-trail"


def _page(
    *,
    title: str = "Council approves the river trail | Example Town News",
    site_name: str = "Example Town News",
    og_type: str | None = "article",
    published: str | None = "2026-10-18T09:30:00+00:00",
    jsonld: str | None = None,
    body: str = "<p>The extension adds four kilometres of path.</p>",
) -> str:
    """A small inline page. Pass ``text=`` to ``score_page`` to set its
    length, so each test controls exactly which rules fire."""
    head = [f"<title>{title}</title>", f'<meta property="og:site_name" content="{site_name}">']
    if og_type:
        head.append(f'<meta property="og:type" content="{og_type}">')
    if published:
        head.append(f'<meta property="article:published_time" content="{published}">')
    if jsonld:
        head.append(f'<script type="application/ld+json">{jsonld}</script>')
    return f"<html><head>{''.join(head)}</head><body>{body}</body></html>"


# ---------------------------------------------------------------------------
# url_ok
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # Slugs that hold a structural word as part of a segment.
        # courlan's own structure filter rejects all three.
        "https://www.example.com/pets/fat-cat/how-to-feed",
        "https://www.example.com/news/power-user/tips",
        "https://www.example.com/search/best-laptops-2026",
        # Everyday article shapes.
        "https://www.example.com/2026/10/18/council-approves-river-trail/",
        "https://www.example.com/news/987654",
        "https://www.example.com/blogs/news/autumn-restock",
        "https://news.example.com/local/bridge-reopens",
        # A query string courlan's strict mode would strip still passes,
        # and the URL is never rewritten.
        "https://www.example.com/article.php?id=42&ref=home",
        # WordPress Plain and Numeric permalinks.
        "https://www.example.com/?p=123",
        "https://www.example.com/index.php?p=123",
        "https://www.example.com/archives/123",
        # An article under an archive section (The Atlantic's shape).
        "https://www.example.com/technology/archive/2024/01/ai-chatbots/677123/",
        "https://www.example.com/magazine/archive/2023/10/some-story/675123/",
        # News sections named like static pages or records (Reuters'
        # /legal/), with an article under them.
        "https://www.example.com/legal/government/judge-blocks-rule-2026-07-27/",
        "https://www.example.com/help/how-to-reset-a-router",
        "https://www.example.com/blog/products/new-launch",
        "https://www.example.com/news/profiles/mayor-retires",
        # Year-shaped numeric ids (Drupal's /node/<id>).
        "https://www.example.com/node/2015",
        "https://www.example.com/p/2041",
        # "page" is pagination only when a number follows it.
        "https://www.example.com/page/how-we-report",
        # Slugs that name a file type, and endings that are no file
        # type (news CMSs). courlan's own tests match ``-png`` and
        # ``.js`` anywhere in a URL and reject any ending it doesn't
        # know, so PR10b's first cut rejected all of these.
        "https://www.example.com/blog/how-to-compress-png-files",
        "https://www.example.com/blog/converting-jpg-to-webp",
        "https://www.example.com/blog/png-vs-jpg",
        "https://www.example.com/news/2026/10/14/apple-drops-png-support/",
        "https://www.example.com/blog/img-tags-explained",
        "https://www.example.com/blog/node.js-22-released",
        "https://www.example.com/news/national/council-vote/article68765432.ece",
        "https://www.example.com/city/news/council-vote/112233.cms",
        "https://www.example.com/rj/noticia/2026/10/18/council-vote.ghtml",
        "https://www.example.com/cgi-bin/news.pl?id=42",
        # Page endings of CMSs courlan doesn't know: nyc.gov's agency
        # press releases, JavaServer Faces, ColdFusion.
        "https://www.example.com/site/doh/about/press/pr2026/flu-shots-available.page",
        "https://www.example.com/news/item.jsf?id=123",
        "https://www.example.com/news/report.cfml",
        # A slug that keeps a dotted word, with a trailing slash (Hugo,
        # Jekyll pretty permalinks): the path has no ending.
        "https://www.example.com/blog/moving-to-fly.io/",
        "https://www.example.com/posts/notes-on-deno.land/",
        # A slug that ends in a software name, before a trailing slash
        # (Hugo, Eleventy) or under a page ending (Jekyll): a code
        # word there is part of the slug, not a file's ending.
        "https://www.example.com/posts/getting-started-with-three.js/",
        "https://www.example.com/posts/understanding-package.json/",
        "https://www.example.com/blog/why-i-moved-off-tailwind.css/",
        "https://www.example.com/blog/reading-a-pom.xml/",
        "https://www.example.com/2026/10/18/whats-new-in-vue.js.html",
    ],
)
def test_url_ok_keeps_articles(url):
    assert url_ok(url, SITE)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.example.com/library/riverlm3",
        "https://www.example.com/library/riverlm3-png-tools",
        "https://www.example.com/library/home",
        "https://www.example.com/library/why-we-chose-deno.land",
        "https://www.example.com/library/flu-shots-available.page",
        "https://www.example.com/library/getting-started-with-three.js/",
        "https://www.example.com/library/whats-new-in-vue.js.html",
    ],
)
def test_url_ok_takes_the_owners_link_pattern_as_the_article_signal(url):
    """Under the owner's ``link_pattern`` courlan isn't asked, nor its
    2-to-5-letter ending rule: the pattern says what an article is, as
    ``score_page`` takes it."""
    assert url_ok(url, SITE, owner_pattern=True)


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        # Without the owner's pattern, an unknown ending at the end of
        # the path is a file, as courlan reads it ...
        ("/blog/why-we-chose-deno.land", False),
        ("/parks/trails/maps/trail-1.gpx", False),
        # ... but a path that ends in ``/`` has no ending ...
        ("/blog/why-we-chose-deno.land/", True),
        # ... and a document or media ending is a file either way ...
        ("/files/annual-report.pdf/", False),
        ("/files/annual-report.pdf.html", False),
        # ... while a code word names a file only at the very end.
        ("/blog/whats-new-in-vue.js", False),
        ("/blog/whats-new-in-vue.js/", True),
        ("/blog/whats-new-in-vue.js.html", True),
    ],
)
def test_url_ok_reads_an_ending_only_at_the_end_of_the_path(path, ok):
    assert url_ok("https://www.example.com" + path, SITE) is ok


@pytest.mark.parametrize(
    "ending", ["jhtml", "jspx", "shtm", "dhtml", "phtm", "xhtm", "rhtml", "lasso", "nsf", "land"]
)
def test_url_ok_reads_a_sitemap_entrys_unknown_ending_as_a_page(ending):
    """A sitemap lists pages, and main fetched every entry: an ending of
    2 to 5 letters no list here knows (ATG's ``.jhtml``, ``.jspx``,
    ``.shtm``, Domino's ``.nsf``) is a page's there. A listing page's
    link keeps the rule, as courlan's ending test read it on main."""
    url = f"https://www.example.com/news/story-123-council-approves-plan.{ending}"
    assert url_ok(url, SITE, sitemap_entry=True)
    assert url_ok(url, SITE, sitemap_entry=True, section="/news/")
    assert not url_ok(url, SITE)


@pytest.mark.parametrize(
    "path",
    [
        "/uploads/trail-map.jpg",
        "/files/annual-report.pdf",
        "/files/annual-report.pdf.html",
        "/feed.xml",
        "/assets/app.js",
        "/img_1234",
        "/tag/parks/",
        "/about/",
    ],
)
def test_url_ok_keeps_its_other_rules_for_a_sitemap_entry(path):
    assert not url_ok("https://www.example.com" + path, SITE, sitemap_entry=True)


@pytest.mark.parametrize(
    "path",
    [
        "/news/council-approves-river-trail/index.html",
        "/council-approves-river-trail/index.html",
        "/news/20261020/index.html",
        "/news/council-approves-river-trail/default.aspx",
        "/news/council-approves-river-trail/index.php",
        "/news/2026/council-approves-river-trail/index.html",
        "/2026/10/20/politics/council-approves-river-trail/index.html",
    ],
)
@pytest.mark.parametrize("sitemap_entry", [False, True])
def test_url_ok_reads_a_folders_index_page_as_the_folder(path, sitemap_entry):
    """Round 7: a static or exported site publishes each story as a folder
    with its own index page. courlan reads an index page under a short
    path as a home page, and it was shown the path above the page as
    ``x/x/``, so ``/news/<slug>/index.html`` read as a home page and a
    feed of them as ``[no list]``. The folder is judged instead, as
    courlan judged the real path."""
    assert url_ok("https://www.example.com" + path, SITE, sitemap_entry=sitemap_entry)


@pytest.mark.parametrize(
    ("path", "section"),
    [
        # The site's own index page is the home page.
        ("/index.html", None),
        ("/default.aspx", None),
        # A folder that is no article's is judged as that folder.
        ("/about/index.html", None),
        ("/privacy/index.html", None),
        ("/tag/parks/index.html", None),
        ("/2026/10/index.html", None),
        ("/blog/2026/index.html", None),
        ("/category/news/index.html", "/category/news/"),
        # With no web page's ending it is no folder's index page, and
        # courlan judges it as before.
        ("/library/home", None),
        ("/blog/index", None),
    ],
)
def test_url_ok_keeps_its_rules_for_a_folders_index_page(path, section):
    url = "https://www.example.com" + path
    assert not url_ok(url, SITE, section=section)
    assert not url_ok(url, SITE, section=section, sitemap_entry=True)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.example.com/library/tag/python",
        "https://www.example.com/library/2026/07/",
        "https://www.example.com/library/riverlm3.png",
        "https://www.example.com/library/riverlm3.zip/",
        # A code word at the very end of the path is a file's ending,
        # under the owner's pattern too.
        "https://www.example.com/library/getting-started-with-three.js",
        "https://www.example.com/library/feed.xml",
        "https://www.example.com/library/about",
        "https://www.other-site.com/library/riverlm3",
    ],
)
def test_url_ok_keeps_its_own_rules_under_the_owners_link_pattern(url):
    assert not url_ok(url, SITE, owner_pattern=True)


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        # A news section filed under a static section (councils,
        # universities, companies): its articles are articles ...
        ("/about/news/council-approves-river-trail", True),
        ("/about-us/news/harvest-market-returns/", True),
        ("/about/press/2026/flu-shots-available", True),
        ("/about/media/news-releases/new-library-hours", True),
        ("/pages/blog/our-new-trail-map", True),
        # ... and so are those of a section named like no news section
        # (round 7): only a page directly under a static section is
        # static. score_page judges the rest of the static tree, as main
        # fetched every sitemap entry.
        ("/about-us/news-and-media/council-approves-river-trail/", True),
        ("/about/media-centre/council-approves-river-trail", True),
        ("/about/press-room/new-library-hours", True),
        ("/contact-us/notices/road-closure-on-main-street/", True),
        ("/about/team/dana-smith", True),
        ("/about/team/news", True),
        # The section page itself, the pages directly under a static
        # section and the news section's own listings are not.
        ("/about/news", False),
        ("/about/news/", False),
        ("/about-us/news-and-media/", False),
        ("/about/history", False),
        ("/about/team", False),
        ("/pages/our-story", False),
        ("/policies/refund-policy", False),
        ("/about/news/page/2/", False),
        ("/about/news/tag/parks/", False),
        ("/about/news/2026/10/", False),
        ("/about/news/contact", False),
    ],
)
def test_url_ok_reads_a_news_section_under_a_static_section(path, ok):
    assert url_ok("https://www.example.com" + path, SITE) is ok


@pytest.mark.parametrize(
    "path",
    [
        "/about/media-centre/council-approves-river-trail",
        "/about-us/news-and-media/council-approves-river-trail/",
        "/contact-us/notices/road-closure-on-main-street/",
    ],
)
@pytest.mark.parametrize("section", [None, "static section", "subsection"])
def test_url_ok_reads_a_page_deeper_in_a_static_section_with_or_without_its_section(path, section):
    """Round 7: a page below a static section's subsection is a candidate
    on every feed, not only on one that reads that subsection: a root
    feed's sitemap and a listing page with another list of links on it
    both find it."""
    url = "https://www.example.com" + path
    segments = path.strip("/").split("/")
    own = {
        "static section": f"/{segments[0]}/",
        "subsection": f"/{segments[0]}/{segments[1]}/",
    }.get(section)
    assert url_ok(url, SITE, section=own)
    assert url_ok(url, SITE, section=own, sitemap_entry=True)


@pytest.mark.parametrize(
    ("path", "section", "owner_pattern"),
    [
        ("/shop/journal/new-trail-shoes", "/shop/journal/", False),
        ("/shop/journal/new-trail-shoes", "/shop/journal/", True),
        ("/category/news/council-approves-river-trail", "/category/news/", False),
        ("/products/canvas-tote", "/products/", True),
        ("/members/dana", "/members/", True),
    ],
)
def test_url_ok_judges_only_the_segments_below_the_feeds_own_section(path, section, owner_pattern):
    """The feed reads a section the owner chose (its address's section,
    or its link pattern), so the segment rules don't judge that
    section's own segments: its pages are candidates, which
    ``score_page`` judges. Without the section they are not."""
    url = "https://www.example.com" + path
    assert url_ok(url, SITE, owner_pattern=owner_pattern, section=section)
    assert not url_ok(url, SITE, owner_pattern=owner_pattern)


@pytest.mark.parametrize(
    ("path", "section"),
    [
        # Outside the section the rules are whole.
        ("/about/", "/about/media-centre/"),
        ("/about/team", "/about/media-centre/"),
        ("/shop/canvas-tote", "/shop/journal/"),
        # The section page itself is the listing.
        ("/about/media-centre/", "/about/media-centre/"),
        ("/about/media-centre", "/about/media-centre/"),
        # Below it, the segments below the section, the page's own name
        # and the file endings are judged.
        ("/about/media-centre/tag/parks/", "/about/media-centre/"),
        ("/about/media-centre/page/2/", "/about/media-centre/"),
        ("/about/media-centre/contact", "/about/media-centre/"),
        ("/about/media-centre/2026/10/", "/about/media-centre/"),
        ("/about/media-centre/annual-report.pdf", "/about/media-centre/"),
        # A prefix that ends inside a segment owns only the whole
        # segments before it.
        ("/library/tags", "/library/ta"),
        ("/library/tag/python", "/library/t"),
    ],
)
def test_url_ok_keeps_its_rules_outside_and_below_the_feeds_own_section(path, section):
    assert not url_ok("https://www.example.com" + path, SITE, section=section)
    assert not url_ok("https://www.example.com" + path, SITE, owner_pattern=True, section=section)


def test_url_ok_keeps_a_loopback_host_that_is_the_sources_own():
    # courlan rejects an IP or localhost with a port; every loopback
    # test server has one.
    assert url_ok("http://127.0.0.1:8765/news/first-post", "http://127.0.0.1:8765/news/")
    assert url_ok("http://localhost:8080/blog/hello", "http://localhost:8080")
    # A different loopback host is another site.
    assert not url_ok("http://127.0.0.2:8765/news/first-post", "http://127.0.0.1:8765/")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.example.com/",
        "https://www.example.com/tag/python/",
        "https://www.example.com/category/news/local/",
        "https://www.example.com/author/dana/",
        "https://www.example.com/blog/page/2/",
        "https://www.example.com/page/2/",
        "https://www.example.com/archive/",
        "https://www.example.com/archives/",
        "https://www.example.com/archives/2026/07/",
        "https://www.example.com/users/dana",
        "https://www.example.com/forum/users",
        "https://www.example.com/?page_id=2",
        "https://www.example.com/?s=x",
        "https://www.example.com/index.php?page_id=2",
        "https://www.example.com/help",
        "https://www.example.com/legal/",
        "https://www.example.com/2026/",
        "https://www.example.com/2026/07/",
        "https://www.example.com/blog/2026/",
        # Without the slash courlan accepts a year, so the rule is ours.
        "https://www.example.com/2026",
        "https://www.example.com/blog/2026",
        "https://www.example.com/archives/2015",
        "https://www.example.com/blog/2026/07/",
        "https://www.example.com/news/2026/10/18/",
        "https://www.example.com/about/",
        "https://www.example.com/about/team",
        "https://www.example.com/contact-us",
        "https://www.example.com/pages/our-story",
        "https://www.example.com/products/canvas-tote",
        "https://www.example.com/search",
        "https://www.example.com/blog/?s=trail",
        "https://www.example.com/news/council-approves-river-trail/?replytocom=12",
        "https://www.example.com/2026/10/",
        "https://www.example.com/feed/",
        "https://www.example.com/wp-content/uploads/2026/10/trail.jpg",
        "https://www.example.com/files/annual-report.pdf",
        "https://www.example.com/wp-content/uploads/2026/10/trail.JPG",
        # Upper-case endings outside any rejected section: only the
        # (lower-cased) ending rejects them.
        "https://www.example.com/files/Trail-Map.JPG",
        "https://www.example.com/news/Episode-12.MP3",
        # Endings of 2 to 5 letters that are no web page's are files,
        # listed or not (map downloads, documents, video).
        "https://www.example.com/parks/trails/maps/trail-1.gpx",
        "https://www.example.com/parks/trails/maps/trail-2.kmz",
        "https://www.example.com/files/minutes-2026-10.rtf",
        "https://www.example.com/media/council-meeting.wmv",
        # A file behind a page ending, and a camera's file name as the
        # page (WordPress attachment pages).
        "https://www.example.com/files/doc.pdf.html",
        "https://www.example.com/blog/IMG_1234",
        "https://www.example.com/2026/10/img-1234/",
        "https://www.example.com/news/feed.xml",
        "https://www.example.com/assets/app.js",
        "https://www.example.com/download?file=annual-report.pdf",
        "https://www.example.com/blog/home",
        "https://www.example.com/index.html",
        "https://www.example.com/2026_10_01_archive.html",
        "https://www.example.com/kontakt",
        "https://www.other-site.com/news/a-story",
        "mailto:news@example.com",
    ],
)
def test_url_ok_rejects_pages_that_are_not_articles(url):
    assert not url_ok(url, SITE)


# ---------------------------------------------------------------------------
# Sitemaps
# ---------------------------------------------------------------------------


def test_wordpress_core_sitemaps_classify_on_the_subtype_token():
    classes = {loc.rsplit("/", 1)[-1]: classify_sitemap(loc) for loc, _ in _locs("wp-sitemap.xml")}
    assert classes == {
        "wp-sitemap-posts-post-1.xml": "post",
        # Holds "posts", but the subtype token is "page".
        "wp-sitemap-posts-page-1.xml": "page",
        "wp-sitemap-taxonomies-category-1.xml": "taxonomy",
        "wp-sitemap-taxonomies-post_tag-1.xml": "taxonomy",
        "wp-sitemap-users-1.xml": "author",
    }
    assert order_sitemaps(loc for loc, _ in _locs("wp-sitemap.xml")) == [
        "https://www.example.com/wp-sitemap-posts-post-1.xml",
    ]


def test_yoast_sitemaps_classify_and_the_page_sitemap_is_skipped():
    locs = [loc for loc, _ in _locs("yoast-sitemap_index.xml")]
    classes = {loc.rsplit("/", 1)[-1]: classify_sitemap(loc) for loc in locs}
    assert classes == {
        "post-sitemap.xml": "post",
        "post-sitemap2.xml": "post",
        "page-sitemap.xml": "page",
        "category-sitemap.xml": "taxonomy",
        "post_tag-sitemap.xml": "taxonomy",
        "author-sitemap.xml": "author",
        "attachment-sitemap.xml": "media",
        "product-sitemap.xml": "product",
        "product_cat-sitemap.xml": "taxonomy",
    }
    assert order_sitemaps(locs) == [
        "https://www.example.com/post-sitemap.xml",
        "https://www.example.com/post-sitemap2.xml",
    ]


def test_recently_edited_about_page_is_excluded_when_a_post_sitemap_exists():
    index = dict(_locs("yoast-sitemap_index.xml"))
    page_sitemap = "https://www.example.com/page-sitemap.xml"
    # The page sitemap is the newest thing on the site, and its newest
    # entry is the About page, newer than any post.
    assert max(index.values()) == index[page_sitemap]
    pages = sorted(_locs("yoast-page-sitemap.xml"), key=lambda e: e[1], reverse=True)
    newest_post = max(lastmod for _, lastmod in _locs("yoast-post-sitemap2.xml"))
    assert "https://www.example.com/about-us/" in [loc for loc, m in pages if m > newest_post]

    assert page_sitemap not in order_sitemaps(index)


def test_a_lone_page_sitemap_is_kept_and_its_entries_still_filtered():
    page_sitemap = "https://www.example.com/wp-sitemap-posts-page-1.xml"
    assert order_sitemaps([page_sitemap, "https://www.example.com/wp-sitemap-users-1.xml"]) == [
        page_sitemap,
    ]
    kept = [loc for loc, _ in _locs("wp-sitemap-posts-page-1.xml") if url_ok(loc, SITE)]
    assert kept == ["https://www.example.com/our-history/"]
    # And a page that gets past url_ok is still scored as a page.
    assert not _accepted(_fixture("yoast-about.html"), "https://www.example.com/our-history/")


def test_a_page_sitemap_reads_after_an_unknown_sitemap():
    # No post sitemap, so the page sitemap stays, but as the last
    # fallback: an unknown sitemap may hold the posts.
    assert order_sitemaps([
        "https://www.example.com/wp-sitemap-posts-page-1.xml",
        "https://www.example.com/sitemap-1.xml",
        "https://www.example.com/wp-sitemap-users-1.xml",
    ]) == [
        "https://www.example.com/sitemap-1.xml",
        "https://www.example.com/wp-sitemap-posts-page-1.xml",
    ]


def test_shopify_sitemaps_keep_only_the_blogs():
    locs = [loc for loc, _ in _locs("shopify-sitemap.xml")]
    assert [classify_sitemap(loc) for loc in locs] == ["product", "page", "taxonomy", "post"]
    assert order_sitemaps(locs) == ["https://shop.example.com/sitemap_blogs_1.xml"]


def test_news_sitemap_reads_first():
    locs = [loc for loc, _ in _locs("news-sitemap_index.xml")]
    assert order_sitemaps(locs) == [
        "https://www.example.com/news-sitemap.xml",
        "https://www.example.com/sitemaps/sitemap-posts-2026-10.xml",
        "https://www.example.com/sitemap-1.xml",
    ]
    # Its entries are articles.
    assert all(url_ok(loc, SITE) for loc, _ in _locs("news-sitemap.xml"))


@pytest.mark.parametrize(
    ("url", "cls"),
    [
        ("https://www.example.com/sitemap.xml", "other"),
        ("https://www.example.com/sitemap_index.xml", "other"),
        # Drupal pages a big sitemap with ?page=; that isn't a page sitemap.
        ("https://www.example.com/sitemap.xml?page=2", "other"),
        ("https://www.example.com/newssitemap.xml", "news"),
        ("https://www.example.com/sitemap-news.xml.gz", "news"),
        ("https://www.example.com/blog/sitemap.xml", "post"),
        # Whole words only: "heritage" holds no "tag".
        ("https://www.example.com/heritage-sitemap.xml", "other"),
        # WordPress core under Plain permalinks: the class is in the query.
        ("https://www.example.com/?sitemap=posts&sitemap-subtype=page", "page"),
        ("https://www.example.com/?sitemap=posts&sitemap-subtype=post&paged=2", "post"),
        ("https://www.example.com/?sitemap=taxonomies&sitemap-subtype=category", "taxonomy"),
        ("https://www.example.com/?sitemap=users", "author"),
    ],
)
def test_classify_sitemap_edge_cases(url, cls):
    assert classify_sitemap(url) == cls


@pytest.mark.parametrize(
    ("name", "cls"),
    [
        # A custom post type named with a media, product or user word
        # lists posts: the word only qualifies its last one (round 6).
        ("media_release-sitemap.xml", "other"),
        ("media-release-sitemap.xml", "other"),
        ("product_update-sitemap.xml", "other"),
        ("user_story-sitemap.xml", "post"),
        ("wp-sitemap-posts-media_release-1.xml", "post"),
        ("wp-sitemap-posts-product_update-1.xml", "post"),
        ("?sitemap=posts&sitemap-subtype=media_release", "post"),
        # Taxonomies keep their class by their last word, or by WordPress
        # core's taxonomy provider, whatever the taxonomy is called.
        ("product_tag-sitemap.xml", "taxonomy"),
        ("tribe_events_cat-sitemap.xml", "taxonomy"),
        ("wp-sitemap-taxonomies-post_format-1.xml", "taxonomy"),
        ("wp-sitemap-taxonomies-region-1.xml", "taxonomy"),
        ("sitemap-tax-post_tag.xml", "taxonomy"),
        # A product or author sitemap is still one, and so is an
        # attachment sitemap ...
        ("attachment-sitemap.xml", "media"),
        ("store-products-sitemap.xml", "product"),
        ("sitemap_products_1.xml", "product"),
        ("wp-sitemap-posts-product-1.xml", "product"),
        ("sitemap-authors.xml", "author"),
        # ... but a video or image sitemap lists the pages that show them
        # (and a ``video`` post type's posts), as main read them, and
        # "media" names a site's media section (round 7).
        ("video-sitemap.xml", "other"),
        ("sitemap-images.xml", "other"),
        ("sitemap-media.xml", "other"),
        ("media/sitemap.xml", "other"),
        # After "sitemap" the rest counts by its last word too, after
        # WordPress core's provider word (round 7: a post type's name may
        # hold a "-").
        ("sitemap-pt-post-2026-10.xml", "post"),
        ("sitemap-media-releases.xml", "other"),
        ("sitemap-tax-category.xml", "taxonomy"),
        ("wp-sitemap-posts-media-release-1.xml", "post"),
        ("wp-sitemap-posts-product-update-1.xml", "post"),
        ("wp-sitemap-posts-user-story-1.xml", "post"),
        ("wp-sitemap-posts-page-1.xml", "page"),
        ("wp-sitemap-posts-product-1.xml", "product"),
        ("wp-sitemap-taxonomies-event-region-1.xml", "taxonomy"),
        ("wp-sitemap-users-1.xml", "author"),
        ("?sitemap=posts&sitemap-subtype=media-release", "post"),
        ("blog-posts-sitemap.xml", "post"),
        # A name with no "sitemap" in it counts by its last word too, and
        # so does the section a sitemap is filed under.
        ("media-releases.xml", "other"),
        ("sitemaps/product-updates.xml", "other"),
        ("media-centre/sitemap.xml", "other"),
        ("video-news/sitemap.xml", "news"),
        ("videos.xml", "other"),
        ("attachments.xml", "media"),
        ("shop/sitemap.xml", "product"),
        # A taxonomy named by a word this module doesn't know is read, as
        # main read every sitemap (score_page then rejects a brand's
        # archive pages, one fetch each).
        ("product_brand-sitemap.xml", "other"),
    ],
)
def test_a_sitemap_name_of_several_words_counts_by_its_last_word(name, cls):
    assert classify_sitemap(f"https://www.example.com/{name}") == cls


# ---------------------------------------------------------------------------
# score_page
# ---------------------------------------------------------------------------


def test_yoast_post_is_an_article():
    score, reasons = score_page(
        _fixture("yoast-post.html"), "https://www.example.com/news/council-approves-river-trail/"
    )
    assert score >= ACCEPT_SCORE
    assert "+3 json-ld article" in reasons


def test_dated_og_website_post_is_accepted():
    # Many builders tag every page og:type=website; the date saves it.
    score, reasons = score_page(
        _fixture("dated-website-post.html"), "https://www.example.com/blog/spring-planting-guide"
    )
    assert score >= ACCEPT_SCORE, reasons
    assert "+1 dated 2026-10-14" in reasons
    assert not any("website" in r for r in reasons)


def test_about_page_is_rejected():
    score, reasons = score_page(_fixture("about.html"), "https://www.example.com/about")
    assert score < ACCEPT_SCORE
    assert "-3 og:type website, undated" in reasons


def test_yoast_about_page_with_og_article_is_rejected():
    score, reasons = score_page(_fixture("yoast-about.html"), "https://www.example.com/about-us/")
    assert score < ACCEPT_SCORE
    # og:type=article earns nothing next to JSON-LD AboutPage, and the
    # WebPage node's datePublished is not an article date.
    assert not any(r.startswith("+3") for r in reasons)
    assert "-1 no date" in reasons
    assert "-3 json-ld aboutpage/website, undated" in reasons


def test_og_article_post_without_json_ld_is_accepted():
    # Undated, so og:type=article is what carries it: 3 - 1 + 1.
    score, reasons = score_page(
        _fixture("og-article-post.html"), "https://www.example.com/posts/squeaky-bike-chain/"
    )
    assert "+3 og:type article" in reasons
    assert "-1 no date" in reasons
    assert score >= ACCEPT_SCORE, reasons


def test_undated_og_article_with_a_site_wide_json_ld_website_is_rejected():
    # The spec's -3 for a JSON-LD WebSite applies even next to
    # og:type=article when the page is undated: 3 - 3 - 1 + 1 = 0. This
    # is the false negative left open for PR10b's review and the owner's
    # VPS probe (docs/ux-notes/PR10a.md, "Open items"). Changing it is a
    # spec change, so this test pins today's rule on purpose.
    html = _page(
        published=None,
        jsonld='{"@context":"https://schema.org","@type":"WebSite","name":"Example Town News"}',
    )
    score, reasons = score_page(html, STORY_URL, text="x" * 650)
    assert reasons == [
        "+3 og:type article", "-3 json-ld website, undated", "-1 no date", "+1 650 characters",
    ]
    assert score == 0


def test_prefixed_json_ld_types_count():
    for prefix in ("https://schema.org/", "http://schema.org/", "schema:"):
        html = _page(
            og_type=None,
            published=None,
            jsonld=(
                '{"@context":"https://schema.org","@type":"' + prefix + 'NewsArticle",'
                '"datePublished":"2026-07-27"}'
            ),
        )
        score, reasons = score_page(html, STORY_URL, text="x" * 450)
        assert reasons == ["+3 json-ld newsarticle", "+1 dated 2026-07-27"], prefix
        assert score >= ACCEPT_SCORE


def test_long_listing_with_dated_cards_is_rejected():
    # Six cards, each with a time.entry-date.published and a full
    # excerpt: the first card's date is not the listing's.
    html = _fixture("listing-long.html")
    score, reasons = score_page(html, "https://www.example.com/blog/")
    assert any(r.startswith("+1") and r.endswith("characters") for r in reasons)
    assert "-3 og:type website, undated" in reasons
    assert score < ACCEPT_SCORE
    # The same cards as divs, with no <article> and no <h1>: their dates
    # disagree, so none is the page's.
    divs = (
        html.replace("<article", "<div").replace("</article>", "</div>")
        .replace('<header class="page-header"><h1 class="page-title">Blog</h1></header>', "")
    )
    score, reasons = score_page(divs, "https://www.example.com/blog/")
    assert "-1 no date" in reasons
    assert score < ACCEPT_SCORE
    # Cards all from one day still make a listing: more than one
    # <article>, and no <h1> to say which is the page's.
    no_h1 = html.replace('<header class="page-header"><h1 class="page-title">Blog</h1></header>', "")
    same_day = re.sub(r'datetime="[^"]+"', 'datetime="2026-10-18T09:30:11-07:00"', no_h1)
    _, reasons = score_page(same_day, "https://www.example.com/blog/")
    assert "-1 no date" in reasons
    # A theme that titles every card with an <h1>.
    h1_cards = no_h1.replace('<h2 class="entry-title">', '<h1 class="entry-title">').replace(
        "</a></h2>", "</a></h1>"
    )
    _, reasons = score_page(h1_cards, "https://www.example.com/blog/")
    assert "-1 no date" in reasons


def test_a_post_with_related_cards_keeps_its_own_date():
    # The post's <h1> and date sit in its <article>; the related posts
    # below are <article>s with dates of their own.
    related = "".join(
        f'<article class="related"><h3><a href="/news/older-{n}">Older {n}</a></h3>'
        f'<time class="entry-date published" datetime="2026-09-0{n}">Sep {n}</time></article>'
        for n in range(1, 4)
    )
    html = _page(og_type="website", published=None, body=(
        '<main><article class="post"><header><h1>Council approves the trail</h1>'
        '<time class="entry-date published" datetime="2026-10-14">Oct 14</time></header>'
        f'<p>Story.</p></article><section class="related-posts">{related}</section></main>'
    ))
    score, reasons = score_page(html, STORY_URL, text="x" * 650)
    assert "+1 dated 2026-10-14" in reasons
    assert score >= ACCEPT_SCORE


def test_listing_microdata_dates_are_not_the_pages():
    cards = "".join(
        f'<div itemscope itemtype="https://schema.org/BlogPosting">'
        f'<h2 itemprop="headline"><a href="/blog/post-{n}">Post {n}</a></h2>'
        f'<meta itemprop="datePublished" content="2026-10-{10 + n}"></div>'
        for n in range(4)
    )
    html = _page(og_type="website", published=None, body=f"<main>{cards}</main>")
    _, reasons = score_page(html, "https://www.example.com/blog/", text="x" * 650)
    assert "-1 no date" in reasons
    # One microdata date on a one-article page is the page's.
    single = _page(og_type="website", published=None, body=(
        '<div itemscope itemtype="https://schema.org/BlogPosting"><h1>Post</h1>'
        '<meta itemprop="datePublished" content="2026-10-11"></div>'
    ))
    _, reasons = score_page(single, STORY_URL, text="x" * 650)
    assert "+1 dated 2026-10-11" in reasons


def test_a_date_in_the_url_counts_as_dated():
    html = _page(og_type="website", published=None)
    score, reasons = score_page(
        html, "https://www.example.com/2026/10/18/council-approves-river-trail", text="x" * 650
    )
    assert "+1 date in URL" in reasons
    assert not any("website" in r for r in reasons)
    assert score >= ACCEPT_SCORE, reasons


@pytest.mark.parametrize(
    ("body", "dated"),
    [
        # A class-less <time> in the article's header is its date.
        (('<article><header><h1>Council approves the trail</h1>'
          '<time datetime="2026-10-14">Oct 14</time></header><p>Story.</p></article>'), True),
        # So is one anywhere in the h1's own article: Astro, Next.js and
        # Webflow templates print the date in a div under the title.
        (('<main><article><h1>Council approves the trail</h1>'
          '<p><time datetime="2026-10-14">Oct 14</time></p><p>Story.</p></article></main>'), True),
        (('<article><div class="date"><time datetime="2026-10-14">Oct 14</time></div>'
          '<h1>Council approves the trail</h1><p>Story.</p></article>'), True),
        # In the article's footer it is not ("updated", comment times).
        (('<article><header><h1>Council approves the trail</h1></header><p>Story.</p>'
          '<footer><time datetime="2026-10-14">Oct 14</time></footer></article>'), False),
        # Nor in its related-posts aside, nor one marked as an update.
        (('<article><h1>Council approves the trail</h1><p>Story.</p><aside>'
          '<time datetime="2026-10-14">Oct 14</time></aside></article>'), False),
        (('<article><h1>Council approves the trail</h1><p>Updated '
          '<time class="updated" datetime="2026-10-14">Oct 14</time></p></article>'), False),
        # Nor outside the article.
        (('<article><header><h1>Council approves the trail</h1></header><p>Story.</p></article>'
          '<div class="sidebar"><time datetime="2026-10-14">Oct 14</time></div>'), False),
        # Nor a card's, when several articles hold an h1 (a listing).
        (('<article><h1><a href="/a">A</a></h1><time datetime="2026-10-14">Oct 14</time></article>'
          '<article><h1><a href="/b">B</a></h1><time datetime="2026-10-13">Oct 13</time></article>'),
         False),
    ],
)
def test_a_class_less_time_counts_only_as_the_articles_own_date(body, dated):
    html = _page(og_type="website", published=None, body=body)
    _, reasons = score_page(html, STORY_URL, text="x" * 650)
    assert ("+1 dated 2026-10-14" in reasons) is dated
    assert ("-1 no date" in reasons) is not dated


def test_an_astro_style_post_is_accepted_without_a_link_pattern():
    """og:type=website on every page, no JSON-LD, and the date in a
    plain <time> inside the post's article: a real article on a site
    with no feed (PR10b review)."""
    score, reasons = score_page(
        _fixture("astro-post.html"), "https://www.example.com/blog/rebuilding-the-river-trail-bridge/"
    )
    assert score >= ACCEPT_SCORE, reasons
    assert "+1 dated 2026-10-14" in reasons


def test_a_page_with_a_plain_time_and_no_og_tags_is_accepted():
    html = (
        "<html><head><title>Council approves the trail</title></head><body><main><article>"
        "<h1>Council approves the trail</h1><p><time datetime='2026-10-14'>Oct 14</time></p>"
        f"<p>{'The extension adds four kilometres of path. ' * 16}</p></article></main></body></html>"
    )
    score, reasons = score_page(html, STORY_URL)
    assert score >= ACCEPT_SCORE, reasons


def test_the_lists_date_dates_a_page_that_carries_none():
    """A listing card's date (or a news sitemap's) is the site saying
    when the page was published: it counts like a date on the page."""
    html = _page(og_type="website", published=None)
    listed = dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc)
    score, reasons = score_page(html, STORY_URL, text="x" * 650)
    assert score < ACCEPT_SCORE
    score, reasons = score_page(html, STORY_URL, text="x" * 650, listed_date=listed)
    assert "+1 listed 2026-10-14" in reasons
    assert not any("website" in r for r in reasons)
    assert score >= ACCEPT_SCORE, reasons
    # The page's own date wins over the list's.
    dated = _page(og_type="website", published="2026-10-12T08:00:00+00:00")
    _, reasons = score_page(dated, STORY_URL, text="x" * 650, listed_date=listed)
    assert "+1 dated 2026-10-12" in reasons


@pytest.mark.parametrize(("fixture", "url"), [
    ("about.html", "https://www.example.com/about"),
    ("yoast-about.html", "https://www.example.com/about-us/"),
    ("listing-long.html", "https://www.example.com/blog/"),
    ("listing.html", "https://www.example.com/blog/"),
])
def test_static_pages_and_listings_stay_rejected_with_the_wider_date_rule(fixture, url):
    score, reasons = score_page(_fixture(fixture), url)
    assert score < ACCEPT_SCORE, reasons


_LONG_BODY = "<p>" + "The extension adds four kilometres of path along the north bank. " * 12 + "</p>"


def _builder_post(meta: str, *, og_type: str | None = "website") -> str:
    """A site builder's post page: no ``<article>``, no JSON-LD, no date
    metadata; ``meta`` is what follows its ``<h1>``."""
    head = "<title>Council approves the river trail | Example</title>"
    if og_type:
        head += f'<meta property="og:type" content="{og_type}">'
    h1 = "<h1 class='heading'>Council approves the river trail</h1>"
    return (
        f"<html><head>{head}</head><body><div class='navbar'><a href='/'>Example</a></div>"
        f"<div class='section'><div class='container'>{h1}{meta}"
        f"<div class='rich-text'>{_LONG_BODY}</div></div></div>"
        "<div class='footer'><p>&copy; 2026 Example</p></div></body></html>"
    )


@pytest.mark.parametrize(
    "meta",
    [
        # Webflow: the date printed as text right after the title.
        "<div class='blog-date'>October 14, 2026</div>",
        "<div class='blog-date'>Oct. 14th, 2026</div>",
        "<div class='blog-date'>14 October 2026</div>",
        "<div class='blog-date'>Wednesday, October 14, 2026</div>",
        "<div class='blog-date'>Posted on October 14, 2026</div>",
        "<p>2026-10-14</p>",
        "<div class='meta'><span>October 14, 2026</span><span>5 min read</span></div>",
        # A WordPress block theme: a plain <time> beside the title in
        # its container, with no <article> around them.
        "<div class='wp-block-post-date'><time datetime='2026-10-14T09:00:00+00:00'>Oct 14</time></div>",
    ],
)
@pytest.mark.parametrize("og_type", ["website", None])
def test_a_builder_post_dated_next_to_its_title_is_an_article(meta, og_type):
    """Round 7: Webflow tags every page og:type=website and prints a post's
    date as text, and a WordPress block theme prints it in a plain
    ``<time>`` beside an ``<h1>`` with no ``<article>``. Both pages read
    as undated, so a feed of them stored nothing, where main stored every
    post."""
    score, reasons = score_page(_builder_post(meta, og_type=og_type), STORY_URL)
    assert "+1 dated 2026-10-14" in reasons
    assert score >= ACCEPT_SCORE, reasons


@pytest.mark.parametrize(
    "meta",
    [
        # Nothing next to the title, or no date there.
        "",
        "<p class='lead'>Who we are and how the gym started.</p>",
        # An update is not when the page was published.
        "<div class='meta'>Updated October 14, 2026</div>",
        # A paragraph is no line of post meta, whatever it starts with.
        "<p>" + "October 14, 2015 was the day we opened in a warehouse by the river. " * 3 + "</p>",
        "<div><p>Founded</p><p>October 14, 2015</p></div>",
        "<div class='meta'>Founded October 14, 2015</div>",
        # A listing built of div cards: several <time>s beside its title.
        "".join(
            f"<div class='card'><h2><a href='/blog/post-{n}'>Post {n}</a></h2>"
            f"<time datetime='2026-10-{14 - n:02d}'>Oct {14 - n}</time></div>"
            for n in range(3)
        ),
        # Nor one that only looks like a date.
        "<div class='blog-date'>Octember 14, 2026</div>",
        "<div class='blog-date'>October 32, 2026</div>",
    ],
)
def test_a_builder_page_with_no_date_next_to_its_title_stays_undated(meta):
    score, reasons = score_page(_builder_post(meta), STORY_URL)
    assert "-1 no date" in reasons
    assert score < ACCEPT_SCORE, reasons


@pytest.mark.parametrize(
    "main",
    [
        # A news page that prints today's date under its title, above
        # its <article> cards.
        "<h1>News</h1><p>Wednesday, October 14, 2026</p>"
        + "".join(
            f"<article><h2><a href='/news/story-{n}'>Story {n}</a></h2>"
            f"<p>{'Excerpt. ' * 20}</p></article>"
            for n in range(3)
        ),
        # A listing whose div cards title each post with an <h1>.
        "".join(
            f"<div class='card'><h1><a href='/news/story-{n}'>Story {n}</a></h1>"
            f"<p>October {14 - n}, 2026</p><p>{'Excerpt. ' * 20}</p></div>"
            for n in range(3)
        ),
    ],
    ids=["article cards", "several h1"],
)
def test_a_date_next_to_the_title_counts_only_on_a_page_of_one_title_and_no_article(main):
    """A listing is no story: with ``<article>`` cards, or with an
    ``<h1>`` on each card, the date by its first title is not the
    page's."""
    html = (
        "<html><head><title>News | Example</title><meta property='og:type' content='website'>"
        f"</head><body><main>{main}</main></body></html>"
    )
    score, reasons = score_page(html, "https://www.example.com/news/")
    assert "-1 no date" in reasons
    assert score < ACCEPT_SCORE, reasons


def test_home_page_redirect_is_rejected():
    score, reasons = score_page(
        _fixture("home.html"),
        "https://www.example.com/2026/05/04/old-post/",
        final_url="https://www.example.com/",
    )
    assert score < ACCEPT_SCORE
    assert "-3 redirected to /" in reasons
    assert "-3 title is the site name" in reasons


def test_redirect_to_the_same_path_is_not_a_redirect():
    url = "http://www.example.com/news/council-approves-river-trail"
    _, reasons = score_page(
        _fixture("yoast-post.html"),
        url,
        final_url="https://www.example.com/news/council-approves-river-trail/",
    )
    assert not any("redirected" in r for r in reasons)


def test_soft_404_is_rejected():
    score, reasons = score_page(_fixture("soft-404.html"), "https://www.example.com/news/gone/")
    assert score < ACCEPT_SCORE
    assert "-3 soft 404 title" in reasons


# 450 characters: neither +1 nor -2 for length, so a dated og:type=article
# page scores 4 and only the title can reject it. (At 600+ a fully
# marked-up page scores 5, and the -3 alone leaves it at 2.)
_MID_TEXT = "x" * 450


@pytest.mark.parametrize("owner_configured", [False, True])
@pytest.mark.parametrize(
    "title",
    [
        "404",
        "404 – Example Town News",
        "Error 404 | Example Town News",
        "Example Town News | 404",
        "404 Not Found",
        "404: Page not found",
        "404 Error",
    ],
)
def test_a_404_title_is_a_soft_404(title, owner_configured):
    html = _page(title=title)
    assert _accepted(_page(), STORY_URL, text=_MID_TEXT, owner_configured=owner_configured)
    score, reasons = score_page(html, STORY_URL, text=_MID_TEXT, owner_configured=owner_configured)
    assert "-3 soft 404 title" in reasons
    assert score < ACCEPT_SCORE


@pytest.mark.parametrize("owner_configured", [False, True])
@pytest.mark.parametrize(
    ("title", "site_name", "jsonld"),
    [
        ("404 errors: how to fix them | Town News", "Town News", None),
        ("Error 404 explained | Town News", "Town News", None),
        # A site whose own name reads as a not-found title.
        ("Our autumn issue | Not Found", "Not Found", None),
        ("FBI story | 404 Media", "404 Media",
         ('{"@context":"https://schema.org","@type":"NewsArticle",'
          '"datePublished":"2026-07-27T12:00:00Z"}')),
    ],
)
def test_a_title_that_only_mentions_404_is_not_a_soft_404(title, site_name, jsonld, owner_configured):
    html = _page(title=title, site_name=site_name, jsonld=jsonld)
    score, reasons = score_page(html, STORY_URL, text=_MID_TEXT, owner_configured=owner_configured)
    assert not any("404" in r for r in reasons)
    assert score >= ACCEPT_SCORE, reasons


def test_foreign_canonical_counts_against_a_page():
    _, reasons = score_page(
        _fixture("foreign-canonical.html"), "https://www.example.com/news/wildfire-order/"
    )
    assert "-2 canonical on www.wire.example.org" in reasons
    # A canonical on another host of the same site is not foreign.
    _, reasons = score_page(
        _fixture("yoast-post.html"), "https://example.com/news/council-approves-river-trail/"
    )
    assert not any("canonical" in r for r in reasons)


def test_undated_library_page_is_accepted_under_a_link_pattern():
    html = _fixture("ollama-library-model.html")
    url = "https://models.example.com/library/riverlm3"
    # No article type, no date: on its own it is not an article...
    assert not _accepted(html, url)
    # ...but the owner's link pattern is the article signal.
    score, reasons = score_page(html, url, owner_configured=True)
    assert score >= ACCEPT_SCORE, reasons


@pytest.mark.parametrize(
    ("fixture", "url", "final_url", "reason"),
    [
        ("soft-404.html", "https://www.example.com/library/gone", None, "-3 soft 404 title"),
        ("home.html", "https://www.example.com/library/old", "https://www.example.com/",
         "-3 redirected to /"),
        ("foreign-canonical.html", "https://www.example.com/library/copy", None,
         "-2 canonical on www.wire.example.org"),
    ],
)
def test_owner_configured_keeps_the_hard_rejects(fixture, url, final_url, reason):
    score, reasons = score_page(_fixture(fixture), url, final_url=final_url, owner_configured=True)
    assert score < ACCEPT_SCORE
    assert reason in reasons


def test_owner_configured_rejects_a_page_under_300_characters():
    html = _fixture("ollama-library-model.html")
    url = "https://models.example.com/library/riverlm3"
    score, reasons = score_page(html, url, text="A compact open model.", owner_configured=True)
    assert score < ACCEPT_SCORE
    assert "-2 only 21 characters" in reasons
    # Kind and date scoring are skipped entirely.
    assert not any("date" in r or "og:type" in r for r in reasons)


@pytest.mark.parametrize(
    ("length", "accepted"),
    [(150, False), (299, False), (300, True), (450, True)],
)
def test_a_short_page_loses_two_points(length, accepted):
    # An undated JSON-LD Article: 3 - 1 = 2 before the length rule.
    html = _page(
        og_type=None,
        published=None,
        jsonld='{"@context":"https://schema.org","@type":"Article","headline":"Trail"}',
    )
    score, reasons = score_page(html, STORY_URL, text="x" * length)
    assert (f"-2 only {length} characters" in reasons) is not accepted
    assert (score >= ACCEPT_SCORE) is accepted, reasons


def test_text_passed_in_is_used_instead_of_extracting():
    html = _fixture("yoast-post.html")
    url = "https://www.example.com/news/council-approves-river-trail/"
    _, reasons = score_page(html, url, text="x" * 650)
    assert "+1 650 characters" in reasons


def test_empty_page_scores_below_acceptance():
    score, _ = score_page("", "https://www.example.com/news/x")
    assert score < ACCEPT_SCORE


# ---------------------------------------------------------------------------
# listing_links
# ---------------------------------------------------------------------------


def test_listing_links_takes_the_repeating_post_list():
    links = listing_links(_fixture("listing.html"), "https://www.example.com/blog/")
    base = "https://www.example.com/blog/"
    assert links == [
        Candidate(
            base + "2026/10/18/council-approves-river-trail/",
            dt.datetime(2026, 10, 18, 16, 30, 11, tzinfo=dt.timezone.utc),
            "Council approves the river trail extension",
        ),
        Candidate(
            base + "2026/10/12/harvest-market-returns/",
            dt.datetime(2026, 10, 12, 21, 2, 45, tzinfo=dt.timezone.utc),
            "The harvest market returns to the square",
        ),
        # No <time> in this card.
        Candidate(base + "2026/09/30/new-bus-routes/", None, "Three new bus routes start Monday"),
        Candidate(
            base + "2026/09/22/school-garden-wins-grant/",
            dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc),
            "School garden wins a provincial grant",
        ),
        Candidate(
            base + "2026/09/15/rowing-club-hosts-regatta/",
            dt.datetime(2026, 9, 15, 8, tzinfo=dt.timezone.utc),
            "Rowing club hosts its first regatta",
        ),
    ]


def test_listing_links_resolves_against_base():
    # The fixture's hrefs are relative to <base href=".../blog/">. Read
    # as if it sat elsewhere on the site, they still land under /blog/.
    links = listing_links(_fixture("listing.html"), "https://www.example.com/news/archive-copy")
    assert links and all(c.url.startswith("https://www.example.com/blog/2026/") for c in links)


def test_listing_links_drops_chrome_and_foreign_links():
    urls = {c.url for c in listing_links(_fixture("listing.html"), "https://www.example.com/blog/")}
    assert not any("popular" in u or "footer" in u or "menu-link" in u for u in urls)
    assert not any("partner" in u for u in urls)


def test_listing_links_needs_three_distinct_links():
    html = """<html><body><main>
      <article><h2><a href="/news/one">One</a></h2></article>
      <article><h2><a href="/news/two">Two</a></h2></article>
      <article><h2><a href="/news/two#comments">Two again</a></h2></article>
    </main></body></html>"""
    assert listing_links(html, "https://www.example.com/news/") == []


def test_listing_links_takes_the_post_cards_over_a_larger_table_of_downloads():
    """A parks page lists 4 news cards and a table of 8 trail-map
    downloads. The downloads are files (endings no web page has), so
    they never form a group, and the cards are the list."""
    cards = "".join(
        f'<article><h2><a href="/parks/trails/news/trail-update-{n}">Trail update {n}</a></h2></article>'
        for n in range(4)
    )
    rows = "".join(
        f'<tr><td>Trail {n}</td><td><a href="/parks/trails/maps/trail-{n}.{ext}">Download</a></td></tr>'
        for n, ext in enumerate(["gpx", "kmz", "kml", "gpx", "kmz", "kml", "gpx", "kmz"])
    )
    html = f"<html><body><main><div>{cards}</div><table>{rows}</table></main></body></html>"
    page = "https://www.example.ca/parks/trails"
    assert [c.url for c in listing_links(html, page)] == [
        f"https://www.example.ca/parks/trails/news/trail-update-{n}" for n in range(4)
    ]


def test_listing_links_prefers_title_links_over_image_links_to_the_same_posts():
    # The thumbnail link comes first in each card and repeats as often
    # as the title link; the longer text decides the tie.
    cards = "".join(
        f'<li><a class="thumb" href="/news/{slug}"><img src="/i/{slug}.jpg" alt=""></a>'
        f'<h3><a href="/news/{slug}">{title}</a></h3></li>'
        for slug, title in [("alpha", "Alpha story"), ("beta", "Beta story"), ("gamma", "Gamma story")]
    )
    html = f"<html><body><main><ul>{cards}</ul></main></body></html>"
    assert [c.title for c in listing_links(html, "https://www.example.com/news/")] == [
        "Alpha story", "Beta story", "Gamma story",
    ]


def test_listing_links_keeps_post_card_headers_without_main():
    # Cards in plain divs with their own <header>: only the page-level
    # header (outside every sectioning element) is chrome.
    html = """<html><body>
      <header><a href="/news/menu-one">M1</a><a href="/news/menu-two">M2</a>
        <a href="/news/menu-three">M3</a><a href="/news/menu-four">M4</a></header>
      <div class="posts">
        <section class="post"><header><h2><a href="/news/alpha">Alpha story</a></h2></header></section>
        <section class="post"><header><h2><a href="/news/beta">Beta story</a></h2></header></section>
        <section class="post"><header><h2><a href="/news/gamma">Gamma story</a></h2></header></section>
      </div>
    </body></html>"""
    assert [c.url for c in listing_links(html, "https://www.example.com/news/")] == [
        "https://www.example.com/news/alpha",
        "https://www.example.com/news/beta",
        "https://www.example.com/news/gamma",
    ]


@pytest.mark.parametrize("role_main", [True, False])
def test_listing_links_keeps_div_card_headers(role_main):
    # Div cards with their own <header><h2><a> and <time>, under a div
    # with role="main" (an ARIA scope), or under no scope at all (their
    # headers repeat card by card, unlike the page's one header).
    html = _fixture("div-cards-listing.html")
    if not role_main:
        html = html.replace(' role="main"', "")
    links = listing_links(html, "https://www.example.com/blog/")
    base = "https://www.example.com/blog/"
    assert [(c.url, c.title) for c in links] == [
        (base + "council-approves-river-trail", "Council approves the river trail extension"),
        (base + "harvest-market-returns", "The harvest market returns to the square"),
        (base + "new-bus-routes", "Three new bus routes start Monday"),
        (base + "school-garden-wins-grant", "School garden wins a provincial grant"),
        (base + "rowing-club-hosts-regatta", "Rowing club hosts its first regatta"),
    ]
    assert [c.published.date().isoformat() for c in links] == [
        "2026-10-18", "2026-10-12", "2026-09-30", "2026-09-22", "2026-09-15",
    ]


def test_listing_links_keeps_a_header_inside_an_aria_main_region():
    # ARIA scopes a header by role as well as by tag: under role="main"
    # it is the region's header, not the page's banner.
    html = """<html><body>
      <header><a href="/news/menu-one">M1</a><a href="/news/menu-two">M2</a>
        <a href="/news/menu-three">M3</a><a href="/news/menu-four">M4</a></header>
      <div id="content" role="main">
        <header class="top-stories"><ul>
          <li><a href="/news/alpha">Alpha story</a></li>
          <li><a href="/news/beta">Beta story</a></li>
          <li><a href="/news/gamma">Gamma story</a></li>
        </ul></header>
      </div>
    </body></html>"""
    assert [c.title for c in listing_links(html, "https://www.example.com/news/")] == [
        "Alpha story", "Beta story", "Gamma story",
    ]
