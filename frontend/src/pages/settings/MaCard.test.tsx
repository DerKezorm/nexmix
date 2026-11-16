/**
 * Music Assistant in the settings: connecting it by address and token (the token never shown again), the providers
 * chosen (a Plex provider beside Plex named as doubled), the card with its catalogue, what Music Assistant cannot do,
 * when its token runs out, its playlists, and disconnecting asking first.
 */
import type { CatalogStatus } from '../../api/plex'
import type { MaServer, MediaServer } from '../../api/servers'
import { changeLanguage } from '../../i18n'
import { Page, Server, plain } from '../../test/harness'
import { until } from '../../test/wait'
import { MediaServers } from './MediaServers'

const server = new Server()
/** A token typed in the test, made up at run time. */
const TYPED = ['made', 'up', Math.random().toString(36).slice(2)].join('-')
const page = new Page()

const NOT_CONNECTED: CatalogStatus = {
  connected: false, library: false, library_title: '', ready: false,
  counts: { artists: 0, albums: 0, tracks: 0, songs: 0, moods: 0 }, synced_at: null, full_at: null, running: null,
  queued: null, last: null, analysis: { state: 'unknown', percent: null }, nightly: '03:30',
}
const READY: CatalogStatus = {
  ...NOT_CONNECTED, connected: true, library: true, library_title: 'Musik', ready: true,
  counts: { artists: 5, albums: 12, tracks: 33, songs: 30, moods: 0 }, synced_at: '2026-11-15T12:00:00Z',
}
const CAPS_PLEX = { moods: true, sound: true, smart_playlists: true, duplicates: false, changes: 'both' as const, playlists: true }
const CAPS_MA = { moods: false, sound: false, smart_playlists: false, duplicates: false, changes: 'events' as const, playlists: true }
const PLEX: MediaServer = { id: 1, kind: 'plex', name: 'Studio', active: true, library: 'Music', ready: true, capabilities: CAPS_PLEX, primary: true }
const MA: MediaServer = { id: 3, kind: 'music_assistant', name: 'Wohnzimmer', active: true, library: 'Musik', ready: true, capabilities: CAPS_MA, primary: false }
const TOKEN = { expires_at: '2027-11-16T08:00:00Z', days_left: 365, warn: false, expired: false }
const MA_VIEW: MaServer = {
  id: 3, kind: 'music_assistant', name: 'Wohnzimmer', active: true, library: 'Musik', ready: true, token: TOKEN,
  connected_at: '2026-11-16T08:00:00Z', providers: 1, doubled: [],
}
const PROVIDERS = [
  { handle: 'aaaaaaaaaaaaaaaa', name: 'Musik', kind: 'filesystem_local', chosen: false, available: true },
  { handle: 'bbbbbbbbbbbbbbbb', name: 'Plex', kind: 'plex', chosen: false, available: true },
]

beforeEach(async () => {
  await changeLanguage('de', false)
  server.answers = {
    'GET /api/v1/servers': { servers: [PLEX] },
    'GET /api/v1/jellyfin': { servers: [] },
    'GET /api/v1/music-assistant': { servers: [] },
    'GET /api/v1/plex': { connected: true, via: 'plextv', server: 1, server_name: 'Studio', owned: true, library: { key: '3', title: 'Music' }, connected_at: '2026-11-13T14:40:00Z' },
    'GET /api/v1/catalog/status': NOT_CONNECTED,
    'GET /api/v1/catalog/status?server=1': { ...READY, library_title: 'Music' },
    'GET /api/v1/catalog/status?server=3': READY,
    'GET /api/v1/plex/playlists': { active: true, playlists: 0, synced: 0, pending: 0, waiting: 0, error: 0, smart: 0, polled_at: null, poll_error: null },
    'GET /api/v1/settings': { preview_allowed: true },
    'POST /api/v1/music-assistant/connect': { server: { ...MA_VIEW, library: null, providers: 0, ready: false }, providers: PROVIDERS },
    'GET /api/v1/music-assistant/3/providers': { providers: PROVIDERS, doubled: [] },
    'PUT /api/v1/music-assistant/3/providers': { server: MA_VIEW, providers: PROVIDERS },
    'GET /api/v1/music-assistant/3/playlists': { active: true, kind: 'music_assistant', playlists: 2, synced: 2, pending: 0, waiting: 0, error: 0, smart: 0, order_minutes: 1, polled_at: '2026-11-16T08:00:00Z', poll_error: null },
    'DELETE /api/v1/music-assistant/3': 204,
  }
  server.fails = {}
  server.install()
})
afterEach(() => {
  page.close()
  vi.unstubAllGlobals()
})

function card(id: string): HTMLElement {
  const found = document.getElementById(id)
  expect(found).toBeTruthy()
  return found!
}

describe('connecting Music Assistant', () => {
  it('takes the address and a token, then asks for the providers', async () => {
    await page.show(<MediaServers />)
    await page.click(page.button('Music Assistant', card('servers-add')))
    const box = card('ma-new')
    expect(plain(box.textContent)).toContain('langlebigen Token')
    const inputs = box.querySelectorAll('input')
    expect(inputs[1].type).toBe('password')
    await page.type(inputs[0], 'http://ma.example.com:8095')
    await page.type(inputs[1], TYPED)
    server.answers['GET /api/v1/servers'] = { servers: [PLEX, { ...MA, library: null, ready: false }] }
    server.answers['GET /api/v1/music-assistant'] = { servers: [{ ...MA_VIEW, library: null, providers: 0, ready: false }] }
    await page.click(page.button('Verbinden', box))
    expect(server.called('POST', '/api/v1/music-assistant/connect')!.body).toEqual({ url: 'http://ma.example.com:8095', token: TYPED })
    await until(() => document.querySelector('[data-testid="ma-providers"]'), 'the choice of the providers')
    expect(document.getElementById('ma-new')).toBeNull()
    expect(page.text).not.toContain(TYPED)
    const choice = document.querySelector('[data-testid="ma-providers"]') as HTMLElement
    const boxes = choice.querySelectorAll('input[type="checkbox"]')
    expect(boxes).toHaveLength(2)
    expect(page.button('Anbieter übernehmen', choice)!.disabled).toBe(true)
    await page.click(boxes[0] as HTMLElement)
    server.answers['GET /api/v1/music-assistant'] = { servers: [MA_VIEW] }
    await page.click(page.button('Anbieter übernehmen', choice))
    expect(server.called('PUT', '/api/v1/music-assistant/3/providers')!.body).toEqual({ handles: ['aaaaaaaaaaaaaaaa'] })
    await until(() => document.querySelector('[data-testid="ma-connected"]'), 'the connected card')
  })

  it('says why Music Assistant was not connected', async () => {
    server.fails['POST /api/v1/music-assistant/connect'] = { status: 422, code: 'bad_token' }
    await page.show(<MediaServers />)
    await page.click(page.button('Music Assistant', card('servers-add')))
    const inputs = card('ma-new').querySelectorAll('input')
    await page.type(inputs[0], 'http://ma.example.com:8095')
    await page.type(inputs[1], 'x')
    await page.click(page.button('Verbinden', card('ma-new')))
    expect(page.text).toContain('Der Schlüssel fehlt oder enthält Leerzeichen.')
  })
})

describe('the Music Assistant card', () => {
  beforeEach(() => {
    server.answers['GET /api/v1/servers'] = { servers: [PLEX, MA] }
    server.answers['GET /api/v1/music-assistant'] = { servers: [MA_VIEW] }
  })

  it('shows the catalogue, the token, what Music Assistant cannot do, and its playlists', async () => {
    await page.show(<MediaServers />)
    const box = card('ma-3')
    await until(() => box.querySelector('[data-testid="music_assistant-playlists"]'), 'the playlists')
    const text = plain(box.textContent)
    expect(text).toContain('Mit Music Assistant „Wohnzimmer“ verbunden.')
    expect(text).toContain('5 Künstler · 12 Alben · 33 Titel')
    expect(text).toContain('gibt es bei Music Assistant nicht, Genre und Jahrzehnt gehen')
    expect(text).toContain('Music Assistant wird beim Tippen nie gefragt')
    expect(text).toContain('gültig bis')
    expect(text).toContain('2 von 2 in Music Assistant gespeichert')
    expect(plain(box.querySelector('[data-testid="music_assistant-playlists-looked"]')!.textContent)).toContain('jede Minute')
    expect(box.querySelector('[data-testid="ma-token"]')).toBeNull()
    expect(text).not.toMatch(/[–—]/)
  })

  it('warns 30 days before the token runs out, and says when it ran out', async () => {
    server.answers['GET /api/v1/music-assistant'] = { servers: [{ ...MA_VIEW, token: { ...TOKEN, days_left: 12, warn: true } }] }
    await page.show(<MediaServers />)
    await until(() => document.querySelector('[data-testid="ma-token"]'), 'the warning')
    expect(plain(document.querySelector('[data-testid="ma-token"]')!.textContent)).toContain('Der Token läuft in 12 Tagen ab')
    page.close()
    server.answers['GET /api/v1/music-assistant'] = { servers: [{ ...MA_VIEW, token: { ...TOKEN, days_left: 0, warn: true, expired: true } }] }
    await page.show(<MediaServers />)
    await until(() => document.querySelector('[data-testid="ma-token"]'), 'the alert')
    expect(plain(document.querySelector('[data-testid="ma-token"]')!.textContent)).toContain('abgelaufen')
  })

  it('names a provider that brings the tracks twice', async () => {
    server.answers['GET /api/v1/music-assistant'] = { servers: [{ ...MA_VIEW, doubled: ['Plex'] }] }
    await page.show(<MediaServers />)
    await until(() => document.querySelector('[data-testid="ma-doubled"]'), 'the hint')
    expect(plain(document.querySelector('[data-testid="ma-doubled"]')!.textContent)).toContain('Plex bringt Titel, die nexmix auch direkt kennt')
  })

  it('asks before other providers replace the catalogue, and sends nothing without', async () => {
    await page.show(<MediaServers />)
    const box = card('ma-3')
    await until(() => box.querySelector('[data-testid="ma-connected"]'), 'the card')
    await page.click(page.button('Andere Anbieter wählen', box))
    await until(() => box.querySelector('[data-testid="ma-providers"]'), 'the choice')
    await page.click(box.querySelectorAll('input[type="checkbox"]')[1] as HTMLElement)
    await page.click(page.button('Anbieter übernehmen', box))
    expect(page.text).toContain('Andere Anbieter wählen?')
    await page.click(page.button('Abbrechen', document.querySelector('[role="dialog"]') as HTMLElement))
    expect(server.called('PUT', '/api/v1/music-assistant/3/providers')).toBeUndefined()
  })

  it('asks before disconnecting and says what goes', async () => {
    await page.show(<MediaServers />)
    const box = card('ma-3')
    await until(() => box.querySelector('[data-testid="ma-connected"]'), 'the card')
    await page.click(page.button('Music Assistant trennen', box))
    expect(page.text).toContain('nexmix vergisst den Token und den Katalog mit 33 Titeln')
    expect(server.changes()).toEqual([])
    await page.click(page.button('Music Assistant trennen', document.querySelector('[role="dialog"]') as HTMLElement))
    await until(() => server.changes().includes('DELETE /api/v1/music-assistant/3'), 'disconnected')
  })
})
