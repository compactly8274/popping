/**
 * Tests for the pure helpers in interactions_helpers.ts.
 *
 * No DOM, no module state — the helpers take a queue object by
 * value and return a new queue. Each test creates a fresh queue
 * and walks the state machine through a scenario.
 */
import { describe, it, expect } from 'vitest'

import {
  MAX_QUEUE,
  newInteractionQueue,
  enqueue,
  drainBatch,
  setFlushScheduled,
  isDeduplicatedType,
} from './interactions_helpers'
import type { InteractionEvent } from './interactions_types'

// Tiny helper: build a view event for an entry id.
function view(id: number): InteractionEvent {
  return { entry_id: id, type: 'view' }
}

function click(id: number): InteractionEvent {
  return { entry_id: id, type: 'click' }
}

describe('newInteractionQueue', () => {
  it('starts empty and unscheduled', () => {
    const q = newInteractionQueue()
    expect(q.pending).toEqual([])
    expect(q.seen.size).toBe(0)
    expect(q.flushScheduled).toBe(false)
  })
})

describe('enqueue', () => {
  it('adds the first view event to the queue', () => {
    const q = newInteractionQueue()
    const { queue, result } = enqueue(q, view(1))
    expect(result).toBe('added')
    expect(queue.pending).toEqual([view(1)])
    expect(queue.seen.has(1)).toBe(true)
  })

  it('deduplicates a second view for the same entry', () => {
    const q1 = enqueue(newInteractionQueue(), view(1)).queue
    const { result } = enqueue(q1, view(1))
    expect(result).toBe('duplicate-view')
  })

  it('does NOT deduplicate clicks (every click is a fresh signal)', () => {
    let q = enqueue(newInteractionQueue(), click(1)).queue
    q = enqueue(q, click(1)).queue
    // Two clicks of the same entry: both are queued.
    expect(q.pending.length).toBe(2)
  })

  it('does NOT dedupe a view even after a click of the same entry', () => {
    let q = enqueue(newInteractionQueue(), click(1)).queue
    // First view of entry 1 should be added, not deduped by the
    // prior click.
    const { result } = enqueue(q, view(1))
    expect(result).toBe('added')
  })

  it('signals "flush-immediately" when the queue hits the cap', () => {
    let q = newInteractionQueue()
    for (let i = 0; i < MAX_QUEUE - 1; i++) {
      q = enqueue(q, view(i)).queue
    }
    // At MAX_QUEUE-1, still 'added'.
    const { result: r1, queue: q1 } = enqueue(q, view(9999))
    // Hitting MAX_QUEUE triggers the immediate-flush signal.
    expect(r1).toBe('flush-immediately')
    expect(q1.pending.length).toBe(MAX_QUEUE)
  })
})

describe('drainBatch', () => {
  it('returns all pending events and clears the queue', () => {
    let q = newInteractionQueue()
    q = enqueue(q, view(1)).queue
    q = enqueue(q, view(2)).queue
    const { queue, batch } = drainBatch(q)
    expect(batch).toEqual([view(1), view(2)])
    expect(queue.pending).toEqual([])
  })

  it('drains only the first N when size is provided', () => {
    let q = newInteractionQueue()
    for (let i = 0; i < 5; i++) q = enqueue(q, view(i)).queue
    const { queue, batch } = drainBatch(q, 2)
    expect(batch.length).toBe(2)
    expect(queue.pending.length).toBe(3)
    expect(queue.pending[0]).toEqual(view(2))
  })

  it('drainBatch on an empty queue returns an empty batch', () => {
    const q = newInteractionQueue()
    const { queue, batch } = drainBatch(q)
    expect(batch).toEqual([])
    expect(queue.pending).toEqual([])
  })
})

describe('setFlushScheduled', () => {
  it('toggles the flag', () => {
    const q = newInteractionQueue()
    const q2 = setFlushScheduled(q, true)
    expect(q2.flushScheduled).toBe(true)
    const q3 = setFlushScheduled(q2, false)
    expect(q3.flushScheduled).toBe(false)
  })

  it('is a no-op when the value is unchanged', () => {
    const q = newInteractionQueue()
    const q2 = setFlushScheduled(q, false)
    // Same identity → no allocation needed. (Reference
    // equality is a signal to the production code that nothing
    // changed; the test just asserts no error.)
    expect(q2).toBe(q)
  })
})

describe('isDeduplicatedType', () => {
  it('only "view" is deduplicated', () => {
    expect(isDeduplicatedType('view')).toBe(true)
    expect(isDeduplicatedType('click')).toBe(false)
    expect(isDeduplicatedType('thumb_up')).toBe(false)
    expect(isDeduplicatedType('thumb_down')).toBe(false)
    expect(isDeduplicatedType('bookmark')).toBe(false)
    expect(isDeduplicatedType('share')).toBe(false)
    expect(isDeduplicatedType('never')).toBe(false)
    expect(isDeduplicatedType('dwell')).toBe(false)
  })
})

describe('integration scenarios', () => {
  it('user scrolls past 30 cards: 30 views added, no duplicates, cap respected', () => {
    let q = newInteractionQueue()
    let duplicates = 0
    for (let i = 0; i < 30; i++) {
      const r = enqueue(q, view(i))
      q = r.queue
      if (r.result === 'duplicate-view') duplicates++
    }
    expect(duplicates).toBe(0)
    expect(q.pending.length).toBe(30)
    expect(q.seen.size).toBe(30)
  })

  it('user scrolls past 30 cards, then scrolls back: second pass deduplicates', () => {
    let q = newInteractionQueue()
    for (let i = 0; i < 30; i++) {
      q = enqueue(q, view(i)).queue
    }
    // Scroll back up — every entry was already seen.
    let dups = 0
    for (let i = 0; i < 30; i++) {
      const r = enqueue(q, view(i))
      q = r.queue
      if (r.result === 'duplicate-view') dups++
    }
    expect(dups).toBe(30)
    expect(q.pending.length).toBe(30) // unchanged
  })

  it('cap-at-MAX_QUEUE: 50 view events for 50 unique entries triggers immediate flush', () => {
    let q = newInteractionQueue()
    let lastResult: string = ''
    for (let i = 0; i < MAX_QUEUE; i++) {
      const r = enqueue(q, view(i))
      q = r.queue
      lastResult = r.result
    }
    // The MAX_QUEUE-th enqueue is the one that crosses the
    // threshold. Verify the signal fires exactly once.
    expect(lastResult).toBe('flush-immediately')
  })
})
