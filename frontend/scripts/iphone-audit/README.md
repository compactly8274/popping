# iPhone layout scripts

Everything here renders the built app in a browser with iPhone emulation
(390x844 at 3x and 320x568 at 2x, touch on; a few states also at 312x675, a
390 phone with Page Zoom at 125%; desktop flows at 1280x800 with no touch)
against a fully mocked API
(`capture.cjs` `mockApi`, data in `fixtures.cjs`). All scripts are CommonJS
(`.cjs`) because `frontend/package.json` is `"type": "module"`, and card
locators are scoped to `main.md\:hidden` because App renders the hidden
desktop grid too.

| File | What it is |
|---|---|
| `smoke.cjs` | The layout guard. Runs in CI on every push; exit 1 on any failure. |
| `flows.cjs` | Behaviour flows: what the app sends when a person uses it. `smoke.cjs` loads it and runs each flow at 390. |
| `capture.cjs` | The original audit: screenshots plus a raw overflow scan per state. Exports `mockApi` and `IOS_UA`. |
| `probe.cjs` | Targeted measurements behind the audit. |
| `fixtures.cjs` | Sources, entries and settings the mock serves, plus the long strings the checks lean on. |
| `run.sh` | Local wrapper: build, preview on 4173, then capture, analyze and probe. |

## Requirements

Node 22 and the `playwright` package resolvable with `require()` plus a
browser it can launch. Nothing is added to `package.json`.

- Claude Code cloud container: `playwright` is
  `/opt/node-tools/node_modules/playwright` (on `NODE_PATH`) and Chromium is in
  `/opt/pw-browsers`, so set `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`. Never
  run `playwright install` there (the CDN is blocked). WebKit is not available.
- CI: `npm install --no-save ... playwright@1.56.1` and
  `npx playwright install --with-deps chromium`; the Safari-engine report
  installs WebKit in its own job, which never holds up the image (see below).
- Elsewhere: install `playwright` outside the repo (or `--no-save`) and
  `npx playwright install chromium`.

`smoke.cjs` never sets `PLAYWRIGHT_BROWSERS_PATH`; the caller does.
`run.sh` exports it because it is the local-only wrapper.

## smoke.cjs

    node scripts/iphone-audit/smoke.cjs <base> [--csp] [--strict] \
         [--browser chromium|webkit] [--out <dir>] [--only <state>[,<state>...]] \
         [--states-only]

- `<base>`: where `vite preview` serves the build, e.g. `http://127.0.0.1:4173`.
- `--csp`: serve every document with the production Content-Security-Policy,
  read from `frontend/nginx.conf.template` with one regex, and fail on any
  "Refused to" console message (rule C9). Chromium only; ignored with a note
  under `--browser webkit`. Exits 1 if the template has no CSP.
- `--strict`: C6 and C7 fail the run instead of warning (from PR9).
- `--browser webkit`: Playwright's `devices['iPhone 13']` with the viewport set
  explicitly to each width (the descriptor's own viewport is 390x664).
  Chromium uses an iOS Safari user agent with `isMobile` and `hasTouch`.
- `--out <dir>`: write `<width>-<state>.png` for every state. Without it no
  screenshots are taken (CI cannot retrieve them anyway). That picture is
  taken before a flow's `run`, so it shows where a flow starts; a flow saves
  its later steps with `shot(label)`, written as `<width>-<state>-<label>.png`.
- `--only`: run only the named states (comma-separated).
- `--states-only`: skip the behaviour flows (`flows.cjs`). CI's WebKit
  report-only run uses it.

After the summary, the 10 slowest flows are listed with their times.

Local run, from `frontend/` (section 13 of the plan):

    npm ci && npm run build
    ./node_modules/.bin/vite preview --host 127.0.0.1 --port 4173 --strictPort &
    P=$!
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
      node scripts/iphone-audit/smoke.cjs http://127.0.0.1:4173 --csp --out "$SCRATCHPAD/smoke"
    kill $P

Look at every PNG afterwards: the rules catch overflow, your eyes catch "this
looks wrong".

### How a state runs

For each width, each state gets a fresh browser context (never
`setViewportSize`: long sessions gave false `pointer: coarse` readings) with
`colorScheme: 'dark'`, the `en-CA` locale, the `America/Vancouver` time zone
and `clock.setFixedTime(F.NOW)`, so relative times ("12m ago") are stable.
`setFixedTime` freezes `Date.now` but timers keep running. The phone widths
have `isMobile`, `hasTouch` and an iOS user agent. The 1280 width
(`desktop: true`, not in the default widths) has neither, uses a desktop
Safari user agent and skips C1; only states and flows that list it run there,
and they need `wait: 'main.grid article[data-card-id]'`, because the default
wait looks for a phone card in the hidden `main.md:hidden`. Then:

1. `mockApi(context, { onUnmocked })`, the runtime listeners (C8) and, with
   `--csp`, the CSP route (C9); then the state's `before` hook (extra routes);
2. open `url`, wait for a phone card (`main.md\:hidden article[data-card-id]`)
   or the state's `wait` selector, let requests settle and any load-time
   toast go away;
3. run the state's `setup`, let requests settle, wait 400 ms;
4. run the layout rules, take the screenshot, then run the rules that
   temporarily change the page (C2b, C6), which put everything back;
5. run the state's `run({ page, context, width, settle, fail, shot })`.

### States (PR2)

| State | What it shows |
|---|---|
| `dashboard` | the phone dashboard at 390, 320 and 312 (also runs C2b and C16); the App's top bar (`header[data-app-header]`, not the Column headers) must hold exactly search, refresh and the Menu button, and no settings gear. PR7a: the Edit pill (`button[data-edit-tabs]`) sits outside the tablist, on screen, uncovered and at least 44px tall |
| `tabs-editor` | PR7a, at 390 and 320: the Edit tabs sheet (`[role="dialog"][aria-label="edit tabs"]`, in `C12_ROOTS`) after its slide-up: Brief, then a row per tab with ↑, ↓, Move to top and a Show switch |
| `column-popover` | News's ⋯ popover at 390 and 320: Mark all as read, Sort (Newest | Best, PR1c) and Show stories from (PR4d: `button[data-window]` for 1 day, 2 days, 7 days and All, 2 days chosen, each 44px tall, inside the viewport and unclipped); a pointerdown outside closes it, and ⋯ again closes rather than reopens it; Saved has no ⋯ (`[data-column-action="prefs"]`: nothing in it would apply) |
| `card-summary` | summaries open on card 101 (180-character title, thumbnail, 60-character source) and 102 (90-character unbroken token) |
| `search-x` | search open with "x" typed |
| `search-60` | search with the 60-character `F.SEARCH_QUERY_60` (results header) |
| `menu` | the Menu (≡, `open menu`) after its slide-in, at 390, 320 and 312 |
| `settings-feeds` | Settings → Feeds (`?view=settings&tab=feeds`) |
| `settings-add-youtube` | Feeds → the third feed-manager tab (Add), YouTube selected, long name and URL typed |
| `settings-add-error` | Add, YouTube, a Test that fails with YouTube's longest message (`F.YOUTUBE_BLOCKED_MESSAGE`) |
| `settings-add-newcat` | Add, the category picker's New-category field with a long name |
| `settings-add-headers` | Add, Advanced open: a browser identity and a refused `Cookie` header |
| `settings-suggested` | Feeds → the second feed-manager tab (Suggested) |
| `settings-llm` | the AI tab |
| `settings-notifications`, `settings-history`, `settings-hidden`, `settings-starred` (Saved), `settings-voted` (Downvoted), `settings-reset` | one per tab via `?view=settings&tab=`; `settings-reset` also holds the build line (PR1r), which passes C14 at 320 |
| `brief-empty` | 320 only: the phone Brief tab with `/api/brief/latest` returning `[]` |
| `brief-needs-ai` | PR1c, at 390, 320 and 312: `/api/llm/status` says `configured: false`; the phone Brief tab, expanded, with no brief: "Brief needs an AI provider · Set up" on its own 44px row, on one line |
| `brief-needs-ai-compact` | PR1c, the same at 390, 320 and 312 with one brief, collapsed: the Set up row under "Brief" and its time |
| `caught-up` | PR3b, at 390 and 320: the NHL tab with its 3 stories seen (`seen:nhl` in the store): no card above ✓ "You're all caught up", then C8's row naming the next tab with new stories (Podcast) and the folded "Earlier (3)"; every row 44px, none `role="status"` or a `<section>` |
| `not-seen-row` | PR3b: the NHL tab with nothing seen: its 3 cards, then the 44px "3 not seen · Mark as read" (D24), no Earlier |
| `earlier-open` | PR3b: the NHL tab with two stories seen and Earlier opened: one card, "1 not seen · Mark as read", "Earlier (2)" and its two cards at opacity 0.6 |
| `card-action-sheet` | PR5a, at 390 and 320: the first card's ⋯ sheet (`[role="dialog"][aria-label="story actions"]`, in `C12_ROOTS`) after its slide-up: Hide, Edit feed, Copy link, Open in browser and Cancel, each 44px |
| `card-preview-long` | PR5a, D34, at 390, 320 and 312: For You's first card (101) with its 300-character preview under the headline, `-webkit-line-clamp: 2`, no taller than two lines and cut, so C11 and C14 run with a long blurb |
| `card-no-preview` | PR5a, at 390, 320 and 312: the News tab, whose story 103 has `preview: null` and shows none (the card as it was), while 101 shows its preview |

The three read-model states install the page's clock in `before` and hold it
(`pauseAt`) just before the tap on the NHL tab: its few cards sit on screen
with the marker, which on the phone uses them up a second later (PR3c's end
rule), so each state is the tab as it opens.

Selectors avoid visible labels that the redesign changes: feed-manager tabs are
picked by position, headers by `aria-label` (`close settings`, `close menu`).

To add a state, append to `STATES` in `smoke.cjs`:

    { name, url, widths, wait, before({ page, context }), setup({ page, settle }),
      extra: ['C16'], checks: false, run({ page, fail, shot }) }

Every field but `name` is optional. `extra` adds rules for that state only;
`checks: false` skips the layout rules; `run` does assertions after the checks
and reports with `fail(rule, detail)`.

### Rules

| Rule | Check |
|---|---|
| C1 | `matchMedia('(pointer: coarse)')` matches (the harness really is a touch phone). |
| C2 | `body.scrollWidth` and `#root.scrollWidth` ≤ `innerWidth` (`html` hides overflow behind `overflow-x: clip`). Names the widest box outside any scroller. |
| C2b | `dashboard` only: with `#root` given a 34px bottom padding (the home indicator; Chromium's `env()` is 0), `document.scrollingElement.scrollHeight` ≤ `innerHeight + 1`, and no in-flow child of `#root` ends below its content box. The second half is needed because `#root` is border-box, so Chromium shows the overlap that iOS turns into a scroll range. |
| C3 | Every visible box has `left ≥ -1` and `right ≤ innerWidth + 1`. Only descendants of `[data-hscroll]` are skipped. |
| C4 | Every visible vertical scroller (computed `overflow-y` auto or scroll; not `[data-hscroll]` strips, not form controls) has `scrollWidth ≤ clientWidth + 1` and `overscroll-behavior-y: contain`. |
| C5 | Each line of every text node lies inside its nearest ancestor whose `overflow-x` is not visible (or the viewport), unless an ancestor up to that box has `text-overflow: ellipsis` or `-webkit-line-clamp`, a `[data-hscroll]` strip is in between, or the box is ≤ 1px or `clip: rect(0 0 0 0)` (sr-only). |
| C6 | With the `(pointer: coarse)` 16px `!important` rule deleted through CSSOM, every visible input (not checkbox, radio, range), select and textarea is ≥ 16px. The rule is put back afterwards. Warning until `--strict`. |
| C7 | Inside `[role=dialog]`, the Menu, `header`, `[role=tablist]` and toast buttons: every button, link, select, `[role=button/tab/switch]` is ≥ 43.5px tall, and ≥ 43.5px wide when it has no text; checkboxes and radios are measured on their `label`; `[data-tap-exempt]` is skipped. Warning until `--strict`. |
| C8 | No `pageerror`, no console error except the expected `/auth/me` 404, no request the mock does not answer, no `alert`/`confirm`/`prompt`. |
| C9 | With `--csp`: no "Refused to" console message. |
| C11 | Text inside `button`, `[role=tab]`, `time`, `[data-one-line]` or a `nowrap` link sits on one line. |
| C12 | Inside the phone `main`, the Settings dialog and the Menu: every text line and every input, select and button is ≥ 8px from both screen edges, unless inside `[data-bleed]` or `[data-hscroll]`. |
| C13 | A card headline is ≥ 55% of the card's content box; no text block is taller than 8 lines while narrower than 160px. |
| C14 | A box with `text-overflow: ellipsis` is at least `min(96px, its scrollWidth)` wide. |
| C15 | No visible `input[type=search]`. |
| C16 | The top bar's title (`header h1`) is not cut off (`scrollWidth <= clientWidth`). |
| C17 | Every state and flow: no `PUT /api/preferences/<key>` of a set key (`starred_entries`, `hidden_entries`, `voted_entries`, `read_entries:*`, `seen:*`). From PR2b they change only by PATCH; PR2c makes the server refuse the PUT. The mock reports it (`onSetKeyPut`) and answers the server's 409. |

"Visible" means: has client rects, not `visibility: hidden`, no ancestor at
opacity 0, not inside `[inert]` or `[aria-hidden="true"]`, not inside the
closed Menu (its panel starts at or past `innerWidth`), and, while an
`aria-modal` dialog is open, inside that dialog (or a `[role=status]` toast):
the Settings sheet and the Menu cover the page behind them.

Each rule reports the outermost offender only (a failing button is reported,
its label text is not reported again under the same rule).

Markers the rules understand: `data-hscroll` (an intentional horizontal strip),
`data-bleed` (edge-to-edge on purpose), `data-one-line` (must not wrap),
`data-tap-exempt` (prose links inside cards).

### Output

Each state prints one progress line. Then every failure is printed in full:

    FAIL <width> <state> <rule>: <what is wrong>
        at   <selector path>
        rect l=… t=… r=… b=… (w x h)
        text "<text>"

When `GITHUB_ACTIONS` is set the failures are also `::error::` annotations
(CI screenshots cannot be retrieved and annotations stay readable when raw
logs are not). GitHub keeps only the first few annotations of a step, so there
is one annotation per distinct problem (rule, element, text) naming every
width and state it failed in. Each warning rule gets one `::warning::` summary.
Warnings (C6, C7 without `--strict`) are listed once per width, rule and
element, with the states they appeared in. A summary of counts per width,
state and rule closes the output. Exit 1 on any failure, 2 on a usage error.

### CI

`.github/workflows/build.yml`, job `frontend-checks`, after `npm run build`:
installs `playwright@1.56.1` with `--no-save` (and proves `package.json` and
the lockfile are untouched), installs Chromium and WebKit, starts
`vite preview` on 127.0.0.1:4173, then runs

    node scripts/iphone-audit/smoke.cjs http://127.0.0.1:4173 --csp --strict
    node scripts/iphone-audit/smoke.cjs http://127.0.0.1:4173 --browser webkit --states-only   # report only

The WebKit step is `continue-on-error`: WebKit on Linux is not iOS Safari.
It skips the flows, which drive Chromium's touch input and would add about
as much time again for a report nobody gates on. The job has a 30-minute
timeout.

Read CI failures with
`gh api repos/<owner>/<repo>/check-runs/<id>/annotations`.

## flows.cjs

Each flow is shaped like a state with `checks: false` and a `run`, and runs at
390 in its own context with the same mock, clock, CSP and C8 listeners. A flow
records the requests it cares about with a page route (`record()`), which runs
before `capture.cjs`'s context mock and then falls through to it, or answers
for it. `flows.cjs` must not require `smoke.cjs` (smoke requires it).

| Flow | What it proves |
|---|---|
| `flow-fj-youtube-handle` | YouTube, `@handle`, Name blank: Test and Add are enabled; Add sends the resolved feed and `somehandle_yt` |
| `flow-fj2-youtube-video-link` | a watch link with Name blank says "Name is chosen after Test."; Add runs one Test, then one POST named `channel_<id6>_yt` |
| `flow-fp-auto-subreddit` | Auto-add with `r/python` fills the form as Subreddit and sends nothing |
| `flow-fq-new-category` | New category "NEWS" lands in "news"; Add feed waits while a new category is pending; "Hockey" shows as "Hockey (new)" and is sent as typed |
| `flow-fr-headers` | a half-filled header holds Test and Add with its message; switching to Subreddit drops the headers from the POST |
| `flow-fs-auto-already-added` | Auto-add answering with a feed already followed shows "Already added: …" |
| `flow-fg-settings-back` | Menu → Settings opens Feeds; after Done, history grew by at most one entry and Back stays in the app with Settings closed |
| `flow-ft-menu-edit-feed` | a source row's › in the Menu opens that feed in Settings, in view; a `?source=` change while Settings is open moves to another feed |
| `flow-fu-jump-to-column` | Jump to column selects that phone tab and scrolls it into the strip |
| `flow-tab-stays-on-star` | PR1b: on News, saving a story fills the Saved tab in front of it; the phone stays on News, stores `cat:news` (`popping.v1.mobileTab`), and once the save is in the store a reload opens News with the story saved |
| `flow-unsave-last-saved` | PR1b, with one saved story (seeded in the store): a new phone opens on For You; PR7a: unsaving the only saved story leaves Saved in the strip, empty ("No stories yet", no first-feed button), with the phone still on it and `meta:saved` stored; saving from For You doesn't move the phone |
| `flow-tab-migrates-index` | PR1b: a stored old index (`mobileCol.last` = 0) with the preferences 1.5 s late (they bring the saved story, seeded in the store, that made index 0 Saved) opens Saved, stores `meta:saved` and drops the index; migrated before them it would be For You |
| `flow-tab-gone-on-load` | PR1b: a stored tab whose category is gone opens For You, not Brief, and stores it |
| `flow-filter-clear-keeps-tab` | PR1b: on News, a source filter leaves `cat:news` stored; clearing it while the unfiltered stories are 1.5 s late stores nothing in between and lands back on News |
| `flow-foryou-fails-keeps-tab` | PR1b: a new phone stores For You; PR7a: a refresh whose For You request fails (503) empties the tab but keeps it, with the phone on it and `meta:foryou` stored, and its stories come back when it loads again. PR3b: the failed poll leaves the stories shown in place (a poll never takes a story out of a snapshot); the tab is empty once it starts over (Deals and back) |
| `flow-foryou-down-settles` | PR1b, For You failing (503) throughout: For You is an empty tab; a new phone shows Saved but stores nothing; a tap on Saved stores it, and unsaving its only story leaves the phone on Saved, still stored (PR7a) |
| `flow-foryou-down-holds-fresh` | PR1b, For You failing (503) and nothing stored: the phone shows a category (not an empty Saved or For You) without storing it; saving a story there fills Saved in front of it, and the phone stays on that category and still stores nothing |
| `flow-foryou-down-holds-index` | PR1b: the same with only an old index stored (`mobileCol.last` = 1); the index isn't migrated while For You is down |
| `flow-prefs-fail-keeps-saved` | PR1b: `GET /api/preferences` fails (500) with `meta:saved` stored; PR7a: the phone shows Saved (always a tab, empty while unread), and `meta:saved` stays stored, also after a poll |
| `flow-tap-tab-scrolls-top` | PR1b: only the phone list carries `data-scroller`; tapping the shown tab scrolls that list to the top (at once under reduced motion) without moving the phone; another tab opens at its own top |
| `flow-sort-best` | PR1c: For You opens on Best, in the server's order; News opens Newest (newest first) with no minimum score slider; Best reorders it by score and stores `column_prefs:news` with `sort: 'best'`; tapping a saved view's chip that captured News as Newest writes no `column_prefs` and keeps Best; Saved has no ⋯ (so no Newest \| Best, PR4d) |
| `flow-brief-needs-ai` | PR1c, `/api/llm/status` saying `configured: false`: the phone Brief's four Generate / Regenerate states (no brief expanded and collapsed, then one brief expanded and collapsed) each read "Brief needs an AI provider · Set up" (44px); Set up opens Settings › AI; with a provider set up, closing Settings brings Regenerate back; with it gone again, coming back to the front reads the status again, and at 1280 (`setViewportSize`) the header's Brief is the same Set up, which opens Settings › AI, and comes back as Brief after Done |
| `flow-bookmark-on-save` | PR1c: the bookmark (`save`) on a story sends one `bookmark` interaction; again (`unsave`) sends none |
| `flow-bookmark-on-save-desktop` | PR1c, 1280: the card's bookmark sends one `bookmark`, unsaving none (PR5a took Save out of the right-click menu); keyboard `b` on the selected card (save, then unsave) sends one in all. PR7a: the arrow keys walk the grid's columns, the first of which is the now empty Saved, so ArrowRight comes first |
| `flow-saved-order` | PR1c, rewritten in PR4c: Saved lists saves newest save first, in the order they were saved, latest first, whatever their stories' dates (saved as 103, 105, 111, 101, it reads 101, 111, 105, 103; by date it would read 101, 103, 105, 111) |
| `flow-saved-by-id` | PR4c, D5: 503 and 402 never reach a tab (the page's fetch drops them from `/api/entries` and `/api/foryou`). Saved still lists 105, 503, 103 (newest save first) with 503's thumbnail loaded and its preview shown (PR5a: by-ids carries `preview`, as the server's projection does), from one `GET /api/entries/by-ids` for just the saves no tab holds, `999999,503` (999999 has no row). A save made on another device (seeded in the store, then a return to the front) is asked for alone (`402`); its answer is held while one more poll lands, and that poll doesn't ask for 402 again; let go, 402 shows at the top. With the clock held from here, an unsave takes 503 out, and its Undo brings it back from the by-id cache with by-ids offline and no request: in its old place (402, 105, 503, 103) when tapped before the unsave's 1 s batch, at the top (503, 402, 105, 103) once `runFor(1000)` sent the unsave to the store. Unsaved again, it stays out through a poll; poll after poll, 999999 is never asked for again |
| `flow-saved-ignores-window` | PR4d, D5: Saved has no window. 605 (10 days old) and 505 (2.8 days) are outside the 2 days the tabs load, so one `GET /api/entries/by-ids` (`505,605`) brings them, and Saved lists 103, 505, 605 (newest save first) |
| `flow-hide-undo-after-poll` | PR4c, clock held from the load (`holdClockFromLoad`: each 1 s batch goes only on `runFor`, the toast stays up, and no card has a second on screen before the flow's first step, so scrolling 102 into view, which takes the cards above it off the top, marks none of them seen): on For You (101, 301, 102, …), 102 is scrolled to the top of the screen and read for a dwell, then hidden with ⋯ → Hide (PR5a; `hideViaSheetHeld` steps the held clock a frame at a time until the sheet is up, with seen on scroll paused under it) (the badge drops by one and no more; its PATCH reaches the store) and a poll comes back without it in either list (the mock leaves hidden stories out, as the server does since PR4b). Undo: 102 is back, the list isn't started over at its top and the card being read is still on screen (a put-back goes into the snapshot's place; only a new snapshot starts the list over at its top, PR3c), and after the seen batch the badge is as before the hide (the put-back story isn't marked by coming back; the Undo's own batch is still held). Then News (never drawn before) shows 102 and For You (opened again, so built afresh) reads exactly as before the hide: the Undo itself put 102 back in both lists, and in For You at its place (102 is in the middle, so a put-back at the top or the end both fail). A poll before the Undo's batch also leaves it out, and News and For You, each opened afresh after it, still do the same; the store then loses the hide. Then the page's fetch drops 102 (the server no longer returns it): a poll inside two minutes of the Undo keeps it in News, and after `runFor(120_000)` a poll takes it out of For You and News |
| `flow-saved-drops-hidden-and-deleted` | PR4c: Saved holds 503 (by id only: the page's fetch keeps it out of the tabs), 105 and 103. With the clock held, hiding 105 from its card's ⋯ in Saved (`hideViaSheetHeld`) takes it out within 1 s while the hide's batch is still held (so before the store has it), and it stays out after `runFor(1000)` and a poll that no longer returns it (hide wins over saved); by-ids is never asked for it. Feed 14 (503's) is then deleted in the store (another device): after the next poll Saved shows 103 alone, though the by-id cache still holds 503 |
| `flow-saved-keeps-a-dropped-save` | PR4c: Saved holds 105 and 103, both from the tabs (no by-ids request). The page's fetch then drops 103 from the next poll, and its by-ids answer is held: Saved keeps drawing 103 meanwhile (its last copy). The answer comes back with no row (the server no longer has it): after the next poll Saved shows 105 alone, and 103 is never asked for again |
| `flow-hide-all-downvoted-undo` | PR4c, clock held as above: Settings › Voted with 102 and 103 downvoted. Hide all downvoted (its PATCH reaches the store), a poll without them, Undo, and a poll before the Undo's batch that still leaves them out. News, opened only then, shows 102 and 103, and For You, opened after it, reads as before the hide (102 back in its middle): the bulk Undo put them back as a single hide's does; the store loses both hides |
| `flow-hidden-restore-all-after-poll` | PR4c, clock held from the load (`hideAndPoll`): 101 hidden on For You, its PATCH in the store, a poll without it. Settings › Hidden, Restore all, close Settings: News (never drawn before) and For You (opened again after News) show 101 before the restore's batch goes; the store then loses the hide |
| `flow-hide-undo-until-returned` | PR4c, clock held from the load: hide 101, a poll without it, Undo, the Undo's batch goes, and a poll returns 101. The page's fetch then drops 101 (as past its category's limit): after the next poll, News and For You, each opened afresh, no longer show it, though two minutes haven't passed (the lay-over ends once a poll returned the story) |
| `flow-hide-undo-feed-gone-after`, `flow-hide-undo-feed-gone-before` | PR4c, clock held from the load: hide 101, a poll without it. 101's feed (3) is deleted in the store (another device) after the Undo reached the store (`-after`), or before the Undo (`-before`), each followed by a poll. No "Other" tab shows, right away or after the next poll: a put-back story of a gone feed is neither laid over a poll nor put back by an Undo |
| `flow-hide-undo-foryou-fails` | PR4c, clock held from the load: hide 101, a poll without it, Undo, then a poll whose `/api/foryou` answers 503. Deals and back: For You starts over with no stories (not 101 alone). With For You back, a poll still sent before the Undo's batch: Deals and back, For You shows 101 (laid over the good answer); the store then loses the hide |
| `flow-hide-two-undo-the-second` | PR4c, clock held from the load: on For You, 101 then 102 hidden with no poll between (For You's list still held the hidden 101 when 102 was hidden), a poll without either, then Undo on the toast (102's). Deals and back: For You reads 301, 102, 201, … (102 at its place, not an index off); the same after a poll sent before the Undo's batch (the lay-over); the store then loses 102's hide and keeps 101's |
| `flow-two-hides-restore-all`, `flow-two-hides-restore-rows`, `flow-two-hides-restore-rows-adjacent` | PR4c, clock held from the load (`hideAndPoll` twice): two hides on For You with a poll after each, then Settings › Hidden: Restore all (`-all`, 101 and 102), or Restore on each row, the older hide first (`-rows`, 101 then 102; `-rows-adjacent`, 102 then 301, the story just above it). Deals and back: For You reads exactly as before the hides; the store then loses both |
| `flow-hide-foryou-fails-then-undo` | PR4c, clock held from the load: hide 102, a poll without it, then a poll whose `/api/foryou` answers 503, and Undo. Deals and back: For You shows no stories (not 102 alone). With For You back, a poll still sent before the Undo's batch: For You reads as before the hide (102 laid over the good answer, at its place); the store then loses the hide |
| `flow-saved-batches-late-saves` | PR4c, PR3a: 250 saves, the newest two (105, 103) held by the tabs and 248 only by id (synthetic rows from a by-ids route in the flow, answers held). The phone reopens on Saved (stored tab), which draws 105, 103; by-ids is asked twice (200, then 48). Let go, Saved draws 30 cards, not 250; scrolled to the end, 60 |
| `flow-saved-retries-a-failed-fetch` | PR4c: saves 103 and 503 (503 kept out of the tabs). The first `by-ids` request fails (`'offline'`), and Saved shows 103; with by-ids answering again, the next poll asks for 503 once more, and Saved shows 503, 103 |
| `flow-seen-prune-keeps-saved` | PR4c, App's keep set: `seen:news` holds 1000 marks (its cap), the oldest 402 (saved and hidden, so never fetched) and 503 (fetched by id for Saved, then unsaved: only the by-id cache holds it), then 998 marks of stories nothing holds. Opening 101 adds a mark: the store's `seen:news` keeps 402 and 503 and loses 2000001, the oldest of the 998 |
| `flow-reset-keeps-saves` | PR1c: with saves, hides, votes, a saved view and News's seen marks (PR3b: `seen:news` and the legacy `read_entries:news`), last visit, prefs and section folds stored, Reset reading state deletes the reading-state rows and `history_group_by` but never `starred_entries`, `hidden_entries`, `voted_entries` or `filter_presets`; after the reload the server holds only those four and the Saved tab is back. PR2b: of a save and a seen mark (PR3b: a story opened) still queued (the clock paused inside their 1 s batch) when Reset is tapped, the save goes before the DELETEs and is stored next to 103, and the seen mark is dropped: no write for a key Reset deletes, before or after the DELETEs |
| `flow-prefs-resync` | PR2b, sync rule 5: a hide's PATCH is held; a full GET answered meanwhile (the store without it) leaves the story hidden, and so does a GET sent while it was held but answered with the old set only after the PATCH's answer was taken in; a GET sent after that is taken in as is |
| `flow-visibility-flush` | PR2b: a save inside its 1 s batch (clock paused), then the page hides: one PATCH of it goes at once with `keepalive`, and it is in the store |
| `flow-prefs-retry` | PR2b, sync rule 2: the first save's PATCH answers 503 (`prefMock(context).fail`); a second save waits for the retry, which goes 2 s later with both ids; both are stored and show saved, with no error toast |
| `flow-prefs-405` | PR2b: PATCH answers 405 (a server rolled back to before PR2a) four times: the story keeps showing saved through the retries at 2, 4 and 8 s, then one "Couldn't save" toast; nothing is stored and no timed retry follows; once the server takes PATCH again, hiding the page saves it |
| `flow-prefs-fail-save-keeps-list` | PR2b: the preferences GET fails (500) while the store holds one save (103), so no story shows as saved. A new save sends one PATCH that adds only that id, with no whole-list `starred_entries` PUT (C17). The store keeps 103, and once the GET works, the next return shows both in Saved |
| `flow-prefs-return-before-answer` | PR2b, sync rule 4: the keepalive PATCH a hide sends for a save is held (not yet at the server) across the return, then let through: the save is stored, and only that one PATCH of it is ever sent |
| `flow-prefs-landed-unread` | PR2b, sync rule 4: the hide's keepalive PATCH lands but its answer is held; another device unsaves the story meanwhile (the store is reseeded), then the page comes back and the answer arrives: one PATCH in all, the store keeps the unsave, and the story shows unsaved |
| `flow-prefs-hide-while-sending` | PR2b, leaving: a save's batch PATCH is held (sent, not answered, nothing else queued) when the page hides; the hide sends the save again with `keepalive`, which is stored and answered; the held request fails (aborted) only after the return: two PATCHes in all, the save stays stored and shown, no toast |
| `flow-tabs-reorder` | PR7a, tap-count: Edit, ↑ on News, Done (3 taps) moves News one place, with one `category_layout` PUT that stores the whole order; focus stays on "Move News up", the live line reads "News, 5 of 10" (empty before), and Done puts focus back on the pill. Edit, Move to top on the last tab, Done (3 taps) makes it second: focus goes to its ↓ (↑ is now disabled) and the line reads "<tab>, 2 of 10" |
| `flow-tabs-reorder-desktop` | PR7a at 1280: Menu → Edit tabs, Move News to top, Done: the grid's columns and Jump to column follow the new order (For You stays the row above the grid); focus goes back to ≡ |
| `flow-tab-hide` | PR7a, tap-count: Edit, Show News off, Done (3 taps): News leaves the strip and Jump to column (which lists the strip's tabs after Brief), `hidden` is stored with no order, and a News story stays in For You; Show on puts News back in its place |
| `flow-tab-hide-desktop` | PR7a at 1280, with News hidden in the store: no News column, and ArrowRight / ArrowDown walk the grid's columns one by one (Saved empty, then each category; an empty column, Saved or from PR4d CATEGORY_40's, selects nothing) and never reach News |
| `flow-new-category-at-end` | PR7a, a stored order with Video first: the strip follows it, the feed editor's category picker lists Video first, and a feed moved to a new "Hockey" category puts Hockey last in the strip and in Jump to column |
| `flow-new-category-alpha` | PR7a, no stored order: the new "Hockey" category lands in label order, right after Deals |
| `flow-saved-always` | PR7a: with nothing saved, the strip starts Brief, Saved, For You; the empty Saved says "No stories yet" without the first-feed button, and a tap stores it |
| `flow-fresh-device-opens-foryou` | PR7a: a stored order that puts News and a filled Saved ahead of For You; a new device still opens on For You and stores it |
| `flow-first-feed-empty-meta` | PR7a: only built-in feeds and no stories (route overrides): a new device opens on For You, which says "No stories yet" with a 44px "Add your first feed"; one tap opens Settings → Feeds |
| `flow-first-feed-load-fails` | PR7a: the first GET /api/sources answers 503 (only built-ins and no stories once it works): the error banner shows, and neither the empty For You nor the empty Saved offers "Add your first feed"; after a refresh brings the built-in feeds back, the button shows |
| `flow-desktop-select-follows-tab` | PR7a at 1280: a News card selected with the arrow keys stays selected when Edit tabs moves News to the front, and ArrowDown goes on down News; hiding News drops the selected card, and ArrowDown then selects in the column now at News's place (Saved, with one seeded save) |
| `flow-foryou-empty-desktop` | PR7a at 1280: with /api/foryou answering [], the For You row is still there and says "No stories yet" (no first-feed button: there are feeds), and Jump to column → For You lands on it |
| `flow-desktop-layout` | PR3a at 1280, with 25 For You stories (the row shows its top 6, PR4d, so the window is made 400px tall) and 67 more NHL stories (70): the one `data-scroller` is the wrapper around Brief, the For You row and the grid; under the For You row (taller than the window) the grid is taller than 300px and the document doesn't scroll. The NHL column renders 30 cards, then 60 once the wrapper is scrolled to 300px above its sentinel, and no more without another scroll; only the wrapper moved. PR3b: Deals's end row (in NHL's grid row), scrolled to the wrapper's bottom edge and clicked with the mouse (Chromium focuses the button on mousedown): the wrapper doesn't move and the focus doesn't go to Deals's marker far above the screen. News's end row ("N not seen · Mark as read"), scrolled to the wrapper's bottom edge and pressed with Enter from the keyboard: the News column draws no card, and the focus is on its "You're all caught up", inside the wrapper's visible box (NHL's 60 cards keep the grid row as tall as it was, so the marker would otherwise be far above the screen). Security's end row pressed the way a screen reader sending mouse events alone does (focused, then a click with detail 1 and no pointerdown): the focus is on Security's marker. The NHL header says "70 new"; ⋯ → Mark all as read leaves no card above ✓ "You're all caught up", "Earlier (70)" folded, no "N new", and the toast "Marked 70 as read" |
| `flow-auto-append` | PR3a, the phone, with 67 more NHL stories (70): the NHL tab renders 30 cards, then 60 with its list scrolled to 300px above the `[data-more]` sentinel (inside the 600px margin, still out of sight), no more without another scroll, then all 70 at the end, with the sentinel gone; scrolled to the end again, the last card is in view for the `end` shot; PR3b: the list ends with the marker, "70 not seen · Mark as read", in view with the last card for the `end` shot. PR3c: the page's clock is held (`holdClock`), so no card is read on the way and none is used up |
| `flow-scroller-rotate` | PR3a, loaded at 1280 on the NHL tab (70 stories), the phone Column's memo holding across the flip (PR3a seeded a `column_sections:nhl` row for that; since PR3b nothing but `scrollerRef` changes): only the wrapper carries `data-scroller`; resized to 390, only the phone list does, its sentinel loads 60, and a click on the shown tab puts it back at the top; resized to 1280 again, only the wrapper |
| `flow-desktop-keys-past-batch` | PR3a at 1280, with 70 NHL stories: 31 ArrowDowns in the NHL column select its 31st card; the column renders 60 (the batch grown to reach the selection) and the card is scrolled into view |
| `flow-open-marks-seen` | PR3b: opening a story (its headline) from the News tab sends one PATCH adding it to `seen:news`, the badge goes from 12 to 11, the list doesn't move and the end row reads "11 not seen · Mark as read"; a middle-click on another headline (an `auxclick`, which the harness keeps on the page and records with `middle: true`) adds it too and the badge reads 10, and so does a middle-click on story 104's Reddit-thread link (badge 9); a search result (601) goes to `seen:nhl`; a story opened with the clock held and the page hidden at once goes out in the hide's keepalive PATCH and reaches the store; on the Podcast tab a middle-click on an episode's Listen adds it to `seen:podcast`; at 1280 (`setViewportSize`) a card in the desktop For You row goes to `seen:security`. PR3c: the page's clock is held from the tap on News (`holdClock`), so the middle-click's scroll to 104 takes cards off the top unread: each open drops the badge at once, and prefSync's batch (`runFor`) then adds exactly the three opened; the search's 300 ms wait and its PATCH run on `runFor`; the story opened as the page hides is checked unmarked before its open (read and scrolled off in real time, it was already in `seen:news`, so its open sent nothing); on Podcast, with the end rule held off, the badge drops by one at the middle-click |
| `flow-badge-cap` | PR3b, the NHL tab with 103 stories: its badge reads "99+" and the tab is named "NHL, 103 new" (News "News, 12 new"); Saved, seeded with three saves no one has seen (601-603), has no badge and no name with a count, and once tapped shows its 3 cards with no marker, no next-tab row, no Earlier button or group, no dimmed card, and no ⋯ (so no Mark all); after an open the name reads 102 and the badge still "99+"; after Mark all the badge and the count in the name are gone |
| `flow-caught-up` | PR3b, the NHL tab with 40 stories, the page's clock held: flung to its end it reads "40 not seen · Mark as read", with no Earlier and no next-tab row; PR3c: once the last k cards have been on screen with the marker for the dwell (`runFor`) they are used up (the end rule) and the row reads "40−k not seen", not "You're all caught up", and the badge 40−k: the cards flung past stay unseen; two opened stay in place and the row reads 38−k; one tap on the row marks the rest (`seen:nhl` gets all 40), leaving ✓ "You're all caught up" (a tap is a finger's press: the focus doesn't go to it) and the toast "Marked 38−k as read"; Undo removes exactly those and brings them back above the marker, with "Earlier (2+k)" holding the two opened and the k read at the end; Earlier opened, then marked again from the keyboard (focus, Enter): the focus is on the marker, not `<body>`, and Earlier is folded again (the same Column, a new snapshot); then News and back, the list starts with the marker and "Earlier (40)" folded, and the badge is gone; PR3c: on Podcast, its two unseen episodes on screen with the end row, the end row focused (`focus()`): after the dwell and the seen batch the end rule has used them up and the row reads "You're all caught up", and the focus is on it (`DIV[data-caught-up]`), not `<body>` |
| `flow-next-tab` | PR3b, C8, with NHL's stories seen: under its marker one 44px row reads "Podcast · 2", named "Podcast, 2 new" (the next tab in the strip with new stories; Podcast's 3 older stories count as seen, D33) and opens Podcast, stored, in 2 taps; with Podcast hidden the row names Security; with every story seen there is no row and no badge |
| `flow-mark-all-read` | PR3b: ⋯'s first row is Mark all as read (44px); News, ⋯, Mark all (3 taps) leaves no card above the marker, "Earlier (12)" folded, no badge, the toast "Marked 12 as read" and `seen:news` holding the 12; the focus the row held goes back to ⋯ (not `<body>`); Undo removes exactly those, puts the 12 back above the marker in their order, takes Earlier away and shows no second toast; marked again from the keyboard (⋯ and the row, each focused and Enter), the focus is on ⋯ again, and the row is now disabled |
| `flow-new-arrival-appends` | PR3b, the NHL tab with 40 stories, all but 601 and 602 seen (the page's clock held from the tap on the tab, PR3c: the two sit on screen with the marker): a story that arrives with a poll (`withArrivals`) goes after them, at the end of the unseen ones, and the badge reads 3; with Earlier open and the list scrolled so the marker is 300px above the screen (`markerAbove`: the app's observer has seen it there), the next arrival waits (counted: the badge reads 4) and the card being read doesn't move; scrolled back to the top, it goes in at the end. Then, reloaded with every story seen (NHL caught up, no next-tab row), Earlier open and the marker 300px above the screen (`markerAbove` again, at each such scroll) with `overflow-anchor: none` on the list (as on Safari): a News story arrives (News badge 1) and the Earlier card being read stays put with no next-tab row coming in; at the top the row reads "News · 1"; marker above the screen again, an NHL story arrives (badge 1): it waits, the card being read stays put and the next-tab row stays; at the top the arrival goes in, the end row reads "1 not seen · Mark as read" and the next-tab row is gone |
| `flow-arrival-batch-edge` | PR3b, the NHL tab with 33 stories, the three oldest seen (the page's clock held from the tap on the tab, PR3c): it draws 30 unseen cards and the end row at once; with Earlier (3) open and the marker on screen, a MutationObserver on the list records any moment the marker or the Earlier group is missing; one arrival is drawn 31st at once, nothing was missing, the end row reads "31 not seen · Mark as read" and no "load more" sentinel came back |
| `flow-earlier-keys-fold` | PR3b at 1280, `seen:nhl` 602 and 603: ArrowRight to the NHL column and two ArrowDowns select Earlier's first card and open it; `r` ("Nothing new", a new snapshot) folds it (`aria-expanded="false"`, no card of it drawn) and drops the selection, so the next ArrowDown selects the column's first card (601) with Earlier still folded; ArrowDown into Earlier opens it again, and a click on "Earlier (2)" folds it and drops the selection too: an `h` sent in the same page task as the click (a microtask after it, so only a drop made in React's flush of the click, from a layout effect, is in time), then `b` and `s`, send no interaction, show no toast and leave "Earlier (2)" and its cards' summaries as they were; Enter on the focused Earlier button opens it, and the next ArrowDown selects 601. (Folded, Earlier draws none of its cards, so the flow reads the dropped selection from what the keys do, not from the page.) |
| `flow-return-after-10-min` | PR3b, C1, with the clock installed: a story opened more than 2 s before leaving, then back after 9 min (`away`): nothing moves; a News story arrives while away (`withArrivals`) and the app is back after 11 min: the arrival is first, the opened story is under "Earlier (1)", folded, and the list is at its top; a story opened (in view) and the page left at once, back after 11 min: the list and its scroll position stay as they were; back after 11 min again with the return's entries request held (a `page.route` gate), the tab starts over at its top, the top story is opened with a dispatched middle-click alone (an `auxclick`, button 1: no pointer, touch, click, key or wheel event, so only the open guards can keep it) before the request is let go, and that story is still first, Earlier reads as before and the story the late refresh brought is last; once more, with the list scrolled to 300 (no touch) and its scroll event sent (`scrolledTo`) before the request is let go: the list is the reset one plus the late story at its end, still at 300; once more, with a finger resting on the top card (`restFinger`: a CDP `touchStart`, then a `touchCancel`, so no tap and no scroll) when the request is let go: the list is the reset one plus the late story at its end, still at its top, Earlier as it was; then, one slow return each, with "Earlier (N)" opened by `dispatchEvent('click')` alone (no pointer, touch or key, as a desktop screen reader in Firefox sends), with a bare Shift key pressed (the focus on the page), and with the wheel turned up over the list at its top (a wheel event that scrolls nothing): each time the list is the reset one plus the late story at its end, still at its top, and the opened Earlier stays open. The first 11-minute return's request is held by the same gate until the return's own jump to the top (from 400) has sent its scroll event, which must not call the second reset off; each slow return leaves with the list at 200, so being at 0 on the way back shows the tab started over. PR3c: the cards the scroll to 400 takes off the top were read for the 3 s before it, so they are used up: the first 11-minute return expects them under Earlier with the opened story (none at 390×844); the later steps compare with the list each reset drew; last, the clock held from the tap on NHL (its three stories and its end row on one screen) and the tab left at once: back after 11 min with the return's request held, the dwell and the seen batch run (no input) and the end rule makes the row "You're all caught up"; when the request is let go the three stay above the marker and there is no Earlier (a story used up on screen calls the second reset off) |
| `flow-return-from-earlier` | PR3b, C1, with the clock installed, the NHL tab with 40 stories, six unseen: with Earlier open and the marker 600px above the screen (the hold on: `markerAbove` waits until the app's observer has seen it there), a story arrives while away and the app is back after 11 min: the tab starts over at its top with seven unseen and the marker below the screen; after two opens the badge and the end row both read 5; a poll's arrival goes in at once at the end (the hold was let go although the marker never crossed the screen's edge) and the end row reads 6; scrolled to, the end row stays where it was put. PR3c: the page's clock is held from the tap on the tab (`holdClock`): the six unseen cards sit on screen with the marker at first, and the last ones with it again at the end |
| `flow-scroll-marks-seen` | PR3c, the NHL tab with 40 stories, the page's clock held: opened with a tap, the Menu opened through the dwell and closed, the top card scrolled off at once is not marked (the dwell didn't count under the Menu, and the closed Menu, mounted with `aria-modal`, pauses nothing after); after the dwell (`runFor`), the cards at least half on screen scrolled off the top leave the badge at 40 until the 300 ms seen batch runs (a real-time wait first: a mark written at once would be drawn by then), then the badge drops by as many and, after the 1 s PATCH batch, they go out in one `seen:nhl` PATCH with exactly their ids and the order doesn't change; in a fling (five 350px steps, no time) the cards on screen when it starts (read for a dwell) are added and the ones first shown during it are not; a card seen on scroll just before ⋯ → Mark all is written first: the toast counts the rest, and Undo removes only those |
| `flow-view-on-seen` | PR3c, the clock held: the NHL tab drawn and on screen through the dwell and the interaction batch sends no `view`; its top card opened where it is sends its `click` and one `view`; then the next card, scrolled off the top with it, sends one `view`, and the opened card no second one (one per story used up, whichever way) |
| `flow-seen-tab-switch` | PR3c, the clock held, News lengthened with 20 stories: For You's top story (101) read and scrolled off the top, then the News tab tapped before the 300 ms seen batch can run: the tab opens with 101 under the marker, not above it, and `seen:news` adds it once; the same for a News card read off the top just before a Refresh lands and just before ⋯ → Best (a snapshot started while the seen batch still holds a story places it as seen; each step reads a story no earlier step used); all three are in Earlier |
| `flow-seen-reset-in-place` | PR3c, the NHL tab with 40 stories, the clock held: three screens read and scrolled off the top (marked), the next screen read for a dwell, then the header Refresh: no `seen:` mark for the cards that were being read, the list is at its top, the stories read off the top are under the marker and the cards being read lead the unseen ones, on screen; then two more screens read, the next one read for a dwell, and ⋯ → Best: no mark for those cards and the list at its top (a tab that starts over where it is starts at its top) |
| `flow-seen-turn-mid-batch` | PR3c, the clock held: For You's top story read and scrolled off its top, then the viewport turned to 844×390 before the 300 ms seen batch runs (the grid's columns start their snapshots in that render): the story is not drawn above its category column's marker, it is in that column's Earlier once opened, and `seen:<category>` adds it once |
| `flow-seen-flush-on-hide` | PR3c, the clock held: a card read and scrolled off the top, then the page hidden at once (the 300 ms batch can't run): the hide's keepalive PATCH of `seen:nhl` carries it, and only it, to the store; then new cards on screen, the page hidden 60% into their dwell and back at once (iOS fires a frozen timer late): half a dwell after the return they don't count when scrolled off, and the next cards, read for a full dwell, do |
| `flow-saved-open-only` | PR3c: Saved with 12 saves, the clock held: read and scrolled through to its end with the dwell between scrolls, it sends no `seen:` PATCH and no `view` |
| `flow-landscape-no-marks` | PR3c, the clock held: the NHL tab read, then turned to 844×390 (past 768px, the desktop layout): with the wrapper scrolled and the dwell and every batch run, no `seen:` PATCH; back at 390 the NHL cards are the same elements (nothing remounted), and two read and scrolled off the top send exactly those two |
| `flow-fv-card-source-tap` | the card's feed button opens that feed's editor in Settings (`?source=`). PR5a: it sits on the header line above the headline and must not reach the headline's top edge |
| `flow-card-votes` | PR5a, D2: ▲ on a News card turns orange and `aria-pressed`, sends one `thumb_up`, shows no toast and moves nothing; ▲ again turns it off (one `thumb_down`); ▼ turns blue (one `thumb_down`); ▲ then switches (two `thumb_up`); the card stays and the store holds the vote |
| `flow-card-save` | PR5a, D2: the bookmark (`save`) fills, sends one `bookmark` and no toast; once its PATCH has gone, `unsave` empties it, sends no `bookmark`, shows "Removed from Saved · Undo" and its PATCH removes the id from the store; Undo saves it again (an add) with no second `bookmark`; `starred_entries` saw add, remove, add |
| `flow-card-share` | PR5a: Share on story 103 calls the `navigator.share` stub with the story's title and its own address (not old.reddit.com's) and sends one `share`; a share whose promise rejects (the sheet dismissed, AbortError) sends no `share`, copies nothing and shows no toast; with `navigator.share` gone it copies the address, shows "Link copied" and sends no `share`; a story with a `javascript:` address (added with `withStories`) has Share disabled, and its sheet's Copy link and Open in browser are disabled too |
| `flow-card-sheet` | PR5a: a News card's ⋯ opens `[role="dialog"][aria-label="story actions"]` with four 44px rows, Hide, Edit feed, Copy link and Open in browser, none disabled, and a 44px Cancel; focus starts on Cancel, Tab wraps to Hide and Shift+Tab back; Esc closes it and puts the focus on that card's ⋯; a tap outside closes it; Edit feed opens that feed in Settings (`?source=`) and the focus doesn't go back to the ⋯ under it; from a search result (601) Esc keeps the query and the results, and Hide on that only result puts the focus on the search field (`#app-search`); Hide on a News card takes it away and the focus lands on the next card's headline |
| `flow-hide-undo` | PR5a, D25, the clock installed: ⋯ → Hide takes the first News card away with a "Hidden · Undo" toast and (after the 1 s batch) a PATCH adding it to `hidden_entries`; Undo puts it back in its place with a PATCH removing it, and 6 s later no `never` has gone; a second card hidden and the toast left to expire (5 s) sends exactly one `never`, after the toast and not before, and the story is in the store |
| `flow-hide-keys-desktop` | PR5a, 1280, the clock installed: `h` on the keyboard-selected card hides it with one "Hidden · Undo" toast and drops the selection; `h`, `b` and `a` again act on nothing (the one toast stays, no `bookmark`, `thumb_up`, `starred_entries` or `voted_entries` write, one `hidden_entries` add); after the toast exactly one `never`; the next card hidden with `h` twice and taken back with Undo sends no `never` and stays unselected; a held `b` (keydown and its repeat) saves once and a held `a` votes up once |
| `flow-swipe-left-hide` | PR5a (CDP touches, Chromium only): a 160px drag left across a News card's headline hides it, with the "Hidden · Undo" toast and no open; Undo puts it back in its place |
| `flow-swipe-right-dead` | PR5a (CDP touches, Chromium only): a 160px drag right across a card's headline never moves it (no `transform` written during the drag), hides nothing, shows no toast, writes nothing and opens nothing |
| `flow-card-preview` | PR5a, D34: For You's story 101 shows its 300-character blurb inside the headline link, `-webkit-line-clamp: 2` and no taller than two lines, cut; News's 103 (the server sent `preview: null`) shows none; the Summary panel takes the preview's place while open and it comes back once closed; a tap on the blurb opens the story (the harness's `window.open` log) and sends the PATCH adding 101 to `seen:news` |
| `flow-window-default` | PR4d, D5: the first load asks `/api/entries?per_category_limit=100&max_age_hours=48&per_source_cap=25&active_only=true` and `/api/foryou?limit=50&max_age_hours=48`; Podcast shows 401 and 402 (403 and 404 are the paused feed 12's, 405 is 4 days old), Video 501 to 504 (505 and 506 are older), Security none of the paused 10's; the ⋯ offers Show stories from with 2 days chosen and no "Hide if older than" select; Saved has no ⋯ |
| `flow-busy-feed-no-crowding` | PR4d, D5, D28, the owner's bug (the clock held): with the store holding (`dbMock`) 30 hn_top stories in the last 2 days scoring 99 to 90.3 (7001-7030), 90 older ones scoring 89.9 to 81 (7101-7190) and a new News feed, bnn_bloomberg, with 3 stories in the last half hour scoring 8 to 12 (7201-7203), News read to its end shows bnn's three, every other News feed's 2-day stories, and exactly hn_top's 25 best of the 2 days (7001-7025), nothing else. With `refresh` back to `per_category_limit=100` alone (cut by score) bnn and the other feeds are out and 30 of hn_top's show |
| `flow-tab-window-7d` | PR4d, the clock held, an old device (`oldDevice`): ⋯ → 7 days on Nhl sends one `GET /api/entries?category=nhl&limit=200&max_age_hours=168&per_source_cap=25&active_only=true`, shows 601, 602, 603, 604 (604 is 3 days old, 605 ten), marks 7 days chosen and writes `column_prefs:nhl` as `{sort, minScore, window: '7d', maxAgeHours: null}` (null, so an older build still open keeps the window when it writes the row); away on News a poll fetches nothing for Nhl; back on Nhl the rows draw at once in that order and one more fetch goes; a reload with the window stored opens Nhl in the same order from one fetch, with the list at its top |
| `flow-tab-window-in-order` | PR4d, the clock held, an old device: Nhl on Best (601, 602, 603); ⋯ → 7 days with the answer held while 602 is marked seen in the store and a poll's resync brings the mark (a poll never starts a snapshot over: 602 stays above the marker, and no Earlier row); let go, the list untouched, the first answer starts the snapshot over: 601, 604, 603, with the seen 602 under Earlier (without the new snapshot it would read 601, 602, 604, 603); ⋯ → All with its answer held while 601 is opened (the list is touched: no new snapshot) shows 601, 605, 604, 603: the fetched 605 goes in at its place by the tab's sort (`partitionTab`'s `inOrder`), not at the end like an arrival, and the read 601 stays where it is |
| `flow-foryou-window-rerank` | PR4d, the clock held, an old device: For You at 7 days, its 7-day answers routed: the first ranks 101, 301, 102, 201, 401, 104, 303, 502, 604, 505 (the snapshot starts over); the next poll's swaps the top two and brings 506 at rank 9, which goes in between 502 and 604 (its rank among the stories on screen), not second; the stories on screen keep their places |
| `flow-badge-in-window` | PR4d, the clock held, an old device: Video's badge is 4 at 2 days (501 to 504), 2 at 1 day (501, 502), 6 at 7 days (505 and 506 fetched for the tab); off screen on News, the 7-day Video's badge reads 4, the 2-day data |
| `flow-paused-hidden` | PR4d: the paused krebs_on_security (10) is in no tab: For You has no 304 (`F.FOR_YOU_RANKED`, the mock's list, holds it, as the ranking would before PR4b's active filter), Security neither 304 nor 305 |
| `flow-paused-hidden-desktop` | PR4d at 1280: the For You row shows its top 6, without 304; the Security column has neither 304 nor 305 |
| `flow-quiet-tab-kept` | PR4d, D5, an old device: with the page's fetch dropping 601 to 603 (`keepOutOfTabs`), Nhl keeps its tab (its only feed is active) and reads "Nothing in 2 days · Show 7 days"; ⋯ → 7 days shows 604 within 3 taps; back at 2 days the line is back and its "Show 7 days" shows 604 again; the feed editor's category picker still offers nhl; with nhl_news paused in the store and a reload, Nhl is no tab |
| `flow-quiet-tab-pending` | PR4d, D5, an old device, 601 to 603 kept out: Nhl's "Show 7 days" sends the 7-day request, held: no `[data-window-empty]` line and no "Nothing in 7 days" while it is on its way, nor once it answers 503 (the banner shows); the next poll's fetch answers and 604 shows |
| `flow-filter-window` | PR4d, the clock held, an old device: the Menu filter on darknet_diaries asks `GET /api/entries?source=darknet_diaries&limit=200&max_age_hours=48&per_source_cap=25&active_only=true` and shows 401 and 402 (405 is 4 days old); ⋯ → 7 days asks the same at `max_age_hours=168` and 405 joins |
| `flow-refresh-resets` | PR3b: on News with a story opened, a poll that brings a new story puts it at the end and shows no toast; Refresh (the header button) starts the tab over when its stories land: the new stories first, the opened one under "Earlier (1)", and the toast "1 new"; with "Earlier (1)" opened, Refresh again says "Nothing new" and Earlier is folded again (`aria-expanded="false"`, no card of it drawn); at the top with all 13 unseen cards drawn, a Refresh that brings 20 ("20 new") draws the first 30 and the "load more" sentinel, not all 33; on NHL (61 stories, every one unseen) scrolled to its end once, so 60 are drawn, with 601 opened, a Refresh ("Nothing new") starts the tab over with 60 unseen, which the old limit already reaches, and still draws its first 30 and the sentinel. PR3c: the page's clock is held from the tap on News (`holdClock`): opening Earlier scrolls the list to its end, where the last cards sit with the marker, and the NHL part scrolls read cards off the top, so in real time the end rule and scrolling past used stories up; the held "1 new" toast covers the middle of Earlier, so that tap goes to its left end |
| `flow-filter-one-feed-status` | PR7a: the Menu filter on one feed (cisa_kev) with no stories shows that feed's slice-19 status ("last fetch failed: HTTP 503…"), not "No stories yet" |
| `flow-menu-closed-inert` | closing the Menu puts focus back on ≡, and Tab never reaches the closed Menu |
| `flow-h1-taps` | the harness: ≡ then Settings counts 2 taps, passes `taps(page, 2, …)` and fails `taps(page, 1, …)` |
| `flow-h1-touch-drag` | the harness: `touchDrag` does nothing off Chromium; on Chromium an upward drag scrolls the phone list and counts 1 |
| `flow-h1-set-hidden` | the harness: `setHidden` sets `visibilityState` and `hidden`; coming back runs the app's quiet poll |
| `flow-h1-keepalive` | the harness: the fetch log shows the load-time `GET /api/preferences` without keepalive, and the views still queued when the page hides (beacon refused) sent with it. Since PR3c a view is queued when a card is seen on scroll: real time, twice the dwell, then the top card scrolled off the top |
| `flow-h1-pref-store` | the mock: a preference PUT reads back in the list, with `since` and by key; DELETE removes it; PATCH applies `remove` before `add`, `set` with null clears a vote, takes `seen:` keys with a `/`, answers 400 for a key that isn't a set key and 422 for the wrong change or a body the server's schema refuses (an id of 0 or a string, an unknown field, a vote other than up / down / null, a non-canonical vote id); a queued fault answers once; a reload loads the rows with one GET and still sees them; a PUT of a set key (`hidden_entries`, `seen:News/Tech`) is a 409 with the server's detail, leaves the stored row as it was, creates none, and is still reported through `onSetKeyPut` (PR2c; sent from a second context with the same mock, so C17 doesn't fail this flow) |
| `flow-h1-desktop` | the harness at 1280: a mouse desktop with the desktop grid; a card's right-click Copy link and Open in browser (PR5a) land in the clipboard and `window.open` stubs |
| `flow-reload-on-new-build` | PR1r, the phone, with a new build served (`newBuild`): back after 9 min nothing is checked; back after 11 min it is not checked when the page hid right after opening a story; a key pressed during the check, a 404 for the new entry script, the Menu being open, or text in the search field stops the reload; a check cut off by a hide doesn't keep the next return from checking; a save (PATCH) refused with 400 is not retried or resent and the next return reloads, and so does a pending save that hiding sends and the server refuses with 409 (each on its own build); a pending save that hiding sends and the server can't take (503), sent again on the return and by the flush, keeps the page, and so does the same save failing again on the next hide and return; once the server takes it (the save sent by the hide still fails), the return sends it and reloads only once it is saved (each hide waits for the answer: one still unanswered on the return is given up on, not sent again, sync rule 4); a pending save that every PATCH answers with 405 (a server rolled back to before PATCH) is sent again on the return and by the flush, and the page still reloads; the reloaded page checks again but doesn't reload for the same build |
| `flow-reload-desktop-button` | PR1r at 1280: back after 11 min with a new build and no input, the desktop shows "New version · Reload" and doesn't reload by itself; back with its own build served, the button goes; back with a new build again, a scroll and a key during the check don't stop the button; clicking it reloads, and the button is gone after |

Run one with `--only <name>`; PR6 and PR8 add the PATCH and PUT counting flows.

### Helpers (H1)

Every flow made with `flow()` (and so `dashFlow`, `editorFlow`, `aiFlow`)
gets `installHarness(page)` before the page loads: `window.open`,
`navigator.share` and `navigator.clipboard` are stubs that record their
calls, a click on a link that opens a new tab (a card's headline) is
recorded in `opened` and goes nowhere (PR3b; the app's own click handlers
still run), and so is a middle-click on one (an `auxclick`, recorded with
`middle: true`), and fetches to `/api/preferences` and `/api/interactions` are recorded
with their `keepalive` flag, which Playwright's Request does not expose, and
`answered`, true once the app has read the response body (or the request
failed). `harnessLog(page)` returns `{ fetches: [{ method, path, keepalive,
answered }], opened, shared, copied }`, and `answered(page, method, path)`
says whether the latest such request has been taken in: flows wait for it
before moving the clock or the page's visibility, so a retry timed from an
answer, or a return to the front, doesn't race the answer. `sendBeacon` is
not stubbed.

| Helper | What it does |
|---|---|
| `tap(page, locator, opts)` | `locator.tap(opts)`, and one more in `page._taps` (0 when `run` starts) |
| `taps(page, max, id)` | fails the flow under rule `id` when more than `max` taps were used; returns false then |
| `touchDrag(page, { from, to, steps = 10, stepMs = 50, holdMs = 150 })` | one finger from `from` to `to` (`{ x, y }`, CSS px) through CDP `Input.dispatchTouchEvent`, after two animation frames and 300 ms; holds `holdMs` before lifting; counts as one tap. Chromium only: elsewhere it returns false and does nothing |
| `restFinger(page, locator)` | PR3b: one finger put down near the top of `locator` through a CDP `touchStart` and left there (a `pointerdown` and a `touchstart`, no tap, no scroll); the function it returns lifts it with a `touchCancel`, so no click follows. Not a tap (not counted). Chromium only: elsewhere a held Shift key (a `keydown`) stands in |
| `setHidden(page, hidden)` | sets `document.visibilityState` and `document.hidden`, then fires `visibilitychange` |
| `installClock(page, time = F.NOW)` | `page.clock.install`; `touchDrag` then waits in Node instead of on animation frames |
| `putPrefs(page, rows)` | seeds the mock's preference store (`prefMock`) with `rows`, then reloads |
| `prefWrites(page)` | every PUT, PATCH and DELETE to `/api/preferences/<key>` from then on, in order, as `{ method, key, value, body }` |
| `withArrivals(page)` | PR3b: stories pushed onto `window.__arrivals` (in the page) are added to the tab story lists the next poll loads; `arrivalNhl(id)` makes one NHL story from a minute ago |
| `markerAbove(list)` | PR3b: after a scroll that puts the marker above the top of `list` (a `[data-scroller]`), resolves true once the app's own marker observer has seen it there (the hold is on): an IntersectionObserver made then, on the same root, waits for its first report of the marker above the screen; the app's observer saw the crossing in the same rendering update or an earlier one, so its callback runs in the same notification task or an earlier one and nothing sent next overtakes it. False after 5 s |
| `scrolledTo(list, y)` | PR3b: scrolls `list` to `y` and resolves true once its scroll event has been sent (the app's capture listener on the document hears it first); false after 3 s |
| `groupIds(page, 'unseen' \| 'earlier')`, `markerText`, `badge(page, re)`, `textNow` | PR3b: the phone list's card ids above the marker or under Earlier (`[data-group]`), the end row's text, a tab's badge, and any element's text read at once ('' when it isn't there; a locator would wait 30 s for it) |
| `holdClock(page)` | PR3c: after `installClock`, pauses the page's clock a second ahead (`pauseAt`; a smaller step can land in its past): no timer runs until `runFor`. Hold before the tap that opens the tab under test |
| `holdClockFromLoad(page)` | PR4c (rebased onto PR3c): `installClock` and a pause before the page loads, for a flow whose tab under test is the one the page opens on (the hide flows, on For You): no card has a second on screen before the flow's first step, and only its `runFor`s give the cards time. `holdClock` pauses a second ahead, which is the dwell for a card on screen since the load. Called in `before` in place of `installClock` |
| `observersSettled(page)` | PR3c: resolves once every IntersectionObserver has reported the page as it is (a new observer's first report comes in the same task as every older one's); needs no animation frame, so it works with the clock held |
| `listCards(page)`, `scrollListBy(page, px)`, `scrollCardToTop(page, id)` | PR3c: the phone list's unseen cards at least half on screen (`half`) and entirely above its top (`above`); scroll the list (`scrollTop`) and wait for the observers |
| `dwell(page)`, `writeSeen(page)` | PR3c: `runFor(SEEN_DWELL_MS + 100)` (the cards on screen qualify); `runFor` the 300 ms seen batch, then prefSync's 1 s PATCH batch, then `requestsSettled`. `SEEN_DWELL_MS` and `SEEN_BATCH_MS` are read from `src/lib/seen.ts`, so a Gate 1 retune is one edit there |
| `requestsSettled(page)` | PR3c: waits until every `/api/preferences` and `/api/interactions` request in the harness log is answered, so a request a timer just sent has reached the flow's routes; a check that nothing was written or sent comes after it |
| `phoneIds(page)` | PR4c: the ids of the cards the phone draws now, in order, joined by commas |
| `keepOutOfTabs(page, ids)`, `setKeptOut(page, ids)` | PR4c: the page's fetch drops `ids` from `/api/entries` and `/api/foryou` (only by-ids brings them); `setKeptOut` changes the ids from then on, as a poll that stops returning a story |
| `recordByIds(page)`, `holdByIds(page)`, `byIdsAsked(page, id)` | PR4c: every `GET /api/entries/by-ids` `ids` in `page._byIds`, each answered as `page._byIdsAnswer` says (`'mock'`, `'offline'`: the request fails, `'gone'`: no rows); `holdByIds` holds the answers until the function it returns is called |
| `pollDone(page)` | PR4c: `startPoll`, then the bodies of its `/api/entries` and `/api/foryou` as the mock sent them, once the app has taken them in (the phone's refresh button is enabled again); a failed For You gives `forYou: []` and `forYouOk: false`; null when it never finished. `pollHas(got, ids)` says whether they hold any of `ids` |
| `openedIds(page, re)` | PR4c: taps the phone tab `re` and returns the ids it draws once it is selected and has drawn its cards or its "No stories yet" |
| `hideAndPoll(page, context, id, n)`, `tapUndo(page)` | PR4c: with the clock held from the load (`holdClockFromLoad` in `before`), hides `n` on For You (⋯ → Hide since PR5a, `hideViaSheetHeld`), sends the hide's batch (`runFor(1000)`), waits for the store, checks the badge dropped by one and no more (a card read for a dwell and scrolled off the top by the tap would be marked seen, PR3c, and leave the order the flows read afresh), and polls, checking the poll left `n` out; false after a failure under `id`. `tapUndo` taps the toast's Undo |
| `hideViaSheetHeld(page, card)` | PR5a: `hideViaSheet` under a held clock (PR4c's hide flows, `flow-hide-undo`). The sheet starts its slide-up a frame after it mounts and the held clock holds `requestAnimationFrame`, so it steps the clock 16 ms at a time (at most 10) until the sheet is up, then taps Hide. The frames are far short of a dwell and seen on scroll is paused under the sheet; the sheet's focus move after Hide waits for the flow's next `runFor` |
| `oldDevice(page)` | PR4d: `popping.v1.firstRunAt` a month before `F.NOW`, so D33's baseline (stories fetched more than 48 h before the first load count as seen) leaves the fixture's 3- and 10-day-old stories unseen, as a story that arrived since the deploy is on a phone |
| `dbMock(context)` (capture.cjs) | PR4d: the mock's feeds and stories: `addSources(rows)`, `addEntries(rows)` (rows as `F.SOURCES` and `F.ENTRIES` hold them; `F.mkEntry(id, source_id, title, extra)` builds a story), in a flow's `before`, so the server's cuts (window, per-feed cap, per-category limit) apply to them |
| `prefMock(context)` (capture.cjs) | the mock's preference store: `seed(rows)`, `value(key)`, `keys()`, `fail(status, { method = 'PATCH', key, times = 1 })` (the next requests answer `status` and change nothing), `faultsLeft()` |

Tap-count flows prove "at most N taps". They start from their URL and reach
the target only with `tap`, `touchDrag` (a swipe is one tap) and `fill`
(typing is free): no `evaluate`, `pushState`, `.click()`, `.check()`,
`selectOption` or `startPoll`. They assert outcomes (a request sent or not, a
card hidden, a transform, `defaultPrevented`), never exact scroll offsets.
`tap` needs a touch context, so it is for the phone widths.

Chromium doesn't deliver page touch moves under about 40px, so rules about
small movements are unit-tested in `scripts/unit/`. Moves closer than about
50 ms apart fling to different offsets from run to run, hence the defaults.
Flows about seen-on-scroll and IntersectionObserver drive the scroller's
`scrollTop` or `scrollBy`; touch drags are for gestures.

Time: flows that assert dwell, batch, toast, poll or arrival timing call
`installClock(page)` in `before` and move time with `page.clock.runFor`
(`flow-fn-quiet-poll`). A timer set during load can fire hundreds of
milliseconds late in real time, so real pauses have no margin over a short
delay. `runFor` also fires the regular visible poll, so GET counts allow for
it. Where real time is kept (for example "a fling marks nothing"), pause at
least twice the delay under test.

Seen on scroll (PR3c): on the phone a card read on screen (half of it, for
`SEEN_DWELL_MS`) and scrolled off the top is used up, and so are the cards on
screen with the marker once it has been there as long. A flow or state that
counts unseen stories while cards or the marker are on screen holds the
page's clock (`holdClock`) before it opens the tab, and moves it only where
it means to (`dwell`, `writeSeen`). Scrolls in such flows set `scrollTop` and
wait with `observersSettled`, not with a sleep.

Time away (PR1r): `away(page, minutes)` hides the page, moves the page's
`Date.now` on with `page.clock.setSystemTime` (no timers fire) and shows it
again. `newBuild(page)` makes the update check's fetches of `/` (fetch-type
requests only, so page loads still get the real build) return `index.html`
with its entry script renamed to `page._nextScript` (`index-next*.js`; null
serves the page's own build), answers that script's HEAD (a 404 when its
name says `gone`), and counts the checks, the HEADs and the reloads.
`page._hold` holds those fetches (each is counted before it waits); one the
page aborted meanwhile is dropped. Under `vite preview` the live and served ids match, so no other
flow sees a reload. A flow that makes the page reload waits for the reloaded
page before it ends: closing the context while the reload is still being
fetched throws in smoke's CSP route.

### The mutation check

Every new flow, and every new behavioural pytest, is shown to catch what it
guards:

1. Name the one product line it guards.
2. Break that line as a scratch edit.
3. `npm run build`, then `smoke.cjs <base> --only <flow>` (or the single
   pytest).
4. Confirm it fails on the expected rule or assertion.
5. Restore with `git checkout -- <file>`, then `git diff --exit-code`.
6. Record "flow → broken line → failing rule" in the unit's notes,
   `docs/ux-notes/<unit>.md`.

Gesture and timing flows also pass 5 runs in a row before they merge.

## capture.cjs, probe.cjs, run.sh

The original audit:

    bash frontend/scripts/iphone-audit/run.sh

builds, serves on 127.0.0.1:4173 and writes next to the scripts:
`<width>-<nn>-<state>.png` screenshots, `<width>-overflow-scan.json`
(flagged elements per state with computed styles and rects), `summary.json`,
`analysis.txt`, `probe-output.json`. These outputs are git-ignored.

`mockApi(context, { onUnmocked })` matches on the request's pathname
(`/api/`, `/auth/`, `/assets/`), keeps its own deep copy of the sources and
entries per call (POST, PATCH and DELETE change that copy; GET reads it), and
calls `onUnmocked({ method, path, url })` for anything it does not answer.
Preferences are a per-call store too, empty at the start: `PUT
/api/preferences/<key>` stores `{ key, value, updated_at }` and `DELETE`
removes it; `PATCH` changes a set key as the server does (400 for any other
key, 422 for `set` on an id list or `add` / `remove` on `voted_entries`
or for a body `schemas.UserPreferencePatchIn` refuses,
`remove` before `add`, `null` clears a vote, trimmed to the cap smallest id
first); `GET /api/preferences` lists the rows (only those updated after
`since` when it is given) and `GET /api/preferences/<key>` returns one or 404.
`updated_at` is `F.NOW` plus one millisecond per write. Any other method on a
preference key is unmocked (C8). A PUT of a set key (`starred_entries`,
`hidden_entries`, `voted_entries`, `read_entries:*`, `seen:*`) is reported
through `onSetKeyPut({ key })` (smoke rule C17) and answered 409 "Popping was
updated. Reload to keep saving." without storing anything, as on the server
(`SET_KEYS`, PR2c). `prefMock(context)` gives
flows the store: seed rows before the page loads, read them, and queue
faults (see Helpers). Answering `GET /api/preferences` with a fixed body
from a flow would bring it back on every poll and every return to the
front, which re-read the preferences since PR2b; seed the store instead.
As the server since PR4b, `GET /api/entries` (without `q`) and
`GET /api/foryou` leave out the stories the store's `hidden_entries`
holds. `GET /api/entries/by-ids` answers like the real route (PR4c): at
most 200 ids, in the order asked, ids with no row or whose feed is gone
dropped, and each row the card projection (`_CARD_COLUMNS`, never `meta`)
plus `source_name`. `GET /api/entries` honours `max_age_hours` (stories
fetched since then, against `F.NOW`, the page's clock), `per_source_cap`
(each feed's best-scoring stories in the window, by score, `fetched_at`
and id; 422 without `max_age_hours`, as on the server) and `active_only`
(no paused feed's stories), then cuts and orders as `list_entries` does:
the dashboard load (`per_category_limit` with no `category`, `source` or
`q`) keeps each category's first `per_category_limit`, by arrival
(`fetched_at`, `published_at`, id, newest first) with a window and by
score without one, and ignores `limit`; any other request is the flat
cut, `limit` (50 when unset, at most 50 for a search) rows by arrival
with a window, by `published_at` for a search, else by score. Rows a
flow adds on the page's side (`withStories`, `withArrivals`) skip these
cuts; `dbMock(context)` adds them to the store instead. `GET /api/foryou` serves `F.FOR_YOU_RANKED` (the
ranking's order, the paused feed's 304 included) minus the paused feeds'
stories (PR4b's filter) and honours `max_age_hours`; `F.FOR_YOU` is what
For You shows. So Podcast's
403 and 404 (paused feed 12) and 405 (4 days old), and Video's 505 and
506 (older than 2 days), are in no default tab, and 604 (3 days) and 605
(10 days) join Nhl only at 7 days and All.

`capture.cjs` and `probe.cjs` open Settings the way a person does since PR5:
the Menu (`open menu`), then its Settings row.

## Matching CI's fonts locally

GitHub's ubuntu runner renders `system-ui` in DejaVu Sans, which is wider
than the Inter this container defaults to (and wider than SF Pro on an
iPhone). A label that fits locally can fail C14 in CI. To measure with
CI's fonts, run the guard with the bundled fontconfig file:

```bash
FONTCONFIG_FILE=$PWD/scripts/iphone-audit/fonts-ci.conf \
  PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
  node scripts/iphone-audit/smoke.cjs http://127.0.0.1:4173 --csp
```

`fc-match system-ui` with that variable set should print DejaVu Sans.
Run it this way before every push.

## Local loop

What a session runs before pushing. CI stays authoritative, and WebKit
results come only from CI. The database is a throwaway test cluster, not an
app process.

Backend, from the repository root:

    apt-get update && apt-get install -y postgresql-16-pgvector   # Ubuntu noble universe
    pg_ctlcluster 16 main start
    su postgres -c "psql -c \"CREATE ROLE popping_test LOGIN SUPERUSER PASSWORD 'popping_test'\""
    su postgres -c "createdb -O popping_test popping_test"
    PGPASSWORD=popping_test psql -h localhost -U popping_test -d popping_test \
      -c 'CREATE EXTENSION IF NOT EXISTS vector'
    python3.12 -m venv /path/outside/the/repo/venv && . /path/outside/the/repo/venv/bin/activate
    TORCH_VERSION=$(sed -n 's/^ARG TORCH_VERSION=//p' backend/Dockerfile)
    pip install --index-url https://download.pytorch.org/whl/cpu "torch==${TORCH_VERSION}"
    pip install --no-deps -r backend/requirements-dev.lock
    pip install --no-deps -e backend
    cd backend && POSTGRES_HOST=localhost POSTGRES_PORT=5432 POSTGRES_USER=popping_test \
      POSTGRES_PASSWORD=popping_test POSTGRES_DB=popping_test pytest -ra

- `conftest.py` and `0001_initial.py` need the `vector` extension.
- CI uses Python 3.12; a container's default `python3` may be newer.
- apt.postgresql.org may be blocked; the Ubuntu package is enough.
- In a cloud environment the setup script does the installs once; a session
  only starts the cluster (`pg_ctlcluster 16 main start`).
- Two sessions at once use two databases (for example `popping_test` and
  `popping_test_f`), and a git worktree sets `PYTHONPATH=<worktree>/backend`
  so `app` is imported from the worktree.

Frontend, from `frontend/`:

    npm ci
    git diff --exit-code package.json package-lock.json
    npm run lint && npm run build
    node --experimental-strip-types --test 'scripts/unit/*.test.ts'
    nohup ./node_modules/.bin/vite preview --host 127.0.0.1 --port 4173 --strictPort > /tmp/preview.log 2>&1 &
    FONTCONFIG_FILE=$PWD/scripts/iphone-audit/fonts-ci.conf PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
      node scripts/iphone-audit/smoke.cjs http://127.0.0.1:4173 --csp --strict

- Playwright is installed without saving it (`npm install --no-save
  playwright@1.56.1`, as CI does), and the `git diff` above stays clean.
- `vite preview` serves `dist/` from disk, so a rebuild needs no restart.
  Stop it by its PID, not `pkill -f` (the pattern matches the shell running it).
- The strict smoke takes about 3 minutes and reports 0 failures on main.
