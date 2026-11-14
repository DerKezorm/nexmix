/**
 * The whole catalogue in this browser: fetched once (`GET /api/v1/catalog`, packed, named by its ETag), prepared for
 * searching, and asked again only with its name (`If-None-Match`): an unchanged catalogue costs a `304`, a changed one
 * is taken in quietly. Runs in a worker (`catalog.worker.ts`), so that preparing tens of thousands of tracks never stops
 * the page; where there is no worker (the tests), in the page itself.
 */
import { albumDetail, artistDetail, facets, prepare, searchAlbums, searchArtists, searchTracks, type Compact, type Facets, type Filters, type Index } from './catalogSearch'
import { build, seedPositions, suggest, type Mode } from './suggest'

export type Loaded = { changed: boolean; tracks: number; generation: number; fetchMs: number; prepareMs: number; bytes: number; facets: Facets }

export class CatalogError extends Error {
  constructor(public readonly code: string) {
    super(code)
  }
}

export class LocalCatalog {
  private ix: Index | null = null
  private etag = ''
  private generation = 0
  private last: Facets | null = null

  /** `url`: where the catalogue comes from (a guest has its own). */
  async load(fetcher: typeof fetch = fetch, url = '/api/v1/catalog'): Promise<Loaded> {
    const started = performance.now()
    let response: Response
    try {
      response = await fetcher(url, { headers: { Accept: 'application/json', ...(this.etag && this.ix ? { 'If-None-Match': this.etag } : {}) }, credentials: 'same-origin' })
    } catch {
      throw new CatalogError('network')
    }
    if (response.status === 304 && this.ix && this.last) return { changed: false, tracks: this.ix.ids.length, generation: this.generation, fetchMs: performance.now() - started, prepareMs: 0, bytes: 0, facets: this.last }
    if (!response.ok) {
      const data = await response.json().catch(() => null)
      throw new CatalogError(typeof data?.detail?.code === 'string' ? data.detail.code : response.status === 401 ? 'sign_in_required' : 'internal_error')
    }
    const text = await response.text()
    const fetchMs = performance.now() - started
    const before = performance.now()
    const catalog = JSON.parse(text) as Compact
    this.ix = prepare(catalog)
    this.last = facets(this.ix)
    this.etag = response.headers.get('ETag') ?? ''
    this.generation = catalog.generation
    return { changed: true, tracks: this.ix.ids.length, generation: this.generation, fetchMs, prepareMs: performance.now() - before, bytes: text.length, facets: this.last }
  }

  private index(): Index {
    if (!this.ix) throw new CatalogError('no_catalog')
    return this.ix
  }

  tracks(query: string, filters: Filters, limit?: number) {
    return searchTracks(this.index(), query, filters, limit)
  }

  albums(query: string) {
    return searchAlbums(this.index(), query)
  }

  artists(query: string) {
    return searchArtists(this.index(), query)
  }

  album(id: number) {
    return albumDetail(this.index(), id)
  }

  artist(id: number) {
    return artistDetail(this.index(), id)
  }

  suggest(ask: SuggestAsk) {
    const ix = this.index()
    return {
      tracks: suggest(ix, seedPositions(ix, ask.seed), {
        mode: ask.mode,
        exclude: ask.exclude,
        hidden: ask.hidden,
        together: new Map(ask.together),
        sound: new Map(ask.sound),
        limit: ask.limit,
      }),
    }
  }

  build(ask: BuildAsk) {
    const tracks = build(this.index(), ask)
    return { tracks, duration: tracks.reduce((sum, track) => sum + track.duration, 0) }
  }
}

/** What the suggestions are asked with: the seed tracks, the kind, the songs to leave out, the two voices as pairs. */
export type SuggestAsk = { seed: number[]; mode: Mode; exclude: number[]; hidden: number[]; together: [number, number][]; sound: [number, number][]; limit?: number }
export type BuildAsk = { moods: string[]; decades: number[]; minutes: number; hits: number; exclude: number[]; hidden: number[]; seed: number }

/** What the page asks the catalogue, and what comes back (with the time the search took). */
export type Ask =
  | { op: 'load'; url?: string }
  | { op: 'tracks'; query: string; filters: Filters }
  | { op: 'albums'; query: string }
  | { op: 'artists'; query: string }
  | { op: 'album'; id: number }
  | { op: 'artist'; id: number }
  | { op: 'suggest'; ask: SuggestAsk }
  | { op: 'build'; ask: BuildAsk }

export async function answer(catalog: LocalCatalog, ask: Ask): Promise<{ result: unknown; ms: number }> {
  if (ask.op === 'load') return { result: await catalog.load(fetch, ask.url), ms: 0 }
  const started = performance.now()
  let result: unknown
  if (ask.op === 'tracks') result = catalog.tracks(ask.query, ask.filters)
  else if (ask.op === 'albums') result = catalog.albums(ask.query)
  else if (ask.op === 'artists') result = catalog.artists(ask.query)
  else if (ask.op === 'album') result = catalog.album(ask.id)
  else if (ask.op === 'suggest') result = catalog.suggest(ask.ask)
  else if (ask.op === 'build') result = catalog.build(ask.ask)
  else result = catalog.artist(ask.id)
  return { result, ms: performance.now() - started }
}
