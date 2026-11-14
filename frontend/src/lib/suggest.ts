/**
 * Suggestions and filling by mood in the browser: the same rules as the server's (`backend/app/services/suggest.py`),
 * word for word, held to one list of cases (`backend/tests/fixtures/suggest`, `suggest.test.ts`).
 *
 * Suggestions start from a few tracks (the last five of the playlist, or one for "similar to this"). Their profile is
 * the share of each effective mood, of each style and genre of their albums and of each decade; every song, in the
 * version shown for it, scores by how much of that profile it has, with the two voices from the server on top ("often
 * together", "sounds alike"). Below 0.6 it is out; then the kind (fitting, known, discoveries), a little less for the
 * seed's album and artist, at most two per artist (one for discoveries), never the seed's songs, the playlist's or the
 * hidden ones. Each comes with its reason.
 *
 * Filling by mood takes the songs with enough of the chosen moods, from the chosen decades, 1:30 to 10 minutes long,
 * weighs popularity against what was played by the slider, mixes with a little chance (`mulberry32`, the same numbers as
 * the server's for the same seed), at most two per artist, until the length is reached; the same artist never twice in
 * a row where another one can stand between.
 */
import { trackView, type Index, type TrackHit } from './catalogSearch'

export type Mode = 'fit' | 'known' | 'new'
export type Reason = { moods: string[]; style: string | null; decade: number | null; together: number; sound: boolean }
export type Suggestion = TrackHit & { reason: Reason }

export const LIMIT = 14
const MOOD = 0.55
const STYLE = 1.6
const GENRE = 0.6
const DECADE = 0.9
const TOGETHER = 1.2
const SOUND = 2.4
const NEIGHBOUR_DECADE = 0.4
const FLOOR = 0.6
const SOUND_RANGE = 0.6
const SAME_ALBUM = 0.8
const SAME_ARTIST = 0.25
const SHORTEST = 60
const BUILD_SHORTEST = 90
const BUILD_LONGEST = 600

type Profile = { mood: Map<number, number>; style: Map<number, number>; genre: Map<number, number>; decade: Map<number, number>; artists: Set<number>; albums: Set<number> }

const add = (map: Map<number, number>, key: number, value: number) => map.set(key, (map.get(key) ?? 0) + value)
const ascending = (values: Iterable<number>) => [...values].sort((a, b) => a - b)

export function profile(ix: Index, seeds: number[]): Profile {
  const found: Profile = { mood: new Map(), style: new Map(), genre: new Map(), decade: new Map(), artists: new Set(), albums: new Set() }
  for (const position of seeds) {
    for (const [mood, weight] of ix.moodWeights[position]) add(found.mood, mood, weight)
    const album = ix.album[position]
    if (album >= 0) {
      for (const style of ix.albumStyles[album]) add(found.style, style, 1)
      for (const genre of ascending(ix.albumGenres[album])) add(found.genre, genre, 1)
      found.albums.add(album)
      if (ix.albumArtist[album] >= 0) found.artists.add(ix.albumArtist[album])
    }
    const decade = ix.decade[position]
    if (decade) add(found.decade, decade, 1)
  }
  const size = Math.max(1, seeds.length)
  for (const part of [found.mood, found.style, found.genre, found.decade]) for (const [key, value] of part) part.set(key, value / size)
  return found
}

export function artistOf(ix: Index, position: number): number {
  const album = ix.album[position]
  return album >= 0 ? ix.albumArtist[album] : -1
}

export function reason(ix: Index, position: number, seed: Profile, together = 0, sound = false): Reason {
  const shared = [...ix.moodWeights[position].keys()].filter((mood) => seed.mood.has(mood))
  shared.sort((a, b) => seed.mood.get(b)! - seed.mood.get(a)! || a - b)
  const album = ix.album[position]
  const style = album >= 0 ? ix.albumStyles[album].find((one) => seed.style.has(one)) : undefined
  return {
    moods: shared.slice(0, 2).map((mood) => ix.moods[mood]),
    style: style !== undefined && style < ix.styles.length ? ix.styles[style] : null,
    decade: ix.decade[position] || null,
    together,
    sound,
  }
}

/** The places of the tracks the catalogue knows, in the order given. */
export function seedPositions(ix: Index, tracks: number[]): number[] {
  return tracks.map((track) => ix.position.get(track)).filter((position): position is number => position !== undefined)
}

export type SuggestOptions = { mode?: Mode; exclude?: Iterable<number>; hidden?: Iterable<number>; together?: Map<number, number>; sound?: Map<number, number>; limit?: number }

export function suggest(ix: Index, seeds: number[], options: SuggestOptions = {}): Suggestion[] {
  if (!seeds.length) return []
  const { mode = 'fit', together = new Map<number, number>(), sound = new Map<number, number>(), limit = LIMIT } = options
  const seed = profile(ix, seeds)
  const out = new Set<number>([...(options.exclude ?? []), ...(options.hidden ?? []), ...seeds.map((position) => ix.song[position])])
  let most = 0
  for (const count of together.values()) most = Math.max(most, count)
  const scored: [number, number][] = []
  for (const position of ix.popular) {
    const song = ix.song[position]
    if (out.has(song) || ix.duration[position] < SHORTEST) continue
    let mood = 0
    for (const [one, weight] of ix.moodWeights[position]) {
      const share = seed.mood.get(one)
      if (share) mood += share * weight
    }
    const album = ix.album[position]
    let style = 0
    let genre = 0
    if (album >= 0) {
      for (const one of ix.albumStyles[album]) style += seed.style.get(one) ?? 0
      for (const one of ascending(ix.albumGenres[album])) genre += seed.genre.get(one) ?? 0
    }
    const decade = ix.decade[position]
    let near = 0
    if (decade) near = (seed.decade.get(decade) ?? 0) + NEIGHBOUR_DECADE * ((seed.decade.get(decade - 10) ?? 0) + (seed.decade.get(decade + 10) ?? 0))
    let score = mood * MOOD + style * STYLE + genre * GENRE + near * DECADE
    if (most && together.get(song)) score += (together.get(song)! / most) * TOGETHER
    if (sound.has(song)) score += Math.max(0, 1 - sound.get(song)! / SOUND_RANGE) * SOUND
    if (score < FLOOR) continue
    const pop = ix.popScore[position]
    if (mode === 'known') score += pop * 0.55
    else if (mode === 'new') score += ((ix.songPlays.get(song) ?? 0) === 0 ? 0.5 : -0.4) - pop * 0.12
    else score += pop * 0.18
    if (seed.albums.has(album)) score -= SAME_ALBUM
    if (seed.artists.has(artistOf(ix, position))) score -= SAME_ARTIST
    scored.push([score, position])
  }
  scored.sort((a, b) => b[0] - a[0] || ix.ids[a[1]] - ix.ids[b[1]])
  const cap = mode === 'new' ? 1 : 2
  const perArtist = new Map<number, number>()
  const found: Suggestion[] = []
  for (const [, position] of scored) {
    const artist = artistOf(ix, position)
    if (artist >= 0) {
      if ((perArtist.get(artist) ?? 0) >= cap) continue
      perArtist.set(artist, (perArtist.get(artist) ?? 0) + 1)
    }
    const song = ix.song[position]
    found.push({ ...trackView(ix, position), reason: reason(ix, position, seed, together.get(song) ?? 0, sound.has(song)) })
    if (found.length >= limit) break
  }
  return found
}

/** Numbers in [0, 1) from a seed, the same as the server's `mulberry32`. */
export function mulberry32(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state = (state + 0x6d2b79f5) >>> 0
    let value = state
    value = Math.imul(value ^ (value >>> 15), value | 1) >>> 0
    value = (value ^ (value + Math.imul(value ^ (value >>> 7), value | 61))) >>> 0
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296
  }
}

export type BuildOptions = { moods: string[]; decades?: number[]; minutes?: number; hits?: number; exclude?: Iterable<number>; hidden?: Iterable<number>; seed?: number; perArtist?: number }

export function build(ix: Index, options: BuildOptions): TrackHit[] {
  const { decades = [], minutes = 60, hits = 55, seed = 1, perArtist = 2 } = options
  const places = new Map(ix.moods.map((name, place) => [name, place]))
  const wanted = new Set(options.moods.filter((name) => places.has(name)).map((name) => places.get(name)!))
  if (!wanted.size) return []
  const chosenDecades = new Set(decades)
  const out = new Set<number>([...(options.exclude ?? []), ...(options.hidden ?? [])])
  const chance = mulberry32(seed)
  const scored: [number, number][] = []
  for (let position = 0; position < ix.ids.length; position++) {
    const song = ix.song[position]
    if (song !== ix.ids[position] || out.has(song)) continue
    if (ix.duration[position] < BUILD_SHORTEST || ix.duration[position] > BUILD_LONGEST) continue
    if (chosenDecades.size && !chosenDecades.has(ix.decade[position])) continue
    let mood = 0
    for (const [one, weight] of ix.moodWeights[position]) if (wanted.has(one)) mood += weight
    if (mood <= 0.4) continue
    const pop = ix.popScore[position] / 6
    const played = (ix.songPlays.get(song) ?? 0) > 0 ? 0.3 : 0
    const score = mood + pop * (hits / 50) * 1.2 - (1 - hits / 100) * played + chance() * 0.35
    scored.push([score, position])
  }
  scored.sort((a, b) => b[0] - a[0] || ix.ids[a[1]] - ix.ids[b[1]])
  const counts = new Map<number, number>()
  const picked: number[] = []
  let total = 0
  for (const [, position] of scored) {
    const artist = artistOf(ix, position)
    if (artist >= 0 && (counts.get(artist) ?? 0) >= perArtist) continue
    if (artist >= 0) counts.set(artist, (counts.get(artist) ?? 0) + 1)
    picked.push(position)
    total += ix.duration[position]
    if (total >= minutes * 60) break
  }
  for (let index = 1; index < picked.length; index++) {
    const before = artistOf(ix, picked[index - 1])
    if (before >= 0 && artistOf(ix, picked[index]) === before) {
      let later = -1
      for (let other = index + 1; other < picked.length; other++)
        if (artistOf(ix, picked[other]) !== before) {
          later = other
          break
        }
      if (later >= 0) [picked[index], picked[later]] = [picked[later], picked[index]]
    }
  }
  return picked.map((position) => trackView(ix, position))
}
