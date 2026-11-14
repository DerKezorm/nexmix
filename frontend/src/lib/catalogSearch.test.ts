/**
 * The search in the browser: held to the same list of queries as the server (`backend/tests/fixtures/search`, recorded
 * from the server's search of a made-up library), and its rules on a small catalogue of its own: rank, filters, one song
 * once, words in any order without accents.
 */
import { albumDetail, artistDetail, facets, fold, prepare, searchAlbums, searchArtists, searchTracks, words, type Compact } from './catalogSearch'
import { MOOD_GROUPS } from './moodGroups'

/** Read from the disk, relative to the folder the tests run in (`frontend`); no node types in this project. */
const fs = (await import(/* @vite-ignore */ ['node', 'fs'].join(':'))) as unknown as { readFileSync(path: string, encoding: 'utf-8'): string }
const FIXTURES = '../backend/tests/fixtures/search'
const read = (name: string) => JSON.parse(fs.readFileSync(`${FIXTURES}/${name}`, 'utf-8'))

type Case = { case: { q: string; kind?: string; mood?: string; decade?: number; genre?: string }; expect: { total: number; items: unknown[] } }

describe('the same as the server', () => {
  const ix = prepare(read('catalog.json') as Compact)
  const cases = read('cases.json') as Case[]

  it('has the recorded cases to hold to', () => {
    expect(cases.length).toBeGreaterThan(40)
  })

  for (const { case: one, expect: wanted } of read('cases.json') as Case[]) {
    it(`finds the same for ${JSON.stringify(one)}`, () => {
      const kind = one.kind ?? 'tracks'
      let found: { total: number; items: unknown[] }
      if (kind === 'albums') {
        const result = searchAlbums(ix, one.q)
        found = { total: result.total, items: result.albums }
      } else if (kind === 'artists') {
        const result = searchArtists(ix, one.q)
        found = { total: result.total, items: result.artists }
      } else {
        const result = searchTracks(ix, one.q, { mood: one.mood, decade: one.decade, genre: one.genre })
        found = { total: result.total, items: result.tracks }
      }
      expect(found).toEqual(wanted)
    })
  }

  it('groups the moods as the server does', () => {
    const server = JSON.parse(fs.readFileSync('../backend/app/services/mood_groups.json', 'utf-8'))
    expect(MOOD_GROUPS).toEqual(server)
  })

  it('shows an album and an artist as the server does', () => {
    expect(albumDetail(ix, 205)?.tracks.map((track) => track.id)).toEqual([311, 312, 313])
    expect(artistDetail(ix, 101)?.albums.map((album) => album.id)).toEqual([204, 202, 203, 201])
    expect(albumDetail(ix, 999)).toBeNull()
    expect(artistDetail(ix, 999)).toBeNull()
  })
})

/** A small catalogue of its own, every name made up. */
function small(): Compact {
  return {
    format: 1,
    generation: 1,
    moods: ['Calm/Peaceful', 'Energetic', 'Mellow'],
    genres: ['Pop', 'Rock'],
    styles: [],
    artists: [
      [1, 'Lumen Kite', [1], []],
      [2, 'Ørsted Bloom', [], []],
    ],
    albums: [
      [10, 'First Light', 0, 1991, 1, [1], [], [2], 100],
      [11, 'Best of Lumen Kite', 0, 2008, 1, [0], [], [], 200],
      [12, 'Kälte', 1, 2015, 0, [0], [], [0], 300],
    ],
    tracks: [
      [100, 'Paper Moon', 0, '', 200, 5000, 0, [], 1, 1, 100, 1991],
      [101, 'Moonlight', 0, '', 210, 9000, 0, [1], 1, 2, 101, 1991],
      [102, 'Paper Moon', 1, '', 200, 5100, 0, [], 1, 1, 100, 1991],
      [103, 'Paper Moon (Live)', 1, '', 230, 4000, 0, [], 1, 2, 100, 1991],
      [104, 'Straße', 2, 'Ørsted Bloom feat. Lumen Kite', 190, 300, 0, [0], 1, 1, 104, 2015],
      [105, 'Moon', 2, '', 100, 10, 0, [], 1, 2, 105, 2015],
    ],
  }
}

describe('the rules', () => {
  const ix = prepare(small())
  const ids = (query: string, filters = {}) => searchTracks(ix, query, filters).tracks.map((track) => track.id)

  it('folds case, accents and ß away', () => {
    expect(fold('Ärger im Café — Straße!')).toBe('arger im cafe strasse')
    expect(fold('ﬁne Zoë')).toBe('fine zoe')
    expect(words('  Paper   MOON ')).toEqual(['paper', 'moon'])
  })

  it('finds words in any order, parts of words, without accents', () => {
    expect(ids('moon paper')).toEqual(ids('Paper Moon'))
    expect(ids('strasse')).toEqual([104])
    expect(ids('bloom')).toEqual([104, 105])
    expect(ids('ight')).toEqual([101, 100])
  })

  it('puts the title itself first, then a title that begins so, then the rest by popularity', () => {
    // "Moon" is the title (8), "Moonlight" begins with it (4), "Paper Moon" only holds the word.
    expect(ids('moon')).toEqual([105, 101, 100])
  })

  it('shows a song once, as the version shown, and counts its versions', () => {
    const found = searchTracks(ix, 'paper moon')
    expect(found.total).toBe(1)
    expect(found.tracks[0]).toMatchObject({ id: 100, song: 100, versions: 3 })
    // A word that only the live version has finds that version.
    expect(ids('paper live')).toEqual([103])
  })

  it('filters by effective mood, original decade and genre', () => {
    // Energetic is the artist's mood (0.4): every track of Lumen Kite has it, as one song each.
    expect(ids('', { mood: 'Energetic' })).toEqual([101, 100])
    // The group: Mellow of the album (0.7) and Calm/Peaceful of the track (1) and of the album (0.7).
    expect(ids('', { mood: 'g:calm' })).toEqual([101, 100, 104, 105])
    expect(ids('', { mood: 'Mellow' })).toEqual([101, 100])
    expect(ids('', { decade: 1990 })).toEqual([101, 100])
    expect(ids('', { genre: 'Rock' })).toEqual([101, 100])
    expect(ids('', { genre: 'Jazz' })).toEqual([])
    expect(ids('', { mood: 'g:none' })).toEqual([])
  })

  it('lists albums newest first without words, by title with them', () => {
    expect(searchAlbums(ix, '').albums.map((album) => album.id)).toEqual([12, 11, 10])
    expect(searchAlbums(ix, 'kalte').albums.map((album) => album.title)).toEqual(['Kälte'])
    expect(searchArtists(ix, '').artists.map((artist) => artist.name)).toEqual(['Lumen Kite', 'Ørsted Bloom'])
    expect(searchArtists(ix, 'bloom').artists.map((artist) => artist.name)).toEqual(['Ørsted Bloom'])
  })

  it('offers the moods with their tracks and the decades there are', () => {
    const offered = facets(ix)
    // Of the original years: the best-of of 2008 holds songs of 1991.
    expect(offered.decades).toEqual([1990, 2010])
    expect(offered.moods.find((mood) => mood.name === 'Energetic')?.tracks).toBe(4)
  })
})
