# UX overhaul progress

Status of every unit in `docs/UX_OVERHAUL_PLAN.md`. Each unit PR updates this file. Device checks live in `docs/DEVICE_CHECKS.md`.

| Unit | Track | Status | PR |
|---|---|---|---|
| H0 Plan revision, README fix, Device checks list | docs | merged | #26 |
| H1 Test harness groundwork and local loop | F | merged | #29 |
| PR1r Build stamp and reload on a new build | F | merged | #32 |
| PR1a Scheduler defaults, extraction off the loop, Reddit Test, History | B | merged | #27 |
| PR1b Tab keys, resolveTab, tap-to-top, phone scroller | F | merged | #36 |
| PR1c Newest / Best, Reset scope, bookmark on save, Brief AI row | F | merged | #38 |
| PR2a Preference PATCH routes | B | merged | #28 |
| PR2b prefSync client and sync rules | F | merged | #40 |
| PR2c Refuse whole-list PUTs of set keys | B (floating) | merged | #37 |
| PR3a Desktop single scroller and batching | F | merged | #43 |
| PR3b Read model | F | merged | #45 |
| PR3c Seen on scroll | F | merged | #46 |
| Gate 1 | owner | | |
| PR4a Ingest rules, brief guard, On this day | B | merged | #30 |
| PR4b Window parameters and by-ids projection | B | merged | #31 |
| PR4c Saved by id | F | merged | #47 |
| PR4d Window UI | F | merged | #49 |
| PR5a Reddit card, action sheet, Hide hold | F | merged | #48 |
| PR5b Muted feeds | F | not started | |
| PR6a queue_check and the check-now route | B | merged | #33 |
| PR6b Pull to refresh and checkTabNow | F | not started | |
| PR6c Slim phone header and the first-feed row | F | not started | |
| Gate 2 | owner | | |
| PR7a Tab order and hiding | F | merged | #42 |
| PR7b Tab ⋯ sheet and empty states | F | not started | |
| PR8a Custom tabs | F | not started | |
| PR8b Remove chips, presets and the old Sources UI | F | not started | |
| PR9a Migration 0031 and feed health backend | B | merged | #35 |
| PR9b Feed health UI | F | not started | |
| PR10a article_detect library | B | merged | #34 |
| PR10b Website crawl wiring and errors | B | merged | #39 |
| PR11a Preview endpoint, browser headers, PATCH type | B | not started | |
| PR11b Add flow and 409 | F | not started | |
| PR11c Find its feed | F | not started | |
| PR12a Menu of tasks and 4 Settings tabs | F | not started | |
| PR12b Copy sweep | F | not started | |

Work in progress is pushed to `claude/ux-<unit>` branches as it goes.

## Owner decisions after the plan (2026-10-20)

- Claude merges PR9a and PR10b once CI is green and the unit's review step has passed; the owner no longer holds those merges. PR10b's read-only probe of the live website feeds moves to `docs/DEVICE_CHECKS.md`, to run after deploying.
- The device gates don't hold frontend merges. Their checks stay in `docs/DEVICE_CHECKS.md` for the owner to run after deploying, and any `fail:` becomes a fix unit.
- PR2c still merges only after both devices run the PR2b build (`popping-update`, then relaunch each device), because merging it earlier makes old app copies fail to save.

## Owner decisions (2026-10-21)

- The owner tests on the phone only; no desktop is in use. Device checks are written phone-first, and desktop-only lines say "(If you have a desktop)".
- PR2c's condition is now that the phone shows the PR2b build in Settings. No browser tab is open on an old build; a browser opened later loads the current build.
  - Met on 2026-10-21: the phone shows the PR2b build `b6bd7e7f` (20 Oct 21:09). PR2c merged as #37.
- The process stays as before: every unit gets the full review, and units with major fixes or risky logic get re-checks. Each unit is built on top of the one before it, so the next can start while the last finishes review.
- PR10b merged after six review rounds. The owner's real-feed probe (baseline, then 3 hours and a day after deploying) is its final check. PR11a waits for that probe.
- D34: cards get preview text under the headline, and the Summary arrow stays for the full AI summary. PR5a builds it.
- **Stop point.** The owner chose to stop after Gate 2 and PR9b. The units still to build are PR3c, PR4c, PR4d, PR5a, PR5b, PR6b, PR6c and PR9b. PR11a (built, #41) merges once the website-feed probe passes. PR7b, PR8a, PR8b, PR11b, PR11c, PR12a and PR12b are not built unless the owner asks later; they depend on each other, so they would be done as one stretch.
- **Lighter process (2026-10-21).** Every unit keeps its full first review. After that, at most one re-check round, and only when a fix changed behaviour the owner would see; small test-only findings are fixed on the spot or noted. PR3b had three re-check rounds and PR3c one under the earlier rule; PR4c stops after its third.
- **Gate 1 passed (2026-10-22)** on build 8516098: every phone check, the auto-reload, and the dwell feel (no retune).
- **Wrap-up (2026-10-22).** The owner asked to finish the units in flight and stop: PR4d (#49) and PR5a (#48) are the last units of this run. PR5b, PR6b, PR6c and PR9b (and Gate 2) are not built; PR7b, PR8a, PR8b, PR11b, PR11c, PR12a and PR12b stay out unless the owner asks. PR11a is built (#41) and can merge any time; nothing calls it until PR11b.
- **Bug fixed by PR4d:** a quieter feed (BNN Bloomberg) never reached its category tab, because the tabs were cut to each category's 100 best stories of all time. The 2-day window with the 25-per-feed cap fixes it; `flow-busy-feed-no-crowding` guards it.
- The owner declined a fix for daily feeds waiting up to a day for their first check after each backend restart (the startup stagger ignores `last_attempt_at`). Not in the plan; left as is.
- 3-hour website-feed probe (2026-10-21, about 9 h after the e1f0390 update): no feed shows an error. Nine of 14 were checked after the update. Four daily feeds were not due yet; the owner checked them by hand (nothing new) and deleted `bamboohr`. The day-later probe is still to come.
