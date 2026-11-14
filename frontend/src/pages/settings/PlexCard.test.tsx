/**
 * The Plex card: the steps, signing in through plex.tv (the window, the waiting, the servers, the library), the
 * address and token by hand, the catalogue with its numbers and its progress, the sonic analysis, and the two things
 * that delete (disconnect, another library) asking first.
 */
import type { CatalogStatus, PlexState } from '../../api/plex'
import { changeLanguage } from '../../i18n'
import { Page, Server, plain } from '../../test/harness'
import { until } from '../../test/wait'
import { PlexCard, pinTiming } from './PlexCard'

const server = new Server()
const page = new Page()

const NOT_CONNECTED: CatalogStatus = {
  connected: false, library: false, library_title: '', ready: false,
  counts: { artists: 0, albums: 0, tracks: 0, songs: 0, moods: 0 }, synced_at: null, full_at: null, running: null,
  queued: null, last: null, analysis: { state: 'unknown', percent: null }, nightly: '03:30',
}
const READY: CatalogStatus = {
  ...NOT_CONNECTED, connected: true, library: true, library_title: 'Music', ready: true,
  counts: { artists: 1234, albums: 4123, tracks: 52518, songs: 45457, moods: 288 },
  synced_at: '2026-11-13T14:44:00Z', full_at: '2026-11-13T14:44:00Z',
  last: { kind: 'full', reason: 'first', state: 'done', error: null, step: 'switch', progress: 1000, started_at: '2026-11-13T14:42:00Z', finished_at: '2026-11-13T14:44:00Z', artists: 1234, albums: 4123, tracks: 52518, songs: 45457, changed: 0, removed: 0 },
  analysis: { state: 'off', percent: null },
}
const CONNECTED: PlexState = {
  connected: true, via: 'plextv', server_name: 'Studio', owned: true, library: { key: '3', title: 'Music' }, connected_at: '2026-11-13T14:40:00Z',
}

type FakeWindow = { location: { href: string }; opener: unknown; close: () => void }
let opened: FakeWindow | null
let openCalls: unknown[][]

beforeEach(async () => {
  await changeLanguage('de', false)
  // One ask for the PIN every 200 ms: quick enough for the tests, and quiet long enough between two asks for the
  // page to settle (a test waits until no request is open).
  pinTiming.poll = 200
  opened = { location: { href: '' }, opener: window, close: vi.fn() }
  openCalls = []
  vi.stubGlobal('open', (...args: unknown[]) => {
    openCalls.push(args)
    return opened
  })
  server.answers = {
    'GET /api/v1/plex': { connected: false },
    'GET /api/v1/catalog/status': NOT_CONNECTED,
    'POST /api/v1/plex/pin': { id: '4001', url: 'https://app.plex.tv/auth#?clientID=nexmix-x&code=abc', seconds: 600 },
    'GET /api/v1/plex/pin/4001': { done: false, left: 590 },
    'POST /api/v1/plex/connect': { ...CONNECTED, libraries: [{ key: '3', title: 'Music' }] },
    'GET /api/v1/plex/libraries': { libraries: [{ key: '3', title: 'Music' }, { key: '7', title: 'Hörbücher' }] },
    'PUT /api/v1/plex/library': { ...CONNECTED, library: { key: '7', title: 'Hörbücher' } },
    'DELETE /api/v1/plex': 204,
    'POST /api/v1/catalog/sync': { started: true },
    'PUT /api/v1/settings': { catalog_full_at: '04:15' },
  }
  server.fails = {}
  server.install()
})
afterEach(() => {
  page.close()
  vi.unstubAllGlobals()
  pinTiming.poll = 2000
})

function steps(): string[] {
  return [...document.querySelectorAll('[data-testid="plex-steps"] .st')].map((step) => `${step.className.replace('st', '').trim() || 'done'}:${plain(step.textContent)}`)
}

function connectedAndReady(status: CatalogStatus = READY): void {
  server.answers['GET /api/v1/plex'] = CONNECTED
  server.answers['GET /api/v1/catalog/status'] = status
}

describe('before Plex is connected', () => {
  it('shows the three steps to come and the two ways in', async () => {
    await page.show(<PlexCard />)
    expect(document.querySelector('#plex h2')!.textContent).toBe('Plex')
    expect(steps()).toEqual(['todo:1Angemeldet über plex.tv', 'todo:2Server', 'todo:3Mediathek'])
    expect(page.button('Mit Plex verbinden')).toBeTruthy()
    expect(page.button('Adresse und Schlüssel von Hand')).toBeTruthy()
    expect(server.changes()).toEqual([])
  })

  it('signs in through plex.tv in a new window, waits, offers the servers and connects the one chosen', async () => {
    await page.show(<PlexCard />)
    await page.click(page.button('Mit Plex verbinden'))
    expect(server.changes()).toEqual(['POST /api/v1/plex/pin'])
    // The window was opened at the click, sent to Plex's page, and can reach nothing back here.
    expect(openCalls).toHaveLength(1)
    expect(opened!.location.href).toBe('https://app.plex.tv/auth#?clientID=nexmix-x&code=abc')
    expect(opened!.opener).toBeNull()
    expect(page.text).toContain('Melde dich im neuen Fenster bei Plex an und erlaube den Zugriff.')
    expect(steps()[0]).toBe('now:1Angemeldet über plex.tv')
    await until(() => server.calls.some((call) => call.url === '/api/v1/plex/pin/4001'), 'the first ask')
    expect(document.querySelector('[data-testid="plex-servers"]')).toBeNull()
    server.answers['GET /api/v1/plex/pin/4001'] = {
      done: true, offered: 2, servers: [{ index: 0, name: 'Studio', owned: true, libraries: [{ key: '3', title: 'Music' }] }],
    }
    await until(() => document.querySelector('[data-testid="plex-servers"]'), 'the servers')
    expect(steps()).toEqual(['done:✓Angemeldet über plex.tv', 'now:2Server', 'todo:3Mediathek'])
    expect(page.text).toContain('eigener Server · 1 Musikmediathek')
    await page.click(page.button('Wählen: Studio'))
    expect(server.called('POST', '/api/v1/plex/connect')!.body).toEqual({ pin_id: '4001', server: 0 })
    expect(steps()).toEqual(['done:✓Angemeldet über plex.tv', 'done:✓Server „Studio“', 'done:✓Mediathek „Music“'])
  })

  it('gives the sign-in up at the server too when the operator cancels', async () => {
    server.answers['DELETE /api/v1/plex/pin/4001'] = 204
    await page.show(<PlexCard />)
    await page.click(page.button('Mit Plex verbinden'))
    await page.click(page.button('Abbrechen'))
    await until(() => server.changes().includes('DELETE /api/v1/plex/pin/4001'), 'the sign-in given up')
    expect(server.changes()).toEqual(['POST /api/v1/plex/pin', 'DELETE /api/v1/plex/pin/4001'])
    expect(page.button('Mit Plex verbinden')).toBeTruthy()
    expect(steps()[0]).toBe('todo:1Angemeldet über plex.tv')
  })

  it('offers a link when the browser blocked the window', async () => {
    opened = null
    await page.show(<PlexCard />)
    await page.click(page.button('Mit Plex verbinden'))
    const link = document.querySelector<HTMLAnchorElement>('[data-testid="plex-waiting"] a')!
    expect(link.textContent).toBe('Bei Plex anmelden')
    expect(link.getAttribute('href')).toBe('https://app.plex.tv/auth#?clientID=nexmix-x&code=abc')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
    expect(link.getAttribute('target')).toBe('_blank')
  })

  it('says when the sign-in ran out and starts it again', async () => {
    server.fails['GET /api/v1/plex/pin/4001'] = { status: 404, code: 'pin_expired' }
    await page.show(<PlexCard />)
    await page.click(page.button('Mit Plex verbinden'))
    await until(() => page.text.includes('Die Anmeldung bei Plex ist abgelaufen.'), 'the end of the sign-in')
    expect(page.button('Mit Plex verbinden')).toBeTruthy()
  })

  it('says when plex.tv cannot be reached', async () => {
    server.fails['POST /api/v1/plex/pin'] = { status: 502, code: 'plextv_unreachable' }
    await page.show(<PlexCard />)
    await page.click(page.button('Mit Plex verbinden'))
    expect(page.text).toContain('plex.tv ist nicht erreichbar.')
    expect(opened!.close).toHaveBeenCalled()
  })

  it('connects by address and token, and forgets the token in the field', async () => {
    server.answers['POST /api/v1/plex/connect'] = { ...CONNECTED, via: 'manual', library: null, libraries: [{ key: '3', title: 'Music' }, { key: '7', title: 'Hörbücher' }] }
    server.answers['PUT /api/v1/plex/library'] = { ...CONNECTED, via: 'manual' }
    await page.show(<PlexCard />)
    await page.click(page.button('Adresse und Schlüssel von Hand'))
    const [address, token] = [...document.querySelectorAll<HTMLInputElement>('#plex input')]
    expect(token.type).toBe('password')
    expect(page.button('Verbinden')!.disabled).toBe(true)
    await page.type(address, 'http://plex:32400')
    await page.type(token, 'made-up-token')
    await page.click(page.button('Verbinden'))
    expect(server.called('POST', '/api/v1/plex/connect')!.body).toEqual({ url: 'http://plex:32400', token: 'made-up-token' })
    // Two libraries: the choice of the library, without a question (nothing is replaced yet).
    expect(document.querySelector('[data-testid="plex-libraries"]')).toBeTruthy()
    expect(steps()[0]).toBe('done:✓Adresse und Schlüssel')
    await page.click(page.button('Wählen: Music'))
    expect(page.dialog).toBeNull()
    expect(server.called('PUT', '/api/v1/plex/library')!.body).toEqual({ key: '3' })
  })

  it('says why connecting by hand failed', async () => {
    server.fails['POST /api/v1/plex/connect'] = { status: 502, code: 'plex_refused' }
    await page.show(<PlexCard />)
    await page.click(page.button('Adresse und Schlüssel von Hand'))
    const [address, token] = [...document.querySelectorAll<HTMLInputElement>('#plex input')]
    await page.type(address, 'http://plex:32400')
    await page.type(token, 'wrong')
    await page.click(page.button('Verbinden'))
    expect(page.text).toContain('Plex hat den Schlüssel abgelehnt. Verbinde Plex neu.')
  })
})

describe('once Plex is connected', () => {
  it('shows the catalogue as the design has it', async () => {
    connectedAndReady()
    await page.show(<PlexCard />)
    expect(steps()).toEqual(['done:✓Angemeldet über plex.tv', 'done:✓Server „Studio“', 'done:✓Mediathek „Music“'])
    const rows = [...document.querySelectorAll('#plex .kv dt')].map((dt) => `${dt.textContent}=${plain(dt.nextElementSibling!.textContent)}`)
    expect(rows[0]).toBe('Katalog=1.234 Künstler · 4.123 Alben · 52.518 Titel')
    expect(rows[1]).toMatch(/^Zuletzt abgeglichen=13\.11\.2026, \d\d:44 · danach alle 15 Minuten nur Neues$/)
    expect(rows[2]).toBe('Suche=lokal in nexmix, Plex wird beim Tippen nie gefragt')
    expect(rows[3]).toBe('Stimmungen=288 aus Plex (Titel, Album, Künstler)')
    expect(rows[4]).toBe('Ganzer Abgleich=jede Nacht um')
    expect([...document.querySelectorAll('#plex [data-testid="plex-connected"] button:not([role="switch"])')].map((button) => button.textContent)).toEqual([
      'Jetzt abgleichen', 'Andere Mediathek wählen', 'Plex trennen',
    ])
    expect(document.querySelector('[data-testid="plex-analysis"] b')!.textContent).toBe('Klanganalyse ist aus')
  })

  it('switches listening in off and on for everybody', async () => {
    connectedAndReady()
    server.answers['GET /api/v1/settings'] = { preview_allowed: true }
    server.answers['PUT /api/v1/settings'] = { preview_allowed: false }
    await page.show(<PlexCard />)
    const box = document.querySelector('[data-testid="preview-switch"]')!
    expect(plain(box.textContent)).toBe('Reinhören überhaupt erlaubenAus: niemand hört rein, auch keine Gäste. An: 30 Sekunden ab gut einem Drittel des Titels, über nexmix geleitet. Dein Plex-Schlüssel bleibt auf dem Server.')
    const button = box.querySelector<HTMLButtonElement>('[role="switch"]')!
    expect(button.getAttribute('aria-checked')).toBe('true')
    await page.click(button)
    expect(server.called('PUT', '/api/v1/settings')!.body).toEqual({ preview_allowed: false })
    expect(button.getAttribute('aria-checked')).toBe('false')
    // Refused by the server: the switch goes back and says why.
    server.calls = []
    server.fails['PUT /api/v1/settings'] = { status: 403, code: 'operator_network' }
    await page.click(button)
    expect(server.called('PUT', '/api/v1/settings')!.body).toEqual({ preview_allowed: true })
    expect(button.getAttribute('aria-checked')).toBe('false')
    expect(box.querySelector('[role="alert"]')).toBeTruthy()
  })

  it('shows the sonic analysis as Plex has it', async () => {
    connectedAndReady({ ...READY, analysis: { state: 'running', percent: 40 } })
    await page.show(<PlexCard />)
    expect(document.querySelector('[data-testid="plex-analysis"] b')!.textContent).toBe('Klanganalyse läuft, 40 % analysiert')
    page.close()
    connectedAndReady({ ...READY, analysis: { state: 'on', percent: 100 } })
    await page.show(<PlexCard />)
    expect(document.querySelector('[data-testid="plex-analysis"] b')!.textContent).toBe('Klanganalyse ist an')
  })

  it('shows the progress of the first sync', async () => {
    connectedAndReady({
      ...READY, ready: false, counts: NOT_CONNECTED.counts, synced_at: null, last: null,
      running: { ...READY.last!, state: 'running', step: 'tracks', progress: 427, finished_at: null },
    })
    await page.show(<PlexCard />)
    expect(plain(document.querySelector('[data-testid="plex-catalog"]')!.textContent)).toBe('wird geholt … 42 %')
    expect(document.querySelector('[role="progressbar"]')!.getAttribute('aria-valuenow')).toBe('42')
    expect(page.button('Jetzt abgleichen')!.disabled).toBe(true)
  })

  it('syncs now and says so', async () => {
    connectedAndReady()
    await page.show(<PlexCard />)
    await page.click(page.button('Jetzt abgleichen'))
    expect(server.changes()).toEqual(['POST /api/v1/catalog/sync'])
    expect(page.text).toContain('Der Abgleich läuft.')
  })

  it('says why the last sync failed and that the catalogue stays', async () => {
    connectedAndReady({ ...READY, last: { ...READY.last!, state: 'failed', error: 'plex_unreachable' } })
    await page.show(<PlexCard />)
    const alert = plain(document.querySelector('[data-testid="plex-failed"]')!.textContent)
    expect(alert).toMatch(/^Der letzte Abgleich ist gescheitert: Plex ist nicht erreichbar\. Der Katalog vom 13\.11\.2026, \d\d:44 bleibt, bis es wieder klappt\.$/)
  })

  it('asks before disconnecting, says what goes, and sends nothing without the yes', async () => {
    connectedAndReady()
    await page.show(<PlexCard />)
    await page.click(page.button('Plex trennen'))
    expect(page.dialog).toBeTruthy()
    expect(plain(page.dialog!.textContent)).toContain('nexmix vergisst den Schlüssel und den Katalog mit 52.518 Titeln.')
    expect(plain(page.dialog!.textContent)).toContain('Zurückholen lässt sich das nur durch neues Verbinden.')
    expect(server.changes()).toEqual([])
    await page.inDialog('Abbrechen')
    expect(server.changes()).toEqual([])
    await page.click(page.button('Plex trennen'))
    await page.inDialog('Plex trennen')
    expect(server.changes()).toEqual(['DELETE /api/v1/plex'])
    expect(page.button('Mit Plex verbinden')).toBeTruthy()
  })

  it('asks before another library replaces the catalogue', async () => {
    connectedAndReady()
    await page.show(<PlexCard />)
    await page.click(page.button('Andere Mediathek wählen'))
    expect(document.querySelector('[data-testid="plex-libraries"] .chip')!.textContent).toBe('gewählt')
    await page.click(page.button('Wählen: Hörbücher'))
    expect(plain(page.dialog!.textContent)).toContain('Der Katalog von „Music“ wird gelöscht und der von „Hörbücher“ neu geholt.')
    expect(server.changes()).toEqual([])
    await page.inDialog('Abbrechen')
    expect(server.changes()).toEqual([])
    await page.click(page.button('Wählen: Hörbücher'))
    await page.inDialog('Mediathek wechseln')
    expect(server.changes()).toEqual(['PUT /api/v1/plex/library'])
    expect(server.called('PUT', '/api/v1/plex/library')!.body).toEqual({ key: '7' })
  })

  it('keeps the time of the nightly sync', async () => {
    connectedAndReady()
    await page.show(<PlexCard />)
    const time = document.querySelector<HTMLInputElement>('input[type="time"]')!
    expect(time.value).toBe('03:30')
    expect(time.getAttribute('aria-label')).toBe('Uhrzeit des nächtlichen ganzen Abgleichs, leer für aus')
    await page.type(time, '04:15')
    expect(server.changes()).toEqual([])
    time.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
    await until(() => server.changes().length > 0, 'the save')
    expect(server.called('PUT', '/api/v1/settings')!.body).toEqual({ catalog_full_at: '04:15' })
  })

  it('speaks English as well, and never pauses with a dash', async () => {
    await changeLanguage('en', false)
    connectedAndReady({ ...READY, analysis: { state: 'running', percent: 3 } })
    await page.show(<PlexCard />)
    expect(steps()).toEqual(['done:✓Signed in through plex.tv', 'done:✓Server “Studio”', 'done:✓Library “Music”'])
    expect(page.text).toContain('1,234 artists · 4,123 albums · 52,518 tracks')
    expect(page.text).toContain('Sonic analysis is running, 3 % analysed')
    expect(document.querySelector('#plex')!.textContent).not.toMatch(/[–—]/)
  })
})
