/**
 * The editor of a playlist against a stand-in server and the made-up catalogue of the server's tests: the head, the
 * search with "in" for a song already there, adding and taking out with their undo, moving by keyboard, renaming, the
 * read-only view of a shared playlist, and the questions before deleting and before taking a share back (nothing is
 * sent without them).
 */
import { Route, Routes } from 'react-router-dom'

import type { Me } from '../api/client'
import type { Change, Entry, Playlist } from '../api/playlists'
import { changeLanguage } from '../i18n'
import { editorTiming } from '../lib/editorTiming'
import { Page, Server, plain } from '../test/harness'
import { until } from '../test/wait'
import { PlaylistEditor } from './PlaylistEditor'

const me = { id: 1, name: 'jule', display_name: 'Jule', role: 'member' } as unknown as Me
vi.mock('../state/auth', () => ({ useAuth: () => ({ me }) }))

const fs = (await import(/* @vite-ignore */ ['node', 'fs'].join(':'))) as unknown as { readFileSync(path: string, encoding: 'utf-8'): string }
const CATALOG = JSON.parse(fs.readFileSync('../backend/tests/fixtures/search/catalog.json', 'utf-8'))

const server = new Server()
const page = new Page()
const jule = { id: 1, name: 'jule', display_name: 'Jule', avatar: null }
const edith = { id: 2, name: 'edith', display_name: '', avatar: null }

const entry = (id: number, track: number, title: string, by = jule): Entry => ({
  id, track, title, artist: 'Someone', album: 'Some Album', album_id: 201, cover: true, year: 1984, duration: 200,
  song: track, missing: false, added_by: by, added_by_name: by.display_name || by.name, added_at: '2026-11-13T10:00:00Z',
})

function playlist(over: Partial<Playlist> = {}): Playlist {
  return {
    id: 7, name: 'Grillabend', description: '', owner: jule, access: 'owner', shares: 1, version: 5,
    created_at: '2026-11-13T10:00:00Z', updated_at: '2026-11-13T10:00:00Z', deleted_at: null, purge_at: null, days_left: null,
    count: 3, duration: 664, covers: [201, 205, 207], people: [jule, edith], contributors: 2, moods: ['Romantic', 'Summery'],
    items: [entry(1, 301, 'Northern Lights'), entry(2, 311, 'Straße der Träume', edith), entry(3, 317, 'Señorita Lluvia')],
    smart: false, plex: { state: 'synced', error: null, smart: false, total: null, synced_at: '2026-11-13T10:00:00Z', gone_in_plex: false, note: null },
    ...over,
  }
}

const change = (over: Partial<Change>): Change => ({ version: 6, op: 'add', entries: [], order: [1, 2, 3], ...over })

beforeEach(async () => {
  await changeLanguage('de', false)
  editorTiming.rename = 0
  server.answers = {
    'GET /api/v1/catalog/status': { connected: true, library: true, library_title: 'Music', ready: true, counts: { artists: 6, albums: 10, tracks: 30, songs: 23, moods: 20 }, synced_at: null, full_at: null, running: null, queued: null, last: null, analysis: { state: 'off', percent: null }, nightly: '', search: 'browser' },
    'GET /api/v1/catalog': CATALOG,
    'GET /api/v1/playlists/7': playlist(),
  }
  server.fails = {}
  server.install()
})
afterEach(() => {
  page.close()
  vi.unstubAllGlobals()
})

async function open(): Promise<void> {
  await page.show(
    <Routes>
      <Route path="/playlists/:id" element={<PlaylistEditor />} />
      <Route path="/" element={<p data-testid="overview">overview</p>} />
    </Routes>,
    '/playlists/7',
  )
  await until(() => document.querySelector('[data-testid="search-track"]'))
}

const rows = () => [...document.querySelectorAll<HTMLElement>('[data-testid="track-row"]')]
const searchInput = () => document.querySelector<HTMLInputElement>('input[type="search"]')

describe('the editor', () => {
  it('shows the head: name, count and length, people, the moods in German, share and delete for the owner', async () => {
    await open()
    expect(document.querySelector<HTMLInputElement>('input.name')!.value).toBe('Grillabend')
    expect(plain(document.querySelector('[data-testid="playlist-meta"]')!.textContent)).toBe('3 Titel · 11 Min.')
    expect([...document.querySelectorAll('.moodbar .chip')].map((chip) => chip.textContent)).toEqual(['Romantisch', 'Sommerlich'])
    expect(page.button('Teilen')).toBeTruthy()
    expect(page.button('Löschen')).toBeTruthy()
    expect(rows().map((row) => row.querySelector('.t')!.textContent)).toEqual(['Northern Lights', 'Straße der Träume', 'Señorita Lluvia'])
    expect(rows()[1].querySelector('.by')!.textContent).toBe('Eedith')
  })

  it('searches the catalogue in the browser and marks a song already there as in', async () => {
    await open()
    await page.type(searchInput(), 'northern lights')
    const found = await until(() => {
      const hits = [...document.querySelectorAll<HTMLElement>('[data-testid="search-track"]')]
      return hits.length && hits[0].querySelector('.t')!.textContent === 'Northern Lights' ? hits : null
    })
    expect(found[0].classList.contains('in')).toBe(true)
    expect(found[0].querySelector('.t')!.getAttribute('data-in')).toBe('drin')
    expect(found[0].querySelector('button.add')).toBeNull()
    expect(plain(found[0].textContent)).toContain('4 Fassungen in der Mediathek')
    expect(plain(document.querySelector('[data-testid="timing"]')!.textContent)).toMatch(/^4 Songs in 31 Titeln\s*gesucht in .+ ms$/)
    // Dax Orlo's Northern Lights is another song: it can be added.
    expect(found[1].querySelector('button.add')).toBeTruthy()
  })

  it('adds a track and takes it out again with the undo of the sentence at the foot', async () => {
    server.answers['POST /api/v1/playlists/7/items'] = change({ entries: [entry(9, 320, 'Northern Lights')], order: [1, 2, 3, 9] })
    server.answers['POST /api/v1/playlists/7/items/remove'] = change({ version: 7, op: 'remove', order: [1, 2, 3] })
    await open()
    await page.type(searchInput(), 'northern lights')
    const other = await until(() => document.querySelectorAll<HTMLElement>('[data-testid="search-track"]')[1]?.querySelector<HTMLButtonElement>('button.add'))
    await page.click(other)
    expect(server.called('POST', '/api/v1/playlists/7/items')!.body).toEqual({ tracks: [320] })
    expect(rows().length).toBe(4)
    expect(rows()[3].classList.contains('fresh')).toBe(true)
    expect(plain(document.querySelector('[data-testid="toast"]')!.textContent)).toBe('„Northern Lights“ hinzugefügtRückgängig')
    await page.click(page.button('Rückgängig'))
    expect(server.called('POST', '/api/v1/playlists/7/items/remove')!.body).toEqual({ entries: [9] })
    expect(rows().length).toBe(3)
  })

  it('adds a whole album without the songs already there, and says so when nothing is left', async () => {
    server.answers['POST /api/v1/playlists/7/items'] = change({ entries: [entry(9, 306, 'Glasshouse'), entry(10, 307, 'Drive'), entry(11, 330, 'Intro')], order: [1, 2, 3, 9, 10, 11] })
    await open()
    await page.click(page.button('Alben'))
    await page.type(searchInput(), 'greatest')
    const add = await until(() => document.querySelector<HTMLButtonElement>('[data-testid="search-album"] button.add'))
    await page.click(add)
    // Northern Lights of the best-of is another version of the song already in the playlist: it stays out.
    expect(server.called('POST', '/api/v1/playlists/7/items')!.body).toEqual({ tracks: [306, 307, 330] })
    expect(plain(document.querySelector('[data-testid="toast"]')!.textContent)).toBe('3 Titel hinzugefügtRückgängig')
  })

  it('offers another version of a song in the album view, not the very track', async () => {
    await open()
    await page.click(page.button('Alben'))
    await page.type(searchInput(), 'greatest')
    await page.click(await until(() => document.querySelector<HTMLElement>('[data-testid="search-album"]')))
    await until(() => document.querySelector('[data-testid="album-view"]'))
    const hits = [...document.querySelectorAll<HTMLElement>('[data-testid="search-track"]')]
    // The best-of version of Northern Lights is another track of a song in the playlist: it can be chosen here.
    expect(hits[0].querySelector('.t')!.textContent).toBe('Northern Lights')
    expect(hits[0].querySelector('button.add')).toBeTruthy()
    expect(page.button('Alle hinzufügen')).toBeTruthy()
  })

  it('takes a track out, and the undo brings it back after the one before', async () => {
    server.answers['POST /api/v1/playlists/7/items/remove'] = change({ op: 'remove', order: [1, 3] })
    server.answers['POST /api/v1/playlists/7/items/restore'] = change({ version: 7, op: 'restore', entries: [entry(2, 311, 'Straße der Träume', edith)], order: [1, 2, 3] })
    await open()
    await page.click(page.button('„Straße der Träume“ entfernen'))
    expect(server.called('POST', '/api/v1/playlists/7/items/remove')!.body).toEqual({ entries: [2] })
    expect(rows().length).toBe(2)
    await page.click(page.button('Rückgängig'))
    expect(server.called('POST', '/api/v1/playlists/7/items/restore')!.body).toEqual({ entries: [2], after: 1 })
    expect(rows().map((row) => row.dataset.entry)).toEqual(['1', '2', '3'])
  })

  it('moves a track with Alt and the arrow keys', async () => {
    server.answers['POST /api/v1/playlists/7/items/move'] = change({ op: 'move', order: [2, 1, 3] })
    await open()
    const first = rows()[0]
    first.focus()
    first.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', altKey: true, bubbles: true }))
    await until(() => server.called('POST', '/api/v1/playlists/7/items/move'))
    expect(server.called('POST', '/api/v1/playlists/7/items/move')!.body).toEqual({ entry: 1, after: 2 })
    await until(() => rows()[0].dataset.entry === '2')
    // Without Alt the arrow keys do nothing.
    rows()[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    expect(server.calls.filter((call) => call.method === 'POST').length).toBe(1)
  })

  it('loads the playlist anew when a change skips a version', async () => {
    server.answers['POST /api/v1/playlists/7/items/move'] = change({ version: 8, op: 'move', order: [2, 1, 3] })
    await open()
    const before = server.calls.filter((call) => call.url === '/api/v1/playlists/7').length
    rows()[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', altKey: true, bubbles: true }))
    await until(() => server.calls.filter((call) => call.url === '/api/v1/playlists/7').length > before)
  })

  it('renames a moment after typing', async () => {
    server.answers['PUT /api/v1/playlists/7'] = { version: 6, name: 'Gartenfest' }
    await open()
    const name = document.querySelector<HTMLInputElement>('input.name')!
    name.focus()
    await page.type(name, 'Gartenfest')
    await until(() => server.called('PUT', '/api/v1/playlists/7'))
    expect(server.called('PUT', '/api/v1/playlists/7')!.body).toEqual({ name: 'Gartenfest' })
  })

  it('shows a playlist shared to see without anything to change', async () => {
    server.answers['GET /api/v1/playlists/7'] = playlist({ access: 'read', owner: edith })
    await open()
    expect(document.querySelector('input.name')).toBeNull()
    expect(plain(document.querySelector('h1.name')!.textContent)).toBe('Grillabend')
    expect(plain(document.querySelector('.plhead')!.textContent)).toContain('Nur ansehen')
    expect(plain(document.querySelector('.plhead')!.textContent)).toContain('von edith')
    expect(document.querySelector('[data-testid="search-track"] button.add')).toBeNull()
    expect(document.querySelector('.trow .rm')).toBeNull()
    expect(page.button('Teilen')).toBeUndefined()
    expect(page.button('Löschen')).toBeUndefined()
    expect(document.querySelector('[data-testid="invite-open"]')).toBeNull()
  })

  it('lets whoever may change the playlist invite guests, and counts the guests of a running invitation', async () => {
    server.answers['GET /api/v1/playlists/7'] = playlist({ access: 'edit', owner: edith, guests: 2 })
    await open()
    expect(plain(document.querySelector('[data-testid="invite-open"]')!.textContent)).toBe('Gäste einladen2')
    expect(page.button('Teilen')).toBeUndefined()
    expect(page.button('Löschen')).toBeUndefined()
  })

  it('asks before deleting and sends nothing without the answer', async () => {
    server.answers['DELETE /api/v1/playlists/7'] = playlist()
    await open()
    await page.click(page.button('Löschen'))
    expect(plain(page.dialog!.textContent)).toContain('„Grillabend“ liegt dann 30 Tage im Papierkorb und lässt sich bis dahin wiederherstellen.')
    // It can be taken back: the button is not the red one.
    expect(page.button('In den Papierkorb', page.dialog!)!.className).toContain('primary')
    await page.inDialog('Abbrechen')
    expect(server.changes()).toEqual([])
    await page.click(page.button('Löschen'))
    await page.inDialog('In den Papierkorb')
    expect(server.changes()).toEqual(['DELETE /api/v1/playlists/7'])
    await until(() => document.querySelector('[data-testid="overview"]'))
  })

  it('shares with a member at once and asks before taking a share back', async () => {
    const shares = { shares: [{ person: edith, access: 'read' }], people: [edith, { id: 3, name: 'rafael', display_name: '', avatar: null }] }
    server.answers['GET /api/v1/playlists/7/shares'] = shares
    server.answers['PUT /api/v1/playlists/7/shares/3'] = { ...shares, shares: [...shares.shares, { person: shares.people[1], access: 'edit' }] }
    server.answers['DELETE /api/v1/playlists/7/shares/2'] = { ...shares, shares: [] }
    await open()
    await page.click(page.button('Teilen'))
    const dialog = page.dialog!
    expect(plain(dialog.textContent)).toContain('Der Betreiber sieht alle Playlisten ohnehin.')
    const groups = [...dialog.querySelectorAll<HTMLElement>('.sharerow .seg')]
    await page.click(page.button('Bearbeiten', groups[1]))
    expect(server.changes()).toEqual(['PUT /api/v1/playlists/7/shares/3'])
    expect(server.called('PUT', '/api/v1/playlists/7/shares/3')!.body).toEqual({ access: 'edit' })
    await page.click(page.button('Nicht geteilt', groups[0]))
    expect(plain(page.dialog!.textContent)).toContain('edith sieht „Grillabend“ dann nicht mehr. Du kannst sie jederzeit wieder teilen.')
    await page.inDialog('Abbrechen')
    expect(server.changes()).toEqual(['PUT /api/v1/playlists/7/shares/3'])
    await page.click(page.button('Nicht geteilt', groups[0]))
    await page.inDialog('Nicht mehr teilen')
    expect(server.changes()).toEqual(['PUT /api/v1/playlists/7/shares/3', 'DELETE /api/v1/playlists/7/shares/2'])
  })

  it('says when the playlist is not there or not shared', async () => {
    server.fails['GET /api/v1/playlists/7'] = { status: 404, code: 'not_found' }
    await page.show(
      <Routes>
        <Route path="/playlists/:id" element={<PlaylistEditor />} />
      </Routes>,
      '/playlists/7',
    )
    expect(plain(document.querySelector('[data-testid="playlist-missing"]')!.textContent)).toBe('Diese Playlist gibt es nicht, oder sie ist nicht mit dir geteilt. Zu den Playlisten')
  })

  it('speaks English too, without dashes', async () => {
    await changeLanguage('en', false)
    await open()
    expect(plain(document.querySelector('[data-testid="playlist-meta"]')!.textContent)).toBe('3 tracks · 11 min')
    expect(document.querySelector('[data-testid="editor"]')!.textContent).not.toMatch(/[–—]/)
    expect(searchInput()!.placeholder).toBe('Title, artist, album …')
  })
})
