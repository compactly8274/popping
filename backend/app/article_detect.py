"""Tell a website's articles apart from the rest of its pages.

A ``type="generic_scrape"`` source reads a site that has no feed: its
sitemap, or a listing page. Both list far more than articles — About,
Contact, tag, author and shop pages sit right next to the posts, and a
page sitemap often sorts first because static pages get edited. This
module holds the tests that keep those out:

- ``url_ok`` — a cheap yes/no on a candidate URL, before any fetch.
- ``classify_sitemap`` / ``order_sitemaps`` — which of a site's
  sitemaps to read, and in what order.
- ``score_page`` — whether a fetched page is an article.
- ``listing_links`` — the article links on a listing page.

Pure: no network, no database, no process-local state. ``courlan``,
``tld``, ``lxml`` and ``trafilatura`` are used only as parsers. The
callers (``app.sources.generic_scrape``, ``app.feed_autodiscovery``)
do the SSRF-guarded fetching.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from email.utils import parsedate_to_datetime
from typing import Iterable, Iterator, NamedTuple
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

import courlan
import lxml.html
import tld
from lxml import etree

# A page is accepted at this score or more (see ``score_page``).
ACCEPT_SCORE = 2


class Candidate(NamedTuple):
    """One article link from a site's own list: a listing page's card or
    a sitemap entry.

    - ``published``: when the list says the page was published, a
      listing card's ``<time datetime>`` or a news sitemap's
      ``<news:publication_date>``. A date the site gives for that page,
      so ``score_page`` counts it (``listed_date``).
    - ``title``: the card's link text or a news sitemap's title.
    - ``modified``: a sitemap's ``<lastmod>``. When the page last
      changed, not when it was published (static pages get edited), so
      it orders a sitemap but never dates a page. A page was published
      no later than it.
    - ``place``: a sitemap entry's place in the site's own order, the
      sitemap documents as the crawl read them, each top to bottom
      (None for a listing card, whose list is in its own order).
      Candidates are sorted by date, which moves every undated entry
      last, so this is the only record of where the site listed it.
    """

    url: str
    published: dt.datetime | None
    title: str | None
    modified: dt.datetime | None = None
    place: int | None = None


# ---------------------------------------------------------------------------
# Sites and hosts
# ---------------------------------------------------------------------------


def _host(url_or_host: str) -> str:
    """Lower-case host of a URL, or the value itself when it is a bare
    host (``example.com``, ``127.0.0.1:8080`` keeps no port)."""
    value = url_or_host.strip()
    if "://" not in value:
        value = "http://" + value
    try:
        return (urlsplit(value).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _site(host: str) -> str:
    """The registrable domain (``news.bbc.co.uk`` -> ``bbc.co.uk``) from
    ``tld``'s bundled public-suffix list. An IP, ``localhost`` or an
    unknown suffix has none, so the host itself is the site."""
    if not host:
        return ""
    return tld.get_fld(host, fix_protocol=True, fail_silently=True) or host


def same_site(url: str, site: str) -> bool:
    """True when ``url`` belongs to the same registrable domain as
    ``site`` (a URL or a host). Subdomains count: a blog at
    ``blog.example.com`` and its pages at ``www.example.com`` are one
    site. Separate tenants on a shared suffix (two ``*.blogspot.com``
    blogs) are not."""
    host = _host(url)
    return bool(host) and _site(host) == _site(_host(site))


# ---------------------------------------------------------------------------
# url_ok
# ---------------------------------------------------------------------------

# Whole path segments that mark a page as not an article wherever they
# appear: taxonomy and author listings, CMS plumbing, account and cart
# pages. Whole segments, not substrings: ``/pets/fat-cat/...`` and
# ``/news/power-user/...`` are articles.
_NON_ARTICLE_SEGMENTS = frozenset({
    "tag", "tags", "category", "categories", "author", "authors",
    "attachment", "uploads",
    "wp-admin", "wp-content", "wp-includes", "wp-json", "cdn-cgi",
    "feed", "rss", "atom", "trackback",
    "cart", "checkout", "basket", "wishlist", "account", "my-account",
    "login", "logout", "signin", "sign-in", "signup", "sign-up", "register",
    "shop", "collections",
})

# Words that name a listing or a record only in some positions. As the
# last segment they are the listing itself (``/forum/users``,
# ``/blog/products/``); as the first segment with one identifier after
# them they are one record (``/users/dana``, ``/products/canvas-tote``,
# ``/members/123``). A news section of the same name with an article
# under it (``/blog/products/new-launch``,
# ``/news/profiles/mayor-retires``) is fine.
_RECORD_SEGMENTS = frozenset({
    "user", "users", "profile", "profiles", "member", "members",
    "comments", "product", "products",
})

# Archive listings: the archive itself (``/archive/``) or a date
# archive under it (``/archives/2026/07/``). An article under it is
# fine: WordPress's numeric permalinks (``/archives/123``) and The
# Atlantic's ``/<section>/archive/2024/01/<slug>/<id>/``.
_ARCHIVE_SEGMENTS = frozenset({"archive", "archives"})

# Sections whose bare year is a year archive (``/blog/2026/``), unlike
# a numeric id elsewhere (``/node/2015``, ``/p/2041``).
_YEAR_ARCHIVE_PARENTS = _ARCHIVE_SEGMENTS | {
    "blog", "blogs", "news", "posts", "articles", "stories",
}

# Static pages, rejected when they are the page itself (the last
# segment): ``/about/``, ``/blog/contact``, ``/search``. A search
# section with articles under it (``/search/best-laptops-2026``) is
# fine; ``score_page`` judges whatever such a URL turns out to be.
_STATIC_SLUGS = frozenset({
    "about", "about-us", "about-me", "contact", "contact-us", "contacts",
    "privacy", "privacy-policy", "privacy-notice", "cookies", "cookie-policy",
    "terms", "terms-of-use", "terms-of-service", "terms-and-conditions", "tos",
    "legal", "disclaimer", "accessibility", "imprint", "impressum",
    "careers", "jobs", "advertise", "advertising", "faq", "faqs",
    "help", "support", "subscribe", "newsletter", "donate", "sitemap",
    "search", "team", "our-team", "staff", "embed", "amp",
    "wp-login.php", "xmlrpc.php",
})

# Sections whose pages are static pages: Shopify's ``/pages/<x>``,
# ``/policies/<x>``, and ``/about/<x>``. Only a page directly under one
# is static (``_static_section``): councils, universities and companies
# file their news deeper in them (``/about/news/<slug>``,
# ``/about-us/news-and-media/<slug>``). Not ``legal``, ``help`` or
# ``support``: news sites file stories under those (Reuters'
# ``/legal/government/...``). As the page itself they are still static
# slugs above.
_STATIC_SECTIONS = frozenset({
    "pages", "policies", "about", "about-us", "contact", "contact-us",
})

# Query keys of pages that aren't articles: a WordPress search
# (``?s=``), static page and attachment ids, comment-reply and share
# links, cart actions, paging.
_NON_ARTICLE_QUERY_KEYS = frozenset({
    "s", "page_id", "attachment_id", "replytocom", "share",
    "add-to-cart", "add_to_cart", "paged",
})

# Query keys that make the site root a post: WordPress's Plain
# permalinks (``/?p=123``), which its core sitemap lists for every post.
_ROOT_POST_QUERY_KEYS = frozenset({"p"})

_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_MONTH_RE = re.compile(r"0?[1-9]|1[0-2]")
_DAY_RE = re.compile(r"0?[1-9]|[12]\d|3[01]")
_NUMBER_RE = re.compile(r"\d+")

# courlan only judges the host, the page's own name and the query (see
# ``url_ok``); this stands in for the source's own host and for the
# path above the page.
_PROBE_HOST = "example.com"

# Files, by the page's own ending: ``/uploads/trail.jpg``, ``/feed.xml``,
# ``/report.pdf``. Only the ending counts. courlan's own file-type test
# matches ``.js`` or ``-png`` anywhere in a URL, so it read
# ``/blog/how-to-compress-png-files`` and ``/blog/node.js-22-released``
# as files.
_FILE_EXTENSIONS = frozenset({
    "jpg", "jpeg", "png", "gif", "svg", "webp", "avif", "bmp", "ico", "tif", "tiff", "heic",
    "pdf", "zip", "gz", "tgz", "tar", "rar", "7z", "iso", "dmg", "exe", "apk", "msi",
    "mp3", "m4a", "aac", "ogg", "oga", "wav", "flac", "opus",
    "mp4", "m4v", "mov", "avi", "webm", "mkv", "flv", "swf",
    "css", "js", "mjs", "json", "xml", "rss", "atom", "rdf", "csv", "ics", "vcf",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "epub",
    "woff", "woff2", "ttf", "otf", "eot",
})
# File endings that also end the names of software, which dev blogs keep
# in their slugs (Hugo, Eleventy and Jekyll keep the dots): ``three.js``,
# ``package.json``, ``tailwind.css``, ``pom.xml``. They name a file only
# at the true end of a path (``/assets/app.js``, ``/feed.xml``). Before
# a ``/`` (``/posts/whats-new-in-vue.js/``) or under a page ending
# (``/2026/10/18/whats-new-in-vue.js.html``) they are a word of the
# slug; a document or media ending there (``annual-report.pdf/``,
# ``doc.pdf.html``) is still a file.
_CODE_EXTENSIONS = frozenset({"js", "mjs", "json", "css", "xml"})
_DOCUMENT_EXTENSIONS = _FILE_EXTENSIONS - _CODE_EXTENSIONS
# Endings of web pages that courlan knows (its own whitelist, without
# ``.txt`` and ``.xml``, which are files here).
_PAGE_EXTENSIONS = frozenset({
    "html", "htm", "xhtml", "shtml", "phtml", "mht", "mhtml", "htx",
    "php", "php3", "php4", "php5", "asp", "aspx", "adp", "jsp", "cfm", "cgi", "pl", "do",
    "stm", "amp",
})
# Endings of article pages on CMSs that courlan doesn't know: news CMSs
# (Escenic's ``.ece``, the Times Internet ``.cms``, Globo's ``.ghtml``),
# the ``.page`` of nyc.gov's agency press releases, JavaServer Faces'
# ``.jsf`` and ColdFusion's ``.cfml``. They are left out of what courlan
# sees.
_ARTICLE_EXTENSIONS = frozenset({"ece", "cms", "ghtml", "page", "jsf", "cfml"})
# Any other ending of 2 to 5 letters is a file of a type not listed
# above (``.gpx``, ``.kmz``, ``.rtf``, ``.wmv``), as courlan's own
# ending test reads the end of a path. Not at the end of a path that
# ends in ``/`` (``/blog/moving-to-fly.io/`` has no ending, for courlan
# too), not under the owner's link pattern, and not in a sitemap (see
# ``url_ok``).
_LETTERS_ENDING_RE = re.compile(r"[a-z]{2,5}")
# A camera's file name used as the page name (WordPress attachment
# pages: ``/img_1234/``, ``/IMG-1234``).
_CAMERA_NAME_RE = re.compile(r"img[-_]?\d+")
# A folder's own index page under a web page's ending
# (``/news/council-vote/index.html``, ``/news/20261020/default.aspx``):
# static and exported sites publish each story as a folder with its own
# index page. ``url_ok`` judges the folder.
_INDEX_PAGE_NAMES = frozenset({"index", "default"})
_EXTENSION_RE = re.compile(r"^(.+)\.([a-z][a-z0-9]{0,4})$")
# A page name of one word, which courlan judges as a whole (``contact``,
# ``login``, ``impressum``, ``index``, ``home``, ``feed``). A slug of
# several words is judged by our rules and ``score_page``, not by the
# words in it.
_ONE_WORD_RE = re.compile(r"[^\W_]+")


def _split_extension(page: str) -> tuple[str, str]:
    """``("article68765432", "ece")`` for ``article68765432.ece``;
    ``(page, "")`` when the page name has no ending."""
    match = _EXTENSION_RE.match(page)
    return (match.group(1), match.group(2)) if match else (page, "")


def _is_index_page(page: str) -> bool:
    """``index.html``, ``default.aspx``: a folder's own index page
    (``_INDEX_PAGE_NAMES`` under a web page's ending)."""
    stem, extension = _split_extension(page)
    return stem in _INDEX_PAGE_NAMES and extension in _PAGE_EXTENSIONS


def _courlan_page(page: str) -> str:
    """The page's own name as courlan sees it (see ``url_ok``): a
    one-word name as it is, with its ending when it is a web page's, and
    any longer slug as a neutral word."""
    stem, extension = _split_extension(page)
    name = stem if _ONE_WORD_RE.fullmatch(stem) else "post"
    return f"{name}.{extension}" if extension in _PAGE_EXTENSIONS else name


def _is_file(page: str, *, at_end: bool = True, any_ending: bool = True) -> bool:
    """Whether the page's own name (lower case) names a file: a file
    ending (``trail.jpg``, ``episode-12.mp3``), any other ending of 2 to
    5 letters that is no web page's (``trail-3.gpx``), a document or
    media ending under a page ending (``doc.pdf.html``), or a camera's
    file name (``img_1234``).

    ``at_end``: nothing follows the name (the path doesn't end in
    ``/``). Before a ``/`` only a document or media ending counts, not
    a code word (``_CODE_EXTENSIONS``), and the 2-to-5-letter rule
    needs ``at_end`` and ``any_ending`` both."""
    stem, extension = _split_extension(page)
    if extension in _PAGE_EXTENSIONS:
        stem, inner = _split_extension(stem)
        if inner in _DOCUMENT_EXTENSIONS:
            return True
    elif extension in (_FILE_EXTENSIONS if at_end else _DOCUMENT_EXTENSIONS):
        return True
    elif (
        at_end
        and any_ending
        and extension not in _ARTICLE_EXTENSIONS
        and _LETTERS_ENDING_RE.fullmatch(extension)
    ):
        return True
    return bool(_CAMERA_NAME_RE.fullmatch(stem))


def _segments(path: str) -> list[str]:
    return [unquote(s).lower() for s in path.split("/") if s]


def _is_date_tail(segments: list[str]) -> bool:
    """True for ``[YYYY]``, ``[YYYY, MM]`` or ``[YYYY, MM, DD]``."""
    if not 1 <= len(segments) <= 3:
        return False
    return all(
        pattern.fullmatch(s) for pattern, s in zip((_YEAR_RE, _MONTH_RE, _DAY_RE), segments, strict=False)
    )


def _date_archive(segments: list[str]) -> bool:
    """A path that ends in a date archive. ``/YYYY/MM/`` and
    ``/YYYY/MM/DD/`` are archives at any depth. A lone year is one only
    as the whole path (``/2026/``) or under a blog, news or archive
    section (``/blog/2026/``); elsewhere it is a numeric id
    (``/node/2015``, ``/p/2041``)."""
    if any(len(segments) >= n and _is_date_tail(segments[-n:]) for n in (3, 2)):
        return True
    if _YEAR_RE.fullmatch(segments[-1]):
        return len(segments) == 1 or segments[-2] in _YEAR_ARCHIVE_PARENTS
    return False


def _listing_path(segments: list[str], own: int = 0) -> bool:
    """A path whose segments name a listing, a record or CMS plumbing
    rather than an article (see the segment sets above). The first
    ``own`` segments are the feed's own section (``_own_segments``) and
    aren't judged."""
    for i, segment in enumerate(segments):
        if i < own:
            continue
        rest = segments[i + 1:]
        if segment in _NON_ARTICLE_SEGMENTS:
            return True
        if segment in _RECORD_SEGMENTS and (not rest or (i == 0 and len(rest) == 1)):
            return True
        if segment in _ARCHIVE_SEGMENTS and (not rest or _is_date_tail(rest)):
            return True
        # Pagination: /blog/page/2/.
        if segment == "page" and len(rest) == 1 and _NUMBER_RE.fullmatch(rest[0]):
            return True
    return False


def _static_section(segments: list[str]) -> bool:
    """A page directly under a static section (``_STATIC_SECTIONS``):
    ``/about/history``, ``/pages/our-story``,
    ``/policies/refund-policy``. A deeper path is judged like any other
    path, so the news a site files there comes in
    (``/about/news/<slug>``, ``/about-us/news-and-media/<slug>``), and
    ``score_page`` judges the rest (``/about/team/<name>``), one fetch
    each, as main fetched every sitemap entry."""
    return len(segments) == 2 and segments[0] in _STATIC_SECTIONS


def _own_segments(path: str, section: str | None) -> int:
    """How many of ``path``'s segments are the feed's own section
    (``section``, a path prefix such as ``/about/news/``): its whole
    segments, when ``path`` is below it, else 0. A prefix that ends
    inside a segment (``/about/news-``) owns only the segments before
    it."""
    if not section or not path.startswith(section) or path.rstrip("/") == section.rstrip("/"):
        return 0
    return len(_segments(section[: section.rfind("/") + 1]))


def url_ok(
    url: str,
    site: str,
    *,
    owner_pattern: bool = False,
    section: str | None = None,
    sitemap_entry: bool = False,
) -> bool:
    """Cheap test of whether ``url`` could be an article on ``site``
    (the source's URL or host). Same-site http(s) URLs only.

    Three checks, each on what it reads well:

    - **The path above the page is ours**, by whole segment and by
      position (``_NON_ARTICLE_SEGMENTS`` anywhere; record, archive and
      pagination words only where they are the listing or one record;
      a page directly under a ``_STATIC_SECTIONS`` word, as in
      ``/about/history``, but not ``/about/news/<slug>``; date archives
      last). The site root is
      the home page unless its query names a post (WordPress's
      ``/?p=123``). courlan's own structure filter matches substrings:
      it reads
      ``/pets/fat-cat/how-to-feed`` as a category page,
      ``/news/power-user/tips`` as a user page, and any ``/search/``
      as a search page.
    - **Files are ours too**, by the page's own name only (``_is_file``):
      a file ending, any other ending of 2 to 5 letters that is no web
      page's or known CMS's, or a camera's file name.
      ``/blog/png-vs-jpg``, ``/blog/node.js-22-released`` and
      ``/press/flu-shots-available.page`` are articles; ``trail.jpg``,
      ``trail-3.gpx`` and ``img_1234`` are not. Only the end of the path
      has an ending, as courlan reads it: a path that ends in ``/``
      (``/blog/moving-to-fly.io/``) and the name under a page ending
      are judged by the document and media endings alone, so a slug
      that ends in a software name (``/posts/whats-new-in-vue.js/``,
      ``vue.js.html``) is an article and ``annual-report.pdf/`` a file.
    - **courlan judges the rest**: ``check_url(strict=True)`` on a
      one-word page name (contact, login, imprint and index pages,
      ``/feed``), the query (a file type hidden in it) and the host
      (malformed hosts). A slug of several words reaches it as a
      neutral word, and a news CMS's ending it doesn't know (``.ece``,
      ``.cms``) is left off. It is a yes/no test
      only. The URL it would normalize to is never used, because strict
      normalizing drops query parameters it doesn't know.

    A folder's own index page (``/news/<slug>/index.html``,
    ``/news/20261020/default.aspx``) is judged as the folder
    (``/news/<slug>/``), by all three checks: courlan reads an index
    page under a short path as a home page, and the path it would read
    above it is ours. ``/index.html`` is still the home page.

    When the URL's host is the source's own host, courlan's host check
    is skipped: the owner chose that host, and courlan rejects an IP or
    ``localhost`` with a port, which every loopback test server uses.

    ``owner_pattern``: the URL is under the owner's ``link_pattern``,
    which is the article signal (as ``score_page``'s
    ``owner_configured``), so courlan isn't asked, and neither is the
    2-to-5-letter ending rule that reads like it
    (``/library/why-we-chose-deno.land``); our path rules and the file
    endings (as read above) still apply.

    ``section``: the section the feed reads (its ``link_pattern``, else
    its address's section, such as ``/about/news/``). The owner chose
    it, so for a URL below it the segment rules (listing, record and
    static section) judge only the segments below it:
    ``/about/news/<slug>`` and ``/shop/journal/<slug>`` are candidates
    of a feed that reads them. The page's own name, date archives, the
    file endings and courlan are judged as for any URL.

    ``sitemap_entry``: the URL is a sitemap's entry. A sitemap lists a
    site's pages, and main fetched every entry, so an ending this module
    doesn't know is a page's there (ATG's ``.jhtml``, ``.jspx``,
    ``.shtm``, Domino's ``.nsf``): the 2-to-5-letter ending rule doesn't
    apply, while the file endings (as read above) still do. A file the
    sitemap lists under an ending of its own is dropped by the page
    fetch's HTML check, once. A listing page's links keep the rule, as
    courlan's ending test read them on main.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if not same_site(url, site):
        return False

    path = parts.path
    segments = _segments(path)
    if len(segments) > 1 and _is_index_page(segments[-1]):
        # A folder's own index page is the folder.
        path = path[: path.rstrip("/").rfind("/") + 1]
        segments = segments[:-1]
    query_keys = {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
    if not segments and not query_keys & _ROOT_POST_QUERY_KEYS:
        return False  # the home page
    own = _own_segments(path, section)
    if _listing_path(segments, own):
        return False
    if segments and (segments[-1] in _STATIC_SLUGS or (not own and _static_section(segments))):
        return False
    if segments and _date_archive(segments):
        return False
    if query_keys & _NON_ARTICLE_QUERY_KEYS:
        return False
    page = segments[-1] if segments else ""
    if _is_file(
        page,
        at_end=not path.endswith("/"),
        any_ending=not (owner_pattern or sitemap_entry),
    ):
        return False
    # Blogspot's date archives (/2026_10_01_archive.html).
    if page.endswith("_archive.html"):
        return False
    if owner_pattern:
        return True

    own_host = parts.hostname.lower().rstrip(".") == _host(site)
    netloc = _PROBE_HOST if own_host else parts.netloc
    # Keep the page's own name as courlan should see it (and its
    # trailing slash); every segment above it was judged above. A root
    # post (/?p=123) keeps the bare root.
    shown = ""
    if segments:
        trailing = "/" if path.endswith("/") else ""
        shown = "x/" * (len(segments) - 1) + _courlan_page(page) + trailing
    probe = f"{parts.scheme}://{netloc}/{shown}"
    if parts.query:
        probe += "?" + parts.query
    return courlan.check_url(probe, strict=True) is not None


# ---------------------------------------------------------------------------
# Sitemaps
# ---------------------------------------------------------------------------

SITEMAP_NEWS = "news"
SITEMAP_POST = "post"
SITEMAP_PAGE = "page"
SITEMAP_TAXONOMY = "taxonomy"
SITEMAP_AUTHOR = "author"
SITEMAP_PRODUCT = "product"
SITEMAP_MEDIA = "media"
SITEMAP_OTHER = "other"

# Never read: their entries are listings, people, products or files
# (attachment pages). A video, image or media sitemap is read: its
# ``<loc>`` is the page that shows the video or image (Google's sitemap
# extensions), Yoast names a ``video`` post type's sitemap
# ``video-sitemap.xml``, and "media" names a site's media section and
# its releases (``sitemap-media-releases.xml``, ``/media/sitemap.xml``).
SKIPPED_SITEMAPS = frozenset({
    SITEMAP_TAXONOMY, SITEMAP_AUTHOR, SITEMAP_PRODUCT, SITEMAP_MEDIA,
})

# Whole words of a sitemap's file name (``_sitemap_tokens``). Checked
# in this order, so the subtype wins over a broader word next to it:
# WordPress core's ``wp-sitemap-posts-page-1.xml`` is pages, not posts.
_SITEMAP_TOKENS: tuple[tuple[str, frozenset[str]], ...] = (
    (SITEMAP_TAXONOMY, frozenset({
        "tag", "tags", "category", "categories", "cat", "taxonomy",
        "taxonomies", "topic", "topics", "collection", "collections",
    })),
    (SITEMAP_AUTHOR, frozenset({"author", "authors", "user", "users"})),
    (SITEMAP_PRODUCT, frozenset({"product", "products", "shop"})),
    (SITEMAP_MEDIA, frozenset({"attachment", "attachments"})),
    (SITEMAP_PAGE, frozenset({"page", "pages"})),
    (SITEMAP_NEWS, frozenset({"news", "googlenews"})),
    (SITEMAP_POST, frozenset({
        "post", "posts", "article", "articles", "blog", "blogs", "story", "stories",
    })),
)

_SITEMAP_EXT_RE = re.compile(r"\.(?:xml|txt)(?:\.gz)?$|\.gz$")
_TOKEN_SPLIT_RE = re.compile(r"[-_.]+")
_PART_SPLIT_RE = re.compile(r"[-.]+")
_SITEMAP_WORD_RE = re.compile(r"sitemaps?")
# Trailing page numbers: post-sitemap2, sitemap_products_1.
_TOKEN_DIGITS_RE = re.compile(r"\d+$")
_NOISE_TOKENS = frozenset({"sitemap", "sitemaps", "index", "wp", "xml", ""})
# WordPress core's sitemap providers: ``wp-sitemap-<provider>-<subtype>-1``.
_WP_PROVIDERS = frozenset({"posts", "taxonomies", "users"})


def _head_word(phrase: str) -> str | None:
    """The last word of ``phrase``, without page numbers, "index" and
    the like: the word the others only qualify."""
    words = [
        word
        for word in (_TOKEN_DIGITS_RE.sub("", raw) for raw in _TOKEN_SPLIT_RE.split(phrase))
        if word not in _NOISE_TOKENS
    ]
    return words[-1] if words else None


def _sitemap_tokens(name: str) -> list[str]:
    """The words of a sitemap name that say what it lists, without
    "sitemap" and page numbers.

    A name of several words counts by its last word, which the others
    only qualify: a WordPress type or taxonomy joined with "_"
    (``post_tag``, ``product_cat``, ``media_release``,
    ``product_update``), the whole name in front of "sitemap" (Yoast's
    and Rank Math's ``<type>-sitemap.xml``, Wix's
    ``blog-posts-sitemap.xml``, a ``media-release-sitemap.xml``), and a
    name with no "sitemap" in it (a section's ``/media-centre/sitemap.xml``,
    a ``media-releases.xml``). So ``media_release-sitemap.xml`` lists
    releases, not media, and ``post_tag-sitemap.xml`` tags. After
    "sitemap" the rest counts by its last word too
    (``sitemap-pt-post-2026-10``, ``sitemap-media-releases``), after
    WordPress core's provider word when it leads
    (``wp-sitemap-<provider>-<subtype>-1``): ``wp-sitemap-posts-page-1``
    gives posts and page, ``wp-sitemap-posts-media-release-1`` posts and
    release, ``wp-sitemap-taxonomies-region-1`` taxonomies and region.
    WordPress allows a "-" in a post type's name. "sitemap" glued to a
    word is split off, so ``newssitemap1`` and ``sitemapnews`` both give
    ``news``; a word is never searched for inside another (``heritage``
    holds no tag)."""
    lead, *rest = _SITEMAP_WORD_RE.split(name.lower(), maxsplit=1)
    head = _head_word(lead)
    tokens = [head] if head else []
    if not rest:
        return tokens
    parts = [p for p in _PART_SPLIT_RE.split(_SITEMAP_WORD_RE.sub("-", rest[0])) if p]
    if parts and parts[0] in _WP_PROVIDERS:
        tokens.append(parts.pop(0))
    head = _head_word("-".join(parts))
    if head:
        tokens.append(head)
    return tokens


def _class_of_tokens(tokens: Iterable[str]) -> str | None:
    found = set(tokens)
    for cls, words in _SITEMAP_TOKENS:
        if found & words:
            return cls
    return None


def classify_sitemap(url: str) -> str:
    """The kind of pages a sitemap lists, from its URL alone: one of
    ``news``, ``post``, ``page``, ``taxonomy``, ``author``,
    ``product``, ``media`` or ``other`` (a plain ``sitemap.xml``, an
    index, or a name we don't know).

    Whole subtype tokens decide first. WordPress core names its
    sitemaps ``wp-sitemap-<provider>-<subtype>-<n>.xml``, so
    ``wp-sitemap-posts-page-1.xml`` contains "posts" but lists pages.
    The page class also covers Yoast's ``page-sitemap*.xml`` and
    Shopify's ``sitemap_pages_*.xml``. Of the query, only WordPress
    core's Plain-permalink form is read
    (``/?sitemap=posts&sitemap-subtype=page``): the ``?page=2`` of a
    paged Drupal sitemap is not a page sitemap.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return SITEMAP_OTHER
    segments = [s for s in parts.path.lower().split("/") if s]
    name = _SITEMAP_EXT_RE.sub("", segments[-1]) if segments else ""

    cls = _class_of_tokens(_sitemap_tokens(name))
    if cls:
        return cls
    query = {k.lower(): v for k, v in parse_qsl(parts.query)}
    wp_plain = [query[k] for k in ("sitemap-subtype", "sitemap") if k in query]
    cls = _class_of_tokens(t for value in wp_plain for t in _sitemap_tokens(value))
    if cls:
        return cls
    # A sitemap filed under its section: /blog/sitemap.xml.
    for segment in reversed(segments[:-1]):
        cls = _class_of_tokens(_sitemap_tokens(segment))
        if cls:
            return cls
    return SITEMAP_OTHER


_SITEMAP_ORDER = {SITEMAP_NEWS: 0, SITEMAP_POST: 1, SITEMAP_OTHER: 2, SITEMAP_PAGE: 3}


def order_sitemaps(urls: Iterable[str]) -> list[str]:
    """The sitemaps worth reading, best first: news, then post (post,
    article and blog), then unknown ones, keeping the given order
    within each class. Taxonomy, author, product and media (attachment)
    sitemaps are dropped.

    A page sitemap is dropped whenever a news or post sitemap exists.
    Page sitemaps are where About and Contact come from, and because
    static pages get edited they often carry the newest ``<lastmod>``.
    When there is no news or post sitemap, the page sitemap is kept
    last as a fallback; ``url_ok`` and ``score_page`` still filter its
    entries.
    """
    classed = [(u, classify_sitemap(u)) for u in urls]
    has_posts = any(c in (SITEMAP_NEWS, SITEMAP_POST) for _, c in classed)
    kept = [
        (u, c) for u, c in classed
        if c not in SKIPPED_SITEMAPS and not (c == SITEMAP_PAGE and has_posts)
    ]
    return [u for u, c in sorted(kept, key=lambda pair: _SITEMAP_ORDER[pair[1]])]


# ---------------------------------------------------------------------------
# Page parsing helpers
# ---------------------------------------------------------------------------

_HTML_PARSER = lxml.html.HTMLParser(encoding="utf-8")


def _parse_html(html: str):
    """An lxml document for ``html``, or None. Parsed from UTF-8 bytes,
    because lxml refuses a str that carries an XML encoding line."""
    if not html or not html.strip():
        return None
    try:
        return lxml.html.document_fromstring(
            html.encode("utf-8", "replace"), parser=_HTML_PARSER
        )
    except (ValueError, etree.ParserError):
        return None


def _clean_text(value: str | None) -> str:
    return " ".join((value or "").split())


def parse_date(raw: str | None) -> dt.datetime | None:
    """A timezone-aware UTC datetime from an ISO-8601 or RFC 822 date
    string, or None. A date without a time is midnight UTC, and a
    value before 1990 is treated as junk."""
    value = (raw or "").strip()
    if not value:
        return None
    parsed: dt.datetime | None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    parsed = parsed.astimezone(dt.timezone.utc)
    return parsed if parsed.year >= 1990 else None


# ---------------------------------------------------------------------------
# score_page
# ---------------------------------------------------------------------------

_ARTICLE_TYPES = frozenset({
    "article", "blogposting", "newsarticle", "analysisnewsarticle",
    "backgroundnewsarticle", "opinionnewsarticle", "reportagenewsarticle",
    "reviewnewsarticle", "liveblogposting", "techarticle",
    "scholarlyarticle", "report",
})
# JSON-LD that cancels an ``og:type=article``: Yoast marks every page
# ``og:type=article``, but its JSON-LD still calls a page a WebPage.
_PAGE_TYPES = frozenset({"webpage", "aboutpage", "contactpage"})
# JSON-LD that counts against a page (with ``og:type=website``).
_SITE_TYPES = frozenset({"website", "aboutpage", "contactpage"})

# Published-date metadata, most specific first. Modified dates don't
# count: Yoast gives every page an ``article:modified_time``.
_DATE_META = (
    "article:published_time", "og:published_time", "datepublished",
    "parsely-pub-date", "sailthru.date", "article.published", "pubdate",
    "publishdate", "publish-date", "publish_date", "dc.date.issued",
    "dcterms.issued", "dcterms.created", "dcterms.date", "dc.date", "date",
)
_TIME_CLASSES = frozenset({
    "published", "entry-date", "post-date", "publish-date", "date-published",
    "dt-published",  # microformats h-entry
})
_URL_DATE_RE = re.compile(
    r"/(?:19|20)\d{2}/(?:0?[1-9]|1[0-2])/"
    r"|(?<!\d)(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])(?!\d)"
)

# Title separators: "Post title – Site", "Site | Post title".
_TITLE_SPLIT_RE = re.compile(r"\s+[|–—·»:\-]+\s+")
# Each alternative is a whole title part: "404", "Error 404",
# "404 Not Found", "404: Page not found". A title that only starts with
# 404 ("404 errors: how to fix them") or a site named "404 Media" is not
# one.
_SOFT_404_RE = re.compile(
    r"^(?:error\s*)?404(?:\s*[:\-–—]?\s*(?:error|(?:page\s+)?not\s+found))?$"
    r"|^(?:page\s+)?not\s+found$"
    r"|^nothing\s+(?:was\s+)?found$"
    r"|^(?:oops[!.,]?\s*)?(?:sorry[!.,]?\s*)?(?:that\s+|the\s+|this\s+)?page\s+"
    r"(?:can(?:'|’|no)?t|could\s*n[o']?t|could not)\s+be\s+found",
    re.IGNORECASE,
)

_LONG_TEXT = 600
_SHORT_TEXT = 300


class _PageFacts(NamedTuple):
    og_type: str
    site_name: str
    titles: list[str]
    h1: str
    canonical: str | None
    jsonld_types: set[str]
    # A published date from the page's metadata, else whether its URL
    # carries one (/2026/07/slug).
    published: dt.datetime | None
    url_dated: bool


def _jsonld_nodes(data) -> Iterable[dict]:
    """Top-level JSON-LD nodes: the script's own object(s), ``@graph``
    members, and each one's ``mainEntity``. Nothing deeper, so a
    listing page whose ``ItemList`` holds BlogPostings isn't read as
    an article, nor an article's ``isPartOf`` WebSite as a site page."""
    stack = data if isinstance(data, list) else [data]
    for node in stack:
        if not isinstance(node, dict):
            continue
        graph = node.get("@graph")
        members = graph if isinstance(graph, list) else []
        for item in [node, *members]:
            if not isinstance(item, dict):
                continue
            yield item
            main = item.get("mainEntity")
            for sub in main if isinstance(main, list) else [main]:
                if isinstance(sub, dict):
                    yield sub


# "schema:NewsArticle" and "https://schema.org/NewsArticle" are both
# "NewsArticle".
_SCHEMA_PREFIX_RE = re.compile(r"^(?:schema:|https?://(?:www\.)?schema\.org/)", re.IGNORECASE)


def _node_types(node: dict) -> set[str]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return {
        _SCHEMA_PREFIX_RE.sub("", v.strip()).lower() for v in values if isinstance(v, str)
    }


def _marked_dates(doc) -> list[tuple[object, dt.datetime]]:
    """Every element that marks a published date in the body, with its
    date: a ``<time datetime>`` marked as published (``pubdate``,
    ``itemprop``, a published class) or inside an article's header, and
    microdata ``itemprop="datePublished"`` on any element. Not a
    footer's "updated" ``<time>``."""
    out = []
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        itemprop = (el.get("itemprop") or "").strip().lower() == "datepublished"
        if el.tag == "time":
            stamp = el.get("datetime") or (el.get("content") if itemprop else None)
            if not stamp:
                continue
            classes = set((el.get("class") or "").lower().split())
            above = [a.tag for a in el.iterancestors()]
            in_article_header = "header" in above and "article" in above[above.index("header"):]
            if not (
                el.get("pubdate") is not None or itemprop or classes & _TIME_CLASSES
                or in_article_header
            ):
                continue
        elif itemprop:
            stamp = el.get("content") or el.get("datetime")
        else:
            continue
        parsed = parse_date(stamp)
        if parsed:
            out.append((el, parsed))
    return out


# Inside an article, the parts whose ``<time>`` is not the article's
# own: its footer ("Updated ..."), and related-post lists and menus.
_NOT_OWN_TIME_TAGS = frozenset({"footer", "aside", "nav"})
_UPDATED_CLASS_RE = re.compile(r"updat|modif", re.IGNORECASE)


def _own_times(scope) -> Iterator[dt.datetime]:
    """The plain ``<time datetime>`` dates in ``scope``, in page order:
    ones with no published marker (a theme that prints the date in a
    ``<div>`` under the title, as Astro, Next.js and Webflow templates
    do), outside the scope's footer, asides and menus, and not marked as
    an update."""
    for el in scope.iter("time"):
        stamp = el.get("datetime")
        if not stamp or _UPDATED_CLASS_RE.search(el.get("class") or ""):
            continue
        outside = False
        for ancestor in el.iterancestors():
            if ancestor is scope:
                break
            if ancestor.tag in _NOT_OWN_TIME_TAGS:
                outside = True
                break
        if outside:
            continue
        parsed = parse_date(stamp)
        if parsed:
            yield parsed


def _article_time(article) -> dt.datetime | None:
    """The first plain ``<time datetime>`` of an article (``_own_times``)."""
    return next(_own_times(article), None)


_MONTH_NUMBERS = {
    name: number
    for number, names in enumerate(
        [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
         ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
         ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
         ("dec", "december")],
        start=1,
    )
    for name in names
}
# A date written out at the start of a line: "October 20, 2026",
# "Oct. 20th, 2026", "20 October 2026", "2026-10-20", after an optional
# "Posted on" or "Published" and a weekday ("October 20, 2026 · 5 min
# read" is one; "Updated October 20, 2026" and "Founded October 20,
# 2015" are not).
_TEXT_DATE_RE = re.compile(
    r"(?:(?:posted|published)(?:\s+on)?:?\s+)?"
    r"(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+)?"
    r"(?:(?P<m1>[a-z]{3,9})\.?\s+(?P<d1>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y1>(?:19|20)\d{2})"
    r"|(?P<d2>\d{1,2})(?:st|nd|rd|th)?\s+(?P<m2>[a-z]{3,9})\.?,?\s+(?P<y2>(?:19|20)\d{2})"
    r"|(?P<y3>(?:19|20)\d{2})-(?P<m3>\d{2})-(?P<d3>\d{2}))"
    r"(?!\w)",
    re.IGNORECASE,
)
# The longest text of the element after a title that is read for a
# date: a line of post meta, not a paragraph of the page.
_META_LINE_CHARS = 80


def _text_date(text: str) -> dt.datetime | None:
    """The date ``text`` starts with (``_TEXT_DATE_RE``), or None."""
    match = _TEXT_DATE_RE.match(_clean_text(text))
    if not match:
        return None
    if match["y3"]:
        year, month, day = int(match["y3"]), int(match["m3"]), int(match["d3"])
    else:
        name = (match["m1"] or match["m2"]).lower()
        if name not in _MONTH_NUMBERS:
            return None
        year = int(match["y1"] or match["y2"])
        month, day = _MONTH_NUMBERS[name], int(match["d1"] or match["d2"])
    try:
        return dt.datetime(year, month, day, tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _title_date(doc, h1) -> dt.datetime | None:
    """A page with one ``<h1>`` and no ``<article>``, whose theme marks
    no date, dates itself next to its title:

    - a plain ``<time datetime>`` in the ``<h1>``'s own container
      (``_own_times``), when it is the only one there: a WordPress block
      theme's ``div.wp-block-post-date`` beside its
      ``h1.wp-block-post-title``. A listing's cards hold one each.
    - a date written out at the start of the element right after the
      ``<h1>``, when that element is a short line
      (``_META_LINE_CHARS``): a Webflow post's ``div.blog-date``, which
      has no ``<time>`` and no metadata.
    """
    if h1 is None or doc.find(".//article") is not None or len(doc.findall(".//h1")) != 1:
        return None
    container = h1.getparent()
    if container is not None:
        times = list(_own_times(container))
        if len(times) == 1:
            return times[0]
    after = h1.getnext()
    while after is not None and not isinstance(after.tag, str):
        after = after.getnext()
    if after is None:
        return None
    # Each piece of text as its own word: two spans' "October 14, 2026"
    # and "5 min read" don't run together.
    line = _clean_text(" ".join(after.itertext()))
    return _text_date(line) if len(line) <= _META_LINE_CHARS else None


def _body_date(doc) -> dt.datetime | None:
    """The page's published date from its body, only when the page is
    one article. A listing's post cards each carry a marked date, and
    the first card's is not the listing's.

    - When the first ``<h1>`` sits in an ``<article>`` (else a
      ``<header>``), a marked date inside that element is the page's.
      Not when several articles hold an ``<h1>``: that is a listing
      whose cards use one.
    - With no marked date there, a plain ``<time datetime>`` inside the
      first ``<h1>``'s own ``<article>`` is the page's
      (``_article_time``): that article is the page's one story.
    - Otherwise a marked date counts only when the page has at most one
      ``<article>`` and all its marked dates agree.
    - A page with no marked date at all, one ``<h1>`` and no
      ``<article>`` may date itself next to its title (``_title_date``).
    """
    marked = _marked_dates(doc)
    h1 = doc.find(".//h1")
    if h1 is not None:
        ancestors = list(h1.iterancestors())
        scope = next((a for a in ancestors if a.tag == "article"), None)
        if scope is not None and sum(
            1 for a in doc.iter("article") if a.find(".//h1") is not None
        ) > 1:
            return None
        if scope is None:
            scope = next((a for a in ancestors if a.tag == "header"), None)
        if scope is not None:
            for el, parsed in marked:
                if scope in el.iterancestors():
                    return parsed
            if scope.tag == "article":
                parsed = _article_time(scope)
                if parsed:
                    return parsed
    if not marked:
        return _title_date(doc, h1)
    if len(doc.findall(".//article")) > 1:
        return None
    if len({parsed for _, parsed in marked}) > 1:
        return None
    return marked[0][1]


def _page_facts(doc, url: str) -> _PageFacts:
    # Head metadata by property or name. Microdata (``itemprop``) can
    # sit on every card of a listing, so ``_body_date`` reads it.
    meta: dict[str, str] = {}
    for el in doc.iter("meta"):
        key = (el.get("property") or el.get("name") or "").strip().lower()
        content = el.get("content")
        if key and content is not None and key not in meta:
            meta[key] = content.strip()

    types: set[str] = set()
    site_name = meta.get("og:site_name", "")
    published = None
    for script in doc.iter("script"):
        if (script.get("type") or "").strip().lower() != "application/ld+json":
            continue
        try:
            data = json.loads(script.text or "")
        except ValueError:
            continue
        for node in _jsonld_nodes(data):
            node_types = _node_types(node)
            types |= node_types
            if node_types & _ARTICLE_TYPES and published is None:
                raw = node.get("datePublished")
                published = parse_date(raw) if isinstance(raw, str) else None
            if "website" in node_types and not site_name and isinstance(node.get("name"), str):
                site_name = node["name"].strip()

    if published is None:
        for key in _DATE_META:
            if key in meta:
                published = parse_date(meta[key])
                if published:
                    break
    if published is None:
        published = _body_date(doc)

    titles = [_clean_text(meta.get("og:title"))]
    title_el = doc.find(".//title")
    if title_el is not None:
        titles.append(_clean_text(title_el.text_content()))
    h1_el = doc.find(".//h1")
    canonical = None
    for link in doc.iter("link"):
        rels = (link.get("rel") or "").lower().split()
        if "canonical" in rels and link.get("href"):
            canonical = urljoin(url, link.get("href").strip())
            break
    return _PageFacts(
        og_type=meta.get("og:type", "").lower(),
        site_name=_clean_text(site_name),
        titles=[t for t in titles if t],
        h1=_clean_text(h1_el.text_content()) if h1_el is not None else "",
        canonical=canonical,
        jsonld_types=types,
        published=published,
        url_dated=bool(_URL_DATE_RE.search(urlsplit(url).path)),
    )


def _title_parts(title: str) -> list[str]:
    return [p.strip().rstrip(".!").strip() for p in _TITLE_SPLIT_RE.split(title) if p.strip()]


def _soft_404(facts: _PageFacts) -> bool:
    """True when a whole title part (of ``og:title``, ``<title>`` or
    the first ``<h1>``) is a not-found title. A part that is the site's
    own name is skipped, whatever it says."""
    name = facts.site_name.casefold()
    for title in [*facts.titles, facts.h1]:
        if any(
            _SOFT_404_RE.search(part)
            for part in _title_parts(title)
            if not (name and part.casefold() == name)
        ):
            return True
    return False


def _site_name_title(facts: _PageFacts) -> bool:
    """True when a title is nothing but the site's name ("Example Blog",
    or "Example Blog | Example Blog"), as on the home page a dead link
    redirects to. "Example Blog | My post" is not."""
    name = facts.site_name.casefold()
    if not name:
        return False
    return any(
        all(part.casefold() == name for part in _title_parts(title))
        for title in facts.titles
    )


def _same_path(a: str, b: str) -> bool:
    def norm(u: str) -> str:
        return unquote(urlsplit(u).path).rstrip("/") or "/"
    return norm(a) == norm(b)


def _extracted_text(html: str, url: str) -> str:
    """Main text the way the generic_scrape plugin extracts it."""
    try:
        import trafilatura

        doc = trafilatura.bare_extraction(html, url=url, favor_recall=True)
    except Exception:  # noqa: BLE001 - an unparseable page has no text
        return ""
    return (getattr(doc, "text", None) or "").strip() if doc is not None else ""


def score_page(
    html: str,
    url: str,
    *,
    final_url: str | None = None,
    text: str | None = None,
    owner_configured: bool = False,
    listed_date: dt.datetime | None = None,
) -> tuple[int, list[str]]:
    """Score a fetched page as an article: ``(score, reasons)``, where
    the page is accepted at ``ACCEPT_SCORE`` (2) or more and each reason
    reads like ``"+3 json-ld article"``.

    ``url`` is the URL that was asked for and ``final_url`` the one the
    fetch ended on after redirects. ``text`` is the page's extracted
    main text; when None it is extracted here, the same way the plugin
    does. Run it off the event loop, like any trafilatura call.

    ``listed_date``: when the site's own list says the page was
    published (``Candidate.published``: a listing card's
    ``<time datetime>`` or a news sitemap's publication date). It dates
    the page like a date on the page itself. Never a sitemap's
    ``<lastmod>``, which every static page has too.

    Kind and date:
    - +3 for ``og:type=article`` or a JSON-LD Article, BlogPosting or
      NewsArticle. ``og:type=article`` earns nothing when JSON-LD calls
      the page a WebPage, AboutPage or ContactPage: Yoast marks every
      page ``og:type=article``.
    - -3 for ``og:type=website`` or a JSON-LD WebSite, AboutPage or
      ContactPage, but only when the page has no JSON-LD article and
      no date. Many CMSs tag every page ``og:type=website``.
    - +1 for a published date (the page's, or the list's), -1 for none.

    Length: +1 for 600+ characters, -2 under 300.

    Hard rejects: -3 for a soft-404 title or one that is just the site
    name, -3 for a redirect to another path, -2 for a canonical URL on
    another site.

    ``owner_configured`` (the source has a ``link_pattern`` or
    ``sitemap_url``): the owner's choice is the article signal, so the
    page starts at +2 and only the hard rejects and the under-300 rule
    apply. An ollama-style ``/library/<model>`` page declares no
    article type and no date, and is still what the owner asked for.
    """
    reasons: list[str] = []
    score = 0

    def add(points: int, why: str) -> None:
        nonlocal score
        score += points
        reasons.append(f"{points:+d} {why}")

    doc = _parse_html(html)
    if doc is None:
        add(-3, "no page")
        return score, reasons
    facts = _page_facts(doc, final_url or url)
    if text is None:
        text = _extracted_text(html, final_url or url)
    length = len(text.strip())

    if owner_configured:
        add(2, "owner's link pattern or sitemap")
    else:
        article_ld = facts.jsonld_types & _ARTICLE_TYPES
        if article_ld:
            add(3, "json-ld " + "/".join(sorted(article_ld)))
        elif facts.og_type == "article" and not facts.jsonld_types & _PAGE_TYPES:
            add(3, "og:type article")
        dated = facts.published is not None or listed_date is not None or facts.url_dated
        site_ld = facts.jsonld_types & _SITE_TYPES
        if not article_ld and not dated:
            if facts.og_type == "website":
                add(-3, "og:type website, undated")
            elif site_ld:
                add(-3, "json-ld " + "/".join(sorted(site_ld)) + ", undated")
        if facts.published is not None:
            add(1, "dated " + facts.published.date().isoformat())
        elif listed_date is not None:
            add(1, "listed " + listed_date.date().isoformat())
        elif facts.url_dated:
            add(1, "date in URL")
        else:
            add(-1, "no date")
        if length >= _LONG_TEXT:
            add(1, f"{length} characters")

    if length < _SHORT_TEXT:
        add(-2, f"only {length} characters")
    if _soft_404(facts):
        add(-3, "soft 404 title")
    elif _site_name_title(facts):
        add(-3, "title is the site name")
    if final_url and not _same_path(url, final_url):
        add(-3, "redirected to " + (urlsplit(final_url).path or "/"))
    if facts.canonical and not same_site(facts.canonical, final_url or url):
        add(-2, "canonical on " + _host(facts.canonical))
    return score, reasons


# ---------------------------------------------------------------------------
# listing_links
# ---------------------------------------------------------------------------

# Page chrome: nav and aside always; header and footer only when they
# belong to the page. That is the ARIA rule for the banner and
# contentinfo roles: no article, aside, main, nav or section element,
# and no element with an article, complementary, feed, main, navigation
# or region role, holds them. Themes that build cards from divs give
# their cards headers too, so an unscoped header or footer that repeats
# at one tag path as often as a listing's cards do is a card's, not the
# page's. So a post card's own <header><h2><a> stays.
_CHROME_TAGS = frozenset({"nav", "aside"})
_EDGE_TAGS = frozenset({"header", "footer"})
_SCOPING_TAGS = frozenset({"article", "aside", "main", "nav", "section"})
_SCOPING_ROLES = frozenset({
    "article", "complementary", "feed", "main", "navigation", "region",
})
_CHROME_ROLES = frozenset({"navigation", "banner", "contentinfo", "complementary"})
_MIN_GROUP = 3


def _role(el) -> str:
    return (el.get("role") or "").strip().lower()


def _drop_chrome(doc) -> None:
    doomed = []
    unscoped_edges: dict[tuple[str, ...], list] = defaultdict(list)
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        if el.tag in _CHROME_TAGS or _role(el) in _CHROME_ROLES:
            doomed.append(el)
        elif el.tag in _EDGE_TAGS and not any(
            a.tag in _SCOPING_TAGS or _role(a) in _SCOPING_ROLES for a in el.iterancestors()
        ):
            unscoped_edges[_tag_path(el)].append(el)
    for edges in unscoped_edges.values():
        if len(edges) < _MIN_GROUP:
            doomed.extend(edges)
    for el in doomed:
        el.drop_tree()


def _tag_path(el) -> tuple[str, ...]:
    """The element's tag path from the root, without classes or
    positions: WordPress gives each post card its own classes
    (``post-123 category-news``), so classes would split one list
    into groups of one."""
    return tuple(a.tag for a in reversed(list(el.iterancestors()))) + (el.tag,)


def _link_title(a) -> str:
    text = _clean_text(a.text_content())
    if text:
        return text
    if a.get("title"):
        return _clean_text(a.get("title"))
    img = a.find(".//img")
    return _clean_text(img.get("alt")) if img is not None and img.get("alt") else ""


def listing_links(html: str, page_url: str, *, section: str | None = None) -> list[Candidate]:
    """The article links on a listing page, in page order:
    ``[Candidate(url, published, title)]``.

    - Links resolve against ``<base href>`` when the page has one.
    - Only same-site links that pass ``url_ok`` count (below the feed's
      own ``section``, as ``url_ok`` reads it), and never the page
      itself.
    - nav, aside and page-level header and footer are dropped first.
    - Links group by their tag path (``main/div/article/h2/a``), and
      the largest group with 3 or more distinct URLs wins: a listing's
      cards repeat, menus and one-off links don't. On a tie, the group
      with the longer link text wins, so titles beat "Read more" and
      image links to the same posts.
    - Each link's date is the first ``<time datetime>`` in its card
      (the largest ancestor that holds no other link of the group).
    Returns ``[]`` when no group reaches 3.
    """
    doc = _parse_html(html)
    if doc is None:
        return []
    base = page_url
    base_el = doc.find(".//base[@href]")
    if base_el is not None:
        resolved = urljoin(page_url, base_el.get("href").strip())
        if urlsplit(resolved).scheme in ("http", "https"):
            base = resolved
    _drop_chrome(doc)

    page_key = urlsplit(page_url)._replace(fragment="").geturl()
    groups: dict[tuple[str, ...], list[tuple[str, object]]] = defaultdict(list)
    for a in doc.iter("a"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        try:
            absolute = urlsplit(urljoin(base, href))._replace(fragment="").geturl()
        except ValueError:
            continue
        if absolute == page_key or not url_ok(absolute, page_url, section=section):
            continue
        groups[_tag_path(a)].append((absolute, a))

    best: list[tuple[str, object]] | None = None
    best_key: tuple[int, float] = (0, 0.0)
    for links in groups.values():
        urls = {u for u, _ in links}
        if len(urls) < _MIN_GROUP:
            continue
        mean_text = sum(len(_link_title(a)) for _, a in links) / len(links)
        key = (len(urls), mean_text)
        if key > best_key:
            best, best_key = links, key
    if best is None:
        return []

    # Which group URLs sit under each element, to find each link's card.
    under: dict[object, set[str]] = defaultdict(set)
    for u, a in best:
        for ancestor in a.iterancestors():
            under[ancestor].add(u)

    out: list[Candidate] = []
    seen: set[str] = set()
    for u, a in best:
        if u in seen:
            continue
        seen.add(u)
        card = a
        for ancestor in a.iterancestors():
            if under[ancestor] != {u}:
                break
            card = ancestor
        published = None
        for el in card.iter("time"):
            published = parse_date(el.get("datetime"))
            if published:
                break
        out.append(Candidate(u, published, _link_title(a) or None))
    return out
