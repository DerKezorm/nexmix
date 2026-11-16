/**
 * Plex and the catalogue, under `/api/v1/plex` (the operator) and `/api/v1/catalog` (everybody signed in). No answer
 * carries a token: the browser never sees one.
 */
import { api } from './client'

export type PlexLibrary = { key: string; title: string }

export type PlexState =
  | { connected: false }
  | { connected: true; via: 'plextv' | 'manual'; server?: number; server_name: string; owned: boolean; library: PlexLibrary | null; connected_at: string }

export type PlexServerChoice = { index: number; name: string; owned: boolean; libraries: PlexLibrary[] }

export type PinStart = { id: string; url: string; seconds: number }
export type PinState = { done: false; left: number } | { done: true; offered: number; servers: PlexServerChoice[] }

export type CatalogRun = {
  kind: 'full' | 'partial'
  reason: string
  state: 'running' | 'done' | 'failed' | 'stopped'
  error: string | null
  step: string
  /** Thousandths of the whole run. */
  progress: number
  started_at: string | null
  finished_at: string | null
  artists: number
  albums: number
  tracks: number
  songs: number
  changed: number
  removed: number
}

export type Analysis = { state: 'unknown' | 'off' | 'running' | 'on'; percent: number | null }

export type CatalogStatus = {
  /** The kind of the server whose catalogue this is (null: none connected). */
  kind?: 'plex' | 'jellyfin' | 'music_assistant' | null
  connected: boolean
  library: boolean
  library_title: string
  ready: boolean
  counts: { artists: number; albums: number; tracks: number; songs: number; moods: number }
  synced_at: string | null
  full_at: string | null
  running: CatalogRun | null
  queued: 'full' | 'partial' | null
  last: CatalogRun | null
  analysis: Analysis
  nightly: string
  /** Where the interface searches: the whole catalogue in the browser, or for a large one on the server. */
  search?: 'browser' | 'server'
}

/** The playlists' sync with Plex, for the Plex card. */
export type PlaylistSync = {
  active: boolean
  /** Every playlist kept on the server: the ones it is the source of and the mirrors there. */
  playlists: number
  /** The playlists whose source is this server. */
  sources?: number
  /** Mirrors on this server of playlists whose source is another server. */
  mirrors?: number
  synced: number
  pending: number
  waiting: number
  error: number
  smart: number
  /** At most this many minutes for an order changed in Plex to arrive here (more playlists, longer). */
  order_minutes?: number
  polled_at: string | null
  poll_error: string | null
  /** Several playlists missing from the server's list at once: none went to the trash, nexmix keeps looking. */
  missing?: number
  /** Mirrors on the server emptied there, each waiting for an answer at its playlist (none taken over). */
  emptied?: number
}

export const plexApi = {
  state: () => api<PlexState>('/api/v1/plex'),
  begin: () => api<PinStart>('/api/v1/plex/pin', { method: 'POST' }),
  poll: (id: string) => api<PinState>(`/api/v1/plex/pin/${encodeURIComponent(id)}`),
  cancel: (id: string) => api<void>(`/api/v1/plex/pin/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  /** `alongside`: a server more beside the ones connected ("add a server"); its number comes back as `added`. */
  connectChosen: (pinId: string, server: number, alongside = false) =>
    api<PlexState & { libraries: PlexLibrary[]; added?: number }>('/api/v1/plex/connect', { method: 'POST', body: alongside ? { pin_id: pinId, server, alongside } : { pin_id: pinId, server } }),
  connectByHand: (url: string, token: string, alongside = false) =>
    api<PlexState & { libraries: PlexLibrary[]; added?: number }>('/api/v1/plex/connect', { method: 'POST', body: alongside ? { url, token, alongside } : { url, token } }),
  /** `server`: a Plex server connected beside the one of the Plex card. */
  libraries: (server?: number) => api<{ libraries: PlexLibrary[] }>('/api/v1/plex/libraries', { query: server ? { server } : undefined }),
  chooseLibrary: (key: string, server?: number) => api<PlexState>('/api/v1/plex/library', { method: 'PUT', body: { key }, query: server ? { server } : undefined }),
  disconnect: () => api<void>('/api/v1/plex', { method: 'DELETE' }),
  playlists: () => api<PlaylistSync>('/api/v1/plex/playlists'),
  syncPlaylists: () => api<PlaylistSync>('/api/v1/plex/playlists/sync', { method: 'POST' }),
}

export const catalogApi = {
  /** `server`: the catalogue of that media server (a playlist's); none: the first one connected. */
  status: (server?: number | null) => api<CatalogStatus>('/api/v1/catalog/status', { query: server ? { server } : undefined }),
  sync: (server?: number | null) => api<{ started: boolean }>('/api/v1/catalog/sync', { method: 'POST', query: server ? { server } : undefined }),
  cover: (album: number, size: 80 | 200 | 400 = 200) => `/api/v1/catalog/cover/${album}?s=${size}`,
}

/** Whether a run is under way or waiting: then the page asks again every few seconds. */
export function busy(status: CatalogStatus | null): boolean {
  return Boolean(status && (status.running || status.queued))
}
