/**
 * With two servers, the first Plex is disconnected from its own card while its tab is open: its tab leaves the second
 * row and "Take all over" stops offering it, without loading the page again (as for Jellyfin, Music Assistant and a
 * further Plex).
 */
import { Route, Routes } from 'react-router-dom'

import type { Me } from '../api/client'
import type { CatalogStatus } from '../api/plex'
import type { MediaServer } from '../api/servers'
import { changeLanguage } from '../i18n'
import { Page, Server, plain } from '../test/harness'
import { until } from '../test/wait'
import { SettingsPage } from './SettingsPage'

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

beforeEach(async () => {
  await changeLanguage('de', false)
  server.answers = {
    'GET /api/v1/settings': { public_url: 'https://mix.example.com', password_login: true, two_factor_required: false, backup_schedule: 'off', backup_keep: 7, guest_preview: true, guest_max: 10, guest_hours: 24 },
    'GET /api/v1/servers': { servers: [PLEX, JELLY] },
    'GET /api/v1/jellyfin': { servers: [{ ...JELLY, via: 'quick', library: { handle: 'aaaaaaaaaaaaaaaa', title: 'Musik' }, user: { handle: 'bbbbbbbbbbbbbbbb', name: 'Erika' }, connected_at: '2026-11-15T12:00:00Z' }] },
    'GET /api/v1/music-assistant': { servers: [] },
    'GET /api/v1/plex': { connected: true, via: 'plextv', server: 1, server_name: 'Studio', owned: true, library: { key: '3', title: 'Music' }, connected_at: '2026-11-13T14:40:00Z' },
    'DELETE /api/v1/plex': 204,
    'GET /api/v1/catalog/status': READY,
    'GET /api/v1/catalog/status?server=1': READY,
    'GET /api/v1/catalog/status?server=2': READY,
    'GET /api/v1/plex/playlists': { active: true, playlists: 0, synced: 0, pending: 0, waiting: 0, error: 0, smart: 0, polled_at: null, poll_error: null },
    'GET /api/v1/settings/readiness': { open: 0, points: [] },
  }
  server.fails = { 'GET /api/v1/storage': { status: 503, code: 'busy' } }
  server.install()
})
afterEach(() => {
  page.close()
  vi.unstubAllGlobals()
})

const row = () => [...document.querySelectorAll('[data-testid="sub-tabs"] [role="tab"]')].map((item) => plain(item.textContent).trim())

it('drops the tab of the first Plex once it is disconnected from its card', async () => {
  await page.show(
    <Routes>
      <Route path="/settings" element={<SettingsPage />} />
    </Routes>,
    '/settings?tab=servers&sub=1',
  )
  await until(() => row().length > 0, 'the second row')
  expect(row()[0]).toBe('Studio(Plex)')
  const card = document.getElementById('plex')!
  await until(() => page.button('Plex trennen', card), 'the button')
  // From now on the server knows only Jellyfin.
  server.answers['GET /api/v1/servers'] = { servers: [JELLY] }
  server.answers['GET /api/v1/plex'] = { connected: false }
  await page.click(page.button('Plex trennen', card)!)
  await page.inDialog('Plex trennen')
  expect(server.changes()).toEqual(['DELETE /api/v1/plex'])
  // One server left: no second row, and nothing named after the server let go of.
  await until(() => document.querySelector('[data-testid="sub-tabs"]') === null, 'the second row gone')
  expect(row()).not.toContain('Studio(Plex)')
})
