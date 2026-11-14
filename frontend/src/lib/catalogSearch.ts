/**
 * The search of the catalogue in the browser: the same rules as the server's (`backend/app/services/search.py`), word
 * for word, so that a large library searched on the server finds the same as a small one searched here. Both are held to
 * one list of queries (`backend/tests/fixtures/search`, `catalogSearch.test.ts`).
 *
 * Words in any order, each a part of the title, the track's artist, the album's artist or the album, folded (lower case,
 * accents gone, ß as ss). Filters: a mood or a group of moods on the effective moods (track 1, album 0.7, artist 0.4),
 * a decade of the original year, a genre of the album. Rank: the song's popularity, the mood's weight twice, 8 for the
 * title itself, else 4 when it begins so, 2.5 when the artist begins with the first word; ties by the lower id. One song
 * once: the version shown for it when found, else the best found; `versions` says how many the library holds.
 */
import { MOOD_GROUPS } from './moodGroups'

export type Compact = {
  format: number
  generation: number
  moods: string[]
  genres: string[]
  styles: string[]
  /** [id, name, [moods], [styles]] */
  artists: [number, string, number[], number[]][]
  /** [id, title, artist, year, cover, [genres], [styles], [moods], added] */
  albums: [number, string, number, number, number, number[], number[], number[], number][]
  /** [id, title, album, own artist, seconds, listeners, plays, [moods], disc, number, song, original year] */
  tracks: [number, string, number, string, number, number, number, number[], number, number, number, number][]
}

export type TrackHit = {
  id: number
  title: string
  artist: string
  album: string
  album_id: number | null
  cover: boolean
  year: number | null
  duration: number
  song: number
  versions: number
}
export type AlbumHit = { id: number; title: string; artist: string; artist_id: number | null; year: number | null; cover: boolean; tracks: number }
export type ArtistHit = { id: number; name: string; moods: string[]; cover: number | null; albums: number }
export type Filters = { mood?: string; decade?: number; genre?: string }
export type TrackResult = { total: number; tracks: TrackHit[] }
export type AlbumResult = { total: number; albums: AlbumHit[] }
export type ArtistResult = { total: number; artists: ArtistHit[] }
export type AlbumDetail = { album: AlbumHit; tracks: TrackHit[] }
export type ArtistDetail = { artist: ArtistHit; tracks: TrackHit[]; albums: AlbumHit[] }
export type Facets = { moods: { name: string; tracks: number }[]; groups: { key: string; moods: string[] }[]; genres: string[]; decades: number[]; tracks: number }

export const TRACK_LIMIT = 80
const ALBUM_LIMIT = 60
const ALBUMS_RECENT = 40
const ARTIST_LIMIT = 80
const ARTIST_TOP = 15
const TRACK_WEIGHT = 1
const ALBUM_WEIGHT = 0.7
const ARTIST_WEIGHT = 0.4

/** Lower case, ß as ss, accents gone (NFKD without marks), anything but letters and digits a single space. */
export function fold(text: unknown): string {
  return String(text ?? '')
    .toLowerCase()
    .replace(/ß/g, 'ss')
    .normalize('NFKD')
    .replace(/\p{M}/gu, '')
    .replace(/[^\p{L}\p{N}]+/gu, ' ')
    .trim()
}

export function words(query: unknown): string[] {
  return fold(query)
    .split(' ')
    .filter((word) => word !== '')
}

/** Strings compared by their code points, as the server's sort does (never by the language's rules). */
function cmp(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0
}

export type Index = {
  moods: string[]
  genres: string[]
  styles: string[]
  ids: number[]
  title: string[]
  artist: string[]
  album: number[]
  duration: number[]
  rating: number[]
  disc: number[]
  number: number[]
  song: number[]
  original: number[]
  hay: string[]
  titleFold: string[]
  shownFold: string[]
  moodWeights: Map<number, number>[]
  songPop: Map<number, number>
  songVersions: Map<number, number>
  /** Per song: how often its versions were played, together (the suggestions' "discoveries"). */
  songPlays: Map<number, number>
  position: Map<number, number>
  albumIds: number[]
  albumTitle: string[]
  albumArtist: number[]
  albumYear: number[]
  albumCover: boolean[]
  albumGenres: Set<number>[]
  albumStyles: number[][]
  albumAdded: number[]
  albumFold: string[]
  albumTitleFold: string[]
  albumTracks: number[][]
  albumPosition: Map<number, number>
  artistIds: number[]
  artistName: string[]
  artistMoods: number[][]
  artistFold: string[]
  artistAlbums: number[][]
  artistTracks: number[][]
  artistPosition: Map<number, number>
  byMood: Map<number, number>
  byDecade: Set<number>
  popular: number[]
  /** Per track: the song's popularity as it counts in the rank, the decade of its original year. */
  popScore: number[]
  decade: number[]
  /** Per mood: [track, weight] for every track that has it; per song its tracks. */
  moodPairs: Map<number, [number, number][]>
  members: Map<number, number[]>
  moodCache: Map<string, Map<number, number>>
}

export function prepare(catalog: Compact): Index {
  const ix: Index = {
    moods: catalog.moods,
    genres: catalog.genres,
    styles: catalog.styles ?? [],
    ids: [], title: [], artist: [], album: [], duration: [], rating: [], disc: [], number: [], song: [], original: [],
    hay: [], titleFold: [], shownFold: [], moodWeights: [], songPop: new Map(), songVersions: new Map(), songPlays: new Map(), position: new Map(),
    albumIds: [], albumTitle: [], albumArtist: [], albumYear: [], albumCover: [], albumGenres: [], albumStyles: [], albumAdded: [],
    albumFold: [], albumTitleFold: [], albumTracks: [], albumPosition: new Map(),
    artistIds: [], artistName: [], artistMoods: [], artistFold: [], artistAlbums: [], artistTracks: [], artistPosition: new Map(),
    byMood: new Map(), byDecade: new Set(), popular: [],
    popScore: [], decade: [], moodPairs: new Map(), members: new Map(), moodCache: new Map(),
  }
  catalog.artists.forEach(([id, name, moods], position) => {
    ix.artistIds.push(id)
    ix.artistName.push(name)
    ix.artistMoods.push(moods)
    ix.artistFold.push(fold(name))
    ix.artistAlbums.push([])
    ix.artistTracks.push([])
    ix.artistPosition.set(id, position)
  })
  const albumMoods: number[][] = []
  catalog.albums.forEach(([id, title, artist, year, cover, genres, styles, moods, added], position) => {
    ix.albumIds.push(id)
    ix.albumTitle.push(title)
    ix.albumArtist.push(artist)
    ix.albumYear.push(year)
    ix.albumCover.push(Boolean(cover))
    ix.albumGenres.push(new Set(genres))
    ix.albumStyles.push(styles)
    ix.albumAdded.push(added)
    const name = artist >= 0 ? ix.artistName[artist] : ''
    ix.albumFold.push(fold(`${title} ${name}`))
    ix.albumTitleFold.push(fold(title))
    ix.albumTracks.push([])
    ix.albumPosition.set(id, position)
    albumMoods.push(moods)
    if (artist >= 0) ix.artistAlbums[artist].push(position)
  })
  catalog.tracks.forEach(([id, title, album, own, duration, rating, plays, moods, disc, number, song, original], position) => {
    const artist = album >= 0 ? ix.albumArtist[album] : -1
    const albumArtist = artist >= 0 ? ix.artistName[artist] : ''
    const shown = own || albumArtist
    const weights = new Map<number, number>()
    if (artist >= 0) for (const mood of ix.artistMoods[artist]) weights.set(mood, ARTIST_WEIGHT)
    if (album >= 0) for (const mood of albumMoods[album]) weights.set(mood, ALBUM_WEIGHT)
    for (const mood of moods) weights.set(mood, TRACK_WEIGHT)
    const albumTitle = album >= 0 ? ix.albumTitle[album] : ''
    ix.ids.push(id)
    ix.title.push(title)
    ix.artist.push(shown)
    ix.album.push(album)
    ix.duration.push(duration)
    ix.rating.push(rating)
    ix.disc.push(disc)
    ix.number.push(number)
    ix.song.push(song)
    const year = original || (album >= 0 ? ix.albumYear[album] : 0)
    ix.original.push(year)
    ix.hay.push(fold([title, own, albumArtist, albumTitle].filter((part) => part).join(' ')))
    ix.titleFold.push(fold(title))
    ix.shownFold.push(fold(shown))
    ix.moodWeights.push(weights)
    ix.songPop.set(song, Math.max(ix.songPop.get(song) ?? 0, rating))
    ix.songVersions.set(song, (ix.songVersions.get(song) ?? 0) + 1)
    ix.songPlays.set(song, (ix.songPlays.get(song) ?? 0) + plays)
    ix.position.set(id, position)
    if (album >= 0) ix.albumTracks[album].push(position)
    if (artist >= 0) ix.artistTracks[artist].push(position)
    for (const [mood, weight] of weights) {
      ix.byMood.set(mood, (ix.byMood.get(mood) ?? 0) + 1)
      const pairs = ix.moodPairs.get(mood)
      if (pairs) pairs.push([position, weight])
      else ix.moodPairs.set(mood, [[position, weight]])
    }
    if (year) ix.byDecade.add(Math.floor(year / 10) * 10)
    ix.decade.push(Math.floor(year / 10) * 10)
    const members = ix.members.get(song)
    if (members) members.push(position)
    else ix.members.set(song, [position])
  })
  ix.popScore = ix.song.map((song) => Math.log10(1 + (ix.songPop.get(song) ?? 0)))
  const shownFirst: number[] = []
  for (let position = 0; position < ix.ids.length; position++) if (ix.song[position] === ix.ids[position]) shownFirst.push(position)
  const pop = (position: number) => Math.log10(1 + (ix.songPop.get(ix.song[position]) ?? 0))
  shownFirst.sort((a, b) => pop(b) - pop(a) || ix.ids[a] - ix.ids[b])
  ix.popular = shownFirst
  return ix
}

/** The moods a filter stands for (by place); null without a filter. An unknown one stands for none. */
export function moodSet(ix: Index, mood: string | undefined): Set<number> | null {
  if (!mood) return null
  const names = mood.startsWith('g:') ? (MOOD_GROUPS.find(([key]) => key === mood.slice(2))?.[1] ?? []) : [mood]
  const places = new Map(ix.moods.map((name, position) => [name, position]))
  return new Set(names.filter((name) => places.has(name)).map((name) => places.get(name)!))
}

function genreOf(ix: Index, genre: string | undefined): number | null {
  if (!genre) return null
  return ix.genres.indexOf(genre)
}

export function trackView(ix: Index, position: number): TrackHit {
  const album = ix.album[position]
  const year = album >= 0 ? ix.albumYear[album] : 0
  return {
    id: ix.ids[position],
    title: ix.title[position],
    artist: ix.artist[position],
    album: album >= 0 ? ix.albumTitle[album] : '',
    album_id: album >= 0 ? ix.albumIds[album] : null,
    cover: album >= 0 ? ix.albumCover[album] : false,
    year: year || null,
    duration: ix.duration[position],
    song: ix.song[position],
    versions: ix.songVersions.get(ix.song[position]) ?? 1,
  }
}

/** The strongest weight of a wanted mood per track that has one; kept per filter (the catalogue never changes). */
function moodWeights(ix: Index, mood: string, moods: Set<number>): Map<number, number> {
  let found = ix.moodCache.get(mood)
  if (!found) {
    found = new Map()
    for (const one of moods)
      for (const [position, weight] of ix.moodPairs.get(one) ?? []) if (weight > (found.get(position) ?? 0)) found.set(position, weight)
    if (ix.moodCache.size > 400) ix.moodCache.clear()
    ix.moodCache.set(mood, found)
  }
  return found
}

/** The `limit` largest of the values (the last one is the lowest score that still counts). */
function largest(values: Iterable<number>, limit: number): number[] {
  const all = [...values].sort((a, b) => b - a)
  return all.slice(0, limit)
}

export function searchTracks(ix: Index, query: string, filters: Filters = {}, limit = TRACK_LIMIT): TrackResult {
  const toks = words(query)
  const moods = moodSet(ix, filters.mood)
  const genre = genreOf(ix, filters.genre)
  const decade = filters.decade ?? 0
  if (!toks.length && !filters.mood && !decade && !filters.genre) return { total: ix.popular.length, tracks: ix.popular.slice(0, limit).map((position) => trackView(ix, position)) }
  const joined = toks.join(' ')
  const first = toks[0] ?? ''
  const moodOf = moods !== null ? moodWeights(ix, filters.mood ?? '', moods) : null
  const scoreOf = (position: number): number | null => {
    if (decade && ix.decade[position] !== decade) return null
    if (genre !== null) {
      const album = ix.album[position]
      if (album < 0 || !ix.albumGenres[album].has(genre)) return null
    }
    if (moodOf !== null && !moodOf.has(position)) return null
    const hay = ix.hay[position]
    for (const tok of toks) if (!hay.includes(tok)) return null
    let score = ix.popScore[position] + (moodOf !== null ? moodOf.get(position)! : 0) * 2
    if (toks.length) {
      const title = ix.titleFold[position]
      if (title === joined) score += 8
      else if (title.startsWith(joined)) score += 4
      if (ix.shownFold[position].startsWith(first)) score += 2.5
    }
    return score
  }
  // First the best score per song over every track found; then, only for the songs that can reach the first `limit`,
  // the version to show (the one shown for the song when found, else the best found, ties by the lower id).
  const best = new Map<number, number>()
  const visit = (position: number) => {
    const score = scoreOf(position)
    if (score === null) return
    const song = ix.song[position]
    if (score > (best.get(song) ?? -1)) best.set(song, score)
  }
  if (moodOf !== null) for (const position of moodOf.keys()) visit(position)
  else for (let position = 0; position < ix.ids.length; position++) visit(position)
  if (!best.size) return { total: 0, tracks: [] }
  const top = largest(best.values(), limit)
  const threshold = top[top.length - 1]
  const shownOf = new Map<number, number>()
  for (const [song, score] of best) {
    if (score < threshold) continue
    let chosen = -1
    let chosenScore = -1
    for (const position of ix.members.get(song) ?? []) {
      const found = scoreOf(position)
      if (found === null) continue
      if (ix.ids[position] === song) {
        chosen = position
        break
      }
      if (found > chosenScore || (found === chosenScore && chosen >= 0 && ix.ids[position] < ix.ids[chosen])) {
        chosen = position
        chosenScore = found
      }
    }
    shownOf.set(song, chosen)
  }
  const ranked = [...shownOf.keys()].sort((a, b) => best.get(b)! - best.get(a)! || ix.ids[shownOf.get(a)!] - ix.ids[shownOf.get(b)!]).slice(0, limit)
  return { total: best.size, tracks: ranked.map((song) => trackView(ix, shownOf.get(song)!)) }
}

export function albumView(ix: Index, position: number): AlbumHit {
  const artist = ix.albumArtist[position]
  return {
    id: ix.albumIds[position],
    title: ix.albumTitle[position],
    artist: artist >= 0 ? ix.artistName[artist] : '',
    artist_id: artist >= 0 ? ix.artistIds[artist] : null,
    year: ix.albumYear[position] || null,
    cover: ix.albumCover[position],
    tracks: ix.albumTracks[position].length,
  }
}

export function searchAlbums(ix: Index, query: string): AlbumResult {
  const toks = words(query)
  const all = ix.albumIds.map((_id, position) => position)
  if (!toks.length) {
    const recent = all.sort((a, b) => ix.albumAdded[b] - ix.albumAdded[a] || ix.albumIds[a] - ix.albumIds[b])
    return { total: ix.albumIds.length, albums: recent.slice(0, ALBUMS_RECENT).map((p) => albumView(ix, p)) }
  }
  const joined = toks.join(' ')
  const found = all.filter((p) => toks.every((tok) => ix.albumFold[p].includes(tok)))
  const head = (p: number) => (ix.albumTitleFold[p].startsWith(joined) ? 0 : 1)
  found.sort((a, b) => head(a) - head(b) || cmp(ix.albumTitleFold[a], ix.albumTitleFold[b]) || ix.albumIds[a] - ix.albumIds[b])
  return { total: found.length, albums: found.slice(0, ALBUM_LIMIT).map((p) => albumView(ix, p)) }
}

function artistCover(ix: Index, position: number): number | null {
  const albums = ix.artistAlbums[position].filter((p) => ix.albumCover[p]).sort((a, b) => ix.albumYear[b] - ix.albumYear[a] || ix.albumIds[a] - ix.albumIds[b])
  return albums.length ? ix.albumIds[albums[0]] : null
}

export function artistView(ix: Index, position: number): ArtistHit {
  return {
    id: ix.artistIds[position],
    name: ix.artistName[position],
    moods: ix.artistMoods[position].slice(0, 4).map((mood) => ix.moods[mood]),
    cover: artistCover(ix, position),
    albums: ix.artistAlbums[position].length,
  }
}

export function searchArtists(ix: Index, query: string): ArtistResult {
  const toks = words(query)
  const joined = toks.join(' ')
  const found = ix.artistIds.map((_id, position) => position).filter((p) => toks.every((tok) => ix.artistFold[p].includes(tok)))
  const head = (p: number) => (toks.length && ix.artistFold[p].startsWith(joined) ? 0 : 1)
  found.sort((a, b) => head(a) - head(b) || cmp(ix.artistFold[a], ix.artistFold[b]) || ix.artistIds[a] - ix.artistIds[b])
  return { total: found.length, artists: found.slice(0, ARTIST_LIMIT).map((p) => artistView(ix, p)) }
}

export function albumDetail(ix: Index, albumId: number): AlbumDetail | null {
  const position = ix.albumPosition.get(albumId)
  if (position === undefined) return null
  const tracks = [...ix.albumTracks[position]].sort((a, b) => ix.disc[a] - ix.disc[b] || ix.number[a] - ix.number[b] || ix.ids[a] - ix.ids[b])
  return { album: albumView(ix, position), tracks: tracks.map((p) => trackView(ix, p)) }
}

export function artistDetail(ix: Index, artistId: number): ArtistDetail | null {
  const position = ix.artistPosition.get(artistId)
  if (position === undefined) return null
  const perSong = new Map<number, number>()
  for (const p of ix.artistTracks[position]) {
    const song = ix.song[p]
    const held = perSong.get(song)
    if (held === undefined || (ix.ids[held] !== song && (ix.ids[p] === song || ix.ids[p] < ix.ids[held]))) perSong.set(song, p)
  }
  const pop = (p: number) => ix.songPop.get(ix.song[p]) ?? 0
  const top = [...perSong.values()].sort((a, b) => pop(b) - pop(a) || ix.ids[a] - ix.ids[b]).slice(0, ARTIST_TOP)
  const albums = [...ix.artistAlbums[position]].sort((a, b) => ix.albumYear[b] - ix.albumYear[a] || ix.albumIds[a] - ix.albumIds[b])
  return { artist: artistView(ix, position), tracks: top.map((p) => trackView(ix, p)), albums: albums.map((p) => albumView(ix, p)) }
}

export function facets(ix: Index): Facets {
  return {
    moods: ix.moods.map((name, position) => ({ name, tracks: ix.byMood.get(position) ?? 0 })),
    groups: MOOD_GROUPS.map(([key, moods]) => ({ key, moods })),
    genres: ix.genres,
    decades: [...ix.byDecade].sort((a, b) => a - b),
    tracks: ix.ids.length,
  }
}
