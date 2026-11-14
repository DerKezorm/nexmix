/**
 * Suggestions and filling by mood in the browser: held to the cases the server's rules give
 * (`backend/tests/fixtures/suggest/cases.json`, on the made-up catalogue of `fixtures/search`), and the rules themselves
 * on that catalogue: one song once, at most two per artist, never the seed, the playlist or the hidden, the reasons, the
 * voices, the same mix for the same seed.
 */
import { prepare, type Compact } from './catalogSearch'
import { artistOf, build, mulberry32, seedPositions, suggest, type Mode } from './suggest'

const fs = (await import(/* @vite-ignore */ ['node', 'fs'].join(':'))) as unknown as { readFileSync(path: string, encoding: 'utf-8'): string }
const read = (path: string) => JSON.parse(fs.readFileSync(`../backend/tests/fixtures/${path}`, 'utf-8'))

type SuggestCase = { seed: number[]; mode?: Mode; exclude?: number[]; hidden?: number[]; together?: Record<string, number>; sound?: Record<string, number>; limit?: number }
type BuildCase = { moods: string[]; decades?: number[]; minutes?: number; hits?: number; exclude?: number[]; seed: number }
type Recorded = { random: number[]; suggest: { case: SuggestCase; expect: unknown[] }[]; build: { case: BuildCase; expect: unknown[] }[] }

const ix = prepare(read('search/catalog.json') as Compact)
const recorded = read('suggest/cases.json') as Recorded
const asMap = (record: Record<string, number> = {}) => new Map(Object.entries(record).map(([key, value]) => [Number(key), value]))

const runSuggest = (one: SuggestCase) =>
  suggest(ix, seedPositions(ix, one.seed), { mode: one.mode, exclude: one.exclude, hidden: one.hidden, together: asMap(one.together), sound: asMap(one.sound), limit: one.limit })
const runBuild = (one: BuildCase) => build(ix, { moods: one.moods, decades: one.decades, minutes: one.minutes, hits: one.hits, exclude: one.exclude, seed: one.seed })

describe('the same as the server', () => {
  it('has the recorded cases to hold to', () => {
    expect(recorded.suggest.length).toBeGreaterThan(10)
    expect(recorded.build.length).toBeGreaterThan(5)
  })

  it('draws the same numbers from the same seed', () => {
    const numbers = mulberry32(7)
    expect(recorded.random.map(() => Math.round(numbers() * 1e12) / 1e12)).toEqual(recorded.random)
  })

  for (const { case: one, expect: wanted } of recorded.suggest)
    it(`suggests the same for ${JSON.stringify(one)}`, () => {
      expect(runSuggest(one)).toEqual(wanted)
    })

  for (const { case: one, expect: wanted } of recorded.build)
    it(`fills the same for ${JSON.stringify(one)}`, () => {
      expect(runBuild(one)).toEqual(wanted)
    })
})

describe('the rules', () => {
  const songs = (found: { song: number }[]) => found.map((track) => track.song)

  it('suggests each song once, in the version shown, and never the seed', () => {
    const found = runSuggest({ seed: [301], limit: 50 })
    expect(found.length).toBeGreaterThan(0)
    expect(found.every((track) => track.id === track.song)).toBe(true)
    expect(new Set(songs(found)).size).toBe(found.length)
    expect(songs(found)).not.toContain(301)
    // Another version of the seed's song as the seed: the song stays out all the same.
    expect(songs(runSuggest({ seed: [305], limit: 50 }))).not.toContain(301)
  })

  it('takes at most two per artist, one for discoveries', () => {
    for (const [mode, cap] of [['fit', 2], ['known', 2], ['new', 1]] as [Mode, number][])
      for (const seed of [[301], [311, 312, 313], [326], [324, 317, 312, 302, 326]]) {
        const per = new Map<number, number>()
        for (const track of runSuggest({ seed, mode, limit: 50 })) {
          const artist = artistOf(ix, ix.position.get(track.id)!)
          per.set(artist, (per.get(artist) ?? 0) + 1)
        }
        expect(Math.max(0, ...per.values())).toBeLessThanOrEqual(cap)
      }
  })

  it('leaves out what is in the playlist and what is hidden', () => {
    const plain = songs(runSuggest({ seed: [326], limit: 50 }))
    expect(plain).toContain(328)
    expect(songs(runSuggest({ seed: [326], hidden: [328], limit: 50 }))).not.toContain(328)
    const left = songs(runSuggest({ seed: [326], exclude: [328, plain[0]], limit: 50 }))
    expect(left).not.toContain(328)
    expect(left).not.toContain(plain[0])
  })

  it('gives the reason: shared moods, a style, the decade', () => {
    const found = runSuggest({ seed: [311], limit: 50 }).find((track) => track.song === 312)!
    expect(found.reason).toEqual({ moods: ['Bittersweet', 'Romantic'], style: 'Chanson', decade: 1970, together: 0, sound: false })
  })

  it('lets the two voices lift a song and says so', () => {
    expect(songs(runSuggest({ seed: [322], limit: 50 }))).not.toContain(326)
    const lifted = runSuggest({ seed: [322], sound: { 326: 0.05 }, limit: 50 })
    expect(lifted[0].song).toBe(326)
    expect(lifted[0].reason.sound).toBe(true)
    const together = runSuggest({ seed: [301, 302, 303, 304], together: { 311: 3 }, limit: 50 }).find((track) => track.song === 311)!
    expect(together.reason.together).toBe(3)
  })

  it('suggests nothing without a seed', () => {
    expect(runSuggest({ seed: [] })).toEqual([])
    expect(runSuggest({ seed: [999999] })).toEqual([])
  })

  it('fills by mood: the same mix for the same seed, each song once, two per artist, never side by side', () => {
    const one = { moods: ['Romantic', 'Warm', 'Brooding', 'Atmospheric', 'Melancholy'], minutes: 240, seed: 2 }
    const found = runBuild(one)
    expect(runBuild(one)).toEqual(found)
    expect(new Set(songs(found)).size).toBe(found.length)
    const places = found.map((track) => artistOf(ix, ix.position.get(track.id)!))
    for (let at = 1; at < places.length; at++) if (places[at] === places[at - 1]) expect(places.slice(at).every((place) => place === places[at])).toBe(true)
    const per = new Map<number, number>()
    for (const place of places) per.set(place, (per.get(place) ?? 0) + 1)
    expect(Math.max(...per.values())).toBeLessThanOrEqual(2)
    expect(runBuild({ moods: ['No Such Mood'], seed: 1 })).toEqual([])
    const short = runBuild({ moods: ['Romantic', 'Warm', 'Brooding'], minutes: 5, seed: 9 })
    const seconds = short.map((track) => track.duration)
    expect(seconds.slice(0, -1).reduce((a, b) => a + b, 0)).toBeLessThan(300)
    expect(seconds.reduce((a, b) => a + b, 0)).toBeGreaterThanOrEqual(300)
  })
})
