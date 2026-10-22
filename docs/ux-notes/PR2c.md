# PR2c: Refuse whole-list PUTs of set keys

Backend track, floating. The server change and its pytest, plus the `capture.cjs` PUT mock
(the spec's one cross-track exception) and the two flows that wrote set keys by PUT through it.

**Status (2026-10-21): the hold is lifted, and the branch is rebased onto PR2b.** See "Rebase onto
PR2b" at the end. The merge-condition text below is kept as it was written before PR2b merged.

**Merge condition (D23, D32): do not merge yet.** PR2b (the client that writes set keys only by
PATCH) has not been built. This PR is opened but held until PR2b is merged and deployed, and the
Device checks list shows both devices on the PR2b build stamp. Merged before that, every save,
hide, vote and read mark from today's bundle would be refused (409), on every device.

The gate is read from `docs/DEVICE_CHECKS.md` **on main**, because that is where the owner
replies (D21) and this branch's copy reaches main only when it merges. It is met when either:

- PR2b's own Device checks lines are ticked (or moved under "Checked") on the phone and on the
  desktop. Step 0 of every check confirms the Settings build line on the device, so a ticked
  PR2b line means that device ran the PR2b build; or
- if PR2b's lines don't cover both devices, the line the PR2b session adds to main once PR2b's
  main-branch build is green, `- [ ] #<PR2b> sha-<short> · Force-quit and reopen the
  home-screen app, reload the desktop tab, and open Settings → Reset on each. Pass: both show
  the PR2b build.`, is ticked.

This PR adds no merge-condition line of its own: on this branch it could never be ticked before
merge, and the list's rule is that a check runs only after the main-branch build for its merge
commit is green.

## What changed

- **`backend/app/routes/preferences.py`**
  - `SET_KEYS`: the one list of set keys, `tuple(SET_KEY_CAPS)`: `starred_entries`,
    `hidden_entries`, `voted_entries`, `read_entries:*`, `seen:*`. `muted_sources` is not in it:
    PR5b hasn't merged, and it adds the key to `SET_KEY_CAPS`, which makes it a PATCH key and a
    refused PUT key at once.
  - `_set_key(key)` (was `_set_key_cap`) returns the `SET_KEYS` entry a key falls under
    (`seen:Tech` -> `seen:*`) or None. PATCH uses it for its allowlist (400 for anything else)
    and then looks up the cap; PUT uses it for the refusal. So the PATCH allowlist and the PUT
    refusal share one list, as the spec asks.
  - `PUT /api/preferences/{key}` of a set key returns 409 with detail
    `"Popping was updated. Reload to keep saving."` (`SET_KEY_PUT_DETAIL`) and writes nothing.
    The allowlist runs first, so an unknown key (`seen:`, `hidden_entries:x`, a newline, a NUL,
    over 60 characters) is still a 400, not a 409.
  - PATCH and DELETE are unchanged. GET is unchanged.
  - Docstrings and comments: the module's endpoint list, the PUT docstring (why the 409), and
    the coercion notes (rows PUT before this are still in the database, so PATCH keeps coercing
    them).
- **`frontend/scripts/iphone-audit/capture.cjs`**: the preference store's PUT answers 409 with
  the same detail for set keys (`PREF_SET_KEY_RE`, mirroring `SET_KEYS`) and leaves the stored
  row alone. The mock comment and `frontend/scripts/iphone-audit/README.md` say so.
- **`frontend/scripts/iphone-audit/flows.cjs`**
  - `flow-h1-pref-store` PUT `hidden_entries` and `starred_entries` to test the store. It now
    writes `last_viewed:Probe` and `column_prefs:Probe` (keys no column uses) for the same
    checks, and also asserts that a PUT of `hidden_entries` is a 409 with the detail and that
    no row appears. This is the mock-level "old-bundle PUT refused" check.
  - `flow-reload-on-new-build`: today's bundle still saves by PUT, which the mock store now
    refuses with 409. The flow's own `starred_entries` route already answered the refused
    sends (steps 6-8b); it now also answers the held send itself, with a 200 `{ key, value,
    updated_at }` row after the 500 ms hold and the `put-done` event, instead of passing it on
    to the store. So step 8c still tests PR1r's "fails (503, 503), then saved, then reload"
    path, which no other step covers (step 7 is the refused-for-good path). The 8c comment,
    its failure message and the README row are unchanged from PR1r.

## Tests

- **New `backend/tests/test_preferences_put_set_keys.py`** (35 cases):
  - `test_set_keys_are_pinned` (no_db): `SET_KEYS` is exactly the five set keys, has no
    duplicates, equals `SET_KEY_CAPS`' keys (the PATCH allowlist), and every entry has a 409
    case below; none of the PUT-key examples is a set key.
  - A 409 per set key, with examples `starred_entries`, `hidden_entries`, `voted_entries`,
    `read_entries:Tech`, `read_entries:For You`, `read_entries:News/Tech`, `seen:Tech` and
    `seen:News/Tech`: the detail text, and no row written.
  - The case the 409 exists for: a row written by PATCH (another device) is unchanged after a
    stale whole-list PUT, per set key.
  - PATCH and DELETE of each set key still work.
  - PUT of `column_prefs:*` (with and without `/`), `last_viewed:*`, `column_sections:*`,
    `filter_presets` and `history_group_by` is still stored.
  - PUT of an unknown key is still 400.
- **`test_preferences_anonymous.py`**: the anonymous round trip PUTs `column_prefs:1` instead
  of `read_entries:1`, and a new `test_anonymous_round_trip_patch_get_list_delete` does the same
  round trip for `read_entries:1` by PATCH. The suffix-charset test PUTs `last_viewed:Saved`,
  `column_sections:For You` and `column_prefs:Filtering`, and PATCHes `read_entries:For You`
  and `seen:For You`.
- **`test_preferences_delta.py`**: PR2a's tests seeded set-key rows by PUT.
  - Rows that only an old PUT could produce (over the cap, malformed lists and vote maps) are
    now written straight to the table by `_store()`, the way a pre-PR2c PUT stored them.
    `test_cap_trims_an_over_cap_row_written_by_put` is renamed `..._by_an_old_put`.
  - Rows a client can still produce are seeded by PATCH (`remove` before `add`, the cap trim's
    newest-first order).
  - The slash-key round trip now expects PUT of `read_entries:News/Tech` and `seen:News/Tech`
    to be a 409 (which still proves the route matched; a plain `{key}` gave 404) and writes the
    row by PATCH. A new `test_slash_key_put_of_a_non_set_key` keeps a stored PUT through a
    `/` key (`column_prefs:News/Tech`).
  - The 60-character check confirms 60 still works by PATCH on the set key and by PUT on a
    60-character `column_prefs:` key.

## Verification

Run twice: first on main da8d2a0 (PR10a), then again after rebasing onto main 2d5f052 (PR9a
and PR1b merged in between).

- **Full backend pytest** on local Postgres (`popping_test`): on da8d2a0, **13 failed, 1172
  passed**; after the rebase, **13 failed, 1275 passed**. Both times the 13 failures diff clean
  against the DNS-only list in `/root/popping-env/baseline.txt`.
- The preference tests and `test_oidc_route_gating.py`: 128 passed.
- **Frontend** (`capture.cjs`, `flows.cjs` and the README changed; no app source):
  - `npm run lint` 0 errors (the 6 existing warnings), `npm run build` ok, unit tests 72/72 on
    da8d2a0 and 88/88 after the rebase. `package.json` and the lockfile are unchanged.
  - Full `smoke.cjs --csp --strict` on a preview at 127.0.0.1:4174, on da8d2a0: **0 failures,
    0 warnings** (238 s). `flow-h1-pref-store` and `flow-reload-on-new-build` (a timing flow)
    then ran 5 more times together: 0 failures each time.
  - **After the rebase: 8 failures in 5 flows, all expected until PR2b.** PR1b (#36) added
    flows that save or vote through today's bundle, which still PUTs the whole
    `starred_entries` / `voted_entries` list. The mock now answers those with the 409, so each
    logs a failed load (C8), and `flow-tab-stays-on-star` also finds no Saved tab after its
    reload (`TAB-STAR`), because the save was refused:
    `flow-tab-stays-on-star`, `flow-unsave-last-saved`, `flow-foryou-down-settles`,
    `flow-foryou-down-holds-fresh`, `flow-foryou-down-holds-index`. With the mock's 409
    disabled, the same five pass (0 failures), so the 409 is the only cause. This is the D23
    merge condition showing up in the harness: today's bundle can't save against this server.
    They pass again once this branch sits on PR2b, whose bundle writes those keys by PATCH.
    Every other state and flow passed, including `flow-h1-pref-store` and
    `flow-reload-on-new-build`.
- **Mutation checks** (each line broken, the tests run, the file restored from a copy and
  compared byte for byte):

  | Broken line | Failing test(s) |
  |---|---|
  | PUT's `if _set_key(key) is not None:` disabled | all 8 `test_put_of_a_set_key_is_409_and_writes_nothing`, all 8 `test_put_of_a_set_key_keeps_the_stored_row`, both `test_slash_keys_round_trip_through_every_route` (18) |
  | `seen:*` left out of `SET_KEYS` | `test_set_keys_are_pinned`, the `seen:*` cases of the three per-key tests, and the PATCH tests on `seen:*` keys in `test_preferences_delta.py` and `test_preferences_anonymous.py` (12): one list drives both routes |
  | the 409 check moved above `_validate_key` | `test_put_of_an_unknown_key_is_still_400[seen:]`, `test_key_longer_than_60_is_400[PUT]`, and the `read_entries:a\nb` and `seen:a\x00b` PUT cases of `test_newline_or_nul_in_key_is_400` (4) |
  | `capture.cjs`: the set-key 409 disabled (`npm run build`, then `--only flow-h1-pref-store`) | `flow-h1-pref-store`: `H1-PREFS: a PUT of a set key answered 200 …, expected 409`, and the list and since checks see `hidden_entries` |

- **Security checklist:** no new route; PUT keeps its Pydantic body and gains a refusal before
  any write; the allowlist still runs first; no new stored-JSON reads; no network I/O.
- Housekeeping: the preview on port 4174 was held by a `vite preview` left over from a removed
  worktree (`wf_3047c0f2-d9b-1`, nothing driving it); it was stopped by PID before this unit's
  preview started there.

## Review fixes (second commit)

- **Device checks merge gate.** The first commit's Pending line "Merge condition, checked before
  #<pr> merges" is removed: it existed only on this held branch, so nothing on main could be
  ticked before the merge, and it broke the list's "run a check only after the main-branch build
  is green" rule. The gate is now defined by what is on main (top of this file, and the PR2b
  open item).
- **`flow-reload-on-new-build` step 8c** again tests "a pending save fails (503, 503), then is
  saved, then the page reloads". The first commit let the third send fall through to the mock's
  new 409, which repeated step 7's refused-for-good path and left the `'saved'` outcome of
  `put()` followed by a reload untested. The flow's route now answers that send 200 with a
  `{ key: 'starred_entries', value, updated_at }` row; the 8c comment and failure message are
  back to PR1r's wording, which the README row and the flow's header comment already match.
- **Checks after the fixes** (preview on 127.0.0.1:4174; frontend harness and docs only, no app
  or backend source changed, so pytest was not re-run):
  - `npm run lint` 0 errors (the 6 existing warnings), `npm run build` ok, unit tests 88/88,
    `package.json` and the lockfile unchanged.
  - `flow-reload-on-new-build` and `flow-h1-pref-store` with `--csp --strict`, 5 runs: 0
    failures each time.
  - Mutation check: `preferences.tsx`'s successful-PUT result changed from `'saved'` to
    `'unsaved'`, then `npm run build` and `--only flow-reload-on-new-build`: fails with
    `RELOAD-FLUSH: back after 11 min with a new build: no reload` (events `nav, nav, put-done`,
    2 reloads), so 8c now catches a save that succeeds on retry but still holds the reload. With
    the first commit's 409 this mutation passed. Restored with `git checkout` and `git diff
    --exit-code`, then rebuilt.
  - Full `smoke.cjs --csp --strict`: the same 8 expected failures in the 5 PR1b flows listed
    under Verification (the 409 against today's bundle), plus one `flow-fl-rename-keeps-filter-and-view`
    `FL: the field shows "Slickdeals Renamed"` while another session's smoke loaded the
    container (load average 4.6). It doesn't touch set keys or the reload flow, and passed 3 of
    3 runs on its own right after.

## Deviations from the spec

- **`SET_KEYS` is derived from `SET_KEY_CAPS`** (`tuple(SET_KEY_CAPS)`), as PR2a's notes
  proposed, rather than a second literal list. The caps dict is where a new set key has to go
  anyway (PATCH needs its cap), so one edit adds it to PATCH and to the PUT refusal. The pin
  test fixes the five names.
- **No dedicated "old-bundle PUT refused" flow.** The Verification list names one, but the
  bundle under test when this PR merges is PR2b's, which never sends that PUT, and a flow that
  drives today's bundle (tap Save, see the 409 toast) would fail as soon as this branch is
  rebased onto PR2b. The refusal is covered by pytest per key, by `flow-h1-pref-store`'s
  mock-level 409 check, and by smoke's C8 rule: a 409 is a failed load in the browser console,
  so once PR2b's flows save, hide and vote, any set-key PUT they sent would fail them unless
  listed in `expectedFailedLoads`.
- **The frontend CI job is red on this PR until it is rebased onto PR2b.** The five PR1b flows
  above fail against the spec's 409 mock with today's bundle. Not changed on purpose: adding the
  set-key paths to their `expectedFailedLoads` would hide exactly the regression the mock is
  there to catch, and holding the mock change back would leave the harness answering 200 to a
  request the server refuses. The PR is held anyway (D23), and its CI is re-run after the
  rebase.
- **Today's bundle doesn't "retry once" on the 409.** The spec says a stale bundle's `put()`
  retries once and then toasts. Bundles from PR1r on don't retry a 4xx (a 409 included), and
  toast "Couldn't save your latest changes to Popping…" at once; older bundles retry once. Either
  way the stored row is untouched.

## Open items for later units

- **Before merging this PR:** PR2b merged and deployed, PR2b's legacy seed sends no set-key PUT
  (its unit test "legacy localStorage lists plus server rows producing no set-key PUT"), and the
  merge gate above met on main's `docs/DEVICE_CHECKS.md`. Then rebase onto main: PR2b rewrites
  `flow-reload-on-new-build`'s saves (PATCH, not PUT) and adds PATCH to the `capture.cjs`
  store, so expect conflicts in `flows.cjs` and `capture.cjs`; keep PR2b's versions and re-apply
  this PR's set-key 409 in the PUT branch and the `flow-h1-pref-store` changes. Re-run the full
  strict smoke and every flow after the rebase; `flow-tab-stays-on-star`,
  `flow-unsave-last-saved` and the three `flow-foryou-down-*` flows must pass with the 409 in
  place. In `flow-reload-on-new-build`, PR2b's PATCH version of the save restores the saved path
  through the store (its PATCH answers 200), so the flow's own 200 for a PUT is no longer
  needed; after the rebase, check that step 8c still ends in a successful save (a 200 for its
  third send) before the reload, not a refusal.
- **PR2b:** this PR's merge gate is read from main (see the top). If PR2b's Device checks lines
  don't show that the phone and the desktop both run the PR2b build, add the line quoted above
  to main's `docs/DEVICE_CHECKS.md` once PR2b's main-branch build is green.
- **PR2b:** its flows that save, hide or vote should leave the set-key paths out of
  `expectedFailedLoads`, so the mock's 409 catches any PUT that slips through. To see this
  PR's guard before PR2c merges, run PR2b's branch with this PR's `capture.cjs` PUT branch: the
  five PR1b flows above should pass. `isSetKey` in
  `prefSync.ts` and `PREF_SET_KEY_RE` in `capture.cjs` should agree with `SET_KEYS`; a pin in
  a frontend-owned `test_frontend_*` file can check that once `prefSync.ts` exists.
- **PR5b:** add `muted_sources` to `SET_KEY_CAPS` (it joins `SET_KEYS`, so PUT of it becomes a
  409), to the pin and the examples in `test_preferences_put_set_keys.py`, and to
  `PREF_SET_KEY_RE` in `capture.cjs`.
- **Revert:** safe. PUT of set keys is accepted again, and PR2b never sends it.

## Device checks

Added under Pending in `docs/DEVICE_CHECKS.md`, with `#<pr>` as the PR placeholder. Both are run
after this PR's main-branch build is green and deployed; the merge gate itself is not one of
them (see the top).

- `- [ ] #<pr> · After the deploy, on the phone save a story, hide one and upvote one, then reload the desktop tab. Pass: the desktop shows all three, and neither device shows "Couldn't save".`
- `- [ ] #<pr> · If any device or tab still runs a build from before PR2b, save a story there. Pass: it shows "Couldn't save…", and after a reload of that device your saves from the phone are all still there and saving works.`

## Rebase onto PR2b (2026-10-21)

The hold is lifted. PR2b is merged (#40) and deployed, and the owner's phone shows its build in
Settings (`b6bd7e7f`, 20 Oct 21:09). That is the condition as the owner restated it on 2026-10-21:
the phone only, since no desktop is in use and no browser tab is open on an old build
(`docs/ux-notes/PROGRESS.md`). This PR's three commits were squashed into one and rebased onto
main 6ae69ae, which adds PR1c (#38), PR2b (#40), PR10b (#39), PR7a (#42), PR3a (#43) and D34.

- **Backend:** merged cleanly. `SET_KEYS` is still `tuple(SET_KEY_CAPS)` and covers `seen:*`.
  PR7a's new `category_layout` key is a PUT key, not a set key, so it is still stored. It joins
  `PUT_KEY_EXAMPLES` in `test_preferences_put_set_keys.py`, which now has 36 cases.
- **`capture.cjs`:** PR2b's mock is kept as it was: the stateful store, PATCH, `prefMock` and
  `onSetKeyPut`. Its PUT branch still reports a set key through `onSetKeyPut`, and now also
  answers it 409 `{ "detail": "Popping was updated. Reload to keep saving." }`
  (`SET_KEY_PUT_DETAIL`) and leaves the row alone. C17 in `smoke.cjs` is unchanged and remains
  the guard, because the bundle no longer sends these PUTs. The 409 makes the mock answer as the
  server does.
- **`flows.cjs`:** every flow is PR2b's version.
  - `flow-h1-pref-store`: PR2b already tests the store with `column_prefs:news` and
    `filter_presets`, so this PR's key swap is dropped. The set-key check is re-applied at the
    end of the flow. PUTs of `hidden_entries` (seeded) and `seen:News/Tech` answer 409 with the
    detail, the seeded row is unchanged, no row is created, and `onSetKeyPut` reported both.
    The check runs in a second browser context with its own `mockApi`, because in the flow's
    context smoke reports any set-key PUT as C17.
  - `flow-reload-on-new-build`: this PR's step 8c change is gone. The flow answered the held PUT
    itself with a 200, which was a workaround for the bundle that saved by PUT. PR2b's saves are
    PATCHes, which the store answers 200.
- **Docs:**
  - `frontend/scripts/iphone-audit/README.md`: main's rows and store paragraph. The 409 is added
    to the `flow-h1-pref-store` row, the store paragraph and the C17 row.
  - `docs/DEVICE_CHECKS.md`: main's list, plus this PR's two #37 lines under Pending, between
    #36 and #38. This branch's merge-condition line was already removed in the review fixes.
  - `PROGRESS.md`: main's table, with PR2c "in review | #37" and the condition noted as met.
- The deviation "the frontend CI job is red on this PR until it is rebased onto PR2b" no longer
  applies. The five PR1b flows pass with the 409 in place.

Checks after the rebase (database `popping_test`, preview on 127.0.0.1:4175):

- **Full backend pytest:** 13 failed, 1755 passed. The 13 failures match the DNS-only list in
  `/root/popping-env/baseline.txt`. The four preference test files (`put_set_keys`, `anonymous`,
  `delta`, `category_layout`): 133 passed.
- **Frontend:** `npm run lint` 0 errors (5 warnings), `npm run build` ok, unit tests 175/175.
  `package.json` and the lockfile are unchanged.
- **Full `smoke.cjs --csp --strict` with `fonts-ci.conf`:** 0 failures, 0 warnings, 155 runs in
  1003 s, with a load average of 4 to 6 from other sessions.
  - Passed: `flow-tab-stays-on-star`, `flow-unsave-last-saved`, the three `flow-foryou-down-*`
    flows, `flow-h1-pref-store` and `flow-reload-on-new-build`.
  - `flow-h1-pref-store` and `flow-reload-on-new-build` then ran 3 more times together: 0
    failures each time.
- **Mutation checks:** each file was restored from a copy and compared byte for byte, and the
  tests passed again.

  | Broken line | Failing test(s) |
  |---|---|
  | PUT's `if _set_key(key) is not None:` disabled | 16 of 36 in `test_preferences_put_set_keys.py`: all 8 `test_put_of_a_set_key_is_409_and_writes_nothing` and all 8 `test_put_of_a_set_key_keeps_the_stored_row` |
  | `capture.cjs`: the set-key 409 removed (the PUT falls through and stores the row) | `flow-h1-pref-store`, 4 × `H1-PREFS`: both PUTs "answered 200 …, expected 409", "the refused PUT changed hidden_entries to [999]", and the store holds `hidden_entries,seen:News/Tech` |
  | `capture.cjs`: the `onSetKeyPut` call removed from the set-key branch | `flow-h1-pref-store`: `H1-PREFS: onSetKeyPut reported []` |
