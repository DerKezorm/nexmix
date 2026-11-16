/**
 * Playlists under `/api/v1/playlists`. Every change is an operation about entry ids; its answer and the event the other
 * browsers get carry the new version and the whole order (`Change`), so a page applies either the same way.
 */
import { api } from './client'
import type { Mirror } from './mirrors'

export type Person = { id: number | null; name: string; display_name: string; avatar: string | null }
export type Access = 'owner' | 'operator' | 'edit' | 'read'

export type Summary = { count: number; duration: number; covers: number[]; people: Person[]; contributors: number; moods?: string[] }

/** How the copy of a playlist on its media server (Plex or Jellyfin) stands. */
export type PlexSync = {
  state: 'synced' | 'pending' | 'waiting' | 'error' | 'off'
  /** The kind of the playlist's server: `plex`, `jellyfin` or `music_assistant` (null: none). */
  kind?: string | null
  /** The code of the last failure, while it waits for the server or failed. */
  error: string | null
  smart: boolean
  /** All entries of a smart playlist (the page shows the first ones). */
  total: number | null
  synced_at: string | null
  /** Deleted in Plex, therefore in the trash. */
  gone_in_plex: boolean
  /** Sorted in Plex and here at the same time: this order stands, Plex's can be taken. */
  note: { at: string; count: number } | null
  /** The server lost many entries at once (or became empty): nexmix kept them, they go back after the catalogue sync. */
  loss?: { at: string; count: number } | null
  /** Tracks added in Music Assistant while nexmix wrote there by place: they may be gone (which, nobody knows). */
  concurrent?: { at: string | null; count: number } | null
}

export type Card = Summary & {
  id: number
  name: string
  description: string
  owner: Person | null
  access: Access
  shares: number
  version: number
  created_at: string
  updated_at: string
  deleted_at: string | null
  purge_at: string | null
  /** Whole days still in the trash. */
  days_left: number | null
  /** The guests of a running invitation; null without one. */
  guests?: number | null
  smart: boolean
  plex: PlexSync
  /** Hidden from this account's overview (nothing else changes). */
  hidden?: boolean
  /** The media server the playlist takes its tracks from (its catalogue is searched); null: none connected yet. */
  server?: number | null
  /** Its mirrors on other servers, kept in step both ways. */
  mirrors?: Mirror[]
}

export type Entry = {
  id: number
  track: number
  title: string
  artist: string
  album: string
  album_id: number | null
  cover: boolean
  year: number | null
  duration: number
  song: number | null
  missing: boolean
  /** Put in through Plex (Plexamp, Plex Web); `added_at` is when nexmix saw it. */
  from_plex?: boolean
  /** Why the entry is in nexmix only: Plex refused its track, or the connected server has no such track. */
  plex_hint?: 'refused' | 'elsewhere' | 'missing' | null
  added_by: Person | null
  added_by_name: string
  added_at: string
  /** The servers of the playlist's mirrors that have no track for this entry. */
  absent?: number[]
}

export type Playlist = Card & { moods: string[]; items: Entry[] }

/** What a change did: the new version, the entries added or brought back, the whole order, the new summary. */
export type Change = {
  version: number
  op: 'add' | 'remove' | 'restore' | 'move' | 'sync' | 'none'
  by?: Person | null
  entries: Entry[]
  order?: number[]
  summary?: Summary
  skipped?: number[]
  removed?: number[]
  moved?: boolean
  /** A change that came from Plex carries the name too (it may have been renamed there). */
  name?: string
}

export type Shares = { shares: { person: Person; access: 'read' | 'edit' }[]; people: Person[] }

const base = (id: number) => `/api/v1/playlists/${id}`

export const playlistsApi = {
  list: (trash = false) =>
    api<{ playlists: Card[]; trash_days: number; show_smart_playlists?: boolean }>('/api/v1/playlists', { query: trash ? { trash: true } : undefined }),
  /** `server`: the media server it takes its tracks from; none: the first one connected. */
  create: (name: string, server?: number | null) => api<Playlist>('/api/v1/playlists', { method: 'POST', body: server ? { name, server } : { name } }),
  read: (id: number) => api<Playlist>(base(id)),
  rename: (id: number, name: string) => api<{ version: number; name: string }>(base(id), { method: 'PUT', body: { name } }),
  trash: (id: number) => api<Card>(base(id), { method: 'DELETE' }),
  purge: (id: number) => api<{ deleted: boolean }>(base(id), { method: 'DELETE', query: { purge: true } }),
  restore: (id: number) => api<Playlist>(`${base(id)}/restore`, { method: 'POST' }),
  add: (id: number, tracks: number[]) => api<Change>(`${base(id)}/items`, { method: 'POST', body: { tracks } }),
  remove: (id: number, entries: number[]) => api<Change>(`${base(id)}/items/remove`, { method: 'POST', body: { entries } }),
  bringBack: (id: number, entries: number[], after: number | null) => api<Change>(`${base(id)}/items/restore`, { method: 'POST', body: { entries, after } }),
  move: (id: number, entry: number, after: number | null) => api<Change>(`${base(id)}/items/move`, { method: 'POST', body: { entry, after } }),
  shares: (id: number) => api<Shares>(`${base(id)}/shares`),
  share: (id: number, account: number, access: 'read' | 'edit') => api<Shares>(`${base(id)}/shares/${account}`, { method: 'PUT', body: { access } }),
  unshare: (id: number, account: number) => api<Shares>(`${base(id)}/shares/${account}`, { method: 'DELETE' }),
  events: (id: number) => `${base(id)}/events`,
  sync: (id: number) => api<PlexSync>(`${base(id)}/sync`, { method: 'POST' }),
  takePlexOrder: (id: number) => api<Change>(`${base(id)}/plex-order`, { method: 'POST' }),
  keepOrder: (id: number) => api<PlexSync>(`${base(id)}/plex-order`, { method: 'DELETE' }),
  /** Take over what the server lost at once: the entries kept for it go here too. */
  takeLoss: (id: number) => api<Change>(`${base(id)}/missing/remove`, { method: 'POST' }),
  /** Keep them: the note goes, they go back to the server after the next catalogue sync. */
  keepLoss: (id: number) => api<PlexSync>(`${base(id)}/missing`, { method: 'DELETE' }),
  seenConcurrent: (id: number) => api<PlexSync>(`${base(id)}/concurrent`, { method: 'DELETE' }),
  /** Hide from the own overview, or show again: only this account's view. */
  hide: (id: number) => api<{ id: number; hidden: boolean }>(`${base(id)}/hidden`, { method: 'PUT' }),
  show: (id: number) => api<{ id: number; hidden: boolean }>(`${base(id)}/hidden`, { method: 'DELETE' }),
}

/**
 * A change applied to the playlist the page holds: only the next version (the one after the page's), else null, and
 * the page loads the playlist anew (it missed one). A change it has already (its own answer came after the event, or
 * the other way round) is left as it is.
 */
export function applyChange(playlist: Playlist, change: Change): Playlist | null | 'same' {
  if (change.op === 'none' || change.version <= playlist.version) return 'same'
  if (change.version !== playlist.version + 1 || !change.order) return null
  const known = new Map(playlist.items.map((entry) => [entry.id, entry]))
  for (const entry of change.entries) known.set(entry.id, entry)
  const items: Entry[] = []
  for (const id of change.order) {
    const entry = known.get(id)
    if (!entry) return null
    items.push(entry)
  }
  const name = typeof change.name === 'string' ? change.name : playlist.name
  return { ...playlist, ...(change.summary ?? {}), version: change.version, items, name }
}
