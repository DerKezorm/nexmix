/**
 * Suggestions for a guest: the editor's rules (`suggest.ts`, the same as the server's), asked from this one playlist
 * only. The seed is the last five tracks of the list, every song of the list stays out, in the kind "fitting". A guest
 * sees no other playlist, so there is no "often together" (it would count playlists of the household), no "sounds
 * alike" and no songs hidden by somebody.
 */
import type { SuggestAsk } from './localCatalog'

/** How many of the last tracks of the list say what fits. */
export const SEED = 5
/** How many suggestions a guest gets, as drawn. */
export const LIMIT = 15

export function guestAsk(items: { track: number; song: number | null; missing?: boolean }[]): SuggestAsk {
  const present = items.filter((entry) => !entry.missing)
  return {
    seed: present.slice(-SEED).map((entry) => entry.track),
    mode: 'fit',
    exclude: [...new Set(items.map((entry) => entry.song).filter((song): song is number => song !== null))],
    hidden: [],
    together: [],
    sound: [],
    limit: LIMIT,
  }
}
