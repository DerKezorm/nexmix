/** A change applied to the playlist a page holds: only the next version, a gap means loading anew, an old one is
 * left alone (the answer to the own change and its event come both). */
import { applyChange, type Change, type Entry, type Playlist } from './playlists'

const entry = (id: number) => ({ id, track: 300 + id, title: `T${id}` }) as Entry
const base = { id: 7, version: 5, items: [entry(1), entry(2)], count: 2, duration: 10, covers: [], people: [], contributors: 0, moods: [] } as unknown as Playlist

describe('applying a change', () => {
  it('takes the next version with its new entries and order', () => {
    const next = applyChange(base, { version: 6, op: 'add', entries: [entry(3)], order: [3, 1, 2], summary: { count: 3, duration: 15, covers: [201], people: [], contributors: 0 } } as Change)
    expect(next).not.toBeNull()
    const found = next as Playlist
    expect(found.items.map((one) => one.id)).toEqual([3, 1, 2])
    expect(found.version).toBe(6)
    expect(found.count).toBe(3)
  })

  it('leaves an old version and a change of nothing alone', () => {
    expect(applyChange(base, { version: 5, op: 'move', entries: [], order: [2, 1] })).toBe('same')
    expect(applyChange(base, { version: 6, op: 'none', entries: [] })).toBe('same')
  })

  it('asks for the whole playlist after a gap or an unknown entry', () => {
    expect(applyChange(base, { version: 7, op: 'move', entries: [], order: [2, 1] })).toBeNull()
    expect(applyChange(base, { version: 6, op: 'move', entries: [], order: [2, 9] })).toBeNull()
  })
})
