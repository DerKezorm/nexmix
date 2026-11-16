/**
 * The server part of the settings in tabs, as in the other nex apps: the row of round tabs in its order, each tab with
 * only its cards, the tab in the address (an old name such as `?tab=plex` and an unknown one lead to the right tab, and
 * the address says so), "Ready for the internet?" above the sign-in, and the second row of the media servers: a tab per
 * server with its kind as a sign, "Add a server" and "Take all over", none with one server.
 */
import { Route, Routes, useLocation } from 'react-router-dom'

import type { Me } from '../api/client'
import type { CatalogStatus } from '../api/plex'
import type { MediaServer } from '../api/servers'
import { changeLanguage } from '../i18n'
import { Page, Server, plain } from '../test/harness'
import { SettingsPage } from './SettingsPage'
import { SERVER_TABS, serverTab } from './settings/tabs'

const me = { id: 1, name: 'jule', display_name: 'Jule', role: 'operator', profile: { theme: 'system' } } as unknown as Me
const auth = { status: 'signedIn', me: me as Me | null, setMe: vi.fn(), refresh: vi.fn(async () => undefined), signOut: vi.fn(async () => undefined) }
vi.mock('../state/auth', () => ({ useAuth: () => auth }))

const server = new Server()
const page = new Page()

const READY: CatalogStatus = {
  connected: true, library: true, library_title: 'Music', ready: true,
  counts: { artists: 3, albums: 4, tracks: 21, songs: 20, moods: 0 }, synced_at: '2026-11-15T12:00:00Z', full_at: null, running: null,
  queued: null, last: null, analysis: { state: 'unknown', percent: null }, nightly: '03:30',
}
const CAPS = { moods: true, sound: true, smart_playlists: true, duplicates: false, changes: 'both' as const, playlists: true }
const PLEX: MediaServer = { id: 1, kind: 'plex', name: 'Studio', active: true, library: 'Music', ready: true, capabilities: CAPS, primary: true }
const JELLY: MediaServer = { id: 2, kind: 'jellyfin', name: 'Probe', active: true, library: 'Musik', ready: true, capabilities: { ...CAPS, moods: false }, primary: false }
const MA: MediaServer = { id: 3, kind: 'music_assistant', name: 'Wohnzimmer', active: true, library: 'Musik', ready: true, capabilities: { ...CAPS, moods: false }, primary: false }

function Where() {
  const location = useLocation()
  return <output data-testid="where">{location.pathname + location.search}</output>
}
const where = () => document.querySelector('[data-testid="where"]')!.textContent
const app = () => (
  <>
    <Routes>
      <Route path="/settings" element={<SettingsPage />} />
      <Route path="/account" element={<p data-testid="account">account</p>} />
    </Routes>
    <Where />
  </>
)
const row = (testid: string) => [...document.querySelectorAll(`[data-testid="${testid}"] [role="tab"]`)].map((item) => [plain(item.textContent).trim(), item.getAttribute('aria-selected')])
const tabIn = (testid: string, name: string) => [...document.querySelectorAll<HTMLButtonElement>(`[data-testid="${testid}"] [role="tab"]`)].find((item) => plain(item.textContent).trim().startsWith(name))
const titles = () => [...document.querySelectorAll('.settings .card > h2')].map((item) => plain(item.textContent).trim())

beforeEach(async () => {
  await changeLanguage('de', false)
  auth.me = me
  server.answers = {
    'GET /api/v1/settings': { public_url: 'https://mix.example.com', password_login: true, two_factor_required: false, backup_schedule: 'off', backup_keep: 7, guest_preview: true, guest_max: 10, guest_hours: 24 },
    'GET /api/v1/servers': { servers: [PLEX] },
    'GET /api/v1/jellyfin': { servers: [] },
    'GET /api/v1/music-assistant': { servers: [] },
    'GET /api/v1/plex': { connected: true, via: 'plextv', server: 1, server_name: 'Studio', owned: true, library: { key: '3', title: 'Music' }, connected_at: '2026-11-13T14:40:00Z' },
    'GET /api/v1/catalog/status': READY,
    'GET /api/v1/catalog/status?server=1': READY,
    'GET /api/v1/catalog/status?server=2': READY,
    'GET /api/v1/catalog/status?server=3': READY,
    'GET /api/v1/plex/playlists': { active: true, playlists: 0, synced: 0, pending: 0, waiting: 0, error: 0, smart: 0, polled_at: null, poll_error: null },
    'GET /api/v1/settings/readiness': { open: 0, points: [] },
    'GET /api/v1/accounts': [],
    'GET /api/v1/invites': [],
    'GET /api/v1/oidc/admin/links': [],
    'GET /api/v1/oidc/admin/providers': [],
    'GET /api/v1/oidc/authentik/address': { public_url: 'https://mix.example.com', seen: 'https://mix.example.com', stale: false },
    'GET /api/v1/backups': [],
  }
  server.fails = { 'GET /api/v1/storage': { status: 503, code: 'busy' } }
  server.install()
})
afterEach(() => {
  page.close()
  vi.unstubAllGlobals()
})

describe('the tabs of the server', () => {
  it('stand in one row in their order, each with its sign, Media servers first', async () => {
    await page.show(app(), '/settings')
    expect(row('tabs')).toEqual([
      ['Medienserver', 'true'], ['Anmeldung', 'false'], ['Konten', 'false'], ['Gäste', 'false'], ['Mail', 'false'],
      ['Web Push', 'false'], ['Sicherung', 'false'], ['Sprachen', 'false'], ['Log', 'false'], ['API', 'false'],
    ])
    expect(document.querySelectorAll('[data-testid="tabs"] [role="tab"] svg').length).toBe(10)
    // Only the media servers: the Plex card and "Add a server", nothing of the sign-in or the accounts.
    expect(document.getElementById('plex')).toBeTruthy()
    expect(document.getElementById('servers-add')).toBeTruthy()
    expect(document.getElementById('accounts')).toBeNull()
    expect(document.querySelector('.settings[role="tabpanel"]')!.getAttribute('aria-label')).toBe('Medienserver')
  })

  it('show only the cards of the tab chosen, and the address names it', async () => {
    await page.show(app(), '/settings')
    await page.click(tabIn('tabs', 'Konten'))
    expect(where()).toBe('/settings?tab=accounts')
    expect(document.getElementById('accounts')).toBeTruthy()
    expect(document.getElementById('plex')).toBeNull()
    await page.click(tabIn('tabs', 'Gäste'))
    expect(where()).toBe('/settings?tab=guests')
    expect(titles()).toEqual(['Gäste'])
    await page.click(tabIn('tabs', 'Sicherung'))
    expect(titles()).toContain('Sicherung')
    expect(titles()).not.toContain('Gäste')
  })

  it('put "Ready for the internet?" above the sign-in, as in the other nex apps', async () => {
    await page.show(app(), '/settings?tab=signin')
    const all = titles()
    expect(all[0]).toBe('Bereit fürs Internet?')
    expect(all.length).toBeGreaterThan(1)
    await page.click(tabIn('tabs', 'Medienserver'))
    expect(titles()).not.toContain('Bereit fürs Internet?')
  })

  it('lead an old name to the tab that holds it now, and an unknown one to the first, and say so in the address', async () => {
    await page.show(app(), '/settings?tab=plex')
    expect(row('tabs')[0]).toEqual(['Medienserver', 'true'])
    expect(where()).toBe('/settings?tab=servers')
    page.close()
    for (const nonsense of ['nonsense', 'constructor', '__proto__', 'toString']) {
      await page.show(app(), `/settings?tab=${nonsense}`)
      expect(row('tabs')[0], nonsense).toEqual(['Medienserver', 'true'])
      expect(where(), nonsense).toBe('/settings?tab=servers')
      page.close()
    }
    await page.show(app(), '/settings?tab=mail')
    expect(row('tabs')[4]).toEqual(['Mail', 'true'])
    expect(where()).toBe('/settings?tab=mail')
  })

  it('name every tab the way the address does', () => {
    for (const tab of SERVER_TABS) expect(serverTab(tab)).toBe(tab)
    expect(serverTab('plex')).toBe('servers')
    expect(serverTab(null)).toBe('servers')
    expect(serverTab('hasOwnProperty')).toBe('servers')
  })

  it('are no place for a member, who is sent to the account', async () => {
    auth.me = { ...me, role: 'member' } as Me
    await page.show(app(), '/settings?tab=accounts')
    expect(document.querySelector('[data-testid="account"]')).toBeTruthy()
    expect(document.querySelector('[data-testid="tabs"]')).toBeNull()
  })

  it('say the tabs in English too', async () => {
    await changeLanguage('en', false)
    await page.show(app(), '/settings')
    expect(row('tabs').map(([name]) => name)).toEqual(['Media servers', 'Sign-in', 'Accounts', 'Guests', 'Mail', 'Web Push', 'Backup', 'Languages', 'Log', 'API'])
  })
})

describe('the second row of the media servers', () => {
  it('is not there with one server: its card and "Add a server" one below the other', async () => {
    await page.show(app(), '/settings?tab=servers')
    expect(document.querySelector('[data-testid="sub-tabs"]')).toBeNull()
    expect(document.getElementById('plex')).toBeTruthy()
    expect(document.getElementById('servers-add')).toBeTruthy()
  })

  it('has a tab per server with its kind, "Add a server" and "Take all over", and shows one at a time', async () => {
    server.answers['GET /api/v1/servers'] = { servers: [PLEX, JELLY, MA] }
    server.answers['GET /api/v1/jellyfin'] = { servers: [{ ...JELLY, via: 'quick', library: { handle: 'aaaaaaaaaaaaaaaa', title: 'Musik' }, user: { handle: 'bbbbbbbbbbbbbbbb', name: 'Erika' }, connected_at: '2026-11-15T12:00:00Z' }] }
    server.answers['GET /api/v1/music-assistant'] = { servers: [{ ...MA, token: { expires_at: '2027-11-16T08:00:00Z', days_left: 365, warn: false, expired: false }, connected_at: '2026-11-16T08:00:00Z', providers: 1, doubled: [] }] }
    await page.show(app(), '/settings?tab=servers')
    expect(row('sub-tabs')).toEqual([
      ['Studio(Plex)', 'true'], ['Probe(Jellyfin)', 'false'], ['Wohnzimmer(Music Assistant)', 'false'], ['Server hinzufügen', 'false'], ['Alles übernehmen', 'false'],
    ])
    const signs = [...document.querySelectorAll('[data-testid="sub-tabs"] [role="tab"]')].map((item) => item.getAttribute('title'))
    expect(signs).toEqual(['Plex', 'Jellyfin', 'Music Assistant', null, null])
    expect(document.querySelectorAll('[data-testid="sub-tabs"] [role="tab"] svg').length).toBe(5)
    // The first server: only its card.
    expect(document.getElementById('plex')).toBeTruthy()
    expect(document.getElementById('jellyfin-2')).toBeNull()
    expect(document.getElementById('servers-add')).toBeNull()
    await page.click(tabIn('sub-tabs', 'Probe'))
    expect(where()).toBe('/settings?tab=servers&sub=2')
    expect(document.getElementById('jellyfin-2')).toBeTruthy()
    expect(document.getElementById('plex')).toBeNull()
    await page.click(tabIn('sub-tabs', 'Server hinzufügen'))
    expect(where()).toBe('/settings?tab=servers&sub=add')
    expect(document.getElementById('servers-add')).toBeTruthy()
    expect(document.getElementById('jellyfin-2')).toBeNull()
    await page.click(tabIn('sub-tabs', 'Alles übernehmen'))
    expect(where()).toBe('/settings?tab=servers&sub=transfer')
    expect(document.querySelectorAll('[data-testid="transfer-card"], [id^="transfer-"]').length).toBeGreaterThan(0)
    expect(document.getElementById('servers-add')).toBeNull()
  })

  it('opens the server the address names, and the first for a server that is not there', async () => {
    server.answers['GET /api/v1/servers'] = { servers: [PLEX, MA] }
    server.answers['GET /api/v1/music-assistant'] = { servers: [{ ...MA, token: { expires_at: '2027-11-16T08:00:00Z', days_left: 365, warn: false, expired: false }, connected_at: '2026-11-16T08:00:00Z', providers: 1, doubled: [] }] }
    await page.show(app(), '/settings?tab=servers&sub=3')
    expect(row('sub-tabs')[1]).toEqual(['Wohnzimmer(Music Assistant)', 'true'])
    expect(document.getElementById('ma-3')).toBeTruthy()
    page.close()
    await page.show(app(), '/settings?tab=servers&sub=99')
    expect(row('sub-tabs')[0]).toEqual(['Studio(Plex)', 'true'])
    expect(document.getElementById('plex')).toBeTruthy()
  })

  it('offers no "Take all over" where no server can take playlists', async () => {
    server.answers['GET /api/v1/servers'] = { servers: [PLEX, { ...JELLY, ready: false }] }
    await page.show(app(), '/settings?tab=servers')
    expect(row('sub-tabs').map(([name]) => name)).toEqual(['Studio(Plex)', 'Probe(Jellyfin)', 'Server hinzufügen', 'Alles übernehmen'])
    page.close()
    server.answers['GET /api/v1/servers'] = { servers: [{ ...PLEX, ready: false }, { ...JELLY, ready: false }] }
    await page.show(app(), '/settings?tab=servers')
    expect(row('sub-tabs').map(([name]) => name)).not.toContain('Alles übernehmen')
  })
})
