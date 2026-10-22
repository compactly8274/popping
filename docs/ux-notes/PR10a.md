# PR10a: article_detect library

Backend track. A pure library plus fixtures and tests. Nothing calls it yet: PR10b wires it into
`generic_scrape` and `feed_autodiscovery`. No route, no migration, no frontend, no flows or smoke
states, and no change in behaviour.

## What changed

- **`backend/app/article_detect.py` (new, pure).** No network, no database, no process-local
  state. It imports `courlan`, `tld`, `lxml` and `trafilatura`, all already in
  `requirements.lock`, and uses them only as parsers.
  - **`url_ok(url, site)`.** A yes/no test on a candidate URL. `site` is the source's URL or host.
    - Same site only, by registrable domain from `tld`'s bundled public-suffix list
      (`news.example.com` and `www.example.com` are one site; two `*.blogspot.com` blogs are
      not). An IP or `localhost` has no registrable domain, so the host itself is the site.
    - The path above the page is judged by whole segment, not substring, and by position:
      - taxonomy, CMS, account and cart segments anywhere (`tag`, `category`, `author`, `feed`,
        `wp-content`, `cart`, `collections`, …);
      - record words (`user(s)`, `profile(s)`, `member(s)`, `comments`, `product(s)`) only as the
        last segment, or as the first segment with one identifier after them (`/users/dana`,
        `/products/canvas-tote`). `/blog/products/new-launch` is an article;
      - `archive`/`archives` only as the last segment or before a date archive
        (`/archives/2026/07/`). `/archives/123` and `/technology/archive/2024/01/slug/677123/` are
        articles;
      - `page` only before a page number (`/blog/page/2/`);
      - static-page slugs as the last segment (`about`, `contact`, `privacy-policy`, `search`,
        `legal`, `help`, …);
      - static sections as the first segment (`pages`, `policies`, `about`, `contact`). Not
        `legal`, `help` or `support`: Reuters files news under `/legal/`;
      - a trailing date archive: `/YYYY/MM/` and `/YYYY/MM/DD/` at any depth, and a lone year only
        as the whole path (`/2026/`) or under `archive(s)`, `blog(s)`, `news`, `posts`, `articles`
        or `stories`. `/node/2015` and `/p/2041` are numeric ids;
      - query keys of non-articles (`s`, `page_id`, `attachment_id`, `replytocom`, `share`,
        `add-to-cart`, `paged`).
    - The site root is the home page unless its query has `p` (WordPress Plain permalinks,
      `/?p=123`, which its core sitemap lists for every post).
    - Then `courlan.check_url(strict=True)` judges the host, the page's own segment and the
      query (file types, feeds, contact, login and index pages, bad hosts). It is called on a
      probe URL whose path above the page is neutral segments. Its verdict is used as yes/no only;
      the normalized URL is never kept.
    - When the URL's host is the source's own host, the probe uses a placeholder host, so
      courlan's host check is skipped (loopback servers with a port).
  - **`classify_sitemap(url)`** returns `news`, `post`, `page`, `taxonomy`, `author`, `product`,
    `media` or `other`, from the path, plus WordPress core's Plain-permalink query
    (`/?sitemap=posts&sitemap-subtype=page`). The file name is split into whole tokens on `-_.`,
    with page numbers and the word "sitemap" removed (also when glued on: `newssitemap`). The
    classes are checked in a fixed order (taxonomy, author, product, media, page, news, post), so
    the subtype wins: `wp-sitemap-posts-page-1` is page, `post_tag-sitemap` is taxonomy,
    `product_cat-sitemap` is taxonomy. A name with no known token falls back to its directory
    (`/blog/sitemap.xml` is post), then `other`.
  - **`order_sitemaps(urls)`** applies the spec's policy: news first, then post, then other.
    Taxonomy, author, product and media are dropped. A page sitemap is dropped whenever a news or
    post sitemap is in the list, and kept last as a fallback otherwise.
  - **`score_page(html, url, *, final_url=None, text=None, owner_configured=False)`** returns
    `(score, reasons)`. A page is accepted at `ACCEPT_SCORE` (2) or more. Each reason reads like
    `"+3 json-ld article"` or `"-3 redirected to /"`.
    - +3 for a JSON-LD Article-family node, or for `og:type=article` when JSON-LD doesn't call
      the page a WebPage, AboutPage or ContactPage.
    - −3 for `og:type=website`, or JSON-LD WebSite, AboutPage or ContactPage, only when there is
      no JSON-LD article and no date.
    - +1 for a published date, −1 for none. +1 for 600+ characters, −2 under 300.
    - Hard rejects: −3 for a soft-404 title or a title that is just the site name; −3 for a
      redirect to another path; −2 for a canonical on another site.
    - `owner_configured` starts the page at +2 ("owner's link pattern or sitemap") and applies
      only the hard rejects and the under-300 rule, with no kind, date or 600+ scoring. So one
      threshold serves both modes: an owner-configured page is accepted unless a hard reject or
      the length rule fires.
    - `text` is the page's extracted main text. PR10b passes the text it already extracts. When
      it is None, it is extracted here with the plugin's options (`bare_extraction(...,
      favor_recall=True)`).
  - **`listing_links(html, page_url)`** returns `[Candidate(url, published, title)]` in page
    order. A `Candidate` unpacks as `(url, published, title)`, the shape PR10b gives sitemap
    entries.
    - It honours `<base href>`.
    - It drops nav, aside and `role` chrome. It drops a header or footer only when it is the
      page's: no article, aside, main, nav or section element and no element with an article,
      complementary, feed, main, navigation or region role holds it (ARIA's banner and
      contentinfo rule), and it doesn't repeat 3 or more times at one tag path the way div post
      cards' headers do.
    - It keeps only links that pass `url_ok` against the page, and never the page itself.
    - It groups links by tag path and takes the largest group with 3 or more distinct URLs. On a
      tie, the group with longer link text wins, so titles beat thumbnail or "Read more" links.
    - Each link's date is the first `<time datetime>` in its card: the largest ancestor that
      holds no other link of the group.
  - `same_site(url, site)` and `parse_date(raw)` are public, because PR10b can use them.
- **Fixtures (`backend/tests/fixtures/scrape/`).** All are hand-written from public CMS layouts:
  - Sitemaps:
    - WordPress core index and its page sitemap (`wp-sitemap.xml`, `wp-sitemap-posts-page-1.xml`).
    - Yoast index, page sitemap and post sitemap (`yoast-sitemap_index.xml`,
      `yoast-page-sitemap.xml`, `yoast-post-sitemap2.xml`). The page sitemap is the newest entry
      in the index, and the About page is newer than any post.
    - Shopify index (`shopify-sitemap.xml`).
    - Google News sitemap and a news site's mixed index (`news-sitemap.xml`,
      `news-sitemap_index.xml`).
  - Pages:
    - a WordPress listing (`listing.html`) with `<base>`, menus, pagination, tag links, external
      partner links, a sidebar list longer than the listing, and a footer list;
    - a Yoast post (`yoast-post.html`), a Yoast About page (`yoast-about.html`) and a plain About
      page (`about.html`);
    - a dated `og:type=website` post (`dated-website-post.html`);
    - a six-card blog section with full excerpts and a published `<time>` on every card
      (`listing-long.html`), and a div-card listing under `role="main"` (`div-cards-listing.html`);
    - an undated `og:type=article` post with no JSON-LD (`og-article-post.html`);
    - an undated ollama-style `/library/<model>` page (`ollama-library-model.html`);
    - the home page a dead link redirects to (`home.html`), a soft 404 (`soft-404.html`), and a
      wire story with a foreign canonical (`foreign-canonical.html`).
- **`backend/tests/test_article_detect.py` (new, `no_db`, 138 tests after the review fixes).**
  - `url_ok`: the spec's must-pass slugs (`/pets/fat-cat/how-to-feed`, `/news/power-user/tips`,
    `/search/best-laptops-2026`), everyday article shapes, a query string, a loopback host equal
    to the source's own host (and a different loopback host rejected), and 20 shapes that must be
    rejected.
  - Sitemaps:
    - WordPress core, Yoast, Shopify and News class and order;
    - the recently edited About page excluded when a post sitemap exists;
    - a lone page sitemap kept, with its entries still filtered by `url_ok` and `score_page`;
    - edge names: a plain sitemap, Drupal's `?page=2`, `newssitemap`, `.xml.gz`, `/blog/sitemap.xml`
      and `heritage-sitemap`.
  - `score_page`:
    - the Yoast post and the dated `og:type=website` post are accepted;
    - the About page and the Yoast About page are rejected;
    - the home-page redirect and the soft 404 are rejected;
    - a same-path redirect (http to https, trailing slash) is not counted as a redirect;
    - a foreign canonical counts, and a canonical on another host of the same site does not;
    - the ollama page is rejected on its own and accepted under `owner_configured`;
    - owner mode keeps each hard reject and the under-300 rule, with no kind or date scoring;
    - text passed in is used.
  - `listing_links`:
    - the fixture's five posts, in order, with titles and dates;
    - `<base>` honoured;
    - chrome and foreign links dropped;
    - fewer than 3 distinct links gives `[]`;
    - title links beat thumbnail links;
    - card headers survive without `<main>`.
- `ARCHITECTURE.md` is unchanged, because the library adds no process-local state.
  `frontend/scripts/iphone-audit/README.md` is unchanged, because no flows or states were added.

## Deviations from the spec

- **`url_ok` doesn't give courlan the whole URL.** The spec uses
  `courlan.check_url(strict=True)` as the yes/no test, and also requires the cat, user and search
  slugs to pass. courlan 1.4.0 rejects all three in strict mode. Its `SITE_STRUCTURE` regex
  matches `[/_-](cat|user|…)/[^/]+/?$`, so `fat-cat/` and `power-user/` match, and any
  `/search/` segment matches. So the path above the page is ours, by whole segment, and courlan
  sees that path as neutral `x/` segments. It still judges the host, the page's own segment and
  the query. That is also how "skip courlan's domain check for the source's own host" is done: the
  probe gets a placeholder host. The `with_nav=True` option doesn't help, because it still rejects
  `/search/`.
- **`order_sitemaps` is new.** The spec puts the page-sitemap rule under `classify_sitemap`, and
  the ordering in PR10b's `_crawl_sitemaps`. The rule needs the whole list ("skip it whenever a
  post sitemap exists"), so it is a pure function here and is tested with the fixtures. PR10b
  calls it instead of re-implementing it.
- **`classify_sitemap` has no WordPress-specific branch.** Checking the classes in a fixed order
  (page and taxonomy before post) classifies `wp-sitemap-posts-page-1` as page from its whole
  tokens. A WordPress custom post type (`wp-sitemap-posts-recipe-1`) reads as post.
- **`score_page`'s owner mode is a +2 start**, not a separate accept rule, so callers compare
  one score with one threshold. The spec's rules are unchanged: only the hard rejects and the
  under-300 rule apply.
- **What counts as a date** isn't spelled out in the spec. The library counts:
  - published-date metadata only: `article:published_time` and its peers (by `property` or
    `name`), and `datePublished` on JSON-LD Article-family nodes;
  - a body date, only when the page is one article: a `<time datetime>` marked as published
    (`pubdate`, `itemprop`, `published`, `entry-date` or `dt-published`) or inside an article's
    header, or microdata `itemprop="datePublished"`. When the first `<h1>` sits in an
    `<article>` (else a `<header>`), a marked date inside it is the page's, unless several
    articles hold an `<h1>`. Otherwise one counts only when the page has at most one `<article>`
    and all its marked dates agree. So a listing's cards don't date the listing;
  - a dated URL path (`/2026/10/slug`, `2026-10-18`).

  It does not count modified times (Yoast gives every page `article:modified_time`), the
  `datePublished` on a Yoast WebPage node, or trafilatura's guessed date, which can come from a
  copyright line. Without these limits the Yoast About page earns +1 and escapes the −3.
- **JSON-LD is read at the top level, in `@graph`, and in `mainEntity`, and no deeper.** So a
  listing's `ItemList` of BlogPostings doesn't make the listing an article. The Article family
  includes schema.org's subtypes (TechArticle, LiveBlogPosting, the NewsArticle subtypes, …).
- **Soft-404 titles** cover "404", "Error 404", "404 Not Found", "404: Page not found", "Page not
  found", "Not found", "Nothing found" and WordPress's "Oops! That page can't be found." Each must
  be a whole part of `<title>` or `og:title` split on separators (`|`, `–`, `-`, …), or of the
  first `<h1>`. A part that equals the site name is skipped. So "404 errors: how to fix them" and
  "FBI story | 404 Media" are not soft 404s.
- **JSON-LD `@type` prefixes** (`schema:NewsArticle`, `https://schema.org/NewsArticle`) are
  stripped before matching.
- **`/archives/123` passes `url_ok`,** though courlan's strict filter alone would reject any
  `/archives/` path. That is WordPress's built-in Numeric permalink, so rejecting it would empty
  every such site. "Title is the site name" means every
  part equals `og:site_name`, or the JSON-LD WebSite name. So "Example Blog | My post" does not
  count.
- **`listing_links` filters with `url_ok` before grouping,** not just by same site. Without that,
  each card's tag links join its "Read more" links in one larger group (`article/footer/a`), and
  that group wins.

## Verification

- **Full backend pytest** on `popping_test`, after the fast-forward onto main 520ee43 (PR1r and
  PR6a): **13 failed, 1060 passed**. The 13 failures diff clean against the DNS-only baseline in
  `/root/popping-env/baseline.txt`. That is 997 on main plus the 63 new tests. Before the
  fast-forward, on ed956b4, the result was 13 failed, 1009 passed, with the same 13 failures.
- `tests/test_article_detect.py` alone: 63 passed in about 0.5 s, with no database.
- `ruff check --select F,E9,B` on both new Python files: clean.
- No frontend file changed, so frontend lint, build, unit tests and smoke were not needed. No
  backend test that pins frontend source was touched.
- **Security checklist:** no routes, no fetches and no stored JSON. `courlan.check_url` runs
  without `with_redirects`, so it never touches the network. `tld` reads its bundled suffix list.
  `trafilatura` parses a string it is handed.
- **Mutation checks.** A script broke each guarded line, ran the named test(s), expected a
  failure, and restored the file byte for byte (checked by SHA-256). All 43 mutations were caught:

  | Broken line | Failing test |
  |---|---|
  | courlan judges the whole path / host check never skipped | `test_url_ok_keeps_articles` / `test_url_ok_keeps_a_loopback_host_that_is_the_sources_own` |
  | no same-site check / no segment list / no static slugs / no date-archive rule / no query-key rule | `test_url_ok_rejects_pages_that_are_not_articles` |
  | segment list matched by substring / static slugs at any position | `test_url_ok_keeps_articles` |
  | post class checked before page | `test_wordpress_core_sitemaps_classify_on_the_subtype_token` |
  | post class checked before taxonomy | `test_yoast_sitemaps_classify_and_the_page_sitemap_is_skipped` |
  | tokens matched as substrings / query read as part of the name | `test_classify_sitemap_edge_cases` |
  | page sitemap kept beside posts | `test_recently_edited_about_page_is_excluded_when_a_post_sitemap_exists` |
  | lone page sitemap dropped | `test_a_lone_page_sitemap_is_kept_and_its_entries_still_filtered` |
  | skipped classes read | `test_shopify_sitemaps_keep_only_the_blogs` |
  | news not first | `test_news_sitemap_reads_first` |
  | og:type article not cancelled by WebPage / JSON-LD date from any node / modified time counts | `test_yoast_about_page_with_og_article_is_rejected` |
  | no JSON-LD article +3 | `test_yoast_post_is_an_article` |
  | website −3 even when dated / no h-entry or article-header time | `test_dated_og_website_post_is_accepted` |
  | no website −3 | `test_about_page_is_rejected` |
  | threshold 3 / owner mode without the +2 | `test_dated_og_website_post_is_accepted`, `test_undated_library_page_is_accepted_under_a_link_pattern` |
  | owner mode still scores kind and date / no under-300 rule | `test_owner_configured_rejects_a_page_under_300_characters` |
  | no soft-404 / no site-name / no redirect / no foreign-canonical rule | `test_soft_404_is_rejected`, `test_home_page_redirect_is_rejected`, `test_foreign_canonical_counts_against_a_page`, `test_owner_configured_keeps_the_hard_rejects` |
  | redirect compares raw paths | `test_redirect_to_the_same_path_is_not_a_redirect` |
  | canonical compared by host, not site | `test_foreign_canonical_counts_against_a_page` |
  | `text` argument ignored | `test_text_passed_in_is_used_instead_of_extracting` |
  | no chrome dropped | `test_listing_links_takes_the_repeating_post_list`, `test_listing_links_drops_chrome_and_foreign_links` |
  | header/footer kept only inside article or main | `test_listing_links_keeps_post_card_headers_without_main` |
  | `<base>` ignored | `test_listing_links_resolves_against_base` |
  | links filtered by same site only / foreign links kept | `test_listing_links_takes_the_repeating_post_list` / `test_listing_links_drops_chrome_and_foreign_links` |
  | no tie-break on link text | `test_listing_links_prefers_title_links_over_image_links_to_the_same_posts` |
  | group minimum 2 | `test_listing_links_needs_three_distinct_links` |
  | date read from the link instead of its card | `test_listing_links_takes_the_repeating_post_list` |

  This table covered the first commit. The review found that it had no row for the
  `og:type=article` +3 or the JSON-LD site −3, and no default-mode test for the under-300 rule. Those
  rows are in "Review fixes" below.

  On the first run, three mutations survived, and each one was fixed:
  - **The query-key rule.** The test URL `/?s=trail` was already rejected as the home page. The
    test now uses `/blog/?s=trail` and a `?replytocom=` link.
  - **Two malformed mutations** were rewritten to break the intended line: reading the query into
    the sitemap name, and owner mode still scoring kind and date.

## Review fixes (second commit)

The `/code-review` findings, each fixed, with tests:

- **A listing page counted as dated.** On a listing, every card's `<time class="entry-date
  published">` matched, and the first one became the page's date. So a blog section with long
  excerpts scored +2 and was accepted. Body dates now count only when the page is one article (see
  "What counts as a date"), and microdata `itemprop` dates are read with them, not as head
  metadata. `listing-long.html` now scores −3. Its div variant (no `<article>`, no `<h1>`), a
  same-day variant and an `<h1>`-per-card variant are rejected too. A post with dated related-post
  `<article>` cards keeps its own date.
- **Soft-404 matched any title starting with 404.** Anchored to a whole title part, and site-name
  parts are skipped. Tested in both modes: seven not-found titles get −3, and "404 errors: how to
  fix them", "Error 404 explained", a site named "Not Found" and "FBI story | 404 Media" don't.
  - These tests use 450 characters of text, so a dated `og:type=article` page scores 4 (owner
    mode: 2) and only the title can reject it. At 600+ characters a fully marked-up page scores
    3 + 1 + 1 = 5, and the −3 leaves it at 2, still accepted. That is the spec's arithmetic; see
    Open items.
- **`url_ok` rejected real article sections.** The fixes are listed in "What changed": record and
  archive words by position, `page` only before a number, `legal`, `help` and `support` no longer
  static sections, and a lone year an archive only at the root or under a blog, news or archive
  section. New must-pass cases: `/archives/123`, The Atlantic's and Reuters' shapes, `/help/how-to-…`,
  `/blog/products/new-launch`, `/news/profiles/…`, `/node/2015`, `/p/2041`, `/page/how-we-report`.
  New must-reject cases: `/archive/`, `/archives/`, `/archives/2026/07/`, `/archives/2015`,
  `/page/2/`, `/users/dana`, `/forum/users`, `/2026`, `/2026/07/`, `/blog/2026`, `/blog/2026/07/`,
  `/news/2026/10/18/`, `/help`, `/legal/`. courlan itself rejects a year with a trailing slash, so
  the slash-less forms are what pin the lone-year rule.
- **WordPress Plain permalinks.** `/?p=123` passes; `/?page_id=2`, `/index.php?page_id=2`, `/` and
  `/?s=x` don't. `classify_sitemap` reads WordPress core's plain sitemap query
  (`?sitemap=posts&sitemap-subtype=page` is page, `?sitemap=taxonomies&…` is taxonomy,
  `?sitemap=users` is author).
- **`_drop_chrome` dropped div-card headers.** Role scoping and the repeat rule (see "What
  changed"). `div-cards-listing.html` gives all five titles and dates, both under `role="main"`
  and with the role removed. A header inside a `role="main"` region is kept.
- **Prefixed JSON-LD types.** Stripped; `https://schema.org/`, `http://schema.org/` and `schema:`
  NewsArticle are each accepted with their `datePublished`. The finding's trafilatura-date part
  needed no change: trafilatura's guessed date still doesn't count.
- **Untested rules, now pinned:**
  - `og:type=article` +3: `og-article-post.html`, undated, scores 3 − 1 + 1 = 3 and is accepted.
  - The JSON-LD site −3: the Yoast About test asserts `-3 json-ld aboutpage/website, undated`. A
    new test pins the open PR10b edge case: an undated `og:type=article` page with only a JSON-LD
    WebSite node, at 650 characters, scores exactly 0.
  - The −2 under-300 rule in default mode: an undated JSON-LD Article stub (3 − 1 = 2 before
    length) is rejected at 150 and 299 characters and accepted at 300 and 450. The finding
    suggested `yoast-post.html` with 150 characters, but that page is dated, so it scores
    3 + 1 − 2 = 2 and stays accepted. The stub is undated so the rule decides the result.
  - A date in the URL: an undated `og:type=website` page at `/2026/10/18/slug` gets
    `+1 date in URL`, no website −3, and is accepted.
  - A class-less `<time>` counts in the article's header, but not in its footer or outside the
    article.
  - Sitemap fallback order: an unknown `sitemap-1.xml` reads before a lone page sitemap.

**Mutation check for the review fixes.** A script applied each mutation to
`article_detect.py`, ran `tests/test_article_detect.py`, required the named test to fail, and
restored the file byte for byte (checked by SHA-256). All 34 were caught:

| Broken line | Failing test |
|---|---|
| body date: first marked date always / no article-count rule / no distinct-dates rule / no `<h1>`-per-card rule | `test_long_listing_with_dated_cards_is_rejected` |
| body date: no `<h1>` scope | `test_a_post_with_related_cards_keeps_its_own_date` |
| microdata read as head meta / microdata on non-`<time>` ignored | `test_listing_microdata_dates_are_not_the_pages` |
| no in-article-header `<time>` | `test_a_class_less_time_counts_only_in_the_articles_header` |
| URL date ignored (`url_dated=False`) | `test_a_date_in_the_url_counts_as_dated` |
| old prefix-matching 404 regex / site-name part not skipped | `test_a_title_that_only_mentions_404_is_not_a_soft_404` |
| no 404 alternative | `test_a_404_title_is_a_soft_404` |
| JSON-LD prefix kept | `test_prefixed_json_ld_types_count` |
| no `og:type=article` +3 (`elif False`) | `test_og_article_post_without_json_ld_is_accepted` |
| no JSON-LD site −3 (`elif False`) | `test_undated_og_article_with_a_site_wide_json_ld_website_is_rejected`, `test_yoast_about_page_with_og_article_is_rejected` |
| −2 rule only in owner mode | `test_a_short_page_loses_two_points` |
| other and page sitemap ranks swapped | `test_a_page_sitemap_reads_after_an_unknown_sitemap` |
| plain-permalink sitemap query ignored | `test_classify_sitemap_edge_cases` |
| record words / archive / `page` rejected anywhere; `legal`, `help`, `support` as sections; lone year always an archive; root always home | `test_url_ok_keeps_articles` |
| record words / archive / `page` never rejected; lone year never an archive; no `/YYYY/MM/` rule; root never home; no `page_id` key | `test_url_ok_rejects_pages_that_are_not_articles` |
| no role scoping in `_drop_chrome` | `test_listing_links_keeps_a_header_inside_an_aria_main_region` |
| no repeat rule (drop every unscoped header) | `test_listing_links_keeps_div_card_headers` |
| unscoped headers never dropped | `test_listing_links_takes_the_repeating_post_list` and four more |

On the first run, two mutations survived and were fixed with new tests: the `<h1>` scope (now
`test_a_post_with_related_cards_keeps_its_own_date`) and "lone year never an archive". courlan
already rejects `/2026/` and `/blog/2026/`, so the slash-less `/2026`, `/blog/2026` and
`/archives/2015` were added.

**Checks after the fixes:** `tests/test_article_detect.py`: 138 passed. Full backend pytest on
`popping_test`: 13 failed, 1135 passed (1060 + 75 new tests). The 13 failures diff clean against
the DNS-only baseline. `ruff check --select F,E9,B` on both files: clean.

## Risks and data-compatibility notes

- None at runtime: nothing imports the module yet.
- Revert is safe.

## Open items for later units

- **The owner's junk sites aren't covered yet.** Their URLs weren't available to this session,
  and this container has no DNS, so nothing was fetched live. Once the owner sends the URLs, save
  each page and sitemap offline into `backend/tests/fixtures/scrape/` and add cases (spec's last
  Tests bullet).
- **PR10b wiring:**
  - `_crawl_sitemaps`: run `order_sitemaps` on the robots.txt-listed sitemaps and on each index's
    nested list, before the newest-first sort. Run `url_ok(entry, source.url)` on every entry,
    then `link_pattern`.
  - `_extract_one`: pass `text=` from the `bare_extraction` it already runs, and `final_url=`
    once `fetch_html` gains `return_final_url`, both in the same `asyncio.to_thread` call. Accept
    when `score >= ACCEPT_SCORE`. The `reasons` list suits a debug log line.
    `owner_configured=bool(link_pattern or sitemap_url)`.
  - `listing_links(html, url)` when there is no link pattern. Its `Candidate.published` can feed
    the newest-first sort and the undated cap.
  - `feed_autodiscovery._site_key` is an approximate registrable domain. `article_detect.same_site`
    uses the public-suffix list. Consider switching `_same_site` to it, so the crawl and the
    filter agree on what "same site" means (for example `*.blogspot.com` tenants).
- **Scoring false negative to watch in PR10b's review:** an undated post with `og:type=article`
  on a site whose every page carries a site-wide JSON-LD WebSite node scores +3 −3 −1 +1 = 0 and
  is rejected. This follows the spec's −3 rule literally. If the owner's VPS probe shows such
  feeds losing stories, the fix is to stop counting the WebSite node against a page that earned
  +3 from `og:type=article`. That needs a spec change.
  `test_undated_og_article_with_a_site_wide_json_ld_website_is_rejected` pins today's result
  (score 0), so changing it means updating that test on purpose.
- **Soft-404 arithmetic to watch in PR10b's review:** the spec's −3 for a 404 or site-name title
  doesn't reject a page that earns everything else (`og:type=article` or JSON-LD article, a date
  and 600+ characters: 5 − 3 = 2). A soft 404 rarely carries article markup and a published
  date, so this is left as specified. If the probe shows such pages, making the title rules an
  outright reject is a spec change.

## Device checks

None. Nothing calls the library until PR10b, so there is nothing to see on a device. Nothing was
added to `docs/DEVICE_CHECKS.md`.
