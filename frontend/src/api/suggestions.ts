/**
 * Suggestions and listening in: `/api/v1/suggestions` (the two voices of the server, "not again", and the suggestions
 * themselves where the catalogue is not in the browser) and `/api/v1/preview` (whether listening in is on, the sound of
 * a track). Songs are named by the id of the version shown for them.
 */
import type { TrackHit } from '../lib/catalogSearch'
import type { Mode, Suggestion } from '../lib/suggest'
import { api } from './client'

export type Together = { songs: { song: number; count: number }[]; playlists: number }
export type Sonic = { state: 'unknown' | 'off' | 'running' | 'on'; songs: { song: number; distance: number }[] }
export type PreviewState = { allowed: boolean; seconds: number; per_hour: number; left: number; retry_after: number | null }

const strings = (values: number[]) => values.map(String)

export const suggestionsApi = {
  together: (songs: number[]) => api<Together>('/api/v1/suggestions/together', { query: { songs: strings(songs) } }),
  sonic: (tracks: number[]) => api<Sonic>('/api/v1/suggestions/sonic', { query: { track: strings(tracks) } }),
  hidden: () => api<{ songs: number[] }>('/api/v1/suggestions/hidden'),
  hide: (track: number) => api<{ songs: number[] }>('/api/v1/suggestions/hidden', { method: 'POST', body: { track } }),
  unhide: (track: number) => api<{ songs: number[] }>(`/api/v1/suggestions/hidden/${track}`, { method: 'DELETE' }),
  /** Worked out on the server: the same rules, its own voices and hidden songs. */
  suggest: (seed: number[], mode: Mode, playlist?: number) =>
    api<{ tracks: Suggestion[]; seed: number[]; took_ms: number }>('/api/v1/suggestions', { query: { seed: strings(seed), mode, playlist } }),
  build: (options: { moods: string[]; decades: number[]; minutes: number; hits: number; seed: number; playlist?: number }) =>
    api<{ tracks: TrackHit[]; duration: number }>('/api/v1/suggestions/mood', {
      query: { moods: options.moods, decades: strings(options.decades), minutes: options.minutes, hits: options.hits, seed: options.seed, playlist: options.playlist },
    }),
}

export const previewApi = {
  state: () => api<PreviewState>('/api/v1/preview'),
  /** The address of a track's preview, for an audio element (the session cookie goes with it). */
  sound: (track: number) => `/api/v1/preview/${track}`,
}
