/**
 * A playlist on more servers than its source (`/api/v1/playlists/{id}/mirrors`, `/move`): its mirrors, the preview
 * before copying or moving, the copy (a mirror kept in step, or once), the move, an entry's track on a mirror chosen by
 * hand, and the tracks only in a mirror. "Take all over" for a whole server (`/api/v1/servers/{id}/transfer`, the
 * operator). Tracks are nexmix's numbers; no answer names a server's own ids.
 */
import { api } from './client'
import type { Change } from './playlists'

export type MirrorState = 'synced' | 'pending' | 'waiting' | 'error' | 'gone' | 'off'

export type Extra = { id: number; title: string; artist: string; album: string; duration: number }

export type Mirror = {
  server: number
  kind: 'plex' | 'jellyfin'
  name: string
  state: MirrorState
  error: string | null
  synced_at: string | null
  /** Entries without a track on that server. */
  absent: number
  /** Tracks put in there that the source has no match for. */
  extras: Extra[]
  /** Emptied on its server, waiting for "empty it here too" or "keep" (nothing taken over meanwhile). */
  emptied: { at: string | null; count: number } | null
}

export type FoundTrack = { track: number; title: string; artist: string; album: string; duration: number }

/** An entry not found, or found by an unsure step (`song`, `title`), or left out (`skip`). */
export type PreviewRow = {
  track: number
  title: string
  artist: string
  album: string
  duration: number
  how: 'none' | 'skip' | 'song' | 'title' | string
  found: FoundTrack | null
  /** How many entries (of several playlists) have this track. */
  count: number
}

export type Preview = {
  server: number
  entries: number
  found: number
  unsure: number
  missing: number
  rows: PreviewRow[]
  more: number
  /** Whether an entry found by an unsure step counts as found (copying), or is only offered until chosen (moving,
   * taking all over). */
  preselect?: boolean
  /** Take all over: how many playlists, and how many smart ones stay where they are. */
  playlists?: number
  smart?: number
}

/** A choice by hand, by an entry's track: the target's track, or null to leave it out. */
export type Choices = Record<number, number | null>

export type Transfer = {
  id: number
  server: number
  mode: 'mirror' | 'move'
  old: 'keep' | 'delete'
  state: 'running' | 'done' | 'stopped' | 'failed'
  total: number
  done: number
  skipped: number
  stopping: boolean
  error: string | null
  disconnect: number | null
  started_at: string
  finished_at: string | null
}

const base = (id: number) => `/api/v1/playlists/${id}`

export const mirrorsApi = {
  list: (id: number) => api<{ mirrors: Mirror[] }>(`${base(id)}/mirrors`),
  preview: (id: number, server: number, choices: Choices = {}, move = false) =>
    api<Preview>(`${base(id)}/mirrors/preview`, { method: 'POST', body: { server, choices, ...(move ? { move } : {}) } }),
  copy: (id: number, server: number, choices: Choices, once: boolean) =>
    api<{ playlist: number; mirrors: Mirror[] }>(`${base(id)}/mirrors`, { method: 'POST', body: { server, choices, once } }),
  move: (id: number, server: number, choices: Choices, old: 'keep' | 'delete') =>
    api<{ playlist: number; server: number; found: number; entries: number; mirrors: Mirror[] }>(`${base(id)}/move`, { method: 'POST', body: { server, choices, old } }),
  forget: (id: number, server: number) => api<void>(`${base(id)}/mirrors/${server}`, { method: 'DELETE' }),
  remake: (id: number, server: number) => api<{ mirrors: Mirror[] }>(`${base(id)}/mirrors/${server}/remake`, { method: 'POST' }),
  choose: (id: number, server: number, entry: number, track: number | null) =>
    api<{ mirrors: Mirror[] }>(`${base(id)}/mirrors/${server}/entries/${entry}`, { method: 'PUT', body: { track } }),
  takeExtra: (id: number, server: number, extra: number, track: number) =>
    api<Change>(`${base(id)}/mirrors/${server}/extras/${extra}/take`, { method: 'POST', body: { track } }),
  dropExtra: (id: number, server: number, extra: number) => api<void>(`${base(id)}/mirrors/${server}/extras/${extra}`, { method: 'DELETE' }),
  /** The answer to a mirror emptied on its server: empty the playlist everywhere too, or keep it (filled again). */
  emptied: (id: number, server: number, empty: boolean) => api<{ mirrors: Mirror[] }>(`${base(id)}/mirrors/${server}/emptied`, { method: 'POST', body: { empty } }),
}

/** The preview of "take all over", worked out in the background: how far it got, and its result once `done`. */
export type Looking = {
  state: 'running' | 'done' | 'stopped' | 'failed'
  done: number
  total: number
  error: string | null
  result: Preview | null
}

export const transferApi = {
  look: (server: number, mode: 'mirror' | 'move', source?: number | null) =>
    api<{ preview: Looking }>(`/api/v1/servers/${server}/transfer/preview`, { method: 'POST', body: { mode, ...(source ? { source } : {}) } }),
  looking: (server: number) => api<{ preview: Looking | null }>(`/api/v1/servers/${server}/transfer/preview`),
  start: (server: number, body: { mode: 'mirror' | 'move'; old: 'keep' | 'delete'; choices: Choices; source?: number | null; disconnect?: number | null }) =>
    api<{ transfer: Transfer }>(`/api/v1/servers/${server}/transfer`, { method: 'POST', body }),
  state: (server: number) => api<{ transfer: Transfer | null }>(`/api/v1/servers/${server}/transfer`),
  stop: (server: number) => api<{ transfer: Transfer }>(`/api/v1/servers/${server}/transfer`, { method: 'DELETE' }),
}
