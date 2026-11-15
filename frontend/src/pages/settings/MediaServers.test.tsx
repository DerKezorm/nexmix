/**
 * The media servers in the settings: "Add a server" with Plex or Jellyfin, connecting Jellyfin by Quick Connect (the
 * code, the waiting, the token never in the browser) or an API key, the library and the user chosen, the Jellyfin card
 * with its catalogue and what Jellyfin cannot do, another Plex server beside the first, and disconnecting asking first.
 */
import type { CatalogStatus } from '../../api/plex'
import type { JellyfinServer, MediaServer } from '../../api/servers'
import { changeLanguage } from '../../i18n'
import { Page, Server, plain } from '../../test/harness'
import { until } from '../../test/wait'
import { quickTiming } from './JellyfinCard'
import { MediaServers } from './MediaServers'

const server = new Server()
const page = new Page()

const NOT_CONNECTED: CatalogStatus = {
  connected: false, library: false, library_title: '', ready: false,
  counts: { artists: 0, albums: 0, tracks: 0, songs: 0, moods: 0 }, synced_at: null, full_at: null, running: null,
  queued: null, last: null, analysis: { state: 'unknown', percent: null }, nightly: '03:30',
}
const JELLY_READY: CatalogStatus = {
  ...NOT_CONNECTED, connected: true, library: true, library_title: 'Musik', ready: true,
  counts: { artists: 7, albums: 9, tracks: 21, songs: 18, moods: 0 }, synced_at: '2026-11-15T12:00:00Z',
}
const CAPS_PLEX = { moods: true, sound: true, smart_playlists: true, duplicates: false, changes: 'both' as const, playlists: true }
const CAPS_JELLY = { moods: false, sound: false, smart_playlists: false, duplicates: true, changes: 'poll' as const, playlists: false }
const PLEX: MediaServer = { id: 1, kind: 'plex', name: 'Studio', active: true, library: 'Music', ready: true, capabilities: CAPS_PLEX, primary: true }
const JELLY: MediaServer = { id: 2, kind: 'jellyfin', name: 'Probe', active: true, library: 'Musik', ready: true, capabilities: CAPS_JELLY, primary: false }
const JELLY_VIEW: JellyfinServer = {
  id: 2, kind: 'jellyfin', name: 'Probe', active: true, ready: true, via: 'quick', library: { handle: 'aaaaaaaaaaaaaaaa', title: 'Musik' },
  user: { handle: 'bbbbbbbbbbbbbbbb', name: 'Erika' }, connected_at: '2026-11-15T12:00:00Z',
}
const USERS = [
  { handle: 'bbbbbbbbbbbbbbbb', name: 'Erika', admin: true },
  { handle: 'cccccccccccccccc', name: 'Jonas', admin: false },
]

beforeEach(async () => {
  await changeLanguage('de', false)
  quickTiming.poll = 200
  server.answers = {
    'GET /api/v1/servers': { servers: [PLEX] },
    'GET /api/v1/jellyfin': { servers: [] },
    'GET /api/v1/plex': { connected: true, via: 'plextv', server: 1, server_name: 'Studio', owned: true, library: { key: '3', title: 'Music' }, connected_at: '2026-11-13T14:40:00Z' },
    'GET /api/v1/catalog/status': NOT_CONNECTED,
    'GET /api/v1/catalog/status?server=1': { ...JELLY_READY, library_title: 'Music' },
    'GET /api/v1/catalog/status?server=2': JELLY_READY,
    'GET /api/v1/plex/playlists': { active: true, playlists: 0, synced: 0, pending: 0, waiting: 0, error: 0, smart: 0, polled_at: null, poll_error: null },
    'GET /api/v1/settings': { preview_allowed: true },
    'POST /api/v1/jellyfin/quick': { id: 'a1b2c3d4e5f6a1b2c3d4e5f6', code: '482913', name: 'Probe', seconds: 600 },
    'GET /api/v1/jellyfin/quick/a1b2c3d4e5f6a1b2c3d4e5f6': { done: false, left: 590 },
    'POST /api/v1/jellyfin/connect': { server: { ...JELLY_VIEW, user: null }, libraries: [{ handle: 'aaaaaaaaaaaaaaaa', title: 'Musik' }], users: USERS },
    'GET /api/v1/jellyfin/2/choices': { libraries: [{ handle: 'aaaaaaaaaaaaaaaa', title: 'Musik' }], users: USERS },
    'PUT /api/v1/jellyfin/2/user': JELLY_VIEW,
    'DELETE /api/v1/jellyfin/2': 204,
    'DELETE /api/v1/servers/3': 204,
    'POST /api/v1/catalog/sync?server=2': { started: true },
  }
  server.fails = {}
  server.install()
})
afterEach(() => {
  page.close()
  vi.unstubAllGlobals()
  quickTiming.poll = 2000
})

function card(id: string): HTMLElement {
  const found = document.getElementById(id)
  expect(found).toBeTruthy()
  return found!
}

describe('adding a server', () => {
  it('offers Plex and Jellyfin under the Plex card', async () => {
    await page.show(<MediaServers />)
    expect(document.getElementById('plex')).toBeTruthy()
    const add = card('servers-add')
    expect(add.querySelector('h2')!.textContent).toBe('Server hinzufügen')
    expect(page.button('Plex', add)).toBeTruthy()
    expect(page.button('Jellyfin', add)).toBeTruthy()
    expect(server.changes()).toEqual([])
  })

  it('connects Jellyfin by Quick Connect: the code, the waiting, then the user', async () => {
    await page.show(<MediaServers />)
    await page.click(page.button('Jellyfin', card('servers-add')))
    const box = card('jellyfin-new')
    expect(plain(box.textContent)).toContain('ein Passwort gibst du hier nicht ein')
    await page.type(box.querySelector('input'), 'http://jellyfin.example.com:8096')
    await page.click(page.button('Mit Quick Connect verbinden', box))
    expect(server.called('POST', '/api/v1/jellyfin/quick')!.body).toEqual({ url: 'http://jellyfin.example.com:8096' })
    expect(document.querySelector('[data-testid="jellyfin-code-value"]')!.textContent).toBe('482913')
    expect(page.text).toContain('Quick Connect und gib diesen Code ein')
    expect(page.text).toContain('Noch 10 Min. gültig')
    await until(() => server.calls.some((call) => call.url === '/api/v1/jellyfin/quick/a1b2c3d4e5f6a1b2c3d4e5f6'), 'the first ask')
    expect(server.called('POST', '/api/v1/jellyfin/connect')).toBeUndefined()
    // Confirmed in Jellyfin: nexmix connects, the new server shows its card and asks for the user.
    server.answers['GET /api/v1/jellyfin/quick/a1b2c3d4e5f6a1b2c3d4e5f6'] = { done: true, left: 580 }
    server.answers['GET /api/v1/servers'] = { servers: [PLEX, JELLY] }
    server.answers['GET /api/v1/jellyfin'] = { servers: [{ ...JELLY_VIEW, user: null }] }
    await until(() => document.querySelector('[data-testid="jellyfin-users"]'), 'the choice of the user')
    expect(server.called('POST', '/api/v1/jellyfin/connect')!.body).toEqual({ quick: 'a1b2c3d4e5f6a1b2c3d4e5f6' })
    expect(document.getElementById('jellyfin-new')).toBeNull()
    const steps = [...card('jellyfin-2').querySelectorAll('.st')].map((one) => plain(one.textContent))
    expect(steps).toEqual(['✓Quick Connect', '✓Mediathek „Musik“', '3Benutzer'])
    server.answers['GET /api/v1/jellyfin'] = { servers: [JELLY_VIEW] }
    await page.click(page.button('Wählen: Erika'))
    expect(server.called('PUT', '/api/v1/jellyfin/2/user')!.body).toEqual({ handle: 'bbbbbbbbbbbbbbbb' })
    await until(() => document.querySelector('[data-testid="jellyfin-connected"]'), 'the connected card')
    expect(plain(card('jellyfin-2').textContent)).toContain('7 Künstler · 9 Alben · 21 Titel')
  })

  it('says when the code ran out and gets a new one', async () => {
    server.fails['GET /api/v1/jellyfin/quick/a1b2c3d4e5f6a1b2c3d4e5f6'] = { status: 404, code: 'quick_expired' }
    await page.show(<MediaServers />)
    await page.click(page.button('Jellyfin', card('servers-add')))
    await page.type(card('jellyfin-new').querySelector('input'), 'http://jellyfin.example.com:8096')
    await page.click(page.button('Mit Quick Connect verbinden'))
    await until(() => page.text.includes('Der Code ist abgelaufen. Hol einen neuen.'), 'the code ran out')
    await page.click(page.button('Neuen Code holen'))
    expect(server.calls.filter((call) => call.method === 'POST' && call.url === '/api/v1/jellyfin/quick')).toHaveLength(2)
  })

  it('gives a Quick Connect up at the server too when the operator cancels', async () => {
    server.answers['DELETE /api/v1/jellyfin/quick/a1b2c3d4e5f6a1b2c3d4e5f6'] = 204
    await page.show(<MediaServers />)
    await page.click(page.button('Jellyfin', card('servers-add')))
    await page.type(card('jellyfin-new').querySelector('input'), 'http://jellyfin.example.com:8096')
    await page.click(page.button('Mit Quick Connect verbinden'))
    await page.click(page.button('Abbrechen', card('jellyfin-new')))
    await until(() => server.changes().includes('DELETE /api/v1/jellyfin/quick/a1b2c3d4e5f6a1b2c3d4e5f6'), 'given up')
  })

  it('connects Jellyfin with an API key by hand and never shows the key again', async () => {
    await page.show(<MediaServers />)
    await page.click(page.button('Jellyfin', card('servers-add')))
    await page.click(page.button('API-Schlüssel von Hand'))
    const inputs = card('jellyfin-new').querySelectorAll('input')
    await page.type(inputs[0], 'http://jellyfin.example.com:8096')
    expect(inputs[1].type).toBe('password')
    await page.type(inputs[1], 'made-up-key-for-the-test')
    await page.click(page.button('Verbinden', card('jellyfin-new')))
    expect(server.called('POST', '/api/v1/jellyfin/connect')!.body).toEqual({ url: 'http://jellyfin.example.com:8096', key: 'made-up-key-for-the-test' })
  })

  it('says why Jellyfin was not connected', async () => {
    server.fails['POST /api/v1/jellyfin/quick'] = { status: 409, code: 'quick_off' }
    await page.show(<MediaServers />)
    await page.click(page.button('Jellyfin', card('servers-add')))
    await page.type(card('jellyfin-new').querySelector('input'), 'http://jellyfin.example.com:8096')
    await page.click(page.button('Mit Quick Connect verbinden'))
    expect(page.text).toContain('Quick Connect ist in diesem Jellyfin abgeschaltet. Verbinde mit einem API-Schlüssel.')
  })

  it('connects another Plex server beside the first one', async () => {
    server.answers['POST /api/v1/plex/connect'] = { connected: true, via: 'manual', server: 1, server_name: 'Studio', owned: true, library: { key: '3', title: 'Music' }, connected_at: '2026-11-13T14:40:00Z', added: 3, libraries: [{ key: '5', title: 'Dachboden' }] }
    await page.show(<MediaServers />)
    await page.click(page.button('Plex', card('servers-add')))
    const box = card('plex-new')
    expect(box.querySelector('h2')!.textContent).toBe('Weiteren Plex-Server verbinden')
    await page.click(page.button('Adresse und Schlüssel von Hand', box))
    const inputs = box.querySelectorAll('input')
    await page.type(inputs[0], 'http://attic.example.com:32400')
    await page.type(inputs[1], 'made-up-token')
    await page.click(page.button('Verbinden', box))
    expect(server.called('POST', '/api/v1/plex/connect')!.body).toEqual({ url: 'http://attic.example.com:32400', token: 'made-up-token', alongside: true })
    await until(() => document.getElementById('plex-new') === null, 'the adding done')
  })
})

describe('the Jellyfin card', () => {
  beforeEach(() => {
    server.answers['GET /api/v1/servers'] = { servers: [PLEX, JELLY] }
    server.answers['GET /api/v1/jellyfin'] = { servers: [JELLY_VIEW] }
  })

  it('shows the catalogue, what Jellyfin cannot do, and how its playlists stand', async () => {
    server.answers['GET /api/v1/jellyfin/2/playlists'] = { active: true, kind: 'jellyfin', playlists: 3, synced: 2, pending: 0, waiting: 1, error: 0, smart: 0, order_minutes: 1, polled_at: '2026-11-15T12:00:00Z', poll_error: null }
    server.answers['POST /api/v1/jellyfin/2/playlists/sync'] = server.answers['GET /api/v1/jellyfin/2/playlists']
    await page.show(<MediaServers />)
    const box = card('jellyfin-2')
    await until(() => box.querySelector('[data-testid="jellyfin-playlists"]'), 'the playlists of Jellyfin')
    const text = plain(box.textContent)
    expect(text).toContain('Mit Jellyfin „Probe“ verbunden.')
    expect(text).toContain('7 Künstler · 9 Alben · 21 Titel')
    expect(text).toContain('gibt es bei Jellyfin nicht, Genre und Jahrzehnt gehen')
    expect(text).toContain('lokal in nexmix, Jellyfin wird beim Tippen nie gefragt')
    expect(text).not.toContain('Plex')
    expect(text).toContain('2 von 3 in Jellyfin gespeichert · 1 wartet auf Jellyfin')
    expect(plain(box.querySelector('[data-testid="jellyfin-playlists-looked"]')!.textContent)).toContain('danach alle 15 Sekunden')
    expect(text).not.toMatch(/[–—]/)
    await page.click(page.button('Jetzt abgleichen', box))
    expect(server.changes()).toEqual(['POST /api/v1/catalog/sync?server=2'])
    await page.click(page.button('Playlisten jetzt abgleichen', box))
    await until(() => server.called('POST', '/api/v1/jellyfin/2/playlists/sync'), 'synced the playlists')
  })

  it('says when several playlists are missing on the server and why one could not be looked at', async () => {
    server.answers['GET /api/v1/jellyfin/2/playlists'] = { active: true, kind: 'jellyfin', playlists: 3, synced: 1, pending: 0, waiting: 0, error: 2, smart: 0, order_minutes: 1, polled_at: '2026-11-15T12:00:00Z', poll_error: 'jellyfin_user_gone', missing: 2 }
    await page.show(<MediaServers />)
    const box = card('jellyfin-2')
    await until(() => box.querySelector('[data-testid="jellyfin-playlists"]'), 'the playlists of Jellyfin')
    const text = plain(box.querySelector('[data-testid="jellyfin-playlists"]')!.textContent)
    expect(text).toContain('2 Playlisten auf Jellyfin nicht gefunden. nexmix legt nichts in den Papierkorb und sieht weiter nach.')
    expect(plain(box.querySelector('[data-testid="jellyfin-playlists-looked"]')!.textContent)).toContain('Den Jellyfin-Benutzer der Playlisten gibt es nicht mehr. Verbinde Jellyfin neu')
    expect(plain(box.textContent)).not.toMatch(/[–—]/)
  })

  it('asks before disconnecting, says what goes, and sends nothing without the yes', async () => {
    await page.show(<MediaServers />)
    await page.click(page.button('Jellyfin trennen', card('jellyfin-2')))
    expect(page.dialog).toBeTruthy()
    const said = plain(page.dialog!.textContent)
    expect(said).toContain('nexmix vergisst den Schlüssel und den Katalog mit 21 Titeln.')
    expect(said).toContain('Zurückholen lässt sich das nur durch neues Verbinden.')
    await page.inDialog('Abbrechen')
    expect(server.changes()).toEqual([])
    await page.click(page.button('Jellyfin trennen', card('jellyfin-2')))
    server.answers['GET /api/v1/jellyfin'] = { servers: [] }
    server.answers['GET /api/v1/servers'] = { servers: [PLEX] }
    await page.inDialog('Jellyfin trennen')
    expect(server.changes()).toEqual(['DELETE /api/v1/jellyfin/2'])
    await until(() => document.getElementById('jellyfin-2') === null, 'the card gone')
  })

  it('shows another Plex server with its own card that asks before it disconnects', async () => {
    server.answers['GET /api/v1/servers'] = { servers: [PLEX, { ...PLEX, id: 3, name: 'Dachboden', primary: false, library: 'Alt' }] }
    server.answers['GET /api/v1/jellyfin'] = { servers: [] }
    server.answers['GET /api/v1/catalog/status?server=3'] = { ...JELLY_READY, counts: { ...JELLY_READY.counts, tracks: 900 } }
    await page.show(<MediaServers />)
    const box = card('plex-3')
    expect(plain(box.querySelector('h2')!.textContent)).toBe('Plex „Dachboden“')
    await page.click(page.button('Server trennen', box))
    expect(plain(page.dialog!.textContent)).toContain('Katalog mit 900 Titeln')
    await page.inDialog('Abbrechen')
    expect(server.changes()).toEqual([])
    await page.click(page.button('Server trennen', box))
    await page.inDialog('Server trennen')
    expect(server.changes()).toEqual(['DELETE /api/v1/servers/3'])
  })
})
