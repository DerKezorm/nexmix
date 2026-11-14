/**
 * Where the editor searches: the catalogue in this browser (a worker, `localCatalog.ts`), or for a library past the
 * server's limit the server (`GET /api/v1/catalog/search`). The server decides (`search` in the catalogue's status);
 * both ways answer in the same form, so the page does not know which one it asked.
 */
import { useEffect, useState } from 'react'

import { api, ApiError } from '../api/client'
import { suggestionsApi } from '../api/suggestions'
import type { AlbumDetail, AlbumResult, ArtistDetail, ArtistResult, Facets, Filters, TrackHit, TrackResult } from './catalogSearch'
import { answer, CatalogError, LocalCatalog, type Ask, type BuildAsk, type Loaded, type SuggestAsk } from './localCatalog'
import type { Suggestion } from './suggest'

export type Timed<T> = T & { ms: number }

export interface CatalogSource {
  readonly mode: 'browser' | 'server'
  /** The filters there are; for the browser the catalogue is fetched (again, quietly) first. `changed`: a new state. */
  load(): Promise<{ facets: Facets; changed: boolean }>
  tracks(query: string, filters: Filters): Promise<Timed<TrackResult>>
  albums(query: string): Promise<Timed<AlbumResult>>
  artists(query: string): Promise<Timed<ArtistResult>>
  album(id: number): Promise<AlbumDetail | null>
  artist(id: number): Promise<ArtistDetail | null>
  /** Suggestions for the seed tracks; the server leaves out the playlist's songs and its own hidden ones itself. */
  suggest(ask: SuggestAsk & { playlist?: number }): Promise<Timed<{ tracks: Suggestion[] }>>
  /** Filling by mood. */
  build(ask: BuildAsk & { playlist?: number }): Promise<{ tracks: TrackHit[]; duration: number }>
  dispose(): void
}

type Asker = (ask: Ask) => Promise<{ result: unknown; ms: number }>

function workerAsker(): { ask: Asker; stop: () => void } | null {
  if (typeof Worker === 'undefined') return null
  let worker: Worker
  try {
    worker = new Worker(new URL('./catalog.worker.ts', import.meta.url), { type: 'module' })
  } catch {
    return null
  }
  let next = 0
  const waiting = new Map<number, { resolve: (value: { result: unknown; ms: number }) => void; reject: (error: unknown) => void }>()
  worker.onmessage = (event: MessageEvent<{ id: number; result?: unknown; ms?: number; error?: string }>) => {
    const found = waiting.get(event.data.id)
    if (!found) return
    waiting.delete(event.data.id)
    if (event.data.error) found.reject(new CatalogError(event.data.error))
    else found.resolve({ result: event.data.result, ms: event.data.ms ?? 0 })
  }
  worker.onerror = () => {
    for (const found of waiting.values()) found.reject(new CatalogError('internal_error'))
    waiting.clear()
  }
  return {
    ask: (ask) =>
      new Promise((resolve, reject) => {
        next += 1
        waiting.set(next, { resolve, reject })
        worker.postMessage({ id: next, ask })
      }),
    stop: () => worker.terminate(),
  }
}

/** `url`: where the catalogue comes from (a guest has its own). */
export function browserSource(url = '/api/v1/catalog'): CatalogSource {
  const viaWorker = workerAsker()
  const inline = new LocalCatalog()
  const ask: Asker = viaWorker ? viaWorker.ask : (question) => answer(inline, question)
  const timed = async <T,>(question: Ask) => {
    const { result, ms } = await ask(question)
    return { ...(result as T), ms }
  }
  return {
    mode: 'browser',
    load: async () => {
      const { result } = await ask({ op: 'load', url })
      const loaded = result as Loaded
      if (loaded.changed) console.debug(`catalog: ${loaded.tracks} tracks, ${loaded.bytes} bytes, fetched in ${Math.round(loaded.fetchMs)} ms, prepared in ${Math.round(loaded.prepareMs)} ms`)
      return { facets: loaded.facets, changed: loaded.changed }
    },
    tracks: (query, filters) => timed<TrackResult>({ op: 'tracks', query, filters }),
    albums: (query) => timed<AlbumResult>({ op: 'albums', query }),
    artists: (query) => timed<ArtistResult>({ op: 'artists', query }),
    album: async (id) => (await ask({ op: 'album', id })).result as AlbumDetail | null,
    artist: async (id) => (await ask({ op: 'artist', id })).result as ArtistDetail | null,
    // The playlist is only the server's business (it reads it itself); here its songs come as `exclude`.
    suggest: ({ playlist: _playlist, ...question }) => timed<{ tracks: Suggestion[] }>({ op: 'suggest', ask: question }),
    build: async ({ playlist: _playlist, ...question }) => (await ask({ op: 'build', ask: question })).result as { tracks: TrackHit[]; duration: number },
    dispose: () => viaWorker?.stop(),
  }
}

export function serverSource(): CatalogSource {
  const search = async <T,>(query: Record<string, string | number | undefined>) => {
    const started = performance.now()
    const found = await api<T & { took_ms: number }>('/api/v1/catalog/search', { query })
    return { ...found, ms: performance.now() - started }
  }
  const orNull = async <T,>(path: string): Promise<T | null> => {
    try {
      return await api<T>(path)
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null
      throw error
    }
  }
  return {
    mode: 'server',
    load: async () => ({ facets: await api<Facets>('/api/v1/catalog/facets'), changed: false }),
    tracks: (query, filters) => search<TrackResult>({ q: query, kind: 'tracks', mood: filters.mood || undefined, decade: filters.decade || undefined, genre: filters.genre || undefined }),
    albums: (query) => search<AlbumResult>({ q: query, kind: 'albums' }),
    artists: (query) => search<ArtistResult>({ q: query, kind: 'artists' }),
    album: (id) => orNull<AlbumDetail>(`/api/v1/catalog/albums/${id}`),
    artist: (id) => orNull<ArtistDetail>(`/api/v1/catalog/artists/${id}`),
    suggest: async (question) => {
      const started = performance.now()
      const found = await suggestionsApi.suggest(question.seed, question.mode, question.playlist)
      return { tracks: found.tracks, ms: performance.now() - started }
    },
    build: (question) => suggestionsApi.build(question),
    dispose: () => undefined,
  }
}

/** How often an open editor asks quietly whether the catalogue changed (and whenever the tab comes back). */
export const REFRESH_MS = 5 * 60 * 1000

/** The source for the server's choice, loaded; `version` counts the states of the catalogue taken in, so the page
 * searches again when a new one came. */
export function useCatalogSource(mode: 'browser' | 'server' | null) {
  const [source, setSource] = useState<CatalogSource | null>(null)
  const [facets, setFacets] = useState<Facets | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [version, setVersion] = useState(0)
  useEffect(() => {
    if (mode === null) return
    // Made here, not during drawing: a worker belongs to the effect that starts it and ends with it.
    const source = mode === 'server' ? serverSource() : browserSource()
    setSource(source)
    setFacets(null)
    let gone = false
    let loading = false
    const load = async () => {
      if (loading) return
      loading = true
      try {
        const loaded = await source.load()
        if (gone) return
        setFacets(loaded.facets)
        setFailed(null)
        if (loaded.changed) setVersion((value) => value + 1)
      } catch (error) {
        if (!gone) setFailed(error instanceof CatalogError || error instanceof ApiError ? error.code : 'internal_error')
      } finally {
        loading = false
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), REFRESH_MS)
    const back = () => {
      if (document.visibilityState === 'visible') void load()
    }
    document.addEventListener('visibilitychange', back)
    return () => {
      gone = true
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', back)
      source.dispose()
    }
  }, [mode])
  return { source, facets, failed, version }
}
