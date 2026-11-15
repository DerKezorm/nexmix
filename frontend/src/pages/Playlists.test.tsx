/**
 * The playlists page: while the catalogue comes the progress of the first sync, a first sync that failed says why (the
 * operator gets the way to the card), no button before the catalogue is there. Then the cards (mosaic, count and
 * length, people, the mark of a shared one), "New playlist" once per click, and the trash with its question before
 * anything is deleted for good. What the account hid from its overview, and the smart playlists of Plex unless it asks
 * for them, are left out; both are its own view, kept on the server, and come back in one click.
 */
import { act } from 'react'

import type { Me } from '../api/client'
import type { CatalogStatus } from '../api/plex'
import { changeLanguage } from '../i18n'
import { Page, Server, plain } from '../test/harness'
import { until } from '../test/wait'
import { LOOK_MS, PlaylistsPage } from './PlaylistsPage'

const me = { id: 1, name: 'jule', display_name: 'Jule', role: 'operator' } as unknown as Me
const auth = {
  me: me as Me | null,
  setMe: (next: Me) => {
    auth.me = next
  },
}
vi.mock('../state/auth', () => ({ useAuth: () => auth }))

const server = new Server()
const page = new Page()

const BASE: CatalogStatus = {
  connected: true, library: true, library_title: 'Music', ready: false,
  counts: { artists: 0, albums: 0, tracks: 0, songs: 0, moods: 0 }, synced_at: null, full_at: null, running: null,
  queued: null, last: null, analysis: { state: 'unknown', percent: null }, nightly: '03:30',
}
const RUNNING = { kind: 'full', reason: 'first', state: 'running', error: null, step: 'tracks', progress: 333, started_at: '2026-11-13T14:42:00Z', finished_at: null, artists: 0, albums: 0, tracks: 0, songs: 0, changed: 0, removed: 0 } as const

const NONE = { playlists: [], trash_days: 30 }

beforeEach(async () => {
  await changeLanguage('de', false)
  auth.me = me
  server.answers = { 'GET /api/v1/catalog/status': BASE, 'GET /api/v1/playlists': NONE, 'GET /api/v1/playlists?trash=true': NONE }
  server.fails = {}
  server.install()
})
afterEach(() => {
  page.close()
  vi.unstubAllGlobals()
})

describe('the playlists while the catalogue comes', () => {
  it('show the progress of the first sync', async () => {
    server.answers['GET /api/v1/catalog/status'] = { ...BASE, running: RUNNING }
    await page.show(<PlaylistsPage />)
    const box = document.querySelector('[data-testid="playlists-loading"]')!
    expect(plain(box.textContent)).toContain('Katalog wird geholt …')
    expect(box.querySelector('[role="progressbar"]')!.getAttribute('aria-valuenow')).toBe('33')
    expect(document.querySelectorAll('main button, button').length).toBe(0)
  })

  it('show the waiting sync the same way', async () => {
    server.answers['GET /api/v1/catalog/status'] = { ...BASE, queued: 'full' }
    await page.show(<PlaylistsPage />)
    expect(document.querySelector('[data-testid="playlists-loading"]')).toBeTruthy()
  })

  it('say "no playlist yet" once the catalogue is there, with its numbers above', async () => {
    server.answers['GET /api/v1/catalog/status'] = { ...BASE, ready: true, counts: { artists: 5, albums: 11, tracks: 52518, songs: 45457, moods: 288 } }
    await page.show(<PlaylistsPage />)
    expect(plain(document.querySelector('.lead p')!.textContent)).toBe(
      '52.518 Titel aus deiner Plex-Mediathek „Music“ · alles landet als Playlist in Plex und ist damit auch in Plexamp und Music Assistant.',
    )
    const none = document.querySelector('[data-testid="playlists-none"]')!
    expect(plain(none.textContent)).toBe('Noch keine PlaylistDer Katalog ist da: 52.518 Titel aus „Music“. Hier erscheinen die Playlisten, die ihr gemeinsam baut.')
    // Now there is something to make: the button in the head and the dashed card.
    expect([...document.querySelectorAll('button')].map((button) => plain(button.textContent).trim())).toEqual(['Neue Playlist', '+Neue Playlist'])
  })

  it('say why the first sync failed, and lead the operator to the card', async () => {
    server.answers['GET /api/v1/catalog/status'] = { ...BASE, last: { ...RUNNING, state: 'failed', error: 'plex_refused', finished_at: '2026-11-13T14:43:00Z' } }
    await page.show(<PlaylistsPage />)
    const failed = document.querySelector('[data-testid="playlists-failed"]')!
    expect(plain(failed.textContent)).toBe('Der Katalog konnte nicht geholt werden: Plex hat den Schlüssel abgelehnt. Verbinde Plex neu. Mehr unter Einstellungen.')
    expect(failed.querySelector('a')!.getAttribute('href')).toBe('/settings?tab=plex')
  })

  it('tell a member without links', async () => {
    auth.me = { ...me, role: 'member' } as Me
    server.answers['GET /api/v1/catalog/status'] = { ...BASE, last: { ...RUNNING, state: 'failed', error: 'plex_refused' } }
    await page.show(<PlaylistsPage />)
    expect(plain(document.querySelector('[data-testid="playlists-failed"]')!.textContent)).toBe('Der Katalog konnte noch nicht geholt werden. Der Betreiber sieht in den Einstellungen nach.')
    expect(document.querySelector('a')).toBeNull()
  })

  it('come in English too', async () => {
    await changeLanguage('en', false)
    server.answers['GET /api/v1/catalog/status'] = { ...BASE, ready: true, counts: { artists: 5, albums: 11, tracks: 1200, songs: 1000, moods: 20 } }
    await page.show(<PlaylistsPage />)
    expect(plain(document.querySelector('[data-testid="playlists-none"] p')!.textContent)).toBe('No playlist yet')
    expect(document.querySelector('main, .view')!.textContent).not.toMatch(/[–—]/)
  })
})

const READY: CatalogStatus = { ...BASE, ready: true, counts: { artists: 5, albums: 11, tracks: 30, songs: 23, moods: 20 } }
const person = (id: number, name: string) => ({ id, name, display_name: '', avatar: null })
const card = (over: Record<string, unknown>) => ({
  id: 7, name: 'Grillabend', description: '', owner: person(1, 'jule'), access: 'owner', shares: 0, version: 3,
  created_at: '2026-11-13T10:00:00Z', updated_at: '2026-11-13T11:00:00Z', deleted_at: null, purge_at: null, days_left: null,
  count: 3, duration: 4380, covers: [201, 205, 207, 210], people: [person(1, 'jule'), person(2, 'edith')], contributors: 2,
  ...over,
})

describe('the playlists once the catalogue is there', () => {
  beforeEach(() => {
    server.answers['GET /api/v1/catalog/status'] = READY
  })

  it('show a card per playlist with its mosaic, count, length and people', async () => {
    server.answers['GET /api/v1/playlists'] = { playlists: [card({}), card({ id: 8, name: 'Von Edith', owner: person(2, 'edith'), access: 'edit', covers: [205], people: [person(2, 'edith')] }), card({ id: 9, name: 'Geteilt', shares: 2, covers: [] })], trash_days: 30 }
    await page.show(<PlaylistsPage />)
    const cards = [...document.querySelectorAll<HTMLAnchorElement>('[data-testid="playlist-card"]')]
    expect(cards.map((one) => one.getAttribute('href'))).toEqual(['/playlists/7', '/playlists/8', '/playlists/9'])
    expect(plain(cards[0].querySelector('.meta')!.textContent)).toBe('3 Titel · 1 Std. 13 Min.')
    expect(cards[0].querySelectorAll('.mosaic img').length).toBe(4)
    expect(cards[0].querySelector('.mosaic img')!.getAttribute('src')).toBe('/api/v1/catalog/cover/201?s=200')
    expect(cards[1].querySelectorAll('.mosaic.one img').length).toBe(1)
    expect(cards[2].querySelectorAll('.mosaic img').length).toBe(0)
    expect(cards[0].querySelectorAll('.avs > *').length).toBe(2)
    // Another person's playlist says whose it is, an own shared one that it is shared; an own unshared one says nothing.
    expect(plain(cards[1].querySelector('.badge')!.textContent)).toBe('Von edith')
    expect(plain(cards[2].querySelector('.badge')!.textContent)).toBe('Geteilt')
    expect(cards[0].querySelector('.badge')).toBeNull()
  })

  it('look for changes of others while the page is in view, and at once when it comes back', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    try {
      await page.show(<PlaylistsPage />)
      const looks = () => server.calls.filter((call) => call.method === 'GET' && call.url === '/api/v1/playlists').length
      expect(looks()).toBe(1)
      server.answers['GET /api/v1/playlists'] = { playlists: [card({})], trash_days: 30 }
      await act(async () => vi.advanceTimersByTime(LOOK_MS))
      await until(() => document.querySelectorAll('[data-testid="playlist-card"]').length === 1)
      expect(looks()).toBe(2)
      // Hidden: no look; back in view: one at once.
      vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
      await act(async () => vi.advanceTimersByTime(LOOK_MS))
      expect(looks()).toBe(2)
      vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
      await act(async () => document.dispatchEvent(new Event('visibilitychange')))
      expect(looks()).toBe(3)
    } finally {
      vi.useRealTimers()
      vi.restoreAllMocks()
    }
  })

  it('make a new playlist once, however often it is clicked', async () => {
    let answer: (value: Response) => void = () => undefined
    const made = new Promise<Response>((resolve) => (answer = resolve))
    server.install()
    const original = globalThis.fetch
    const posts: RequestInit[] = []
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        posts.push(init)
        return made
      }
      return original(url, init)
    })
    await page.show(<PlaylistsPage />)
    const button = page.button('Neue Playlist')!
    button.click()
    button.click()
    document.querySelector<HTMLButtonElement>('.newcard')!.click()
    answer(new Response(JSON.stringify({ id: 12 }), { status: 201, headers: { 'Content-Type': 'application/json' } }))
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(posts.length).toBe(1)
    expect(JSON.parse(String(posts[0].body))).toEqual({ name: 'Neue Playlist' })
  })

  it('show the trash with the days left, bring back at once, delete for good only after asking', async () => {
    server.answers['GET /api/v1/playlists?trash=true'] = { playlists: [card({ id: 5, name: 'Alt', deleted_at: '2026-11-01T10:00:00Z', purge_at: '2026-12-01T10:00:00Z', days_left: 18 })], trash_days: 30 }
    server.answers['POST /api/v1/playlists/5/restore'] = card({ id: 5 })
    server.answers['DELETE /api/v1/playlists/5?purge=true'] = { deleted: true }
    await page.show(<PlaylistsPage />)
    const trash = document.querySelector('[data-testid="trash"]')!
    expect(plain(trash.textContent)).toContain('Gelöschte Playlisten bleiben 30 Tage hier, danach sind sie endgültig weg.')
    expect(plain(trash.querySelector('small')!.textContent)).toBe('Gelöscht am 01.11.2026 · noch 18 Tage')
    await page.click(page.button('Endgültig löschen', trash))
    expect(server.changes()).toEqual([])
    expect(plain(page.dialog!.textContent)).toContain('„Alt“ ist danach weg und lässt sich nicht zurückholen.')
    // For good: the red button.
    expect(page.button('Endgültig löschen', page.dialog!)!.className).toContain('bg-bad')
    await page.inDialog('Abbrechen')
    expect(server.changes()).toEqual([])
    await page.click(page.button('Endgültig löschen', trash))
    await page.inDialog('Endgültig löschen')
    expect(server.changes()).toEqual(['DELETE /api/v1/playlists/5?purge=true'])
    await page.click(page.button('Wiederherstellen'))
    expect(server.changes()).toEqual(['DELETE /api/v1/playlists/5?purge=true', 'POST /api/v1/playlists/5/restore'])
  })

  it('say the playlists in English too', async () => {
    await changeLanguage('en', false)
    server.answers['GET /api/v1/playlists'] = { playlists: [card({ count: 1, duration: 200 })], trash_days: 30 }
    await page.show(<PlaylistsPage />)
    expect(plain(document.querySelector('[data-testid="playlist-card"] .meta')!.textContent)).toBe('1 track · 3 min')
    expect(page.button('New playlist')).toBeTruthy()
  })
})

describe('what the overview leaves out', () => {
  const PROFILE = { theme: 'system', timezone: '', timezone_source: 'browser', notify_login: true }
  beforeEach(() => {
    server.answers['GET /api/v1/catalog/status'] = READY
    auth.me = { ...me, profile: { ...PROFILE, show_smart_playlists: false } } as unknown as Me
  })
  const shownIds = () => [...document.querySelectorAll<HTMLAnchorElement>('[data-testid="playlist-card"]')].map((one) => one.getAttribute('href'))
  const smartList = (show: boolean) => ({
    playlists: [card({}), card({ id: 8, name: 'Recently Added', smart: true, owner: person(1, 'jule') }), card({ id: 9, name: 'Weg damit', hidden: true })],
    trash_days: 30,
    show_smart_playlists: show,
  })

  it('leaves the smart playlists of Plex out until the chip asks for them, and keeps the choice on the server', async () => {
    server.answers['GET /api/v1/playlists'] = smartList(false)
    server.answers['PUT /api/v1/me/preferences'] = { ...PROFILE, show_smart_playlists: true }
    await page.show(<PlaylistsPage />)
    expect(shownIds()).toEqual(['/playlists/7'])
    const chip = document.querySelector<HTMLButtonElement>('[data-testid="smart-toggle"]')!
    expect(plain(chip.textContent)).toBe('Smart-Playlisten aus Plex zeigen 1')
    expect(chip.getAttribute('aria-pressed')).toBe('false')
    await page.click(chip)
    expect(server.changes()).toEqual(['PUT /api/v1/me/preferences'])
    expect(server.called('PUT', '/api/v1/me/preferences')!.body).toEqual({ show_smart_playlists: true })
    expect(shownIds()).toEqual(['/playlists/7', '/playlists/8'])
    expect(chip.getAttribute('aria-pressed')).toBe('true')
    expect(chip.className).toContain('on')
  })

  it('shows the smart playlists at once for an account that chose them', async () => {
    server.answers['GET /api/v1/playlists'] = smartList(true)
    await page.show(<PlaylistsPage />)
    expect(shownIds()).toEqual(['/playlists/7', '/playlists/8'])
  })

  it('puts the chip back when the choice could not be kept', async () => {
    server.answers['GET /api/v1/playlists'] = smartList(false)
    server.fails['PUT /api/v1/me/preferences'] = { status: 422, code: 'bad_preference' }
    await page.show(<PlaylistsPage />)
    await page.click(document.querySelector<HTMLButtonElement>('[data-testid="smart-toggle"]'))
    expect(shownIds()).toEqual(['/playlists/7'])
    expect(document.querySelector('[data-testid="smart-toggle"]')!.getAttribute('aria-pressed')).toBe('false')
    expect(document.querySelector('[role="alert"]')).toBeTruthy()
  })

  it('says "no playlist yet" when only smart playlists of Plex are there and left out', async () => {
    server.answers['GET /api/v1/playlists'] = { playlists: [card({ id: 8, name: 'Recently Added', smart: true })], trash_days: 30, show_smart_playlists: false }
    await page.show(<PlaylistsPage />)
    expect(document.querySelector('[data-testid="playlists-none"]')).toBeTruthy()
    expect(document.querySelector('[data-testid="smart-toggle"]')).toBeTruthy()
    expect(shownIds()).toEqual([])
  })

  it('has no chip without smart playlists', async () => {
    server.answers['GET /api/v1/playlists'] = { playlists: [card({})], trash_days: 30, show_smart_playlists: false }
    await page.show(<PlaylistsPage />)
    expect(document.querySelector('[data-testid="smart-toggle"]')).toBeNull()
  })

  it('hides a playlist from its menu and shows it again from the list below, nothing else is sent', async () => {
    server.answers['GET /api/v1/playlists'] = smartList(false)
    server.answers['PUT /api/v1/playlists/7/hidden'] = { id: 7, hidden: true }
    server.answers['DELETE /api/v1/playlists/9/hidden'] = { id: 9, hidden: false }
    await page.show(<PlaylistsPage />)
    // The one hidden before is listed below with its way back; the smart one is not (the chip decides about it).
    const hidden = () => [...document.querySelectorAll('[data-testid="hidden-row"] b')].map((one) => one.textContent)
    expect(hidden()).toEqual(['Weg damit'])
    expect(plain(document.querySelector('[data-testid="hidden"]')!.textContent)).toContain(
      'Nur in deiner Übersicht ausgeblendet. In Plex und für alle anderen bleibt alles, wie es ist.',
    )
    const menu = document.querySelector<HTMLButtonElement>('[data-testid="card-menu"]')!
    expect(menu.getAttribute('aria-label')).toBe('Mehr zu Grillabend')
    // The menu is no part of the link to the playlist.
    expect(menu.closest('a')).toBeNull()
    await page.click(menu)
    expect(menu.getAttribute('aria-expanded')).toBe('true')
    await page.click(document.querySelector<HTMLButtonElement>('[data-testid="card-hide"]'))
    expect(server.changes()).toEqual(['PUT /api/v1/playlists/7/hidden'])
    expect(shownIds()).toEqual([])
    expect(hidden()).toEqual(['Grillabend', 'Weg damit'])
    await page.click(page.button('Wieder zeigen', document.querySelectorAll('[data-testid="hidden-row"]')[1] as HTMLElement))
    expect(server.changes()).toEqual(['PUT /api/v1/playlists/7/hidden', 'DELETE /api/v1/playlists/9/hidden'])
    expect(shownIds()).toEqual(['/playlists/9'])
  })

  it('closes the menu with Escape and puts a card back that could not be hidden', async () => {
    server.answers['GET /api/v1/playlists'] = { playlists: [card({})], trash_days: 30, show_smart_playlists: false }
    server.fails['PUT /api/v1/playlists/7/hidden'] = { status: 404, code: 'not_found' }
    await page.show(<PlaylistsPage />)
    await page.click(document.querySelector<HTMLButtonElement>('[data-testid="card-menu"]'))
    await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })))
    expect(document.querySelector('[data-testid="card-hide"]')).toBeNull()
    await page.click(document.querySelector<HTMLButtonElement>('[data-testid="card-menu"]'))
    await page.click(document.querySelector<HTMLButtonElement>('[data-testid="card-hide"]'))
    await until(() => document.querySelector('[role="alert"]') !== null)
    expect(shownIds()).toEqual(['/playlists/7'])
    expect(document.querySelector('[data-testid="hidden"]')).toBeNull()
  })

  it('says it in English too', async () => {
    await changeLanguage('en', false)
    server.answers['GET /api/v1/playlists'] = smartList(false)
    await page.show(<PlaylistsPage />)
    expect(plain(document.querySelector('[data-testid="smart-toggle"]')!.textContent)).toBe('Show smart playlists from Plex 1')
    expect(page.button('Show again')).toBeTruthy()
    expect(plain(document.querySelector('[data-testid="hidden"] h2')!.textContent)).toBe('Hidden')
    expect(document.querySelector('.view')!.textContent).not.toMatch(/[–—]/)
  })
})

describe('a new playlist with more than one media server', () => {
  const caps = { moods: true, sound: true, smart_playlists: true, duplicates: false, changes: 'both', playlists: true }
  const servers = [
    { id: 1, kind: 'plex', name: 'Studio', active: true, library: 'Music', ready: true, capabilities: caps, primary: true },
    { id: 2, kind: 'jellyfin', name: 'Probe', active: true, library: 'Musik', ready: true, capabilities: { ...caps, moods: false, playlists: false }, primary: false },
  ]

  beforeEach(() => {
    server.answers['GET /api/v1/catalog/status'] = { ...BASE, ready: true, counts: { artists: 5, albums: 11, tracks: 52518, songs: 45457, moods: 288 } }
    server.answers['GET /api/v1/servers'] = { servers }
    server.answers['POST /api/v1/playlists'] = { id: 12 }
  })

  it('asks where the tracks come from, the first connected chosen, and makes nothing on cancel', async () => {
    await page.show(<PlaylistsPage />)
    await page.click(page.button('Neue Playlist'))
    expect(page.dialog).toBeTruthy()
    expect(plain(page.dialog!.textContent)).toContain('Woher sollen die Titel kommen?')
    const options = [...page.dialog!.querySelectorAll<HTMLInputElement>('input[type="radio"]')]
    expect(options.map((one) => [plain(one.parentElement!.textContent), one.checked])).toEqual([['Plex „Studio“Music', true], ['Jellyfin „Probe“Musik', false]])
    await page.inDialog('Abbrechen')
    expect(server.changes()).toEqual([])
  })

  it('makes the playlist on the server chosen', async () => {
    await page.show(<PlaylistsPage />)
    await page.click(page.button('Neue Playlist'))
    await page.check(page.dialog!.querySelectorAll<HTMLInputElement>('input[type="radio"]')[1])
    await page.inDialog('Playlist anlegen')
    await until(() => server.called('POST', '/api/v1/playlists'), 'the playlist made')
    expect(server.called('POST', '/api/v1/playlists')!.body).toEqual({ name: 'Neue Playlist', server: 2 })
  })

  it('asks nothing with one server ready', async () => {
    server.answers['GET /api/v1/servers'] = { servers: [servers[0], { ...servers[1], ready: false }] }
    await page.show(<PlaylistsPage />)
    await page.click(page.button('Neue Playlist'))
    expect(page.dialog).toBeNull()
    expect(server.called('POST', '/api/v1/playlists')!.body).toEqual({ name: 'Neue Playlist' })
  })

  it('marks a playlist that lives in nexmix only', async () => {
    server.answers['GET /api/v1/playlists'] = { playlists: [card({ id: 7, name: 'Auf Jellyfin', server: 2, plex: { state: 'local', error: null, smart: false, total: null, synced_at: null, gone_in_plex: false, note: null } })], trash_days: 30 }
    await page.show(<PlaylistsPage />)
    expect(plain(document.querySelector('[data-testid="sync-mark"]')!.textContent)).toBe('Nur in nexmix')
  })
})
