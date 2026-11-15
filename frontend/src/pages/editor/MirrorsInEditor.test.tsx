/**
 * Mirrors in the editor of a playlist and "take all over" in the settings, against a stand-in server: the badges of the
 * source and the mirrors with their state, an entry without a track on a mirror says so, "copy to …" and "move to …"
 * for the owner only (and only when another server is there), the mirror's own dialog that asks before letting it go;
 * the card "take all playlists over" with its preview, its start, its progress and "stop".
 */
import { Route, Routes } from 'react-router-dom'

import type { Me } from '../../api/client'
import type { Mirror, Transfer } from '../../api/mirrors'
import type { Entry, Playlist } from '../../api/playlists'
import type { MediaServer } from '../../api/servers'
import { changeLanguage } from '../../i18n'
import { Page, Server, plain } from '../../test/harness'
import { until } from '../../test/wait'
import { PlaylistEditor } from '../PlaylistEditor'
import { TransferCard } from '../settings/TransferCard'

const me = { id: 1, name: 'jule', display_name: 'Jule', role: 'member' } as unknown as Me
vi.mock('../../state/auth', () => ({ useAuth: () => ({ me }) }))

const server = new Server()
const page = new Page()
const jule = { id: 1, name: 'jule', display_name: 'Jule', avatar: null }
const caps = (moods: boolean) => ({ moods, sound: moods, smart_playlists: moods, duplicates: !moods, changes: 'both' as const, playlists: true })
const studio: MediaServer = { id: 1, kind: 'plex', name: 'Studio', active: true, library: 'Musik', ready: true, primary: true, capabilities: caps(true) }
const probe: MediaServer = { id: 2, kind: 'jellyfin', name: 'Probe', active: true, library: 'Musik', ready: true, primary: false, capabilities: caps(false) }

const entry = (id: number, track: number, title: string, absent: number[] = []): Entry => ({
  id, track, title, artist: 'Someone', album: 'Some Album', album_id: 201, cover: false, year: 1984, duration: 200,
  song: track, missing: false, added_by: jule, added_by_name: 'Jule', added_at: '2026-11-13T10:00:00Z', absent,
})
const mirror = (over: Partial<Mirror> = {}): Mirror => ({ server: 2, kind: 'jellyfin', name: 'Probe', state: 'synced', error: null, synced_at: null, absent: 1, extras: [], ...over })

function playlist(over: Partial<Playlist> = {}): Playlist {
  return {
    id: 7, name: 'Grillabend', description: '', owner: jule, access: 'owner', shares: 0, version: 5,
    created_at: '2026-11-13T10:00:00Z', updated_at: '2026-11-13T10:00:00Z', deleted_at: null, purge_at: null, days_left: null,
    count: 2, duration: 400, covers: [], people: [jule], contributors: 1, moods: [],
    items: [entry(1, 301, 'Northern Lights'), entry(2, 311, 'Op. 3', [2])],
    smart: false, plex: { state: 'synced', kind: 'plex', error: null, smart: false, total: null, synced_at: null, gone_in_plex: false, note: null },
    server: 1, mirrors: [mirror()],
    ...over,
  }
}

beforeEach(async () => {
  await changeLanguage('de', false)
  server.answers = {
    'GET /api/v1/catalog/status?server=1': { connected: true, library: true, library_title: 'Music', ready: true, counts: { artists: 1, albums: 1, tracks: 2, songs: 2, moods: 0 }, synced_at: null, full_at: null, running: null, queued: null, last: null, analysis: { state: 'off', percent: null }, nightly: '', search: 'server' },
    'GET /api/v1/catalog/facets?server=1': { moods: [], groups: [], genres: [], decades: [], tracks: 2 },
    'GET /api/v1/servers': { servers: [studio, probe] },
    'GET /api/v1/playlists/7': playlist(),
  }
  server.fails = {}
  server.install()
  const plainFetch = server.fetch()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url.startsWith('/api/v1/suggestions')) return new Response(JSON.stringify({ tracks: [] }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      if (url.startsWith('/api/v1/catalog/search')) return new Response(JSON.stringify({ total: 0, tracks: [], albums: [], artists: [], took_ms: 1 }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      return plainFetch(url, init)
    }),
  )
})
afterEach(() => {
  page.close()
  vi.unstubAllGlobals()
})

async function open(): Promise<void> {
  await page.show(
    <Routes>
      <Route path="/playlists/:id" element={<PlaylistEditor />} />
    </Routes>,
    '/playlists/7',
  )
  await until(() => document.querySelector('[data-testid="server-badges"]'))
}

describe('mirrors in the editor', () => {
  it('shows the source and the mirror with its state, and the entry that is not on the mirror', async () => {
    await open()
    expect(plain(document.querySelector('[data-testid="source-chip"]')!.textContent)).toBe('Quelle: Studio')
    expect(plain(document.querySelector('[data-testid="mirror-chip"]')!.textContent)).toBe('Spiegel: Probe')
    expect(document.querySelector('[data-testid="mirror-chip"]')!.getAttribute('aria-label')).toBe('Spiegel auf Probe: abgeglichen')
    const absent = [...document.querySelectorAll('[data-testid="entry-absent"]')].map((one) => plain(one.textContent))
    expect(absent).toEqual(['Nicht auf Probe'])
    // Probe holds the mirror already: only moving goes there, copying has no server left.
    expect(document.querySelector('[data-testid="copy-open"]')).toBeNull()
    expect(document.querySelector('[data-testid="move-open"]')).toBeTruthy()
  })

  it('offers copying and moving to the owner, not to one who may only change it', async () => {
    server.answers['GET /api/v1/playlists/7'] = playlist({ mirrors: [] })
    await page.show(
      <Routes>
        <Route path="/playlists/:id" element={<PlaylistEditor />} />
      </Routes>,
      '/playlists/7',
    )
    await until(() => document.querySelector('[data-testid="copy-open"]'))
    expect(document.querySelector('[data-testid="server-badges"]')).toBeNull()
    page.close()
    server.answers['GET /api/v1/playlists/7'] = playlist({ mirrors: [], access: 'edit', owner: { id: 3, name: 'olivia', display_name: 'Olivia', avatar: null } })
    await page.show(
      <Routes>
        <Route path="/playlists/:id" element={<PlaylistEditor />} />
      </Routes>,
      '/playlists/7',
    )
    await until(() => document.querySelector('[data-testid="playlist-head"]'))
    await until(() => server.called('GET', '/api/v1/servers'))
    expect(document.querySelector('[data-testid="copy-open"]')).toBeNull()
    expect(document.querySelector('[data-testid="move-open"]')).toBeNull()
  })

  it('opens the mirror with the entry not there, and asks before letting the mirror go', async () => {
    server.answers['PUT /api/v1/playlists/7/mirrors/2/entries/2'] = { mirrors: [mirror({ absent: 0 })] }
    server.answers['DELETE /api/v1/playlists/7/mirrors/2'] = 204
    await open()
    await page.click(document.querySelector<HTMLButtonElement>('[data-testid="mirror-chip"]'))
    expect(plain(page.dialog!.textContent)).toContain('1 Titel ohne Gegenstück auf Probe')
    await page.click(page.button('Spiegel lösen', page.dialog!))
    expect(plain(page.dialog!.textContent)).toContain('Sie bleibt dort, wie sie ist.')
    await page.inDialog('Abbrechen')
    expect(server.changes()).toEqual([])
    await page.click(page.button('Spiegel lösen', page.dialog!))
    await page.inDialog('Spiegel lösen')
    await until(() => server.changes().length === 1)
    expect(server.changes()).toEqual(['DELETE /api/v1/playlists/7/mirrors/2'])
  })
})

describe('take all over', () => {
  const run = (over: Partial<Transfer> = {}): Transfer => ({ id: 1, server: 2, mode: 'move', old: 'keep', state: 'running', total: 4, done: 1, skipped: 0, stopping: false, error: null, disconnect: null, started_at: '2026-11-15T10:00:00Z', finished_at: null, ...over })

  it('shows the preview of every playlist, starts, shows the progress and stops', async () => {
    server.answers['GET /api/v1/servers/2/transfer'] = { transfer: null }
    server.answers['POST /api/v1/servers/2/transfer/preview'] = { server: 2, entries: 12, found: 11, unsure: 0, missing: 1, rows: [], more: 0, playlists: 4, smart: 1 }
    server.answers['POST /api/v1/servers/2/transfer'] = { transfer: run() }
    server.answers['DELETE /api/v1/servers/2/transfer'] = { transfer: run({ stopping: true }) }
    await page.show(<TransferCard target={probe} others={[studio]} />)
    await page.click(page.button('Alle übernehmen …'))
    await until(() => document.querySelector('[data-testid="transfer-playlists"]'))
    expect(plain(document.querySelector('[data-testid="transfer-playlists"]')!.textContent)).toBe('4 Playlisten. 1 Smart-Playlist bleibt auf ihrem Server.')
    expect(plain(document.querySelector('[data-testid="mirror-count"]')!.textContent)).toBe('11 von 12 gefunden1 nicht gefunden')
    expect(server.called('POST', '/api/v1/servers/2/transfer/preview')!.body).toEqual({ mode: 'mirror', choices: {} })
    await page.click(page.button('Verschieben', page.dialog!))
    await until(() => server.calls.filter((call) => call.url === '/api/v1/servers/2/transfer/preview').length === 2)
    await page.click(document.querySelector<HTMLButtonElement>('[data-testid="transfer-go"]'))
    await until(() => document.querySelector('[data-testid="transfer-state"]'))
    expect(server.called('POST', '/api/v1/servers/2/transfer')!.body).toEqual({ mode: 'move', old: 'keep', choices: {}, source: null, disconnect: null })
    expect(plain(document.querySelector('[data-testid="transfer-state"]')!.textContent)).toBe('Läuft: 1 von 4 übernommen.')
    await page.click(page.button('Anhalten'))
    await until(() => page.button('Hält nach der laufenden Playlist an …'))
    expect(server.changes()).toContain('DELETE /api/v1/servers/2/transfer')
  })

  it('asks before moving with the old copies deleted and a server let go of', async () => {
    server.answers['GET /api/v1/servers/2/transfer'] = { transfer: run({ state: 'done', done: 4 }) }
    server.answers['POST /api/v1/servers/2/transfer/preview'] = { server: 2, entries: 3, found: 3, unsure: 0, missing: 0, rows: [], more: 0, playlists: 1, smart: 0 }
    await page.show(<TransferCard target={probe} others={[studio]} />)
    expect(plain(document.querySelector('[data-testid="transfer-state"]')!.textContent)).toBe('Fertig: 4 von 4 übernommen, 0 übersprungen.')
    await page.click(page.button('Alle übernehmen …'))
    await page.click(page.button('Verschieben', page.dialog!))
    await page.click(page.button('Dort löschen', page.dialog!))
    await page.choose(document.querySelector<HTMLSelectElement>('[data-testid="transfer-let-go"]'), '1')
    await until(() => server.calls.some((call) => call.url === '/api/v1/servers/2/transfer/preview' && (call.body as { source?: number }).source === 1))
    await page.click(document.querySelector<HTMLButtonElement>('[data-testid="transfer-go"]'))
    expect(plain(page.dialog!.textContent)).toContain('Danach wird Studio getrennt')
    await page.inDialog('Abbrechen')
    expect(server.called('POST', '/api/v1/servers/2/transfer')).toBeUndefined()
  })
})
