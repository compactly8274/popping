# Popping: make it quick and obvious (deep-dive plan)

## Context

Your verdict after PR1–PR10 and PR #25: "It's come a long ways… but it's still crap using it."
The complaints:

1. Hunting through things, multi-tapping; nothing is quick.
2. You can't put categories in the order you want.
3. You don't understand how to modify or save views.
4. Nothing is intuitive to a new user.
5. Upvote / downvote / star / read / fresh-vs-not are confusing.
6. Most tabs show 100+ items after you add a feed, with no idea why.
7. After adding a feed there's no sign it's actually pulling. Finding out means filtering,
   refreshing, and flipping settings like the browser option.
8. Adding a website sometimes pulls random pages that aren't articles.

Goal: every common task takes one or two taps, and every state explains itself. A newly added
feed visibly proves it works, or says why it doesn't and what fixes it.

**Product principle (your note):** Popping is how you quit Reddit.
- Your own sources, in a UI that feels like Reddit.
- A real end: once you've seen what's new, the app says so and there is nothing more to scroll.
- Familiar social-media conventions instead of explanations. No tutorials, tip cards or help text
  where the convention is obvious.
- Anything that makes the app worse is left out (listed under "Dropped").

How this plan was made (read-only):
- 9 investigators found 94 problems, and a critic verified the root causes.
- An architect wrote PR-by-PR specs.
- A skeptic checked every claim against the code and found 1 blocker and about 25 major issues.
- The architect fixed them.
- I spot-checked the main root causes in the code myself.
- **Review round (2026-10-19).** 15 reviewers re-read the whole plan against main 4be8766: 8
  section fact-checkers and 7 cross-cutting lenses (sequencing, security, data integrity, product,
  testing, backend operations, iOS and constraints). Skeptics then tried to refute each finding,
  and a completeness round looked for what the first pass missed.
  - 227 findings were confirmed (1 blocker, 22 major, 177 minor, 27 nits), and 253 of the plan's claims
    were confirmed as correct.
  - Every must and should change from that review is applied in this revision. The owner's answers
    to the review's questions are decisions D20–D33.
  - The work is now cut into about 34 smaller units on two tracks (D20).

The full spec is the appendix at the end.

Constraints you set:
- Safe and secure.
- No new ports, containers, daemons, processes, service workers or web workers.
- Don't touch Docker/compose, nginx, package.json/lock, config.py, main.py or url_safety.py.
- Migrations are additive and nullable, with exactly one new head.
- Real fixes, not duct tape.
- Document as you go.
- Don't shrink the plan to save tokens.

Not frozen (D29): `.github/workflows/build.yml` and `README.md` may change where a unit needs it.

## Root causes (all checked in the code)

- **Nothing is quick:**
  - Opening a story never marks it read: `onActivate` is never passed (Column.tsx:732). So each
    story costs 3 taps.
  - Sort does nothing: App.tsx re-sorts every section newest-first.
  - The phone remembers its tab by position, so tabs jump when Saved or For You appear.
  - A hidden header tap marks a whole tab read.
  - Every card renders at once.
  - Settings has 8 tabs.
  - Adding a feed takes about 11 taps and files it under "Other".
- **Category order:**
  - Tabs are always alphabetical: categories are sorted by label (App.tsx:902-910) and appended
    after Saved and For You (977-992).
  - No preference stores an order (preferences.py allowlist).
- **Views:**
  - Saved views are invisible until one exists and show no active state.
  - Applying one rewrites your normal column settings.
  - Updating one is hidden behind ⋯.
  - Reset deletes saved views and saved stories on every device.
- **Card actions:**
  - Six unlabelled icons that mean three things.
  - Unstarring still records a +4 bookmark.
  - Reading time is sent in milliseconds but scored as seconds.
  - Settings > History is always empty.
  - Four "new" signals, built from two rules.
- **100+ items:**
  - Each tab is that category's all-time top 100, with no time limit, and paused feeds are
    included (entries.py:397-422).
  - A new RSS, Reddit or podcast feed's first fetch imports its whole back catalogue
    (scheduler.py `_ingest`; no cap). Website feeds already cap at 10 new pages a poll
    (`_MAX_NEW_PER_POLL`, generic_scrape.py:42), but they drain up to 200 sitemap URLs over many
    polls and keep every undated page.
  - "Fresh" only means "not ticked".
  - Read marks are capped at 200, so old stories come back as unread.
  - Wikipedia On this day posts up to 50 events plus up to 50 selected anniversaries per fetch
    (about 60 a day), all stamped "now", in News.
- **Feed health:**
  - A zero-item fetch counts as success (scheduler.py:491-492).
  - Fetchers swallow errors, and a 403 on a website shows as "no list".
  - Check now is a 3-second toast.
  - **Refresh never checks feeds**; it only re-reads the database.
  - Browser identity is manual.
  - Subreddit Test always fails (routes/sources.py:1153-1154 imports a function that doesn't
    exist).
  - Scheduled runs can be skipped. Website extraction (`trafilatura.bare_extraction`) runs
    synchronously on the one event loop (generic_scrape.py:141-143), and the scheduler is built
    with no `job_defaults` (scheduler.py:1795). So every job, the daily brief included, keeps
    APScheduler's 1 s misfire grace, and a blocked loop makes runs miss it. A longer grace alone
    would only hide this; PR1a fixes both halves.
- **Websites pull junk:**
  - Auto-add makes a scraper if the sitemap lists any URL.
  - Any page with a title and some text counts as an article.
  - Undated pages are kept and sort to the top.
  - A section URL silently falls back to the whole site.

## Decisions (my defaults, revised for your note; approving this plan approves them)

D1. **Cards look like Reddit posts:**
   - Feed icon, feed name and time, then the title, thumbnail and Summary arrow.
   - Action bar: ▲ ▼ (orange/blue when active, tap again to undo), Save (bookmark), Share, ⋯.
   - ⋯ holds Hide, Hide stories from <feed>, Edit feed, Copy link and Open in browser.
   - Swipe left = Hide.
   - No ✓ button, no 👁, no labels.

D2. **What the buttons do:**
   - Votes do what they do on Reddit: the card stays, and your taste is recorded.
   - Hide removes the story. Its Undo fully undoes it, ranking signal included (D25).
   - The "tuning…" toasts go.
   - Unsaving no longer counts as a save. Unsaving keeps one "Removed from Saved · Undo" toast;
     saving stays silent.

D3. **Seen = used up.**
   - A story counts as read once you open it or scroll past it (on screen about a second, then
     gone off the top).
   - It stays where it is until you leave the tab or pull to refresh.
   - The ranking's "viewed" signal is recorded at that moment instead of whenever a card is
     drawn.
   - On the first deploy, older stories start as seen on each device (D33).

D4. **A real end:**
   - Each tab shows unseen stories first, then "You're all caught up", then seen stories folded
     under "Earlier (N)".
   - If you flicked past some cards without reading them, the end row instead reads
     "N not seen · Mark as read" (D24).
   - The number on a tab is how many you haven't seen.
   - "Mark all as read" lives in the tab's ⋯.
   - "+N new" and the hidden header tap go.

D5. **Each tab shows what arrived in the last 2 days.** You can change that per tab: 1 day / 2 days /
   7 days / All.
   - No "Show older" row; the end is the end.
   - A tab whose feeds brought nothing in 2 days stays, and says "Nothing in 2 days · Show 7 days".
   - Saved always lists every save.

D6. **A new feed** brings its newest 10 stories plus the last 2 days, never its whole back catalogue.
   This also holds when a feed first works days after you added it, when it comes back after 3
   days or more without a successful check, and when you change its address.

D7. **Tab order:**
   - ↑/↓, Move to top and Show switches, opened from an "Edit" pill at the end of the tab strip,
     the Menu, or a tab's ⋯.
   - Brief first; Saved and For You always there unless hidden.
   - Before you set an order: Brief, Saved, For You, then categories alphabetically. New
     categories go at the end.
   - A new device opens on For You.

D8. **Saved views become your own tabs** (a name plus feeds).
   - A "From: A, B, C · Edit" line shows what's in one.
   - "New tab" in Edit tabs makes one.
   - Edits save immediately.
   - Old views convert automatically once your feeds have loaded; the old data is kept for
     rollback.
   - The chip rows and the Menu feed checklist go.

D9. **"Hide stories from <feed>"** mutes a feed without deleting it. Paused feeds leave the tabs.
   On one of your own tabs the same row reads "Remove <feed> from this tab".

D10. **Pull down to refresh** on the phone; the Refresh button stays on desktop.
   - Refresh asks the server to check this tab's feeds now (10-minute cooldown per feed). Feeds
     that are failing keep their backoff.
   - It shows "Checking 6 feeds…", then "3 new". That status row is the only result shown.
   - The phone header loses its Refresh button.

D11. **Feed health:**
   - One migration (0031) with three nullable columns.
   - Every feed gets a short status pill, and each problem gets one fix button (for example
     "Read as Safari", with Undo, offered only where the feed's type sends custom headers).
   - "No list" and "Reddit unavailable" are setup states, not failures (D27).
   - My feeds gets search, a "Needs attention" group and category headings.
   - Check now shows its result in place.

D12. **Website feeds only pull articles:**
   - URL and page scoring; news sitemaps first.
   - Stories already stored are skipped.
   - At most 2 undated pages per check; undated stories sort last. The same undated cap applies to
     every feed type.
   - A site that refuses the request (401, 402 or 403) shows as Blocked. A 429 shows as an HTTP
     error, with Check now. A missing robots.txt or guessed sitemap is not an error.

D13. **One Add field with a real preview** (the stories it will pull, plus one count line).
   - The category defaults to the tab you're on when that tab is a category. On Brief, Saved,
     For You and your own tabs it uses the type default, as auto-add does today.
   - Duplicates are refused.
   - The first check's result is shown.
   - A blocked site is retried as Safari, with Undo.
   - "Find its feed" for web pages that have one. It previews first and changes the feed only
     after "Use this feed".

D14. **Menu becomes tasks:** Add feed · Feeds (N need attention) · Saved · Edit tabs · Activity ·
   Settings.
   - Settings goes from 8 tabs to 4: Feeds / AI & Brief / Activity / General.
   - Old links still work.

D15. **No tutorials.**
   - Kept: functional states only — "Add your first feed" when you have none, "Brief needs an AI
     provider · Set up", and short errors with their fix.
   - One vocabulary everywhere: stories, feeds, tabs, Saved.
     - "Category" stays as the feed editor's field word, because a feed can also sit in your own
       tabs (PR12b).

D16. **Speed:**
   - Cards load in batches as you scroll, and loading stops at "You're all caught up".
   - Reads, saves and hides sync between devices. This lands before seen-on-scroll, which writes
     often.
   - Undo toasts of the same kind combine.

D17. **Wikipedia On This Day:** 5 selected items a day in its own tab, kept out of For You.

D18. **A feed that gets blocked later** gets a one-tap "Read as Safari" fix where its type sends
   custom headers. The automatic Safari retry runs only in preview and add.

D19. **Deferred** (each needs your yes later):
   - category rename/merge;
   - per-feed volume cap;
   - deleting old stories;
   - JSON Feed;
   - Framing Watch on the phone;
   - drag-to-reorder;
   - automatic Safari fallback on scheduled checks;
   - Restore from Activity also removing the ranking signal (needs a DELETE route). Undo on Hide
     no longer waits for this (D25).

**Dropped from the first draft because they'd make it worse:**
- the tip card and "How Popping works" sheet;
- the ✓ Read button and 👁;
- "Not interested" as a separate idea (▼ and Hide already mean that on Reddit);
- the "Show older" row and the 3-older-stories-per-feed floor (they defeat the end);
- explanatory copy;
- the vocabulary guard test.

### Decisions from the review (owner-approved 2026-10-19)

D20. **Units and tracks.** About 34 units instead of 13 PRs: a strictly serial frontend track, a
   parallel backend track, squash-merge, never stack. Each unit branches from up-to-date main
   after the previous unit in its track has merged.

D21. **Device checks.** `docs/DEVICE_CHECKS.md` on main is the running list and the only reply
   channel. Issues are turned off on this repo; if you turn them on, the list can move to a pinned
   issue. Frontend merges hold at Gate 1 (after PR3c) and Gate 2 (after PR6c). The backend
   track continues during a gate.

D22. **PR1r.** A build line in Settings, and a reload onto a new build when you return after 10
   minutes or more away. Pending writes are flushed first, it reloads at most once per build, and
   never while the page is visible.

D23. **PR2c.** Once both devices show the PR2b build, the server refuses whole-list PUTs of set keys
   with 409 "Popping was updated. Reload to keep saving."

D24. **Cards flicked past.** When every unseen card has rendered and some are still unseen, the end
   row reads "N not seen · Mark as read" (44px, the same path and Undo as Mark all). The 1 s dwell
   rule stays.

D25. **Undo on Hide cancels the `never` signal.** PR5a holds it on the device until the Undo toast
   expires, so no backend route is needed. Removed from D19 and PR13.

D26. **Tapping a feed's name on a card** opens a temporary one-feed view (PR8a). Edit feed stays in
   the card's ⋯.

D27. **Setup states.** `[no list]` and "Reddit unavailable" never count toward `error_count` and
   never auto-disable a feed. Their fix re-enables it.

D28. **The 25-per-feed cap** keeps the 25 best-scoring stories in the window, not the 25 newest.

D29. **Not frozen.** `.github/workflows/build.yml` and `README.md` may change: a 30-minute job
   timeout, a states-only WebKit run, a corrected PR-tag comment, and README.md:85 (PR images are
   never published).

D30. **Local test setup.** The cloud environment's setup script installs Postgres 16 + pgvector, a
   Python 3.12 venv and `npm ci`. No SessionStart hook is added to the repo.

D31. **Models.** Every unit uses the default model at the listed effort. No model switch, no fast
   mode, no premium tier.

D32. **Standing OK.** I squash-merge once CI is green and the unit's review step has passed, except
   PR9a (you read the rollback note and merge it) and PR2c (only after both devices run the PR2b
   build).

D33. **First deploy of the read model.** Each device treats stories fetched more than 48 hours
   before its first load as already seen, locally, writing nothing to the server. PR3b and PR3c
   deploy together.

D34. **Preview text under every headline (owner, 2026-10-21).** Cards show a short blurb under the
   title, the way Reddit and Facebook do, and tapping the title, the blurb or the picture opens
   the article itself. The blurb is the AI summary when one is already cached, otherwise the
   feed's own description, cleaned and cut to about 300 characters. The Summary arrow stays: it
   expands the full 3-4 sentence AI summary in place, for more without leaving the app. PR5a
   builds it, including the small backend change that sends the blurb with each story.


## The units (from the revised spec in the appendix)

Two tracks (D20). Each track is serial. Cross-track edges are listed below the tables. One session
may run both tracks interleaved, as long as those edges and each track's order hold.

**Before both tracks**

| Unit | What you'll notice on the phone | Track | Migration |
|---|---|---|---|
| H0 | Nothing in the app. This plan is final and on main, README.md:85 is fixed, and the Device checks list exists | docs | no |

**Backend track (B)**

| Unit | What you'll notice on the phone | Track | Migration |
|---|---|---|---|
| PR1a | Reading a website feed no longer stalls the app; the daily brief and feed checks stop getting skipped; Subreddit Test works; Settings → History shows your history | B | no |
| PR2a | Nothing yet: the new save route has no caller | B | no |
| PR4a | A new or resumed feed adds 10 stories plus the last 2 days, not its backlog; On this day posts 5 a day; the brief skips a new feed's backfill | B | no |
| PR4b | Nothing yet: new query options that PR4c and PR4d use | B | no |
| PR6a | Nothing yet: the check-now route that PR6b uses | B | no |
| PR9a | Nothing on screen yet: every feed starts recording its last attempt and counts | B | 0031 |
| PR10a | Nothing: a page-scoring library with tests | B | no |
| PR10b | Website feeds pull articles, not About, tag or shop pages; a site that refuses us shows as Blocked | B | no |
| PR11a | Nothing yet: the preview route that PR11b uses | B | no |
| PR2c (floating) | An old copy of the app can't overwrite your saves; it asks you to reload | B | no |

**Frontend track (F)**

| Unit | What you'll notice on the phone | Track | Migration |
|---|---|---|---|
| H1 | Nothing in the app: tests gain tap counting, touch drags, fake-clock rules and a real desktop width | F (test-only) | no |
| PR1r | Settings shows the build you're running; the app picks up a new build when you come back after 10 minutes | F | no |
| PR1b | Saving never jumps tabs; a new phone opens on For You; tapping the current tab scrolls to the top | F | no |
| PR1c | Newest / Best works; Reset keeps saves; Brief says when it needs AI | F | no |
| PR2b | Saves, hides and reads made on one device never undo another's | F | no |
| PR7a | Your tab order and hidden tabs; Saved and For You always there | F | no |
| PR3a | Desktop scrolls as one page; long tabs load as you scroll | F | no |
| PR3b | Opening a story uses it up; each tab ends in "You're all caught up", then "Earlier (N)"; badges count new stories | F | no |
| PR3c | Scrolling past a story uses it up | F | no |
| **Gate 1** | You try seen-on-scroll on the phone (PR3b and PR3c deployed together) | — | — |
| PR4c | Saved shows every save | F | no |
| PR4d | Tabs show the last 2 days; ⋯ → Show stories from; quiet tabs stay | F | no |
| PR5a | Cards look like Reddit posts: ▲ ▼ Save Share ⋯; swipe left hides; Undo fully undoes | F | no |
| PR5b | Hide every story from one feed | F | no |
| PR6b | Pull down to refresh: "Checking 6 feeds…", then "3 new" | F | no |
| PR6c | A slimmer header; "Add your first feed" when you have none | F | no |
| **Gate 2** | You try pull to refresh, the new header, and PR5a's swipe and sheet on the phone | — | — |
| PR9b | Every feed shows whether it's pulling, and each problem has one fix | F | no |
| PR7b | Every tab's ⋯ is one sheet; an empty tab says why | F | no |
| PR8a | Saved views become your own tabs; New tab; tap a feed's name to see just that feed | F | no |
| PR8b | The chip rows and the old Sources list are gone | F | no |
| PR11b | One Add field with a real preview, and the first check's result | F | no |
| PR11c | "Find its feed" for a web-page feed | F | no |
| PR12a | A Menu of tasks and Settings in 4 tabs | F | no |
| PR12b | One vocabulary everywhere | F | no |

**Cross-track edges** (the backend unit merges first):
- PR2a before PR2b.
- PR4b before PR4c and PR4d.
- PR6a before PR6b.
- PR9a before PR9b.
- PR11a before PR11b.
- PR10b and PR9b before PR11c.
- PR2b deployed on both devices before PR2c.

PR13 (deferred items, each needing a fresh yes, D19) is not a unit on either track.

**Calls the spec makes (each can be vetoed):**
- **C1:** After 10+ minutes away, tabs re-sort (new stories first) and scroll to the top. Under
  10 minutes nothing moves, so you return to the same spot after reading an article. If the app
  went to the background within about 2 s of opening a story, nothing moves either, however long
  you were away. Coming back first runs PR1r's check for a new build.
- **C2:** On desktop a story is used up only when opened or via Mark all, because several columns
  scroll at once there.
- **C3:** Tapping the current tab while already at the top refreshes it, like Reddit's Home tab.
- **C4:** The tab's ⋯ has a Refresh row, so there's a non-gesture way to refresh (VoiceOver,
  Switch Control). PR6b adds it to today's tab popover, and PR7b's sheet keeps it.
- **C5:** Settings → Downvoted and "Hide all downvoted" are removed, since ▼ no longer means hide.
- **C6:** On desktop, `a` / `z` upvote and downvote (the Reddit Enhancement Suite keys). They
  ignore Cmd, Ctrl and Alt, so Cmd+Z and Cmd+A still work.
- **C7:** The built-in feeds stay. Until you add a feed of your own, an "Add your first feed" row
  sits above For You's list or its empty message, and an empty Saved or For You tab shows the same
  button (PR7a). An install can be empty, so the row never depends on For You having stories.
- **C8:** Under a caught-up tab's marker, one row opens the next shown tab that has new stories
  ("News · 12"). Hidden tabs are never named. When every tab is at 0 the row is absent, so the end
  of the last tab is the end of the app.

## How I'll work

- **Step 1: document first (H0).**
  - This plan lives in `docs/UX_OVERHAUL_PLAN.md` on branch `claude/ux-overhaul-plan`, as draft
    PR #26. The text was scanned for personal details (names, emails, hosts, IPs) and has none.
  - Done: the appendix was revised to D1–D19 and re-verified against main 4be8766, then revised
    again with the 2026-10-19 review (D20–D33).
  - H0 also fixes README.md:85 (PR images are never pushed: build.yml:175 and 187, and the same
    for the frontend image, push only on push events; the fix is already on this branch), adds
    `docs/DEVICE_CHECKS.md`, and merges #26. #26 is docs only, so it merges before any code
    unit. H1 fixes the build.yml:12 comment along with its other build.yml changes.
- **Branching and merging (D20).**
  - Branch each unit from up-to-date main and open it against main; build.yml:14-18 runs CI only
    for PRs whose base is main. Never stack. While one track's PR is in CI, work on the other track.
    After any merge, `git fetch && git rebase origin/main` before pushing. Because no branch ever
    holds an unmerged parent, this is a plain rebase, never `--onto`, so squash merges cost nothing.
  - If a stack is ever unavoidable, open the child as a draft against main and record the parent's
    tip SHA. After the parent squash-merges, run `git rebase --onto origin/main <parent-tip>
    <child>`, force-push, and re-run the full smoke and flows.
  - Every merge must be safe to deploy alone, because each push to main publishes `:latest`, which
    `popping-update` pulls. That includes in-between states such as PR3b without PR3c, or PR2c
    before both devices run PR2b (PR2c has its own merge condition).
- **Each unit:**
  - Read `docs/DEVICE_CHECKS.md` first: move ticked items under "Checked", and turn `fail:`
    replies into fix units.
  - Implement the spec. For units marked "Plan first", use plan mode; when you have said to keep
    going without approvals, write the plan into the unit's notes file before changing code.
  - Run the local loop below: tsc, lint and build; unit tests; backend pytest on local Postgres;
    `smoke.cjs --csp --strict`; every flow. Run the mutation check on each new flow, and loop
    gesture and timing flows 5 times.
  - Run the security checklist (appendix Rules) and note the result in the PR body.
  - Open a draft PR, get the 4 CI checks green, run the unit's review step, then squash-merge under
    the standing OK (D32).
  - After the main-branch build for the merge commit is green, add the unit's lines to the Device
    checks issue and send at most 6 screenshots (below).
  - Merging never deploys anything: you release by running `popping-update`.
- **Documentation:** working notes go in per-unit files, `docs/ux-notes/<unit>.md`, so the two
  tracks never edit the same lines and any session can resume. This plan changes only when a
  decision or the spec changes. `ARCHITECTURE.md` and `frontend/scripts/iphone-audit/README.md` are
  updated as things change. The device-check list lives in the GitHub issue, not in a file.
- **Migrations:** only 0031: three nullable columns, a metadata-only change. Its rollback steps are
  in PR9a: prefer reverting the app code and keeping the migration file.

### Deploy and device checks

1. **The Device checks list.** `docs/DEVICE_CHECKS.md` on main is the running list and the only
   reply channel (D21). Issues are turned off on this repo, so it is a file.
   - You reply by editing it on GitHub: tick a line, or write `fail: <line>` under it. Telling the
     session in chat works too; the session then records it in the file.
   - A unit's PR adds its lines under "Pending". Run a check only after the main-branch build run
     for that merge commit is green. Images publish minutes after the merge, and
     `cancel-in-progress` (build.yml:24-26) means a quick second merge replaces the first run.
   - Each line is an unticked item: `- [ ] #<pr> sha-<short> · <one action>. Pass: <one sentence>.`
   - PR6's on-device list lives there, in Gate 2.
   - Each session reads the file at start, moves ticked items under "Checked", and turns `fail:`
     replies into fix units.
2. **Step 0 for every check:**
   - Run `popping-update` only after that run is green.
   - Run and paste the read-only revision check:
     `cd /opt/popping && for s in backend frontend; do printf '%s ' $s; docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' $(docker compose images -q $s); done`
   - Force-quit the home-screen app from the app switcher, reopen it, and reload any desktop tab.
     `index.html` is no-cache and there is no service worker, so a cold launch loads the new
     bundle, while a resumed page keeps the old one.
   - Confirm Settings shows the expected build (PR1r).
3. **Gates are sections of that issue.** Gates never auto-pass. You reply `pass`, `fail: <line>` or
   `waive`. While a gate is open, the frontend track may prepare its next branch locally but
   doesn't merge, and the backend track continues.
   - **Gate 1 (PR3b and PR3c, deployed together):**
     - Open the top story of a badged tab and come back. Pass: the badge drops by 1 and the list
       hasn't moved.
     - Scroll slowly past 5 cards. Pass: they count as seen.
     - Fling past 20. Pass: those stay unseen, and the end row offers "N not seen · Mark as read".
     - Dwell feel. Too eager → `SEEN_DWELL_MS` 1500, right → 1000, too slow → 700. This is a
       retune, not a stop.
     - Rotate to landscape and back, then scroll. Pass: marks still happen.
   - **Gate 2 (PR6b and PR6c, deployed together; also PR5a's swipe and sheet):**
     - Pull at the top. Pass: the spinner follows the finger, and a release past the threshold
       shows "Checking N feeds…" and then a result. A short pull springs back with no check.
     - Shaky upward scroll from the top. Pass: native scroll, no spinner.
     - Diagonal drag. Pass: a pull or a card swipe, never both, never a stuck list.
     - Long-press a link. Pass: the iOS preview, no spinner.
     - Short tab. Pass: same as a pull at the top.
     - Ten slow pulls in a row. Pass: all arm.
     - Pass: no doubled movement.
     - Small downward wobble, then scroll up. Pass: native scroll.
     - VoiceOver. Pass: it reads "Checking…" and the result.
     - Swipe a card left (PR5a). Pass: it hides, and Undo brings it back.
     - Open a card's ⋯ (PR5a). Pass: the sheet rises from the bottom, every row is in reach, and a
       tap outside closes it.
4. **Rollback.**
   - Never edit tracked files in `/opt/popping`: `git pull --ff-only` would then refuse, and every
     later `popping-update` stops.
   - Never pin in `.env`: later updates would silently stay on the pin.
   - The file keeps "Last good: sha-<…>", the last passed check. It was deployed, so its tag
     exists.
   - Fast frontend-only rollback:
     `cd /opt/popping && POPPING_IMAGE_TAG=sha-<last good> docker compose up -d --no-deps frontend`
     (`--no-deps` keeps the backend on its image; see PR9a's rollback note). Then no
     `popping-update` until the revert is merged and its main run is green.
   - Backend schema rollback follows PR9a.
   - After any deploy or revert, relaunch every device.
5. **Deploy timing.** You deploy at the two gates and whenever convenient, except between
   PR3b's and PR3c's merges (D33). Read PR9a's rollback
   note before deploying it. Production problems are fixed forward, or reverted newest-first down
   the dependency table.
6. **Screenshots.**
   - Render the unit's new or changed states with:
     `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node scripts/iphone-audit/smoke.cjs http://127.0.0.1:4173 --csp --only <states> --out <scratchpad>/shots/<unit>`.
     Without `--out`, smoke writes no PNGs.
   - Look at each one, and send at most 6 with the session's file-send tool. If a session has no
     such tool, describe them in the unit's PR.
   - A flow screenshot shows only its start; H1 adds a `shot(label)` callback for end states.
   - Never commit PNGs.

### Local loop (D30)

CI stays authoritative. The local loop catches problems before a push. If setup fails, say so in
the unit's notes and the PR.

- **Backend:**
  - `apt-get install postgresql-16-pgvector`, then `pg_ctlcluster 16 main start` (the cloud
    environment's setup script does the install).
  - Create the `popping_test` role and database, and run `CREATE EXTENSION vector`.
  - A python3.12 venv with `--no-deps` installs of `requirements-dev.lock` and `-e backend`.
  - `pytest`.
  - This is a throwaway test cluster, not an app process.
- **Frontend:**
  - `npm ci`, then `git diff --exit-code` on `package.json` and the lockfile.
  - Lint, build, unit tests.
  - `vite preview` on 127.0.0.1:4173.
  - Smoke with `FONTCONFIG_FILE=…/fonts-ci.conf` and `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`,
    `--csp --strict`.
  - WebKit results come only from CI.
- **The mutation check** (for each new flow or pytest that guards a rule):
  - Name the guarded line and break it.
  - Run `npm run build`, then `--only <flow>` (or the single pytest).
  - Confirm it fails on the expected rule.
  - Restore with `git checkout` plus `git diff --exit-code`.
  - Record the result in the unit's notes.
- Loop gesture and timing flows 5 times.

### What I can't do from the cloud

- Run EXPLAIN on the server. I'll test the window and story-count queries on a production-sized
  synthetic table locally (about 400k rows), and give you a read-only command to run if you want.
- Know the junk websites. Send the URLs whenever; the fixtures cover common CMS layouts until then.
  Before PR10b merges, you run a read-only probe on the VPS that compares item counts on your
  existing website feeds.
- Try gestures and dwell on an iPhone. That is what the two gates are for.
- See which build a device runs. PR1r's build line shows it.

### Stop rule

- A unit stops when a step would need a forbidden file, a non-additive migration, or a change to a
  decision above, or when your device check contradicts a decision in a way a constant retune can't
  fix.
- I note why in the unit's notes and the PR, and continue with the other track and with
  independent units. A stop on one track leaves the other track running.
- A retune (for example `SEEN_DWELL_MS`) is not a stop.

### How each unit is run

Baseline for every unit: the default model (D31); effort `high`, set once with
`CLAUDE_CODE_EFFORT_LEVEL=high` in the cloud environment's variables (the default model starts at
medium); Auto permissions (Accept edits if Auto is off); one session per unit; no fast mode;
ultracode off while implementing.

"Review agents" means 2–4 parallel review subagents, one per lens, plus an adversarial verifier per
finding (run as a workflow when you have turned ultracode on). "Plan first" means plan mode; when
you have said to keep going without approvals, the plan goes into the unit's notes file first
instead.

| Unit | Effort | Plan first | Review before merge |
|---|---|---|---|
| H0 | high | no | 2 agents: every change landed; new anchors re-checked |
| H1 | high | no | /code-review high; full strict smoke and every flow 5 times locally |
| PR1r | high | no | /code-review high; reload flow 5 times; owner confirms the build line |
| PR1a | high | no | /code-review medium; pytest |
| PR1b | high | no | /code-review high; 390px screenshots |
| PR1c | high | no | /code-review high |
| PR2a | high | no | /code-review high + /security-review; FOR UPDATE test with the lock removed must fail |
| PR2b | xhigh | yes | review agents: ordering and merge semantics; iOS lifecycle; tests and mutation; /code-review ultra if the plan allows |
| PR2c | high | no | /code-review medium; merge condition above |
| PR7a | high | no | /code-review high; tap-count flows |
| PR3a | high | no | /code-review high; the 1280 flow |
| PR3b | xhigh | yes | Explore inventory first; review agents: partition rules; call sites and removals; memo and stale closures; a11y and smoke; /code-review ultra if the plan allows |
| PR3c | xhigh | yes | review agents: observer lifecycle; visibility and flush order; flow determinism; then Gate 1 |
| PR4a | high | no | /code-review xhigh |
| PR4b | high | no | /code-review high + /security-review; EXPLAIN on a synthetic 400k-row table |
| PR4c | high | no | /code-review medium |
| PR4d | high | no | /code-review high |
| PR5a | high | no | /code-review xhigh |
| PR5b | high | no | /code-review high |
| PR6a | high | no | /code-review high + /security-review |
| PR6b | xhigh | yes | review agents: gesture reducer against iOS touch rules; queue and completion against PR6a; status row a11y and layout |
| PR6c | high | no | /code-review high; then Gate 2 |
| PR9a | xhigh | yes | review agents: migration and rollback; ingest bookkeeping; query cost; the owner merges |
| PR9b | high | no | /code-review high |
| PR7b | high | no | /code-review high |
| PR8a | xhigh | yes | review agents: conversion and data safety; keys, focus and UI |
| PR8b | high | no | Explore inventory first; /code-review high |
| PR10a | high | no | /code-review medium |
| PR10b | xhigh | yes | review agents: crawl errors against today's behaviour; scoring false negatives; broken-test inventory; owner's read-only VPS probe |
| PR11a | high | no | /security-review + /code-review high |
| PR11b | high | yes | /code-review xhigh; tap-count flows |
| PR11c | high | no | /code-review medium |
| PR12a | high | no | /code-review high |
| PR12b | medium | no | /code-review low; full smoke and every flow |

Notes:
- Set effort with `/effort high` or `/effort xhigh` as an argument, or with
  `CLAUDE_CODE_EFFORT_LEVEL` in the environment. Don't rely on the `effortLevel` settings key.
- `/security-review` needs an `origin` remote. If it doesn't run in a cloud session, use one
  security-lens review agent with the Rules checklist.
- Use the built-in Explore subagent for call-site inventories (PR3b, PR8b). Separate sessions each
  have their own clone, so worktree isolation isn't needed.
- At most two sessions at once (one per track), because cloud sessions share rate limits.

## Verification (end to end)

- **Every unit:** CI's four checks (backend tests, frontend lint + build with strict smoke, both
  images), plus the local loop above and the unit's review step.
- **New flows** assert the target tap counts and the new behaviour:
  - open marks read; scroll-past marks seen; caught-up marker and the "N not seen" row; pull to
    refresh checks feeds and keeps backoff; the status row is the only result;
  - tab badge counts unseen; quiet tabs keep their tab;
  - Reddit-style card actions; Hide and its Undo (no `never` after Undo); hide feed;
  - tab reorder ≤3 taps and Move to top; custom tab create ≤6 taps; one-feed view;
  - Needs attention; Read as Safari only where headers are sent;
  - add preview ≤5 taps, counting the paste; Find its feed with its confirm; Menu rows reach their
    target in 2 taps;
  - reload on a new build (PR1r); keepalive flush on hide; old-bundle PUT refused (PR2c).
- **New pytest:** scheduler defaults and the brief:daily grace; preference deltas, `{key:path}`
  keys and caps; the PUT 409 per set key; ingest rules, the resume gap and the backfill cap; the
  brief guard; entries window (both query paths) and the by-ids projection; check-now queue,
  backoff and completion; migration 0031 single head and up/down/up; source health; website error
  semantics (robots.txt 404 still finds sitemaps; refusals show Blocked); article_detect fixtures;
  preview deadline, semaphore and cache; duplicate 409.
- **At the gates:** you pass Gate 1 and Gate 2 in the Device checks list.
- **After the final unit:** a full strict smoke at 390, 320 and 312 wide plus 1280, every flow, and
  a screenshot set.

---

# Appendix: implementation spec (re-verified, Reddit-replacement revision, review-revised 2026-10-19)

> Written for D1–D19 and re-checked against main 4be8766 (which includes #25) by an architect, an
> adversarial reviewer and a final revision pass. Revised on 2026-10-19 by applying every must and
> should change from the review round, which also cut the work into the units below and added
> D20–D33. The decisions above still win if anything here conflicts with them.

**Base:** `origin/main` 4be8766, which includes #25 (display names, migration 0030). Its `frontend/` and `backend/` trees are byte-identical to #25's branch (355c596). All line numbers below are `main` line numbers, and every anchor was re-checked against 4be8766, the review round's included.

**Product principle:** Popping replaces Reddit. You bring your own sources, the UI works like Reddit, and there is a real end. The spec uses familiar conventions instead of explanations. When you have seen everything new, the app tells you so and there is nothing more to scroll.

## Summary

Every merge needs CI green and the unit's review step (D32) unless the merge condition says more. "Track order" means the unit only follows the previous unit in its track; there is no code dependency. PR13 is listed for completeness.

| Unit | Title | What you'll notice on the phone | Size | Migration | Track | Depends on | Merge condition |
|---|---|---|---|---|---|---|---|
| PR0 | Merge #25 | Feeds keep the name you type (#25, merged) | M | 0030 (merged) | — | none | merged |
| H0 | Plan revision, README fix, Device checks list | Nothing in the app: this plan on main, README.md:85 fixed, the Device checks list open | S | no | docs | none | the owner has read the units, gates and decisions |
| PR1a | Scheduler defaults, extraction off the event loop, Reddit Test, History | Website feeds no longer stall the app; the daily brief and feed checks stop getting skipped; Subreddit Test works; History shows your history | S | no | B | H0 | standard |
| PR2a | Preference PATCH routes | Nothing yet: the PATCH save route, with no caller | M | no | B | PR1a (track order) | standard |
| PR4a | Ingest rules, brief guard, On this day | A new or resumed feed adds 10 stories plus the last 2 days; On this day posts 5 a day; the brief skips a new feed's backfill | M | no | B | PR2a (track order; scheduler.py after PR1a) | standard |
| PR4b | Window parameters and by-ids projection | Nothing yet: window, per-feed cap and active-only options; the by-ids projection | M | no | B | PR4a (track order) | standard; the EXPLAIN result |
| PR6a | queue_check and the check-now route | Nothing yet: the check-now route | M | no | B | PR4b (track order; scheduler.py after PR4a) | standard |
| PR9a | Migration 0031 and feed health backend | Nothing on screen: feeds record their last attempt and counts | L | 0031 | B | PR0 (head 0030), PR4a, PR6a (`queued_at`; scheduler.py order) | the owner reads the rollback note and merges it |
| PR10a | article_detect library | Nothing: the article_detect library and fixtures | M | no | B | PR9a (track order) | standard |
| PR10b | Website crawl wiring and errors | Website feeds pull articles; a site that refuses us shows as Blocked | L | no | B | PR10a, PR9a (error kinds), PR1a (`_extract_one` in a thread), PR4a (the undated cap) | the owner's read-only VPS probe of item counts |
| PR11a | Preview endpoint, browser headers, PATCH type | Nothing yet: the preview route, browser headers, PATCH `type` | M | no | B | PR10b (`plan_candidates`), PR9a, PR4a (`select_items_for_ingest`) | standard |
| PR2c | Refuse whole-list PUTs of set keys | An old copy of the app can't overwrite your saves | S | no | B (floating) | PR2b deployed on both devices; PR5b if it includes `muted_sources` | the Device checks list shows both devices on the PR2b build |
| H1 | Test harness groundwork and local loop | Nothing in the app: test harness groundwork and the local loop | M | no | F (test-only) | H0 | flows 5 times locally |
| PR1r | Build stamp and reload on a new build | Settings shows the build; the app reloads onto a new build after 10 minutes away | S | no | F | H1 | standard; after its first deploy the owner confirms the build line |
| PR1b | Tab keys, resolveTab, tap-to-top, phone scroller | Saving never moves you to another tab. A new phone opens on For You. Tapping the current tab scrolls to the top | M | no | F | PR1r (track order), H1 | standard |
| PR1c | Newest \| Best, Reset scope, bookmark on save, Brief AI row | Newest / Best works. Reset keeps saves. Brief says when it needs AI | M | no | F | PR1b | standard |
| PR2b | prefSync client and sync rules | Saves, hides and reads made on one device never undo another's | L | no | F | PR2a; PR1c (file overlap only) | standard |
| PR7a | Tab order and hiding | Your tab order and hidden tabs. Saved and For You are always there | M | no | F | PR1b (tab keys), PR2b (stale-GET protection for the new PUT key) | standard |
| PR3a | Desktop single scroller and batching | Desktop scrolls as one page. Long tabs load 30 at a time | M | no | F | PR7a (track order), H1 (1280 width), PR1b (scroller element) | standard |
| PR3b | Read model: open marks seen, marker, Earlier, badges | Opening a story uses it up. Each tab ends in "You're all caught up", then "Earlier (N)". Badges count new stories | L | no | F | PR3a, PR2a (`seen:` prefix), PR2b (`addIds`), PR1b, PR1c, PR1r (C1's update check) | standard; deploys only together with PR3c |
| PR3c | Seen on scroll | Scrolling past a story uses it up | M | no | F | PR3b | standard; then **Gate 1** |
| PR4c | Saved by id | Saved holds every save | S | no | F | PR4b, PR3b (prune keep set); Gate 1 passed | standard |
| PR4d | Window UI | Tabs show the last 2 days. ⋯ → Show stories from. Quiet tabs stay | M | no | F | PR4b, PR3b (snapshot), PR4c | standard |
| PR5a | Reddit card, action sheet, Hide hold | Cards look like Reddit posts: feed, time, title, then ▲ ▼ Save Share ⋯. Swipe left hides, and Undo fully undoes | L | no | F | PR3b (✓ gone), PR2b | standard |
| PR5b | Muted feeds | You can hide every story from one feed | M | no | F | PR5a, PR2b, PR4d (`extraByTab`) | standard |
| PR6b | Pull to refresh and checkTabNow | Pull down to refresh: "Checking 6 feeds…", then "3 new" | L | no | F | PR6a, PR1b (scroller element), PR3b (snapshot reset), PR5a (shared swipe constants), PR5b (muted feeds left out) | standard |
| PR6c | Slim phone header and the first-feed row | A slimmer header. "Add your first feed" when you have none | S | no | F | PR6b | standard; then **Gate 2** |
| PR9b | Feed health UI | Every feed shows whether it is pulling, and each problem has one fix | M | no | F | PR9a, PR6b (status row, `checkTabNow`), PR7a (tab order); Gate 2 passed | standard |
| PR7b | Tab ⋯ sheet and empty states | Every tab's ⋯ is one sheet. An empty tab says why | M | no | F | PR7a, PR3b (Mark all), PR4d, PR5a (ActionSheet), PR6b (Refresh row), PR9b (feedHealth) | standard |
| PR8a | Custom tabs | Saved views become your own tabs, with their feeds listed under the header. New tab. Tap a feed's name to see just that feed | L | no | F | PR7a, PR7b, PR1b, PR3b, PR4d, PR5b (mute filter), PR6b | standard |
| PR8b | Remove chips, presets and the old Sources UI | The chip rows and the old Sources list are gone | M | no | F | PR8a | standard |
| PR11b | Add flow and 409 | One add field with a real preview, and the first check's result | L | no | F | PR11a, PR7a (category order), PR8a (`view:<id>` tabs), PR9b | standard |
| PR11c | Find its feed | "Find its feed" for a web-page feed | S | no | F | PR11b, PR10b, PR9b | standard |
| PR12a | Menu of tasks and 4 Settings tabs | A Menu of tasks and Settings in 4 tabs | M | no | F | PR5a (C5), PR7b (tab ⋯ sheet), PR8b, PR9b, PR11b | standard |
| PR12b | Copy sweep | One vocabulary everywhere | S | no | F | PR12a | standard; full smoke and every flow |
| PR13 | Deferred | Deferred items, each needing a fresh yes (D19) | varies | maybe | — | as needed | the owner's yes |

**Gates (D21):** Gate 1 sits after PR3c (PR3b and PR3c deploy together); Gate 2 sits after PR6c. Frontend units that follow a gate don't merge until the owner has passed it in the Device checks list. The backend track continues during a gate.

**Cross-track edges** (the backend unit merges first): PR2a→PR2b; PR4b→PR4c and PR4d; PR6a→PR6b; PR9a→PR9b; PR11a→PR11b; PR10b and PR9b→PR11c; PR2b deployed on both devices→PR2c.

**Files the tracks share:** backend units never edit the backend/tests files that pin frontend source (slice19, slice21, `test_frontend_*`, `test_slice30_safe_external_url.py`, `test_source_create_sitemap_link_pattern.py`); frontend units own those. `test_b1_b8_coverage_gaps.py` pins backend source only (`scheduler.py`, `generic_scrape.py`'s header path, `article_extract.py`, the plugin files), so the backend units that touch those files own it. PR1a and PR10b both touch `generic_scrape.py` and stay in the backend track. PR4a, PR6a and PR9a all touch `scheduler.py` and run in that order. PR2c touches `routes/preferences.py` and `test_preferences_anonymous.py`, which only PR2a touched before it. Exceptions:
- PR11a, a backend unit, adds the `BROWSER_HEADERS` pin to `test_frontend_feed_editor_rules.py`. PR4d also edits that file, so whichever of the two merges second rebases on the other.
- PR2c also makes `capture.cjs`'s PUT mock answer 409 for set keys.
- PR11b, a frontend unit, carries the duplicate-URL 409 in `routes/sources.py`, so the text and `ALREADY_FOLLOW_RE` ship together.

**Order changes from the previous spec, and why:**
1. **Two tracks instead of one line (D20).** The backend halves of complaints 6, 7 and 8 (PR4a, PR9a, PR10b) don't need the frontend work, so they ship in the first third instead of waiting behind PR3–PR8. Complaint 2 (PR7a) moves up to sixth on the frontend track, because it needs only PR1b's tab keys and PR2b's PUT protection.
2. **Groundwork first (H1, PR1r).** Without H1, harness bugs look like product bugs inside large PRs. PR1r makes every later deploy reach the devices by itself, and lets the owner confirm the build being checked.
3. **PR1 splits in three.** It held five unrelated changes. The backend fixes (PR1a) are pytest-only and fix live bugs now, and take PR6's misfire fix with them. Tab keys (PR1b) are the riskiest state change, and later units build on them. Sort, Reset and Brief (PR1c) can each be reverted separately.
4. **Delta sync ships alone, before seen-on-scroll (PR2a, PR2b).** While you scroll, seen marks arrive about once a second. Today's whole-list PUTs, and App's whole-list setters built from refs mirrored in effects, would drop them or undo them. PR2a is additive with no caller, so it merges and reverts alone. The client protocol (PR2b) needs its own focused session at the highest settings. PR2c is a guard whose risk is timing, so it gets its own merge condition.
5. **The read model splits in three (PR3a, PR3b, PR3c).** The desktop single scroller and batching (PR3a) fix a layout bug that exists anyway. PR3b holds the read model and every removal of ✓, swipe-right, `m`, the context-menu "Mark as read", the header tap and the chip, so there is never a second way to mark something read: after PR3b a story is used up only by opening it or by Mark all. Seen-on-scroll (PR3c) needs on-device tuning, so it can be reverted alone. PR3b and PR3c deploy together (D33).
6. **The window follows the read model (PR4d after PR3b),** so badges and Earlier count inside the window from the start. The ingest fix (PR4a) stops floods (complaint 6) without waiting for PR3. Saved by id (PR4c) lands before the window, so old saves never vanish.
7. **The card follows the read model (PR5a after PR3b).** By then ✓ is gone, so PR5a is only the Reddit layout, Hide, Share and the Hide hold. The new preference key (PR5b, muted feeds) can be reverted separately from the layout.
8. **Pull-to-refresh pairs with Check now.** The backend (PR6a) goes first and stays inert. The gesture (PR6b) is isolated for Gate 2, and it needs PR3b's snapshot reset. The header Refresh is removed only once pull exists (PR6c, D10).
9. **PR7 splits.** Tab order (PR7a) ships early. Only the ⋯ bottom sheet (PR7b) needs PR4d, PR5a, PR6b and PR9b. It matches the card's ⋯ and sits within thumb reach.
10. **PR8 splits.** A problem in the removals (PR8b) can be reverted without losing the converted tabs (PR8a).
11. **PR9 splits, and loses two parts.** Feed health (complaint 7) no longer waits for custom tabs: the backend (PR9a) runs in the backend track and the UI (PR9b) follows Gate 2. Website blocked/no-list surfacing moves to PR10b, which rewrites the same crawl functions and tests anyway. PATCH `type` and the discover headers move to PR11a: its preview uses the headers, and PR11c is the only caller of PATCH `type`.
12. **PR10 splits.** The pure library (PR10a) changes no behaviour. The wiring (PR10b) carries the regression risk to working feeds, so it gets the strongest settings and the owner's probe.
13. **PR11 splits.** The backend preview (PR11a) is inert and security-focused. The 409 ships with its regex (PR11b). Find its feed (PR11c) is a small, separate piece of UX.
14. **PR12 splits.** The copy sweep (PR12b) would hide the routing changes in the diff, and should be revertible on its own.

**Calls this spec makes (the owner can veto each):**
- **C1, returning to the app.** After 10 minutes or more away, each tab re-sorts (new stories first) and scrolls to the top. Time away is measured with `Date.now`, because timers stop while iOS suspends the page. Under 10 minutes nothing moves, so you can read an article and come back to the same place. If the app went to the background within about 2 s of opening a story, nothing moves either, however long you were away. The return first runs PR1r's check for a new build.
- **C2, desktop is open-only.** On desktop a story is used up when you open it or use Mark all. Several columns scroll together there, so scrolling past one doesn't mean you read it.
- **C3, tab tap refreshes.** Tapping the current tab while already at the top refreshes it, as Reddit's Home tab does.
- **C4, a Refresh row in the tab ⋯.** It is the path for VoiceOver and Switch Control once the phone header loses Refresh. PR6b adds it to today's tab popover, and PR7b's sheet keeps it.
- **C5, the Downvoted list is removed.** Settings → Downvoted and "Hide all downvoted" go, because ▼ no longer means hide.
- **C6, desktop vote keys.** Desktop gets `a` / `z` for ▲ / ▼ (the Reddit Enhancement Suite keys). They ignore Cmd, Ctrl and Alt, so Cmd+Z and Cmd+A still work.
- **C7, built-in feeds.** The built-ins (BBC News, HN, NVD, CISA KEV, GitHub releases, RFD, On this day) stay in the tabs. Until you add a feed of your own, a 44px "Add your first feed" row sits above For You's list or its empty message, and an empty Saved or For You tab shows the same button (PR7a). An install can be empty, so the row never depends on For You having stories. The alternative is for a new install to start with the built-ins in `muted_sources`.
- **C8, the next tab.** Under a caught-up tab's marker, one 44px row names the next shown tab that has new stories ("News · 12") and opens it. It walks `orderColumns().shown`, so hidden tabs are never named. When every tab is at 0 the row is absent, so the end of the last tab is the end of the app.

## Target tap counts

| Task | Today | After (unit) |
|---|---|---|
| Use up a story | 2 (open, ✓) | 1 (open) (PR3b) or 0 (scroll past) (PR3c) |
| Know you've seen everything new | not possible | 0: "You're all caught up" (PR3b) |
| Go to the next tab with new stories | scroll the tab strip, check badges | 1 (C8) (PR3b) |
| Clear a tab | 1 unlabelled header tap | 0 (scroll to the end), 1 ("N not seen · Mark as read" at the end, D24) or 2 from the top (⋯, Mark all as read) (PR3b) |
| Upvote / downvote | 1 | 1 (PR5a) |
| Hide a story | 1 (eye button; ▼ does something else) | 1 swipe, or 2 (⋯, Hide) (PR5a) |
| Save | 1 | 1 (PR5a) |
| Share a story | 3+ (system long-press menu) | 1 (Share) (PR5a) |
| Hide every story from one feed | not possible | 2 (⋯, Hide stories from X) (PR5b) |
| Check a tab's feeds now | not possible | 1 pull (PR6b) |
| See a tab's older stories | 3 (24 h at most) | 3 (⋯, 7 days, close) (PR4d) |
| Move a tab one place earlier | not possible | 3 (Edit, ↑, Done) (PR7a) |
| Move a tab to the front | not possible | 3 (Edit, Move to top, Done) (PR7a) |
| Hide a tab | not possible | 3 (Edit, switch, Done) (PR7a) |
| Make a tab of 3 feeds | 7 + typing + scrolling | 6 + typing (Edit, New tab, type the name, three ticks, Done) (PR8a) |
| Add a feed to a custom tab | 5 | 3 (Edit on the "From" line, tick, Done) (PR8a) |
| See which feeds a custom tab holds | not shown | 0 (PR8a) |
| See one feed's stories | 2 (≡, the feed under Sources) | 1 (tap the feed's name on a card, D26) (PR8a) |
| Is a feed pulling? | ~5 + a filter | 2 (≡, Settings: a pill on every row) (PR9b) |
| Fix a feed a site now blocks | ~6 | 3 (≡, Settings, Read as Safari), for feeds whose type sends custom headers (PR9b) |
| Find one feed | scroll an unsorted list | 3 (≡, Settings, type) (PR9b) |
| Point a web-page feed at its real feed | delete and re-add | 4 (≡, Feeds, Find its feed, Use this feed) (PR11c) |
| Add a feed and know it pulls | 6, plus ~4 to verify | 5, counting the paste (≡, Add feed, Paste, Preview, Add), with the result shown (PR11b) |
| Restore a hidden story | 4 | 1 (Undo) or 3 (≡, Activity, Restore; Activity opens on Hidden while any story is hidden) (PR12a) |

## Rules for every PR

**Frozen infrastructure**
- No new ports, containers, daemons, background processes, workers or service workers. Never edit Docker/compose, nginx, `package.json`/lockfile, `config.py`, `main.py` or `url_safety.py`.
- `.github/workflows/build.yml` and `README.md` are not frozen (D29). H1 makes the build.yml changes.
- IntersectionObserver and touch listeners run on the main thread, with no library.

**Routes and tunables**
- New routes go on existing routers. Sources routes use `_write_deps`. Preferences routes use `current_user` + `resolve_user_id`.
- OIDC gating is per router (`main.py:290-304`).
- Tunables are read with `runtime_settings.get_many`, then parsed and clamped as in `brief._resolve_window_hours` (`brief.py:321-339`). A missing key falls through to the default and is not cached (`runtime_settings.py:214-241`). A changed row can take up to 1 h to apply (`:52`).

**Security checklist (every unit that touches a route, a fetch or stored JSON)**
- New routes sit inside `main.py`'s `_api_deps` loop (:290-304), and `test_oidc_route_gating.py` passes.
- State-changing routes take a Pydantic JSON body, and no GET has side effects.
- Any server read of client-written preference JSON is coerced and capped.
- Network I/O goes only through `fetch_html`, `fetch_html_checked`, `fetch_rss` or `reddit_client`. `trafilatura` and `courlan` are used only as parsers.
- Run `/security-review` or a security-lens review agent, and note the result in the PR body.

**Libraries**
- Pure libraries in `frontend/src/lib/*.ts` stay runnable under `node --experimental-strip-types --test` (build.yml:127).
  - Allowed: statement-level `import type {…}` from anywhere, and value imports of other pure libs with an explicit `.ts` extension.
  - Not allowed: React or the DOM, value imports from `api.ts`, extensionless paths, inline `{ type X }` imports. On Node 22, extensionless and inline-type imports fail under strip-types.
- The new pure libs are `sorting`, `columns`, `prefSync`, `seen`, `feedOrder`, `pull`, `window`, `buildId`, `feedHealth` and `customTabs`. Existing ones extended: `categories`, `settingsTabs` and `apiErrors`.
- Unit tests live in `frontend/scripts/unit/`.
- React glue lives in `lib/use*.ts`.

**Timers, not clocks**
- Smoke freezes `Date.now` with `context.clock.setFixedTime` but keeps timers running (`smoke.cjs:934`). So the app's dwell and pull timing use `setTimeout` and event deltas, and only C1's time away uses `Date.now`.
- Flows that assert dwell, batch, toast, poll, silent-refresh or arrival timing install `page.clock` and advance with `runFor`. C1's time away is tested with `page.clock.setFixedTime(F.NOW + 11 min)` while the page is hidden.
- Where real time is kept, pauses are at least 2× `SEEN_DWELL_MS`. GET-count assertions allow for the regular visible poll.

**Flows**
- H1 adds the helpers to `flows.cjs` (neither `tap` nor `taps` exists today):
  - `tap()` is `locator.tap()` and increments `page._taps`; `taps(page, max, id)` fails above max. A swipe counts as 1, and `fill` is free. Tap-count flows use only `tap`, `touchDrag` and `fill` from a starting URL, with no evaluate, pushState or click shortcuts.
  - `touchDrag(page, {from, to, steps, stepMs=50, holdMs=150})` uses a CDP session, returns early off Chromium, waits two rAFs plus 300 ms before touchStart, and holds before touchEnd. Flows assert outcomes only, never exact scroll offsets.
  - `setHidden(page, bool)` defines both `visibilityState` and `hidden` and dispatches `visibilitychange`.
  - A fetch wrapper records `init.keepalive`, which Playwright can't see.
  - Seen and IntersectionObserver flows drive `scrollTop` and `scrollBy`.
- Stub `window.open`, `navigator.share` and the clipboard in every flow (H1).
- Gestures use CDP `Input.dispatchTouchEvent` through `touchDrag` on Chromium. They return early when `page.context().browser().browserType().name() !== 'chromium'`, because the WebKit run is report-only (`build.yml:146-148`). Chromium doesn't show the page touch moves under about 40px, so small-movement rules are tested in pure-lib unit tests (PR6b's `pull.test.ts`).
- Desktop flows use `widths: [1280]` and `wait: 'main.grid article[data-card-id]'`. Without that, `runState` waits for a phone card inside the hidden `main.md:hidden` (`smoke.cjs:87, 987`). H1 adds `1280: {width: 1280, height: 800, deviceScaleFactor: 1, desktop: true}` to `WIDTHS`, but not to `DEFAULT_WIDTHS`. `desktop: true` drops `isMobile`, `hasTouch` and the iOS UA (`contextOptions`, `smoke.cjs:883-901`), and skips smoke check C1 (pointer: coarse). App code splits phone and desktop on `isDesktop` only, never on breakpoints.
- Each PR names the flows and states it rewrites.

**Smoke**
- New sheets join `C12_ROOTS` (`smoke.cjs:68`).
- Rows added inside a Column are never `<section>`, because `dashboard` asserts exactly one `main section` (`smoke.cjs:153`).
- Nothing keeps `[role="status"]` mounted while idle. Smoke waits up to 8 s for none before every state (`smoke.cjs:990`), and smoke check C7 (tap targets) measures buttons inside it (`:667`).
- From H1, `capture.cjs` keeps an in-memory preference store for GET (with `since`), PUT and DELETE; PR2b adds PATCH. Today's mock echoes `body.value` for every method (`capture.cjs:182-186`), so a PATCH would get `null` back.
- H1 makes `smoke.cjs` pass an optional `shot(label)` to `run()`, writing `${width}-${state.name}-${label}.png`, so flow end states can be screenshotted.

**Sheets**
- `role="dialog"`, `aria-modal`, and the focus trap and inert pattern from `Drawer.tsx:122-242`.
- Safe-area bottom padding and 44px rows.
- Scroll bodies use `overflow-y-auto overscroll-contain`, or strict smoke check C4 fails on the first scrollable sheet.
- Enter and exit transitions are `motion-safe` only.
- A sheet with a text field pads its bottom with `useVisualViewport().kbInset`.
- Inputs are 17px `type="text"` with an `inputMode`. URL fields also get `autoCapitalize="none" autoCorrect="off"`.

**Copy**
- Use stories, feeds, tabs and Saved.
- No sentence that explains a convention.
- An error is one short sentence and one fix.

**Revert and version skew**
- "Revert is safe" in a unit's Risks means old clients and stored data stay compatible.
- As a git revert, a unit stands alone only until the first unit that lists it under Depends on (Summary table) merges. For example PR1b→PR1c, PR2b→PR7a, PR3b→PR3c, PR5a→PR5b, PR10b→PR11a. After that, fix forward or revert newest-first.
- After any deploy or revert, relaunch every device. A resumed page keeps the newer bundle, and its PATCH (405) or new-key writes (400) fail against a reverted server. From PR1r on, a device also reloads by itself on its next return after 10 minutes away.
- Keep each unit's data-compatibility notes.

**Docs and screenshots**
- Working notes go in `docs/ux-notes/<unit>.md`. This plan is `docs/UX_OVERHAUL_PLAN.md`, and it changes only when a decision or the spec changes.
- Update `ARCHITECTURE.md` (new process-local state) and `frontend/scripts/iphone-audit/README.md` (new flows and states).
- Take screenshots with `smoke.cjs --only <state> --out <scratchpad>/shots/<unit>`. Never commit PNGs.
- Device checks follow "Deploy and device checks" in How I'll work: the unit's PR adds its lines to `docs/DEVICE_CHECKS.md`, the owner checks after the main build is green, and the owner relaunches the home-screen app before checking.
- The local loop and the mutation check are in How I'll work; H1 builds the test helpers they use.

---
## PR0: Merge #25 (merged)

**Already on `main`:**
- `labels.ts` `sourceLabel` / `rememberDisplayNames`, and `useSourceLabels.ts`.
- `feedRules.ts` `uniqueSlug` / `cleanDisplayName`.
- The FeedEditor Name field.
- The flow `flow-fl-rename-keeps-filter-and-view`.
- Migration `0030_source_display_name.py`.

**Rollback.** `backend/Dockerfile:48` runs `alembic upgrade head && uvicorn`. A reverted image has no 0030 script while the database is at 0030, so the upgrade fails and uvicorn never starts. So:
1. **Preferred: revert only the app code.** Keep `0030_source_display_name.py` in `backend/alembic/versions`. The column is nullable, so old code ignores it and the head stays at 0030.
2. **If the schema truly must go:** on the image that is still deployed, run `docker compose exec backend alembic downgrade 0029`. Then merge the revert and run `popping-update`. The names people typed are lost.
3. **If that order was missed** and the backend no longer starts: `docker compose run --rm backend alembic stamp --purge 0029`. `--purge` is needed because the reverted image can't resolve revision 0030. The column stays (nullable, ignored). If the PR is re-applied later, run `alembic stamp 0030` instead of upgrading, because the column already exists.
- `popping-update` prunes old images. To run an older build, use a one-shot `POPPING_IMAGE_TAG=sha-<full sha> popping-update` on the command line, never a `.env` pin: compose reads `.env` (`docker-compose.yml:81, 117`), so a pin silently stops every later update.

---

## H1: Test harness groundwork and local loop

**What it is:** the first unit on the frontend track. Test-only, plus one inert attribute. Nothing changes for the owner. Without it, harness bugs look like product bugs inside large PRs, and the local steps promised in "How I'll work" can't run in a cloud session.

**Units:**
- H1: Test harness groundwork and local loop (frontend track). Depends on: H0 merged. Merge condition: CI green and the full smoke plus every flow passing 5 times in a row locally.

### H1: Test harness groundwork and local loop (frontend track)

#### `flows.cjs` helpers
- A header comment defines tap counting:
  - `tap(page, locator)` calls `locator.tap()` (contexts have `hasTouch`) and increments `page._taps`.
  - `taps(page, max, id)` fails the flow when `page._taps > max`.
  - A swipe through `touchDrag` counts as 1. Typing with `fill` is free.
  - A tap-count flow starts from a URL and uses only `tap`, `touchDrag` and `fill`. No `evaluate`, `pushState`, `.click()` or `startPoll` shortcuts. Today's helpers navigate without taps: `openEditor` uses `history.pushState` plus popstate (`flows.cjs:155-161`), and existing flows mix `.click()`, `.check()` and `selectOption`.
- `touchDrag(page, {from, to, steps, stepMs = 50, holdMs = 150})`:
  - opens a CDP session with `page.context().newCDPSession(page)` and sends `Input.dispatchTouchEvent`;
  - returns early when `page.context().browser().browserType().name() !== 'chromium'`, because the WebKit run is report-only;
  - waits two rAFs plus 300 ms before touchStart (a Node-side sleep when the clock is installed), and holds `holdMs` before touchEnd;
  - flows assert outcomes only (POST sent or not, card hidden, transform applied, `defaultPrevented`), never exact scroll offsets.
  - Why: in probes against `/opt/pw-browsers` Chromium, a drag right after `goto` failed to scroll in 4 of 12 runs, and moves 0-16 ms apart flung to different offsets. Moves 50 ms apart with a 150 ms hold gave the same result every time. Chromium also never delivers page touch moves under about 40 px, so the 3 px / 12 px pull rules are unit-tested in `pull.test.ts` (PR6b), not in flows.
- `setHidden(page, bool)` defines both `document.visibilityState` and `document.hidden` and dispatches `visibilitychange`. The only precedent (`flows.cjs:932-933`) overrides `visibilityState` alone. App code reads `visibilityState`.
- An `addInitScript` fetch wrapper records `init.keepalive` per request for `/api/preferences` and `/api/interactions`. Playwright's Request has no keepalive property, so flows assert on this record.
- Stubs for `window.open`, `navigator.share` and the clipboard, applied to every flow. This replaces "Stub `window.open` in all of them" from the old PR1.
- Seen and IntersectionObserver flows (PR3b, PR3c) drive `scroller.scrollTop` / `scrollBy`, which delivered observer callbacks reliably in probes. Touch drags are kept for gestures.

#### Timers in flows
- Flows that assert dwell, batch, toast, poll or arrival timing call `page.clock.install({time: F.NOW})` in `before` and advance with `page.clock.runFor`, following FN (`flows.cjs:806-821`). In a probe, a `setTimeout(…, 1000)` set during load fired 426 ms late in real time, so a 1.2 s real pause has no margin over a 1 s dwell.
- Where real time is kept (for example "a fling marks nothing"), pause at least 2× `SEEN_DWELL_MS`.
- `runFor` also fires the regular visible poll, so GET-count assertions allow for it.

#### `smoke.cjs`
- `WIDTHS` gains `1280: {width: 1280, height: 800, deviceScaleFactor: 1, desktop: true}`. It is not added to `DEFAULT_WIDTHS`. This moves here from the old PR3.
  - For a `desktop` width, `contextOptions` (`smoke.cjs:883-901`) sets `isMobile: false`, `hasTouch: false` and a desktop UA, and page checks skip smoke check C1 (pointer: coarse). Today every width gets `isMobile`, `hasTouch` and `IOS_UA`, so a 1280 flow emulates a touch tablet, and Card's `matchMedia('(hover: hover)')` (`Card.tsx:599`) reports none.
  - App code splits phone and desktop on `isDesktop` (width, `App.tsx:581`) only.
  - Desktop flows use `widths: [1280]` and `wait: 'main.grid article[data-card-id]'`. Without that, `runState` waits for a phone card inside the hidden `main.md:hidden` (`smoke.cjs:87, 987`).
- `run()` receives an optional `shot(label)` that writes `${width}-${state.name}-${label}.png` under `--out`. Today `shot()` runs at `smoke.cjs:1003`, before `state.run` (`:1004-1005`), so a flow PNG shows only the start. Without `--out` nothing is written (`:980`).
- A `--states-only` flag skips flows, for the WebKit report-only step.
- At the end, print the 10 slowest flows.
- The `dashboard` check reads `header[data-app-header] button` (`smoke.cjs:145-149`). The App `<header>` (`App.tsx:2568`) gets the inert `data-app-header`. Today the check passes only because For You hides its ⋯ (`Column.tsx:385`), and PR3b shows that ⋯.

#### `capture.cjs`
- A stateful in-memory preference store for GET (with `since`), PUT and DELETE. PR2b adds PATCH.
  - Today GET `/api/preferences` always returns `{items: []}` (`capture.cjs:181`), and every other method echoes `body.value` (`:182-186`), so a PATCH would get `null` back.
  - Existing flows don't reload, and the only preference mock is FL's `filter_presets` PUT recorder (`flows.cjs:742`), so the store doesn't change current flows.

#### Mutation check (defined here, used by every unit)
For each new flow, and each new behavioural pytest:
1. Name the one product line it guards (for example: drop `onActivate` in Column; remove `FOR UPDATE` in the PATCH route; make `markSeen` skip the batch).
2. Break that line as a scratch edit.
3. Run `npm run build`, then `smoke.cjs <base> --only <flow>` (or the single pytest).
4. Confirm it fails on the expected rule or assertion.
5. Restore with `git checkout -- <file>`, then `git diff --exit-code`.
6. Record "flow → broken line → failing rule" in the unit's notes file (`docs/ux-notes/<unit>.md`).
Gesture and timing flows also run 5 times in a row before merge.

#### `.github/workflows/build.yml` (D29)
- `timeout-minutes: 30` on `frontend-checks` (`build.yml:101-148` has none).
- The WebKit report-only step runs `smoke.cjs … --browser webkit --states-only`. On the last main run, the frontend job took 483 s, of which the WebKit run was 270 s.
- Fix the line-12 comment: PR builds are never pushed (`build.yml:175, 187` and `:225, 237` gate login and push on push events in both image jobs).

#### Local loop
The same recipe is summarised under "How I'll work → Local loop". It is a throwaway test cluster, not an app process. CI stays authoritative. If setup fails, say so in the unit's notes.
- **Backend:**
  - `apt-get update && apt-get install -y postgresql-16-pgvector` (Ubuntu noble universe; apt.postgresql.org is blocked), then `pg_ctlcluster 16 main start`.
  - Create the `popping_test` role (LOGIN SUPERUSER, password `popping_test`) and database, then `CREATE EXTENSION IF NOT EXISTS vector` in it. `conftest.py` and `0001_initial.py` need the extension.
  - `python3.12 -m venv` outside the repo (CI uses 3.12; the container's default `python3` is 3.13), then `pip install --no-deps -r backend/requirements-dev.lock` and `pip install --no-deps -e backend`. CPU torch comes first, as in `build.yml`.
  - `pytest` with the `POSTGRES_*` variables from `build.yml`.
  - D30: in the cloud environment, the setup script does the installs once; a session only starts the cluster.
- **Frontend:**
  - `cd frontend && npm ci`, then `git diff --exit-code package.json package-lock.json`.
  - `npm run lint`, `npm run build`, `node --experimental-strip-types --test 'scripts/unit/*.test.ts'`.
  - `./node_modules/.bin/vite preview --host 127.0.0.1 --port 4173 --strictPort` in the background.
  - `FONTCONFIG_FILE=$PWD/scripts/iphone-audit/fonts-ci.conf PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node scripts/iphone-audit/smoke.cjs http://127.0.0.1:4173 --csp --strict` (see the iphone-audit README, "Matching CI's fonts locally"; C14 differs under the container's default font).
  - WebKit results come only from CI.

#### Tests
- The full strict smoke and every existing flow pass unchanged, 5 times in a row locally.
- One harness flow each for `taps` (a known 2-tap path counts 2 and fails at `taps(page, 1, …)`), `touchDrag` (returns early off Chromium), `setHidden` and the keepalive recorder.
- `dashboard` passes with the `header[data-app-header] button` selector.

#### Risks and rollback
- Test-only. Revert is safe.
- CI time: about 8 min today. Without `--states-only` on WebKit, the roughly 63 new flows would take the frontend job to about 18-20 min.

**Run with:** effort high · plan first: no · review: /code-review high; full strict smoke and every flow 5 times locally.

---

## PR1r: Build stamp and reload on a new build

**What you'll notice:**
- Settings shows "Build 3f9a1c2e · 19 Oct, 14:02".
- After a deploy, the phone moves to the new build by itself the next time you come back after 10 minutes or more away. Nothing reloads while you look at it.
- On desktop, a small "New version · Reload" button appears instead.

**Why (D22):**
- Nothing in `frontend/src` reloads a running bundle. Only `App.tsx:1391` (Reset), `:2528` (LoginPage) and `:2698` (UserBadge) call reload.
- There is no service worker and no lazy chunk that could 404. nginx serves `/static/` as immutable and `/` as no-cache (`frontend/nginx.conf.template:21-33`). So only a cold launch loads a new build, and an iPhone home-screen app is resumed rather than relaunched for days.
- Every "until it reloads" risk in this plan is unbounded without this. The owner also can't tell which build a device runs: `/health` has no version, and the sha exists only as an image label.

**Units:**
- PR1r: Build stamp and reload on a new build (frontend track). Depends on: H1. Merge condition: CI green; after the first deploy, the owner relaunches each device once and confirms the Settings build line in the Device checks list.

### PR1r: Build stamp and reload on a new build (frontend track)

#### Changes
**Build stamp (always):**
- `frontend/vite.config.ts` gets `define: {__BUILD_TIME__: JSON.stringify(new Date().toISOString())}`, declared in a new `frontend/src/build.d.ts`. `vite.config.ts` isn't frozen, `package.json` is untouched, and `define` inlines a literal, so CSP is unaffected.
- Settings shows "Build <8-char id> · <built time>" in its Reset area. PR12a moves it into General.
- The id is a hash of the live document's `/static/` script `src` and stylesheet `href`.

**`lib/buildId.ts` (pure, import-free):**
- `buildIdFromHtml(html)` collects every `/static/…` `src` and `href`, sorts and joins them. A CSS-only change can leave the JS hash unchanged, so all of them count.
- `shortId(id)` gives the 8-character display id.
- The live document's id is read in the hook, from `script[type=module]` and `link[rel=stylesheet]`.

**`lib/useUpdateCheck.ts` (React glue):**
- Runs only when `import.meta.env.PROD`.
- Checks on `visibilitychange` → visible after at least 10 min hidden, and on `pageshow` with `persisted` after the same gap. Time away is measured with `Date.now`, because timers stop while iOS suspends the page.
- Skips when the page hid within about 2 s of opening a story, like C1's guard.
- Fetches `/` with `cache: 'no-store'` (`connect-src 'self'` allows it). If the ids differ, it reloads. This is an inequality test, so a revert also reloads.
- Before reloading:
  - HEAD the new entry script and require a 200;
  - run the pending-save flush, capped at about 3 s. Until PR2b, that is the preferences provider's pagehide flush (`preferences.tsx:555-587`) and the interactions flush (`interactions.ts`). From PR2b on, it is `registerBeforeFlush` plus the prefs flush.
- A `sessionStorage` guard (inside try/catch) allows at most one reload per build id.
- A visible page never reloads. Desktop instead gets a non-modal "New version · Reload" button. It is not `role="status"`, because smoke waits for no `[role="status"]` before every state (`smoke.cjs:990`).

**Later units:** PR3b's C1 return branch runs this update check first. The old risks "until the PWA reloads" (PR1c) and "until it reloads" (PR2b) now read "until the device next returns after 10 min away (PR1r)".

#### Tests
- **Unit:** `buildId.test.ts`: sorting, a CSS-only change, a missing stylesheet, the short id.
- **Flow `flow-reload-on-new-build`:** route only fetch-type requests for `/` to HTML with a different script `src`, and use `setHidden` plus `page.clock` (H1). Then check:
  - no reload under 10 min hidden;
  - one reload after 11 min, with the flush sent first;
  - no second reload for the same id.
  Under `vite preview` the ids match, so other flows are unaffected.
- **Smoke:** the Settings state that holds the build line passes C14 at 320.

#### Risks and rollback
- A reload that ran before the flush, or looped, would lose data. Hence flush first, the HEAD check and the once-per-build guard.
- Revert is safe. Devices then stay on whatever build they last loaded until a cold launch.

**Run with:** effort high · plan first: no · review: /code-review high; reload flow 5 times; owner confirms the build line.

---

## PR1: Tabs and sorting behave

**What you'll notice:**
- Saving never moves you to another tab, and a new phone opens on For You. (PR1b)
- Tapping the current tab scrolls it to the top. (PR1b)
- Newest | Best really reorders a tab. (PR1c)
- Reset keeps saved and hidden stories and saved views. (PR1c)
- Brief shows "Brief needs an AI provider · Set up" when no provider is set. (PR1c)
- Behind the scenes (PR1a): scheduled checks are no longer skipped when the server is busy, the subreddit Test works, and Settings → History shows entries again.

**Complaints:** "multi tapping", "Nothing is quick".

**Units:**
- PR1a: Scheduler defaults, extraction off the event loop, Reddit Test, History (backend track). Depends on: H0. Merge condition: CI green. It fixes live bugs at once, so it goes first on the backend track.
- PR1b: Tab keys, resolveTab, tap-to-top, phone scroller (frontend track). Depends on: PR1r. Merge condition: CI green. As a git revert, PR1b stands alone only until PR1c merges (Rules: Revert and version skew). After that, fix forward or revert newest-first.
- PR1c: Newest | Best, Reset scope, bookmark on save, Brief AI row (frontend track). Depends on: PR1b (file overlap in the columns memo and Column props). Merge condition: CI green.

### PR1a: Scheduler defaults, extraction off the event loop, Reddit Test, History (backend track)

#### Backend
**Scheduler (moved here from the old PR6):**
- `scheduler.py:1795` becomes `AsyncIOScheduler(timezone="UTC", job_defaults={"misfire_grace_time": 120, "coalesce": True})`, so every job gets it: ingest, dynamic, `pref:recompute`, rescore and framing. Today no `add_job` sets a grace, so every job keeps APScheduler's 1 s default (APScheduler 3.11.3, `requirements.lock:16`). Jobs that set their own `coalesce` keep it.
- `brief:daily` (`scheduler.py:1892-1900`) gets an explicit `misfire_grace_time` of about 3600, so a busy or restarting server still sends the day's brief.
- A longer grace only hides the symptom. The cause is that `trafilatura.bare_extraction` runs synchronously inside the async `_extract_one` (`generic_scrape.py:141-143`), up to 10 pages per poll, on the one event loop that also serves the API. Run it through `await asyncio.to_thread(…)`, as `feed_autodiscovery.py:146` already does for `determine_feed`. PR10b's `score_page` later runs inside the same thread call.

**History (`routes/interactions.py`):**
- Delete `type_list.append("dwell")` (`:272-273`), which empties History today because it turns the optional filter into `IN (…, dwell)`.
- Exclude `{dwell, view}` only when no types are given. Explicit types pass through, except `dwell`. Today `exclude_types` applies unconditionally (`:277`, `:306-307`). PR3c makes views frequent.

**Reddit Test (`routes/sources.py:1153-1154`):**
- Today it imports `fetch_subreddit` from `app.sources.reddit`, which has none. The real one is `reddit_client.fetch_subreddit` (`reddit_client.py:677`), and it takes a subreddit name, not a URL.
- `sub = normalize_subreddit(url)` (`sources/reddit.py:41`), then `reddit_client.fetch_subreddit(sub, …, limit=25)`:
  - `reddit_client.is_disabled()` → `unsupported_type`;
  - catch `reddit_client.RedditFetchThrottled` (`reddit_client.py:914`) before the generic `except Exception`, → `network_error`;
  - an empty listing → `not_found`. Private, banned and 403 subs land there too, because `fetch_subreddit` turns `httpx.HTTPError` and `ValueError` into `[]` (`reddit_client.py:703-710`).

#### Tests
- **pytest `test_ingest_misfire_grace.py`** (no_db): build the scheduler and assert the job defaults on the jobs as built, plus `brief:daily`'s grace. If a live blocked-loop test is kept, it uses a private `AsyncIOScheduler` with `shutdown(wait=False)` in a `finally` block.
- **pytest `test_interactions_history_types.py`** (db): no types returns `never` and `bookmark` rows but no `dwell` or `view`; `?types=view` returns views; `?types=dwell` returns none.
- **pytest `test_source_test_reddit.py`** (no_db): monkeypatches `reddit_client.fetch_subreddit` and `is_disabled`, as `test_reddit_throttle_cooldown.py:147-148` does, and covers disabled, throttled, empty and a listing.
- The existing `generic_scrape` tests stay green with the `to_thread` move.

#### Risks and rollback
- With views excluded by default, Settings → History's Read group (`Settings.tsx:1289-1295`) is empty. PR1c drops that group; Activity covers views in PR12a.
- `coalesce=True` means a job that missed several runs runs once. That is the intent.
- Revert is safe.

**Run with:** effort high · plan first: no · review: /code-review medium; pytest.

### PR1b: Tab keys, resolveTab, tap-to-top, phone scroller (frontend track)

#### Changes
**`lib/columns.ts` (new):**
- Keys: `meta:brief`, `meta:saved`, `meta:foryou`, `meta:search`, `meta:filter` (removed in PR8b) and `cat:<name>`, plus `colKey` / `parseColKey`.
- `resolveTab(tabs, wanted, lastIndex, {loaded, filterActive})`:
  - If the wanted key exists, use it.
  - If it is missing while the tab list isn't loaded yet, or under `meta:filter`, show the neighbour at `lastIndex` but keep the stored key.
  - If it is missing once columns are loaded, `selectTab` the neighbour that is shown, so that becomes the stored key. Keeping a missing key would make the phone jump back to Saved or For You when they reappear (today Saved and For You are added only when non-empty, `App.tsx:983-988`), which is the bug this PR fixes.
  - If nothing is stored, pick the first tab that is not Brief and not empty, preferring For You.
- Later, everything that walks tabs uses PR7a's `orderColumns().shown`.

**`storage.ts`:** add `mobileTab`. `mobileColLast` is read once, to migrate.

**`App.tsx`:**
- Columns gain `key` (977-992, 1028-1042).
- `mobileTab` replaces `mobileCol` (557), `loadMobileCol` (147-157), `followTabRef` (561, 1546-1552, 2233-2235) and the `mobileCol` part of the clamp effect (1553-1564).
  - Keep the desktop `selectedColumnIndex` clamp (1556) as its own effect: `useEffect(() => { if (columns.length) setSelectedColumnIndex((i) => Math.min(i, columns.length - 1)) }, [columns.length])`.
  - `selectTab(key)` (a `useCallback`) replaces `selectMobileCol` (562-565, called at 2382, 3025 and 3051). Only `selectTab` and `jumpToCategory` write the stored key.
  - Delete `mobileColRef` (2226-2227). It is dead once `followTabRef` goes, and lint flags it.
  - The phone Column's `selectedId` (3099) compares `selectedColumnIndex` with the index of the resolved key.
- **`firstRefreshDone`:** a state set in `refresh()` after the first applied response, success or error.
  - Nothing resolves until `prefs.loading` is false **and** `firstRefreshDone` is true. Otherwise a stored index would migrate against an empty list and overwrite the saved tab.
  - It replaces a `columns.length > 0` gate, because from PR7a Saved and For You are always present, so `columns.length` is at least 2 before anything loads.
  - The first-load skeleton renders while `!firstRefreshDone`.
- A stored index migrates to `columns[i].key`, and −1 becomes `meta:brief`.
- **Phone scroller element:**
  - App holds the phone list element in state: `const [phoneScroller, setPhoneScroller] = useState<HTMLElement | null>(null)`.
  - The phone Column becomes `<Column key={tabKey} scrollerRef={setPhoneScroller} …>` (3088). Pass the setter itself, not an arrow: slice19's `<Column\b[^>]*>` regex (`test_frontend_slice19_empty_column_source_status.py:138`, which also asserts at least 2 Column call sites at `:139`) stops at the first `>`, so an arrow before `sourcesByName=` fails it.
  - Column attaches it as a callback ref on its list (`Column.tsx:493`) and marks that list `data-scroller`. That list is the phone's real scroller. The App wrapper at 3077 only scrolls the Brief tab, because the Column `<section>` is `h-full`. Only the phone list carries `data-scroller`.
  - Hooks that need the scroller take the element as an effect dependency, so they rebind on every keyed tab remount: `useSeenObserver(isDesktop ? null : phoneScroller)` in PR3c and `usePullToRefresh(phoneScroller)` in PR6b.
- Tapping the active tab scrolls the list to the top (instantly under reduced motion).

#### Tests
- **Unit `columns.test.ts`:**
  - the neighbour fallback: the key is kept while loading or under `meta:filter`, and the neighbour becomes the stored key once loaded;
  - index migration, and −1 → `meta:brief`;
  - no migration before `firstRefreshDone`, including while meta tabs are present;
  - a fresh device skipping an empty Saved and Brief.
- **pytest:** slice19 keeps its literal and its `<Column` regex (`:138`).
- **Flows:**
  - `flow-tab-stays-on-star` (News, because the fixtures have no tech tab).
  - `flow-unsave-last-saved`: unsave the only Saved story, then save from the tab shown; the tab stays put.
  - `flow-tap-tab-scrolls-top`.
  - FM stays green.

#### Risks and rollback
- Revert is safe. The old build opens tab 0.
- As a git revert, PR1b stands alone only until PR1c merges (Rules: Revert and version skew). After that, fix forward or revert newest-first.

**Run with:** effort high · plan first: no · review: /code-review high; 390px screenshots.

### PR1c: Newest | Best, Reset scope, bookmark on save, Brief AI row (frontend track)

#### Changes
**`lib/sorting.ts` (new, pure):**
- `entryTime = published_at ?? fetched_at`.
- `byNewest`: dated before undated, then by id.
- `byBest`: `composite_score` descending, then `byNewest`. It applies only to category and filter tabs, where `/api/entries` already orders by `composite_score`.
- For You's Best keeps the server order: a stable sort on the original index. `routes/foryou.py:158-173` sorts by `composite_score` × a convergence multiplier but returns the raw `composite_score`, so a client re-sort would throw the boost away.
- `defaultSort(key)`: `'best'` for `meta:foryou`, `'newest'` otherwise.
- `normalizeSort(sort, key)`: `top`, `oldest` and unknown values become `newest`. On `meta:foryou`, a stored or missing `top` / `oldest` becomes `best`, because For You never honoured a stored sort (`App.tsx:1064` skips it, and `Column.tsx:385` hides its ⋯). Today `top` has no visible effect anywhere, because both sections re-sort newest-first (`App.tsx:1196-1197`).
- `prefsFor(columnPrefs, key)`: the stored row merged over a per-tab default (sort from `defaultSort`). App's `prefsFor(key)` replaces `columnPrefs[…] ?? DEFAULT_PREFS` at `App.tsx:1052, 2985` and `3137-3138`, and is used by PR7b's sheet. A spread write can then never store `'top'`.

**Types:**
- `ColumnPrefs.sort` and `DEFAULT_PREFS` live in `Column.tsx:11-21` (today `sort: 'top'`, imported by App). `ColumnPrefsValue.sort` (`preferences.tsx:97`) mirrors it. Both gain `'best'`.
- Default prefs objects stay module constants, because Column's memo compares prefs by reference (`Column.tsx:890-915`).

**`App.tsx`:**
- The `columns` memo (1049-1073) sorts once with `normalizeSort(prefs.sort, key)`. Saved is never sorted. `minScore` is ignored. The age filter is unchanged until PR4d.
- In `sectionsByColumn` (1162-1201), delete `fresh.sort` / `past.sort` (1196-1197).
- `applyPreset` (1860-1873) keeps `setActiveSources` and stops applying `preset.columnPrefs`. `savePreset` and `updatePreset` (1875-1891, 1915-1930) keep capturing them, so the stored data stays the same for rollback, and PR8a drops them at conversion (D8). Update the comment above `applyPreset`. Otherwise the chips keep overwriting the new Sort, and from PR4d the window, until PR8b.
- Keyboard `b` (2040-2067) records `bookmark` only when it saves.
- **`resetLocalState` (1328-1393, keys 1356-1361):**
  - Stop deleting `starred_entries`, `hidden_entries` and `filter_presets`.
  - Delete the `read_entries`, `last_viewed`, `column_prefs` and `column_sections` rows, plus `history_group_by`.
  - Update the copy at `Settings.tsx:486-498`. Its "on this device" wording is inaccurate today, because Reset already deletes server preferences.
- **LLM status lives in App:**
  - App holds `llmConfigured: boolean | null` and fetches `api.llmStatus()` once after `authProbed` (568), again when `settingsOpen` (489) goes from true to false, and on visibility resume.
  - `null` means loading or failed, and shows the normal buttons.
  - App passes it, with `onOpenSettings`, to both BriefCard sites (2759, 3079), and uses it for the header Brief button (2690-2697). A status fetched inside BriefCard would stay stale after "Set up" until a reload, and the header button lives in App, so it can't read BriefCard's state.

**`Column.tsx`:**
- Sort becomes a 44px Newest | Best segmented control, showing `normalizeSort(prefs.sort, key)`. It is hidden on `meta:saved`, where it would do nothing.
- The Minimum score slider (434-461) goes.

**`Card.tsx`:** ☆ (1416-1439) and the menu's Save (778-788) record `bookmark` only when the story was not already saved.

**`BriefCard.tsx`:**
- Gains `llmConfigured` and `onOpenSettings` props (Props 19-40, signature 327).
- With `llmConfigured === false`, all four generate buttons (`generate-compact` 537, `generate` 587, `regenerate-compact` 649, `regenerate` 725) and the header Brief button (`App.tsx:2690-2697`) become a 44px "Brief needs an AI provider · Set up" that calls `onOpenSettings('llm')`.
- `Settings.tsx:405-412` already sits on the AI tab and disables itself, so it is unchanged.

**`Settings.tsx`:** History drops its Read group (1289-1295), which PR1a's default leaves empty.

#### Tests
- **Unit `sorting.test.ts`:**
  - `byNewest` and `byBest`, including undated stories;
  - For You keeps the server order: a convergence-boosted entry with a lower raw `composite_score` stays first;
  - `normalizeSort` per key;
  - `prefsFor`: For You with no row resolves to Best; a window or sort write on For You with no row stores `'best'`, never `'top'`.
- **Flows:**
  - `flow-sort-best`. It also taps a preset chip: no `column_prefs` PUT is sent, and the tab keeps its sort.
  - `flow-brief-needs-ai`:
    1. Route `/api/llm/status` to `configured: false`. Check the expanded and collapsed no-brief states (the phone BriefCard starts expanded, `App.tsx:3085`).
    2. Route `/api/brief/latest` to one brief. Check both regenerate states (`capture.cjs:173` returns `[]` today).
    3. Tap Set up, flip the mock to configured, close Settings, and assert Generate is back.
    4. Check the header Brief at 1280 with `page.setViewportSize`. The sub-row is `hidden sm:flex` (`App.tsx:2676`), so it can't be reached at 390.
  - `flow-reset-keeps-saves`: no DELETE of starred, hidden or presets.
  - FM stays green.
- **Smoke:** `column-popover`, plus a new `brief-needs-ai` state at 390, 320 and 312, where the 44px label risks C11 and C14.

#### Risks and rollback
- An old bundle reads `best` as oldest-first until the device next returns after 10 min away (PR1r).
- A device still on a pre-PR1 bundle still deletes saves, hides and presets if Reset is pressed there. PR1r bounds how long that lasts. No server change is needed, because Reset is deliberate and rare.
- Revert is safe.

**Run with:** effort high · plan first: no · review: /code-review high.

---
## PR2: Preferences sync by change, not by overwrite

**What you'll notice:**
- Something saved on the laptop shows as saved on the phone within a minute.
- Two devices never undo each other's reads, saves or hides.
- Leaving the app right after acting loses nothing.
- After PR2c, a days-old copy of the app on another device can no longer wipe your saves. It shows "Couldn't save" instead.

**Why first:** PR3 writes a seen mark about once a second. Today each write PUTs the whole list (`preferences.tsx:524-550`). App builds those lists from refs that are mirrored in effects (App.tsx:765-781), so two calls in one tick pass stale lists. A second device, or a suspended page, overwrites the result.

**Units:**
- **PR2a: Preference PATCH routes** (backend track). Depends on: nothing functional; it follows PR1a in the backend track. Merge condition: CI green and the review step. It is additive with no caller, so it merges alone, and reverts alone until PR2b merges.
- **PR2b: prefSync client and sync rules** (frontend track). Depends on: PR2a merged (cross-track), PR1c in track order (file overlap only: `preferences.tsx` and App.tsx, no functional edge on PR1). Merge condition: CI green and the review step.
- **PR2c: Refuse whole-list PUTs of set keys** (backend track, floating). Depends on: PR2b deployed, and the Device checks list showing both devices on the PR2b build stamp (D23). It doesn't hold the backend track.

### PR2a: Preference PATCH routes (backend track)

#### Backend (routes/preferences.py)

**Key routes:**
- Declare GET, PUT, DELETE and the new PATCH as `/preferences/{key:path}` (today plain `{key}` at `routes/preferences.py:186, 219, 279`).
  - Categories can contain `/`: `_validate_category` checks only empty, length 40 and reserved names (`sources.py:335-355`), and `lib/categories.ts:91-98` checks only empty, length and reserved names (no character rule).
  - The client sends `encodeURIComponent(key)` (`preferences.tsx:566`). Uvicorn decodes `%2F` before routing, and Starlette's `{key}` matches `[^/]+`, so today a PUT to `read_entries:News/Tech` gets a silent 404 and every seen mark for that tab is lost.
- `_validate_key` (`routes/preferences.py:131-152`) keeps enforcing the allowlist, so `{key:path}` is not a security change.
  - Switch `_PREFERENCE_KEY_RE.match` to `fullmatch`. With `match`, the `$` at the end of the pattern (107-128) accepts a trailing newline.
  - Return 400 when `len(key) > 60`. The column is `String(60)` (`models.py:430`), so a longer key is a Postgres `DataError` and a 500 today.
- **New suffixed namespace `seen:<category>`** (cap 1000, PATCH-only). Add `"seen"` to the suffixed names in `_PREFERENCE_KEY_RE` (107-128) and to the set-key list below. PR3b writes new marks there. Old bundles keep PUTting 200-id whole lists to `read_entries:<col.name>` (App.tsx:2161, `preferences.tsx:594-606`), which is the same string as a per-category `read_entries:<category>` key, so new marks must not share it.

**`PATCH /api/preferences/{key:path}`:**
- **Allowed keys:** set keys only: `starred_entries`, `hidden_entries`, `read_entries:*`, `seen:*` and `voted_entries`. `muted_sources` joins in PR5b. Other keys return 400.
  - `read_entries:*` stays a PATCH key because PR2b writes it until PR3b moves new marks to `seen:*`. Mark all and Reset (PR3b, PR1c) cover both prefixes.
- **Body:** a Pydantic model with `extra='forbid'`:
  - `{add?: int[], remove?: int[]}` for the list keys. Each id is an int from 1 to 2**63-1, and each list is at most the key's cap long.
  - `{set?: {"<id>": "up" | "down" | null}}` only for `voted_entries`: at most 2000 keys, each a digit string.
  - A wrong op/key pairing (for example `set` on a list key, or `add` on `voted_entries`) returns 422.
- **One transaction:**
  1. `INSERT … ON CONFLICT DO NOTHING`
  2. `SELECT … FOR UPDATE`
  3. coerce the locked row's existing value: a non-list (or, for votes, non-dict) becomes empty, and non-int elements are dropped. PUT stores opaque JSON (`routes/preferences.py:246-266`), so without this a malformed row gives a 500 on every PATCH.
  4. apply the change: `remove` before `add`
  5. trim down to the cap, smallest entry id first, matching `trimVotedEntries` (`preferences.tsx:814-822`). Entry ids are monotonic, so this tracks story age. The client's `pruneIds` sends the ids it drops in the same PATCH's `remove`.
  6. write `value` with `updated_at = func.now()`
  7. return `UserPreferenceOut`
- **Caps:** `read_entries:*` 1000, `seen:*` 1000, hidden 1000, starred 1000, voted 2000. They are pinned against `preferences.tsx`: `MAX_READ_PER_KEY` is new (it covers both `read_entries:*` and `seen:*`), and `MAX_HIDDEN`, `MAX_STARRED` and `MAX_VOTED` are at 793-797. The server trim is a backstop, because clients prune first (PR3b).

**`GET /api/preferences?since=<iso>`** (additive, 155) returns only rows with `updated_at > since`. Without `since` it returns every row, as today.
- `since` is an `AwareDatetime`. A naive timestamp returns 422.
- `func.now()` is the transaction's start time. Two PATCHes on different keys don't block each other (FOR UPDATE locks one row), so the one that started first can commit after a poll has already moved past its timestamp. The client covers this with a 60 s overlap (PR2b, sync rule 8); the server stays on `func.now()`.

#### Tests
- **pytest:** `test_preferences_delta.py` (db) covers:
  - **FOR UPDATE, deterministically:** session A holds `SELECT … FOR UPDATE` on the row. Start PATCH B as a task and assert it isn't done after 0.2 s. Commit A with an added id, await B, and assert the union. The test must fail with `with_for_update()` removed (the unit's mutation check).
  - a first-write race: two concurrent PATCHes on a key that doesn't exist yet (the `INSERT … ON CONFLICT` path)
  - the cap holding, with the smallest ids trimmed first
  - `remove` applied before `add` in one body
  - non-set keys returning 400; a key longer than 60 returning 400; a trailing newline returning 400
  - every 422: an unknown field, an id of 0 or 2**63, a list over the cap, `set` on a list key, `add` on `voted_entries`, a vote value other than up/down/null, a naive `since`
  - a malformed existing row (a string, a dict on a list key, mixed elements) coerced, not a 500
  - anonymous users
  - suffixed `read_entries:*` and `seen:*` keys
  - `since`, including two overlapping transactions that commit in reverse order: a since-GET sent with the 60 s overlap returns both rows
- **pytest (slash keys):** a test that sends `read_entries:News/Tech` and `seen:News/Tech`, percent-encoded, through PUT, PATCH, GET and DELETE, and gets 200 / 204 and the row back. Replace the "NOT covered here" note at `test_preferences_anonymous.py:66-72` (it says a `/` in a column id 404s at route matching) with a pointer to it.

#### Risks and rollback
- Revert is safe while nothing calls PATCH, because the route, the `{key:path}` change and the `since` parameter are additive.
- Once PR2b merges, a PR2a revert fails PR2b's calls. Revert newest-first (Rules: "Revert is safe"). After a revert, a resumed page keeps the newer bundle and its PATCHes get 405; PR2b's sync rule 2 keeps those deltas queued until the device reloads.

**Run with:** effort high · plan first: no · review: /code-review high + /security-review; FOR UPDATE test with the lock removed must fail.

### PR2b: prefSync client and sync rules (frontend track)

#### Frontend

**`lib/prefSync.ts` (pure):**
- `isSetKey`, `diffIds`, `foldDelta`, `applyIdDelta` and `applyVoteDelta`.
- `mergeIncoming(prev, rows, pending, {full})` works on raw rows and reuses references for unchanged keys, so a resync doesn't re-render every Column (`Column.tsx:890-915`). Sync rule 6 defines full and since modes.
- `pruneIds(ids, keep, cap)` drops ids absent from `keep` first, then the smallest. It returns the dropped ids too, so they go out in the same PATCH's `remove`.

**`preferences.tsx`:**
- **New calls:** `addIds(key, ids)`, `removeIds(key, ids)` and `setVotes({[id]: 'up' | 'down' | null})`. Each uses a functional `setState` and queues its delta directly, so two calls in one tick both land.
- **The whole-list setters for set keys go.** These handlers move to the new calls:
  - `hideEntry`, `restoreHiddenEntry`, `toggleHideEntry`, `toggleStarEntry`, `markEntryRead`, `unmarkEntryRead`, `markColumnRead`, `setEntryVote`, `onHideAllDownvoted`, and every Undo.
  - The Settings prop closures: `onRestoreHidden` (App.tsx:3191) → `removeIds('hidden_entries', [id])`; `onRestoreAllHidden` (3197) → `removeIds('hidden_entries', <current ids>)`; `onUnstarAll` (3203) → `removeIds('starred_entries', <current ids>)`, and its Undo → `addIds` of the cleared ids; `onUnstarEntry` (3220) → `removeIds`.
  - `unmarkColumnRead` (App.tsx:1659) → `removeIds(read_entries:<col>, ids)`, and `toggleEntryRead` (1737) → the new calls. PR3b deletes `toggleEntryRead` later and turns `unmarkColumnRead` into Mark all's Undo (`removeIds` of the added ids under each `seen:<category>` key).
  - "Clear all" is `removeIds` of the ids held now; Undo re-adds them with `addIds`. Both are accepted in order.
  - Delete the set-key aliases in App.tsx:713-740 (`setReadEntries`, `setHiddenEntries`, `setStarredEntries`, `setEntryVote`), the `MAX_HIDDEN` / `MAX_STARRED` imports (App.tsx:61-62), and the whole-list setter fields of `PreferencesContextValue` (`preferences.tsx:371-430`).
- **Caps leave App:** the App-side slices go (App.tsx:185 `MAX_PER_COLUMN`, 1728, 1769, 1809, 1847, 2161, 3238), and so does `setReadEntries`'s 200 slice (`preferences.tsx:598-602`).
- **Flush:** set keys go out as PATCH in a 1 s batch, and other keys as PUT in a 250 ms batch. Today `scheduleSync` (542-550) is a single fixed batch, not a debounce. Responses are adopted through `mergeIncoming`.
- **Leaving:** `visibilitychange: hidden` joins `pagehide` / `beforeunload` (552-586) and sends the same requests with `keepalive`. In the home-screen app, opening a story fires only `visibilitychange`. Each keepalive PATCH carries one key's delta, so bodies stay far under the 64 KB keepalive limit.
- **`registerBeforeFlush(fn)`:** a provider hook. The hidden, pagehide and beforeunload handlers run every registered function before they read the queue. PR3b's and PR3c's seen collectors drain through it, and PR1r's reload path calls the same flush (PR1r ships first against today's flush; PR2b keeps that entry point working).
- **`resync()`:**
  - Each visible poll sends `GET ?since=` (sync rule 8). Resume sends a full GET, which is also how deletions arrive. Only one runs at a time, and requests coalesce (sync rule 7). The first-mount GET (`preferences.tsx:468-503`) goes through the same path, so it counts toward "one at a time".
  - Queued deltas and in-flight ones (sent but not yet acknowledged) are re-applied on top.
  - A GET sent before a key's latest write was acknowledged is ignored for that key (sync rule 5). This uses request sequence numbers, not clocks, and mirrors `sourceChangesRef` for feeds (App.tsx:474, 1236-1240, 1262-1275).
- **`seedFromLocalStorage` (`preferences.tsx:250-365`) stops writing.** Delete its PUT side (the `api.setPreference` calls at 279, 320 and 339). Keep the `safeRemoveItem` calls for the legacy keys and setting the seed flag (348; the key is `SEED_FLAG_KEY`, 220). Today it runs whenever the flag is unset, and Reset clears every `popping.*` key including the flag (App.tsx:1335-1341), so it can PUT whole lists over the server's rows. If any seeding stays, set keys go through `addIds` (a union), and non-set keys are written only when the first GET shows no row for that key.
- Old bundles keep using PUT, which is still accepted until PR2c.

**Sync rules** (all in `preferences.tsx`, with the pure parts in `prefSync.ts`):
1. At most one PATCH in flight per key. The next batch for that key waits for its ack.
2. On a network error, 5xx, 404 or 405: fold the failed delta back ahead of newer pending deltas with `foldDelta` (later ops win), and keep it in the re-apply overlay. A 404 or 405 means the server was rolled back. Retry after 2, 4 and 8 s and again on resume, and toast only once retries are exhausted. Never reuse `put()`'s "skip if a newer value is queued" rule (`preferences.tsx:524-541`) for set keys: a newer delta doesn't contain the older one.
3. On 400, 409 or 422: drop the delta, remove it from the overlay, and toast.
4. An in-flight record clears when its request settles. A deadline of about 30 s (measured with `Date.now`) or the next resume is the backstop, because a keepalive or suspended fetch may never settle on iOS. After the deadline the record is dropped and the next full GET is adopted.
5. Stale-GET protection covers every key with a queued or in-flight write, PUT or PATCH, through one per-key write sequence. A pending PUT value stays on top until its ack is adopted. This covers PUT keys such as `column_prefs:*`, and later `category_layout` (PR7a) and `custom_tabs` (PR8a).
6. `mergeIncoming(prev, rows, pending, {full})` works on raw rows.
   - In full mode, a key missing from the response is deleted unless a queued or in-flight write covers it.
   - In since mode, missing keys are left alone.
   - An empty array or object from the server is a real value. Don't reuse the `length > 0 ? server : seed` idiom of `mergeStateFromServer` (`preferences.tsx:890-923`).
7. `resync({full})` coalesces: a full request made during a since-GET runs right after it, never dropped. Resume issues one full resync.
8. The watermark advances only from GET responses, never from PATCH or PUT responses, and is kept as the server's `updated_at` string (a `Date` round-trip cuts microseconds). Polls send `since = watermark − 60 s`. `mergeIncoming` is idempotent and reuses references, so rows sent again cost nothing.
9. `prefs.discardPending()` clears the queue, its timers and pending retries, and awaits in-flight requests. `resetLocalState` (App.tsx:1328-1393) calls it before the DELETEs; today the reload's `pagehide` flush would send queued writes after the DELETEs and re-create the rows.

**`api.ts`:** add `patchPreference`, and an optional `since` on `getPreferences(since?)` (api.ts:828).

**`App.tsx`:** `refresh()` and the visibility handler (1443-1451) call `prefs.resync()`.

**`capture.cjs`:** H1 built the stateful preference store (GET with `since`, PUT, DELETE). PR2b adds PATCH to it: apply `remove` before `add` on list keys and `set` on `voted_entries`, bump `updated_at`, and return the row. It also adds the fault hooks its flows need: a PATCH that answers 503 once, and a PATCH that answers 405.

#### Tests
- **Unit:** `prefSync.test.ts` covers:
  - two adds in one tick
  - add then remove
  - two devices
  - an in-flight delta surviving a stale GET
  - a PUT key in flight surviving a stale full GET
  - reference identity
  - caps, votes and prune order (smallest id first; the dropped ids in `remove`; client and server holding the same set after a prune)
  - a failed delta folded back ahead of newer ones
  - a deleted key and an empty list in full mode; a missing key left alone in since mode
  - a full request during a since-GET running right after it
  - a PATCH response not moving the watermark
  - an in-flight record cleared by its deadline
  - legacy localStorage lists plus server rows producing no set-key PUT and removing no server id
- **Flows:**
  - `flow-prefs-resync`: a GET answered with the old set while a PATCH is in flight leaves the story hidden.
  - `flow-visibility-flush`: a save, then hidden, then a keepalive PATCH within the 1 s batch.
  - `flow-prefs-retry`: the first PATCH answers 503 and the second succeeds; both ids end up stored.
  - `flow-prefs-405`: a PATCH answering 405 keeps the delta queued and shows no "saved" state change.
  - `flow-reset-keeps-saves` (PR1c) is extended: queue a delta, Reset, and assert no PATCH or PUT after the DELETEs.
  - FL's `filter_presets` PUT (`flows.cjs:742`) stays a PUT.

#### Risks and rollback
- A cached old bundle can still overwrite set keys with a whole-list PUT until the device next returns after 10 minutes away (PR1r). PR2c closes this.
- A PR2b revert leaves PR2a's routes unused, which is safe. Once PR7a merges, PR2b no longer reverts alone (Rules: "Revert is safe"); fix forward or revert newest-first.
- After any deploy or revert, relaunch every device and check the Settings build line (D21, PR1r).

**Run with:** effort xhigh · plan first: yes · review: review agents: ordering and merge semantics; iOS lifecycle; tests and mutation; /code-review ultra if the plan allows.

### PR2c: Refuse whole-list PUTs of set keys (backend track, floating)

**Why:** the old provider GETs once per lifetime (`preferences.tsx:468-503`), so a resumed bundle that is days old holds stale lists. Its next save, hide or vote PUTs the whole list (`setHiddenEntries` 671-680, `setStarredEntries`, `setEntryVote`, through `put()` 524-541), the server upserts it as given (`routes/preferences.py:246-266`), and PR2b's full resync then adopts it, wiping saves made since then on every device. Relaunch instructions only cover the devices the owner remembers, and a desktop tab left visible never triggers PR1r.

#### Backend (routes/preferences.py)
- `PUT /api/preferences/{key:path}` returns 409 "Popping was updated. Reload to keep saving." for set keys: `starred_entries`, `hidden_entries`, `voted_entries`, `read_entries:*` and `seen:*`.
  - `muted_sources` is included if PR5b merged first; otherwise PR5b adds it to the list.
  - The list is one `SET_KEYS` constant, shared with the PATCH allowlist and pinned by a test.
- PATCH and DELETE are unchanged.
- A stale bundle's `put()` then retries once and shows its existing "Couldn't save" toast (`preferences.tsx:524-541`) instead of overwriting.

#### Tests and mocks
- Rewrite `test_preferences_anonymous.py:30-85`, which PUTs `read_entries:*`, to use `column_prefs:*` or PATCH.
- A 409 test per set key, and a test that pins `SET_KEYS`.
- `capture.cjs`'s PUT mock answers 409 for set keys.

#### Merge condition
- Merge only after PR2b is deployed and the Device checks list shows both devices on the PR2b build stamp (D23, D32). PR2b must already have removed the legacy seed's set-key PUTs.

#### Risks and rollback
- After PR2c, an old bundle loses only its own unsaved action until it reloads.
- Revert is safe: PUT of set keys is accepted again, and PR2b never sends it.

**Run with:** effort high · plan first: no · review: /code-review medium; merge condition above.

---
## PR3: Seen means used up; a real end

**What you'll notice:**
- Opening a story or scrolling past it on the phone uses it up. Nothing moves or dims while you read.
- Each tab shows new stories, then "You're all caught up", then "Earlier (N)" folded. A row under the marker opens the next tab that has new stories (C8).
- Cards you flicked past without reading leave the end row reading "N not seen · Mark as read" (D24).
- Badges count new stories (99+). The "+N new" chip and ✓ are gone.
- Long tabs load 30 at a time as you scroll.
- ⋯ → Mark all as read.
- On desktop the page scrolls as one, and opening a story uses it up (C2).
- The first deploy doesn't open every tab at 99+: stories fetched more than 48 hours before a device's first load count as already seen (D33).

**Complaints:** "the fresh vs not … confusing", "upvote, downvote, star, read … all confusing", "multi tapping".

**Units:**
- **PR3a: Desktop single scroller and batching** (frontend track). Depends on H1 (the 1280 desktop width) and PR1b (the phone scroller element held in state). Merges when CI is green and its review passed. Safe to deploy alone: it fixes a desktop layout bug that exists today and renders the same cards in batches.
- **PR3b: Read model: open marks seen, marker, Earlier, badges** (frontend track). Depends on PR1r (C1's return runs its build check), PR1b, PR1c, PR2a (the `seen:` prefix in the allowlist) and PR2b (`addIds` / `removeIds` and `registerBeforeFlush`), and on PR3a (the sentinel root and batching). Merges when CI is green and its review passed. Safe to deploy alone: a story is then used up only by opening it or by Mark all. The owner deploys it together with PR3c (D33).
- **PR3c: Seen on scroll** (frontend track). Depends on PR3b. Merges when CI is green and its review passed. Device gate 1 follows it: frontend units after PR3c don't merge until the owner passes Gate 1 (D21). The backend track continues.

Every removal of ✓, swipe-right, `m`, the context-menu "Mark as read", the header tap and the chip lands in PR3b, so there is never a second way to mark a story read. PR3c only adds scrolling past as a way to use one up.

### PR3a: Desktop single scroller and batching (frontend track)

#### Desktop scroller (App.tsx)
- Today the grid `<main>` (App.tsx:2937) is a flex-1 scroll container whose automatic minimum height is 0. A For You row taller than the window squeezes the grid down to its 32px padding, and the document scrolls instead (2558, 2848).
- **Structure (replaces "wrap 2757-2990").** Under `isDesktop`, one `div[data-scroller].flex-1.min-h-0.overflow-y-auto.overscroll-contain` holds BriefCard (2759), PresetChips (2769, removed in PR8b) and the existing branch: search results, the skeleton or empty state, the For You row, FramingWatch and the grid. A literal wrap of 2757-2990 would cut across that ternary and the fragment the phone `<main>` shares (3000), so the wrapper sits around the branch instead.
- Inside it, the desktop search `main` (2778) and the grid (2937) drop their own `flex-1 overflow-y-auto`.
- The phone keeps PresetChips and its own branch unchanged.
- Among Column lists, only the phone list carries `data-scroller` (PR1b). On desktop the single `data-scroller` is App's wrapper.

#### Batching (Column.tsx)
- The list renders its first 30 cards, then a `[data-more]` sentinel that adds 30 more. Today every card renders at once.
- **Sentinel root.** Column takes its sentinel root as a prop (`sentinelRoot`): its own list on the phone (the element PR1b holds in state), App's desktop wrapper on desktop. The sentinel is observed with that root and a 600px bottom `rootMargin`. A margin on the viewport root doesn't reach past the list's clipped edge, and on desktop each Column list grows to fit its content inside a `contents` wrapper (2937-2941), so a list root would always contain the sentinel.
- Rows added inside a Column are never `<section>` (smoke asserts exactly one `main section`, `smoke.cjs:153`).
- `sentinelRoot` joins `_columnPropsEqual` (Column.tsx:890-915) and slice21's parametrize list (`test_slice21_column_memo.py:95-110`).

#### Tests
- **Flows:**
  - `flow-desktop-layout` (`widths: [1280]`, the desktop width H1 adds): with 25 For You cards the grid is taller than 300px, and a 70-story column renders 30 cards before any scroll and 60 after scrolling the wrapper. PR3b adds a Mark all step.
  - `flow-auto-append`: a route returns 70 stories, and the list grows to 30, then 60, then 70. PR3b adds the marker at the end.
- **pytest:** `test_slice21_column_memo.py` adds `sentinelRoot`.

#### Risks and rollback
- Desktop layout changes for everyone at once. The 1280 flow and screenshots cover it.
- Revert is safe.

**Run with:** effort high · plan first: no · review: /code-review high; the 1280 flow.

### PR3b: Read model: open marks seen, marker, Earlier, badges (frontend track)

#### Read model
- **Seen** is the union of every `seen:*` row and every legacy `read_entries:*` row (today's `globalReadIds`, App.tsx:1085-1091, extended to both families).
- **Where marks go:** each new mark is stored under the story's category, as `seen:<category>`, wherever it happened. `seen:` is a fresh, PATCH-only prefix that PR2a adds to the allowlist and the set-key list (cap 1000). Old bundles still PUT 200-id whole lists to `read_entries:<column name>` (App.tsx:2161), which is the same string as a per-category key, so new marks never go there. Legacy `read_entries:*` rows are only read, never written, by new code.
- Category names can contain `/`; PR2a's `{key:path}` routes accept them.
- **Writes in PR3b:** opening a story calls `addIds('seen:<category>', [id])` synchronously inside `onOpenEntry`, with no collector, so the id is in PR2b's queue before the page hides. Mark all calls `addIds` once per key. PR3c adds the scroll collector.
- **Pruning:** a key over 1000 is pruned with `pruneIds`, which drops ids absent from the keep set first, then the smallest id first (entry ids are monotonic, so this follows story age and matches `trimVotedEntries`, preferences.tsx:814-822). The keep set is every fetched id: entries, For You, Saved, and the wider-tab rows (PR4c and PR4d add the Saved-by-id cache and every `extraByTab` row). The dropped ids go in the same PATCH's `remove`, which the server applies before `add`.
- **Events:** opening records `click`, as today. No `dwell` is sent: it came only from ✓ and swipe-right, which go here. Delete `mountTime` (Card.tsx:387-390), whose only use is the dwell value in `commitMarkRead` (550). The on-mount `view` stays until PR3c moves it.
- **First-deploy baseline (D33):** each device stores `firstRunAt` in `storage.ts` (through `safeGetItem` / `safeSetItem`, so a throwing storage is fine). Entries fetched more than 48 h before `firstRunAt` count as seen inside the partition. Nothing is written to the server.
- **Reset** (`resetLocalState`, as PR1c scoped it) deletes the `seen:*` rows as well as the legacy `read_entries:*` rows.

#### Snapshot: `lib/feedOrder.ts` (pure) and `lib/useFeedOrder.ts`
`partitionTab({entries, seen, sort, snapshot, hold, isRemoved}) → {unseen, earlier, snapshot}`:
- **New snapshot:** unseen holds what isn't seen, in the tab's sort. Earlier holds what is seen, in the same sort.
- **Ids in the snapshot keep their group and place,** even once they are seen.
- **New arrivals** append to the end of unseen. They are held back while the marker is above the viewport, because Safari has no scroll anchoring.
- **A story that a poll drops** stays, drawn from the cached Entry, until the next snapshot. The cached Entry is used only for ids the input no longer has.
- **Removal is explicit.** `isRemoved(entry)` is true for hidden ids, deleted feeds (source ids missing from `sources`), and muted feeds on category tabs, For You and search (PR5b's `isShown`). A custom tab lists its own feeds and ignores `muted_sources` (PR8a). Today's inputs are filtered before they reach any partition (`visibleEntries`, App.tsx:886-889; `refresh` drops deleted sources, 1241-1243), so without this input a just-hidden story would look like a poll drop and stay. `isRemoved` applies to cached snapshot entries too, so a removed story leaves at once even when it is no longer in `entries`.
- **Unchanged input returns the previous arrays,** but only when the ids and every Entry reference match the last call. `refresh` merges polls with `reconcileById` (App.tsx:1261, 1274, 1276), so an updated story (summary, score, comment count) arrives as a new object, and Card re-renders on `prev.entry === next.entry` (Card.tsx:1503). Otherwise the arrays are rebuilt from the current Entries.
- **The snapshot carries a generation counter,** bumped on every reset.
- **Resets:** switching tabs (both tabs), Refresh / `r`, Mark all **and its Undo**, a Sort or window change on that tab, a feed edit on a custom tab (PR8a), and returning after 10 min or more hidden (C1, which also scrolls to the top). Polls never reset it. PR4d adds one rule for tabs wider than 2 days.

**Ownership:**
- `lib/useFeedOrder.ts` replaces `sectionsByColumn` (App.tsx:1162-1201). It holds a `Map<tabKey, Snapshot>` in a ref and calls the pure `partitionTab` on each render.
- App owns `resetSnapshot(tab)`. Refresh / `r`, Mark all and its Undo, Sort and window changes, tab switches, PR8a feed edits and C1 call it.
- **One count.** `unseenCount(tab)` comes from the partition: unseen-group ids not in seen, including cached dropped stories and held arrivals. It drives the selected tab's badge, the caught-up rule, the md+ "N new" header and C8's next-tab check. Tabs that aren't selected count unseen from their entries (within the window from PR4d on).

#### Opening (`onOpenEntry`)
- App defines a stable `useCallback` `openEntry(id)` that reads refs (both memos ignore callbacks). It calls `addIds` under the story's `seen:<category>` key and records `lastOpenAt = Date.now()` for C1's open guard.
- App passes it as `onOpenEntry` to:
  - both Columns;
  - `SearchResults`, replacing `readIds` / `onMarkRead` (SearchResults.tsx:31-32, 52-53, 95, 98);
  - the desktop For You row (App.tsx:2886-2930), which drops `unread={!isRead}` and its `onMarkRead` (2896).
- Each site passes `onActivate={() => onOpenEntry(e.id)}`. Card already has `onActivate` (Card.tsx:46), and today only the headline calls it (937).
- Card threads `onActivate` to:
  - the headline (Card.tsx:931-938);
  - the Thumbnail (1738-1757);
  - the Reddit-thread link (1019-1034);
  - Listen (1094-1103);
  - the context-menu "Open in new tab".

#### C1: returning to the app
- `HIDDEN_RESET_MS` (App.tsx:78, 2 min) becomes `RETURN_RESET_MS = 10 * 60_000`.
- The visibility handler (App.tsx:1443-1451) keeps its hidden-at stamp (`hiddenAtRef`). Only its `seenEntryIdsRef` use goes.
- On visible, if `Date.now()` minus the stamp is at least `RETURN_RESET_MS`:
  1. run PR1r's update check first (it may reload onto a new build);
  2. **open guard:** if the page hid within about 2 s of `lastOpenAt`, skip the reset and the scroll-to-top for this return, so the story you just read stays in place for ▲ or Save;
  3. otherwise reset every tab's snapshot and scroll the phone list to the top, before the silent refresh.

#### Column.tsx
- **Header:** the label, ⋯, and "N new" on md+. Delete `role`, `tabIndex`, the handlers and `title` (279-325), the chip (338-371) and "X of Y" (372-377).
- **List:**
  - The first 30 unseen cards (PR3a's batching), then the `[data-more]` sentinel that adds 30 more.
  - Once all unseen cards have rendered, a `[data-caught-up]` row follows:
    - ✓ "You're all caught up" when the tab's unseen count is 0;
    - "N not seen · Mark as read" when it is still above 0 (D24). A card flung past never qualifies, so it can still be unseen. This is a 44px button that runs the same path and Undo as ⋯ → Mark all as read.
    - C8's next-tab row comes next, then a folded 44px `button[data-earlier][aria-expanded]` "Earlier (N)".
    - None of these rows carry `role="status"`.
  - Earlier renders dimmed cards, 30 at a time.
  - Saved has no marker. An empty tab keeps `EmptyColumnMessage`.
- **Props:**
  - `sections` keeps its name with the shape `{unseen, earlier}`. New data props: `unseenCount`, the C8 next tab (label and count), and the snapshot `generation`. PR3c adds `trackSeen`.
  - Column owns the 30-at-a-time limits and `earlierOpen`, and resets them when `generation` changes, so Earlier folds again on every snapshot.
  - Column opens Earlier itself when `selectedId` is in it.
  - Dropped: `newCount`, `totalCount`, `sectionsCollapsed`, the header `onMarkRead`, `onMarkEntryRead` and the chip's `onRefresh`.
  - Every new data prop joins `_columnPropsEqual` (Column.tsx:890-915) and slice21's parametrize list.
- **Keyboard:**
  - The render limit includes `selectedId`'s position at render time.
  - App's ↑/↓ (App.tsx:2007-2017) walks unseen, then Earlier, from the same partition, opening Earlier when reached.
  - The card exists before App scrolls to it (2118-2122).
- **⋯:** now shows on For You too (385). Its first row is "Mark all as read" (44px, disabled at 0).

#### Card.tsx
- Remove:
  - ✓ (1381-1411) and `CheckIcon` (1527);
  - swipe-right: the reveal (864-873) with `readRevealRef` (314, 529-530, 866), `commitMarkRead` (546-555), its commit branch (684) and the clamp (654);
  - the action bar's `onMarkRead` term (1277);
  - the unread ring (838-842);
  - the context-menu "Mark as read" (727-735);
  - the `onMarkRead` prop.
- `unread` becomes `dimmed`.

#### App.tsx
- **Delete:**
  - `newCountByColumn` (1108-1125) and `unreadIdsByColumn` (1137-1147);
  - `sectionsByColumn` (1162-1201), replaced by `useFeedOrder`;
  - `seenEntryIdsRef` and its uses (828-830, 1244-1260, 1678-1682, 2163-2167). `hiddenAtRef` stays for C1;
  - `MAX_SEEN_ENTRY_IDS` / `capSetFIFO` (187-203);
  - `toggleEntryRead` (1737-1757), the `m` key (2022-2032), and the `onMarkRead` wiring for search results (2790) and the desktop For You row (2896);
  - the Column wiring `onRefresh={manualRefresh}` (2948, 3096), `onMarkRead` (2952, 3102) and `onMarkEntryRead` (2953, 3103);
  - `lastViewed` writes (2144), with `lastViewedRef` and `clearLastViewed`. Reset still deletes old rows;
  - section collapse: `setColumnSection` (2411-2449) and the `columnSections` ref and setter (731-734, 760, 775).
- **Badge** (3062-3070): the unseen count, `99+`, `aria-label="<tab>, N new"`. None on Saved or Brief.
- **Mark all.** `markColumnRead` (2131-2187) becomes Mark all. It adds the tab's unseen ids under each story's `seen:<category>` key, resets the snapshot, and shows "Marked N as read · Undo". `unmarkColumnRead` (1659-1685) becomes its Undo: `removeIds` of exactly the ids Mark all added, per key, then a snapshot reset, so the stories are back above the marker.
- **Manual Refresh toast:** "N new" or "Nothing new", worked out from the ids held before the request (replaces 1284-1287). This toast is temporary: from PR6b the status row is the only result shown for every path that queues checks.
- **ShortcutsSheet:** drop the `m` row.

#### Tests
- **Unit `feedOrder.test.ts`:** one case per partition rule above (new-snapshot grouping, keep place once seen, arrivals append, hold while the marker is above the viewport, a dropped story stays, removed stories leave, unchanged input returns the same arrays). Plus:
  - a hidden or deleted snapshot id leaves, while a poll-dropped id stays;
  - the same ids with one updated Entry return the new reference;
  - the generation bumps on reset;
  - sort and window resets;
  - the D33 baseline.
- **pytest `test_slice21_column_memo.py`:**
  - Props drop `newCount`, `totalCount` and `sectionsCollapsed`, and add `unseenCount`, the C8 next tab and `generation`. `sections` keeps its name with the new shape.
  - HANDLERS drop `setColumnSection` and `sectionsCollapsedFor`.
  - Delete `test_sections_collapsed_uses_memo_cache`.
- **Flows:**
  - `flow-open-marks-seen`: opening from a Column, the desktop For You row and search each sends a PATCH adding the id under `seen:<category>`. Hiding the page within 100 ms of an open still sends a keepalive PATCH with the id.
  - `flow-caught-up`: a route override gives the tab enough stories to scroll. Once all are opened or marked, the marker shows on return, and Earlier (N) is first and folded. With stories left unseen, the end row reads "N not seen · Mark as read", and one tap marks them with an Undo.
  - `flow-next-tab` (C8).
  - `flow-mark-all-read`: after Undo, the stories are back above the marker and Earlier is gone or empty.
  - `flow-auto-append` ends with the marker.
  - `flow-new-arrival-appends`.
  - `flow-return-after-10-min` (C1), with the clock installed: after 11 min hidden the tabs reset and scroll to the top. A second case opens a story, hides within 2 s and returns after 11 min: nothing resets.
  - `flow-desktop-layout` (1280) adds a Mark all step.
  - `flow-sort-best` stays green, because Sort resets the snapshot.
  - FN's regex (`flows.cjs:818`) becomes `/^(\d+ new|Nothing new)$/`. FM stays green.
- **Smoke:** `caught-up`, `not-seen-row` and `earlier-open` (390/320).

#### Risks and rollback
- Revert restores ✓. Marks made under `seen:*` are invisible to the old build, so stories used up in PR3b show as unread there until they age out.
- Old bundles keep writing `read_entries:<column>` whole lists until they reload (PR1r). With the `seen:` prefix they can only overwrite their own legacy rows.
- PR2a rejects keys over 60 characters, so a category longer than 55 characters can't take a `seen:` key. Check the longest category in use before merging.

**Run with:** effort xhigh · plan first: yes · review: an Explore inventory of every call site in the delete and rewire lists first; review agents: partition rules; call sites and removals; memo and stale closures; a11y and smoke; /code-review ultra if the plan allows.

### PR3c: Seen on scroll (frontend track)

#### Seen tracking (phone only)
**`lib/seen.ts` (pure, timers injected):**
- **Qualifying:** a card qualifies after it has been at least half visible for `SEEN_DWELL_MS` (1 s) straight (`intersectionRect.height ≥ 0.5 × min(card, root)`), so tall cards count.
- **Seen:** a qualified card is seen when it leaves through the top (`!isIntersecting && boundingClientRect.bottom ≤ rootBounds.top`).
  - The exit counts only when `target.isConnected`, `boundingClientRect.height > 0` and `rootBounds.height > 0`. A detached or `display:none` target reports all-zero rects, which would otherwise pass.
  - Leaving through the bottom does not count.
- **End rule:** once the marker has been half visible for 1 s, every qualified card on screen is seen. So is any card that qualifies while the marker stays visible.
- **Pauses:** a dwell timer that fires while the page is hidden (`document.visibilityState`), or while `modalOpen` is true, restarts instead of qualifying the card. `modalOpen` is an explicit boolean that App builds from its own state: `drawerOpen`, Settings, ShortcutsSheet and TabsEditor (PR7a, already merged), plus each later sheet as it lands (ActionSheet in PR5a, TabSheet in PR8a). It is not the selector `[aria-modal="true"]`, which always matches the closed, always-mounted Drawer (Drawer.tsx:274-275; App.tsx:3157).

**`lib/useSeenObserver.ts`:**
- **Call site:** `useSeenObserver(isDesktop ? null : phoneScroller, {modalOpen, onSeen})`, in App. `phoneScroller` is the list element PR1b holds in state, so the hook rebinds on every keyed tab remount.
- **Runs only while `!isDesktop`.** The phone `<main>` stays mounted, hidden by CSS, at md and up (App.tsx:3000). Turning a large iPhone to landscape would otherwise zero every rect.
- **One IntersectionObserver per root,** with thresholds 0, 0.1 … 1. The root is the phone Column list (`data-scroller`).
- **Root changes:** the observer disconnects and clears its timers.
- **Registry (replaces "early registrations are queued"):** the hook keeps a `Map<entryId, Element>` that outlives any one observer. Register and unregister always edit the Map, and call `observe` / `unobserve` only while an observer exists. Every new observer, after a rotation or a root change, observes every registered element. Card's ref callback is stable and runs only on attach and detach (Card.tsx:326-335), so crossing 768px tears the observer down and back up without remounting a single card.
- **Registration:** Column registers unseen cards from `setCardRef` (Column.tsx:226-232) when `trackSeen` is true, so Card's memo is untouched.
- **`trackSeen`** is a new Column data prop: true only on the phone and not on Saved, Brief or search results. It joins `_columnPropsEqual` and slice21.

**Open only (no scroll tracking):** Saved, Brief, search results, and all of desktop (C2).

#### Writes
- `markSeen(ids)` collects scroll-seen ids for about 300 ms, then makes one `addIds` call per `seen:<category>` key. That is one render per batch, not one per card.
- **Flush on hide:** the collector registers with PR2b's `registerBeforeFlush`. The `visibilitychange: hidden`, `pagehide` and `beforeunload` handlers run it synchronously before they read the queue, so a card that left the top just before the page hid goes out in the keepalive PATCH.
- Open marks keep bypassing the collector (PR3b).

#### Events
- Scroll-seen records `view` (`recordBatched`) at that moment.
- Delete the on-mount view (Card.tsx:499-506).

#### Tests
- **Unit `seen.test.ts`:** zero rects, a detached target, a hidden page, `modalOpen` true, a closed always-mounted dialog that does not pause, a root change, the end rule. A hook case: register, tear the observer down, recreate it, and the card is observed again.
- **Flows** (`page.clock.install` in `before`, advanced with `runFor`, per the timer rule):
  - `flow-scroll-marks-seen`: first switch tab by tapping, and open and close the Menu. Then scroll cards off the top, advancing `runFor(SEEN_DWELL_MS + 100)` between scrolls, then about 350 ms for the seen batch and about 1100 ms for the PATCH batch. The union of `add` across all seen PATCHes is exactly the ids that left the top; the badge drops; the card order is unchanged. In a fling, only cards first shown during the fling stay unseen.
  - `flow-caught-up` gains the flung-card case: a flung card keeps the ✓ text away and the end row reads "N not seen · Mark as read".
  - `flow-view-on-seen`: the `view` event is sent when a card is seen, not on mount.
  - `flow-seen-flush-on-hide`: a card leaves the top, the page hides within 300 ms, and a keepalive PATCH carries it.
  - `flow-landscape-no-marks`: the viewport crosses 768px and no PATCH is sent. Back at 390, scrolling two cards off the top sends exactly those ids.

#### Risks and rollback
- How seen-on-scroll feels needs the owner's hand on the device (`SEEN_DWELL_MS`). Gate 1 tunes it.
- Revert leaves PR3b's model: open or Mark all only. The marks stay valid.

**Run with:** effort xhigh · plan first: yes · review: review agents: observer lifecycle; visibility and flush order; flow determinism; then Gate 1.

### Device gate 1 (after PR3c)
- The owner deploys PR3b and PR3c in the same `popping-update` (D33), after the main-branch build for PR3c's merge commit is green.
- The checklist is a section of the Device checks list (D21). Step 0 (revision check, force-quit and reopen, build line) applies first.
  - Open the top story of a badged tab and come back. Pass: the badge drops by 1 and the list hasn't moved.
  - Scroll slowly past 5 cards. Pass: they count as seen.
  - Fling past 20. Pass: those stay unseen, and the end row offers "N not seen · Mark as read".
  - Dwell feel. Too eager → `SEEN_DWELL_MS` 1500, right → 1000, too slow → 700. This is a retune, not a stop.
  - Rotate to landscape and back, then scroll. Pass: marks still happen.
- Gates never auto-pass. The owner replies "pass", "fail: <line>" or "waive". Until then, frontend units after PR3c may be prepared locally but don't merge. The backend track continues.

---
## PR4: Smaller tabs (window, first-fetch cap, On this day)

**What you'll notice:**
- A tab shows stories that arrived in the last 2 days. ⋯ → Show stories from offers 1 day, 2 days, 7 days or All.
- A new feed adds its newest 10 stories once, even if it first works days after you added it. A feed you resume after weeks away does the same.
- On this day has its own tab, with 5 stories a day.
- Saved holds every save, with full cards. Paused feeds leave the tabs.
- A quiet category keeps its tab. When it has nothing in 2 days it says so, with one tap to 7 days.

**Complaints:** "Most tabs have over a hundred things when I add a feed and I have no idea why".

**Units:**
- **PR4a: Ingest rules, brief guard, On this day** (backend track). Depends on: nothing outside main; runs after PR2a in the backend track. Merge: CI green and its review step (D32).
- **PR4b: Window parameters and by-ids projection** (backend track). Depends on: nothing outside main; runs after PR4a in the backend track. Inert until PR4c and PR4d call it, apart from the For You active and hidden filters. Merge: CI green, its review step and the EXPLAIN result (D32).
- **PR4c: Saved by id** (frontend track, first unit after Gate 1). Depends on: PR4b (the projection), PR3b (the prune keep set). Merge: PR4b merged, CI green, its review step.
- **PR4d: Window UI** (frontend track). Depends on: PR4b, PR4c (old saves never vanish), PR3b (snapshot resets and counts). Merge: PR4b and PR4c merged, CI green, its review step.

### PR4a: Ingest rules, brief guard, On this day (backend track)

#### Backend

**`app/ingest_rules.py` (new, pure):** `select_items_for_ingest(items, *, known, first_fetch, baseline_at, now, keep_first=10, backfill_h=48, max_age_d=30, per_poll=50, max_undated=2) → (kept, counts)`.
- `known` is the set of URLs already stored. `_ingest` runs its known-URL query (`scheduler.py:411-416`) before the rule and passes the set in.
- **First fetch:** the newest 10 (undated after dated), plus everything from the last 2 days.
- **Later polls:**
  - drop items dated before `baseline_at − 2 d` or before `now − 30 d`;
  - keep an undated unknown item only when it sits above the first known URL in feed order (feeds and listings are newest-first), and keep at most `max_undated` of them (2, matching D12). This applies to every feed type; PR10b reuses the same knob for website feeds.
- **Order of the rules:** date rules first. Then `per_poll` (50) applies to unknown items only, newest first, so a burst of more than 50 new stories lands over the next polls instead of being lost.
- Everything dropped is counted in `counts`, by reason.
- **Tunables:** `ingest.first_fetch_keep`, `ingest.backfill_hours`, `ingest.max_age_days`, `ingest.max_per_poll`, `ingest.resume_gap_hours` (default 72) and `ingest.max_undated_per_poll` (default 2). They are read with `runtime_settings.get_many`, then parsed and clamped as in the Rules for every PR.

**`scheduler._ingest`:**
- The rule runs after the normalize loop (`scheduler.py:403-407`) and after the known-URL query (`:411-416`), which now runs first and feeds `known` to the rule.
- **First-fetch detection:** `first_fetch = last_fetch_at IS NULL OR NOT EXISTS (entries WHERE source_id = s.id AND fetched_at >= s.created_at)`, run as one indexed `EXISTS`. A zero-item "success" sets `last_fetch_at` today (`:491`), so `last_fetch_at` alone would end first-fetch detection before a feed ever brought a story.
- **Resume gap:** when `now − last_fetch_at` is more than `ingest.resume_gap_hours`, the poll is treated as a first fetch. This covers a pause, an auto-disable followed by a resume, and a long backoff streak.
- **Baseline:** a first-fetch poll that keeps at least one item sets `created_at` to the server's `now()`, the same transaction time the inserted rows get as `fetched_at` (`models.py:226-228`), so the `EXISTS` above holds for them. An empty first-fetch poll leaves `created_at` alone. A feed that was blocked for days therefore doesn't flood the tab when it first works.
- Counts go to `summary["skipped"]`. Add `IngestResult.skipped: int = 0` (`schemas.py:497-502`).
- **In-flight guard:** the commit session discards the batch and skips the bookkeeping when `source.url` no longer equals the snapshotted `source_url` (`scheduler.py:353`). From PR11a (PATCH `type`), a changed `type` does the same.

**`update_source`:** when `url`, `sitemap_url` or `link_pattern` changes (`url_changed` at `scheduler.py:2329`, `sitemap_url_changed` at `:2339`, `link_pattern_changed` just after; the url branch is at `:2380`), set `last_fetch_at = None` and `created_at = now`. Each of the three re-points the feed, so it starts again from a first fetch.

**Brief guard:** at `brief.py:386` and `:451` (the `fetched_at >= since` filters), add `or_(Entry.published_at.is_(None), Entry.published_at >= Entry.fetched_at - timedelta(hours=backfill_h))`. `backfill_h` comes from the same `ingest.backfill_hours` tunable (default 48) through the tunable path. It excludes a new feed's backfill, which is the intent, without dropping fresh date-only stories. A guard of `published_at >= since` would drop scrapes and KEV items stamped at midnight UTC (`generic_scrape.py:176-195`, `cisa_kev.py:108-118`), and almost everything at small brief windows.

**On this day (D17):**
- `selected` items only, at most 5 a day, in category `on_this_day`.
- Each entry URL is unique per date: `f'{page_url}#otd-{now:%Y-%m-%d}'`. Inserts are unique on `url`, and the same anniversaries recur every year, so without the date "5 a day" would decay to almost nothing. `_fallback_url` (`wikipedia_on_this_day.py:43`) already uses a fragment the same way.
- `_upsert_source` returns existing rows unchanged (`scheduler.py:110-135`). So `start_scheduler` (`scheduler.py:1779`) moves the existing row once, only if it is still in `news`, behind `builtin_category_moved:wikipedia_on_this_day`.
- `/api/foryou` excludes `_HISTORICAL_SOURCE_SLUGS` (`brief.py:91`), as the brief already does (`brief.py:387`).
- Add `'on_this_day': 6.0` to `_HALF_LIFE_HOURS` (`scoring/recency.py:19-25`). Today the category falls back to the 12 h default, which keeps these stories scored high for longer than news.

#### Tests
- **pytest (new):**
  - `test_ingest_rules.py` and `test_ingest_backfill_cap.py`:
    - an empty success, then items 3 days later: only the newest 10 plus 2 days are kept;
    - a feed resumed after 21 days: at most 10 plus 2 days;
    - a new item dated 5 days back on an hourly feed is kept;
    - 80 new dated items within the window arrive over two polls, and all 80 land;
    - an undated 60-item feed gives 10, then at most 2 per poll;
    - `skipped` counts every dropped item.
  - `test_first_success_resets_baseline.py`: `created_at` moves only on a first-fetch poll that keeps an item; an empty first poll leaves it.
  - `test_source_url_change_resets_first_fetch.py`: a `url`, `sitemap_url` or `link_pattern` change resets both fields; a URL change during an in-flight poll inserts nothing.
  - `test_brief_published_guard.py`:
    - a date-only story stamped midnight UTC and fetched inside the window stays;
    - an RSS story published just before `since` and fetched after it stays;
    - a 30-day-old backfill story is excluded;
    - an undated story stays;
    - `window_hours=1` still keeps fresh stories;
    - the convergence path at `brief.py:451` gets the same guard.
  - `test_wikipedia_selected.py`: selected items only, at most 5, and two fetches on different dates that return the same article both insert.
  - `test_builtin_category_move.py`: the move runs once, only from `news`; `half_life_hours('on_this_day')` is 6.
  - `test_foryou_excludes_historical.py`: an On this day story never reaches For You.
- **pytest (rewrite):** `test_ingest_skips_known_urls.py`. Its fixed `_NOW` (`:15`) falls behind the watermark, so use relative dates and assert `skipped`.

#### Risks and rollback
- Website feeds still drain undated sitemap pages at up to 10 a poll (`generic_scrape.py:42`) until PR10b applies the 2-undated limit inside the plugin.
- The On this day stories already stored stay in the new tab until they leave the 2-day window.
- Revert is safe. There is no migration, and the old code ignores `skipped`.

**Run with:** effort high · plan first: no · review: /code-review xhigh.

### PR4b: Window parameters and by-ids projection (backend track)

#### Backend

**`GET /api/entries`:**
- `max_age_hours` (1..8760) filters `fetched_at ≥ now − h`. It uses `ix_entries_fetched_at` (0008), or `ix_entries_fetched_at_composite_score` (0026) where the sort needs it. The 0025 index was dropped by 0029 (`0029_drop_duplicate_fetched_at_index.py:24`); no new index or migration is needed.
- `per_source_cap` (1..200) needs `max_age_hours`, or the request gets 422. The window "All" sends `max_age_hours=8760`, so the cap is never sent alone.
  - It is a `row_number()` per source ordered by `composite_score desc, fetched_at desc, id desc`, so a busy feed keeps its 25 best-scoring stories in the window (D28). The `fetched_at` and `id` tie-breakers make the cut deterministic, because one poll's rows share the same `fetched_at` (the transaction time).
  - On the per-category path it is computed and filtered in an inner subquery before the per-category `row_number`, so a category still gets its full 100.
- With `max_age_hours`, the per-category `row_number` (`entries.py:404-409`) orders by `fetched_at desc, published_at desc nulls last, id desc` instead of `composite_score`, so a new low-score story isn't cut. The per-category cut uses arrival time; the per-source cap uses score (D28).
- `active_only` keeps only `Source.active` feeds.
- **Placement on the per-category path:** these go inside `ranked` (`entries.py:412-416`), because `:418` builds a fresh `select` that drops earlier `.where`s.
- **Flat path** (the saved-view filter path, wider tabs, custom tabs, All). Today it orders by score and applies `limit` directly (`entries.py:362-364`, `:424`), with no subquery to filter a window function in.
  - With `per_source_cap`: wrap the filtered select in a subquery. The category, source, `active_only` and hidden filters go inside it. Add `row_number() over (partition by source_id order by composite_score desc, fetched_at desc, id desc)` and filter `rn_src <= cap` on the outer select.
  - With `max_age_hours`: the outer `ORDER BY` is `fetched_at desc, published_at desc nulls last, id desc`, then `LIMIT`. A custom tab is therefore cut by arrival, not to its top 200 by score.
- **Hidden stories:** without `q`, the caller's `hidden_entries` are excluded.
  - `list_entries` gains `user = Depends(current_user)` and keys the lookup with `resolve_user_id(user)`. Today `routes/entries.py` imports only `require_user` (`:20`).
  - The row is loaded once in Python. Keep only real ints (not bools) with `0 < x < 2**63`, the newest 1000. A malformed stored value then can't take down the main feed.
  - Bind them as one array: `Entry.id != any_(bindparam('hidden', ids, type_=ARRAY(BigInteger)))`, not as one parameter per id.
  - On the per-category path the exclusion goes inside `ranked`; on the flat path, inside the subquery.

**`GET /api/foryou`:**
- The same `max_age_hours` window and hidden-story exclusion, with the same defensive read. `foryou` already has `current_user` (`foryou.py:51`) but doesn't call `resolve_user_id`.
- An unconditional `.where(Source.active.is_(True))` next to the category filter (`foryou.py:110-111`). For You never wants paused feeds, so it needs no opt-in parameter, and old bundles get the fix too.

**`GET /api/entries/by-ids`:**
- It uses one projection helper shared with `list_entries` (the `columns` tuple plus the `meta->>` extractions). It returns the `EntryListOut` fields plus `source_name`: `image_url` / `image_path`, `cached_summary`, `reddit_thread_url`, `reddit_comment_count`, `audio_url`, `duration_seconds`, `transcript_url` and `story_cluster_id`.
- Today it builds `EntryOut` from the ORM row with no image, audio, Reddit-thread or cluster fields (`entries.py:513-544`). Saved would render degraded cards, and the frontend type (`api.ts:113`) hides that from tsc.
- Drop the full `meta` blob. Settings' Hidden, Starred and Voted lists (`Settings.tsx:1657`, `:1730`, `:1815`) read only the projected fields and `source_name`; check them when the helper lands.
- Fix the misleading comment at `entries.py:511-512` ("Same column projection as the list_entries endpoint"), which this change makes true.

**VPS:** run a read-only `EXPLAIN (ANALYZE, BUFFERS)` on both paths. The target is under 150 ms. Before merge, run the same query plans on a synthetic 400k-row Postgres 16 table at 48 h and 8760 h.

#### Tests
- **pytest (new):**
  - `test_entries_window.py`:
    - both paths, the cap, the 422, a low-score new story kept, `active_only`, hidden stories, and no parameters;
    - the flat cap with a category filter and with a source list;
    - a new low-score story is kept under the limit;
    - 8760 plus the cap returns 200;
    - 30 rows from one feed in one transaction (one `fetched_at`): the cap keeps the 25 best-scoring, and two calls return the same ids in the same order;
    - a malformed `hidden_entries` value returns 200.
  - `test_foryou_window.py`: the window and the hidden exclusion; a paused feed whose story is inside the window is absent.
  - `test_entries_by_ids_projection.py`: a seeded row returns `image_path`, `reddit_thread_url`, `audio_url` and `story_cluster_id`.
- **pytest (unchanged):** `test_entries_per_category.py` stays green, because it passes no window.

#### Risks and rollback
- The parameters do nothing without PR4c and PR4d. The For You active filter and the hidden exclusion apply to old bundles at once; both are harmless, because old bundles already filter hidden ids on the device.
- With more than 4 capped feeds in a tab, the 100-story cut by arrival can make that tab's effective window shorter than 2 days.
- Revert is safe. The parameters are additive, and by-ids only gains fields.

**Run with:** effort high · plan first: no · review: /code-review high + /security-review; EXPLAIN on a synthetic 400k-row table.

### PR4c: Saved by id (frontend track)

#### Frontend
- **Saved** (D5: Saved always lists every save):
  - Uses `api.entriesByIds` (`api.ts:549`; 200 per call, chunked), and only for ids not already held. Newest save first.
  - It replaces `visibleStarred` (`App.tsx:946-958`) in the same unit, so Saved stops depending on which stories the tabs loaded.
  - The by-id cache counts as held. Ids the server no longer returns go into a negative cache, so they aren't requested again on every poll.
  - Keep today's rule that hidden wins over saved (`App.tsx:949`).
- **Undo of a hide** re-inserts the cached Entry. PR4b drops hidden stories on the server, so a poll between the hide and the Undo would otherwise lose the card until the next refresh.
- **Prune keep set:** the `markSeen` / `pruneIds` keep set (PR3b) includes the Saved-by-id cache, so pruning never resurfaces an old save as unseen.
- **`capture.cjs`:** the by-ids mock (`capture.cjs:124-127`) mirrors the real projection instead of returning the full fixture entry.

#### Tests
- **Unit:** a `feedOrder.test.ts` or `prefSync.test.ts` case for the keep set with Saved ids.
- **Flows:** `flow-saved-by-id`: a save whose story isn't in the loaded tabs shows in Saved with its thumbnail, newest save first.

#### Risks and rollback
- Revert is safe. Saved falls back to the stories the tabs loaded.

**Run with:** effort high · plan first: no · review: /code-review medium.

### PR4d: Window UI (frontend track)

#### Frontend
- **`lib/window.ts`:** `'1d' | '2d' | '7d' | 'all'`, default `'2d'`, plus `inWindow(fetched_at)`. The legacy `maxAgeHours` mapping is exact:
  - an explicit `window` wins;
  - `maxAgeHours` of 1, 4 or 24 maps to `'1d'` only when `window` is absent;
  - `null` or missing maps to `'2d'`. Today `null` means All (`Column.tsx` `DEFAULT_PREFS`), and treating it as `'1d'` would shrink every tab.
  - New writes set `window` and drop `maxAgeHours`.
- **`ColumnPrefs.window`:** the ⋯ row "Show stories from" (a 4-way segmented control) replaces "Hide if older than" (`Column.tsx:462-482`).
- **Calls** (`api.entries` and `api.forYou` gain `maxAgeHours`, `perSourceCap` and `activeOnly`; `forYou` takes only `limit` and `category` today, `api.ts:528`):
  - `refresh()` (`App.tsx:1222-1231`) calls `entries({perCategoryLimit: 100, maxAgeHours: 48, perSourceCap: 25, activeOnly: true})` and `forYou({limit: 50, maxAgeHours: 48})`;
  - the saved-view filter path (until PR8b removes it): `entries({source, limit: 200, maxAgeHours: 48, perSourceCap: 25, activeOnly: true})`;
  - a wider category tab: `entries({category, limit: 200, maxAgeHours: h, perSourceCap: 25, activeOnly: true})`;
  - For You at a wider window: `forYou({limit: 50, maxAgeHours: h})`;
  - All sends `maxAgeHours: 8760`;
  - on desktop, widened columns refetch one at a time.
- **The desktop For You row** shows its top 6. Its stories also appear in their category columns.
- **Wider tabs** fetch their own rows into `extraByTab`. Only the visible tab refetches. `extraByTab` persists across tab switches.
  - When a tab is wider than 2 days, its first extra response rebuilds the snapshot if the list hasn't been touched since the switch. Otherwise the new rows are sorted into unseen or Earlier by the tab's sort, never appended out of order.
- **Counts:** the badge, unseen and Earlier all count within the tab's effective window. Wider tabs that aren't selected count from the 2-day data.
- **Prune keep set:** it also includes every `extraByTab` row.
- **Categories:** built from the union of active sources' categories (unmuted ones from PR5b on) and the `byCategory` keys, sorted as today (`App.tsx:891-910`). Today they come only from `byCategory`, so with a 2-day window a slow category would lose its tab, and with it the only control that brings its stories back. A category whose feeds are all paused still leaves the tabs. PR7a builds on this; it doesn't redo it.
- **Empty tab:** `EmptyColumnMessage` (`Column.tsx:820-877`) gains a minimal "Nothing in 2 days · Show 7 days" line. "Show 7 days" sets that tab's window. PR7b keeps the line when it rewrites the empty states.
- **Labels:**
  - `categoryLabel('on_this_day')` gives "On This Day" (`labels.ts:97-101`). Add an override so it reads "On this day".
  - On cards, `HISTORICAL_SOURCES` show "On this day" in place of a time. `HISTORICAL_SOURCES` is a plain string list in `feedRules.ts` or `labels.ts`, pinned against `brief._HISTORICAL_SOURCE_SLUGS` (`brief.py:91`) in `test_frontend_feed_editor_rules.py`, as that file already pins `KNOWN_BUILT_INS` (`feedRules.ts:34`).
- **`capture.cjs`:** `/api/entries` honours `max_age_hours`, `per_source_cap` and `active_only`, and `/api/foryou` honours `max_age_hours`. Both filter against `F.NOW`, not `Date.now()`.

#### Tests
- **Unit:** `window.test.ts`: an explicit window; both fields present; legacy 1, 4 and 24; legacy `null`; missing; prefs that arrive through `filter_presets`.
- **pytest:** `test_frontend_feed_editor_rules.py` pins `HISTORICAL_SOURCES`. slice19 pins the "Nothing in 2 days · Show 7 days" line.
- **Fixtures:**
  - Add stories that arrived 3 and 10 days ago.
  - Ids 10 and 12 are the existing paused feeds (`fixtures.cjs:81`, `:84`).
  - Source 15 (`fixtures.cjs:88`, the 40-character category, no stories) becomes a new empty tab. It is the fixture for the "Nothing in 2 days" line.
  - Once `capture.cjs` filters against `F.NOW`, entries 405, 505 and 506 (4000-6000 minutes old) drop out of the default podcast and video tabs, and 403 and 404 drop out with their paused feed. Update the states that showed them.
- **Flows:**
  - `flow-window-default`.
  - `flow-tab-window-7d`: resets the snapshot; asserts the order after switching away and back, and on a cold load.
  - `flow-saved-by-id` stays green with the window on.
  - `flow-paused-hidden`: the paused feed (id 10) is absent from its category tab, the For You tab and the desktop For You row.
  - `flow-badge-in-window`.
  - `flow-quiet-tab-kept`:
    - a category whose only stories are 3 days old keeps its tab;
    - its ⋯ → 7 days shows them within 3 taps;
    - it stays in `CategoryPicker`;
    - once PR5b lands, a category whose feeds are all muted disappears.
  - FU (`flows.cjs:384-389`) clicks the last Jump row; re-check it with the new tab.
- **Smoke:** `column-popover` shows the window control at 390/320.

#### Risks and rollback
- A story that arrived more than 2 days ago and was never seen drops out of the default view. That is by design, because it is the end.
- A very busy feed shows at most its 25 best-scoring stories per tab within the window (D28).
- A stale bundle that still writes `maxAgeHours` can't shrink a tab: `lib/window.ts` lets an explicit `window` win, and a numeric `maxAgeHours` maps to `'1d'` only when `window` is absent. `window.test.ts` covers both fields present.
- Revert is safe on its own until a later unit that lists PR4d under Depends on merges; after that, fix forward or revert newest first.

**Run with:** effort high · plan first: no · review: /code-review high.

---
## PR5: The card is a Reddit post

**What you'll notice:**
- The card shows the feed icon, feed name and time on top, then the title and thumbnail.
- One bar: ▲ ▼ (orange / blue when on; tap again to undo), Save (a bookmark, filled when saved), Share, the summary chevron, and ⋯.
- ⋯ offers Hide, Hide stories from \<feed\>, Edit feed, Copy link and Open in browser.
- Swipe left hides, with Undo. Two quick hides show "2 hidden · Undo".
- Undo fully undoes a hide: the ranking never hears about a hide you took back (D25).
- Saving is silent. Unsaving shows "Removed from Saved · Undo".

**Complaints:** "upvote, downvote, star, read … I hardly know what to do", "Nothing is intuitive to a new user".

**Units:**
- **PR5a: Reddit card, action sheet, Hide hold** (frontend track). Depends on PR2b (hides go through `addIds` / `removeIds`) and PR3b (✓, swipe-right and `onMarkRead` are already gone). Comes after PR4d in the frontend order. Merges once CI is green and its review passed (D32). The owner checks swipe-left and the sheet at Gate 2.
- **PR5b: Muted feeds** (frontend track). Depends on PR5a (the sheet and `hideStory`), PR2a/PR2b (`muted_sources` is a PATCH set key) and PR4d (`extraByTab` exists). If PR2c has already merged, PR5b adds `muted_sources` to PR2c's `SET_KEYS`; if not, PR2c includes it. Merges once CI is green and its review passed.

As a git revert, PR5a stands alone only until PR5b merges (PR5b builds on its sheet and `hideStory`). After that, fix forward or revert newest-first.

### PR5a: Reddit card, action sheet, Hide hold (frontend track)

#### Changes

**`ActionSheet.tsx` (new):** `openActionSheet({title, actions})` + `<ActionSheetHost/>`.
- A bottom sheet with 44px rows and `role="dialog" aria-modal aria-label="story actions"`.
- It uses Drawer's focus trap and inert pattern, closes on an outside tap or Esc, and has safe-area padding.
- It follows the sheet rules in Rules for every PR: the scroll body is `overflow-y-auto overscroll-contain`, and transitions are motion-safe only.
- Esc is handled in a capture-phase listener with `stopPropagation`, as Drawer (`Drawer.tsx:105-118`) and ShortcutsSheet (`ShortcutsSheet.tsx:48-60`) do, so it never reaches App's key handler (which clears the search query on Esc, `App.tsx:1992`).
- On open it stores `document.activeElement`. On close, focus returns to that element if it is still connected. Otherwise (Hide removed the card that opened it) focus goes to the next card's headline, or the previous one, or the tab.
- While a sheet is open, it counts in App's `modalOpen` (built in PR3c from `drawerOpen`, Settings, ShortcutsSheet and the other sheets), so seen-on-scroll pauses.

**The `Card.tsx` header line** (at least 44px tall) moves above the title row (924):
- `SourceIcon` (16px, from the existing `sourceFaviconPath` prop), the feed button (984-997, still labelled `edit feed <name>`), "·", and the time as "5h" (`timeAgo` 216-226 drops "ago").
- `timeAgo(null)` already returns `''`. Hide the "·" with it.
- The feed button keeps opening the feed editor (`onSourceTap`, `App.tsx:2351`) until PR8a. There, a tap opens a one-feed view, and Edit feed stays in ⋯ (D26).

**The action bar** (replaces 1277-1477): ▲, ▼, Save and Share, then, pushed right with `ml-auto`, the chevron (1354-1375) and ⋯ (`more actions`, `data-card-more`).
- Each button is 44×44 with no gaps, keeping today's −12px / −8px bleed.
- The bar is 264px, which fits from a 310px viewport. C7 checks it at 312.

**Votes:**
- `aria-pressed`, with `text-orange-500` / `text-indigo-400` when on.
- `voteInteractionEvents` (254) is unchanged.
- The vote toasts (797, 808, 1293-1295, 1320-1322) go.

**Save:**
- A bookmark icon labelled "save" / "unsave". It records `bookmark` only when saving.
- Saving is silent. Unsaving keeps one toast, "Removed from Saved · Undo", whose Undo re-adds the id through `addIds`. In Saved the card leaves the list, so without this Undo an accidental unsave could not be undone. (Dropped #12 is reworded to match.)

**Share:**
- `navigator.share({title, url: entry.url})` is called synchronously in the tap handler, because iOS needs the user gesture.
- The http(s) check runs before both `navigator.share` and the `copyUrl` fallback. A story whose `entry.url` is not http(s) gets a disabled Share.
- `safeExternalUrl` is not used here, because it would turn Reddit links into old.reddit.com (api.ts:46-62).
- `share` is recorded when the promise resolves. An AbortError records nothing.
- Without `navigator.share`, Share falls back to `copyUrl` (1807), which shows "Link copied".

**Other Card changes:**
- `buildActions()` feeds both ⋯ and right-click (724-824). In PR5a its rows are Hide, Edit feed, Copy link and Open in browser. PR5b adds Hide stories from \<feed\>.
- Swipe left calls `onHide`. Swipe right is clamped dead (654). The touchmove effect's deps (672, today `[onMarkRead, onHideToggle]`) become `[onHide]`.
- Remove the eye button (1448-1474) and the `hidden` and `onHideToggle` props. Update `_cardPropsEqual` (1501-1514).

**Plumbing for hide:**
- Column's Props (`Column.tsx:108-131`, destructured 193-197) and ColumnSection (550-552, 605-607, 650-652, 677-679) lose `onHideToggle` and `hiddenSet`. `onHideEntry` maps to Card's `onHide`.
- SearchResults loses `onHideToggle` and `hiddenSet` (`SearchResults.tsx:33-35, 54-56, 99-101`) and keeps `onHide`.
- The four "Entry hidden." toast closures (`App.tsx:2793, 2899, 2956, 3106`) and the keyboard `h` path (2068-2091) call `hideStory`.
- Update the Column memo comment (`Column.tsx:148-157`) and its comparator.

**`App.tsx`:**
- `hideStory(id)` calls `hideEntry` with toast group `hide`, and holds the `never` interaction until the Undo toast expires (D25):
  - `interactions.ts` gains a held slot next to its batch queue (49-98): a held event is sent when its toast expires, Undo cancels it, and the existing `pagehide` / `visibilitychange: hidden` flush (`onPageHide` 109-140, registered at 142-150) sends any held events first;
  - `Toast.tsx` gains an `onExpire` callback, so App can release the hold when the toast closes without Undo;
  - `h` calls `hideStory`.
- `hideStory` is a `useCallback` that reads changing data through refs or the PR2b delta calls, and joins slice21's HANDLERS.
- Delete `toggleHideEntry` (1791-1831) and `unmarkEntryRead` (1692).
- The save toasts go (2056-2067, 2803-2818, 2909-2924, 2966-2981, 3116-3131), apart from the unsave Undo above.
- `a` / `z` vote on the selected card (C6). They ignore keys pressed with meta, ctrl or alt, so Cmd+A and Cmd+Z still work.
- App's `onKey` (1951-2099) returns early while `modalOpen` is true, so `h`, `a`, `z`, `b` and Esc never act on the page behind a sheet.
- Wire `onHide` into both Columns, the desktop For You row and `SearchResults`.

**`ShortcutsSheet.tsx`:** rows for `b`, `h`, `a` and `z`.

**Preview text (D34, owner decision 2026-10-21):**
- **Backend.** The shared list projection (`list_entries`, For You, search and `/entries/by-ids`) gains `preview: str | None`.
  - It is the non-empty `cached_summary` when there is one, else the feed's own blurb (`meta.summary`), cleaned with `_clean_summary` (HTML stripped, whitespace collapsed) and cut on a word boundary to 300 characters with "…".
  - It is `None` when the result is empty or only repeats the title (case- and punctuation-insensitive), and for Reddit link posts whose blurb is only the "submitted by … [link] [comments]" boilerplate.
  - It is computed in the projection, so there is no new column and no migration. `EntryListOut` gains the field, and old bundles ignore it.
- **Card.** Under the title row, the preview renders as body text in `text-label-secondary`, clamped to 2 lines on phones and 3 from md up (`line-clamp`).
  - The title, the preview and the thumbnail are one open target. They share the headline's link and `onActivate`, so opening from the blurb also marks the story seen (PR3b) and records `click`.
  - The preview hides while the Summary panel is open, so the text is never shown twice.
  - A card with no preview looks as it does today.
- **Tests:**
  - pytest for the projection: cached summary over feed blurb, cleaning, the 300-character cut, title repeats and Reddit boilerplate dropped, `None` when empty, and `/entries/by-ids` carrying it.
  - Flow `flow-card-preview`: the blurb shows clamped, and a tap on it opens the story and sends the `seen:` PATCH.
  - Smoke `card-preview-long` and `card-no-preview` at 390, 320 and 312, so C11 and C14 hold with a long blurb.


#### Tests
- **pytest:**
  - `test_slice30_safe_external_url.py:150-152` expects "Open in browser". Thumbnail's `const open = () => {` stays.
  - `test_slice21_column_memo.py` drops `hiddenSet` and adds `hideStory` to HANDLERS.
- **Flows:**
  - `flow-card-votes`: on, off, no toast, and the card stays.
  - `flow-card-save`: one `bookmark`; unsave records none, shows "Removed from Saved · Undo", and Undo saves it again.
  - `flow-card-share`: a stubbed share with `entry.url`, then the clipboard fallback. A `javascript:` entry is neither shared nor copied.
  - `flow-card-sheet`: 4 rows of at least 44px, the focus trap, and Esc. Esc from a search result's sheet keeps the search query. After Hide, focus lands on the next card's headline.
  - `flow-hide-undo`: Hide, then Undo, sends zero `never`. Hide, then waiting past the toast, sends exactly one.
  - `flow-swipe-left-hide` and `flow-swipe-right-dead` (CDP, moves of 40px or more).
  - FV (845-865): the feed button sits above the headline and must not reach the headline's top edge. Its `?source=` check stays until PR8a.
- **Smoke:**
  - `card-action-sheet` (390/320, `C12_ROOTS`).
  - `dashboard` at 312 runs C7 on the bar.
  - The `card-summary` selector is unchanged.

#### Risks
- Hide no longer marks a story read. Hidden stories leave every tab anyway.
- Hide's `never` is held until its Undo window closes. A revert brings back immediate recording, which old builds did anyway.
- Revert is safe for data: no new key.

**Run with:** effort high · plan first: no · review: /code-review xhigh. The owner checks swipe-left and the sheet at Gate 2.

### PR5b: Muted feeds (frontend track)

#### Changes

**Preferences:**
- `muted_sources` (`number[]`) has no whole-list setter: `addIds` / `removeIds` only, the FeedEditor switch included.
- `MAX_MUTED = 500` in `preferences.tsx`, next to `MAX_HIDDEN` / `MAX_STARRED` / `MAX_VOTED` (793-797).
- Add it to `PREFERENCE_KEYS` and `DEFAULT_STATE` (160-185), `mergeStateFromServer` (890-922) and `prefSync.isSetKey`, with validation.
- Backend: an allowlist entry in `_PREFERENCE_KEY_RE` (`routes/preferences.py:107-127`), and `muted_sources` becomes a PATCH set key with a cap of 500. Extend PR2a's cap pin test. Add it to PR2c's `SET_KEYS` if PR2c has merged.

**`App.tsx`:**
- `muteSource(id)` changes `muted_sources` through `addIds` / `removeIds`. It is a `useCallback` that reads through refs, and joins slice21's HANDLERS.
  - The toast reads "\<Feed\> hidden · Undo".
- `visibleEntries` / `visibleForYou` (886-889, 911-914) keep filtering hidden ids only. The mute filter is one `isShown(entry)` helper, applied where tabs are built: the category tabs, `visibleForYou`, every `extraByTab` row and the search results, before `partitionTab` runs. Badges and C8's next-tab counts read the same filtered tab data, so they are covered too.
  - Custom tabs (PR8a) build from the unmuted base and ignore `muted_sources`. On a custom tab, PR8a turns the ⋯ row into "Remove \<feed\> from this tab".
- Wire `onMute` into both Columns (`onMuteSource` on Column's Props and ColumnSection), the desktop For You row and `SearchResults`.
- `buildActions()` gains the Hide stories from \<feed\> row.

**`Toast.tsx`:** adds `group` / `count`. Events in the same group merge into one toast ("2 hidden · Undo"). Its Undo replays the stored callbacks in reverse (at most 20) through `removeIds`, so every restore lands. Each merged hide's held `never` is released when the merged toast expires, and Undo cancels them all. The unsave toast joins group `save`, replayed through `addIds`.

**Feed screens:**
- `FeedManager` `SourceRow` (258) shows a "Hidden from tabs" tag.
- `FeedEditor` gets a "Show stories in tabs" switch beside Pause (`feeds/FeedEditor.tsx:544-567`; the Pause button is 545-551) that writes the preference.

#### Tests
- **pytest:** `test_preferences_anonymous.py` round-trips `muted_sources`, including PATCH. PR2a's cap pin covers 500.
- **Unit:** a `prefSync.test.ts` case for `muted_sources`.
- `test_slice21_column_memo.py` adds `muteSource` to HANDLERS.
- **Flows:**
  - `flow-mute-source`: the feed's stories leave the category tab and For You, and Undo brings them back. A 7-day-window case: mute a feed, set the tab to 7 days, and none of its stories show.
  - `flow-hide-undo` (extended): two quick hides merge into "2 hidden · Undo", and a single Undo restores both and sends no `never`.

#### Risks
- Revert is safe, because old builds ignore `muted_sources`.

**Run with:** effort high · plan first: no · review: /code-review high.

---

## PR6: Pull to refresh checks the tab's feeds

**What you'll notice:**
- Pull down at the top of a tab to see a spinner, then "Checking 6 feeds…", then "3 new" or "Nothing new". New stories arrive at the top. While some feeds are still being checked it says "3 new · 2 still checking". When every feed was checked recently it says "Checked 4 min ago".
- The phone header is one slim row: title, search, Menu. On the phone, the tab's own header row scrolls away with the stories.
- Tapping the current tab at the top does the same as a pull (C3), and so does ⋯ → Refresh (C4).
- On desktop, Refresh and `r` check the visible columns.
- Pulling never pushes a failing feed past its backoff, so it can't auto-disable a feed.

**Complaints:** "try to refresh … to see if it … pulls".

**Units:**
- **PR6a: queue_check and the check-now route** (backend track). Comes after PR4b, before PR9a (all three touch `scheduler.py`; run them in that order). Inert until PR6b calls it. Merges once CI is green and its review passed.
- **PR6b: Pull to refresh and checkTabNow** (frontend track). Depends on PR6a (must merge first), PR1b (the phone list element held in state), PR3b (the snapshot reset), PR5a (rebases onto Card's swipe code; the two swipe constants move from Card into `pull.ts`) and PR5b (muted feeds are left out of a check). Merges once CI is green and its review passed.
- **PR6c: Slim phone header and the first-feed row** (frontend track). Depends on PR6b: the header Refresh goes only once pull exists (D10). Merges once CI is green and its review passed. **Device gate 2** follows PR6c.

The misfire-grace bullet that used to be here moved to PR1a, together with taking trafilatura off the event loop.

### PR6a: queue_check and the check-now route (backend track)

#### Backend
- **`scheduler.queue_check(ids, force=False)`:**
  - For each of at most 20 feeds, it sets `next_run_time = min(job.next_run_time, now + i×3 s)` on `ingest:<name>` / `_dynamic_job_id` (2055). A job that is already due is never postponed.
  - **Backoff gate.** With `force=False`, a feed with `error_count > 0` that is still inside its backoff window is skipped and reported in `backing_off`. Until PR9a adds `last_attempt_at`, "inside the window" means the job's `next_run_time` is still in the future. Today each failure doubles the interval (capped at 6 h) and the sixth failure auto-disables the feed (`scheduler.py:194-216`, 535-569). Without this gate, a pull every 10 minutes would force a failing feed past its backoff and disable it within about an hour. `force=True` still overrides.
  - **Running jobs.** Jobs run with `max_instances=1` and `coalesce=True` (1808-1809, 2149-2150), so APScheduler drops a run that comes due while one is still executing (a Reddit job sleeping in `_take_token`, a slow scrape). A job that is already running counts as queued and isn't modified. `_ingest` keeps a per-job in-flight set for this.
  - **Reddit in direct mode** (no proxy URL; `reddit_client.py:116-117` allows 1 call per 75 s): queue at most one Reddit feed per call. Subreddits fetched within their own interval are reported as `cooling_down`.
  - The cooldown is `refresh.cooldown_seconds` (default 600; `force` uses 60). It is kept in `_last_queued` and documented in `ARCHITECTURE.md`.
  - Inactive feeds, and feeds with no job, are reported as `paused`.
- **`POST /api/sources/check-now`** (`_write_deps`):
  - A JSON body model: `{source_ids: conlist(int, min_length=1, max_length=20), force: bool}`. More than 20 ids return 422.
  - Response: `{queued, cooling_down, paused, backing_off, queued_ids, queued_at, expected_done_s}`.
  - `queued_at` is the server's clock, which PR6b and PR9b compare against.
  - `expected_done_s` is about `reddit_count × 75` for Reddit feeds queued in direct mode, plus `count × 3 + 20` for the rest.

#### Tests
- **pytest `test_check_now_queue.py`:**
  - a backing-off feed isn't modified, and six calls in a row never auto-disable it;
  - `force` still queues it;
  - a job that is already due isn't postponed;
  - a running job counts as queued and isn't modified;
  - 21 ids return 422;
  - in direct mode, only one Reddit feed is queued per call, and a recently fetched subreddit is `cooling_down`;
  - the response carries `backing_off`, `queued_at` and `expected_done_s`.
- The misfire-grace test (`test_ingest_misfire_grace.py`) is in PR1a.

#### Risks
- Inert until PR6b. Revert is safe, because the route is additive.

**Run with:** effort high · plan first: no · review: /code-review high + /security-review.

### PR6b: Pull to refresh and checkTabNow (frontend track)

#### Gesture: `lib/pull.ts` (pure) and `lib/usePullToRefresh.ts`
**`pull.ts` is a pure reducer:** `step(state, {dx, dy, t, cancelable, scrollTop}) → {state, preventDefault, offset, armed, end}`, with time passed in, the same way `seen.ts` takes its timers. `usePullToRefresh` only feeds it touch events and applies the result.

**Scope:** only the phone Column's scroller. The Brief tab has none, and the tab strip and header sit outside it. `usePullToRefresh(phoneScroller)` takes the list element that App holds in state (PR1b) as an effect dependency, so it rebinds on every keyed tab remount and after a rotation.

**`touchstart` (passive):** track one finger only when:
- `scrollTop === 0` exactly (never below 0, which would freeze a bounce mid-animation),
- the touch is not inside `[data-hscroll]`, and
- no text selection exists.

**`touchmove` (non-passive, like Card.tsx:618-666):**
- An upward move ends tracking, and native scrolling takes over.
- A move with `cancelable === false` also ends tracking, because native scrolling already owns the touch.
- No `preventDefault` is called until `dy ≥ 3` and `dy > |dx|`. Cancelling a first sample of +1 or 2px of jitter can stop iOS from scrolling for the whole touch.
- From then on, downward moves call `preventDefault`. That stops iOS's top bounce for this gesture only. The bottom bounce and all other scrolling stay native, and `overscroll-contain` (Column.tsx:493) is unchanged.
- The lock decision happens once the move passes 12px:
  - It is a pull when `dy > 0` and the card would not lock horizontal, that is `!(|dx| > |dy| × 1.3)`.
  - Otherwise the gesture belongs to Card's swipe.
  - So every diagonal drag is handled by one of the two.
- `SWIPE_DEAD_ZONE_PX` and `SWIPE_DIRECTION_RATIO` move from Card.tsx (290-291) to `pull.ts`, and Card imports them, so the two locks can't drift apart.
- A finger held still for 400 ms or more (long-press or link preview) ends tracking.

**Motion:**
- The content doesn't move before the lock.
- After the lock, the offset is `min(96, dy × 0.5)`, and the pull arms at 64px. The content wrapper and spinner move with `transform`.
- Releasing while armed refreshes. Otherwise, and on `touchcancel`, the content snaps back.
- Under reduced motion the spinner doesn't rotate and the snap is instant.

#### `checkTabNow()` (App.tsx)
1. `refresh({silent: true})` re-reads the dashboard and resets the tab's snapshot. It shows no toast: errors still set the banner (`App.tsx:1203-1206, 1286-1292`).
2. It picks feeds and calls `api.checkNow(ids)`:
   - only active feeds with `error_count == 0`, ranked by oldest `last_fetch_at`, at most 20;
   - a category tab: its feeds, leaving out muted feeds (PR5b);
   - For You: the 20 stalest such feeds, leaving out muted feeds;
   - a custom tab (PR8a): its listed feeds, muted or not;
   - Saved: nothing. Its status row shows the re-read's result.
3. The status row shows "Checking 6 feeds…".
   - It is a `div[data-refresh-status]` overlay at the top of the list with no layout height (sticky or absolute, `pointer-events-none`), so it never moves the cards under the finger and never pushes a card off the top (which would mark it seen).
   - It is mounted, empty, with `aria-live="polite"` and no role, from `touchstart` (or the Refresh tap) until it unmounts after the result. VoiceOver then reads the text when it changes, and smoke's wait for no `[role="status"]` (`smoke.cjs:990`) is unaffected.
   - Silent refreshes run at +25 s, and the last one at `min(max(70, expected_done_s + 10), 300)` s. Their timers clear on a tab change.
   - Each queued feed is judged done when its `last_fetch_at` or `last_error` moved after `queued_at` (PR9b switches to `last_attempt_at`).
   - Then it shows, for 3 s, and unmounts:
     - "3 new" (ids new to this tab) or "Nothing new", once every queued feed is done;
     - "3 new · 2 still checking" when the last refresh comes and some feeds are still pending;
     - "Checked 4 min ago" (from the newest `last_fetch_at` of the tab's feeds) when nothing was queued because every feed was cooling down.
   - If nothing was queued, the result shows after the re-read.
4. **New stories from a check run:** if the list is still at the top with no touch since the pull, the snapshot is rebuilt so they come first. Otherwise they join the end of unseen like any other arrival (PR3).

From PR6b on, the status row (phone) or the sub-row's text slot (desktop) is the only result for every path that queues checks: a pull, the C3 tab tap, ⋯ → Refresh, the desktop Refresh button and `r`. PR3b's manual-refresh toast is gone from these paths.

**C3:** tapping the current tab while the list is already at the top calls `checkTabNow`.

**C4:** a 44px Refresh row goes at the top of the existing Column ⋯ popover (Column.tsx:384-484) and calls `checkTabNow`. PR7b keeps it when the popover becomes a sheet.

**Desktop:** Refresh (2683-2689) and `r` (2092-2098) queue the 20 stalest healthy feeds across the visible columns. The status uses the sub-row's text slot (2677-2681).
- Branch on `isDesktop` (`App.tsx:581`, 768px), not on breakpoints. The sub-row is `hidden sm:flex` (2676), so it shows from 640px. Below 768px, its Refresh and `r` call `checkTabNow` for the current phone tab and report in the list's status row.

#### Tests
- **Unit `frontend/scripts/unit/pull.test.ts`:**
  - +1/+2 jitter, then up: `preventDefault` is never called;
  - +3, then down: `preventDefault` from then on;
  - +6, then up: tracking ends and nothing is prevented;
  - the 12px lock;
  - diagonals just under and over the 1.3 ratio, using the imported `SWIPE_*` constants;
  - `cancelable` true, then false: tracking ends;
  - a 400 ms still finger;
  - a start at `scrollTop < 0` and at `> 0`;
  - releases at 80px and 180px, checking `min(96, dy × 0.5)` and arming at 64;
  - `touchcancel`.
- **Flows** (CDP, returning early on WebKit). Chromium's touch slop (about 38px) hides moves smaller than that, so these flows cover wiring, and every move is 40px or more:
  - `flow-pull-refresh`: switch tabs first (so the rebound scroller is used), then a 180px pull. One POST with News's healthy, unmuted feed ids (the fixture's muted feed and failing feed are not in the body), the GET, and the status texts. A story arriving at +25 s is first in the list. No toast matches `/\d+ new|Nothing new/`. While the row shows, scroll 200px: when it unmounts, the first visible card's top doesn't change and no seen PATCH fires. A case where every feed is cooling down shows "Checked N min ago". Uses `page.clock.install` and `runFor`.
  - `flow-pull-pending`: one feed is still pending at +70 s, and the row reads "N new · 1 still checking".
  - `flow-pull-short`: an 80px pull does nothing.
  - `flow-pull-scrolled`: with `scrollTop > 0`, the list scrolls and nothing is posted.
  - `flow-pull-jitter` is dropped: its +2px move never reaches the page in Chromium. `pull.test.ts` covers the jitter rules.
  - `flow-pull-vs-swipe`: a horizontal drag hides the card, with no POST.
  - `flow-pull-reduced-motion`, `flow-tab-tap-refresh` (C3), `flow-tab-menu-refresh` (C4, the popover row).
  - `flow-refresh-desktop` (`widths: [1280]`), plus a 667px case where the sub-row Refresh calls `checkTabNow` and reports in the list's status row.
  - FN rejects the status texts after a quiet poll.
  - `capture.cjs` mocks check-now, including `backing_off`, `queued_at` and `expected_done_s`.
- **Smoke:** a new `refresh-status` state.

#### Risks
- The non-passive listener makes iOS wait for JavaScript on every move. It returns at once unless it is tracking, and cards already do the same.
- CDP touch in Chromium tests the wiring, not how it feels on iOS: Chromium has no rubber-band, hides moves under about 38px, and the WebKit run is report-only. The thresholds are covered by `pull.test.ts`, and the feel by Gate 2 in the Device checks list.
- **If Gate 2 fails** because a slow pull's first 1-2px move hands the touch to native scrolling (iOS has no touch slop): while tracking, listen to the list's `scroll` event. If `scrollTop` goes below 0, drop the JS transform and arm from `−scrollTop` instead (arm at −64, refresh on `touchend`). Build it only if the owner sees the problem on the device.
- Revert removes the pull and restores the old Refresh behaviour. The header button is still there until PR6c.

**Run with:** effort xhigh · plan first: yes · review: review agents: gesture reducer against iOS touch rules; queue and completion against PR6a; status row a11y and layout.

### PR6c: Slim phone header and the first-feed row (frontend track)

#### Phone header
- Delete the Refresh button (2656-2665).
- On phones the title drops from the 34px large title to 17px semibold, in one 44px row.
- On the phone, Column renders its header (label and ⋯) as the first row of the list, so it scrolls away like Reddit's sort bar. Together these give back about 64px of a 568px screen.

#### Empty states
- The first load with no columns shows "No stories yet." (2835-2837). The rest of today's sentence ("Tap Refresh to check now, or add feeds in Menu → Settings → Feeds.") goes with the header button.
- C7: while the user has no feed of their own (`isBuiltIn`, feedRules.ts:44), a 44px "Add your first feed" row opens `openSettings('feeds')`. It renders above the list, or above EmptyColumnMessage, on For You. An install can be empty: built-ins can be deleted, rows exist only after the first fetch, and For You is windowed to 2 days from PR4. The row reuses the `FirstFeedButton` that PR7a built for the meta-tab empty state, and PR11b retargets both to Add.

#### Tests
- **Smoke:**
  - The `dashboard` header becomes `['open search', 'open menu']` (smoke.cjs:145-149), and C16 still checks the title.
  - A new `first-feed-row` state.
- **Flows:** `flow-first-feed-row` (C7), including a fixture where For You is empty and the row sits above EmptyColumnMessage.

#### Risks
- Revert restores the header button and the large title.

**Run with:** effort high · plan first: no · review: /code-review high; then Gate 2.

### Device gate 2 (after PR6c)
PR6b and PR6c deploy together. These lines are the Gate 2 section of `docs/DEVICE_CHECKS.md`; check them once PR6c's main-branch build is green (see Deploy and device checks). Step 0 first: popping-update, paste the revision check, force-quit and reopen the home-screen app, and confirm Settings shows the expected build (PR1r).
- Pull at the top. Pass: the spinner follows the finger, and a release past the threshold shows "Checking N feeds…" and then a result. A short pull springs back with no check.
- Shaky upward scroll from the top. Pass: native scroll, no spinner.
- Diagonal drag. Pass: a pull or a card swipe, never both, never a stuck list.
- Long-press a link. Pass: the iOS preview, no spinner.
- Short tab. Pass: same as a pull at the top.
- Ten slow pulls in a row. Pass: all arm.
- Pass: no doubled movement (the JS offset plus the native bounce).
- Small downward wobble, then scroll up. Pass: native scroll.
- VoiceOver. Pass: it reads "Checking…" and the result.
- Swipe a card left (PR5a). Pass: it hides, and Undo brings it back.
- Open a card's ⋯ (PR5a). Pass: the sheet rises from the bottom, every row is in reach, and a tap outside closes it.

The gate never auto-passes. The owner replies "pass", "fail: \<line\>" or "waive". While it is open, frontend units after PR6c may be prepared locally but don't merge; the backend track continues (D21).

---
## PR7: Tab order, hiding, and one ⋯ sheet per tab

**What you'll notice:**
- An "Edit" pill opens ↑/↓ arrows, Move to top and Show switches. Changes apply at once.
- Brief stays first, Saved and For You are always present, and desktop follows the same order. With no saved order, tabs read Brief, Saved, For You, then categories alphabetically, as today. Once you have saved an order, new categories go at the end.
- Every tab's ⋯ opens one sheet: Mark all as read, Refresh, Sort, Show stories from, Edit tabs (PR7b).
- An empty tab says why, with one fix (PR7b; the Saved and For You empty state lands in PR7a).

**Complaints:** "can't organize categories in the order I want", "hunting through shit".

**Units:**
- **PR7a: Tab order and hiding** (frontend track). Depends on PR1b (column keys, `resolveTab`, `firstRefreshDone`) and PR2b (the `category_layout` PUT is covered by PR2b's per-key write sequence, so a stale GET can't undo it). Sixth on the frontend track: after PR2b, before PR3a. Merges once CI is green and its review step passed (D32).
- **PR7b: Tab ⋯ sheet and empty states** (frontend track). Depends on PR7a, PR3b (Mark all), PR4d (the window control and the minimal empty line), PR5a (`ActionSheet`), PR6b (the Refresh row, C4) and PR9b (`feedHealth`). Goes after PR9b. Merges once CI is green and its review step passed (D32).

### PR7a: Tab order and hiding (frontend track)

#### Changes
- **`lib/categories.ts` (extend):** add `orderColumns(available, layout) → {shown, hidden}`, `moveKey` and `normalizeLayout` beside the existing exports.
  - With no stored layout, the order is Brief, Saved, For You, then categories alphabetically by label. That is today's order (App.tsx:902-910), so nothing changes until the owner edits it, and a new category still lands alphabetically.
  - Once a layout exists, keys missing from `order` append at the end, in label order.
  - `normalizeLayout` drops non-string and duplicate keys.
  - `categoryOptions` takes an optional `order` and falls back to label order (`categories.ts:73`), so `categories.test.ts:94-97` ('yours sorts by label, ignoring case') stays green.
- **`category_layout`** `{v: 1, order, hidden}` in `preferences.tsx` (one PUT per change), plus its allowlist entry in `routes/preferences.py`. It is a whole-value key; PR2b's per-key write sequence keeps a stale GET from undoing a change.
- **Columns carry `{key, label}`.** `label` is `categoryLabel` for categories, the fixed label for meta tabs, and the name exactly as typed for `view:<id>` (PR8a). Every per-column map, React key and For You special case moves to `key`:
  - the `col.name !== 'For You'` checks (App.tsx:1064, 2939) and Column's `name !== 'For You'` (Column.tsx:385);
  - React keys and `setColumnRef` (App.tsx:2941, 3048);
  - every `columnPrefs` lookup goes through PR1c's `prefsFor(key)`.
  - Column takes `columnKey` and `label` in place of `name`, renders `label` instead of `categoryLabel(name)` (Column.tsx:336), and compares both in `_columnPropsEqual`. The tab strip renders `label` (App.tsx:3061).
- **`App.tsx`:**
  - Categories already come from active sources together with entries: PR4d did this (with unmuted sources from PR5b on), so PR7a only orders them.
  - `allSubsColumns` (977-992) always includes `meta:saved` and `meta:foryou` (today both are skipped when empty, 984-987), then applies `orderColumns`. The tab a fresh device opens still comes from `resolveTab`.
  - Everything that walks tabs uses `orderColumns().shown`: `resolveTab`'s fresh-device pick, the Jump list (built from `shown`, not from `categories`: App.tsx:3160, Drawer.tsx:386-401), j/k and the badge sum. C8's next-tab row (PR3b) is built on `shown` too. The desktop For You row renders only when `meta:foryou` is shown (App.tsx:2848). Hiding a tab doesn't mute its stories in For You.
  - The desktop grid and the Jump list use the same order.
  - The 44px Edit pill sits outside `role="tablist"` (3013-3018), pinned at the trailing edge.
  - A tab selected by code scrolls into view.
  - Delete the `columns.length === 0` branch (2824-2837) except the first-load skeleton, because columns are never empty now. The skeleton renders while `!firstRefreshDone` (PR1b's gate), never on `columns.length === 0`, because Saved and For You are present from the first render.
  - **Saved and For You empty state:** an empty meta tab shows "No stories yet". While the user has no feed of their own (`isBuiltIn`, feedRules.ts:44-46), it adds a 44px "Add your first feed" button that opens `openSettings('feeds')`. Build the button once (for example `FirstFeedButton`), so PR6c's C7 row reuses it and PR11b retargets both.
- **`TabsEditor.tsx` (new):**
  - Built on `GroupedSection` / `GroupedRow`, which move from `Drawer.tsx:520-618` to `components/Grouped.tsx`. `Grouped.tsx` also exports `ChevronRight` (Drawer.tsx:620-638), which Drawer imports for its remaining row (487; the use at 600 is inside `GroupedRow` and moves with it).
  - `GroupedRow` gets a `trailing?: ReactNode` slot. With `trailing` set it renders a `div`, not a button, so the arrows and the switch are never nested inside a button. Rows keep their 44px minimum.
  - Each row: ↑/↓ (44px, disabled at the ends), Move to top (one tap, the same `moveKey`, placing the tab right after Brief), and a Show switch. Brief is pinned first. Done closes the editor.
  - Buttons are labelled "Move \<tab\> up", "Move \<tab\> down" and "Move \<tab\> to top". After a move, focus returns to the same arrow on the moved row, or to the opposite arrow if the pressed one is now disabled. A polite `aria-live` text inside the dialog, empty while idle, announces "\<tab\>, 3 of 8". It is not `role="status"` (Rules).
  - A dialog with a focus trap, in `C12_ROOTS`. It follows the Rules' sheet notes: the scroll body is `overflow-y-auto overscroll-contain`, and transitions are motion-safe only.
  - PR3c, which comes later, counts its open state in App's `modalOpen`, so seen-on-scroll pauses while it is open. From PR5a, `onKey` also returns early while it is open.
  - Opened from the pill and from a Menu "Edit tabs" row. PR7b adds the tab sheet's row.
  - Its last row is the hook point for PR8a's "New tab" row.
- **`CategoryPicker`** lists categories in tab order: both call sites, FeedEditor.tsx:397 and FeedManager.tsx:1219, pass `order` from `usePreferences()`'s `category_layout`.

#### Tests
- **Unit:** extend `categories.test.ts`:
  - `orderColumns` with no layout (Brief, Saved, For You, then categories alphabetically) and with a layout (unknown keys append at the end);
  - `moveKey`, including Move to top;
  - `normalizeLayout`;
  - `categoryOptions` with and without `order` (94-97 stays green).
- **Unit:** `columns.test.ts` adds "no migration before the first refresh while meta tabs are present".
- **pytest:**
  - The allowlist (`category_layout` round-trips).
  - `test_frontend_slice19_empty_column_source_status.py`: the meta literal at `:84` changes to "No stories yet". The rest of the rewrite is PR7b's.
  - `test_slice21_column_memo.py`: the prop list (`:94-109`) gains `columnKey` and `label`.
- **Flows:**
  - `flow-tabs-reorder`: at most 3 taps and one PUT (Edit, ↑, Done). Moving the last tab to second place takes at most 3 taps with Move to top. Focus lands as specified. The desktop order is checked with `widths: [1280]`.
  - `flow-tab-hide`: also checks that the hidden tab is absent from Jump and from j/k.
  - `flow-new-category-at-end` (with a stored layout), plus a case with no layout, where a new category lands alphabetically.
  - `flow-saved-always`, `flow-fresh-device-opens-foryou`.
  - `flow-first-feed-empty-meta`: a route override that returns only built-in feeds and no stories shows "No stories yet" with "Add your first feed" on For You, and the button opens Settings → Feeds.
  - FM stays green. FU (flows.cjs:381-396) still passes with source 15's category (fixtures.cjs:88, a new tab since PR4d) last in the Jump list.
- **Smoke:** add `tabs-editor` (390/320). The pill is visible at 312.

#### Risks and rollback
- Nothing changes until the owner edits the order.
- Revert drops the order, and the key is then ignored. Old bundles ignore `category_layout`.

**Run with:** effort high · plan first: no · review: /code-review high; tap-count flows.

### PR7b: Tab ⋯ sheet and empty states (frontend track)

#### Changes
- **The tab ⋯ becomes a bottom sheet:** PR5a's `ActionSheet`, extended to accept `{action | segmented | disabled}` rows and re-rendered by its host from current state while open, with `aria-label="tab actions"`. Its rows, in order:
  - Mark all as read (44px, disabled at 0; PR3b);
  - Refresh (C4; PR6b added it to the popover, and the sheet keeps it);
  - Sort, Newest | Best (PR1c; hidden on `meta:saved`);
  - Show stories from, the 4-way control (PR4d; hidden on Saved, which always lists every save);
  - Edit tabs (opens TabsEditor).
  - PR8a adds the rows a `view:<id>` tab needs.
- **The popover goes:** Column.tsx:405-484 and its pointerdown/Esc effects (239-260).
- **`EmptyColumnMessage`** (Column.tsx:820-877) gains `columnFeeds` in place of `sourcesByName`: the tab's feeds, memoized in App per column and added to the comparator. It classifies them with `feedHealth` (PR9b, which also deleted the local `AUTO_DISABLE_THRESHOLD`, Column.tsx:814-818). Its messages:
  - "Waiting for first check"
  - "2 feeds failing · Open" (opens Settings → Feeds, where Needs attention is first)
  - "Nothing in 2 days · Show 7 days" (added by PR4d; kept)
  - "No stories yet" for meta tabs, with the first-feed button (PR7a)
- If Column no longer needs `sourcesByName`, drop the prop and its call-site arguments.

#### Tests
- **pytest:**
  - **Rewrite `test_frontend_slice19_empty_column_source_status.py`.** Its six tests pin Column.tsx and App.tsx details:
    - `sourcesByName` typed as `Map<` in Column's props (:54-70)
    - the meta literal (:84, already changed by PR7a) and `last_error`, `error_count` and `last_fetch_at` (:88-91)
    - `sourcesByName` with `.get(` (:99-118)
    - `sourcesByName=` on every `<Column` (:127-144; the regex is at `:138`, and the test also asserts at least 2 Column call sites)
    - a `.slice(0, N)` (:152-163)
    - `auto_disabled` (:171-186)
  - The rewrite pins `columnFeeds` and the messages above instead. It keeps PR4d's pin of "Nothing in 2 days · Show 7 days" with fixture source 15.
  - `test_slice21_column_memo.py` adds `columnFeeds`, and drops `sourcesByName` if Column no longer takes it.
- **Flows:**
  - `flow-tab-sheet`:
    - `page.mouse.click` at ⋯'s centre lands on the backdrop and closes the sheet without reopening it;
    - Esc closes it;
    - focus returns to ⋯;
    - one open gives exactly one sheet;
    - after a Sort or window change, the open sheet shows the new value.
  - FM stays green.
- **Smoke:** `column-popover` becomes `tab-sheet` (390/320).

#### Risks and rollback
- Revert restores the popover. No stored data changes.

**Run with:** effort high · plan first: no · review: /code-review high.

---

## PR8: Saved views become custom tabs

**What you'll notice:**
- Your views are tabs, placed right after For You. Each has "From: BBC News, The Verge, +3 · Edit" under its header.
- If you had no saved tab order, converting your views saves one, so from then on a new category goes at the end of the strip. Move it with Edit.
- Edit tabs → New tab makes a tab of your own: name it, tick its feeds, Done.
- Ticking a feed saves at once.
- On a custom tab, a card's ⋯ offers "Remove \<feed\> from this tab".
- Tapping a feed's name on a card opens just that feed; Back returns you to your tab (D26).
- The chip rows above the tabs are gone (PR8b).

**Units:**
- **PR8a: Custom tabs** (frontend track). Depends on PR7a (TabsEditor, `category_layout`, `{key, label}` columns), PR7b (the tab sheet), PR1b, PR3b (`resetSnapshot`, seen tracking), PR4d (the window fetch parameters), PR5b (`isShown` and muted feeds) and PR6b (pull queues a tab's feeds). Goes after PR7b. Merges once CI is green and its review step passed (D32).
- **PR8b: Remove chips, presets and the old Sources UI** (frontend track). Depends on PR8a. Goes right after it. Merges once CI is green and its review step passed (D32).

### PR8a: Custom tabs (frontend track)

#### Data (D8)
- `custom_tabs` `{v: 1, tabs: [{id, name, sources: number[]}]}` (cap 50), in a pure `lib/customTabs.ts`. Allowlist the new key. It is a whole-value key, covered by PR2b's per-key write sequence.
- **One-time conversion** from a non-empty `filter_presets`:
  - It runs only when all of these hold: `!prefs.loading`, `firstRefreshDone`, `sources.length > 0`, and the server returned no `custom_tabs` row. Row presence is tracked as `null` until a row arrives; it is never inferred from empty data.
  - The row's existence is the "done" marker. Deleting the last tab writes `{v: 1, tabs: []}`, never a DELETE, so the conversion never runs again.
  - Names map to ids through `sourcesByName` (App.tsx:844-849). `columnPrefs` are dropped.
  - Ids come from `preset.id`. Presets without a string id are skipped. Ids that don't match `^[A-Za-z0-9_-]{1,24}$` are regenerated, and duplicates are deduped.
  - A tab is kept when some names don't resolve. A preset whose names are all stale still becomes a tab, whose From line reads "From: no feeds · Edit".
  - In the same step, the converted `view:<id>` keys go right after `meta:foryou` in `category_layout.order`, in preset order. With no stored layout, a default layout is seeded (Brief, Saved, For You, the views, then categories as today). An existing layout keeps its order around them.
- `filter_presets` is left untouched, for rollback.

#### Keys
- `column_prefs` suffixes become column keys. Reads use `columnPrefs[key] ?? columnPrefs[legacyName]`. `view:<id>` keys have no legacy fallback.
- Seen marks need nothing new. Marks are stored under the story's category (`seen:<category>`, PR2a and PR3b), and reads are the union of `seen:*` and the legacy `read_entries:*` rows, so custom tabs need no key of their own.

#### `App.tsx`
- Custom tabs join as `view:<id>`, in `category_layout` order. Their label is the name exactly as typed (PR7a).
- **Fetch scope:**
  - The phone fetches only the selected custom tab: `entries({source: names, limit: 200, maxAgeHours, perSourceCap: 25, activeOnly: true})`, refetched on each visible poll.
  - Desktop fetches its custom columns one at a time, with a fixed per-poll limit (a constant, starting at 3).
  - Each custom tab's fetched rows join `pruneIds`' keep set (PR3b, PR4d), so their marks are never pruned.
- Badges for custom tabs that aren't selected come from the dashboard data, filtered by `source_id`, before muted filtering (custom tabs ignore mutes).
- A custom tab ignores `muted_sources`, and TabSheet marks muted feeds.
- Editing a tab's feeds calls `resetSnapshot` for it (PR3b).
- Pull / Refresh queues the tab's feeds.
- Seen tracking, the marker and Earlier apply unchanged.
- **Card ⋯ on a custom tab:** `buildActions` receives the tab kind. On `view:<id>`, "Hide stories from \<feed\>" becomes "Remove \<feed\> from this tab":
  - it removes that source id from the tab's `sources` through the same debounced `custom_tabs` PUT TabSheet uses, and resets the snapshot;
  - it shows "\<Feed\> removed from \<tab\> · Undo", where Undo restores the previous sources;
  - it is hidden when the feed is the tab's only source.
  - Category tabs, For You and search keep "Hide stories from \<feed\>".
- The tab sheet (PR7b) on a `view:<id>` tab shows its rows as on any tab. Editing the tab's feeds goes through the From line.

#### New tab
- A 44px "New tab" row at the bottom of TabsEditor, reachable from the Edit pill, the Menu's "Edit tabs" and the tab ⋯ → Edit tabs.
- One tap:
  - creates `{id: 't-<base36>', name: '', sources: []}`;
  - inserts `view:<id>` at the end of `category_layout.order`;
  - opens TabSheet with the name field focused in the same tap (`flushSync`, as Drawer.tsx:376 does).
- Nothing is PUT until the tab has a name and at least one feed. Done or a dismiss with an empty name or no feeds discards the tab.
- Done closes TabSheet and TabsEditor and selects the new tab.
- The 6 taps: Edit (1), New tab (2; the name field is already focused), type the name, tick three feeds (3-5), Done (6).

#### `Column.tsx`
- A 44px `<button type="button" aria-label="Edit feeds in <tab>">` reading "From: … · Edit" opens TabSheet. It is never a `<section>`. On the phone it scrolls with the list. It counts only feed ids that still exist.

#### `TabSheet.tsx` (new)
- A 17px name field (trimmed, 1-40 characters; duplicate names are allowed, because keys differ) and a search field.
- A checklist grouped in tab order, labelled with `sourceLabel`, with each feed's `FeedStatus` pill (PR9b shipped it).
- Debounced PUTs, an inline delete confirm, a Done button, and a dialog in `C12_ROOTS`.
- While it is open, TabSheet counts in App's `modalOpen` (PR3c), so seen-on-scroll pauses and `onKey` returns early (PR5a).
- It follows the Rules' sheet notes. Because it has text fields, its bottom padding uses `useVisualViewport().kbInset`.

#### One-feed view (D26)
- The card's feed button (PR5a's header line) opens a temporary view of that one feed instead of the feed editor. Its label becomes "open feed \<name\>". Edit feed stays in the card's ⋯.
- Opening pushes one history entry (`{popping: 'feed'}`, `?view=feed&feed=<id>`), the same pattern `openSettings` uses (App.tsx:526-556), so Back closes it. The view's header row holds ‹ (Back), the feed's label, and Edit feed (`openSettings('feeds', id)`).
- On the phone it replaces the tab's list below the strip. On desktop it replaces the grid with one column, as search results do (App.tsx:2777).
- It is never stored: not in `category_layout`, `custom_tabs` or `mobileTab`. Nothing that walks tabs sees it (C8, Jump, j/k, badges). Leaving it returns to the same tab with that tab's snapshot kept, because it is not a tab switch.
- It fetches `entries({source: [name], limit: 100, maxAgeHours: 168})`, newest first. Paused and muted feeds still show here, because you asked for this feed.
- Opening a story marks it seen. On the phone, seen-on-scroll applies, and the caught-up marker ends the list.
- Its cards' ⋯ leave out "Hide stories from \<feed\>".

#### Reset
- Reset never deletes `custom_tabs`, `category_layout` or `muted_sources`.

#### Tests
- **Unit:**
  - `customTabs.test.ts`: the conversion with empty sources (no write), one stale name, all names stale, and one malformed preset; id regeneration and dedupe.
  - `columns.test.ts`: the legacy `column_prefs` fallback.
- **pytest:** the allowlist (`custom_tabs` round-trips, including for anonymous users).
- **Flows:**
  - `flow-custom-tab-create`: at most 6 taps, as counted above. The new tab sits at the end of the strip, and nothing is PUT before it has a name and a feed.
  - `flow-custom-tab-from-line`.
  - `flow-preset-convert`:
    - delays `/api/sources` until after the preferences GET;
    - includes one stale name;
    - covers "every tab deleted" (no reconversion);
    - checks the tabs' position with and without a stored layout.
  - `flow-reset-keeps-data`: Reset sends no DELETE for `custom_tabs`, `category_layout` or `muted_sources`.
  - `flow-mute-source` gets a custom-tab case: "Remove \<feed\> from this tab" removes its stories, `muted_sources` is unchanged, and Undo restores the feed.
  - `flow-feed-view`: tapping a card's feed name opens `?view=feed&feed=<id>` with only that feed's cards, and Back returns to the same tab.
  - FV (flows.cjs:845-864): the feed button now opens the one-feed view. Its editor assertion moves to ⋯ → Edit feed.
- **Smoke:** add `tab-sheet-custom`, `custom-tab`, `tabs-editor-new` (asserting the new tab's position in the strip) and `feed-view` (390/320).

#### Risks and rollback
- `filter_presets` is frozen at conversion time. After a revert, views that include feeds renamed or deleted since PR8 need those feeds re-ticked.
- Views saved on a device still running an old bundle after the conversion don't become tabs, because old bundles ignore `custom_tabs`. Relaunching every device after a deploy (D21) closes the window. If it ever bites: record the converted preset ids in `custom_tabs` (`{v: 1, tabs, converted: [ids]}`) and convert any unlisted id, create-only.
- Legacy `column_prefs:<name>` writes from stale bundles are shadowed by `column_prefs:<key>`.

**Run with:** effort xhigh · plan first: yes · review: review agents: conversion and data safety; keys, focus and UI.

### PR8b: Remove chips, presets and the old Sources UI (frontend track)

#### Removed
- `PresetChips` (App.tsx:227-460, rendered at 2769-2775), including its slot in PR3a's desktop scroller.
- `viewKind` (960-970) and the multisub block (994-1042). Keep `allSubsColumns` (977-992). Rewrite `baseColumns` (1044) as `allSubsColumns` plus the `view:<id>` columns.
- `activeSources` (571) and its refetch effect (1508-1531); `refresh()`'s `sourceArg` and its deps (1211-1223, 1296); the desktop `viewKind` gates (2848, 2935, 2939); the Drawer filter props (3163-3172); the `FilterPreset` type (219-224) and its use.
- `meta:filter` in `lib/columns.ts`, with `resolveTab`'s `meta:filter` case.
- The filter chip row (2703-2734).
- The handlers at 1860-1931 (`applyPreset`, `savePreset`, `deletePreset`, `renamePreset`, `updatePreset`), 2189-2219 (`toggleSource`, `onSourceRenamed`) and 2362-2369 (`toggleSourceAndMaybeClose`, `clearSourceFilters`).
- Drawer "Save current view" (340-383) and "Sources" (404-495).
- `onRenamed` becomes optional in `sourceSaves.ts` (:41, called at :307), because ids don't change on rename. App stops passing it (2282-2283, 2307). `sourceSaves.test.ts:48` still passes one.
- `removeSource` (2253-2279) prunes the id from every custom tab instead of pruning `activeSources` and `filter_presets`.
- `filter_presets` stays readable but is never written again, for rollback.

#### Tests
- **pytest `test_slice21_column_memo.py`:** HANDLERS drop `toggleSource`, `toggleSourceAndMaybeClose`, `clearSourceFilters` and `onSourceRenamed`, and add `selectTab`.
- **Flows:**
  - FT, FA and FA2 (flows.cjs:357-462) and `capture.cjs:534` open the editor through Settings or `?source=`, because the Menu's Sources rows (Drawer.tsx:479) are gone.
  - FL (flows.cjs:728-766) becomes `flow-fl-rename-in-custom-tab`: a rename keeps the custom tab and updates its From line.
  - FPR (flows.cjs:1040-1068) is retired. `flow-custom-tab-create` (PR8a) replaces it.
  - FU stays at 390 here. Its desktop rewrite at 1280 is in PR12a.
- **Smoke:** no change. `smoke.cjs` has no preset-strip expectations.

#### Risks and rollback
- This removes UI the owner used. The old views survive in `filter_presets`, and a revert brings the chips back.

**Run with:** effort high · plan first: no · review: Explore inventory first (every `activeSources` and preset call site); /code-review high, focused on call-site completeness.

---
## PR9: Feed health you can see (migration 0031)

**What you'll notice:**
- Every feed row has a pill: Checking, 12 this week, Quiet, Blocked, Failing, Paused (which also covers a feed Popping stopped after repeated errors, with the reason), Reddit unavailable, or Not checked yet.
- Each problem shows one fix.
- Feeds has search, then "Needs attention" first, then categories in tab order, then "Built in".
- After a pull, the status row reads "3 new · 1 failing", and "1 failing" opens Feeds, where Needs attention is the first group.

**Complaints:** "it didn't tell me if it is actually pulling", "change the settings, like browser".

**Units:**
- **PR9a: Migration 0031 and feed health backend** (backend track). Depends on PR0 (head 0030), PR4a and PR6a, which edit `scheduler.py` before it, in that order. Merge condition: the owner reads the rollback note below and merges it (D32).
- **PR9b: Feed health UI** (frontend track, the first unit after Gate 2). Depends on PR9a, PR6b (the status row and `checkTabNow`) and PR7a (tab order for the headings). Merge condition: CI green and its review step (D32).
- **Moved out of PR9:**
  - The website blocked and no-list errors go to PR10b, which rewrites the same crawl functions and tests. D27 still applies to them.
  - PATCH `type` and `discover_feed_url(headers=)` go to PR11a. Its preview uses the headers, and PR11c is the only caller of PATCH `type`.
- **Used later:** PR7b's `EmptyColumnMessage`, PR8a's TabSheet slot, PR11b's add result and PR12a's Menu subtitle use `FeedStatus` and `feedHealth()` from PR9b.

### PR9a: Migration 0031 and feed health backend (backend track)

#### Migration
`0031_source_health.py` (`down_revision = "0030"`) adds `last_attempt_at` (timestamptz), `last_item_count` (int) and `last_new_count` (int).
- All three are nullable with no default, and the `models.py` fields are Optional.
- There is no backfill. Existing rows keep NULL until their next run, and PR9b's pill falls back to `last_fetch_at`.
- There is no `fetch_mode` (D18).

#### Backend
**`_ingest` writes:**
- `last_attempt_at` is the completion time of every run, written in one finally-style block on every path: success, failure, `[no list]` and `[reddit off]`.
- On success, also `last_item_count` (counted before the cap) and `last_new_count`.
- When paused, `summary["status"] = "paused"`. Add `IngestResult.status: Optional[str]`. `IngestResult` (schemas.py:497-502) ignores unknown keys, so without the field, `IngestResult(**summary)` (`routes/ingest.py:54-55`) would silently drop it.
- From here on, PR6a's `queue_check` backoff gate can read `last_attempt_at` instead of the job's `next_run_time`.

**Setup states (D27):**
- `[no list]` and `[reddit off]` are configuration states, not failures, whatever the feed's age.
- They set `last_error` and `last_attempt_at`. They never increment `error_count` (models.py:76), never back off, and never auto-disable. Today every failure increments `error_count` and auto-disables at `_AUTO_DISABLE_THRESHOLD = 6` (scheduler.py:206, 530-566).
- Their fix re-enables the feed: the no-list fix's PATCH also sends `active: true` (PR9b, PR10b).
- This replaces the earlier rule that only `[no list]` within 24 h of `created_at` skipped `error_count`. PR10b's removal of the whole-sitemap fallback would otherwise stop existing section feeds within a day.

**Reddit off:**
- `_ingest` checks `reddit_client.is_disabled()` before fetching, only when the feed's type is `reddit`. `is_disabled()` is a global setting (reddit_client.py:268-273), and only Reddit feeds depend on it.

**Not a feed:**
- `fetch_rss` raises `NotAFeedError` when there are zero entries and the response is HTML. The existing zero-entry bozo raise (rss.py:474-482) becomes this error.
- `NotAFeedError` subclasses `ValueError` and keeps "parse" in its message, so `_test_source_impl` still reports `parse_error` (routes/sources.py:1196).
- Keep the single `ET.fromstring`.
- A valid empty feed is still a success.

**Built-in fetchers raise:**
- `nvd.py:127-134`, `cisa_kev.py:95-102` and `wikipedia_on_this_day.py:105-112` log and then re-raise instead of `return []`, so `_classify_error` labels them and their pills can say Failing or Blocked.
- `github_releases` keeps returning `[]` for one repo's 304 or non-200 (github_releases.py:93, 106), and raises from `fetch()` only when every repo failed.
- `dynamic_reddit`'s throttle path stays as it is.

**Error kinds:** `_classify_error` (scheduler.py:220-242) gains the new classes. One table maps each `last_error` prefix to PR9b's action:

| `last_error` prefix | Comes from | Action (PR9b) |
|---|---|---|
| `[blocked]` | HTTP 401, 402, 403 | Read as Safari when allowed (PR9b's gate), otherwise Check now |
| `[not found]` | HTTP 404 | Edit address |
| `[upstream error]` | HTTP 5xx | Check now |
| `[http error]` | other HTTP errors, including 429 | Check now |
| `[network]` | connect errors and timeouts | Check now |
| `[not a feed]` | `NotAFeedError` | Edit address (Find its feed in PR11c) |
| `[no list]` | `NoArticleListError` (defined here, raised by PR10b) | Pick a section |
| `[reddit off]` | Reddit disabled on the server | none; the pill reads "Reddit unavailable" |
| `[error]` | anything else | Check now |

There is no "timeout" kind: timeouts already map to `[network]`.

**`SourceOut`** gains `last_attempt_at`, `last_item_count`, `last_new_count`, `stories_24h`, `stories_7d`, `newest_story_at` and `has_stories`.
- One `_health_counts(session, ids)` helper serves every endpoint that returns a `SourceOut`:
  - the list (`routes/sources.py:463-474`);
  - `GET /sources/{id}` (:477);
  - POST (`create_source_endpoint`, :501);
  - PATCH (`update_source_endpoint`, :774);
  - `/auto` (:671).
  Today only the list fills computed fields (`net_vote_score`), so the others would return zeros.
- The story counts come from one `GROUP BY` over 7 days with `FILTER`. `newest_story_at` is `max(fetched_at)` inside that same `GROUP BY`, so it is null when nothing arrived this week.
- `has_stories` stays an `EXISTS` on `source_id`.
- `GET /sources/{id}` runs the same query with `WHERE source_id = :id`, so PR9b's 5-second poll stays cheap.
- Run `EXPLAIN` on `/sources`, including a source with no entries. An all-time `max(fetched_at)` per source walks the whole `ix_entries_fetched_at` index for such a source: 159 ms on a 400k-row synthetic table, against 4 ms for the 7-day `GROUP BY`.

**Other endpoints:**
- `trigger_now` reuses the job's plugin.

#### Tests
- **pytest (new):**
  - `test_migration_0031_source_health.py`, as two tests. Today nothing runs Alembic in tests, because conftest builds the schema with `create_all` (conftest.py:72-80).
    - A no_db test: the `ScriptDirectory` heads are `['0031']`, and the revision's `down_revision` is `'0030'`.
    - A db test: create a scratch database, run `CREATE EXTENSION vector`, then run `alembic upgrade head`, `alembic downgrade 0030` and `alembic upgrade head` in a subprocess against it. Assert the three nullable columns through `information_schema`, then drop the database.
  - `test_source_health_fields.py`: `last_attempt_at` is written, at or after the run's start, on success, failure, `[no list]` and `[reddit off]`.
  - `test_rss_not_a_feed.py`.
  - `test_sources_story_counts.py`: the list, `GET /sources/{id}`, POST and PATCH all carry real counts; a source with no entries.
  - `test_ingest_paused_status.py`.
  - `test_builtin_fetchers_raise.py`: nvd, cisa_kev and wikipedia_on_this_day re-raise; github_releases raises only when every repo failed.
- **pytest (extend):**
  - `test_scheduler_backoff.py`:
    - classify cases for every prefix in the table;
    - a feed older than 24 h that hits `[no list]` six times stays active;
    - `[reddit off]` never touches `error_count`;
    - a non-Reddit feed is unaffected while Reddit is disabled.
  - `test_trigger_now_accepts_dynamic_sources.py`.
- `test_source_patch_type.py` moved to PR11a with PATCH `type`.

#### Rollback
1. **Preferred:** revert only the app code. Keep `0031_source_health.py` in `alembic/versions`. The columns are nullable, so old code ignores them, and the head stays at 0031.
2. **If the schema really must go:** while the 0031 image is still deployed, run `docker compose exec backend alembic downgrade 0030`. Then merge the revert, wait for the main build to publish the images, and run popping-update.
   - The order matters. `backend/Dockerfile:48` runs `alembic upgrade head && uvicorn`. A reverted image has no 0031 script while the database is at 0031, so the upgrade fails and uvicorn never starts. The reverted image also can't run the downgrade.
3. **If that order was missed** and the backend no longer starts: `docker compose run --rm backend alembic stamp --purge 0030`. `--purge` is needed because the reverted image can't resolve revision 0031. The columns stay (nullable, ignored). If the PR is re-applied later, run `alembic stamp 0031` instead of upgrading, because the columns already exist.
   - popping-update prunes old images. To get an old build back, use a one-shot `POPPING_IMAGE_TAG=sha-<commit>` on the command line, never a `.env` pin, which would silently stop every later popping-update.

#### Risks
- Built-ins that now raise can show Failing or Blocked where they used to look healthy. That is the fix for "Fetchers swallow errors".
- The backend fields do nothing visible until PR9b.

**Run with:** effort xhigh · plan first: yes · review: review agents: migration and rollback; ingest bookkeeping; query cost; the owner merges.

### PR9b: Feed health UI (frontend track)

#### Frontend
**`api.source(id)`.**

**`lib/feedHealth.ts` (pure): `feedHealth()` and `needsAttention()`.**
- **Pill order** (first match wins):
  1. **Paused.** This covers Stopped (auto-disabled after errors) and shows the reason, for example "stopped after 6 failed checks".
  2. **Blocked, Failing or Reddit unavailable**, by the `last_error` kind.
  3. **Checking**, while a check is in flight.
  4. **"N this week"** when `stories_7d > 0`, otherwise **Quiet**. "Last checked" is `last_attempt_at`, falling back to `last_fetch_at`.
  5. **Not checked yet**, only when `last_attempt_at`, `last_fetch_at` and `last_error` are all null.
- No backfill: a row from before 0031 renders through the fallback in step 4.
- Quiet means the feed works but brought nothing new in 7 days, whatever the cause.
- Needs attention lists Blocked and Failing feeds, Paused feeds that Popping stopped, and Quiet feeds that have never brought a story (`!has_stories`).

**`apiErrors.ts` `ERROR_ACTIONS`:**
- One sentence and one action per kind, keyed by the literal `last_error` prefix from PR9a's table. Every prefix `_classify_error` can emit has an entry.

  | Kind | Action |
  |---|---|
  | `[not found]` | Edit address |
  | `[network]`, `[upstream error]`, `[http error]`, `[error]` | Check now |
  | `[blocked]` | Read as Safari, with Undo, when the gate below allows it; otherwise Check now |
  | Paused (stopped or paused) | Resume |
  | `[no list]` | Pick a section (its PATCH also sends `active: true`, D27) |
  | Quiet | Check now |
  | `[not a feed]` | Edit address (Find its feed in PR11c) |
  | `[reddit off]` | No fix; one line saying Reddit isn't set up on the server |

**Read as Safari, gated:**
- Offered for a blocked feed only when `!isBuiltIn(source) && HEADER_TYPES.includes(source.type)` and `custom_headers` has no User-Agent.
- One exported predicate serves both this and FeedEditor's `offerIdentity` (FeedEditor.tsx:224, 279-282), so the two can't drift apart.
- Why: built-ins ignore `custom_headers` (rss.py:533-534), and `update_source` returns early for registered names (scheduler.py:2440-2442). The fix would save a header that is never sent, report success, and leave the pill on Blocked. A blocked built-in (such as RFD's 402), or a feed that already sends a User-Agent, gets Check now instead.
- Optional: the PATCH route refuses `custom_headers` on registered built-ins. Today it refuses only name and URL (routes/sources.py:813-822).
- **One name (D15):**
  - The Last problem button (FeedEditor.tsx:349), the forbidden-site text (apiErrors.ts:183) and the header preset (headers.ts:46) are all renamed "Read as Safari". Today all three say "Use a browser identity".
  - Rewrites: FE and FE3 (flows.cjs:568, 612), `settings-add-headers` (smoke.cjs:263), `apiErrors.test.ts:95` (extend; the file exists), and the `settings-add-headers` row in `scripts/iphone-audit/README.md:96`.

**`FeedStatus`:**
- Appears in `SourceRow`, TabSheet (PR8a's slot), `EmptyColumnMessage` and the Menu subtitle (PR12a).
- `EmptyColumnMessage`'s failing check (Column.tsx:842) goes through `feedHealth()`. Delete the local `AUTO_DISABLE_THRESHOLD = 5` (Column.tsx:818), which disagrees with the backend's 6.
- If that breaks slice19's `error_count` or `auto_disabled` pins (`test_frontend_slice19_empty_column_source_status.py:74-91, 171-186`), update them here. PR7b's rewrite then pins `columnFeeds`.

**`MyFeedsTab` (210-252):**
- Gains a sticky 17px search, then grouped headings: Needs attention, then categories in tab order (PR7a's `orderColumns`), then Built in.
- Group membership and order are frozen while the Feeds tab stays mounted, or until the search text changes. Today the order freezes only while a row is open (FeedManager.tsx:227-230), so Read as Safari on a closed row would move it under the finger at the next poll. Regroup on the next open.

**`applyServerSource` (App.tsx:2231-2238) and `insertSource` (2241-2246):**
- These adopt the health fields from the server row as returned. PR9a fills them on every endpoint, so pills don't flicker after a save.
- `net_vote_score` keeps today's handling.

**Check now:**
1. Keep blur, `flushEditors` and `whenIdle(id)` (FeedEditor.tsx:292-299) before the POST, so the check uses the saved address and headers.
2. `checkNow([id], true)`. If the id comes back as `cooling_down` or `paused`, show the current state at once.
3. Otherwise poll `api.source(id)` every 5 s for up to 90 s, and stop once `last_attempt_at >= queued_at`.
4. Then show an inline line with 3 titles from `api.entries({source: name, limit: 3})`, keeping rows fetched at or after `queued_at`.

PR11b's add result uses the same mechanism.

**`checkTabNow` (PR6b):**
- From here on, a feed counts as done once its `last_attempt_at` is at or after the server's `queued_at` (PR6a), not the phone's clock.
- It waits only on `queued_ids`. Feeds reported as cooling down, paused or backing off are not waited on.

**The pull result:**
- The status row reads "3 new · 1 failing".
- "1 failing" opens `openSettings('feeds')`, where Needs attention is the first group, so no signature change is needed.
- A result row with a link stays up for at least 6 s, and the link is 44px.

#### Tests
- **Unit:**
  - `feedHealth.test.ts` (new): every pill; a pre-0031 row with no `last_attempt_at` falls back to `last_fetch_at`; all-null reads Not checked yet; a blocked built-in gets Check now; a blocked feed that already sends a User-Agent gets Check now.
  - `apiErrors.test.ts` (extend): every prefix `_classify_error` can emit has an `ERROR_ACTIONS` entry; the renamed text at :95.
- **Fixtures:** `fixtures.cjs` `SOURCES` (68-93) gains one row per pill, with the new fields: Blocked, Failing, Paused after errors, Quiet with no stories ever, Reddit unavailable, Not checked yet, and N this week.
- **Flows:**
  - New: `flow-feeds-needs-attention`, `flow-feed-search`, `flow-check-now-inline` (cooling down shows at once), `flow-pull-results` (the link row stays at least 6 s and is 44px), `flow-read-as-safari` (3 taps and Undo; the row keeps its group), `flow-pill-stable-after-edit` (group membership frozen while Feeds is open).
  - Rewrite: FE, FE3 and `settings-add-headers` for the new name.
- **Smoke:** `settings-feeds`.
- **pytest:** the slice19 pins, if `EmptyColumnMessage` changes break them (above).

#### Risks
- The feeds screen is busier. The pill words are kept to eight, and Stopped folds into Paused.
- Revert is safe: old frontends ignore the new fields.

**Run with:** effort high · plan first: no · review: /code-review high.

---

## PR10: Website feeds pull only articles

**What you'll notice:**
- A pasted site pulls its posts, not its About, Contact, tag, author or shop pages.
- A site that blocks us says Blocked, with one fix. A site with no article list says so, and Popping keeps checking it (D27).

**Complaints:** "it pulls random not articles from the site".

It uses only installed packages: `courlan`, `lxml`, `tld` and `trafilatura` (`requirements.lock:47, 109, 219, 230`).

**Units:**
- **PR10a: article_detect library** (backend track). Pure code with fixtures and tests, no wiring and no behaviour change. It sits after PR9a in the track and depends on nothing else. Merge condition: CI green and its review step.
- **PR10b: Website crawl wiring and errors** (backend track). Depends on:
  - PR10a;
  - PR9a (`NoArticleListError` and its `[no list]` class, the D27 setup states, `last_attempt_at`);
  - PR4a (the `ingest.max_undated_per_poll` knob and the known-URL query, which now runs before the ingest rule);
  - PR1a (`_extract_one` already runs trafilatura in `asyncio.to_thread`).
  Merge condition: the owner's read-only VPS probe compares item counts on the existing website feeds before merge.
- **Revert:** PR10b reverts on its own only until PR11a merges. After that, fix forward or revert newest first.

### PR10a: article_detect library (backend track)

#### Backend
**`app/article_detect.py` (new, pure):**
- **`url_ok(url, site)`:**
  - `courlan.check_url(strict=True)` is used only as a yes/no test. Keep the original URL and never store the normalized one, because strict normalizing drops query parameters that aren't allowlisted.
  - Skip courlan's domain check when the host equals the source's own host. It rejects an IP or localhost with a port, which every loopback test server uses.
  - Plus a list of path segments that aren't articles.
- **`classify_sitemap(url)`:**
  - Classify on whole subtype tokens first: `wp-sitemap-posts-page-1.xml` contains "posts" but is a page sitemap.
  - Prefer news, post, article and blog sitemaps. Skip taxonomy, author, product and media ones.
  - A page class covers Yoast `page-sitemap*`, WordPress core `wp-sitemap-posts-page-*` and Shopify `sitemap_pages_*`. Skip it whenever a post, news, blog or article sitemap exists. When it is the only sitemap, keep it as a fallback that `url_ok` and `score_page` still filter. Page sitemaps are the main source of About and Contact junk, because static pages are often edited recently and sort high.
- **`score_page(..., owner_configured)` → `(score, reasons)`.** A page is accepted at a score of 2 or more.
  - Plus:
    - +3 for `og:type` article, or a JSON-LD Article, BlogPosting or NewsArticle. There is no +3 for `og:type=article` when JSON-LD says WebPage, AboutPage or ContactPage, because Yoast marks pages `og:type=article`.
    - +1 for a date.
    - +1 for 600+ characters.
  - Minus:
    - −3 for `og:type=website` or JSON-LD WebSite, AboutPage or ContactPage, only when the page has no JSON-LD Article, BlogPosting or NewsArticle and no date. Many CMSs tag every page `og:type=website`.
    - −2 under 300 characters.
    - −3 for a title equal to the site name or "404".
    - −3 for a redirect to another path.
    - −2 for a foreign canonical.
    - −1 for no date.
  - **When the owner set `link_pattern` or `sitemap_url`** (`owner_configured`): skip kind and date scoring, and keep only the hard rejects: a soft-404 or site-name title, a redirect to another path, a foreign canonical, and under 300 characters. The owner's pattern is the article signal. The plugin's own design case is an ollama-style `/library/<model>` page under a link pattern (models.py:60-71, fixtures.cjs:88), which declares no article type.
- **`listing_links(html, page_url)`** (used by PR10b only when there is no link pattern):
  - honours `<base>`
  - keeps same-site links only
  - drops nav, header, footer and aside
  - takes the largest repeating group of 3 or more
  - reads `<time datetime>`

#### Tests
**`test_article_detect.py`** uses fixtures in `backend/tests/fixtures/scrape/`:
- WordPress, Yoast, Shopify and News sitemaps, including a WordPress core and a Yoast page sitemap;
- a listing page and an about page;
- a home-page redirect and a soft 404;
- a dated `og:type=website` post (accepted);
- an undated ollama-style `/library/` page under a link pattern (accepted);
- an About page (rejected), and a Yoast About page with `og:type=article` and JSON-LD AboutPage (rejected);
- a recently modified About page in a page sitemap (excluded when a post sitemap exists);
- `url_ok` false negatives that must pass: cat, user and search slugs (`/pets/fat-cat/how-to-feed`, `/news/power-user/tips`, `/search/best-laptops-2026`), and a loopback host equal to the source's own host;
- the owner's junk sites, saved offline, once the owner sends the URLs.

#### Risks
- None at runtime: nothing calls the library yet.
- Revert is safe.

**Run with:** effort high · plan first: no · review: /code-review medium.

### PR10b: Website crawl wiring and errors (backend track)

#### Backend
**`feed_autodiscovery.py`:**
- `_parse_sitemap_entries` (284) returns `(url, date, title)`.
- `_crawl_sitemaps` (363) orders and skips sitemaps by class and returns those entries. `discover_sitemap_urls` (199) keeps returning `list[str]` for `/auto` (routes/sources.py:758). Project entries to `(url, lastmod)` before `_newest_first` (347).
- Headers are threaded through `discover_sitemap_urls` (199) and `_crawl_sitemaps` (363) here, and through `_section_candidates` (generic_scrape.py:103) and `_discover_page_links` (generic_scrape.py:332). `discover_feed_url`'s `headers=` moved to PR11a.

**Website errors (moved from PR9; the fix for the review's blocker):**
- **Blocked:** `fetch_html_checked` (article_extract.py) raises a typed error only on 401, 402, 403 and 429.
  - It is `httpx.HTTPStatusError`-compatible, so `_classify_error` (scheduler.py:232-234) yields `[blocked]` for 401-403. A 429 classifies as `[http error]`, whose fix is Check now.
- **404, 410 and other 4xx** keep today's behaviour: treat the document as empty and try the next one (feed_autodiscovery.py:389-394).
  - That covers robots.txt (:375), the `_GUESSED_SITEMAP_PATHS` (:246) and nested sitemap entries. Today a robots.txt or `/sitemap.xml` 404 is routine and the crawl moves on to `/wp-sitemap.xml`. Raising on it would stop website feeds that work today.
  - A 404 counts as not found only on the user's own listing URL or an explicit `sitemap_url`.
- **robots.txt 403** counts as blocked only when every sitemap fetch is also refused.
- **Raising through discovery:**
  - `discover_sitemap_urls` and `_section_candidates` gain an opt-in `raise_errors=False`. Only the plugin's fetch sets it, and there they re-raise the blocked error.
  - Today the catch-all at feed_autodiscovery.py:224-235 turns every exception into `[]`, so a real 403 would surface as `[no list]` and Read as Safari would never show.
  - `asyncio.TimeoutError` maps to `[network]`, not `[no list]`.
  - `probe()` and `/auto` keep returning `[]`.
- **Zero yield:** raise `NoArticleListError` → `[no list]` only when raw discovery is empty.
  - Raw discovery means the sitemap, page links and listing results after `url_ok` and `link_pattern`, before the `_extracted_urls` and stored-URL skips, and before the caps.
  - When every candidate is already known, that is a zero-yield success.
- **Articles refused:** if every article fetch in a non-empty batch got 401 or 403, raise blocked. A site often serves its cached sitemap but challenges article pages. Don't `_mark_extracted` a URL whose fetch failed, so a later poll (for example after Read as Safari) retries it.
- Keep `fetch_html`'s header merge in its own body, so the pins at `test_b1_b8_coverage_gaps.py:302-319` hold.

**`generic_scrape.py`:**
- One `plan_candidates(url, sitemap_url, link_pattern, headers, known_urls) → Plan` serves both `fetch` and `probe`.
- A non-root URL is read page-first. A root URL is read sitemap-first. An explicit `sitemap_url` always reads the sitemap first.
- `link_pattern` filters every candidate.
- Remove the silent whole-sitemap fallback in `_section_candidates` (103-112).
- **Skip stored URLs before extraction.** `_ingest` injects a `known_urls(urls) -> set[str]` callable built on its known-URL query (scheduler.py:411-416), so the plugins stay DB-free (rss.py:348-350).
- **Undated pages:** at most `ingest.max_undated_per_poll` per poll (default 2, PR4a's knob, D12).
- `_extract_one` (123) uses `score_page(..., owner_configured=bool(link_pattern or sitemap_url))`, inside the same `asyncio.to_thread` call that PR1a added. Add a test that a feed with a link pattern accepts an undated page that declares no article type. `fetch_html` gains `return_final_url` (article_extract.py:60).
- `_extract_links_from_html` (268):
  - With a link pattern it keeps today's behaviour: prefix filter, same origin, junk schemes dropped, the page itself excluded, dedup, and no grouping of 3 or more.
  - `listing_links` runs only when there is no pattern.
  - So the `test_extract_links_*` tests still pass. Most of them have only 1 or 2 matching links.
- Keep `custom_headers` explicit in `fetch()` (generic_scrape.py:391, 470-475).

#### Frontend
None. PR5a already shows no time for undated stories.

#### Tests
- **New:**
  - `test_generic_scrape_skips_stored.py`: stored URLs are skipped before extraction; a poll where everything is already stored returns `[]` and doesn't raise.
  - The website error cases:
    - robots.txt 404, `/sitemap.xml` 404 and a working `/wp-sitemap.xml` still yield candidates;
    - robots.txt 403 with a listed sitemap that works yields candidates;
    - all 404s with no link pattern give `[no list]`;
    - a listing page 403 gives `[blocked]`;
    - a sitemap timeout gives `[network]`;
    - sitemap 200 with article pages 403 gives `[blocked]`, and those URLs are not marked extracted.
- **Rewrite:**
  - `test_plugin_sitemap_wins_over_page_links` (test_generic_scrape_page_links.py:151).
  - `test_generic_scrape_sitemap_url.py`:
    - :94-170, three tests: expect `NoArticleListError` with `pytest.raises`, and keep their checks on the discovery call's arguments;
    - :176-216, the dedup test: the second poll still returns `[]` without an error;
    - :249-270, the probe test.
  - `test_generic_scrape_page_links.py`:
    - :88-103, `test_plugin_no_page_links_when_link_pattern_none`. `probe` still returns `[]` (`test_generic_scrape.py:58-63`).
    - :114-145, `test_plugin_page_links_runs_when_sitemap_empty_and_pattern_set`: its URL is non-root, so it is now read page-first.
    - :432-446, `test_page_links_fetch_html_safety_check`: expects `NoArticleListError`.
  - `test_generic_scrape.py`: the discovery fakes at :59, :70, :96, :128, :161 and :194 get `**kwargs`, because `headers=` and `raise_errors=` are now passed.
  - `test_sitemap_newest_first.py`:
    - `_serve` (:34) patches the new fetch name;
    - :54-68, `_newest_first` takes the projected entries;
    - :71-88;
    - :100-109 now asserts there is no fallback.
  - `test_generic_scrape_probe_budget.py:21, 40` monkeypatch `_section_candidates`. Patch `plan_candidates` instead, or keep `_section_candidates` as a thin shim.

#### Risks
- Probe the junk sites read-only from the VPS before merging.
- Before merge, the owner runs the read-only VPS probe against the existing working website feeds and compares item counts with today's.
- Some feeds will pull fewer stories or show `[no list]`.
  - `[no list]` is a setup state (D27): it never counts toward `error_count` and never auto-disables.
  - Its fix (Pick a section) also sends `active: true`, so a feed stopped before this change comes back in one step.
- Until PR10b, website feeds still drain undated sitemap pages (PR4a's Risks).
- Revert is safe until PR11a merges.

**Run with:** effort xhigh · plan first: yes · review: review agents: crawl errors against today's behaviour; scoring false negatives; broken-test inventory; owner's read-only VPS probe.

---
## PR11: One add flow with a preview

**What you'll notice:**
- ≡ → Add feed gives one field. The preview lists the stories, with one line such as "10 stories · 87 older skipped".
- The name is prefilled. The category defaults to the current tab when that tab is a category.
- After Add you see "Checking…", then "10 stories in News" with 3 titles and "Open News". If the first check hasn't finished after 120 s, it says "Still checking" and links to Feeds.
- A blocked site shows "Reading as Safari · Undo".
- A web-page feed gets "Find its feed". It previews the feed it found and switches only after you tap "Use this feed".
- Adding a feed you already follow says so instead of making a second copy.

**Complaints:** "it didn't tell me if it is actually pulling", "Nothing is quick".

**Units:**
- **PR11a: Preview endpoint, browser headers, PATCH type** (backend track). Depends on PR4a (`select_items_for_ingest`), PR9a (`NotAFeedError`, `last_attempt_at`) and PR10b (`plan_candidates`). Inert: nothing calls it until PR11b. Merges under the standing OK (D32).
- **PR11b: Add flow and 409** (frontend track). Depends on PR11a, PR7a (CategoryPicker in tab order), PR8a (`view:<id>` tabs, for the category rule) and PR9b (FeedStatus and the source-poll helper). Merges under the standing OK.
- **PR11c: Find its feed** (frontend track). Depends on PR11b, PR10b and PR9b. Merges under the standing OK.

### PR11a: Preview endpoint, browser headers, PATCH type (backend track)

#### Backend (`routes/sources.py`, `sources/rss.py`)
**`BROWSER_HEADERS`** in `sources/rss.py`: the Safari User-Agent from `headers.ts` (`headers.ts:44`), plus `Accept-Language`.
- It is pinned in `test_frontend_feed_editor_rules.py`, which already reads `headers.ts`. This is the one backend-track edit to a test that pins frontend source. PR4d adds a pin to the same file and PR9b renames the `headers.ts:46` preset; PR11a rebases onto whichever has merged, and its pin reads only the User-Agent at `headers.ts:44`.

**`POST /api/sources/preview`** (`_write_deps`):
- **One deadline.** The whole preview runs against one deadline, `PREVIEW_BUDGET_S = 60`, a module constant that tests monkeypatch. Each step gets `min(its cap, time remaining)`.
- **Semaphore.** `asyncio.Semaphore(2)`, also a module constant.
  - The slot is held until the pipeline task finishes, not until the timeout fires: run the pipeline in a task that holds the slot and wait on it with `await asyncio.wait_for(asyncio.shield(task), remaining)`. On a timeout the response returns, and the task finishes in the background, releases the slot, and can still fill the cache.
  - No queueing: when both slots are taken, return `error_kind: "busy"` at once. So no time is spent waiting for a slot.
- **Cache.** 120 s, at most 32 entries (module constants). It holds only the upstream fetch and parse result, keyed on the URL, `type`, `sitemap_url`, `link_pattern` and a hash of the lower-cased, sorted headers.
  - `duplicate_of`, `already_stored_count`, `will_add_count` and `older_skipped_count` are computed fresh on every request, because an Add, a delete or a first ingest inside 120 s changes them.
  - `headers_used` is an enum, `'default' | 'browser' | 'custom'`. The response never returns header values.
- **Threads.** Parsing runs with `to_thread`. Document in `ARCHITECTURE.md`: the semaphore, the cache, and that thread work keeps running after a timeout (the slot stays held until it ends).
- It uses SSRF-guarded fetchers only.

**Pipeline:**
1. YouTube and Apple resolution.
2. Reddit. Check `reddit_client.is_disabled()` (`reddit_client.py:268`) first and return `error_kind: "reddit_off"` with PR9b's one fix. `fetch_subreddit` never raises and returns `[]` while disabled, so without this check "Reddit off" looks like an empty subreddit.
3. Fetch as a feed: a single attempt with a 10-12 s read, not `fetch_rss`'s 60 s read inside a 2-attempt loop (`rss.py:330-337, 378`).
   - On 401/403 or a challenge page, retry once with `BROWSER_HEADERS`, but only if the caller sent no User-Agent.
   - On a timeout, go on to discovery instead of failing the request.
4. `discover_feed_url`. It reuses step 3's result, so the page is not fetched as a feed twice. `discover_feed_url` (`feed_autodiscovery.py:79`) gains `headers=None` (moved here from PR9).
5. `plan_candidates`, then up to 8 extractions.

**Out of time:** return what is known so far (the kind, items, or "Find its feed" candidates) with `error_kind: "timeout"`, not a bare error.

**Response:**
- `ok`, `kind`, `url`, `headers_used`, `mode`, `feed_title`
- `items[{title, url, published_at, accepted, reason}]`
- `total_count`, `will_add_count`, `older_skipped_count` (from `select_items_for_ingest(first_fetch=True)`)
  - In scrape mode, `total_count` is the accepted candidates, `will_add_count = min(accepted, 10, dated + 2)` (the plugin adds at most 10 pages per poll, `generic_scrape.py:42`, and at most 2 undated ones, PR10b), and `older_skipped_count` comes from candidate dates.
- `kind` is `'podcast'` when the feed has audio enclosures or the itunes namespace, so POST's per-type refresh default (`sources.py:567-579`) and the podcast category apply.
- `already_stored_count`, `duplicate_of`, `error_kind`, `error`
- `error` is one short sentence chosen by `error_kind`. It never carries response-body text, exception reprs or resolved IPs.
- `/sources/test` keeps `_test_source_impl` (974) unchanged.

**PATCH `type`** (moved here from PR9: PR11a defines it, and PR11c is its only caller):
- Dynamic feeds only. The old and the new type must both be in `{rss, podcast, generic_scrape}`.
- Built-ins reject a type change (the registered-name early return, `scheduler.py:2440-2442`).
- The URL is validated under the new type.
- `type_changed` joins both guards in `update_source`: the nothing-changed return (`scheduler.py:2354-2364`) and the category-only return (2449-2458). A type change resets the error fields, nulls `last_fetch_at`, and rebuilds the job (`_add_or_replace_dynamic_job`, 2477).
- The `_ingest` in-flight guard (PR4a) also discards the batch and skips the bookkeeping when the source's `type` no longer equals the snapshotted type.

#### Tests
- **pytest `test_sources_preview.py`:**
  - The harness is `fake_dns` (`tests/conftest.py:226`) with an httpx `MockTransport`, or the `mock_news_site` monkeypatch of the module-level `check_url_safe` / `ssrf_event_hook` bindings (`test_feed_autodiscovery.py:142`). A loopback server works only with those guard bindings patched, because `check_url_safe` refuses 127.0.0.0/8 (`url_safety.py:95`). No production toggle.
  - A 403 answered in both modes, then the browser retry.
  - The deadline: with the budget patched to about 0.2 s, a hanging step 3 still runs discovery; running out returns `timeout` with what is known.
  - The semaphore: a parser stub that sleeps past the budget keeps its slot, and a third preview gets `busy` until the stub ends.
  - The cache: changed options or headers force a refetch; a preview without headers never gets a result fetched with headers; `duplicate_of` and the counts are fresh after an Add.
  - The counts in feed and scrape mode, and `kind='podcast'`.
  - `reddit_off`.
  - The SSRF guard: a private host returns the generic `/sources/test` message, with no reason or IP text.
- **pytest `test_source_patch_type.py`** (moved here from PR9): allowed and refused type pairs, a built-in refused, the URL checked under the new type, the job rebuilt, `last_fetch_at` and the error fields reset, and a type change during an in-flight poll inserts nothing.
- **The `BROWSER_HEADERS` pin** in `test_frontend_feed_editor_rules.py`.

#### Risks
- A website preview can take up to 60 s. The semaphore bounds concurrent preview work, including parses that run past the budget.
- Revert is safe while nothing calls the endpoint. Once PR11b merges, fix forward or revert newest-first.

**Run with:** effort high · plan first: no · review: /security-review + /code-review high.

### PR11b: Add flow and 409 (frontend track)

#### Backend (`routes/sources.py`): duplicates
The YouTube check at `sources.py:553-561` becomes one URL lookup for every type:
- The same URL under the same name returns the existing row. `scheduler.add_source` already returns the existing row for a repeated name (`scheduler.py:2210-2230`), so a double-tapped Add stays safe.
- The same URL under another name returns 409, "You already follow this feed as \<label\>." YouTube keeps "channel" (`test_youtube.py:546-554`).
- Matching is exact on the resolved, normalized URL: lower-case host, no trailing `/`.
- PATCH `url` (`update_source_endpoint`, 779) runs the same check.
- Only `POST /sources` (501) and PATCH return 409. `/auto` (671) keeps returning 200 with the existing feed. Today only its YouTube branch looks one up (718-722); its rss and scrape branches (737-769) gain the same lookup instead of creating a `<host>_2` copy.
- `/feed-recommendations` (1239) drops candidates whose URL is already a `Source.url`. Today it filters by name only (1253-1255), so a suggestion followed under another name would now fail with 409.
- The header denylist (180) still applies.
- The 409 ships in the same PR as the `ALREADY_FOLLOW_RE` change below, so the backend text and the frontend regex never disagree.

#### Frontend
**`feeds/AddFeed.tsx`** replaces `AutoFeedBlock` (FeedManager.tsx:552-720, with its types and helpers to 755) and `AddCustomTab` (757-1331):
- One 17px URL field and a Preview button. The field is focused inside the tap handler that opens Add, so the keyboard and the Paste callout appear with that tap.
- `FeedPreview`: up to 5 titles with dates, one count line, the name, `CategoryPicker`, and Add. Preview titles render as plain text.
- An Options disclosure: type, sitemap, link pattern and headers.
- The draft is kept in a module store.
- **Default category:** on `cat:<name>`, that category. On meta tabs, Brief and `view:<id>`, the type default, as today: `resolveCategory('video')` for YouTube, `resolveCategory('podcast')` for podcasts, otherwise `resolveCategory('other')`, as auto-add does (FeedManager.tsx:629-630, 836-839).
- **Add result:** after Add it polls `api.source(id)` every 5 s. It stops once `last_attempt_at` is later than the row's `created_at`, then fetches the 3 titles with `api.entries({source: name, limit: 3})`. After 120 s it shows "Still checking · Feeds", which opens Needs attention. It shares one helper with PR9b's Check now line.
- PR7a's `FirstFeedButton`, used by PR6c's C7 row and the meta-tab empty state, now opens Add (`openSettings('feeds', {sub: 'add'})`).

**Plumbing:**
- `openSettings(tab, opts?: {sourceId?, sub?, category?})` replaces `(tab, sourceId?)` (App.tsx:526), and every call site is updated.
- `openSettings` writes `?sub=add|history|hidden` next to `source`, or deletes it when unset (App.tsx:526-539). The App sync reads it and validates it against that list (499-519). `closeSettings` (544-556) and the Settings tab strip (Settings.tsx:317-319) delete it along with `source`.
- Settings passes `sub` down with the existing `settingsFocusNonce` (App.tsx:494); no new nonce. FeedManager takes `initialTab` and applies it on mount and when the nonce changes; its focus effect (FeedManager.tsx:102-109) keeps forcing `'mine'` only when `focusSourceId` is set. Settings unmounts while closed (`if (!open) return null`, Settings.tsx:243), so this is the only way to open on Add.
- `category` doesn't go in the URL. `openSettings` seeds the AddFeed draft store with it, and an existing draft is not overwritten.
- `ALREADY_FOLLOW_RE` (apiErrors.ts:67) matches "channel" or "feed", and the rewrite at apiErrors.ts:101 keeps the matched word instead of a hard-coded "channel".
- Add `api.previewSource`.
- **The Drawer gets one "Add feed" row** (`openSettings('feeds', {sub: 'add'})`). Today the Menu has only Settings, Save current view, Jump and Sources (Drawer.tsx:327-495), and PR8b removes the last two. PR12a restyles the row.

#### Tests
- **pytest:**
  - `test_source_duplicate_409.py`: same URL and name returns the row; same URL under another name is 409 with the "feed" wording; YouTube keeps "channel"; PATCH url is checked; the normalization rule; `/auto` returns the existing feed on its rss and scrape branches; recommendations drop followed URLs.
  - `test_source_editor_contract.py::test_name_collision_is_409` (193) gets two URLs. `test_forty_characters_with_spaces_is_stored_stripped` (131) gives its second `_create` a distinct `url=`; with a URL 409 it would fail today, because `_create` always posts the same feed URL (26-35).
  - Rewrite `test_frontend_slice15_add_generic_scrape.py` and `test_source_create_sitemap_link_pattern.py`.
- **Unit:** `apiErrors.test.ts` gets a "feed" row next to the "channel" pin (:39). A new `settingsTabs.test.ts` (in `frontend/scripts/unit/`) with the `sub` cases.
- **Flows:**
  - Rewrite FJ (`flows.cjs:216-237`), FJ2 (239-256), FP (258-273), FQ (275-308), FR (310-332), FS (413-424) and FG2 (866-886). The `openAddTab` helper (16-20) becomes an Add-sheet opener.
  - New: `flow-add-preview` (at most 5 taps; it counts the paste step instead of relying on `fill()`), `flow-add-browser-fallback`, `flow-add-duplicate`, `flow-add-from-meta-tab` (asserts the type-default category from For You), `flow-add-result` (titles, and "Still checking" at 120 s with `page.clock`).
- **Smoke:**
  - Remove `settings-add-*` (smoke.cjs:200-270).
  - Add `add-empty`, `add-preview` (390/320), `add-result` and `add-error`.
  - `capture.cjs` mocks the preview.

#### Risks
- A bundle from before PR11b doesn't match the new "feed" 409 wording, so it shows the server's sentence as it is. That is acceptable until it reloads (PR1r).
- Revert restores the old forms.

**Run with:** effort high · plan first: yes · review: /code-review xhigh; tap-count flows.

### PR11c: Find its feed (frontend track)

#### Frontend
- "Find its feed" shows on `generic_scrape` rows and on `not_a_feed` rows (PR9b's table).
- It previews the URL first and shows the preview's count line and titles. `discover_feed_url` returns the first alternate link, which is often the whole-site feed rather than the section you tracked, so nothing changes until you tap "Use this feed".
- "Use this feed" PATCHes `{type, url}` (PR11a), clears `sitemap_url` and `link_pattern`, and runs PR11b's duplicate check.
- The target count becomes 4 taps: ≡, Feeds, Find its feed, Use this feed.

#### Tests
- **Flows:** `flow-find-its-feed`: the preview shows first, nothing is PATCHed before "Use this feed", and the PATCH clears `sitemap_url` and `link_pattern`.

#### Risks
- Revert is safe; the row's other fixes stay.

**Run with:** effort high · plan first: no · review: /code-review medium.

---

## PR12: Menu of tasks, 4 Settings tabs

**What you'll notice:**
- The Menu lists Add feed, Feeds (2 need attention), Saved, Edit tabs, Activity and Settings.
- Settings has 4 tabs: Feeds / AI & Brief / Activity / General.
- Activity opens on Hidden while any story is hidden, so restoring one is 3 taps.
- Saved's ⋯ has "Clear all saves", with Undo.

**Complaints:** "This app is clunky".

**Units:**
- **PR12a: Menu of tasks and 4 Settings tabs** (frontend track). Depends on PR5a (C5), PR7b (the tab ⋯ sheet), PR8b (the Drawer removals), PR9b (the needs-attention count) and PR11b (the Add feed row). Merges under the standing OK.
- **PR12b: Copy sweep** (frontend track). Depends on PR12a. Ships alone so it can be reverted without touching routing. Merges under the standing OK.

### PR12a: Menu of tasks and 4 Settings tabs (frontend track)

#### Changes
**`Drawer.tsx`** (327-495) becomes task rows built from `Grouped.tsx`:
- Add feed (PR11b's row, restyled), Feeds (N need attention), Saved, Edit tabs, Activity and Settings.
- Saved selects `meta:saved`. If Saved is hidden (PR7a), it shows as a temporary tab until you switch away.
- Activity opens `activity/hidden` while any story is hidden, and `activity/history` otherwise.
- Jump to column shows only on md and up.

**`lib/settingsTabs.ts` (extend):**
- `SETTINGS_TABS` shrinks from 8 ids to 4: `['feeds', 'llm', 'activity', 'general']`.
- `isSettingsTab` (used at App.tsx:508) reads the new list.
- `normalizeSettingsTab(id)` returns `{tab, sub?}` and maps the old ids:
  - `notifications` and `reset` → general
  - `history` and `voted` → activity/history (C5)
  - `hidden` → activity/hidden
  - `starred` → closes Settings and selects Saved: `replaceState` drops `view`, `tab`, `source` and `sub`, and a pending selection of `meta:saved` is applied through `selectTab` once columns load. The sync effect runs on mount, before columns exist, and never rewrites the URL today (App.tsx:499-519).
- The `?tab=` sync (App.tsx:499-519) uses it and applies both `tab` and `sub`.
- The `?tab=` / `?sub=` sync acts only when `view=settings`; PR8a's `?view=feed` is left alone.

**`Settings.tsx`:**
- **AI & Brief:** the LLM section plus a schedule line. `GET /api/llm/status` (`routes/brief.py:333`, `LLMStatus` at `schemas.py:523`) gains `brief_schedule_hour`, null when it is -1. It is read from the existing setting (`config.py:336`, read only; `config.py` is not edited). The line renders it in local time from the UTC hour.
- **Activity:** a History | Hidden segmented control (`HistoryTabContent` 1118, `EntryIdListContent` 1539). It takes `initialTab` from `sub`, like FeedManager.
  - History also lists opened stories. It asks for them with a second request, `?types=view`, because PR1a's default leaves views out, and shows them as their own group. This replaces the Read group that PR1c dropped.
- **General:** Notifications, Reset and PR1r's build line. The `confirmReset` effect (Settings.tsx:176-178) is re-keyed from `'reset'` to `'general'`.

**Removals (C5 and Settings → Saved):**
- Settings props `starredEntries`, `onUnstarAll`, `onUnstarEntry`, `downvotedEntries`, `onHideDownvoted` and `onHideAllDownvoted` (Settings.tsx:93-109).
- `StarredTabContent` (1704) and `DownvotedTabContent` (1789).
- The removed tabs' `TAB_META` entries (111-120) and `TabIcon` cases (541-610).
- App's `downvotedEntries` memo (App.tsx:927-939) and the wiring at 3202-3250, including `onHideAllDownvoted` (3231-3250).
- "Clear all saved" with Undo (App.tsx:3203-3219) is not dropped: it becomes a "Clear all saves" row in Saved's ⋯ sheet (PR7b), with the same Undo.

**Shared table:** no step changes the shared `sources` table.

#### Tests
- **Unit:** `settingsTabs.test.ts` (extend, from PR11b): the 4 ids, every old id's mapping including `sub`, and `isSettingsTab`.
- **pytest:** `/api/llm/status` returns `brief_schedule_hour`, and null when it is -1.
- **Flows:**
  - Update FG (`flow-fg-settings-back`, `flows.cjs:334`), FT (`flow-ft-menu-edit-feed`, 357) and `flow-menu-closed-inert` (398).
  - FU (`flow-fu-jump-to-column`, 381) moves to `widths: [1280]` here, because Jump now shows only on md and up (it stays at 390 through PR8).
  - New: `flow-menu-rows` (each row reaches its target in 2 taps), `flow-settings-old-ids` (every old id, including `starred` landing on Saved and a hidden Saved), `flow-restore-hidden` (3 taps: ≡, Activity, Restore, asserting Activity opened on Hidden), `flow-clear-all-saves` (the ⋯ row and its Undo), and `flow-activity-history-views` (an opened story appears in Activity → History).
- **Smoke:**
  - Remove `settingsState('notifications' | 'history' | 'hidden' | 'starred' | 'voted' | 'reset')` (367-372).
  - Add `menu`, one state per new tab, and `settings-old-id-hidden`.

#### Risks
- Old URLs are mapped, so links and stale bundles' `?tab=` ids still land somewhere sensible.
- Revert is safe.

**Run with:** effort high · plan first: no · review: /code-review high.

### PR12b: Copy sweep (frontend track)

#### Changes
- A hand sweep to stories, feeds and tabs, including the desktop health line (App.tsx:2679, "entries · sources") and the For You row count (2855, "entry / entries").
- "Category" stays as the feed editor's field word. It names the feed's home tab, and a feed can also sit in custom tabs, so "tab" there would mislead. So "New category" and the CategoryPicker wording stay.
- Pinned copy that changes is updated in the same commit: `apiErrors.test.ts`, `categories.test.ts`, the FA2 (`flows.cjs:443`), FA3 (464) and FQ (275) flows, and the smoke `newcat` state (`smoke.cjs:290-296`), wherever their strings change.
- There is no guard test.

#### Tests
- The full strict smoke and every flow pass.

#### Risks
- The copy change is broad. It ships alone, so it reverts alone.

**Run with:** effort medium · plan first: no · review: /code-review low; full smoke and every flow.

---

## PR13: Deferred (each needs a fresh owner yes, D19)

- Category rename/merge.
- A per-feed volume cap UI (with its own nullable migration).
- Retention/pruning: off by default, dry run first. `interactions.entry_id` is ON DELETE CASCADE (`models.py:243-245`).
- JSON Feed and guessed feed paths.
- Framing Watch on the phone.
- Drag-to-reorder.
- Automatic Safari fallback on scheduled fetches (needs `fetch_mode` in its own migration).
- Restore from Activity removing the ranking signal (`DELETE /api/interactions/latest`). Undo on Hide already cancels it, with no route (D25, PR5a).

**Optional hardening (each needs a yes):**
- A streamed body cap of about 5 MB in `fetch_rss` (PR9a).
- Send custom headers to discovery candidate probes only when the candidate is on the same site.
- Raise the starred cap to 5000, or answer "Saved is full" when an add would pass it.
- Watch the automatic rebuild after a pull's checks land (PR6b), after deploy.
- A stretched-link overlay so a tap on the card body opens the story (PR5a).
- (The one-feed view from a card's feed name was adopted as D26, in PR8a.)

---

## Dropped and why

**First-run help and explanations**
1. **The tip card** goes. Social-media conventions (the headline opens, ▲▼, bookmark, swipe, ⋯) need no tutorial (D15).
2. **"How Popping works" and its Menu row** go, for the same reason. The desktop `?` sheet stays and only gains keys. It does not get a focus trap or a touch section.
3. **Meta-tab "nothing to queue" explanations** go. Pulling Saved simply re-reads.

**Old reading-model controls**
4. **"Show older stories" and the "Older" divider** go. A row past the end would undo the end. ⋯ → Show stories from does the same job in 3 taps (D5). D24's "N not seen · Mark as read" row is not a way back to older stories; it only finishes the current ones.
5. **`per_source_floor` (client and backend)** is never built.
   - The window counts arrival time, so a slow feed's new story shows when it arrives. Health pills and the add result prove a feed works.
   - Dropping it also removes a 90-day per-source `row_number` and its 422 rules.
   - `per_source_cap` stays, so one busy feed can't fill a tab. It keeps the 25 best-scoring stories in the window (D28).
6. **✓ Read, swipe-right, `m`, the context-menu "Mark as read", and the "Marked as read/unread" toasts** go. Opening a story or scrolling past it uses it up (D3).
7. **The eye button, "Not interested", "More like this" and the two-signal swipe** go. ▲/▼ and Hide cover them, with Reddit's meanings (D1, D2).
8. **Header-tap mark-read, the "+N new" chip, the seen-set reset and `last_viewed` writes** go (D4).
9. **The "Unread" / "Read (N)" labels and persisted section collapse** go. Earlier folds again on every snapshot, so the end stays the end. `column_sections` rows are left for Reset to clear.
10. **The "Show 30 more" button** goes, because batches append automatically (D16).
11. **The unread accent ring** goes. The marker separates new from seen.

**Toasts**
12. **Chatty toasts go:** vote thank-yous, save toasts, "refreshed", "Entry unhidden." and "Column restored.". The filled icon or the status row is the feedback, and Undo toasts remain. Saving stays silent, but unsaving keeps one "Removed from Saved · Undo" toast, so an accidental unsave can be undone (PR5a).

**Signals and controls**
13. **`dwell` events** go.
    - They came only from ✓ and swipe-right, and time on screen while scrolling measures scroll speed, not reading.
    - The ranker's dwell weight goes idle once every device runs the PR3 build, and `config.py` is untouched.
    - The old dwell-unit fix is moot.
14. **The phone header's Refresh button and 34px large title** go. Pull replaces Refresh (D10), and C4 keeps a path that isn't a gesture. The slimmer title gives the space back to stories.
15. **The add flow's strategy line, "N show now / M under Show older", and `in_window_count`** go. The preview is titles plus one count line (D13).
16. **Settings → Downvoted and "Hide all downvoted"** go (C5). Votes now mean Reddit votes, and they show in Activity → History. (Settings → Saved goes too, but its "Clear all saves" moves to Saved's ⋯, PR12a.)
17. **"Date unknown"** goes. Undated stories show no time.
18. **Move earlier, Move later and Hide this tab in the tab sheet** go. Edit tabs does each in 3 taps, and fewer rows keep the sheet scannable.
19. **Separate "Found nothing" and "Nothing new" pills, and Delete as a quiet feed's fix,** go. Quiet covers all three states, Check now is the one safe fix, and Delete stays in the editor.
20. **Desktop scroll tracking** goes (C2). Desktop columns scroll together, so scrolling past one doesn't mean you read it.

**Tests**
21. **`test_frontend_vocabulary.py`** is not nearly free (it needs JSX scanning plus an allowlist). Copy is fixed by hand in the PRs that touch it (PR12b for the sweep).

## Review notes

**Facts re-checked against main:**
1. **Anchors:** every anchor was re-checked on 4be8766. #25's `frontend/` and `backend/` trees match its branch exactly. `brief.py:386/451` are the `fetched_at >= since` filters. The review of 2026-10-19 checked them again and corrected the ones it found wrong.
2. **Phone scroller:** it is the list at `Column.tsx:493`, which has no `data-scroller` today. PR1b adds it. The `App.tsx:3077` wrapper only scrolls Brief.
3. **Stale lists:** App mirrors `readEntries`, `hiddenEntries` and `starredEntries` into refs inside effects (App.tsx:765-781). `setReadEntries` replaces the whole list (preferences.tsx:592-606). So two calls in one tick lose an id, and a diff would PATCH a removal. PR2b's `addIds` / `removeIds` fix this.
4. **Desktop layout:** the grid `<main>` (2937) is a flex-1 scroll container inside an `h-full flex flex-col` root (2558). Its siblings (BriefCard, the For You row, FramingWatch) can't shrink, so a tall For You row collapses the grid. PR3a's single scroller fixes it.
5. **Phone `<main>`:** it is CSS-hidden at md and up but stays mounted (3000), while the desktop grid is gated by `isDesktop` (2936). The seen observer is gated the same way.
6. **Frozen clock:** smoke calls `context.clock.setFixedTime(F.NOW)` (`smoke.cjs:934`), and timers keep running.
7. **Status wait:** `smoke.cjs:990` waits up to 8 s for no `[role="status"]` before every state.
8. **Preference mock:** `capture.cjs:182-186` echoes `body.value` for every method.
9. **slice19:** six tests pin the empty state and the `<Column` call sites. The `<Column\b[^>]*>` regex (:138) stops at the first `>`, and :139 asserts at least 2 Column call sites.
10. **Widths:** `WIDTHS` has only 390, 320 and 312 (`smoke.cjs:27-33`), and flows default to `[390]` (`flows.cjs:89-95`).
11. **Built-ins:** every plugin gets a job (`scheduler.py:1799-1810`) and a row (110-135). An install can still be empty: built-ins can be deleted, rows exist only after the first fetch, and For You is windowed to 2 days from PR4. So the C7 row does not rely on For You having stories (PR6c), and PR7a's empty meta tab carries the same button.
12. **Brief:** BriefCard has no `onOpenSettings` prop and has four generate buttons. `Settings.tsx:405-412` sits on the AI tab.
13. **`IngestResult`** (schemas.py:497-502) ignores unknown keys. It does not return a 500.
14. **Existing libraries:** `lib/categories.ts` and `lib/settingsTabs.ts` exist (8 ids). Unit tests live in `frontend/scripts/unit/`. `flows.cjs` has no `tap` / `taps`. Card's constants are `SWIPE_DEAD_ZONE_PX` / `SWIPE_DIRECTION_RATIO`. `flows.cjs:742` is FL's PUT.
15. **Entries ranking:** the per-category `row_number` orders by `composite_score` (entries.py:404-409). `categoryLabel('on_this_day')` gives "On This Day".
16. **Tap targets:** 6 × 44 = 264px fits from a 310px viewport, which is 266px of bar at 312.
17. **View signal:** the deep-dive asked for the on-mount `view` change to ship separately, with owner sign-off. D3 is the sign-off, and PR3c is the separate unit.
18. **No reload:** nothing in `frontend/src` reloads a running bundle except sign-in, sign-out and Reset (App.tsx:1391, 2528, 2698). There is no service worker and there are no lazy chunks, and a home-screen app is resumed rather than relaunched, so an old bundle can run for days. PR1r and PR2c exist for this (D22, D23).
19. **Preference routes:** `GET`, `PUT` and `DELETE /preferences/{key}` (`routes/preferences.py:186, 219, 279`) use a plain `{key}`, which doesn't match an encoded `/`. PR2a moves them to `{key:path}`.
20. **Container start:** `backend/Dockerfile:48` runs `alembic upgrade head && uvicorn`, which is why PR9a's rollback keeps the migration file (see its Rollback).

**Corrections not taken as written:**
- **#4 ("3 new" as a button that scrolls up):** not taken. The status row only shows at the top, where the automatic rebuild already applies. Arrivals that land after you've scrolled away follow PR3b's rule.
- **#5 (either a single scroller or open-only on desktop):** both are taken. The single scroller fixes a layout bug that exists regardless, and open-only avoids marking columns you weren't reading.
- **#6 (iOS pull gesture):** taken, plus one rule: a move with `cancelable === false` ends the pull, because iOS has already given the touch to native scrolling.
- **#11 ("Add your first feed" in `EmptyColumnMessage` for For You):** taken in a wider form. An install can be empty: built-ins can be deleted, rows exist only after the first fetch, and For You is windowed to 2 days from PR4. So while you have no feed of your own, C7's 44px row sits above For You's list or above `EmptyColumnMessage` (PR6c), and PR7a's empty meta tab shows the same button.
- **#13, second option (qualify a card on about 600 ms of total visibility):** not taken. It needs a clock across observer callbacks, and a card flung past in under a second wasn't read. The ✓-text rule alone fixes the false end, and D24's "N not seen · Mark as read" row finishes a skimmed tab in one tap.
- **#27 (`?since=` on resync):** taken, with one change: a full GET still runs on resume, because a `since` query can't report deleted rows.

**Design choices to watch after deploy:**
- **`SEEN_DWELL_MS = 1000`** is one constant. Tune it after the owner tries it at Gate 1. Cards flicked past stay unseen; D24's end row marks them in one tap.
- **Read marks** go under `seen:<category>` (PATCH-only, cap 1000 per key). Reads are the union of `seen:*` and the legacy `read_entries:*` rows, and Reset and Mark all cover both. Old bundles still PUT whole lists to `read_entries:<column name>`, which is the same string as a per-category key, so new marks can't share that prefix. PR8 migrates neither, and pruning stays per category.
- **First deploy of the read model (D33):** each device treats stories fetched more than 48 hours before its first load as already seen, locally, writing nothing to the server. If the first open still shows too many new stories, tune the 48 hours.
- **C7 and C8** are the newest calls. Each is a single row and easy to remove.

### Review of 2026-10-19
A second review checked this plan against main 4be8766 before any code was written. It ran 8 section fact-checkers and 7 cross-cutting reviewers (sequencing, security, data integrity, product and UX, testing, backend operations, iOS and constraints), then had skeptics try to refute every finding. 267 raw findings became 209 after merging duplicates and setting 15 nits aside, and a completeness round added 24 more. 227 survived verification (1 blocker, 22 major, 177 minor, 27 nits), and 6 were refuted. Reviewers also confirmed 253 of the plan's claims as correct.

Every must and should change it produced has been applied to this document, and the owner's answers are D20-D33. The blocker and the major findings:
- **Blocker (PR9 → PR10b):** raising on every status ≥ 400 for robots and sitemap fetches would break website feeds that work today.
- PR9a/PR0: the rollback order would stop the backend from starting.
- PR1a: the misfire-grace fix left trafilatura blocking the event loop, and other jobs, including the daily brief, kept the 1 s grace.
- PR1c: `llmStatus` fetched once inside BriefCard went stale after "Set up", and the header Brief button couldn't see it.
- PR2b: retry and failure handling for PATCH deltas was unspecified, and today's PUT retry rule would drop deltas.
- PR4a: first-fetch detection by `last_fetch_at IS NULL` ended on any empty success, so a feed that first yields stories days later floods.
- PR4b: Saved through `/entries/by-ids` would render degraded cards (no image, audio, Reddit thread, cluster or cached summary).
- PR4d: a category with nothing in 2 days lost its tab, and with it the control that brings it back, until PR7.
- PR6a: pull-to-refresh bypassed failure backoff and could auto-disable feeds that were only failing for a while.
- PR6b: fixed +25 s / +70 s timers reported "Nothing new" while queued checks were still waiting.
- PR8a: no way to create a custom tab was specified.
- PR10b: blocked and timeout errors never reached `_ingest`, because `discover_sitemap_urls` swallows every exception.
- PR9b: "Read as Safari" would be offered where headers are ignored (built-ins and Reddit).
- PR10b: `score_page`'s −3 for `og:type=website` rejected real articles, including the documented `link_pattern` case.
- PR2a/PR3b: `read_entries:<category>` keys containing `/` could never be written, because `{key}` doesn't match an encoded slash.
- PR8a: the one-time preset conversion needed gating, a create-only write and a "done" marker.
- PR5b/PR8a: "Hide stories from \<feed\>" did nothing visible on a custom tab.
- PR4b: the flat `/entries` path (wider tabs, custom tabs, All) was under-specified.
- PR4a: the brief guard `published_at >= since` dropped genuinely fresh date-only stories.
- PR3c: cards registered only on mount, so after a rotation recreated the observer, mounted cards were never observed again.
- PR6b: Chromium's CDP touch skips the first 30-40px of movement, so the 3px, 12px and jitter rules had no automated coverage.
- Deploys: nothing ever reloads an old bundle, so every "until it reloads" risk lasted for days and device checks could test the old build (PR1r, PR2c).
- PR8a: the preset conversion resolved names before feeds loaded, and nothing said what happens to names it can't resolve.

### Critical Files for Implementation
- `frontend/src/lib/preferences.tsx`
- `frontend/src/App.tsx`
- `frontend/src/components/Column.tsx`
- `frontend/src/components/Card.tsx`
- `frontend/scripts/iphone-audit/smoke.cjs` (with `flows.cjs` and `capture.cjs`)
- `backend/app/routes/preferences.py`
- `backend/app/scheduler.py`
- `backend/app/routes/entries.py`
- `backend/app/feed_autodiscovery.py` and `backend/app/sources/generic_scrape.py`
