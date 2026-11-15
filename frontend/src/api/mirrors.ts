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
  preview: (id: number, server: number, choices: Choices = {}) => api<Preview>(`${base(id)}/mirrors/preview`, { method: 'POST', body: { server, choices } }),
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
}

export const transferApi = {
  preview: (server: number, mode: 'mirror' | 'move', choices: Choices = {}, source?: number | null) =>
    api<Preview>(`/api/v1/servers/${server}/transfer/preview`, { method: 'POST', body: { mode, choices, ...(source ? { source } : {}) } }),
  start: (server: number, body: { mode: 'mirror' | 'move'; old: 'keep' | 'delete'; choices: Choices; source?: number | null; disconnect?: number | null }) =>
    api<{ transfer: Transfer }>(`/api/v1/servers/${server}/transfer`, { method: 'POST', body }),
  state: (server: number) => api<{ transfer: Transfer | null }>(`/api/v1/servers/${server}/transfer`),
  stop: (server: number) => api<{ transfer: Transfer }>(`/api/v1/servers/${server}/transfer`, { method: 'DELETE' }),
}
