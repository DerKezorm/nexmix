/**
 * The menu "…" on a playlist's card: what the editor offers for the whole playlist, with the editor's rights (owner,
 * member who may change it, member who may only look, operator; shared and smart playlists; copy and move only with a
 * second server; nothing in the trash), opening the editor's own dialogs (rename, share, guests, copy and move with the
 * preview, the question before the trash), and a menu the keyboard can use (arrows, Home, End, Escape with the focus
 * back, Tab, a click beside it).
 */
import { act } from 'react'

import type { Me } from '../api/client'
import type { CatalogStatus } from '../api/plex'
import { changeLanguage } from '../i18n'
import { Page, Server, plain } from '../test/harness'
import { until } from '../test/wait'
import { PlaylistsPage } from './PlaylistsPage'

const me = { id: 1, name: 'jule', display_name: 'Jule', role: 'operator', profile: {} } as unknown as Me
const auth = {
  me: me as Me | null,
  setMe: (next: Me) => {
    auth.me = next
  },
}
vi.mock('../state/auth', () => ({ useAuth: () => auth }))

const server = new Server()
const page = new Page()

const READY: CatalogStatus = {
  connected: true, library: true, library_title: 'Music', ready: true,
  counts: { artists: 5, albums: 11, tracks: 30, songs: 23, moods: 20 }, synced_at: null, full_at: null, running: null,
  queued: null, last: null, analysis: { state: 'unknown', percent: null }, nightly: '03:30',
}
const CAPS = { moods: true, sound: true, smart_playlists: true, duplicates: false, changes: 'both', playlists: true }
const STUDIO = { id: 1, kind: 'plex', name: 'Studio', active: true, library: 'Music', ready: true, capabilities: CAPS, primary: true }
const PROBE = { id: 2, kind: 'jellyfin', name: 'Probe', active: true, library: 'Musik', ready: true, capabilities: { ...CAPS, moods: false }, primary: false }
const SYNCED = { state: 'synced', kind: 'plex', error: null, smart: false, total: null, synced_at: null, gone_in_plex: false, note: null }
const person = (id: number, name: string) => ({ id, name, display_name: '', avatar: null })
const card = (over: Record<string, unknown>) => ({
  id: 7, name: 'Grillabend', description: '', owner: person(1, 'jule'), access: 'owner', shares: 0, version: 3,
  created_at: '2026-11-13T10:00:00Z', updated_at: '2026-11-13T11:00:00Z', deleted_at: null, purge_at: null, days_left: null,
  count: 3, duration: 4380, covers: [], people: [person(1, 'jule')], contributors: 1, smart: false, server: 1, mirrors: [], plex: SYNCED,
  ...over,
})

beforeEach(async () => {
  await changeLanguage('de', false)
  auth.me = me
  server.answers = {
    'GET /api/v1/catalog/status': READY,
    'GET /api/v1/servers': { servers: [STUDIO, PROBE] },
    'GET /api/v1/playlists': { playlists: [card({})], trash_days: 30 },
    'GET /api/v1/playlists?trash=true': { playlists: [], trash_days: 30 },
  }
  server.fails = {}
  server.install()
})
afterEach(() => {
  page.close()
  vi.unstubAllGlobals()
})

const trigger = () => document.querySelector<HTMLButtonElement>('[data-testid="card-menu"]')!
const menu = () => document.querySelector<HTMLElement>('[role="menu"]')
const items = () => [...document.querySelectorAll<HTMLButtonElement>('[role="menu"] [role="menuitem"]')]
const names = () => items().map((item) => plain(item.textContent).trim())
const item = (name: string) => items().find((one) => plain(one.textContent).trim() === name)
const press = async (key: string, at: Element | null = document.activeElement) => {
  await act(async () => at!.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true })))
}
async function open(playlist: Record<string, unknown>, servers = [STUDIO, PROBE]) {
  server.answers['GET /api/v1/playlists'] = { playlists: [card(playlist)], trash_days: 30, show_smart_playlists: true }
  server.answers['GET /api/v1/servers'] = { servers }
  await page.show(<PlaylistsPage />)
  await page.click(trigger())
  return names()
}

const ALL = ['Öffnen', 'Umbenennen …', 'Teilen …', 'Gäste einladen …', 'Kopieren nach …', 'Verschieben nach …', 'Ausblenden', 'Löschen …']

describe('the menu of a card offers what the editor does, with its rights', () => {
  it('everything to the owner, with a second server copy and move too', async () => {
    expect(await open({})).toEqual(ALL)
    expect(menu()!.getAttribute('aria-label')).toBe('Mehr zu Grillabend')
    // The signs of the editor: one per entry.
    expect(items().every((one) => one.querySelector('svg'))).toBe(true)
  })

  it('no copy and no move with one server, as in the editor', async () => {
    expect(await open({}, [STUDIO])).toEqual(['Öffnen', 'Umbenennen …', 'Teilen …', 'Gäste einladen …', 'Ausblenden', 'Löschen …'])
  })

  it('no copy to a server that holds a mirror already, but moving there', async () => {
    const mirrors = [{ server: 2, kind: 'jellyfin', name: 'Probe', state: 'synced' }]
    expect(await open({ mirrors })).toEqual(['Öffnen', 'Umbenennen …', 'Teilen …', 'Gäste einladen …', 'Verschieben nach …', 'Ausblenden', 'Löschen …'])
  })

  it('no copy and no move to a server that keeps no playlists or is not ready', async () => {
    expect(await open({}, [STUDIO, { ...PROBE, ready: false }])).not.toContain('Kopieren nach …')
    page.close()
    expect(await open({}, [STUDIO, { ...PROBE, capabilities: { ...CAPS, playlists: false } }])).not.toContain('Verschieben nach …')
  })

  it('everything to the operator on a playlist of another person', async () => {
    expect(await open({ owner: person(2, 'edith'), access: 'operator' })).toEqual(ALL)
  })

  it('to a member who may change it: rename and guests, nothing that manages it', async () => {
    auth.me = { ...me, role: 'member' } as Me
    expect(await open({ owner: person(2, 'edith'), access: 'edit' })).toEqual(['Öffnen', 'Umbenennen …', 'Gäste einladen …', 'Ausblenden'])
  })

  it('to a member who may only look: open and hide', async () => {
    auth.me = { ...me, role: 'member' } as Me
    expect(await open({ owner: person(2, 'edith'), access: 'read' })).toEqual(['Öffnen', 'Ausblenden'])
  })

  it('a smart playlist only to open, share and hide, for its owner as for the operator', async () => {
    expect(await open({ smart: true })).toEqual(['Öffnen', 'Teilen …', 'Ausblenden'])
    page.close()
    expect(await open({ smart: true, owner: person(2, 'edith'), access: 'operator' })).toEqual(['Öffnen', 'Teilen …', 'Ausblenden'])
    page.close()
    auth.me = { ...me, role: 'member' } as Me
    expect(await open({ smart: true, owner: person(2, 'edith'), access: 'edit' })).toEqual(['Öffnen', 'Ausblenden'])
  })

  it('no menu for a playlist in the trash: there it is bring back or delete for good', async () => {
    server.answers['GET /api/v1/playlists'] = { playlists: [], trash_days: 30 }
    server.answers['GET /api/v1/playlists?trash=true'] = { playlists: [card({ deleted_at: '2026-11-14T10:00:00Z', days_left: 29 })], trash_days: 30 }
    await page.show(<PlaylistsPage />)
    expect(document.querySelector('[data-testid="trash-row"]')).toBeTruthy()
    expect(document.querySelector('[data-testid="card-menu"]')).toBeNull()
  })
})

describe('the menu with the keyboard and the mouse', () => {
  it('is a menu: the focus on the first entry, the arrows round, Home and End, Escape back to the button', async () => {
    await open({})
    expect(trigger().getAttribute('aria-haspopup')).toBe('menu')
    expect(trigger().getAttribute('aria-expanded')).toBe('true')
    expect(document.activeElement).toBe(item('Öffnen'))
    await press('ArrowDown')
    expect(document.activeElement).toBe(item('Umbenennen …'))
    await press('ArrowUp')
    await press('ArrowUp')
    expect(document.activeElement).toBe(item('Löschen …'))
    await press('ArrowDown')
    expect(document.activeElement).toBe(item('Öffnen'))
    await press('End')
    expect(document.activeElement).toBe(item('Löschen …'))
    await press('Home')
    expect(document.activeElement).toBe(item('Öffnen'))
    await press('Escape')
    expect(menu()).toBeNull()
    expect(document.activeElement).toBe(trigger())
    expect(server.changes()).toEqual([])
  })

  it('opens from the button with the arrows (up: on the last entry), and closes with Tab', async () => {
    await page.show(<PlaylistsPage />)
    trigger().focus()
    await press('ArrowDown', trigger())
    expect(document.activeElement).toBe(item('Öffnen'))
    await press('Tab')
    expect(menu()).toBeNull()
    trigger().focus()
    await press('ArrowUp', trigger())
    expect(document.activeElement).toBe(item('Löschen …'))
  })

  it('closes on a click beside it, and does nothing', async () => {
    await open({})
    await act(async () => document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })))
    expect(menu()).toBeNull()
    expect(server.changes()).toEqual([])
  })
})

describe('the entries open the dialogs of the editor', () => {
  it('delete asks first with the editor\'s words, sends nothing on cancel, and puts it in the trash on yes', async () => {
    server.answers['DELETE /api/v1/playlists/7'] = card({ deleted_at: '2026-11-16T10:00:00Z' })
    await open({ mirrors: [{ server: 2, kind: 'jellyfin', name: 'Probe', state: 'synced' }] })
    await page.click(item('Löschen …'))
    expect(menu()).toBeNull()
    const said = plain(page.dialog!.textContent)
    expect(said).toContain('Playlist löschen?')
    expect(said).toContain('„Grillabend“ liegt dann 30 Tage im Papierkorb und lässt sich bis dahin wiederherstellen. In Plex verschwindet sie sofort, Wiederherstellen legt sie dort neu an.')
    expect(said).toContain('Spiegel auf Probe bleiben bis zum endgültigen Löschen.')
    await page.inDialog('Abbrechen')
    expect(server.changes()).toEqual([])
    expect(document.activeElement).toBe(trigger())
    await page.click(trigger())
    await page.click(item('Löschen …'))
    server.answers['GET /api/v1/playlists'] = { playlists: [], trash_days: 30 }
    await page.inDialog('In den Papierkorb')
    expect(server.changes()).toEqual(['DELETE /api/v1/playlists/7'])
    await until(() => document.querySelector('[data-testid="toast"]'), 'the sentence')
    expect(plain(document.querySelector('[data-testid="toast"]')!.textContent)).toBe('„Grillabend“ liegt im Papierkorb.')
  })

  it('rename sends the new name once and shows it on the card; an empty one sends nothing', async () => {
    server.answers['PUT /api/v1/playlists/7'] = { version: 4, name: 'Sommerabend' }
    await open({})
    await page.click(item('Umbenennen …'))
    const field = page.dialog!.querySelector<HTMLInputElement>('[data-testid="rename-input"]')!
    expect(field.value).toBe('Grillabend')
    expect(document.activeElement).toBe(field)
    await page.type(field, '   ')
    expect(page.button('Speichern', page.dialog!)!.disabled).toBe(true)
    await page.type(field, 'Sommerabend')
    await page.inDialog('Speichern')
    expect(server.changes()).toEqual(['PUT /api/v1/playlists/7'])
    expect(server.called('PUT', '/api/v1/playlists/7')!.body).toEqual({ name: 'Sommerabend' })
    expect(page.dialog).toBeNull()
    expect(plain(document.querySelector('[data-testid="playlist-card"] h3')!.textContent)).toBe('Sommerabend')
  })

  it('share opens the editor\'s dialog with the people', async () => {
    server.answers['GET /api/v1/playlists/7/shares'] = { shares: [], people: [person(2, 'edith')] }
    await open({})
    await page.click(item('Teilen …'))
    expect(page.dialog!.getAttribute('aria-label')).toBe('Teilen')
    await until(() => page.dialog!.querySelector('[data-testid="share-list"] .sharerow'), 'the people')
    expect(server.changes()).toEqual([])
  })

  it('guests opens the editor\'s invitation dialog', async () => {
    // The dialog's own answers are its tests' (InviteDialog.test.tsx); here only that it is the one, and asks.
    server.answers['POST /api/v1/playlists/7/invite'] = {
      invite: { code: 'K7MX-QR2H-PA4N', link: 'https://mix.example.com/j/K7MX-QR2H-PA4N', qr: '<svg xmlns="http://www.w3.org/2000/svg"></svg>', state: 'waiting', may_add: true, may_sort: false, max: 10, hours: 24, preview: true, started_at: null, expires_at: null },
      guests: [], defaults: { may_add: true, may_sort: false, max: 10, hours: 24, preview: true },
      address: { url: 'https://mix.example.com', configured: true, reach: 'internet' }, listening: true,
    }
    await open({})
    await page.click(item('Gäste einladen …'))
    expect(page.dialog!.getAttribute('aria-label')).toBe('Gäste einladen')
    await until(() => server.called('POST', '/api/v1/playlists/7/invite'), 'the invitation prepared')
  })

  it('copy and move open the editor\'s dialog with its preview, toward the other server', async () => {
    server.answers['POST /api/v1/playlists/7/mirrors/preview'] = { server: 2, entries: 3, found: 3, unsure: 0, rows: [] }
    await open({})
    await page.click(item('Kopieren nach …'))
    expect(page.dialog!.getAttribute('aria-label')).toBe('Kopieren nach …')
    expect([...page.dialog!.querySelectorAll('[data-testid="mirror-targets"] button')].map((one) => plain(one.textContent))).toEqual(['Probe (Jellyfin)'])
    await until(() => server.called('POST', '/api/v1/playlists/7/mirrors/preview'), 'the preview')
    expect(server.called('POST', '/api/v1/playlists/7/mirrors/preview')!.body).toEqual({ server: 2, choices: {} })
    await page.inDialog('Abbrechen')
    await page.click(trigger())
    await page.click(item('Verschieben nach …'))
    expect(page.dialog!.getAttribute('aria-label')).toBe('Verschieben nach …')
    await until(() => server.calls.filter((call) => call.url === '/api/v1/playlists/7/mirrors/preview').length === 2, 'the second preview')
    expect(server.calls.filter((call) => call.url === '/api/v1/playlists/7/mirrors/preview')[1].body).toEqual({ server: 2, choices: {}, move: true })
  })

  it('open leads to the editor', async () => {
    await open({})
    expect(item('Öffnen')).toBeTruthy()
    await page.click(item('Öffnen'))
    expect(menu()).toBeNull()
  })

  it('says it in English too, without dashes', async () => {
    await changeLanguage('en', false)
    expect(await open({})).toEqual(['Open', 'Rename …', 'Share …', 'Invite guests …', 'Copy to …', 'Move to …', 'Hide', 'Delete …'])
    expect(names().join(' ')).not.toMatch(/[–—]/)
  })
})

describe('the line under "Playlists"', () => {
  it('names Plex with one server, as before', async () => {
    server.answers['GET /api/v1/servers'] = { servers: [STUDIO] }
    await page.show(<PlaylistsPage />)
    expect(plain(document.querySelector('.lead p')!.textContent)).toBe('30 Titel aus deiner Plex-Mediathek „Music“ · alles landet als Playlist in Plex und ist damit auch in Plexamp und Music Assistant.')
  })

  it('says with more servers that each playlist lies on its own, in German and English', async () => {
    await page.show(<PlaylistsPage />)
    const line = plain(document.querySelector('.lead p')!.textContent)
    expect(line).toBe('Musik von Studio (Plex) und Probe (Jellyfin) · jede Playlist liegt auf dem Server, von dem sie ihre Titel nimmt, und ist dort auch in seinen Apps.')
    expect(line).not.toContain('alles landet als Playlist in Plex')
    page.close()
    await changeLanguage('en', false)
    await page.show(<PlaylistsPage />)
    expect(plain(document.querySelector('.lead p')!.textContent)).toBe("Music from Studio (Plex) and Probe (Jellyfin) · each playlist lies on the server it takes its tracks from, and is in that server's apps too.")
  })

  it('names a server called after its kind only once', async () => {
    server.answers['GET /api/v1/servers'] = { servers: [STUDIO, { ...PROBE, id: 3, kind: 'music_assistant', name: 'Music Assistant' }] }
    await page.show(<PlaylistsPage />)
    expect(plain(document.querySelector('.lead p')!.textContent)).toMatch(/^Musik von Studio \(Plex\) und Music Assistant · /)
  })

  it('counts only the servers that are ready', async () => {
    server.answers['GET /api/v1/servers'] = { servers: [STUDIO, { ...PROBE, ready: false }] }
    await page.show(<PlaylistsPage />)
    expect(plain(document.querySelector('.lead p')!.textContent)).toContain('alles landet als Playlist in Plex')
  })
})
