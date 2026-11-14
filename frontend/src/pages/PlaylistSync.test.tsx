/**
 * The playlists' sync with Plex in the interface: the cloud on the cards and the smart mark, the line in the head of a
 * playlist (saved, being saved after a change, waiting for Plex, failed with "try again", "sync now"), the events of
 * the sync, the note "sorted in Plex at the same time" with its undo, a smart playlist only shown, the question before
 * deleting that says what happens in Plex, the trash row of one deleted in Plex, and the row of the Plex card.
 */
import { act } from 'react'
import { Route, Routes } from 'react-router-dom'

import type { Me } from '../api/client'
import type { Card, Change, Entry, Playlist, PlexSync } from '../api/playlists'
import type { CatalogStatus } from '../api/plex'
import { changeLanguage } from '../i18n'
import { editorTiming } from '../lib/editorTiming'
import { Page, Server, plain } from '../test/harness'
import { until } from '../test/wait'
import { PlaylistEditor } from './PlaylistEditor'
import { PlaylistsPage } from './PlaylistsPage'
import { PlexCard } from './settings/PlexCard'

const me = { id: 1, name: 'jule', display_name: 'Jule', role: 'operator' } as unknown as Me
vi.mock('../state/auth', () => ({ useAuth: () => ({ me }) }))

const fs = (await import(/* @vite-ignore */ ['node', 'fs'].join(':'))) as unknown as { readFileSync(path: string, encoding: 'utf-8'): string }
const CATALOG = JSON.parse(fs.readFileSync('../backend/tests/fixtures/search/catalog.json', 'utf-8'))

const server = new Server()
const page = new Page()
const jule = { id: 1, name: 'jule', display_name: 'Jule', avatar: null }

const STATUS: CatalogStatus = {
  connected: true, library: true, library_title: 'Music', ready: true,
  counts: { artists: 6, albums: 10, tracks: 30, songs: 23, moods: 20 }, synced_at: null, full_at: null, running: null,
  queued: null, last: null, analysis: { state: 'off', percent: null }, nightly: '03:30', search: 'browser',
}

const sync = (over: Partial<PlexSync> = {}): PlexSync => ({
  state: 'synced', error: null, smart: false, total: null, synced_at: '2026-11-13T10:00:00Z', gone_in_plex: false, note: null, ...over,
})

const entry = (id: number, track: number, title: string, over: Partial<Entry> = {}): Entry => ({
  id, track, title, artist: 'Someone', album: 'Some Album', album_id: 201, cover: true, year: 1984, duration: 200,
  song: track, missing: false, added_by: jule, added_by_name: 'Jule', added_at: '2026-11-13T10:00:00Z', ...over,
})

function playlist(over: Partial<Playlist> = {}): Playlist {
  return {
    id: 7, name: 'Grillabend', description: '', owner: jule, access: 'owner', shares: 0, version: 5,
    created_at: '2026-11-13T10:00:00Z', updated_at: '2026-11-13T10:00:00Z', deleted_at: null, purge_at: null, days_left: null,
    count: 3, duration: 600, covers: [201], people: [jule], contributors: 1, moods: [],
    items: [entry(1, 301, 'Northern Lights'), entry(2, 311, 'Straße der Träume'), entry(3, 317, 'Señorita Lluvia')],
    smart: false, plex: sync(), ...over,
  }
}

/** The browser's EventSource, played by the test: `emit` hands an event to the open stream. */
class FakeSource {
  static last: FakeSource | null = null
  static CLOSED = 2
  readyState = 1
  listeners: Record<string, ((event: MessageEvent) => void)[]> = {}
  onerror: (() => void) | null = null
  constructor(public url: string) {
    FakeSource.last = this
  }
  addEventListener(name: string, listener: (event: MessageEvent) => void): void {
    ;(this.listeners[name] ??= []).push(listener)
  }
  close(): void {
    this.readyState = 2
  }
  emit(name: string, data: unknown): void {
    for (const listener of this.listeners[name] ?? []) listener({ data: JSON.stringify(data) } as MessageEvent)
  }
}

beforeEach(async () => {
  await changeLanguage('de', false)
  editorTiming.rename = 0
  FakeSource.last = null
  server.answers = {
    'GET /api/v1/catalog/status': STATUS,
    'GET /api/v1/catalog': CATALOG,
    'GET /api/v1/playlists/7': playlist(),
    'POST /api/v1/playlists/7/sync': sync({ state: 'pending' }),
  }
  server.fails = {}
  server.install()
})
afterEach(() => {
  page.close()
  vi.unstubAllGlobals()
})

async function open(withEvents = false): Promise<void> {
  if (withEvents) vi.stubGlobal('EventSource', FakeSource)
  await page.show(
    <Routes>
      <Route path="/playlists/:id" element={<PlaylistEditor />} />
      <Route path="/" element={<p data-testid="overview">overview</p>} />
    </Routes>,
    '/playlists/7',
  )
  await until(() => document.querySelector('[data-testid="playlist-head"]'))
}

const state = () => plain(document.querySelector('[data-testid="sync-state"]')?.textContent)

describe('the head of a playlist', () => {
  it('says it is saved in Plex, and "sync now" asks the server at once', async () => {
    await open()
    expect(state()).toBe('In Plex gespeichert')
    await page.click(page.button('Jetzt mit Plex abgleichen'))
    expect(server.changes()).toEqual(['POST /api/v1/playlists/7/sync'])
    expect(state()).toBe('Wird in Plex gespeichert …')
  })

  it('says "being saved" right after a change', async () => {
    const added: Change = { version: 6, op: 'add', entries: [entry(4, 302, 'Glasshouse')], order: [1, 2, 3, 4] }
    server.answers['POST /api/v1/playlists/7/items'] = added
    await open()
    await until(() => document.querySelector('[data-testid="search-track"]'))
    const add = document.querySelector<HTMLButtonElement>('[data-testid="search-track"] button.add')!
    await page.click(add)
    expect(state()).toBe('Wird in Plex gespeichert …')
  })

  it('waits for Plex and says the changes stay', async () => {
    server.answers['GET /api/v1/playlists/7'] = playlist({ plex: sync({ state: 'waiting', error: 'plex_unreachable' }) })
    await open()
    expect(state()).toBe('Wartet auf Plex. Deine Änderungen bleiben hier und werden nachgeschrieben.')
  })

  it('says why it failed and tries again on the button', async () => {
    server.answers['GET /api/v1/playlists/7'] = playlist({ plex: sync({ state: 'error', error: 'plex_refused' }) })
    await open()
    expect(state()).toBe('Nicht in Plex gespeichert: Plex hat den Schlüssel abgelehnt. Verbinde Plex neu.')
    await page.click(page.button('Erneut versuchen'))
    expect(server.changes()).toEqual(['POST /api/v1/playlists/7/sync'])
  })

  it('shows nothing about Plex while Plex is not connected', async () => {
    server.answers['GET /api/v1/playlists/7'] = playlist({ plex: sync({ state: 'off' }) })
    await open()
    expect(document.querySelector('[data-testid="sync-state"]')).toBeNull()
  })

  it('follows the events of the sync: its state, and what came from Plex with the name', async () => {
    await open(true)
    const source = FakeSource.last!
    await act(async () => source.emit('plex', sync({ state: 'waiting', error: 'plex_timeout' })))
    expect(state()).toContain('Wartet auf Plex')
    const fromPlex = entry(9, 320, 'Northern Lights', { added_by: null, added_by_name: 'Plex', from_plex: true, missing: true, artist: 'Dax Orlo', album: 'Northern Lights' })
    await act(async () => source.emit('items', { version: 6, op: 'sync', by: null, entries: [fromPlex], order: [1, 2, 3, 9], name: 'Umbenannt in Plex', removed: [] }))
    expect(document.querySelector<HTMLInputElement>('input.name')!.value).toBe('Umbenannt in Plex')
    const rows = [...document.querySelectorAll('[data-testid="track-row"]')]
    expect(rows.length).toBe(4)
    // What Plex says of a track the catalogue does not know yet, not "no longer in the library".
    expect(plain(rows[3].textContent)).toContain('Dax Orlo')
    expect(plain(rows[3].textContent)).not.toContain('Nicht mehr in der Mediathek')
    expect(plain(rows[3].textContent)).toContain('Plex')
    // A change from Plex is no change to be written: the state stays as the sync said.
    expect(state()).toContain('Wartet auf Plex')
    await act(async () => source.emit('plex', sync()))
    expect(state()).toBe('In Plex gespeichert')
  })
})

describe('the note "sorted in Plex at the same time"', () => {
  beforeEach(() => {
    server.answers['GET /api/v1/playlists/7'] = playlist({ plex: sync({ note: { at: '2026-11-13T10:00:00Z', count: 3 } }) })
  })

  it('says what happened, and the undo takes Plex\'s order', async () => {
    server.answers['POST /api/v1/playlists/7/plex-order'] = { version: 6, op: 'move', entries: [], order: [3, 1, 2], moved: true }
    await open()
    const note = document.querySelector('[data-testid="plex-note"]')!
    expect(plain(note.textContent)).toContain('Gleichzeitig in Plex umsortiert')
    expect(plain(note.textContent)).toContain('Rückgängig übernimmt die Reihenfolge aus Plex.')
    await page.click(page.button('Rückgängig', note))
    expect(server.changes()).toEqual(['POST /api/v1/playlists/7/plex-order'])
    expect([...document.querySelectorAll('[data-testid="track-row"] .t')].map((one) => one.textContent)).toEqual(['Señorita Lluvia', 'Northern Lights', 'Straße der Träume'])
    expect(document.querySelector('[data-testid="plex-note"]')).toBeNull()
  })

  it('goes when the order here is kept', async () => {
    server.answers['DELETE /api/v1/playlists/7/plex-order'] = sync()
    await open()
    await page.click(page.button('Behalten'))
    expect(server.changes()).toEqual(['DELETE /api/v1/playlists/7/plex-order'])
    expect(document.querySelector('[data-testid="plex-note"]')).toBeNull()
  })
})

describe('a smart playlist of Plex', () => {
  it('is only shown: no adding, no taking out, no deleting, and says how much of it shows', async () => {
    server.answers['GET /api/v1/playlists/7'] = playlist({ smart: true, count: 52731, plex: sync({ smart: true, total: 52731 }) })
    await open()
    await until(() => document.querySelector('[data-testid="search-track"]'))
    expect(plain(document.querySelector('[data-testid="smart-chip"]')!.textContent)).toBe('Smart-Playlist aus Plex, nur lesen')
    expect(document.querySelector('input.name')).toBeNull()
    expect(document.querySelectorAll('[data-testid="track-row"] .rm').length).toBe(0)
    expect(document.querySelectorAll('[data-testid="search-track"] button.add').length).toBe(0)
    expect(page.button('Löschen')).toBeUndefined()
    expect(plain(document.querySelector('[data-testid="smart-shown"]')!.textContent)).toBe('Zu sehen sind die ersten 3 von 52.731 Titeln.')
  })
})

describe('deleting', () => {
  it('says that it goes from Plex at once and comes back there, and sends nothing without the answer', async () => {
    await open()
    await page.click(page.button('Löschen'))
    expect(plain(page.dialog!.textContent)).toContain('In Plex verschwindet sie sofort, Wiederherstellen legt sie dort neu an.')
    await page.inDialog('Abbrechen')
    expect(server.changes()).toEqual([])
  })
})

describe('in English', () => {
  it('says the same without dashes', async () => {
    await changeLanguage('en', false)
    server.answers['GET /api/v1/playlists/7'] = playlist({ plex: sync({ state: 'error', error: 'plex_unreachable', note: { at: '2026-11-13T10:00:00Z', count: 3 } }) })
    await open()
    expect(state()).toBe('Not saved in Plex: Plex cannot be reached.')
    const text = plain(document.querySelector('[data-testid="playlist-head"]')!.textContent)
    expect(text).toContain('Sorted in Plex at the same time')
    expect(text).not.toMatch(/[–—]/)
  })
})

// --- The overview ------------------------------------------------------------------------------------------------------

const card = (over: Partial<Card>): Card => {
  const { items: _items, moods: _moods, ...rest } = playlist()
  return { ...rest, ...over }
}

describe('the cards of the overview', () => {
  beforeEach(() => {
    server.answers['GET /api/v1/playlists?trash=true'] = { playlists: [], trash_days: 30 }
  })

  it('carry the cloud of how each stands in Plex, and the smart mark', async () => {
    server.answers['GET /api/v1/playlists'] = {
      playlists: [
        card({ id: 1, name: 'Da' }),
        card({ id: 2, name: 'Unterwegs', plex: sync({ state: 'pending' }) }),
        card({ id: 3, name: 'Wartet', plex: sync({ state: 'waiting', error: 'plex_unreachable' }) }),
        card({ id: 4, name: 'Kaputt', plex: sync({ state: 'error', error: 'plex_error' }) }),
        card({ id: 5, name: 'Alle Titel', smart: true, plex: sync({ smart: true, total: 52731 }) }),
        card({ id: 6, name: 'Ohne Plex', plex: sync({ state: 'off' }) }),
      ],
      trash_days: 30,
      // The smart playlists of Plex are in the overview only for an account that asked for them.
      show_smart_playlists: true,
    }
    await page.show(<PlaylistsPage />)
    const cards = [...document.querySelectorAll('[data-testid="playlist-card"]')]
    expect(cards.map((one) => plain(one.querySelector('[data-testid="sync-mark"]')?.textContent))).toEqual(['In Plex', 'Wird gespeichert …', 'Wartet auf Plex', 'Nicht in Plex', 'In Plex', ''])
    expect(cards.map((one) => Boolean(one.querySelector('[data-testid="smart-badge"]')))).toEqual([false, false, false, false, true, false])
  })

  it('say in the trash which one was deleted in Plex', async () => {
    server.answers['GET /api/v1/playlists'] = { playlists: [], trash_days: 30 }
    server.answers['GET /api/v1/playlists?trash=true'] = { playlists: [card({ id: 5, name: 'Weg', deleted_at: '2026-11-01T10:00:00Z', days_left: 18, plex: sync({ gone_in_plex: true }) })], trash_days: 30 }
    await page.show(<PlaylistsPage />)
    expect(plain(document.querySelector('[data-testid="trash-row"] small')!.textContent)).toBe('Gelöscht am 01.11.2026 · noch 18 Tage · In Plex gelöscht')
  })
})

// --- The Plex card -----------------------------------------------------------------------------------------------------

describe('the Plex card', () => {
  beforeEach(() => {
    server.answers['GET /api/v1/plex'] = { connected: true, via: 'plextv', server_name: 'Studio', owned: true, library: { key: '3', title: 'Music' }, connected_at: '2026-11-13T14:40:00Z' }
    server.answers['GET /api/v1/catalog/status'] = { ...STATUS, synced_at: '2026-11-13T14:44:00Z' }
  })

  it('shows how the playlists stand in Plex and syncs them all on the button', async () => {
    const overview = { active: true, playlists: 7, synced: 4, pending: 0, waiting: 1, error: 1, smart: 1, polled_at: '2026-11-13T14:45:00Z', poll_error: null }
    server.answers['GET /api/v1/plex/playlists'] = overview
    server.answers['POST /api/v1/plex/playlists/sync'] = overview
    await page.show(<PlexCard />)
    await until(() => document.querySelector('[data-testid="plex-playlists"]'))
    expect(plain(document.querySelector('[data-testid="plex-playlists"]')!.textContent)).toContain('4 von 7 in Plex gespeichert · 1 wartet auf Plex · 1 mit Fehler · 1 Smart-Playlist (nur lesen)')
    expect(plain(document.querySelector('[data-testid="plex-playlists-looked"]')!.textContent)).toContain('danach alle 15 Sekunden')
    await page.click(page.button('Playlisten jetzt abgleichen'))
    expect(server.changes()).toEqual(['POST /api/v1/plex/playlists/sync'])
  })

  it('says when Plex was not reached at the last look', async () => {
    server.answers['GET /api/v1/plex/playlists'] = { active: true, playlists: 0, synced: 0, pending: 0, waiting: 0, error: 0, smart: 0, polled_at: '2026-11-13T14:45:00Z', poll_error: 'plex_unreachable' }
    await page.show(<PlexCard />)
    await until(() => document.querySelector('[data-testid="plex-playlists"]'))
    expect(plain(document.querySelector('[data-testid="plex-playlists"]')!.textContent)).toContain('Noch keine. Playlisten aus Plex erscheinen hier von selbst.')
    expect(plain(document.querySelector('[data-testid="plex-playlists-looked"]')!.textContent)).toContain('Plex nicht erreicht: Plex ist nicht erreichbar.')
  })
})

// --- Only in nexmix, sync now, and a smart one brought back ----------------------------------------------------------

describe('entries only in nexmix', () => {
  it('say when Plex does not take the track, or the server has no such track', async () => {
    server.answers['GET /api/v1/playlists/7'] = playlist({
      items: [entry(1, 301, 'Northern Lights'), entry(2, 311, 'Straße der Träume', { plex_hint: 'refused' }), entry(3, 317, 'Señorita Lluvia', { plex_hint: 'elsewhere', missing: true })],
    })
    await open()
    const rows = [...document.querySelectorAll('[data-testid="track-row"] .s')].map((one) => plain(one.textContent))
    expect(rows[1]).toBe('Plex nimmt diesen Titel nicht an')
    expect(rows[2]).toBe('Auf diesem Plex-Server nicht gefunden')
    expect(rows[0]).not.toContain('Plex')
  })
})

describe('sync now', () => {
  it('is not offered to whoever may only listen', async () => {
    server.answers['GET /api/v1/playlists/7'] = playlist({ access: 'read' })
    await open()
    expect(state()).toBe('In Plex gespeichert')
    expect(document.querySelector('[aria-label="Jetzt mit Plex abgleichen"]')).toBeNull()
  })

  it('and "try again" neither', async () => {
    server.answers['GET /api/v1/playlists/7'] = playlist({ access: 'read', plex: sync({ state: 'error', error: 'plex_refused' }) })
    await open()
    expect([...document.querySelectorAll('button')].some((one) => plain(one.textContent) === 'Erneut versuchen')).toBe(false)
  })
})

describe('a smart playlist deleted in Plex', () => {
  beforeEach(() => {
    server.answers['GET /api/v1/playlists'] = { playlists: [], trash_days: 30 }
  })

  it('says that it comes back as an ordinary one, and sends nothing without the answer', async () => {
    server.answers['GET /api/v1/playlists?trash=true'] = { playlists: [card({ id: 5, name: 'Klug', smart: true, count: 42, deleted_at: '2026-11-01T10:00:00Z', days_left: 18, plex: sync({ smart: true, gone_in_plex: true }) })], trash_days: 30 }
    server.answers['POST /api/v1/playlists/5/restore'] = card({ id: 5, name: 'Klug' })
    await page.show(<PlaylistsPage />)
    await page.click(page.button('Wiederherstellen'))
    expect(plain(page.dialog!.textContent)).toContain('Sie kommt als gewöhnliche Playlist mit ihren letzten 42 Titeln zurück')
    await page.inDialog('Abbrechen')
    expect(server.changes()).toEqual([])
    await page.click(page.button('Wiederherstellen'))
    await page.inDialog('Wiederherstellen')
    expect(server.changes()).toEqual(['POST /api/v1/playlists/5/restore'])
  })

  it('an ordinary one comes back without a question', async () => {
    server.answers['GET /api/v1/playlists?trash=true'] = { playlists: [card({ id: 5, name: 'Weg', deleted_at: '2026-11-01T10:00:00Z', days_left: 18, plex: sync({ gone_in_plex: true }) })], trash_days: 30 }
    server.answers['POST /api/v1/playlists/5/restore'] = card({ id: 5, name: 'Weg' })
    await page.show(<PlaylistsPage />)
    await page.click(page.button('Wiederherstellen'))
    expect(page.dialog).toBeNull()
    expect(server.changes()).toEqual(['POST /api/v1/playlists/5/restore'])
  })
})

describe('the Plex card with many playlists', () => {
  beforeEach(() => {
    server.answers['GET /api/v1/plex'] = { connected: true, via: 'plextv', server_name: 'Studio', owned: true, library: { key: '3', title: 'Music' }, connected_at: '2026-11-13T14:40:00Z' }
    server.answers['GET /api/v1/catalog/status'] = { ...STATUS, synced_at: '2026-11-13T14:44:00Z' }
  })

  it('says how long a new order in Plex takes at most', async () => {
    server.answers['GET /api/v1/plex/playlists'] = { active: true, playlists: 200, synced: 200, pending: 0, waiting: 0, error: 0, smart: 0, order_minutes: 5, polled_at: '2026-11-13T14:45:00Z', poll_error: null }
    await page.show(<PlexCard />)
    await until(() => document.querySelector('[data-testid="plex-playlists-looked"]'))
    expect(plain(document.querySelector('[data-testid="plex-playlists-looked"]')!.textContent)).toContain('in Plex Umsortiertes spätestens nach 5 Minuten')
  })
})
