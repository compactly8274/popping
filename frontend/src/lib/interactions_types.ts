/**
 * Type-only module for the interaction-event queue. Extracted
 * from interactions.ts so the pure helpers in
 * interactions_helpers.ts can import the type without pulling
 * in the DOM-touching side effects of interactions.ts (which
 * adds pagehide/visibilitychange listeners at module load).
 *
 * No runtime exports — TypeScript strips this whole module at
 * compile time.
 */

export type InteractionType =
  | 'view'
  | 'click'
  | 'dwell'
  | 'thumb_up'
  | 'thumb_down'
  | 'bookmark'
  | 'share'
  | 'never'

export interface InteractionEvent {
  entry_id: number
  type: InteractionType
  value?: number
}
