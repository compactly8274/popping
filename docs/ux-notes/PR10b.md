# PR10b: Website crawl wiring and errors

Backend track. Wires PR10a's `article_detect` library into website feeds (`generic_scrape`) and
the sitemap crawl (`feed_autodiscovery`), and makes a website feed's failures say what they are.
No migration, no route, no frontend, no flows or smoke states.

## Plan (written before the code)

### Anchors checked against main 9e41130

- `generic_scrape.py`: `_section_candidates` (103) still has the whole-sitemap fallback;
  `_extract_one` (123) runs `bare_extraction` in `asyncio.to_thread` (PR1a);
  `_extract_links_from_html` is at 280 (spec: 268), `_discover_page_links` at 344 (spec: 332),
  `fetch()` at 438. Every fetch goes through `fetch_html`, which returns None on any error.
- `feed_autodiscovery.py`: `discover_sitemap_urls` (199) with the catch-all that turns every
  exception into `[]` (224-235); `_GUESSED_SITEMAP_PATHS` (246); `_parse_sitemap_entries` (284)
  returns `(loc, lastmod)`; `_newest_first` (347); `_crawl_sitemaps` (363), robots.txt at 375,
  empty documents skipped at 393-395.
- `article_extract.py`: `fetch_html` (60) returns None for every status of 400 or more. There is
  **no `fetch_html_checked`**: the spec names it as if it existed. This unit adds it.
- `scheduler._ingest_once`: the known-URL query is in the commit session (now 745-752); the
  tunables are read after `plugin.fetch()`; `_classify_error` maps `httpx.HTTPStatusError`
  401-403 to `blocked`, 429 to `http error`, and `asyncio.TimeoutError` to `network` (PR9a).
- `NoArticleListError` is in `app/sources/base.py` and is a setup state (PR9a).
- `ingest_rules.IngestTunables.max_undated_per_poll` (default 2) exists (PR4a).
- Pins: `test_b1_b8_coverage_gaps.py:285-319` (`fetch_html` signature and header merge),
  `:221-283` (`_extract_one` passes `custom_headers=custom_headers`, `fetch()` passes
  `custom_headers=self.custom_headers`, `__init__` reads `custom_headers`).

### Design

- **`article_extract`:** `FetchRefusedError(httpx.HTTPStatusError)` for 401, 402, 403 and 429.
  `fetch_html` keeps the header merge in its own body and gains `return_final_url`.
  `fetch_html_checked` delegates to it and raises instead of returning None: refusals as
  `FetchRefusedError`, and connect errors and timeouts as the httpx error. With
  `raise_for_status=True` (only for the user's own listing URL or sitemap) any other 4xx or 5xx
  raises `httpx.HTTPStatusError`; otherwise those still read as an empty document.
- **`feed_autodiscovery`:** `_parse_sitemap_entries` returns `(url, date, title)` (the date is a
  news sitemap's publication date, else `<lastmod>`). `_crawl_sitemaps` orders and skips
  sitemaps with `order_sitemaps`, keeps entries that pass `url_ok`, parses and filters each
  document in a thread, and returns `Candidate(url, published, title)` newest first. A new
  `discover_sitemap_entries(..., headers=, raise_errors=)` returns them; `discover_sitemap_urls`
  keeps returning `list[str]` and never raises, for `/auto`. With `raise_errors`, an empty crawl
  raises: the input sitemap's own failure; a refusal when every sitemap document was refused
  (a refused robots.txt alone never counts); a network error; the crawl budget's
  `asyncio.TimeoutError`.
- **`generic_scrape`:** `plan_candidates(url, sitemap_url, link_pattern, headers, known_urls)`
  → `Plan(mode, candidates, fresh)` serves `fetch` and `probe`.
  - Two reads, the listing page and the sitemap. A section URL reads the page first; a site
    root or an explicit `sitemap_url` reads the sitemap first; a URL that is itself a sitemap
    reads only the sitemap. The first read that finds articles wins.
  - The page read uses the link pattern's prefix filter (today's `_extract_links_from_html`)
    when there is one, and `listing_links` otherwise. The sitemap read is scoped to the link
    pattern, else the URL's section. No whole-sitemap fallback.
  - Every candidate passes `url_ok` and the link pattern.
  - Empty raw discovery raises the most telling failure (a refusal, then the user's own URL
    failing, then a network error), else `NoArticleListError`. With candidates, failures of
    the other read are ignored. `probe` and `/auto` never raise.
  - Stored URLs are skipped through the injected `known_urls` before extraction.
- **`_extract_one`:** `fetch_html_checked(..., return_final_url=True)`, then one
  `asyncio.to_thread` call that runs `bare_extraction` and `score_page(owner_configured=...)`. A
  page under `ACCEPT_SCORE` is rejected. A refusal raises; other failures return None, as today.
- **`fetch()`:**
  - A refused page is not marked extracted.
  - When every fetch in a non-empty batch was refused, one page of the site outside the batch is
    fetched as a control. If it is refused too (or there is none), the poll raises the refusal
    (`[blocked]`, or `[http error]` for 429). If it loads, the refusals are the pages' own
    (members-only pages), and the 401-403 ones are marked so they don't block the feed.
  - Undated pages: at most `max_undated` per poll, and only above the first stored candidate,
    the same rule `ingest_rules` applies to every other feed type. Pages past the cap are marked.
- **`scheduler._ingest`:** reads the tunables before the fetch and passes `known_urls` and
  `max_undated` to a plugin with `takes_ingest_hints`. `known_urls` runs the same known-URL
  query as the commit session (`_stored_urls`), in its own short session.

### Tests

New: `test_generic_scrape_skips_stored.py`, `test_generic_scrape_errors.py` (the spec's website
error cases, through the real `fetch_html_checked` and crawl with a fake transport), and plan,
scoring and undated-cap cases. Rewrites: the tests the spec lists, plus the extraction fakes that
need `**kwargs` and the tests that patch `fetch_html` in `generic_scrape`.

## What changed

- **`backend/app/article_extract.py`**
  - `REFUSED_STATUSES` (401, 402, 403, 429) and `FetchRefusedError(httpx.HTTPStatusError)`, so
    `scheduler._classify_error` reads a refusal from its status code: `[blocked]` for 401-403,
    `[http error]` for 429. The Test route's `except httpx.HTTPStatusError` reads it the same way.
  - `NETWORK_ERRORS` (`httpx.TransportError`, `httpx.TooManyRedirects`). The SSRF hook's plain
    `httpx.RequestError` is not one: a redirect to a denied host still reads as no document.
  - `fetch_html` gains `return_final_url` (returns `(body, final_url)`) and two keyword flags that
    only `fetch_html_checked` sets. Its header merge stays in its own body, so the B4 pins
    (`test_b1_b8_coverage_gaps.py:285-319`) hold. With neither flag it behaves exactly as before.
  - `fetch_html_checked` (new) raises a refusal as `FetchRefusedError` and a transport failure
    as the httpx error. With `raise_for_status`, any other 4xx or 5xx raises
    `httpx.HTTPStatusError`; without it, those read as an empty document, as in `fetch_html`.
    A host that doesn't resolve raises `httpx.ConnectError` (`unresolved_host_error`, review
    fix); a host in a denied range still reads as no document.
  - `most_telling_error(errors)`: a 401-403 refusal, then a 429, then any other HTTP status,
    then a network error or `asyncio.TimeoutError`, then anything else.
- **`backend/app/feed_autodiscovery.py`**
  - `_parse_sitemap_entries` returns `([(page, published, lastmod, title)], [(nested,
    lastmod)])`: a news sitemap's `<news:publication_date>`, the `<lastmod>` and `<news:title>`,
    kept apart (review fix; the first cut merged `<lastmod>` into the date).
  - `_crawl_sitemaps` returns `Candidate(url, published, title, modified, place)` newest first,
    ordered by the publication date, else `<lastmod>`, else a date in the URL. `published` is
    only a publication date, `modified` only the `<lastmod>`, and `place` the entry's place in
    the site's own order (round 3).
    - It orders and skips sitemaps with `article_detect.order_sitemaps`, both the robots.txt
      list and each index's nested list (newest first within a class).
    - It keeps entries that pass `url_ok` against the crawl's site, below the feed's section
      (`section=path_prefix`, round 5).
    - Each document is parsed and filtered in `asyncio.to_thread` (`_read_sitemap`): `url_ok`
      costs about 90 µs per URL, so a 5000-entry sitemap would hold the event loop for about
      half a second.
    - Same-site checks use `article_detect.same_site` (the public-suffix list). The old
      `_site_key` / `_same_site` approximation and the unused `_parse_sitemap` are gone.
    - Headers go on every request, robots.txt included.
    - Failures are collected per document. A document that fails is skipped, as before. With
      `raise_errors`, an empty crawl raises, in order: the input sitemap's own failure (the
      feed's `sitemap_url`, read as the sitemap whatever its address looks like, or its URL when
      that looks like a sitemap; a 404 there is `[not found]`; one that loads but names nothing
      and doesn't look like a sitemap is followed by the site's robots.txt and usual paths,
      round 2); a refusal when every sitemap
      that could list pages was refused (a sitemap index that loaded counts neither way, and a
      refused robots.txt reads as no robots.txt and never counts on its own); a network error.
  - `discover_sitemap_entries(page_url, limit, path_prefix, *, headers, raise_errors,
    input_is_sitemap, owner_pattern)` (new) wraps the crawl in the 30 s budget. Without
    `raise_errors` nothing raises (`[]`); with it, the crawl's error, the budget's
    `asyncio.TimeoutError` (`[network]`) or a host that doesn't resolve (`[network]`)
    propagates.
  - `discover_sitemap_urls` keeps returning `list[str]` and never raises; `/auto` calls it as
    before. It now goes through the same filters, so auto-add makes a website feed only when a
    sitemap lists an article-shaped URL (root cause "Auto-add makes a scraper if the sitemap
    lists any URL").
  - `_looks_like_sitemap` is public as `looks_like_sitemap` (`generic_scrape` uses it).
- **`backend/app/sources/generic_scrape.py`**
  - `plan_candidates(url, sitemap_url, link_pattern, headers, known_urls, *, limit,
    raise_errors) -> Plan(mode, candidates, fresh, fallback)` serves `fetch` and `probe`.
    `fallback` (round 5): the second read gave the candidates.
    - A section URL reads the page, then the sitemap. A site root, a feed with a
      `sitemap_url`, or a URL that looks like a sitemap reads the sitemap, then the page. The
      first read with candidates wins.
    - The page read (`_discover_page_links`) keeps today's prefix filter with a link pattern
      and uses `listing_links` without one. Every link passes `url_ok`, as the owner's choice
      under the link pattern (`owner_pattern`), and judged below the feed's own section
      (`section`: the pattern, else the address's section; round 5). The page is fetched with
      `raise_for_status`, because it is the feed's own address. `listing_links` resolves against
      the final URL when the page redirected within the site. The parse runs in a thread.
    - The sitemap read is scoped to the link pattern, else the URL's section. The whole-sitemap
      fallback is gone (`_section_candidates` is removed; `plan_candidates` replaces it).
    - With `raise_errors`, an empty plan raises `most_telling_error` of the reads' failures,
      else `NoArticleListError`. A read's failure is ignored when the other read finds
      candidates.
    - `known_urls` marks stored candidates; `fresh` leaves them out.
  - `_extract_one(url, custom_headers=None, *, owner_configured=False, listed_date=None)`
    fetches with `fetch_html_checked(..., return_final_url=True, raise_for_status=True)`. One
    `asyncio.to_thread` call (`_read_page`) runs `bare_extraction` and then `score_page(html,
    url, final_url=..., text=..., owner_configured=..., listed_date=...)`, passing the text it
    already extracted. A page under `ACCEPT_SCORE` is rejected, with the reasons in a DEBUG
    line. A refusal raises, and so does a failure that passes (a network error, a 5xx); a page
    that is gone (404, 410, other 4xx), not HTML or not an article returns None. A page with no
    date of its own takes `listed_date` (the list's publication date).
  - `GenericScrapePlugin.fetch(*, known_urls=None, max_undated=None)` (`takes_ingest_hints`):
    - plans with `raise_errors=True`, then takes up to 10 fresh candidates not in
      `_extracted_urls`;
    - a refused page is not marked extracted, nor is a page whose fetch failed in a way that
      passes (`_page_failed`: marked after 3 failed polls in a row);
    - when every page of a non-empty batch was refused, `_settle_refusals` fetches a control:
      the stored stories newest first, then pages read and not kept that answered with a page,
      then candidates not tried yet; never a page that was gone, wasn't HTML, kept failing or
      refused everyone (rounds 2 and 3). A gone one, 404 or 410, passes to the next, up to 3.
      Serves a page: the 401-403 pages are marked, and a 429 stays unmarked. Refused, a 5xx, no
      answer, an answer with no page in it (not HTML, empty), or no control: it raises the
      refusal and leaves the pages unmarked;
    - `_cap_undated`: at most `max_undated` undated pages per poll, and none that are old
      backlog (`_backlog_rule`, see the review fixes). Pages it drops stay marked. A page left for
      a later poll while it was new keeps that verdict (`_deferred_new`, round 3);
    - `owner_configured` is worked out after the plan: the link pattern, or a `sitemap_url` whose
      sitemap gave the candidates. A page read in a poll whose plan came from its second read
      (`Plan.fallback`: the usual read failed or found nothing) is marked `unvouched` and read
      once more when a poll's usual read lists it (round 2 for a `sitemap_url`, round 5 for
      every feed).
  - `probe` uses the same plan without `raise_errors` and skips refused pages, so the Test
    route's preview never raises.
- **`backend/app/sources/base.py`**: the `KnownUrls` type and `SourcePlugin.takes_ingest_hints`
  (False; True only on `GenericScrapePlugin`).
- **`backend/app/scheduler.py`**: `_stored_urls(session, urls)` is the known-URL query, used by
  the commit session as before and by `_known_urls_lookup` (its own short session) during a
  website feed's fetch. `_ingest_once` reads the tunables before the fetch and passes
  `known_urls`, `max_undated` and `backlog_before` (`_backlog_cutoff`, review fix) to a plugin
  with `takes_ingest_hints`.
- **Docs:** README gains a "Website feeds" subsection after Feed health. ARCHITECTURE.md's
  `_extracted_urls` row and its multi-worker note now describe what it holds, and the review
  fixes add a row for `_page_failures` (the only new process-local state; the control fetch
  and the plan live for one poll), and round 3 one for `_deferred_new`.
  `frontend/scripts/iphone-audit/README.md` is unchanged: no flows or states.

## Deviations from the spec

- **`fetch_html_checked` raises more than refusals.** The spec has it raise "only on 401, 402,
  403 and 429". It also raises transport errors, and any other status when the caller passes
  `raise_for_status`. Without that, two of the spec's own rules can't be implemented:
  - "A 404 counts as not found only on the user's own listing URL or an explicit
    `sitemap_url`" needs the 404 from those two fetches.
  - "a sitemap timeout gives `[network]`" needs the timeout. Most sitemap timeouts are httpx
    read timeouts (10 s), well inside the 30 s crawl budget, and `fetch_html` turned them into
    None, so the crawl would end empty and say `[no list]`.

  The spec's behaviour is kept for every other document: a 404, 410 or other 4xx on robots.txt,
  a guessed path or a nested sitemap still reads as empty, and the crawl moves on. A network
  error on one document is skipped too; it only matters when nothing at all was found.
- **A host that doesn't answer is `[network]`, not `[no list]`.** The spec's zero-yield rule
  says `[no list]` "only when raw discovery is empty". Raw discovery is also empty when every
  request failed to connect, and `[no list]` is a setup state that never backs off or stops.
  So an empty plan raises the most telling failure first (above), and `[no list]` only when
  nothing failed. That includes a host that doesn't resolve (a lapsed domain, a resolver
  that is down): `url_safety` refuses it like a denied address, so `article_extract` asks the
  resolver once more after the safety check fails and raises `httpx.ConnectError` for it
  (review fix; `url_safety.py` is frozen and unchanged).
- **The feed's own empty sitemap is a quiet poll, not `[no list]`** (re-check round). Raw
  discovery is empty, but when the feed's own sitemap (its `sitemap_url`, or its address when
  that looks like a sitemap) loaded as an XML sitemap that names nothing, and the other read
  failed in no way, the poll returns nothing and raises nothing. A news sitemap lists only the
  last two days, so it is an empty urlset between stories, and main read such a poll as one
  with nothing new; `[no list]` (Pick a section) would be the wrong fix.
- **The listing page's own failures count too.** A refused or missing listing page decides the
  error only when the sitemap finds nothing either ("a listing page 403 gives `[blocked]`" is
  that case). Today a section feed whose page 404s but whose section is in the sitemap works,
  and it keeps working (`test_a_missing_listing_page_with_a_working_section_sitemap_still_reads`).
- **A site root also reads its home page.** "A root URL is read sitemap-first" leaves the
  second read open. The home page is read after the sitemap, with `listing_links` (or the link
  pattern), so a blog with no sitemap now finds its posts instead of `[no list]`. It runs only
  when the sitemap found nothing, so a feed that works today reads exactly what it read before.
  So "all 404s with no link pattern give `[no list]`" is tested with a home page that answers
  but lists nothing. A home page that 404s is the feed's own address missing: `[not found]`.
- **A URL that looks like a sitemap reads the sitemap first, then the page.** The spec
  doesn't say. The first cut read only the sitemap, which broke an HTML sitemap page
  (`/sitemap/` with a link pattern) that works on main (review fix). Reading an XML sitemap as
  a page finds nothing at the cost of one fetch.
- **`discover_sitemap_entries` is new, and `_section_candidates` is gone.** The spec keeps
  `discover_sitemap_urls` (`list[str]`, for `/auto`) and gives it and `_section_candidates`
  `raise_errors`. The plugin needs the entries' dates and titles (PR11a's preview counts older
  pages from candidate dates), so it calls `discover_sitemap_entries`, and `discover_sitemap_urls`
  wraps it without `raise_errors`. `plan_candidates` replaces `_section_candidates`, which the
  spec allowed for `test_generic_scrape_probe_budget.py`. The test fakes therefore patch
  `discover_sitemap_entries` (returning `Candidate`s) instead of only gaining `**kwargs`.
- **`_newest_first` is unchanged.** Pages are projected to `(url, lastmod)` in `_crawl_sitemaps`
  before it runs, as the spec says, so `test_sitemap_newest_first.py:54-68` needs no rewrite.
- **Articles refused: a control fetch before reporting blocked.** "If every article fetch in a
  non-empty batch got 401 or 403, raise blocked" would stop a feed whose only new page is a
  members-only post. That page is never marked, so every poll's batch would be that page
  alone, refused, and the sixth poll would auto-disable the feed. This is common after a
  restart, when the only unstored candidate left is the refused one. So when the whole batch is
  refused, one page of the site outside the batch is fetched first. The controls are the stored
  stories, newest first, then pages the feed read and didn't keep that answered with a page
  (too old, not an article, over the undated cap; round 3: a site that posts rarely has stored
  nothing), then candidates not tried yet. A page that was gone, wasn't a page, kept failing or
  was a members-only page skipped before is never one (round 2), nor a page still being retried
  after a 5xx (round 3):
  - refused too, a 5xx, no answer, an answer with no page in it, or no other page: the spec's
    rule (raise, pages unmarked);
  - gone (404, 410): the next candidate is tried, up to 3, so a deleted stored story doesn't
    decide;
  - serves a page: the pages refuse everyone, so they are marked and the poll succeeds.

  A 429 is treated as a refusal (`[http error]`, Check now) and is never marked, because a rate
  limit passes.
- **The undated cap also drops old undated backlog,** the other half of PR4a's rule for every
  feed type. Without it, after each restart the plugin would store up to two old undated pages
  per poll that it had passed over before. Which pages are backlog depends on the list's order
  (review fix; the first cut used position everywhere): listed below a stored story on a
  listing page, and in a sitemap for a page with no `<lastmod>` (round 2), by its place in the
  sitemap's own order, not the candidates' date order (round 3), below any stored story whatever
  the stored stories' own dates (round 4's test; round 5 tests both orders of the stored
  stories' dates); in a sitemap, last modified before the poll's backlog line. On a listing
  page, stored stories that lead the list above a page that isn't stored, with a stored story
  further down, are pinned (a sticky post), and the line is that story further down (final
  round); a page the feed read and passed over further down counts as such a story too
  (re-check round), so a first fetch that stores only the sticky posts (the cap) doesn't make
  them the line. A page a poll left for later (refused, failed in a way that passes, read while the
  usual read gave nothing or while one of its sitemaps failed) while it was new is still new
  when it is read, even below a story stored in between (rounds 3 and 5, final round). The rule
  reads the order of the read that gave the poll's plan; a page dropped in a poll whose usual
  read gave nothing (round 5), or one of whose sitemaps failed (final round), is read once more
  when a complete usual read lists it, and judged by that read's order and dates. Pages the cap
  drops stay marked. PR4a's notes
  suggested leaving them unmarked, but then every poll would fetch them again only to drop
  them again, up to 8 wasted fetches a poll on an undated site.
- **`url_ok` applies under a link pattern too,** as the spec's raw-discovery rule says, but
  without courlan (review fix) or its 2-to-5-letter ending rule (round 3): the pattern is the
  owner's article signal. The prefix filter itself is unchanged, and our own path rules and file
  endings still apply, but the segment rules (listing, record and static-section words) judge
  only the segments below the section the feed reads: its link pattern, else its address's
  section, is the owner's choice (round 5). So `/about/news/<slug>` on a feed pointed at
  `/about/news`, and `/products/<x>` under a `/products/` pattern, are candidates, which
  `score_page` judges; a page's own name, date archives and file endings are judged everywhere.
  Only a page directly under a static section (`/about/<x>`, `/pages/<x>`, `/policies/<x>`) is
  static (re-check round, narrowed from round 5's news-section exception): deeper pages are
  judged like any path on every feed, so `/about/news/<slug>` and
  `/about-us/news-and-media/<slug>` are candidates, and `score_page` judges
  `/about/team/<name>`, as main fetched every sitemap entry. A folder's own index page
  (`/news/<slug>/index.html`, `default.aspx`) is judged as the folder (re-check round): courlan
  read it, shown as `/x/x/index.html`, as a home page. A code word that ends
  a software name (`.js`, `.json`, `.css`, `.xml`) is a file's ending only at the very end of the
  path, not before a `/` or under `.html` (round 4). A sitemap's entries aren't judged by the
  2-to-5-letter ending rule either (final round): a sitemap lists pages, and main fetched every
  entry (`.jhtml`, `.nsf`). A listing page's links keep the rule, as courlan read them on main.
  On a listing page the feed's own section is lifted only when the page has no list without it
  (final round), so a category page's subcategory links don't outvote its posts.
- **Video and image sitemaps are read; a sitemap's name counts by its last word** (final round).
  The spec skips media sitemaps, and PR10a classed video and image sitemaps as media. Their
  entries are the pages that show the video or image, though, and Yoast names a `video` post
  type's sitemap `video-sitemap.xml`. Only attachment sitemaps (`attachment`) are skipped as
  media now; "media" names a site's media section and its releases (re-check round). A name of
  several words counts by its last word, so a custom post type's `media_release-sitemap.xml` or
  `product_update-sitemap.xml` is read, while `post_tag-sitemap.xml` and
  `product_cat-sitemap.xml` are still taxonomies. After "sitemap", WordPress core's provider
  word and the last word of the rest count (re-check round), so
  `wp-sitemap-posts-media-release-1.xml` lists posts and `wp-sitemap-taxonomies-region-1.xml`
  a taxonomy. main read every
  sitemap; the spec's skip list (taxonomy, author, product, media) still applies to the
  sitemaps that list archives, people, products or attachment pages.
- **Article pages that fail in a way that passes are asked for again.** The first cut marked a
  page that timed out or answered 5xx, so a transient failure lost the story (review fix). Now
  only a page that was read (gone, not HTML, not an article, kept) is marked. A failed page is
  left for the next poll and never fails the poll (only an all-refused batch raises); after 3
  failed polls in a row it is marked, so a page that never answers can't take a slot of every
  poll.
- **No owner VPS probe before merging.** Per the owner's decision of 2026-10-20 (PROGRESS.md),
  the read-only probe runs after deploying instead, from `docs/DEVICE_CHECKS.md`.

## Verification

- **Backend pytest** (database `popping_test_b`, worktree `PYTHONPATH`): **13 failed, 1301
  passed**, both before and after rebasing onto main 2d5f052 (PR1b, frontend only). The 13
  failures diff clean against the DNS-only baseline in `/root/popping-env/baseline.txt`. Main
  9e41130 gave 13 failed, 1238 passed in the same setup; this unit adds 63 tests:
  - `test_generic_scrape_errors.py` (30): the website error cases through the real fetch path.
    Only the transport is a fake (`httpx.MockTransport` behind `fake_dns`), so
    `fetch_html_checked`, the URL safety check, the SSRF hook, the crawl, `listing_links`,
    trafilatura and `score_page` all run. The cases:
    - robots.txt 404, `/sitemap.xml` 404 and a working `/wp-sitemap.xml` (its page, taxonomy and
      user sitemaps never read);
    - robots.txt 403 with a working sitemap; a refused robots.txt alone; every sitemap refused;
      one refused sitemap beside missing ones;
    - the Yoast index read in class order, end to end, and the About page never fetched;
    - all 404s with a home page that lists nothing (`[no list]`);
    - a sitemap read timeout, the crawl budget running out, and a host that refuses
      connections (all `[network]`);
    - a missing `sitemap_url` and a missing listing page (`[not found]`), a refused listing
      page (`[blocked]`), a rate-limited one (`[http error]`), and a missing listing page whose
      section sitemap still works;
    - article pages refused (`[blocked]`, unmarked, read after a Safari User-Agent is set); a
      members-only page beside a stored story (skipped, the poll succeeds); every page refused
      with stored stories (`[blocked]`); a 429 page that stays for later; a mixed poll;
      rate-limited articles (`[http error]`); a redirect to the home page rejected;
    - a listing page that brings its post and rejects a soft 404;
    - a link pattern that accepts an undated ollama-style page (the spec's test), and the same
      page rejected without one;
    - `probe` and `discover_sitemap_urls` never raise; `fetch_html_checked`'s contract; the
      final URL; news sitemap titles and dates.
  - `test_generic_scrape_skips_stored.py` (11): the spec's stored-URL cases (skipped before
    extraction; an all-stored poll returns `[]` without raising; not fetched again after a
    restart); the undated cap (at most `max_undated`, its default, dated pages uncapped, undated
    backlog below a stored story); and three `scheduler._ingest` runs on the test database. Those
    show the lookup and the knob reach the plugin, and that an all-stored poll is recorded as a
    success.
  - `test_generic_scrape_plan.py` (13): read order per URL kind, scoping, failure precedence,
    no raise without `raise_errors`, headers on every read, `owner_configured`, and the listing
    parse off the event loop.
  - `test_sitemap_crawl_filters.py` (9): parse shapes, `/auto` on a site with only static pages,
    a page sitemap skipped beside a post sitemap, Shopify blogs only, non-article URLs dropped,
    dates, and the sitemap parse off the event loop.
- **Rewritten tests** (the spec's list, plus what the change broke):
  - `test_generic_scrape.py`: the discovery fakes patch `discover_sitemap_entries`; the
    extraction fakes take `**kwargs`. The per-poll-cap, concurrency and extraction-error items
    are dated, so they test what they did before and not the undated cap.
  - `test_generic_scrape_page_links.py`:
    - :88-103 now asserts the page is read with `listing_links`, not the prefix filter, and that
      `NoArticleListError` is raised while `probe` returns `[]`;
    - :114-145 is read page-first, and the sitemap is never asked;
    - :151 (`test_plugin_sitemap_wins_over_page_links`) uses sitemap entries under the pattern
      and asserts the read is scoped to it;
    - :432-446 expects `NoArticleListError` and patches `fetch_html_checked`.
  - `test_generic_scrape_sitemap_url.py`: the three tests at :94-170 expect
    `NoArticleListError` and keep their checks on the discovery call's arguments (the home-page
    read is stubbed, so nothing touches the network). The dedup test's items are dated, and its
    second poll returns `[]` without an error. The probe test also asserts `raise_errors` is
    False.
  - `test_generic_scrape_probe_budget.py` patches `plan_candidates`.
  - `test_sitemap_newest_first.py`: `_serve` patches `fetch_html_checked`; :71-88 read
    `Candidate.url` (and the URL date); :100-109 now asserts there is no fallback.
  - `test_ingest_misfire_grace.py` (PR1a) and `test_generic_scrape_parse_published.py` patched
    `generic_scrape.fetch_html`; they now patch `fetch_html_checked`. The two date tests also
    accept their stub page in `score_page`, because they test the date, not the scoring.
- **Mutation check.** A script broke each guarded line, ran the tests that guard it, required a
  failure, and restored the file byte for byte (checked by SHA-256). The final run caught all 46
  mutations, in all 67 test runs.
  - `article_extract`: refusals never raise; `raise_for_status` ignored; network errors
    swallowed; `most_telling_error` unranked; the final URL not returned.
  - The crawl:
    - robots.txt refusal raising;
    - every-sitemap-refused not raised, and one refusal blocking;
    - the input sitemap's 404 read as empty;
    - network errors and the budget timeout swallowed;
    - nested and robots.txt sitemaps not ordered;
    - no `url_ok`;
    - the parse back on the loop;
    - news titles and dates dropped.
  - The plan:
    - section and root read orders swapped or cut;
    - `sitemap_url` not read first, and a sitemap URL also read as a page;
    - scoping removed, and the link pattern not scoping;
    - the whole-sitemap fallback put back;
    - a failed read sinking the plan;
    - raising without `raise_errors`, and an empty plan not raising `[no list]`;
    - stored URLs not skipped, and an all-stored poll raising;
    - headers dropped;
    - the prefix filter used without a pattern;
    - the listing parse on the loop;
    - the page read without `raise_for_status`.
  - Extraction: `score_page` ignored; `score_page` without the final URL; `owner_configured`
    never set; extraction on the loop.
  - The batch:
    - refused pages marked;
    - the all-refused rule removed;
    - no control fetch, and a refused control counted as loading;
    - a 429 page marked;
    - no undated cap, undated backlog kept, and dated pages capped.
  - The scheduler: no hints passed; no undated knob passed.

  The first run had three survivors, each fixed:
  - "Refusals never raise" survived its listing-page test: the listing fetch uses
    `raise_for_status`, so a 403 there raises anyway. The mutation now runs the article-page
    test, which has no such backstop.
  - "robots.txt sitemaps not ordered" survived because `url_ok` also drops the static pages.
    It got its own test (a page sitemap listed before a post sitemap is never fetched).
  - One mutation string matched twice and was made unique.
- **`ruff check --select F,E9,B`** on every changed app file and every new test: clean. One
  pre-existing B905 in `generic_scrape.py` was fixed (`zip(..., strict=True)`). The other
  findings in the edited old test files and in `scheduler.py` were already on main. CI runs no
  backend linter.
- **Frontend:** no frontend file changed. The backend tests that pin frontend source ran in the
  full pytest above.
- **The VPS probe** (Device checks below): its quoting was checked by splitting the command the
  way the owner's shell and the container's `sh` will, and its SQL ran on sample rows in a
  scratch transaction on the test cluster.
- **Security checklist:** no new route and no stored JSON. Every new request goes through
  `fetch_html` (`fetch_html_checked` delegates to it), so the URL safety check, the per-hop SSRF
  hook and the final-URL check apply to robots.txt, sitemaps, listing pages, article pages and
  the control fetch. Website feeds now send their `custom_headers` to robots.txt, sitemaps and
  the listing page as well as article pages, all on the feed's own site; the route layer's
  header denylist still applies. `trafilatura`, `courlan`, `lxml` and `defusedxml` are used only
  as parsers. Error messages carry the status and the URL the feed already has, never a body.

## Review fixes

The review agents' confirmed findings, each fixed with a test that fails without the fix (the
mutation run below):

1. **A control page that answers 404 or 5xx counted as loaded.** `_settle_refusals` fetches the
   control with `raise_for_status` and counts it only when it returns a page. A 5xx, a
   refusal, a network error, a non-HTML answer or an empty body is not the site serving us; a
   404 or 410 passes to the next candidate (up to 3). (Which pages are controls changed in
   round 2, fixes 1 and 2.)
2. **An HTML sitemap page was never read as a page.** A URL that looks like a sitemap now reads
   the sitemap, then the page, like a site root.
3. **A served sitemap index whose children are all refused read as `[no list]`** (reported
   twice). The refusal rule leaves out the indexes that loaded: an index whose post sitemaps
   are all refused raises the refusal (`[blocked]`, or `[http error]` for 429s). A lone guessed
   403 beside 404s, an index with one refused and one missing child, and a refused sitemap
   beside one that loads are still not blocked.
4. **A host that doesn't resolve read as `[no list]`.** `unresolved_host_error` in
   `article_extract` (used by `fetch_html` with `raise_errors` and by
   `discover_sitemap_entries`) raises `httpx.ConnectError` (`[network]`). A host in a denied
   range still reads as no document.
5. **Article pages that timed out or answered 5xx were marked** (reported twice). See the
   deviation above: `_extract_one` re-raises those, `fetch()` leaves them unmarked
   (`_page_failed`), and a 404 or 410 page stays marked.
6. **A dead `sitemap_url` vouched for the home page's links.** `owner_configured` is worked out
   after the plan, so links `listing_links` found are scored as any page is. An INFO line
   names the `sitemap_url` that listed nothing. The feed still reads its page: raising
   `[not found]` there would stop `sitemap_url` plus `link_pattern` feeds that work today.
   (Round 2, fix 3: such pages are read again once the sitemap lists them.)
7. **Dated articles were scored as undated.**
   - `Candidate.published` (a listing card's `<time datetime>` or a news sitemap's
     publication date) reaches `score_page` as `listed_date`, which counts like the page's own
     date and cancels the `og:type=website` −3. The item also takes it as `published_at` when
     the page carries no date of its own. A sitemap's `<lastmod>` is kept apart
     (`Candidate.modified`) and never dates a page; trafilatura's date is never used for
     scoring.
   - `_body_date` also reads a plain `<time datetime>` inside the first h1's own `<article>`
     (not in its footer, aside or nav, and not marked as an update), when only one article
     holds an h1. New fixture `astro-post.html` (the Astro blog starter's post layout).
   - Still rejected: About, Yoast About, the long listing and the listing fixtures.
8. **"Below the first stored candidate" dropped new pages in `<lastmod>` order.** The position
   rule now applies only to a listing page. In a sitemap, an undated page is backlog when its
   `<lastmod>` is older than the poll's backlog line (the scheduler's `_backlog_cutoff`: the
   baseline minus `backfill_hours`, or `max_age_days`, whichever is later; None on a first
   fetch), or when it has no date at all after the first fetch. A page can't have been
   published after it last changed, so the first is sound; the second can't tell a new
   undated page from old backlog in an order that says nothing. (Round 2, fix 4: a page with
   no `<lastmod>` uses the position rule instead.)
9. **`url_ok`'s courlan step rejected real slugs** (`png-vs-jpg`, `node.js-22-released`,
   `.ece`, `.cms`). File types are judged by the page's own ending (`_FILE_EXTENSIONS`);
   courlan sees a one-word page name as it is and a longer slug as a neutral word, with an
   ending that is neither a file nor a web page it knows left off; and under the owner's link
   pattern (page links and the sitemap read) courlan isn't asked (`owner_pattern`).
   Blogspot's `_archive.html` pages, which only courlan rejected, are rejected by our rules.
   (Round 2, fix 6: any other 2 to 5 letter ending is a file again.)
10. **No request-level test of the feed's headers.** Two tests serve robots.txt, a sitemap index
    listed only there, its post sitemap, the article page and a section's listing page only to
    a Safari User-Agent, and assert that every request carries it. Each fails when
    `custom_headers=headers` becomes `None` on the robots.txt fetch, the sitemap document fetch
    or the listing page fetch (mutation run below).
11. **An explicit `sitemap_url` that didn't look like a sitemap was ignored.**
    `discover_sitemap_entries(..., input_is_sitemap=True)` reads the owner's URL as the sitemap
    whatever its path (`/?sitemap=index`), so a 404 there is `[not found]`. `/auto` doesn't set
    it. (Round 2, fix 5: one that loads but names nothing falls back to robots.txt.)
12. **The VPS probe contradicted Step 0 and its 24-hour count couldn't catch a stopped feed.**
    The baseline is its own line, marked as the one check that runs before Step 0's
    `popping-update`. The probe now has a `since_deploy` column (stories fetched since the
    backend container was created, read from `docker inspect`), and the line a day after
    deploying fails a feed whose `since_deploy` is 0 while its site kept publishing.

### Verification of the review fixes

- **Backend pytest** (database `popping_test_b`, worktree `PYTHONPATH`): **13 failed, 1371
  passed**. The 13 are the DNS-only baseline (`/root/popping-env/baseline.txt`), unchanged.
  The 11 DNS-only source-route tests among them pass with a fake resolver (their four files:
  50 passed).
- **New and rewritten tests:** `test_generic_scrape_errors.py` (+27 cases: controls, the HTML
  sitemap page, refused index children, DNS, transient article failures, gone pages, the dead
  and working `sitemap_url`, list dates, the Astro post, the owner's pattern, headers on every
  request, `/?sitemap=index`), `test_generic_scrape_skips_stored.py` (the undated cap per list
  order, the backlog line, and the scheduler passing it), `test_article_detect.py` (the
  review's `url_ok` slugs, `owner_pattern`, the wider body date, `listed_date`, the static
  pages that stay rejected), `test_generic_scrape_plan.py` (read order for a sitemap-looking
  URL, `input_is_sitemap`, `owner_pattern`, `owner_configured` after a dead `sitemap_url`),
  `test_sitemap_crawl_filters.py` and `test_sitemap_newest_first.py` (`<lastmod>` kept apart).
- **Mutation check of the fixes.** A script broke each guarded line, ran the tests that guard
  it, required a failure, and restored the file byte for byte (SHA-256). 41 mutations, 51 test
  runs, all caught:
  - control: a gone control not skipped, a 5xx control counted as serving, a control with no
    page counted as serving;
  - a sitemap-looking URL read only as a sitemap;
  - the index rule: indexes counted as refusable, every loaded document counted as an index,
    and the looser "any refusal with nothing loaded" rule;
  - DNS: `fetch_html` and the sitemap discovery not raising, and a denied host read as
    unresolved;
  - articles: a 5xx read as gone, network errors swallowed, no `raise_for_status`, a failed
    page marked, a failing page never passed over, a gone page left unmarked;
  - `owner_configured` from the `sitemap_url` alone;
  - dates: the listed date not scored, not passed by `fetch()`, not dating the item, ignored by
    `score_page`; the plain article `<time>` ignored; a footer or "updated" `<time>` counted;
    `<lastmod>` merged into the publication date;
  - the undated cap: position in a sitemap, no position on a listing page, `<lastmod>` not held
    against the line, an undated no-date page kept after the first fetch, the scheduler passing
    no line, the line ignoring the baseline;
  - `url_ok`: courlan seeing the whole slug, our file-type check dropped, courlan asked under
    the owner's pattern, page links and the sitemap read not passing `owner_pattern` (the
    sitemap read's half only at the `plan_candidates` level, where the discovery call is a
    fake; inside `feed_autodiscovery` all three reverts survived, round 2 fix 9);
  - headers dropped on the robots.txt fetch, the sitemap document fetch and the listing page
    fetch, one at a time;
  - the explicit `sitemap_url` read by its looks, and the plan not flagging it.

  The first run had six survivors, each fixed: two mutations had been aimed at the wrong tests;
  `fetch_html`'s own unresolved-host raise and the page links' `owner_pattern` had no test that
  reached them (now `fetch_html_checked` is asserted directly, and a link-pattern listing with a
  `/library/home` link is planned); and "every loaded document counted as an index" got its
  own test (a refused sitemap beside one that loads is not blocked).
- **`ruff check --select F,E9,B`** on every changed app file and test: clean, except the 5
  findings in `scheduler.py` that are on main.
- **The probe:** the exact command in `docs/DEVICE_CHECKS.md` was run with `docker` stubbed to
  run its container half locally, against sample rows in a scratch schema on the test cluster
  (dropped afterwards): it parses, and `since_deploy` reads 0 for a feed whose stories all came
  in the 24 hours before the deploy.

## Review fixes (round 2)

The second review's nine confirmed findings. Each fix has a test that fails when the fix is
reverted in place (Verification below). None was skipped.

1. **Pages already read as gone or not a page were picked as controls** (major). The controls
   were the candidates in date order, so a page this feed had already read and not kept (three
   deleted stories, a PDF at a page-like address, a page that kept failing) came before the
   stored stories. A lone members-only page then read `[blocked]` on every poll after the first,
   and the sixth stopped the feed; on main the page is just marked. `_settle_refusals` now asks
   the stored stories first, newest first (each loaded as an article before), then candidates
   not tried yet, and never a page in `_extracted_urls` that isn't stored. Tests:
   `test_pages_read_as_not_a_page_or_gone_are_never_the_control` (PDF, three 404s; four polls)
   and `test_a_stored_story_is_the_control_before_a_page_not_tried_yet`. (Round 3, fix 1: a
   page read and not kept that served a page can be a control again.)
2. **A second members-only page blocked the feed** (major). Same cause: the first members-only
   page, marked on an earlier poll, was the next poll's control and answered 403. Fixed by 1.
   Test: `test_a_second_members_only_page_does_not_block_the_feed` (one poll, then three with
   the second page listed; the first page is never asked again).
3. **A `sitemap_url` that failed for one poll lost the owner's pages** (major). A poll whose
   `sitemap_url` gave nothing (a 503, a 429, a timeout, an empty urlset) read the home page's
   links without the owner's signal, rejected the undated pages and marked them, so the next
   poll, with the sitemap back, skipped them until a restart. Such a poll now marks the pages it
   reads `unvouched` (the value in `_extracted_urls`, now `OrderedDict[str, bool]`), and
   `_seen` lets a poll whose candidates have the owner's word (`owner_configured`) read them
   once more. Nothing is fetched again while the sitemap stays down, so a dead `sitemap_url`
   costs no extra fetches; a gone page gets one more fetch when the sitemap returns. The INFO
   line now says the `sitemap_url` "gave no pages" (it said "listed nothing" after a 503 too).
   Test: `test_a_sitemap_url_that_fails_for_one_poll_keeps_its_pages` (503, 429, timeout,
   empty; the third poll fetches nothing). (Round 3, fix 4: such a page isn't backlog when it
   comes back below a story stored meanwhile; fix 7: the test runs three failing polls. Round 5,
   fix 2: every feed whose usual read gives nothing for a poll, not only one with a
   `sitemap_url`.)
4. **A sitemap with no `<lastmod>` dropped every new undated page after the first fetch**
   (minor). In a sitemap, a page with no `<lastmod>` now uses the listing page's rule (above
   the first stored candidate is new, below is backlog); only a page with a `<lastmod>` is held
   against the backlog line. A new page at the top of the sitemap comes in again, as on main and
   in the first cut; an oldest-first sitemap still loses it (Risks). Test:
   `test_an_undated_sitemap_page_with_no_lastmod_is_judged_by_its_place`, which replaces
   `test_an_undated_sitemap_page_with_no_date_at_all_is_backlog_after_the_first_fetch`.
   (Round 3, fix 3: the place is the one in the sitemap, not in the candidates' date order.)
5. **A `sitemap_url` that is the site's address read as no sitemap** (minor). Round-1 fix 11
   read the owner's `sitemap_url` directly whatever it looked like, so a home page or HTML
   site-map page saved there no longer reached robots.txt, as it did on main. When that document
   loads but names no page or sitemap at all and its address doesn't look like a sitemap, the
   crawl now reads the site's robots.txt and usual paths after it (`site_sitemaps`, the code the
   site-URL branch already ran). It counts like an index for the refusal rule, so refused site
   sitemaps still read `[blocked]`; a 404 on it is still `[not found]`. Test:
   `test_a_sitemap_url_that_is_a_page_reads_the_sites_sitemaps` (`/` and `/site-map/`). (Round
   3, fix 5: a web page's lines are never a plain-text sitemap; fix 8: tests for both limits.)
6. **`url_ok` accepted file links with endings it didn't list** (minor). Round-1 fix 9 left
   courlan only a neutral word for the page name, so `.gpx`, `.kmz`, `.rtf`, `.wmv` and other
   unlisted endings passed, and on a section page a table of downloads outnumbered the article
   cards in `listing_links`; the poll fetched files and returned nothing. `_is_file` now
   judges the page name: a file ending (`_FILE_EXTENSIONS`); any other ending of 2 to 5 letters
   that is no web page's (`_PAGE_EXTENSIONS`, now courlan's own whitelist without `.txt` and
   `.xml`) or known news CMS's (`_ARTICLE_EXTENSIONS`: `.ece`, `.cms`, `.ghtml`), as courlan's
   ending test reads it; a file ending under a page ending (`doc.pdf.html`); and a camera's file
   name as the page (`img_1234`, courlan's `[/-](img|jpg|png)` narrowed to the whole name, so
   `img-tags-explained` stays an article). Slugs such as `png-vs-jpg` and
   `node.js-22-released` are still articles. Over the 431 URLs in the tests and fixtures, the
   only ones `url_ok` now rejects that round 1 accepted are files (`.xsd`, `.dtd`, `.xsl`,
   `.srt`, `.vtt`, the new cases) and a YouTube handle with a dot. Tests: the new junk and keep
   cases in `test_article_detect.py`, and
   `test_listing_links_takes_the_post_cards_over_a_larger_table_of_downloads`. (Round 3, fix 2:
   the rule reads only the end of a path, not under the owner's pattern, and `.page`, `.jsf`
   and `.cfml` are page endings. Round 4, fix 1: before a `/` and under a page ending only a
   document or media ending is a file, not `.js`, `.json`, `.css` or `.xml`.)
7. **The upper-case ending case passed without the ending rule** (minor). Its URL sat under
   `wp-content/uploads`, which the path rules reject first. New cases outside any rejected
   section: `/files/Trail-Map.JPG`, and `/news/Episode-12.MP3`, which only `_FILE_EXTENSIONS`
   rejects (`mp3` is not all letters, so fix 6's rule doesn't cover it).
8. **`<lastmod>` ordering had no test that could fail** (minor). The fixture was already newest
   first. `test_an_oldest_first_post_sitemap_is_read_newest_first_by_lastmod` serves a post
   sitemap oldest first and crawls it with `limit=1`. The old test checked only that `<lastmod>`
   is carried and never dates a page; it is renamed
   `test_a_post_sitemaps_lastmod_is_carried_but_never_dates`.
9. **The sitemap half of round-1 fix 9 had no test** (minor). The round-1 mutation list
   overstated it (corrected above).
   `test_sitemap_entries_under_the_link_pattern_are_the_owners_choice` plans a site root with
   `link_pattern=/library/` through the real crawl, whose sitemap lists
   `/library/home` and `/library/riverlm3`, and expects both (only `riverlm3` without the
   pattern).

README's "Website feeds" and ARCHITECTURE.md's `_extracted_urls` row follow fixes 1, 3, 4
and 5.

### Verification of the round-2 fixes

- **Revert in place.** A script replaced each fix with the code it replaced (or the reviewer's
  mutation), ran the tests that guard it, required a failure, and restored the file byte for
  byte (SHA-256; `git diff` hashed the same before and after). All 14 were caught:
  - the control selection (findings 1 and 2): 4 tests fail; the stored-first order alone: 1;
  - `unvouched` pages never read again: 4 (every failure kind);
  - the old backlog rule for pages with no `<lastmod>`: 1;
  - the robots.txt fallback removed: 2; the fallback document counted as a list: 2 (the
    refused case);
  - `url_ok`'s file check back to `_FILE_EXTENSIONS` alone: 9; news CMS endings not allowed: 3;
  - `_FILE_EXTENSIONS` emptied: 2; the ending read without lower-casing: 3;
  - `<lastmod>` dropped from the sitemap order (the reviewer's M12d): 1;
  - the reviewer's M9e, M9h and M9i (`owner_pattern` not reaching the sitemap read): 1 each.
- **The reviewers' scratch probes**, run against this tree: the control probe gives `[]` on all
  8 polls for every variant (one, two or three 404s, a PDF, a 500, an empty page on top); the
  `sitemap_url` fallback probe brings `riverlm0` and `riverlm1` on poll 2 after a 503, a
  timeout, a 429, an empty urlset or a 404; probe C finds the article for a `sitemap_url` of
  `/` and `/site-map/`; the no-`<lastmod>` probe through `scheduler._ingest` stores the new
  page when it is listed at the top and not at the bottom (the Risks entry).
- **PR10b test files** (the 12, plus `test_feed_autodiscovery.py` and
  `test_b1_b8_coverage_gaps.py`): 378 passed, 4 failed, the 4 DNS-only `/auto` route tests
  in the baseline.
- **Backend pytest** (database `popping_test_b`, worktree `PYTHONPATH`): **13 failed, 1396
  passed**. The 13 are exactly the DNS-only baseline (`/root/popping-env/baseline.txt`).
- **`ruff check --select F,E9,B`** on every changed app file and test: clean.

## Review fixes (round 3)

The third review's eight confirmed findings: four regressions (three against main) and four
tests that couldn't fail. Each fix has a test that fails when the fix is reverted in place
(Verification below). None was skipped.

1. **A site that posts rarely read `[blocked]` once a members-only page went up** (major; from
   round-2 fix 1). Round 2 kept every page in `_extracted_urls` out of the controls. A feed that
   had stored nothing (every listed story too old, not an article or over the undated cap) then
   had no control, raised the refusal on every poll and stopped on the sixth; main marked the
   page and stayed quiet. `_extracted_urls` now records whether each page answered with a page
   when it was read (`_Read(served, unvouched)`; `_extract_one` reports it through a `served`
   set that `fetch` passes). The controls are the stored stories, then the pages read and passed
   over that served a page (too old, not an article, over the undated cap), then the candidates
   not tried yet. A page that was gone, wasn't HTML, failed 3 polls or was a members-only page
   skipped before is never one, and neither is a page still being retried after a 5xx
   (`_page_failures`; finding 6's test). Test:
   `test_a_page_read_and_passed_over_that_served_a_page_is_a_control` (too old, not an article;
   seven polls, the members-only page asked once).
2. **`url_ok`'s 2-to-5-letter ending rule rejected real article URLs** (major; from round-2 fix
   6). nyc.gov's `.page` press releases and dotted slugs with a trailing slash
   (`/posts/deploying-to-fly.io/`) read `[no list]` on every poll, even under the owner's link
   pattern, where main and round 1 stored them. The rule now reads only the end of a path, as
   courlan does, so it is skipped for a path that ends in `/`. It is also skipped under the
   owner's link pattern (round 1's behaviour: the pattern is the article signal), where only
   `_FILE_EXTENSIONS` count. `.page`, `.jsf` and `.cfml` join `_ARTICLE_EXTENSIONS`. Tests: the
   new keep cases in `test_article_detect.py` and
   `test_url_ok_reads_an_ending_only_at_the_end_of_the_path`. Round 2's
   `/library/riverlm3.gguf` reject case under the owner's pattern is a candidate again, as in
   round 1 and on main (fetched once, it reads as no page), so it left that list, which gained
   `/library/riverlm3.zip/` (a file ending before a trailing slash). (Round 4, fix 1: before a
   `/`, and under a page ending, a code word such as `.js` is part of the slug.)
3. **A new undated page listed first in a sitemap of dated stories was dropped as backlog**
   (minor; from round-2 fix 4). The position rule for a sitemap page with no `<lastmod>` used
   the candidates' date order, which puts every undated entry after every dated one (a
   `<lastmod>` or a `/YYYY/MM/DD/` URL), so such a page was always "below" a stored story and
   was lost for good; main stored it. `Candidate.place` now carries each sitemap entry's place
   in the site's own order (the documents as the crawl read them, each top to bottom), and the
   rule (`_backlog_rule`, moved out of `_cap_undated`) compares places. Test:
   `test_an_undated_page_is_judged_by_its_place_in_the_sitemap_not_the_date_order` (stored
   stories dated by `<lastmod>`, and by their URLs), through the real crawl and sort. (Round 4,
   fix 2: a test whose stored stories are dated against the sitemap's order.)
4. **A page left for a later poll was dropped as backlog when a story listed above it was
   stored meanwhile** (minor; the `sitemap_url` outage half was a regression from round-2 fix 3,
   the 503 half was lost on main too). A poll that leaves a page for later while it is new
   (refused, failed in a way that passes, or read without the owner's word) remembers it in
   `_deferred_new` (bounded FIFO, 1000) until it is read for good, and `_cap_undated` doesn't
   hold the stories stored since against it. The undated cap still applies. A page left for
   later while it was already backlog (after a restart, below a stored story) is still backlog,
   so a 503 can't turn old backlog into a new story. Tests:
   `test_a_page_left_for_a_later_poll_keeps_its_place` (a `sitemap_url` 503; the page 503 on one
   poll or two; the page 429) and
   `test_a_page_left_for_later_while_it_was_backlog_is_still_backlog`.
5. **A `sitemap_url` that is the site's address didn't reach robots.txt when its HTML had a line
   starting with a URL** (minor; from round-2 fix 5). A formatted `srcset` puts one image
   address per line. The plain-text sitemap parse took those lines as entries, so the document
   "named" something and the fallback never ran: `[no list]`, where main read robots.txt.
   `_parse_sitemap_entries` now reads an HTML document (a doctype, `<html>`, `<head>` or
   `<body>`) as naming nothing; a plain-text sitemap never has those. `listed` is still judged
   before the filters (fix 8). Test: `test_a_sitemap_url_that_is_a_page_reads_the_sites_sitemaps`
   gains a `srcset` page for both addresses.
6. **The control exclusion had no test that could fail** (minor). The round-2 tests always had
   a stored story that loaded first. `test_a_page_that_did_not_serve_a_page_is_never_the_control`
   deletes the only stored story on poll 2 and lists, behind a full refused batch, pages read on
   poll 1 that didn't serve a page (a PDF, three deleted stories, a members-only page, a page
   still failing with 503) before a page not tried yet. The poll returns `[]` and those pages
   are asked for once.
7. **Nothing tested that unvouched pages stay unfetched while the `sitemap_url` stays down**
   (minor). `test_a_sitemap_url_that_fails_for_one_poll_keeps_its_pages` now runs three failing
   polls and asserts each model page was asked for once before the sitemap comes back.
8. **The `sitemap_url` fallback's two limits had no test** (minor).
   `test_an_empty_sitemap_url_is_not_replaced_by_the_sites_other_sitemaps` (an empty urlset at
   `/news-sitemap.xml`: robots.txt and the post sitemap are never asked) and
   `test_a_sitemap_url_whose_entries_are_all_left_out_is_not_replaced` (`/?sitemap=index`
   listing only `/about/` and `/contact-us`: robots.txt is never asked).

README's "Website feeds" and ARCHITECTURE.md's `_extracted_urls` row follow fixes 1, 3 and 4;
ARCHITECTURE.md gains a row and a multi-worker note for `_deferred_new`. `scheduler.py`'s
`_backlog_cutoff` docstring now names `_backlog_rule`, which holds the line (docstring only).

### Verification of the round-3 fixes

- **Revert in place.** A script replaced each fix with the code it replaced (or applied the
  reviewer's mutant), ran the tests that guard it, required a failure, and restored the file
  byte for byte (SHA-256; `git diff` hashed the same before and after). All 19 were caught:
  - fix 1: no page read before as a control: 2 fail;
  - fix 6: any page read before as a control (the reviewer's M1a): 3; a page still failing
    taken as untried: 1; every page read counted as served: 2; a non-HTML answer counted as
    served: 1;
  - fix 2: the ending rule back everywhere: 4; under the owner's pattern only: 1; on a trailing
    slash only: 3; `.page`, `.jsf`, `.cfml` not allowed: 3;
  - fix 3: `place` not carried by the crawl: 2; `place` ignored by the rule: 2;
  - fix 4: the remembered verdict ignored by the cap: 4; unvouched pages never remembered: 1;
    every page left for later exempt (the simpler rule): 1; the verdict dropped while a page
    keeps failing: 1;
  - fix 5: a web page read as a plain-text list: 2;
  - fix 7: the reviewer's M2a: 4; fix 8: M4b: 1, M4c: 1.

  The first run had one survivor, an equivalent mutant: the deferral loop also tested whether a
  page was remembered before, which can't change anything, because an entry stays until the
  page is read for good. The clause was removed, and the "503 on two polls" case was added.
- **Against HEAD 68d6f4b's code** (a scratch export with this round's tests): 19 cases fail
  there: the two `srcset` pages, the dormant site (2), the failing-page control, the four
  left-for-later cases, the two sitemap-place cases and eight `url_ok` cases. The rest pass
  there, as they should: fix 4's backlog guard, fix 2's reject cases and the tests for findings
  6 to 8 guard round-2 code that was right.
- **The reviewers' probes**, run against this tree: the dormant site (p4, and the realistic
  sitemap and page-mode variants) is ok on all 8 polls with the members-only page asked once
  (twice when it first comes in beside the public pages), as on main. The mixed and URL-dated
  sitemaps (p1, p1b) store the new undated page on poll 2, as on main. The `sitemap_url` outage
  and the 503 page (p5) store the model page on poll 2; main lost the 503 one. The Prettier
  `srcset` home page (p3) stores the 3 stories, as on main. `url_ok` (p2) accepts `.page`,
  `.jsf`, `.cfml` and dotted slugs with a trailing slash, and the link-pattern `.page` feed and
  the Hugo feed store all 3 pages on poll 1, as on main. The M1a probe tests (4) and the
  verifiers' dormant and `srcset` tests pass.
- **PR10b test files** (the 12, `test_slice27_deferred_perf.py`, `test_feed_autodiscovery.py`
  and `test_b1_b8_coverage_gaps.py`): 418 passed, 4 failed, the 4 DNS-only `/auto` route
  tests in the baseline.
- **Backend pytest** (database `popping_test_b`, worktree `PYTHONPATH`): **13 failed, 1424
  passed**. The 13 are exactly the DNS-only baseline (`/root/popping-env/baseline.txt`).
- **`ruff check --select F,E9,B`** on every changed app file and test: clean, except the 5
  findings in `scheduler.py` that are on main (619000f gives the same 5).

## Review fixes (round 4)

The fourth review's two confirmed findings: one regression against main and one test that
couldn't fail. Each has a test that fails when the fix is reverted in place (Verification
below). Neither was skipped.

1. **Slugs that end in a software name were read as files** (minor; from round-2 fix 6 and
   round-3 fix 2, a regression against main). `_is_file` checked `_FILE_EXTENSIONS` on the
   page name even when the path ends in `/` (Hugo and Eleventy: `/posts/whats-new-in-vue.js/`,
   `/posts/understanding-package.json/`), under the owner's link pattern, and on the name under
   a page ending (Jekyll: `/2026/10/18/whats-new-in-vue.js.html`). Those pages were never
   fetched; main stored them from a sitemap and under a link pattern, because it never ran
   `url_ok` there. `_CODE_EXTENSIONS` (`js`, `mjs`, `json`, `css`, `xml`) now name a file only at
   the very end of the path: before a `/` and under a page ending only a document or media
   ending counts (`_DOCUMENT_EXTENSIONS`, the rest of `_FILE_EXTENSIONS`), so
   `/files/annual-report.pdf/`, `/library/riverlm3.zip/` and `doc.pdf.html` are still files.
   `_is_file` takes `at_end` (nothing follows the name) beside `any_ending` (not under the
   owner's pattern); the 2-to-5-letter rule needs both, as before. At the very end of a path
   nothing changes: `/assets/app.js` and `/blog/hello-node.js` are files (Open items). The
   finding also offered a broader fix, no file ending at all before a `/` (and none under the
   owner's pattern on such a path); that would make `/files/annual-report.pdf/` and
   `/library/riverlm3.zip/` candidates again, which round 3 rejects on purpose, so only the code
   words changed, as the verdict suggested. Tests:
   the new keep cases (five without the owner's pattern, two under it) and the code-word and
   `.pdf.html` cases of `test_url_ok_reads_an_ending_only_at_the_end_of_the_path` in
   `test_article_detect.py`; `test_a_slug_that_ends_in_a_software_name_is_an_article` in
   `test_generic_scrape_errors.py` (Hugo, Eleventy and Jekyll slugs through the real fetch path,
   from a sitemap, from a sitemap under the link pattern and from a listing page under it; all
   four stored on poll 1).
2. **The sitemap-place test couldn't tell "below any stored story" from "below the
   newest-dated one"** (minor; test only). Its two stored stories shared one date, so their
   date order was their sitemap order and `next()` over the candidates found the same stored
   story as `_backlog_rule`'s `min()` over places. The rule's code is unchanged.
   `test_an_undated_page_below_any_stored_story_is_backlog` lists, through the real crawl and
   sort, a new undated page, a stored story dated 2026-10-01, an undated page, and a stored story
   dated 2026-10-18 (by `<lastmod>`, and by `/YYYY/MM/DD/` URL). The page between them is
   fetched and dropped as backlog: it is listed below the older-dated stored story. Under the
   `next()` rule it came in. The crawl and fakes of the place test moved into `_crawl_undated`,
   which both tests use.

### Verification of the round-4 fixes

- **Revert in place.** A script applied each mutant, ran the tests that guard it
  (`test_article_detect.py` and `test_generic_scrape_errors.py` for fix 1,
  `test_generic_scrape_skips_stored.py` for fix 2), required a failure, and restored the
  file byte for byte (SHA-256 of the file, and of `git diff` before and after). All 8 were
  caught:
  - fix 1: HEAD 1ed421d's `_is_file` and its call: 12 fail (9 `url_ok` cases and the 3 crawl
    cases); code words under a page ending only: 6; code words before a `/` only: 9; no file
    ending at all before a `/` (the finding's broader fix): 2 (`annual-report.pdf/`,
    `riverlm3.zip/`); no ending check under a page ending: 2; the 2-to-5-letter rule before a
    `/`: 9; the 2-to-5-letter rule under the owner's pattern: 1;
  - fix 2: the reviewer's mutant (`next()` over the candidates in date order): 2 (both
    datings).
- **The reviewers' probes**, run against this tree: the dotted-slug probe (s2) stores hugo 4/4,
  jekyll 2/2 and the link-pattern feed 2/2 on poll 1, as on main. The three-poll probe (s2b)
  asks for `/posts/getting-started-with-three.js/` once and stores it, from a sitemap, from the
  home page's links and under the link pattern. `url_ok` accepts all six URLs the verdict
  listed, with and without the owner's pattern, and still rejects `/files/annual-report.pdf/`,
  `/files/doc.pdf.html` and `/blog/hello-node.js`.
- **PR10b test files** (the 12, `test_slice27_deferred_perf.py`, `test_feed_autodiscovery.py`
  and `test_b1_b8_coverage_gaps.py`): 434 passed, 4 failed, the 4 DNS-only `/auto`
  route tests in the baseline (418 before this round, plus the 16 new cases).
- **Backend pytest** (database `popping_test_b`, worktree `PYTHONPATH`): **13 failed, 1440
  passed**. The 13 are exactly the DNS-only baseline (`/root/popping-env/baseline.txt`).
- **`ruff check --select F,E9,B`** on the changed files: clean (`article_detect.py` and the three
  test files).

## Review fixes (round 5)

The fifth review's four findings: two regressions against main and two tests that couldn't
fail. Each was reproduced first with the reviewers' probes, against HEAD 39dd76b and main, and
each fix has a test that fails when it is reverted in place (Verification below). None was
skipped.

1. **Articles under `/about/` (or `/about-us/`, `/pages/`) were dropped, even under the owner's
   link pattern** (major; a regression against main). `url_ok` rejected every URL whose first
   segment is a static section, and neither the feed's section nor its link pattern lifted the
   rule. A council or university site that files its news under `/about/news/<slug>` read
   `[no list]` on every poll in all four setups the reviewer tried (a section feed, a section
   feed with a link pattern, a root feed with a `sitemap_url`, a root feed), and `/auto` found
   nothing; main stored the 3 stories in each. Two changes, one for each kind of feed:
   - `url_ok(..., section=)`: the section the feed reads (its link pattern, else its address's
     section) is the owner's choice, so for a URL below it the segment rules (`_listing_path`
     and the static-section rule) judge only the segments below it (`_own_segments`: a prefix
     that ends inside a segment owns only the whole segments before it, and the section page
     itself owns none). The sitemap read (`_read_sitemap`), the link pattern's page links and
     `listing_links` pass it. A page's own name, date archives, file endings and courlan are
     judged as before. So `/about/media-centre/<slug>` comes in on a feed pointed at that
     section, and so do record-shaped pages below a section the owner chose (`/products/<x>`
     under a `/products/` pattern, the old Open item), which `score_page` then judges, as main
     fetched them.
   - With no section to go by (a root feed, `/auto`), a static section with a news section
     between it and the page (`_NEWS_SECTIONS`: `news`, `blog`, `press`, `newsroom`,
     `press-releases` and the like) is not static (`_static_section`): `/about/news/<slug>` is a
     candidate, while `/about/news` itself, `/about/team/<x>`, `/about/team/news` and
     `/pages/our-story` are still rejected. The reviewer's 37 `url_ok` cases give the same
     verdicts as at HEAD except `/about/news/council-approves` and `/pages/news/council-approves`.

   Tests: `test_url_ok_reads_a_news_section_under_a_static_section`,
   `test_url_ok_judges_only_the_segments_below_the_feeds_own_section` and
   `test_url_ok_keeps_its_rules_outside_and_below_the_feeds_own_section` in
   `test_article_detect.py`; in `test_generic_scrape_errors.py`,
   `test_articles_under_an_about_section_are_articles` (the reviewer's four setups for
   `/about/news/`, a root feed for `/about-us/news/`, and three setups that name
   `/about/media-centre/` as the feed's section; through the real fetch path, each checking that
   the feed's usual read found the stories alone and that `/about/` is never fetched) and
   `test_auto_discovery_finds_a_sitemap_of_news_under_an_about_section`. The fake
   `listing_links` in `test_a_listing_page_is_parsed_off_the_event_loop` takes `**kwargs`.
2. **A one-poll failure of a feed's usual read lost pages its other read judged on weaker
   evidence** (minor; a regression against main). Round 2's `unvouched` covered only a feed with
   a `sitemap_url`. Any other feed whose usual read failed or came back empty for one poll read
   the other one and marked its pages for good, though that read judges a page differently: the
   home page's links carry no news sitemap date, so an `og:type=website` story was rejected; a
   sticky post at the top of the home page made a new undated post backlog; and a section feed
   whose listing page answered 503 read its sitemap (oldest first, no `<lastmod>`), where the
   new post sat below the stored ones. main stored each of them when the usual read came back.
   `plan_candidates` now reports `Plan.fallback` (the second read gave the candidates; it
   defaults to False, so the `Plan(...)` values in tests and PR11a's use are unchanged), `fetch`
   sets `unvouched` from it, and `_seen` lets a poll whose plan came from the usual read read
   such a page once more. The `_deferred_new` verdict is kept for them, as for unvouched pages
   before. A feed with a `sitemap_url` behaves as before (its sitemap is its usual read) and
   keeps its INFO line; other feeds log the fallback at DEBUG. Tests, through the real fetch
   path: `test_a_root_feed_whose_news_sitemap_fails_for_one_poll_keeps_its_new_story`,
   `test_a_root_feed_whose_sitemap_fails_for_one_poll_does_not_drop_its_new_post` and
   `test_a_section_feed_whose_listing_fails_for_one_poll_does_not_drop_its_new_post` (the new
   story is rejected or dropped on the bad poll, stored on the next, and asked twice in all);
   `Plan.fallback` asserts in three `test_generic_scrape_plan.py` cases.
3. **The sitemap-place test couldn't tell "below any stored story" from "below the
   oldest-dated one"** (minor; test only). Round 4's test listed the older-dated stored story
   first, so the oldest-dated story was also the first listed. The test now also runs in the
   usual newest-first order (`[new, newer, between, older]`), where the oldest-dated mutant
   brings `between` in; the round-4 order still catches the newest-dated one. The rule's code is
   unchanged.
4. **A code word at the very end of the path under the owner's link pattern had no test**
   (minor; test only). `/library/getting-started-with-three.js` and `/library/feed.xml` join the
   reject list of `test_url_ok_keeps_its_own_rules_under_the_owners_link_pattern`. The code is
   unchanged.

README's "Website feeds" and ARCHITECTURE.md's `_extracted_urls` and `_deferred_new` rows
follow fixes 1 and 2; the Deviations (the undated cap, `url_ok` under a link pattern), Risks
and Open items above and below are corrected for them.

### Verification of the round-5 fixes

- **Reproduced first**, with the reviewers' probes (one plugin instance per scenario through the
  real fetch path), against HEAD 39dd76b, main 619000f and this tree:
  - `/about/news/` (pC): `[no list]` on both polls in all four setups at HEAD; main and this
    tree store the 3 stories on poll 1 in each. `url_ok` (pU): only the two `/about/news/` and
    `/pages/news/` verdicts change from HEAD.
  - The fallback reads (pF2 news sitemap dates, pF sticky post, pF3 section listing 503): HEAD
    asks for the new story once and never stores it; main and this tree store it when the usual
    read is back (this tree asks for it twice). Without the outage (pF2b, pFb) HEAD and this
    tree store it on poll 2 and ask once. The undated-stream probe (pS) gives the same counts
    as HEAD: the oldest-first sitemap with no `<lastmod>` is the documented risk below.
  - Findings 3 and 4 are test gaps: with HEAD's own tests, each reviewer's mutant (the
    oldest-dated stored story; code words not files at the end of a path under the pattern)
    leaves all 418 PR10b tests passing, as the reviewers found; below, they fail now.
- **Revert in place.** A script applied each mutant, ran `test_article_detect.py`,
  `test_generic_scrape_errors.py`, `test_generic_scrape_plan.py` and
  `test_generic_scrape_skips_stored.py`, required a failure, and restored the file byte for byte
  (SHA-256 of the file, and of `git diff` before and after). All 19 were caught:
  - fix 1: HEAD's rules (neither change): 17 fail; no news-section lift: 9; no feed section:
    9; the static-section rule judged inside the feed's section: 6; the listing rules judged
    inside it: 3; a partial segment counted as owned: 2; the section page itself owned: 1; a
    news word as the page itself lifting the rule: 3; the section left out of the sitemap read:
    1, of the link pattern's page links: 1, of `listing_links`: 1;
  - fix 2: `unvouched` set only for a `sitemap_url` (HEAD's rule): 3; `Plan.fallback` never
    set: 10; fallback read as page mode: 3; unvouched pages read on every poll: 4; never read
    again: 8;
  - fix 3: the reviewer's oldest-dated mutant: 3 (both datings, and the sticky-post test);
    round 4's newest-dated mutant: 2;
  - fix 4: the reviewer's mutant (code words not files under the pattern): 2.

  The first run had three survivors, the section left out of each read: the other read found
  the stories, so the feed still stored them. The about-section test now checks that a section
  feed never asks for the sitemap and a root feed never asks for its home page.
- **Against HEAD 39dd76b's code** (a scratch export with this round's tests): 38 cases fail
  there: the 5 accept cases of the news-section test, the 18 `section` cases of `url_ok` (on the
  new argument), the 8 about-section crawls and the `/auto` case, the 3 fallback cases and the
  3 `Plan.fallback` asserts. The 10 reject cases of the news-section test, fix 3's new order
  and fix 4's cases pass there, as they should: they guard rules HEAD had right.
- **PR10b test files** (the 12, `test_slice27_deferred_perf.py`, `test_feed_autodiscovery.py`
  and `test_b1_b8_coverage_gaps.py`): 483 passed, 4 failed, the 4 DNS-only `/auto` route tests
  in the baseline (434 before this round, plus the 49 new cases).
- **Backend pytest** (database `popping_test_b`, worktree `PYTHONPATH`): **13 failed, 1489
  passed**. The 13 are exactly the DNS-only baseline (`/root/popping-env/baseline.txt`).
- **`ruff check --select F,E9,B`** on the changed files: clean (`article_detect.py`,
  `feed_autodiscovery.py`, `generic_scrape.py` and the four test files).

## Review fixes (final round)

The sixth review's six findings: five regressions (against main, and the first one against round
4 too) and one test gap. A previous fixer stopped at the spend limit partway; its WIP commit was
reviewed and folded in here. Fixes 1, 4 and 6 and most of 2 and 3 are kept as it wrote them.
Four of its choices were changed, because each kept or widened a rule that rejects what main
read, or broke round 5: a refused robots.txt counted as a failed document, a sitemap name with
no "sitemap" in it still judged by each of its words, "brand" added to the taxonomy words, and
nine page endings added to a whitelist instead of narrowing the ending rule. As this round asked, each fix
narrows a rule back toward main's behaviour rather than adding a special case. Each finding was
reproduced first with the reviewers' probes. Each fix has a test that fails when the fix is
reverted in place (Verification below). No finding was skipped; two of the reviewers' suggested
mechanisms were not taken (in 3 and 4, with the reason).

1. **A category page's subcategory links outvoted its posts** (minor; a regression against round
   4, and against main when the site has a sitemap). Round 5 passed the page's own section to
   `listing_links`, so on a feed at `/category/news/` the links to `/category/news/<child>/`
   passed `url_ok` and, when there were more of them than posts, made the largest group. Every
   candidate was then a category archive: rejected and marked, with the plan not empty, so the
   feed stored nothing and reported nothing. `_page_links` now reads the listing with `url_ok`'s
   rules on every segment first (round 4's read). It lifts the page's own section only when that
   finds no group of 3 or more (round 5's read). The subcategory links never compete with the
   posts, and `/about/media-centre/<slug>` still comes in. Test:
   `test_a_category_pages_subcategory_links_do_not_outvote_its_posts` (10 posts against 12
   child categories, and 4 against 6; every post stored on poll 1, no child category fetched).
2. **One sitemap document failing for one poll lost a story** (minor; a regression against
   main). Take a news sitemap that answers 503 while the post sitemap beside it loads. The read
   was not empty, so round 5's fallback flag stayed off. An undated `og:type=website` story with
   no news sitemap date was rejected and marked for good. Now:
   - `discover_sitemap_entries(..., failures=)` collects every document of the crawl that
     failed: a 5xx, a timeout or other network error, or a refused sitemap.
   - A 404 or other 4xx is not collected (the document is gone), nor is a refused robots.txt
     (see below).
   - Every sitemap document is now fetched with `raise_for_status`, so its 5xx is seen. A 4xx or
     5xx on any document but the input sitemap still reads as empty and the crawl moves on. Only
     the input's own status fails the crawl, as before.
   - `plan_candidates` sets `Plan.degraded` when the sitemap read gave the candidates and one of
     its documents failed. `Plan.degraded` defaults to False, like `Plan.fallback`.
   - `fetch` marks such a poll's pages `unvouched`, as it marks a fallback read's. Only a
     complete poll (the usual read, with nothing failed) reads them once more, so a news
     sitemap that keeps failing doesn't make every poll fetch the same pages again.

   The WIP also collected a refused robots.txt. Such a refusal tends to last, so every poll of
   that site would be degraded and round 5's fallback re-read would never run for it. A refused
   robots.txt is read as no robots.txt again, as in round 5, and is not collected. Tests:
   - `test_a_news_sitemap_that_fails_beside_a_post_sitemap_keeps_its_new_story` (robots.txt and
     sitemap-index layouts, each with a 503 and a timeout);
   - `test_a_news_sitemap_that_keeps_failing_does_not_reread_its_stories_each_poll`;
   - `test_the_sitemap_crawl_reports_the_documents_that_failed`, which now includes the refused
     robots.txt;
   - a robots.txt axis (lists the sitemap, refused) on
     `test_a_root_feed_whose_sitemap_fails_for_one_poll_does_not_drop_its_new_post`.

   main stores the story on poll 1. This tree stores it on poll 2, the first poll whose
   sitemaps all load, and asks for it twice.
3. **Sitemaps of custom post types named with a media, product or user word were skipped**
   (minor; a regression against main, which read every sitemap). Two changes to
   `classify_sitemap`, both narrowing. Every word the old rule used is still one the new rule
   may use, but it uses fewer, so it skips only sitemaps the old rule skipped:
   - **A name of several words counts by its last word.** That covers three shapes:
     - a WordPress type or taxonomy joined with `_`: `post_tag` reads as tag, `product_cat` as
       cat, `media_release` as release, `product_update` as update, `user_story` as story;
     - the whole name in front of "sitemap": Yoast's `<type>-sitemap.xml`,
       `media-release-sitemap.xml`;
     - a name, or the section folder a sitemap is filed under, with no "sitemap" in it:
       `media-releases.xml`, `/media-centre/sitemap.xml`.

     After "sitemap", each `-` part still counts, so WordPress core's
     `wp-sitemap-<provider>-<subtype>-1` keeps its provider:
     `wp-sitemap-taxonomies-region-1` is a taxonomy, `wp-sitemap-posts-product-1` products, and
     `wp-sitemap-posts-media_release-1` posts.

     The WIP counted each part of a name with no "sitemap" in it, so it still read
     `/media-centre/sitemap.xml` as media. It also added "brand" to the taxonomy words, which
     newly skipped a `brand-sitemap.xml` that main and round 5 read. Both are undone.
     `product_brand-sitemap.xml` is now read as other: `score_page` rejects its `/brand/<x>/`
     archives, one fetch each.
   - **Video and image sitemaps are read.** Their `<loc>` is the page that shows the video or
     image (Google's sitemap extensions), and Yoast names a `video` post type's sitemap
     `video-sitemap.xml`; the reviewer's `video-sitemap.xml` feed read `[no list]` where main
     stored 3. They now class as other, read after news and post sitemaps. The media class is
     attachment sitemaps (`attachment`, `media`), whose entries are attachment pages.

   Not taken: reading the skipped sitemaps as a last resort when the crawl finds nothing. It
   would be a third read, and it could pre-empt the home page's fallback with attachment pages
   or archives. With the two narrowings every case the reviewers tried is read. What is still
   skipped (tag, category, topic, collection, author, user, product, shop, attachment) lists
   archives, people, products or files, as the spec's skip list intends. Tests:
   `test_a_sitemap_name_of_several_words_counts_by_its_last_word` (30 names) and
   `test_a_custom_post_types_sitemap_is_read` (`media_release`, `product_update` and `video`;
   root, section and `sitemap_url` feeds).
4. **A sticky post at the top of a section's listing made every new undated post backlog**
   (minor; a regression against main). `_backlog_rule`, on a listing page only: when stored
   stories lead the list, a page that isn't stored follows them and a stored story comes
   further down, the leading ones are pinned. The line is then the first stored story below the
   first page that isn't stored. With no stored story further down, the leading ones stay the
   line, as before. That is a listing after a restart, whose newest story is stored and the
   pages it passed over are below it. A sitemap keeps "below any stored story is backlog",
   whatever leads it.

   Not taken: the reviewer's other suggestion, which is to make the line the first stored story
   that starts a run of stored ones. Two sticky posts in a row start a run, so that rule reads
   them as the line.

   Tests:
   - `test_a_pinned_post_above_new_posts_does_not_make_them_backlog` (one and two sticky posts,
     three polls, one new post each, through the real fetch path);
   - `test_stored_stories_pinned_at_the_top_of_a_listing_are_not_the_line` (with an old page
     between two stored stories below the pinned one, which stays backlog);
   - `test_a_listing_led_by_stored_posts_with_none_below_keeps_them_as_the_line`;
   - `test_a_stored_story_at_the_top_of_a_sitemap_is_still_the_line`.

   Round 5's root-feed fallback test used a sticky post to make the bad poll drop the new post.
   The sticky post is now pinned, so the test uses a stored post that was edited this morning,
   listed by update time above the new one (the update-time risk below).
5. **A sitemap's pages with an ending this module doesn't know were read as files** (minor; a
   regression against main). Examples are ATG's `.jhtml`, `.jspx`, `.shtm`, `.dhtml` and
   Domino's `.nsf`. `url_ok(..., sitemap_entry=True)` doesn't apply the 2-to-5-letter ending rule
   to a sitemap's entry. The file endings, camera file names and courlan still apply.
   `_read_sitemap` passes it. main never filtered sitemap entries by their ending. A file that a
   sitemap lists under an ending of its own is dropped by the page fetch's HTML check, at one
   fetch, and then marked. A listing page's links keep the rule, as courlan's ending test read
   them on main. So a section feed whose links end in `.jhtml` reads its section of the
   sitemap, as its second read, as main read it. The WIP added nine endings to
   `_ARTICLE_EXTENSIONS` instead; that list is back to round 3's. Tests:
   - `test_url_ok_reads_a_sitemap_entrys_unknown_ending_as_a_page` (10 endings; the same URL as
     a listing link is still rejected);
   - `test_url_ok_keeps_its_other_rules_for_a_sitemap_entry`;
   - `test_a_server_page_ending_is_an_article_page` (6 endings, root and section feeds, through
     the real fetch path).
6. **Round 5's fallback re-read had no test for a feed with a link pattern** (minor; test
   only). The two fallback tests now run with and without `link_pattern="/blog/"`, for one bad
   poll and for two (the root-feed one also with robots.txt refused). With two bad polls the
   page must not be read again on the second. The code is unchanged.

README's "Website feeds" and ARCHITECTURE.md's `_extracted_urls` and `_deferred_new` rows follow
fixes 2 to 5. The Deviations, Risks and Open items are corrected for them.

### Verification of the final-round fixes

- **Reproduced first** with the reviewers' probes (one plugin instance per scenario through the
  real fetch path; findings 1 to 5). Results at HEAD a395334 are the reviewers'. This tree's
  results are below:
  - Subcategories: p_subcat (4 posts, 6 child categories, a sitemap) stores the 4 posts on
    poll 1 and fetches no child category; HEAD stored none. p_subcat2 (10 posts, 12 children)
    gives `[10, 0, 0]`, the same as round 4; HEAD gave `[0, 0, 0]`.
  - Partial sitemap failure: p_partial and p_partial_idx store the new story on poll 2 and ask
    for it twice; HEAD never stored it, and main stored it on poll 1. p_partial_ok (no outage)
    stores it on poll 1 and asks once.
  - Custom post types: p_media stores the 3 releases on poll 1 in all its setups; HEAD read
    `[no list]`. p_product stores 3 for `product_update-sitemap.xml`,
    `wp-sitemap-posts-product_update-1.xml` and `video-sitemap.xml`, as main did.
  - Sticky post: p_sticky stores one new post a poll, 3 in all, as main; HEAD stored none.
  - Endings: p_ext and p_ext2 store the 3 stories on poll 1 for `.jhtml`, `.shtm`, `.dhtml`,
    `.jspx` and `.nsf`, on a root feed and a `/news` feed; HEAD read `[no list]`.
  - The control-fetch probe beside them (p_ctrl) gives the same results as HEAD.
- **Revert in place.** A script applied each of 29 mutants to the worktree and ran the 10 PR10b
  test files that cover this code. It required a failure, then restored the file byte for byte
  (SHA-256 of the file, and of `git diff` before and after). All 29 were caught. The counts are
  failing test cases:
  - fix 1: round 5's lift on every read: 2; never lifting (round 4): 1.
  - fix 2:
    - `Plan.degraded` never set: 6; `unvouched` from the fallback only (HEAD): 5;
    - degraded polls reading unvouched pages again: 1; a non-input 5xx not raised (HEAD): 4;
    - not collected: a document's 5xx: 4, a network error: 3, a refused sitemap: 1, robots.txt's
      5xx: 1;
    - wrongly collected: robots.txt's refusal (the WIP's rule): 5, robots.txt's 404: 1.
  - fix 3: HEAD's every-word rule: 18; the WIP's rule for a name with no "sitemap": 3; the first
    word as the head: 25; video and image skipped (PR10a's words): 5; "brand" a taxonomy word
    (the WIP's): 1; the words after "sitemap" read as one phrase: 4.
  - fix 4: HEAD's rule: 3; pinning in sitemaps too: 1; pinned with nothing stored below (no
    line): 10; the line at the last stored story below instead of the first: 1 (it survived the
    first run, so the pinned test gained a page between two stored stories).
  - fix 5: `sitemap_entry` ignored: 22; not passed by the sitemap read: 12; skipping every file
    test: 4; skipping courlan too: 1.
  - fix 6: `unvouched` left off for owner-configured feeds: 6, for link-pattern feeds: 6; round
    4's rule: 18.
- **Against HEAD a395334's code** (a scratch export with this round's tests): 65 cases fail
  there. They are the new tests of fixes 1 to 5; the `sitemap_entry` cases fail on the new
  argument. Fix 6's link-pattern cases pass there, as they should: the code was right and only
  the test was missing. So do the robots.txt-refused cases, which guard round 5's rule against
  the WIP's.
- **PR10b test files** (the 12, `test_slice27_deferred_perf.py`, `test_feed_autodiscovery.py`
  and `test_b1_b8_coverage_gaps.py`): 576 passed, 4 failed. The 4 are the DNS-only `/auto` route
  tests in the baseline. Round 5 had 483 passed; this round adds 93 cases.
- **Backend pytest** (database `popping_test_b`, worktree `PYTHONPATH`): **13 failed, 1582
  passed**. The 13 are exactly the DNS-only baseline (`/root/popping-env/baseline.txt`).
- **`ruff check --select F,E9,B`** on the changed files: clean (`article_detect.py`,
  `feed_autodiscovery.py`, `generic_scrape.py` and the three test files).

## Review fixes (re-check)

The seventh review's six findings (the tests call this round 7), all regressions against main: a feed that worked there stored
nothing, lost pages for good, or showed a setup state. This round was briefed as building on a
previous fixer's WIP commit at HEAD. There was none: HEAD was the final round's commit f01c6c1
(which had already folded in the earlier WIP) and the worktree was clean, so nothing was reset or
folded in. Each finding was reproduced first with the reviewers' probes (main 619000f, HEAD
f01c6c1, the real fetch path through an httpx MockTransport, and the scheduler's store step). As
this round asked, fixes 1, 2, 4, 5 and 6 narrow a rule back toward main's behaviour. Fix 3 is a
new date read, because no narrowing of `score_page`'s rules alone brings those posts in. Every
fix keeps the error reporting: refusals, timeouts, dead hosts and a missing address still raise
first. Each fix has tests that fail when it is reverted in place (Verification below). No finding
was skipped.

1. **A folder's index page read as a home page** (major). A static or exported site publishes each
   story as a folder with its own index page (`/news/<slug>/index.html`,
   `/news/20261020/index.html`, `/news/<slug>/default.aspx`). `url_ok` showed courlan the page's
   name under placeholder folders (`/x/x/index.html`), and courlan's index-page filter rejects an
   index page under a path of 5 characters or fewer. courlan accepts the real path. Since the
   final round, sitemap entries go through `url_ok` too, so neither read found a candidate, and
   the feed read `[no list]` where main stored every story. The fix: `url_ok` judges a folder's
   own index page (`index` or `default` under a web page's ending, below at least one folder)
   as the folder (`/news/<slug>/`), by all three checks: our path rules, the file endings and
   courlan. `/index.html` is still the home page. `/about/index.html`, `/tag/parks/index.html`,
   `/2026/10/index.html` and `/category/news/index.html` (on the category's own feed) are still
   rejected, as their folders are. `/library/home` and `/blog/index` have no page ending and are
   judged as before. The reviewer's suggestion (show courlan the parent folder's name) was
   taken in this form, so that the folder's name also meets the static-slug and date-archive
   rules. Tests:
   - `test_url_ok_reads_a_folders_index_page_as_the_folder` (7 paths, as a listing link and as a
     sitemap entry);
   - `test_url_ok_keeps_its_rules_for_a_folders_index_page` (10 paths);
   - `test_stories_published_as_folder_index_pages_are_articles`: slug and date folders,
     `index.html` and `default.aspx`, on root, section and `sitemap_url` feeds, through the real
     fetch path.
2. **A newsroom under a static section read its quick links instead of its posts** (major).
   Take a section feed at `/about-us/news-and-media` whose page also has a "Popular services"
   block of 3 or more links in its body, outside nav and aside. `_static_section` rejected every
   `/about-us/<x>/<slug>` link when `_page_links` first read the listing without the section, so
   the service links were the only group. Their pages were fetched and rejected, and the
   section-lifted read never ran. The feed stored nothing and said nothing, where main stored
   every release from its sitemap.

   The fix narrows `_static_section` back toward a page's own place: only a page directly under
   a static section (`/about/<x>`, `/pages/<x>`, `/policies/<x>`) is static. Deeper pages are
   judged like any path, so the releases compete in the first read and win 10 to 4.
   `_NEWS_SECTIONS` existed only for round 5's exception and is gone. `score_page` judges the
   rest of a static tree (`/about/team/<name>`), one fetch each, as main fetched every sitemap
   entry. The final round's open item is fixed by the same change: a root feed of a site that
   files its stories under `/about/media-centre/<slug>` reads them from its sitemap. The
   two-step listing read (round 5's lift only when the page has no list) stays, for sections
   named with a listing word (`/category/news/`, `/shop/journal/`). Tests:
   - `test_a_newsroom_under_a_static_section_reads_its_posts_beside_quick_links` (3 sections,
     3 and 4 quick links; no service page is fetched);
   - root cases for `/about/media-centre/` and `/about-us/news-and-media/` in
     `test_articles_under_an_about_section_are_articles`;
   - `test_url_ok_reads_a_page_deeper_in_a_static_section_with_or_without_its_section`.

   The old rule's expectations changed: `/about/team/dana-smith` and `/about/team/news` are
   candidates now, and the three static-section cases of
   `test_url_ok_judges_only_the_segments_below_the_feeds_own_section` moved to the new test,
   replaced by listing-word sections that still need the lift.
3. **Webflow and block-theme posts read as undated, so their feeds stored nothing** (major).
   Webflow tags every page `og:type=website`, prints a post's date as text and adds no JSON-LD,
   so a post scored −3 (the About page's score). A WordPress block theme with no SEO plugin
   prints the date in a plain `<time>` inside `div.wp-block-post-date`, beside an `<h1>` with no
   `<article>` around them, so a post scored 0. Both were rejected and marked, and the feed
   counted as a quiet success.

   The reviewer's rejected idea (trafilatura's own date as the list's date) re-admits About
   pages: it reads a copyright year as a date. So `_body_date` gains one narrow read
   (`_title_date`), used only on a page with no marked date at all, exactly one `<h1>` and no
   `<article>`:
   - the only plain `<time datetime>` in the `<h1>`'s own container (`_own_times`, the same
     filter as `_article_time`: not in a footer, aside or menu, not an update). A container
     with several gives none, so a listing built of div cards stays undated.
   - a date at the start of the short line right after the `<h1>` (80 characters at most, the
     pieces of text joined by spaces): "October 14, 2026", "Oct. 14th, 2026",
     "14 October 2026", "2026-10-14", after an optional weekday and "Posted on" or "Published".
     "Updated October 14, 2026", "Founded October 14, 2015" and a paragraph that starts with a
     date are not dates of the page.

   A page with `<article>` cards, or an `<h1>` on each card, is a listing and is never read this
   way: a news page that prints today's date under its title stays undated. The About,
   Yoast About and long-listing fixtures still score below 2. Tests:
   - `test_a_builder_post_dated_next_to_its_title_is_an_article` (8 shapes, with and without
     `og:type=website`);
   - `test_a_builder_page_with_no_date_next_to_its_title_stays_undated` (10 shapes);
   - `test_a_date_next_to_the_title_counts_only_on_a_page_of_one_title_and_no_article`
     (`<article>` cards under a dated title, `<h1>` cards);
   - `test_a_site_builders_posts_dated_by_their_title_are_articles` (Webflow and block theme,
     root and section feeds, through the real fetch path; the About page beside them is still
     left out).
4. **Two sticky posts stored by a first fetch became the backlog line** (minor). Take a new
   section feed whose undated listing pins 2 or more sticky posts. Its first fetch keeps 2
   undated pages (the cap): the sticky ones at the top. The other 8 were read and marked. On the
   next poll no stored story sat below the new post, so the final round's pinning rule didn't
   apply. The sticky posts were the line, and every new post was dropped and marked for good.

   As the reviewer suggested, `_backlog_rule` now takes the URLs this plugin has read
   (`_extracted_urls`). A page read and passed over further down shows that the leading stored
   stories are pinned, as a stored story there does, and the line is the first such page. After
   a restart nothing has been read, so the documented restart behaviour (the leading stored
   stories are the line) is unchanged. The first new post after a restart of such a feed is
   still dropped (Risks). Tests:
   - `test_sticky_posts_stored_by_a_first_fetch_do_not_make_new_posts_backlog` (2 and 3 sticky
     posts, a first fetch, then one new post on each of 3 polls, each asked for once);
   - `test_pages_read_and_passed_over_below_pinned_stories_mark_the_line` (with the read pages,
     and after a restart).
5. **The sitemaps of hyphenated custom post types were still skipped** (minor). WordPress
   allows a "-" in a post type's key. Core names its sitemap
   `wp-sitemap-posts-media-release-1.xml`, and each part after "sitemap" counted, so it read
   as media. Likewise `posts-product-update-1` read as product, `posts-user-story-1` as author,
   and `sitemap-media-releases.xml` and `/media/sitemap.xml` as media. main read every sitemap.
   Two narrowings, as the reviewer suggested:
   - After "sitemap", WordPress core's provider word (`posts`, `taxonomies`, `users`) and the
     last word of the rest count: media-release reads as release, product-update as update,
     and `taxonomies-region` is still a taxonomy.
   - "media" is no longer a media word. Only attachment sitemaps (`attachment`, `attachments`)
     are skipped as media.

   Tests: 15 more names in `test_a_sitemap_name_of_several_words_counts_by_its_last_word`
   (`sitemap-media.xml` is now other), and 4 more cases of
   `test_a_custom_post_types_sitemap_is_read`.
6. **An empty news sitemap read `[no list]`** (minor). A news sitemap lists only the last 48
   hours, so between stories it is an empty urlset. When it was the feed's address, or its
   `sitemap_url` beside a page that lists nothing in its HTML, the poll raised
   `NoArticleListError`. Its fix (Pick a section) is the wrong one, and main read the poll as
   one with nothing new.

   `discover_sitemap_entries(..., empty=)` now reports the input sitemap's address when it
   loaded as an XML sitemap (a urlset or a sitemap index, `_is_sitemap_xml`) that names no page
   or sitemap at all. `plan_candidates` then returns an empty plan, a quiet poll. This happens
   only after `most_telling_error` found no failure of the other read. So a refused, missing,
   failing or unreachable listing page still says what it is, and a refused, gone or timed-out
   sitemap raises first in the crawl as before. A sitemap that is a web page, or that lists only
   pages that aren't articles, is still `[no list]`. A root feed's robots.txt or guessed
   sitemaps that are empty are not the feed's own sitemap and still give `[no list]` when nothing
   else lists articles. Tests:
   - `test_an_empty_news_sitemap_between_stories_is_a_quiet_poll` (the feed's address, and a
     `sitemap_url` beside a rendered section and beside the home page: stores, quiet, quiet,
     stores);
   - `test_an_empty_sitemap_url_still_reports_its_listing_pages_failure` (403, 404, 503,
     refused connection);
   - `test_a_sitemap_url_that_is_not_an_empty_sitemap_still_reads_as_before` (refused, gone,
     timeout, a web page, only static pages).

   The final round's `test_an_empty_sitemap_url_is_not_replaced_by_the_sites_other_sitemaps` now
   expects `[]` and keeps its robots.txt and post-sitemap checks.

README's "Website feeds" and ARCHITECTURE.md's `_extracted_urls` row follow fixes 1 to 6. The
Deviations, Risks and Open items are corrected for them. The device checks are unchanged: the
probe they run already compares every website feed with its baseline.

### Verification of the re-check fixes

- **Reproduced first** with the reviewers' probes, 4 polls each, run against main, HEAD and this
  tree (stored per poll):
  - `index_html_root` and `index_html_section`: main 3, 3, 3, 3; HEAD `[no list]` on every
    poll; this tree 3, 3, 3, 3. `index_html_dated_dir`: main and this tree 3, 1, 1, 1; HEAD
    `[no list]`.
  - `static_section_quicklinks10`: main and this tree 10, 1, 1, 1; HEAD 0 on every poll, with no
    error.
  - `webflow_blog_root`: main and this tree 3, 2, 2, 2; HEAD 0.
  - `sticky_two_undated`: main 10, 1, 1, 1. HEAD 2, then 0 on every poll, losing each new post.
    This tree 2, 1, 1, 1, with every post stored (the undated cap on the first fetch, as for any
    feed).
  - `wp_core_cpt_hyphen`: main and this tree 3, 2, 2, 2; HEAD 0.
  - `news_sitemap_feed_empties` and `news_sitemap_url_empties`: main and this tree store, quiet,
    quiet, store; HEAD `[no list]` on the two empty polls.
  - Controls beside them: `sticky_one_undated` gives the same results as HEAD (2, 1, 1, 1),
    and `ghost_root`, `ece_section`, `wp_category_listing` and `blogspot_root` the same on main,
    HEAD and this tree. `hero_grid_section` matches HEAD (one page fewer than main over 4
    polls; not one of this round's findings, and unchanged by it). On
    `static_section_quicklinks` (3 releases against 4 quick links on poll 0), this tree stores
    nothing on poll 0, then 5, 2, 2, every release in the end. main gave 3, 2, 2, 2 and HEAD 0
    on every poll. The largest group wins: that is the listing-page risk.
- **Revert in place.** A script applied each of 23 mutants to the worktree and ran the 10 PR10b
  test files that cover this code. It required a failure, then restored the file byte for byte
  (SHA-256 of the file, and of `git diff` before and after). The counts are failing test cases;
  22 of the 23 were caught.
  - fix 1: no folder read (HEAD): 22; the feed's own section counted on the index page's path
    (`/category/news/index.html` on its own feed): 1; an index name with no page ending: 1. The
    survivor takes the probe's trailing slash from the index page's path instead of the
    folder's. It is equivalent: `_courlan_page` drops any ending that isn't a web page's, and
    courlan's index filter takes an optional trailing slash, so courlan's verdict can't change.
  - fix 2: static at any depth (main's `url_ok` rule): 26; no static-section rule at all: 9.
  - fix 3: no title date (HEAD): 20; no text date: 16; no container `<time>`: 2; the first of
    several `<time>`s: 1; no 80-character cap: 1; read on a page with an `<article>`: 1; read
    on a page of several `<h1>`: 1; a date anywhere in the line instead of at its start: 3.
  - fix 4: read pages not passed (HEAD): 3; read pages left out of the pinned check: 3.
  - fix 5: "media" a media word again: 2; no provider word: 7; each part after "sitemap"
    counting (HEAD): 3.
  - fix 6: the empty sitemap ignored (HEAD): 4; any empty input counting, not only an XML
    sitemap: 1; quiet before the page read's failure: 4; never reported: 4.
- **Against HEAD f01c6c1's code** (a scratch export with this round's three test files): 76
  cases fail there, all new or changed cases of fixes 1 to 6. The guard tests pass there, as
  they should: the date guards, an empty sitemap beside a failing page, and a sitemap that isn't
  an empty one.
- **PR10b test files** (the 12, `test_slice27_deferred_perf.py`, `test_feed_autodiscovery.py`
  and `test_b1_b8_coverage_gaps.py`): 698 passed, 4 failed. The 4 failures are the DNS-only
  `/auto` route tests in the baseline. The final round had 576 passed; this round adds 122
  cases.
- **Backend pytest** (database `popping_test_b`, worktree `PYTHONPATH`): **13 failed, 1704
  passed**. The 13 are exactly the DNS-only baseline (`/root/popping-env/baseline.txt`).
- **`ruff check --select F,E9,B`** on the changed files: clean (`article_detect.py`,
  `feed_autodiscovery.py`, `generic_scrape.py` and the three test files).

## Risks and data-compatibility notes

- **Some website feeds will pull fewer stories or show a new state.** This is the intended
  effect, which the owner's probe checks after deploying (Device checks):
  - `[no list]` is a setup state (D27): it never counts toward `error_count` and never stops
    the feed. Its fix (Pick a section, PR9b) also sends `active: true`.
  - `[blocked]` and `[not found]` count as failures, back off, and stop the feed after six in a
    row, as for every other feed.
- **A section feed now reads its page first.** If `listing_links` picks a repeating group that
  isn't the posts and every one of them is rejected by `score_page`, the feed finds candidates
  but brings nothing (Quiet). It never falls through to the sitemap. The probe's
  `since_deploy` a day after deploying would show it; the fix is a link pattern or a
  `sitemap_url`.
- **A listing ordered by update time** (a model index whose "newest" means recently updated)
  still uses the position rule for undated pages: a new undated page below a stored one that
  was just updated is dropped as backlog. The review asked to keep position for listing pages;
  a `sitemap_url` or a dated listing avoids it.
- **Pinned stories on a listing** (final round) are told apart only by a stored story, or a
  page this process read and passed over (re-check round), further down. A sticky post above
  more new posts than the listing shows, with no such story left on it, is still the line, and
  those new posts, if undated, are backlog. So is the first new undated post after a restart
  when the sticky posts are the only stored stories on the listing (a new feed whose first
  fetch stored only them): the restart forgets which pages were read, so that poll drops it,
  and the pages it reads then mark the line again from the next poll. After a restart, a page
  passed over between the newest stored story and an older one is no longer backlog: it is read
  once more, and comes in if it is an article within the undated cap.
- **A sitemap with no `<lastmod>` and pages with no date** uses the listing page's position rule
  (round 2) on the sitemap's own order (round 3): a new undated page listed above every stored
  one comes in; one listed below any of them is dropped as backlog, whatever the stored stories'
  own dates. A sitemap that lists its newest pages last
  (oldest first, with no `<lastmod>`) therefore brings no undated page after its first fetch,
  where main kept draining 2 a poll. A dated page, or a `<lastmod>`, avoids it. Across several
  sitemap documents the order is the crawl's (best class first, newest first within a class).
- **A `sitemap_url` that is the site's own address** now leads to the site's sitemaps through
  robots.txt (round 2), and those sitemaps vouch for their pages as the owner's
  (`owner_configured`), as any sitemap the `sitemap_url` names does.
- **A feed whose usual read never works** (a root blog with no sitemap, a section whose
  listing page is gone but whose section of the sitemap works) reads every poll from its second
  read (round 5). Its pages are marked `unvouched`, which costs nothing while that lasts; if the
  usual read starts working, each of them is read once more (10 a poll at most), and those that
  weren't backlog when read wait in `_deferred_new` (1000 at most) until then.
- **A sitemap document that keeps failing** (a 5xx, a timeout or a refusal; final round)
  leaves every poll's sitemap read degraded. Its pages are marked `unvouched` and read once more
  only when every document loads again, so while a news sitemap is down for good its dates are
  lost, as before this round, and a page that a fallback poll read is not read once more
  either. A refused robots.txt doesn't count, and neither does a 404.
- **More sitemaps and sitemap entries are read** (final round): video, image and custom-type
  sitemaps (`media_release`, `product_brand`; re-check round: `media-release` in WordPress
  core's names, and any sitemap named "media"), entries with an ending this module doesn't
  know, and (re-check round) a folder's index page (`/news/<slug>/index.html`), which also
  makes a section's own index page (`/news/index.html`) a candidate. Their entries join the
  newest-first order beside the posts'. Those that aren't articles (an image sitemap's static
  pages, a brand's archives, a file under an unknown ending, a section's index page) cost one
  fetch each before `score_page` or the HTML check drops them, and are then marked, as main
  fetched them all.
- **The section a feed reads isn't judged by `url_ok`'s segment words** (round 5): in its
  section of the sitemap, and on its listing page when the page has no list without the lift
  (final round), a section feed at `/category/news` takes a subcategory listing such as
  `/category/news/local/` as a candidate, and a `/products/` pattern its product pages;
  `score_page` alone rejects the ones that aren't articles (one fetch each, then marked), where
  main stored all of them. The words below the section are still judged
  (`/category/news/tag/parks/`, `/category/news/page/2/`).
- **Pages deeper in a static section are candidates on every feed** (re-check round): only a
  page directly under `/about/`, `/pages/` or `/policies/` is static. `/about/team/<name>` and
  `/about/history/<x>` are fetched once each when a sitemap or a listing names them, and
  `score_page` rejects them (an undated bio page), where main stored them. A listing page whose
  body (outside nav and aside) has a larger group of such links than its posts, such as a staff
  grid of `/about/team/<name>` on a home page, reads that group, stores nothing and reports
  nothing (the listing-page risk above); a link pattern fixes it.
- **A date next to a page's title** (re-check round) dates a page with one `<h1>` and no
  `<article>`: the short line right after the `<h1>` when it starts with a date, or the only
  plain `<time>` in the `<h1>`'s container. A builder site's event page ("October 30, 2026"
  under its title) is then dated, and accepted when it is long enough, as main stored it. A
  listing with `<article>` cards or an `<h1>` on each card is never read this way, and a
  container with several `<time>`s gives none.
- **A feed's own sitemap that stays empty** (re-check round) reads as a quiet poll with nothing
  new, as on main, never `[no list]`: a `sitemap_url` set to the news sitemap of a site that
  stopped publishing shows no setup state. A failure of its listing page still reads as what it
  is, and a sitemap that is refused, gone, a web page, or lists only pages that aren't articles
  reads as before.
- **After a restart,** pages that were fetched but not kept (not articles, too old, over the
  undated cap) are fetched once more, and pages that were failing start a new count of 3.
  Stored pages are not fetched. What a page left for later was (new, `_deferred_new`) is
  forgotten, so it is judged by its place again; one below a story stored before the restart
  is then backlog.
- **A control that served a page before** (round 3) may since have turned members-only too; it
  then answers 403 and the poll reads `[blocked]`, the same as a stored story that did. At most
  3 controls are fetched, and they are tried only when a whole batch is refused.
- **Revert is safe until PR11a merges** (it calls `plan_candidates`). There is no migration,
  and no route or response changes shape.

## Open items for later units

- **PR11a:** the preview should call `plan_candidates` (`Plan.candidates` carries each
  candidate's publication date, `<lastmod>`, title and sitemap place for
  `older_skipped_count`; a date in the URL is not carried, `feed_autodiscovery._entry_date`
  reads it). `_read_page` logs `score_page`'s reasons but returns only accept or reject. The
  preview's per-item `accepted` and `reason` will need it to return the score and reasons.
- **PR9b:** a website feed's `[blocked]` comes from its sitemaps, its listing page or its
  article pages. Read as Safari fixes all three: `custom_headers` reach every request.
- **The owner's junk sites** are still not in the fixtures (PR10a's open item). Once they're
  sent, save them offline and add cases.
- **Scoring edge cases** from PR10a stand. A date in the URL earns +1 and cancels the
  `og:type=website` −3, so a static page at a dated URL scores 2 and is accepted (seen while
  writing these tests; not a real-world shape so far).
- **Fixed in the re-check round:** a root feed of a site that files its stories under a
  static section whose name is no news section's (`/about/media-centre/<slug>`) now reads them
  from its sitemap, as main did. Record-shaped paths (`/products/<x>`, `/users/<x>`) are still
  candidates only below the section a feed reads.
- **A date written only as text** is read in one place only (re-check round): at the start of
  the short line right after the `<h1>` of a page with one `<h1>` and no `<article>` (a Webflow
  post's `div.blog-date`), and a plain `<time>` counts beside that `<h1>` when it is the only
  one in its container (a WordPress block theme). A date above the title, after a byline
  ("By Dana · October 14, 2026"), or as text inside an `<article>` still leaves an
  `og:type=website` page undated (−3), and such a page is rejected without a link pattern.
- **A slug that ends in a file type at the very end of its path**
  (`/blog/getting-started-with-vue.js`, `/blog/my-annual-report.pdf`) is read as a file, under
  the owner's link pattern too, and a link pattern doesn't bring it in. main fetched such a page
  from a sitemap or under a link pattern, because it ran `url_ok` only on listing links. Before
  a `/` or under a page ending only a document or media ending is a file (round 4), so the
  generators that keep a slug's dots (Hugo, Eleventy, Jekyll: `/posts/whats-new-in-vue.js/`,
  `/2026/10/18/whats-new-in-vue.js.html`) are read. Any other ending of 2 to 5 letters that is
  no web page's or known CMS's (`/blog/announcing-socket.io`) is a file only at the very end of
  the path, without the owner's link pattern and outside a sitemap, which brings such a feed in
  (rounds 2 and 3, final round). courlan's own ending test, which main's `url_ok` used for
  listing links, reads all of these as files, so on a listing page they still are.
- **The plan's frontend-only rollback command** still lacks `--no-deps` (PR9a's open item).

## Device checks

Added under Pending in `docs/DEVICE_CHECKS.md`. The first three are the read-only VPS probe that
used to be the merge gate; the owner now runs it before deploying (the one exception to Step 0)
and twice after.

- [ ] #<pr> · Baseline, the one check that runs **before** Step 0's `popping-update` (it has to read the old build): with #35 already deployed (the query reads its columns), run `cd /opt/popping && DEPLOYED=$(docker inspect -f '{{.Created}}' $(docker compose ps -q backend)) && docker compose exec -e DEPLOYED="$DEPLOYED" postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT s.name, s.active, s.last_attempt_at, s.last_item_count AS listed, s.last_new_count AS new, left(s.last_error, 40) AS error, count(e.id) FILTER (WHERE e.fetched_at >= '\''$DEPLOYED'\''::timestamptz) AS since_deploy, count(e.id) FILTER (WHERE e.fetched_at >= now() - make_interval(hours => 24)) AS stories_24h, count(e.id) AS stories_7d FROM sources s LEFT JOIN entries e ON e.source_id = s.id AND e.fetched_at >= now() - make_interval(days => 7) WHERE s.type = '\''generic_scrape'\'' GROUP BY s.id ORDER BY s.name"'` and keep the output. Pass: it prints a row per website feed (`since_deploy` counts from the previous deploy here and doesn't matter).
- [ ] #<pr> · 3 hours after deploying this PR (a website feed is checked every 2 hours by default), run the baseline's command again. Pass: every website feed has a `last_attempt_at` after the deploy, and each one with no error in the baseline still shows none (its `listed` count can be lower than before, because About, tag and shop pages no longer count); write `fail: <feed name>` for one that now shows `[no list]`, `[blocked]` or `[not found]`.
- [ ] #<pr> · A day or more after deploying this PR, and before deploying anything else, run the baseline's command once more: `since_deploy` counts only the stories each feed brought since the backend was deployed. Pass: each website feed with stories in the baseline's `stories_7d` has a `since_deploy` above 0, unless its site published nothing since the deploy; write `fail: <feed name>` for one whose `since_deploy` is 0 while its site kept publishing. `listed` is no signal on its own: a check with nothing new lists 0 too.
- [ ] #<pr> · A day after deploying, open the tab of a website feed that used to pull pages that aren't articles. Pass: the stories that arrived since the deploy are its articles, with no About, Contact, tag, author or shop pages.
