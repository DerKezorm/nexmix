/**
 * The media servers (`/api/v1/servers`, everybody signed in) and connecting Jellyfin (`/api/v1/jellyfin`, the operator).
 * No answer carries a token, the address of a server or one of Jellyfin's ids: a library and a user of Jellyfin are
 * named by a handle of nexmix's own.
 */
import { api } from './client'

export type Capabilities = {
  moods: boolean
  sound: boolean
  smart_playlists: boolean
  duplicates: boolean
  changes: 'poll' | 'events' | 'both'
  /** nexmix keeps its playlists there; else a playlist with that source lives in nexmix only. */
  playlists: boolean
}

export type MediaServer = {
  id: number
  kind: 'plex' | 'jellyfin'
  name: string
  active: boolean
  library: string | null
  ready: boolean
  capabilities: Capabilities
  /** The first one connected: the source of a new playlist unless another is chosen. */
  primary: boolean
}

export type Handle = { handle: string; title: string }
export type JellyfinUser = { handle: string; name: string; admin: boolean }

export type JellyfinServer = Omit<MediaServer, 'capabilities' | 'primary' | 'library'> & {
  via: 'quick' | 'key'
  library: Handle | null
  user: { handle: string; name: string } | null
  connected_at: string | null
}

export type QuickStart = { id: string; code: string; name: string; seconds: number }
export type Connected = { server: JellyfinServer; libraries: Handle[]; users: JellyfinUser[] }

export const serversApi = {
  list: () => api<{ servers: MediaServer[] }>('/api/v1/servers'),
  disconnect: (id: number) => api<void>(`/api/v1/servers/${id}`, { method: 'DELETE' }),
}

export const jellyfinApi = {
  list: () => api<{ servers: JellyfinServer[] }>('/api/v1/jellyfin'),
  quick: (url: string) => api<QuickStart>('/api/v1/jellyfin/quick', { method: 'POST', body: { url } }),
  quickState: (id: string) => api<{ done: boolean; left: number }>(`/api/v1/jellyfin/quick/${encodeURIComponent(id)}`),
  quickCancel: (id: string) => api<void>(`/api/v1/jellyfin/quick/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  connectQuick: (id: string) => api<Connected>('/api/v1/jellyfin/connect', { method: 'POST', body: { quick: id } }),
  connectByKey: (url: string, key: string) => api<Connected>('/api/v1/jellyfin/connect', { method: 'POST', body: { url, key } }),
  choices: (server: number) => api<{ libraries: Handle[]; users: JellyfinUser[] }>(`/api/v1/jellyfin/${server}/choices`),
  chooseLibrary: (server: number, handle: string) => api<JellyfinServer>(`/api/v1/jellyfin/${server}/library`, { method: 'PUT', body: { handle } }),
  chooseUser: (server: number, handle: string) => api<JellyfinServer>(`/api/v1/jellyfin/${server}/user`, { method: 'PUT', body: { handle } }),
  disconnect: (server: number) => api<void>(`/api/v1/jellyfin/${server}`, { method: 'DELETE' }),
}
