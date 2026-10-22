// Saved (PR4c, D5): every save, whatever the tabs loaded. No imports (see
// feedRules.ts), so the unit tests run it under strip-types.
//
// ``starred_entries`` holds the saves in the order they were made: a save
// appends its id (``applyIdDelta``, and the server's PATCH the same way),
// so a save made again after an unsave goes to the end. Saved lists them
// newest save first. A save the tabs don't hold (an old story, or one
// saved on another device) is fetched by id (``api.entriesByIds``) into
// App's by-id cache; ids the server no longer returns (a deleted feed's
// stories go with it) are noted, so they aren't asked for again.

/** What Saved needs of a story. */
export type Story = { id: number }

/** The saves to draw, newest save first. ``find`` gives the copy to draw
 *  (App: the tabs' copy, else the by-id cache, else the copy drawn last
 *  time); a save with no copy yet is left out until its fetch lands, and
 *  so is one ``isRemoved`` takes out (hidden: hide wins over saved; a
 *  deleted feed). An id saved twice is drawn once. */
export function savedStories<E extends Story>(
  starred: readonly number[],
  find: (id: number) => E | undefined,
  isRemoved: (e: E) => boolean,
): E[] {
  const out: E[] = []
  const drawn = new Set<number>()
  for (let i = starred.length - 1; i >= 0; i--) {
    const id = starred[i]
    if (drawn.has(id)) continue
    drawn.add(id)
    const e = find(id)
    if (e && !isRemoved(e)) out.push(e)
  }
  return out
}

/** The saves to fetch by id, newest save first (so the first chunk of 200
 *  is the top of Saved): the ones ``skip`` doesn't rule out. App skips a
 *  save the tabs or the by-id cache hold, one the server no longer
 *  returned, one already being fetched, and a hidden one (hide wins, so
 *  it isn't drawn). */
export function savedToFetch(starred: readonly number[], skip: (id: number) => boolean): number[] {
  const out: number[] = []
  const seen = new Set<number>()
  for (let i = starred.length - 1; i >= 0; i--) {
    const id = starred[i]
    if (seen.has(id)) continue
    seen.add(id)
    if (!skip(id)) out.push(id)
  }
  return out
}

/** Where a hidden story stood in one list (App's ``entries`` or
 *  ``forYou``) when it was hidden, for its Undo (``putBack``): the
 *  stories right after it and right before it, nearest first, each run
 *  up to and including the first one that wasn't hidden then, and its
 *  index there. The list a story goes back into is seldom the one it
 *  left: a poll drops hidden stories, and until one does, the list still
 *  holds stories hidden earlier (they are left out only where they are
 *  drawn). An index taken against one of those lists and used in another
 *  can land a place off; the neighbours' ids still say where the story
 *  was. The hidden ones in a run say where it stands among stories that
 *  may come back first (their own Undo). */
export type Place = { next: readonly number[]; prev: readonly number[]; at: number }

/** ``list[i]``'s ``Place``. ``isHidden`` names the stories hidden before
 *  this hide. Take it against the list with the stories still kept for
 *  their Undo back in their places (``putBack``), so a hidden story a
 *  poll has dropped still counts as a neighbour. */
export function placeOf(list: readonly Story[], i: number, isHidden: (id: number) => boolean): Place {
  const run = (step: 1 | -1) => {
    const out: number[] = []
    for (let j = i + step; j >= 0 && j < list.length; j += step) {
      out.push(list[j].id)
      if (!isHidden(list[j].id)) break
    }
    return out
  }
  return { next: run(1), prev: run(-1), at: i }
}

/** ``list`` with each of ``items`` back at its ``Place``, for a hide's
 *  Undo: just before the nearest story of its ``next`` run the list
 *  holds, else just after the nearest of its ``prev`` run, else at its
 *  index (clamped to the list). A neighbour that is one of ``items`` is
 *  waited for, so stories put back together (a bulk hide's Undo, Restore
 *  all) line up as they were. When the list holds none of a run, the
 *  run's last story, if it is a hidden story still kept for its own Undo
 *  (``kept`` gives its place in this list), lends its run on that side.
 *  ``list`` itself when every one is there already (a poll brought it
 *  back first); the input is never changed. */
export function putBack<E extends Story>(
  list: E[],
  items: readonly { item: E; place: Place }[],
  kept: (id: number) => Place | undefined = () => undefined,
): E[] {
  let out = list
  const held = new Set(list.map((e) => e.id))
  let left = items.filter(({ item }) => !held.has(item.id))
  const waiting = new Set(left.map(({ item }) => item.id))
  // The story in ``out`` that ``run`` stands for on ``side``: the nearest
  // one ``out`` holds, along the kept stories' runs; 'wait' when the
  // nearest is a story still to go back; null when none is there.
  const resolve = (run: readonly number[], side: 'next' | 'prev'): number | 'wait' | null => {
    const passed = new Set<number>()
    for (let ids = run; ids.length > 0; ) {
      for (const id of ids) {
        if (held.has(id)) return id
        if (waiting.has(id)) return 'wait'
      }
      const last = ids[ids.length - 1]
      if (passed.has(last)) return null
      passed.add(last)
      ids = kept(last)?.[side] ?? []
    }
    return null
  }
  const indexOf = (id: number) => out.findIndex((e) => e.id === id)
  const insert = (item: E, i: number) => {
    const at = Math.max(0, Math.min(i, out.length))
    out = [...out.slice(0, at), item, ...out.slice(at)]
    held.add(item.id)
    waiting.delete(item.id)
  }
  while (left.length > 0) {
    const stuck: typeof left = []
    for (const p of left) {
      if (held.has(p.item.id)) continue
      const next = resolve(p.place.next, 'next')
      const prev = next === null ? resolve(p.place.prev, 'prev') : null
      if (typeof next === 'number') insert(p.item, indexOf(next))
      else if (typeof prev === 'number') insert(p.item, indexOf(prev) + 1)
      else if (next === 'wait' || prev === 'wait') stuck.push(p)
      else insert(p.item, p.place.at)
    }
    if (stuck.length === left.length) {
      // Each waits for another (the list's order changed between their
      // hides): the first goes by its other side, else its index, and the
      // rest follow it.
      const [p] = stuck.splice(0, 1)
      const prev = resolve(p.place.prev, 'prev')
      insert(p.item, typeof prev === 'number' ? indexOf(prev) + 1 : p.place.at)
    }
    left = stuck
  }
  return out
}
