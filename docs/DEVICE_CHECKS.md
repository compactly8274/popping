# Device checks

This file is the running list of on-device checks for the UX overhaul (`docs/UX_OVERHAUL_PLAN.md`, D21). It is the only reply channel, and every session reads it first. Issues are turned off on this repo, so the list lives here.

## How a check works

1. Each unit's PR adds its lines under **Pending**. Run a check only after the main-branch build for that merge commit is green.
2. Step 0, every time:
   - Run `popping-update`.
   - Paste the revision check under the line:
     `cd /opt/popping && for s in backend frontend; do printf '%s ' $s; docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' $(docker compose images -q $s); done`
   - Force-quit the home-screen app, reopen it, and reload any desktop tab. A resumed page keeps the old bundle.
   - Once PR1r is in, confirm Settings shows the expected build.
3. Reply by editing this file on GitHub: tick a line when it passes, or write `fail: <what you saw>` under it. Telling a session in chat works too; the session records it here.
4. Gates never pass on their own. Write `pass`, `fail: <line>` or `waive` under the gate. While a gate is open, frontend units don't merge; the backend track continues.
5. Rollback:
   - Never edit files in `/opt/popping`.
   - Never pin a tag in `.env`.
   - Frontend-only rollback: `cd /opt/popping && POPPING_IMAGE_TAG=sha-<last good> docker compose up -d --no-deps frontend`. `--no-deps` keeps the backend on its current image: without it, compose recreates the backend on the old tag too, and from PR9a on a backend image older than PR9a can't start against the database (Rollback in `docs/ux-notes/PR9a.md`). After that, no `popping-update` until the revert is merged and green.

**Last good:** `sha-8516098` (2026-10-22: the #46 merge, PR3b and PR3c; the phone checks and Gate 1 passed).

## Pending

- [ ] #27 · Open Menu → Settings → History. Pass: recent hides and saves are listed.
- [ ] #27 · Add a Reddit feed (r/python) and tap Test. Pass: it shows a post count and titles, not an error.
- [ ] #30 · Add a feed with a long back catalogue (a podcast or a blog with 50+ posts) and wait for its first check. Pass: its tab gains at most 10 stories plus any from the last 2 days.
- [ ] #30 · A day or more after the deploy, open the On This Day tab. Pass: the stories that arrived in the last 24 hours number at most 10 (at most 5 per UTC day, which turns at 5 pm Pacific), and none of them are in For You. Stories fetched before the deploy don't count.
- [ ] #30 · The day after adding that feed, open Brief. Pass: no highlight is one of the new feed's older stories.
- [ ] #31 · Pause a feed that has a story in For You, then force-quit and reopen the app and open For You. Pass: none of that feed's stories are in For You (resume the feed afterwards).
- [ ] #35 · An hour after deploying, run `cd /opt/popping && docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT name, last_attempt_at, last_item_count, last_new_count, left(last_error, 30) AS error FROM sources ORDER BY last_attempt_at DESC NULLS LAST LIMIT 15"'`. Pass: the feeds checked since the deploy show a `last_attempt_at` after it, and the ones without an error show both counts.
- [ ] #37 · After the deploy, on the phone save a story, hide one and upvote one, then reload the desktop tab. Pass: the desktop shows all three, and neither device shows "Couldn't save".
- [ ] #37 · If any device or tab still runs a build from before PR2b, save a story there. Pass: it shows "Couldn't save…", and after a reload of that device your saves from the phone are all still there and saving works.
- [ ] #38 · Open For You's ⋯. Pass: Best is chosen; tapping Newest puts the most recent stories first, and Best puts back the order it had.
- [ ] #38 · With a saved view chip showing, set News to Best and tap the chip. Pass: News stays on Best.
- [ ] #38 · Only if no AI provider is set up: open the Brief tab. Pass: it shows "Brief needs an AI provider · Set up" on one line; Set up opens Settings › AI. (With a provider set up: Generate or Regenerate shows as before.)
- [ ] #39 · A day after deploying, open the tab of a website feed that used to pull pages that aren't articles. Pass: the stories that arrived since the deploy are its articles, with no About, Contact, tag, author or shop pages.
- [ ] #40 · With Popping open on the phone, save a story on the laptop, then wait a minute. Pass: the story shows as saved on the phone without a reload.
- [ ] #40 · Hide a story on the phone and, within a few seconds, save a different story on the laptop; then force-quit and reopen the phone app and reload the laptop. Pass: on both, the hidden story stays hidden and the saved one stays saved.
- [ ] #42 · After moving any tab, give a feed a new category (Menu → Settings → Feeds, open the feed, New category). Pass: the new category's tab is last in the strip, and the feed's category list follows the tab order.
- [ ] #42 · With VoiceOver on, open Edit and tap ↑ on a tab. Pass: VoiceOver reads the tab's name and new place, such as "News, 5 of 9", and focus stays on that ↑.
- [ ] #42 · Turn on Airplane Mode, force-quit the home-screen app and open it again. Pass: the red error line shows, and no tab offers "Add your first feed".
- [ ] #43 · (If you have a desktop) On the laptop, make the browser window shorter than the For You row, then scroll down with the trackpad or mouse wheel. Pass: everything under the header (Brief, For You and the columns) scrolls as one page, the header stays at the top, and the columns under For You show their stories at full height, not as a thin strip.
- [ ] #43 · (If you have a desktop) On the laptop, scroll down past the 30th story of a column with more than 30. Pass: the next stories appear as you near the end of the column.
- [ ] #43 · With rotation unlocked, open the home-screen app with the phone held sideways, on a tab with more than 30 stories, then turn it upright. Pass: scrolling the tab to its end loads more stories, and tapping the tab again takes the list back to its top.
- [ ] #45 · (If you have a desktop) On the laptop, open a story from the For You row at the top, then look at its category's column. Pass: the column's "N new" is one lower and the column didn't move.
- [ ] #45 · (If you have a desktop) On the laptop, scroll down to the end of a short column that sits next to a much longer one, press Tab until its "N not seen · Mark as read" is focused, then press Enter. Pass: the page scrolls up to that column's "You're all caught up", which shows the focus ring, and the next Tab goes on from there without a jump.
- [ ] #45 · (If you have a desktop) In Chrome or Edge on the laptop, scroll down to the end of a short column that sits next to a much longer one and click its "N not seen · Mark as read" with the mouse (or trackpad). Pass: the page stays where it is, and the longer column next to it is still at the story you were looking at.
- [ ] #46 · (If you have a desktop) On the laptop, scroll slowly down a column past several stories. Pass: its "N new" doesn't change; on the desktop only opening a story counts it as seen.
- [ ] #47 · Open Menu → Settings → Saved and note the stories it lists, then close Settings and open the Saved tab. Pass: the tab lists the same stories, except any you also hid, each with its picture where it has one, including ones older than anything in the other tabs.
- [ ] #47 · Save one story, then another, then open Saved. Pass: the second one is at the top, the first right under it.
- [ ] #47 · In Saved, tap the bookmark on a story to remove it, then force-quit and reopen the app. Pass: it is gone from Saved and stays gone.
- [ ] #47 · On For You, hide a story and tap Undo on the toast, then open that story's category tab and go back to For You. Pass: the story is in its tab, and back in For You where it was.
- [ ] #47 · On For You, note the top three stories. Hide the first, wait a minute with Popping open, hide the third, and wait another minute. Open Menu → Settings → Hidden, tap Restore all, close Settings and open the first story's category tab. Pass: the story is there straight away; back on For You, the three are in the order you noted (a new story may sit between them).
- [ ] #47 · (If you have a desktop) On the laptop, search for an older story the phone's tabs don't show and save it, then wait a minute with Popping open on the phone. Pass: it shows at the top of Saved on the phone, with its picture.
- [ ] #47 · (If you have more than 30 saves) Open the Saved tab, force-quit Popping and reopen it. Pass: Saved fills in without the app freezing, and more stories load as you scroll to the end.
- [ ] #48 · Open any tab. Pass: each card shows the feed's icon, name and a short time ("5h") on one line above the title, a line or two of preview text under the title (grey, cut after 2 lines; a card with nothing to show has none), and one row of buttons under it: ▲ ▼, a bookmark, Share, the Summary chevron and ⋯; no ✓, no eye, no "ago".
- [ ] #48 · Tap the preview text under a title. Pass: the story opens in Safari; back in Popping that card is still in place and the tab's badge is one lower.
- [ ] #48 · Tap the Summary chevron on a card with preview text. Pass: the full summary opens in place and the preview line is gone; tapping the chevron again closes it and the preview is back.
- [ ] #48 · Tap ▲ on a card, then ▲ again; then ▼. Pass: the arrow turns orange, then grey, then ▼ turns blue; the card never moves and no toast shows.
- [ ] #48 · Tap the bookmark on a card, then tap it again. Pass: it fills with no toast; the second tap empties it and shows "Removed from Saved · Undo", and Undo fills it again; the story is in Saved after that.
- [ ] #48 · Tap Share on a card. Pass: the iOS share sheet opens with the story's own link (for a Reddit story, reddit.com, not old.reddit.com); cancelling it does nothing.
- [ ] #48 · Tap a card's ⋯. Pass: a sheet rises from the bottom with the story's title and the rows Hide, Edit feed, Copy link, Open in browser and Cancel, all in reach; a tap on the dimmed area above closes it; Edit feed opens that feed in Settings; Copy link shows "Link copied".
- [ ] #48 · ⋯ → Hide on a card, then Undo in the toast. Pass: the card leaves at once and comes back in its place; a few minutes later that story is not listed under Menu → Settings → History's hides (the ranking never heard about it).
- [ ] #48 · ⋯ → Hide on a card and let the toast go. Pass: the card stays gone after a force-quit and reopen, and the story is listed under Settings → History's hides within a few minutes.
- [ ] #48 · Swipe a card left, past about a third of its width. Pass: a red "Hide" shows under it as it moves, and letting go hides it with "Hidden · Undo"; a short drag left springs back and hides nothing.
- [ ] #48 · Swipe a card right. Pass: it doesn't move and nothing happens.
- [ ] #48 · With a card's ⋯ sheet open, wait 3 seconds with cards behind it, then close it and flick the top card off the screen. Pass: the badge doesn't drop (a story behind the sheet wasn't being read).
- [ ] #48 · Tap a feed's name on a card. Pass: that feed opens in Settings (as before; a one-feed view comes later).
- [ ] #48 · (If you have a desktop) On the laptop, right-click a card. Pass: a small menu with Hide, Edit feed, Copy link and Open in browser; with a card selected by the arrow keys, `a` and `z` vote and vote back, `h` hides with Undo, `b` saves silently and unsaves with "Removed from Saved · Undo", and `?` lists the four keys.
- [ ] #49 · Open a category tab's ⋯. Pass: it shows "Show stories from" with 1 day · 2 days · 7 days · All and 2 days chosen, and no "Hide if older than".
- [ ] #49 · Force-quit and reopen the app, then open a busy tab (News). Pass: its stories all arrived in the last 2 days (no story older than that above or below the line), and a feed that posts a lot shows at most 25 of its stories.
- [ ] #49 · On News (Newest), scroll through the last day. Pass: BNN Bloomberg's stories from today are there among the others by time, not missing while the busy feeds' older stories fill the tab.
- [ ] #49 · On that tab, tap ⋯ → 7 days. Pass: the older stories join the list in order (on Newest, at the end; ones you had read go under "Earlier"), the badge counts the new ones, and the choice holds after a force-quit and reopen.
- [ ] #49 · With that tab at 7 days, switch to another tab and back. Pass: the 7-day list is there at once, in the same order; set it back to 2 days and the older stories leave.
- [ ] #49 · Open Saved. Pass: it has no ⋯ (nothing in it would apply), and Saved still lists every save, old ones included.
- [ ] #49 · Open a tab whose feeds brought nothing in 2 days (or pick 1 day on a slow tab). Pass: the tab is still in the strip and reads "Nothing in 2 days · Show 7 days" (or "Nothing in 1 day"); tapping "Show 7 days" shows its stories.
- [ ] #49 · Pause every feed of one category (Menu → Settings → Feeds), then force-quit and reopen. Pass: that tab is gone from the strip and from Edit tabs' list; resume one feed and reopen: it is back.
- [ ] #49 · Open the On this day tab. Pass: the tab reads "On this day", and its cards say "On this day" where the time would be.
- [ ] #49 · (If you have a desktop) On the laptop, look at the For You row. Pass: it shows 6 stories, and the rest of For You's stories are in their category columns.

## Gate 1: seen on scroll (PR3b and PR3c, deployed together)

Opens when PR3c merges. Every line is on the phone (the home-screen app).

- [x] Open the top story of a badged tab and come back. Pass: the badge drops by 1 and the list hasn't moved.
- [x] Scroll slowly down a badged tab past 5 stories, about a second on each, then look at the badge. Pass: it is 5 lower (they count as seen), and scrolling back up shows those 5 still in their places.
- [x] On a tab with 30 or more new stories, fling past 20 in one go. Pass: those stay unseen (the badge drops only by the few you had on screen before the fling), and at the end the row offers "N not seen · Mark as read", not "You're all caught up".
- [x] Dwell feel: read a tab at your normal pace for a minute. Too eager (stories you only glanced at count as seen) → `SEEN_DWELL_MS` 1500, right → 1000, too slow (stories you read don't count) → 700. This is a retune, not a stop.
- [x] With rotation unlocked, turn the phone sideways and back upright, then scroll slowly past 2 stories. Pass: the badge drops by 2 (marks still happen).

Passed on 2026-10-22 (build 8516098, PR3b and PR3c). The owner judged the dwell "about right": `SEEN_DWELL_MS` stays at 1000. They would like a visual cue when a story counts as seen; no change for now.

## Gate 2: pull to refresh (PR6b and PR6c, deployed together; also PR5a's swipe and sheet)

Opens when PR6c merges.

- [ ] Pull at the top. Pass: the spinner follows your finger, and a release past the threshold shows "Checking N feeds…" and then a result. A short pull springs back with no check.
- [ ] Shaky upward scroll from the top. Pass: normal scrolling, no spinner.
- [ ] Diagonal drag. Pass: a pull or a card swipe, never both, never a stuck list.
- [ ] Long-press a link. Pass: the iOS preview, no spinner.
- [ ] Short tab. Pass: same as a pull at the top.
- [ ] Ten slow pulls in a row. Pass: all arm.
- [ ] Pass: no doubled movement.
- [ ] Small downward wobble, then scroll up. Pass: normal scrolling.
- [ ] VoiceOver. Pass: it reads "Checking…" and the result.
- [ ] Swipe a card left (PR5a). Pass: a red "Hide" shows under the card as it moves; letting go past about a third of the width hides it with "Hidden · Undo", and Undo puts it back in its place. A diagonal drag either scrolls or swipes, never both.
- [ ] Open a card's ⋯ (PR5a). Pass: the sheet rises from the bottom with Hide, Edit feed, Copy link, Open in browser and Cancel, every row in reach above the home indicator, and a tap on the dimmed area closes it.

## Checked

- 2026-10-22, build 8516098 (PR3b and PR3c; frontend auto-reload confirmed):
  - [x] #32 · After the next frontend deploy, leave the home-screen app in the background for 10 minutes or more without force-quitting it, then come back to it. Pass: it reloads by itself once, and Settings → Reset shows the new build; a desktop tab that was hidden as long shows "New version · Reload" instead.
  - [x] #45 · After the first deploy with PR3b and PR3c, force-quit and reopen the home-screen app and open a tab. Pass: the stories above "N not seen · Mark as read" all arrived in the last 2 days, and the older ones sit folded under "Earlier (N)" at the end of the tab.
  - [x] #45 · On the phone, note a tab's badge, open the top story of that tab, then come back to Popping. Pass: the badge is one lower, the story is still where it was (not faded), and the list hasn't moved.
  - [x] #45 · Scroll a tab to its end. Pass: the last story is followed by "N not seen · Mark as read"; tapping it shows "You're all caught up" with the stories folded under "Earlier (N)" and a "Marked N as read" toast whose Undo brings them back above the line.
  - [x] #45 · On a tab that says "You're all caught up", tap the row under it that names another tab (such as "News · 12"). Pass: that tab opens; a tab hidden in Edit tabs is never named, and with every tab read the row is gone.
  - [x] #45 · Tap a tab's ⋯, then Mark all as read. Pass: the tab shows "You're all caught up", its badge goes, and the same ⋯ row is greyed out until new stories arrive.
  - [x] #45 · Scroll down a tab, switch to another app for 10 minutes or more (not straight after opening a story), then come back. Pass: the tab is back at its top, and the stories you had opened are under "Earlier (N)".
  - [x] #45 · On a busy tab, open "Earlier (N)" and scroll far down it, switch to another app for 10 minutes or more, come back, open the top story, then scroll down to the "N not seen · Mark as read" row. Pass: its number matches the tab's badge, and the row stays still when you reach it.
  - [x] #45 · Open a story, read it in Safari for 10 minutes or more, then come back to Popping. Pass: the list is where you left it, with that story still in place.
  - [x] #45 · On the phone, note the badges of For You and of a category tab, then open a story of that category from For You and come back. Pass: both badges are one lower (a story opened anywhere counts as seen everywhere).
  - [x] #45 · On a tab that says "You're all caught up", open "Earlier (N)", scroll down until that line is off the top of the screen, and stay there while new stories come in (a busy tab, a few minutes). Pass: the story you are reading never jumps; back at the top, the new stories are above the line.
  - [x] #45 · With VoiceOver on, go to the end of a tab and double-tap "N not seen · Mark as read". Pass: VoiceOver lands on "You're all caught up" in its place, not at the top of the page, and can reach the toast's Undo.
  - [x] #46 · On the phone, open a tab with only 2 or 3 new stories (all on screen with the "N not seen · Mark as read" row under them) and wait 2 seconds. Pass: the row turns into "You're all caught up" and the tab's badge goes, with nothing moving.
  - [x] #46 · On a tab with a badge, read the top story's card for a couple of seconds, scroll it off the top, and switch to another app straight away; force-quit Popping from the app switcher and reopen it. Pass: that tab's badge is one lower than before you scrolled.
  - [x] #46 · Tap a tab with a badge and straight away (within a second) open the Menu; leave it open for a few seconds, close it, and flick the top story off the screen at once. Pass: the badge doesn't drop (a story behind the Menu wasn't being read).
  - [x] #46 · Scroll slowly through Saved, and through a search's results. Pass: no tab's badge changes; only opening a story from there counts it as seen.
  - [x] #46 · On the phone, read a few screens down a tab with a badge, then, with a story on screen, tap Refresh at the top. Pass: the tab goes back to its top with that story first (after any new ones), the badge counts it as not seen yet, and the stories you scrolled past are under "Earlier".
  - Phone only: the desktop lines of #45 and #46 stay under Pending. The owner reported that all the phone checks passed and that the app reloaded by itself after 10 minutes in the background.
- 2026-10-21/22, backend e1f0390 (PR10b, deployed 06:54 UTC 2026-10-21):
  - [x] #39 · Baseline, the one check that runs **before** Step 0's `popping-update` (it has to read the old build): with #35 already deployed (the query reads its columns), run `cd /opt/popping && DEPLOYED=$(docker inspect -f '{{.Created}}' $(docker compose ps -q backend)) && docker compose exec -e DEPLOYED="$DEPLOYED" postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT s.name, s.active, s.last_attempt_at, s.last_item_count AS listed, s.last_new_count AS new, left(s.last_error, 40) AS error, count(e.id) FILTER (WHERE e.fetched_at >= '\''$DEPLOYED'\''::timestamptz) AS since_deploy, count(e.id) FILTER (WHERE e.fetched_at >= now() - make_interval(hours => 24)) AS stories_24h, count(e.id) AS stories_7d FROM sources s LEFT JOIN entries e ON e.source_id = s.id AND e.fetched_at >= now() - make_interval(days => 7) WHERE s.type = '\''generic_scrape'\'' GROUP BY s.id ORDER BY s.name"'` and keep the output. Pass: it prints a row per website feed (`since_deploy` counts from the previous deploy here and doesn't matter).
  - [x] #39 · 3 hours after deploying this PR (a website feed is checked every 2 hours by default), run the baseline's command again. Pass: every website feed has a `last_attempt_at` after the deploy, and each one with no error in the baseline still shows none (its `listed` count can be lower than before, because About, tag and shop pages no longer count); write `fail: <feed name>` for one that now shows `[no list]`, `[blocked]` or `[not found]`.
  - [x] #39 · A day or more after deploying this PR, and before deploying anything else, run the baseline's command once more: `since_deploy` counts only the stories each feed brought since the backend was deployed. Pass: each website feed with stories in the baseline's `stories_7d` has a `since_deploy` above 0, unless its site published nothing since the deploy; write `fail: <feed name>` for one whose `since_deploy` is 0 while its site kept publishing. `listed` is no signal on its own: a check with nothing new lists 0 too.
  - The owner ran the baseline at 06:48 UTC on 619000f (no errors). The 3-hour probe ran about 9 hours after the deploy: 9 of 14 website feeds checked, no errors; the 4 daily feeds were not due yet (the owner checked them by hand: nothing new) and `bamboohr` was deleted by the owner. The day-later probe (00:47 UTC 2026-10-22) listed all 13 remaining website feeds checked that day with no error. The probes used a shorter query (name, last check time, new count, error) because the full one did not fit the phone.
- 2026-10-21, build e1f0390 (the phone shows "Build b6bd7e7f · 20 Oct, 21:09"):
  - [x] #36 · On a tab other than For You, force-quit the home-screen app and reopen it. Pass: it opens on the same tab.
  - [x] (phone only) #40 · After deploying, force-quit and reopen the home-screen app, reload the desktop tab, and open Menu → Settings → Reset on each. Pass: both show the same "Build …" line, with this deploy's time. PR2c merges only after this line is ticked.
  - [x] (phone only) #40 · Save a story on the phone and switch to another app straight away; force-quit Popping from the app switcher and reopen it. Pass: the story is still in Saved, on the laptop too.
  - [x] (phone only) #40 · In the Settings app, turn on airplane mode. Come back to Popping and save a story, then switch to the Settings app, turn airplane mode off, wait for Wi-Fi to reconnect, and switch back to Popping. Pass: after a reload of the laptop tab, or its next poll (up to a minute), the story shows as saved.
  - [x] (phone only) #42 · On the phone, tap Edit at the end of the tab strip, tap ↑ on News, then Done. Pass: News moves one place left in the strip; after a force-quit and reopen, and on the laptop after a reload, the strip and the desktop columns keep the new order.
  - [x] #42 · Tap Edit, then Move to top on the last tab, then Done. Pass: that tab is now right after Brief.
  - [x] (phone only) #42 · Tap Edit, turn off Show for one category, then Done. Pass: its tab is gone from the strip, from Menu → Jump to column and from the laptop's columns, and its stories still show in For You; turning Show back on puts the tab back where it was.
  - [x] #42 · Open Saved and remove its only story. Pass: Saved stays in the strip and says "No stories yet", and the phone stays on it. This replaces #36's line about Saved moving the phone.
  - [x] #43 · On the phone, open a tab with more than 30 stories and scroll steadily to its end. Pass: more stories appear before you reach the bottom, with no tap and no visible jump, and the list ends with the tab's last story.
  - #40 build line: checked on the phone only. The owner uses Popping only as the home-screen app (no desktop or browser tab), so this is the build line PR2c waits on.
  - (phone only): the laptop halves of these lines were not checked.
  - Replaced, not run: #36 · Open Saved and remove its only story. Pass: the phone moves to the tab next to it; saving a story from there brings Saved back without moving the phone. (#42's Saved line above replaces it.)
  - Not yet run: #32's auto-reload line (leave the app in the background 10 minutes or more after a deploy).
- 2026-10-21, build 619000f (backend and frontend; revision check OK):
  - [x] (phone only) #32 · After the first deploy with PR1r, force-quit and reopen the home-screen app, reload the desktop tab, and open Menu → Settings → Reset on each. Pass: both show the same "Build <8 characters> · <day> <month>, <time>" line, with the time the image was built.
  - [x] #35 · After `popping-update` (read the PR's rollback note first), run `cd /opt/popping && docker compose exec backend alembic current`. Pass: it prints `0031 (head)`.
  - [x] #35 · Force-quit and reopen the app, then open Menu → Settings → Feeds. Pass: stories load and every feed is listed as before.
  - [x] #36 · On the phone, open the News tab and save a story from it. Pass: a Saved tab appears and the phone stays on News.
  - [x] #36 · Scroll down a tab, then tap that tab's name again. Pass: the list goes back to the top and the tab doesn't change.
  - [x] #38 · On the phone, open the News tab, tap its ⋯ and tap Best. Pass: the list reorders with the highest-ranked stories first, and after a force-quit and reopen News is still on Best.
  - [x] #38 · Save a story, then open Menu → Settings → Reset, tap Reset reading state, then Reset. Pass: after the reload the story is still in Saved, hidden stories stay hidden and the saved view chips are still there.
  - #32 build line: checked on the phone only. It shows "Build 7ebe281e · 19 Oct, 23:05" (the bundle id and its build time). The desktop half was not checked; the owner has no desktop available to test on.
