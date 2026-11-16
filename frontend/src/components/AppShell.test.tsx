/**
 * The frame and the playlists: the head with the mark, the areas in their order (the active one marked; settings only
 * for the operator), the account menu with the picture and the name, the round button for light and dark; below it the
 * view with its title, its line and its empty sentence. The search field of the head is not there: no field without an
 * effect.
 */
import { Route, Routes } from 'react-router-dom'

import type { Me } from '../api/client'
import { changeLanguage } from '../i18n'
import { PlaylistsPage } from '../pages/PlaylistsPage'
import { Page, Server } from '../test/harness'
import { AppShell } from './AppShell'

const me = {
  id: 1, name: 'jule', display_name: 'Jule', role: 'operator', avatar: null, version: '0.1.0', whats_new_seen: '0.1.0',
  profile: { theme: 'system' },
} as unknown as Me
const auth = { me: me as Me | null, signOut: async () => undefined, refresh: async () => undefined }
vi.mock('../state/auth', () => ({ useAuth: () => auth }))

const server = new Server()
const page = new Page()

function app() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<PlaylistsPage />} />
        <Route path="settings" element={<p>settings</p>} />
        <Route path="account" element={<p>account</p>} />
        <Route path="about" element={<p>about</p>} />
      </Route>
    </Routes>
  )
}

beforeEach(async () => {
  const kept = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => kept.get(key) ?? null,
    setItem: (key: string, value: string) => void kept.set(key, value),
    removeItem: (key: string) => void kept.delete(key),
  })
  vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener: () => undefined, removeEventListener: () => undefined }))
  document.documentElement.removeAttribute('data-theme')
  await changeLanguage('de', false)
  auth.me = me
  server.answers = {
    'PUT /api/v1/me/preferences': { theme: 'dark' },
    'GET /api/v1/catalog/status': {
      connected: false, library: false, library_title: '', ready: false,
      counts: { artists: 0, albums: 0, tracks: 0, songs: 0, moods: 0 }, synced_at: null, full_at: null, running: null,
      queued: null, last: null, analysis: { state: 'unknown', percent: null }, nightly: '03:30',
    },
  }
  server.fails = {}
  server.install()
})
afterEach(() => {
  page.close()
  vi.unstubAllGlobals()
})

function tabs(): { text: string; href: string | null; on: boolean }[] {
  return [...document.querySelectorAll('.tabs a')].map((item) => ({ text: item.textContent?.trim() ?? '', href: item.getAttribute('href'), on: item.classList.contains('on') }))
}

describe('the head', () => {
  it('holds the mark, the areas in their order, the account and the button of light and dark', async () => {
    await page.show(app(), '/')
    expect(document.querySelector('header.top')).toBeTruthy()
    expect(document.querySelector('.brand .mark')).toBeTruthy()
    expect(document.querySelector('.wordmark')!.textContent).toBe('NEXMIX')
    expect(tabs()).toEqual([
      { text: 'Playlisten', href: '/', on: true },
      { text: 'Einstellungen', href: '/settings', on: false },
    ])
    // The account first (picture and name), then the button of light and dark.
    const end = [...document.querySelectorAll('.top-end > *')]
    expect(end.length).toBe(2)
    expect(end[0].querySelector('button[aria-haspopup]')).toBeTruthy()
    expect(end[1].classList.contains('icon-btn')).toBe(true)
    expect(document.querySelector('.top button.me .me-name')!.textContent).toBe('Jule')
    // No search field: there is nothing to search yet.
    expect(document.querySelector('.top input')).toBeNull()
  })

  it('leaves the settings to the operator: a member has the playlists only', async () => {
    auth.me = { ...me, role: 'member' } as Me
    await page.show(app(), '/')
    expect(tabs()).toEqual([{ text: 'Playlisten', href: '/', on: true }])
  })

  it('marks the area that is open, and only that one', async () => {
    await page.show(app(), '/settings')
    expect(tabs().map((tab) => tab.on)).toEqual([false, true])
  })

  it('keeps the settings marked on My account, their other part, and on no page outside them', async () => {
    await page.show(app(), '/account?tab=security')
    expect(tabs().map((tab) => tab.on)).toEqual([false, true])
    page.close()
    await page.show(app(), '/about')
    expect(tabs().map((tab) => tab.on)).toEqual([false, false])
  })

  it('speaks English too', async () => {
    await changeLanguage('en', false)
    await page.show(app(), '/')
    expect(tabs().map((tab) => tab.text)).toEqual(['Playlists', 'Settings'])
  })
})

describe('the button of light and dark', () => {
  it('switches the page, keeps the choice in this browser and in the account, and says which theme is on', async () => {
    await page.show(app(), '/')
    const button = document.querySelector<HTMLElement>('.top .icon-btn')!
    expect(button.getAttribute('aria-label')).toBe('Hell oder dunkel')
    expect(button.querySelector('svg')!.getAttribute('class')).toContain('lucide-sun')
    await page.click(button)
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(localStorage.getItem('nexmix.theme')).toBe('dark')
    expect(server.called('PUT', '/api/v1/me/preferences')!.body).toEqual({ theme: 'dark' })
    expect(button.querySelector('svg')!.getAttribute('class')).toContain('lucide-moon')
    await page.click(button)
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
    expect(server.calls.filter((call) => call.url === '/api/v1/me/preferences').at(-1)!.body).toEqual({ theme: 'light' })
  })
})

describe('the playlists', () => {
  it('say what is not there yet, and the operator is shown where to connect Plex', async () => {
    await page.show(app(), '/')
    expect(document.querySelector('h1')!.textContent).toBe('Playlisten')
    expect(document.querySelector('.lead p')!.textContent).toBe('Gemeinsam gebaut aus deiner Musikmediathek.')
    const empty = document.querySelector('[data-testid="playlists-empty"]')!
    expect(empty.textContent).toBe('Noch kein Medienserver verbunden. Verbinde unter Einstellungen Plex, Jellyfin oder Music Assistant.')
    expect(empty.querySelector('a')!.getAttribute('href')).toBe('/settings?tab=servers')
    expect(empty.className).toBe('empty')
  })

  it('tell a member that Plex is not there yet, with no link to the settings', async () => {
    auth.me = { ...me, role: 'member' } as Me
    await page.show(app(), '/')
    const empty = document.querySelector('[data-testid="playlists-empty"]')!
    expect(empty.textContent).toBe('Der Betreiber hat noch keinen Medienserver verbunden. Sobald die Mediathek da ist, geht es hier los.')
    expect(empty.querySelector('a')).toBeNull()
  })

  it('come in English with the same sentences', async () => {
    await changeLanguage('en', false)
    await page.show(app(), '/')
    expect(document.querySelector('[data-testid="playlists-empty"]')!.textContent).toBe('No media server connected yet. Connect Plex, Jellyfin or Music Assistant under Settings.')
    expect(document.querySelector('.lead p')!.textContent).toBe('Built together from your music library.')
  })

  it('name the kind of the first server, where the playlists end up, once its catalogue is there', async () => {
    await changeLanguage('en', false)
    const ready = (kind: string, title: string) => ({
      kind, connected: true, library: true, library_title: title, ready: true,
      counts: { artists: 3, albums: 4, tracks: 1234, songs: 1200, moods: 0 }, synced_at: null, full_at: null, running: null,
      queued: null, last: null, analysis: { state: 'off', percent: null }, nightly: '03:30',
    })
    server.answers['GET /api/v1/playlists'] = { playlists: [], trash_days: 30, show_smart_playlists: false }
    server.answers['GET /api/v1/playlists?trash=true'] = { playlists: [], trash_days: 30 }
    const seen: string[] = []
    for (const [kind, title] of [['plex', 'Music'], ['jellyfin', 'Musik'], ['music_assistant', 'Files']]) {
      server.answers['GET /api/v1/catalog/status'] = ready(kind, title)
      await page.show(app(), '/')
      seen.push((document.querySelector('.lead p')!.textContent ?? '').replace(/[⁨⁩]/g, ''))
      page.close()
    }
    expect(seen).toEqual([
      '1,234 tracks from your Plex library “Music” · everything ends up as a playlist in Plex, and so in Plexamp and Music Assistant too.',
      '1,234 tracks from your Jellyfin library “Musik” · everything ends up as a playlist in Jellyfin.',
      '1,234 tracks from Music Assistant (Files) · everything ends up as a playlist in Music Assistant.',
    ])
  })

  it('have no button without an effect: the new playlist comes with the playlists', async () => {
    await page.show(app(), '/')
    expect([...document.querySelectorAll('main button')].map((button) => button.textContent)).toEqual([])
  })

  it('never use a dash as a pause', async () => {
    await page.show(app(), '/')
    expect(document.querySelector('main')!.textContent).not.toMatch(/[–—]/)
  })
})
