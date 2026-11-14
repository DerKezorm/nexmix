/**
 * Guests: the invitation of a playlist for the people who invite (`/api/v1/playlists/{id}/invite`), the link a guest
 * opens (`/api/v1/join/{code}`), and what a guest reaches with its cookie (`/api/v1/guest`).
 */
import { api } from './client'

export type InviteRights = {
  may_add: boolean
  may_sort: boolean
  max: number
  hours: number
  preview: boolean
}

export type Invite = InviteRights & {
  code: string
  link: string
  /** The QR picture of the link, drawn on the server. */
  qr: string
  state: 'waiting' | 'running'
  started_at: string | null
  expires_at: string | null
}

export type InviteGuest = {
  id: number
  name: string
  count: number
  joined_at: string
}

/** Who can open the address the link starts with: phones everywhere, only in the home network, only this computer. */
export type Reach = 'internet' | 'home' | 'machine'

export type InviteView = {
  invite: Invite | null
  guests: InviteGuest[]
  defaults: InviteRights
  address: { url: string; configured: boolean; reach: Reach }
  /** Listening in is on on the server (the operator's switch). */
  listening: boolean
}

const base = (id: number) => `/api/v1/playlists/${id}/invite`

export const invitesApi = {
  read: (id: number) => api<InviteView>(base(id)),
  prepare: (id: number) => api<InviteView>(base(id), { method: 'POST' }),
  change: (id: number, values: Partial<InviteRights> & { start?: boolean }) => api<InviteView>(base(id), { method: 'PUT', body: values }),
  end: (id: number) => api<void>(base(id), { method: 'DELETE' }),
  removeGuest: (id: number, guest: number) => api<void>(`${base(id)}/guests/${guest}`, { method: 'DELETE' }),
}

export type Door = {
  playlist: { name: string; count: number; duration: number; covers: number[] }
  inviter: string
  joined: boolean
  name: string | null
}

export type GuestEntry = {
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
  /** The name at the entry. */
  by: string
  mine: boolean
}

export type GuestState = {
  me: { id: number; name: string; count: number }
  invite: {
    may_add: boolean
    may_sort: boolean
    max: number
    preview: boolean
    expires_at: string | null
  }
  playlist: {
    name: string
    inviter: string
    version: number
    count: number
    duration: number
    covers: number[]
    items: GuestEntry[]
  }
  catalog: { ready: boolean; search: 'browser' | 'server' }
}

export type GuestChange = {
  op: 'add' | 'remove' | 'restore' | 'move' | 'none'
  version?: number
  skipped?: number[]
  removed?: number[]
  state: GuestState
}

const door = (code: string) => `/api/v1/join/${encodeURIComponent(code)}`

export const joinApi = {
  look: (code: string) => api<Door>(door(code)),
  join: (code: string, name: string) => api<{ name: string }>(door(code), { method: 'POST', body: { name } }),
  cover: (code: string) => (album: number, size: number) => `${door(code)}/cover/${album}?s=${size}`,
}

export const guestApi = {
  state: () => api<GuestState>('/api/v1/guest'),
  add: (tracks: number[]) =>
    api<GuestChange>('/api/v1/guest/items', {
      method: 'POST',
      body: { tracks },
    }),
  remove: (entries: number[]) =>
    api<GuestChange>('/api/v1/guest/items/remove', {
      method: 'POST',
      body: { entries },
    }),
  bringBack: (entries: number[], after: number | null) =>
    api<GuestChange>('/api/v1/guest/items/restore', {
      method: 'POST',
      body: { entries, after },
    }),
  move: (entry: number, after: number | null) =>
    api<GuestChange>('/api/v1/guest/items/move', {
      method: 'POST',
      body: { entry, after },
    }),
  leave: () => api<void>('/api/v1/guest/leave', { method: 'POST' }),
  events: '/api/v1/guest/events',
  catalog: '/api/v1/guest/catalog',
  search: '/api/v1/guest/catalog/search',
  /** Suggestions worked out on the server, for a catalogue too large for the browser. */
  suggestions: '/api/v1/guest/suggestions',
  cover: (album: number, size: number) => `/api/v1/guest/cover/${album}?s=${size}`,
}
