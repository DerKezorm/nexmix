/**
 * The suggestions for a guest, on the made-up catalogue of the server's tests: the editor's rules, seeded from the last
 * five tracks of this playlist, nothing that is in the list (no other version of a song in it either), and no voice of
 * other playlists.
 */
import { prepare, type Compact } from './catalogSearch'
import { guestAsk, LIMIT, SEED } from './guestSuggest'
import { seedPositions, suggest } from './suggest'

const fs = (await import(/* @vite-ignore */ ['node', 'fs'].join(':'))) as unknown as { readFileSync(path: string, encoding: 'utf-8'): string }
const CATALOG = JSON.parse(fs.readFileSync('../backend/tests/fixtures/search/catalog.json', 'utf-8')) as Compact
const ix = prepare(CATALOG)

/** Entries of a guest's list for these tracks, as the server sends them (the song: the id of its shown version). */
const entries = (tracks: number[], missing: number[] = []) => tracks.map((track) => ({ track, song: ix.song[ix.position.get(track)!] ?? null, missing: missing.includes(track) }))

/** What the catalogue in the browser answers (`LocalCatalog.suggest`). */
function asked(ask: ReturnType<typeof guestAsk>) {
  return suggest(ix, seedPositions(ix, ask.seed), { mode: ask.mode, exclude: ask.exclude, hidden: ask.hidden, together: new Map(ask.together), sound: new Map(ask.sound), limit: ask.limit })
}

describe('suggestions for a guest', () => {
  it('seed from the last five tracks that are there, and ask for the fitting kind only', () => {
    const tracks = [301, 302, 303, 304, 305, 306, 307]
    const ask = guestAsk(entries(tracks, [307]))
    expect(ask.seed).toEqual([302, 303, 304, 305, 306])
    expect(ask.seed).toHaveLength(SEED)
    expect(ask.mode).toBe('fit')
    expect(ask.limit).toBe(LIMIT)
  })

  it('never carry a voice of other playlists, sounds or hidden songs', () => {
    const ask = guestAsk(entries([301, 302]))
    expect(ask.together).toEqual([])
    expect(ask.sound).toEqual([])
    expect(ask.hidden).toEqual([])
    const found = asked(ask)
    expect(found.length).toBeGreaterThan(0)
    for (const one of found) {
      expect(one.reason.together).toBe(0)
      expect(one.reason.sound).toBe(false)
    }
  })

  it('are the editor’s suggestions for the same list, fifteen at most', () => {
    const list = [301, 302, 317]
    const ask = guestAsk(entries(list))
    const songs = list.map((track) => ix.song[ix.position.get(track)!])
    const editor = suggest(ix, seedPositions(ix, list), { mode: 'fit', exclude: songs, limit: 15 })
    expect(asked(ask).map((one) => one.id)).toEqual(editor.map((one) => one.id))
    expect(asked(ask).length).toBeLessThanOrEqual(15)
  })

  it('leave out what is in the list, every version of its songs, and keep two per artist', () => {
    const list = [301, 302]
    const found = asked(guestAsk(entries(list)))
    const songsInList = new Set(list.map((id) => ix.song[ix.position.get(id)!]))
    for (const one of found) {
      expect(list).not.toContain(one.id)
      expect(songsInList.has(one.song)).toBe(false)
      expect(one.duration).toBeGreaterThanOrEqual(60)
    }
    expect(new Set(found.map((one) => one.song)).size).toBe(found.length)
    const perArtist = new Map<string, number>()
    for (const one of found) perArtist.set(one.artist, (perArtist.get(one.artist) ?? 0) + 1)
    expect(Math.max(...perArtist.values())).toBeLessThanOrEqual(2)
  })

  it('never suggest the version shown for a song when another version of it is in the list', () => {
    // 308 is a live version of the song whose shown version is 301; 323 one of the song shown as 317.
    const found = asked(guestAsk(entries([308, 323])))
    expect(found.length).toBeGreaterThan(0)
    const ids = found.map((one) => one.id)
    expect(ids).not.toContain(301)
    expect(ids).not.toContain(317)
  })

  it('leave out every song of the list, also beyond the five that seed it', () => {
    // 301 fits 302, 303, 304, 307 and 311 best; 308, a live version of its song, stands first in the list.
    const seedOnly = asked({ ...guestAsk(entries([302, 303, 304, 307, 311])), limit: 15 })
    expect(seedOnly.map((one) => one.id)).toContain(301)
    const found = asked(guestAsk(entries([308, 302, 303, 304, 307, 311])))
    expect(found.length).toBeGreaterThan(0)
    expect(found.map((one) => one.id)).not.toContain(301)
  })

  it('have nothing for an empty list, as in the editor', () => {
    expect(asked(guestAsk([]))).toEqual([])
  })
})
