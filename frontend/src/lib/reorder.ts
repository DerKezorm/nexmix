/** Sorting the entries of a playlist by dragging: where a dragged entry lands. */
import type { Entry } from '../api/playlists'

/** How long a finger holds a row before it moves, and how far a mouse moves before a press becomes a drag. */
export const HOLD_MS = 400
export const SLOP = 6

/** Where an entry dragged from `from` lands when dropped before row `over` (`over` = the number of rows: at the end):
 * the entry it then follows, null at the start; undefined when it stays where it is. */
export function dropAfter(items: Entry[], from: number, over: number): number | null | undefined {
  if (over === from || over === from + 1) return undefined
  const rest = items.filter((_entry, index) => index !== from)
  const at = over > from ? over - 1 : over
  return at === 0 ? null : rest[at - 1].id
}

