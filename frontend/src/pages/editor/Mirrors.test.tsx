/**
 * Copying and moving a playlist to another server, against a stand-in server: the preview says how many were found
 * and how many are unsure; an entry not found is matched by hand through a search of the target, or left out, and the
 * preview is asked again with the choice; copying sends the choices, "copy once" too. Moving with the old copy deleted
 * asks first and sends nothing without the answer. The notes of a mirror: deleted there ("make anew"), tracks only
 * there ("take over as …" through a search of the source, "drop", asked first).
 */
import { changeLanguage } from '../../i18n'
import type { Mirror, Preview } from '../../api/mirrors'
import type { MediaServer } from '../../api/servers'
import { Page, Server, plain } from '../../test/harness'
import { until } from '../../test/wait'
import { CopyDialog } from './CopyDialog'
import { MirrorNotes } from './MirrorStrip'

const server = new Server()
const page = new Page()

const jelly: MediaServer = {
  id: 2,
  kind: 'jellyfin',
  name: 'Probe',
  active: true,
  library: 'Musik',
  ready: true,
  primary: false,
  capabilities: { moods: false, sound: false, smart_playlists: false, duplicates: true, changes: 'both', playlists: true },
}

const preview = (over: Partial<Preview> = {}): Preview => ({
  server: 2,
  entries: 6,
  found: 5,
  unsure: 1,
  missing: 1,
  more: 0,
  rows: [
    { track: 3021, title: 'Op. 3 (No. 2)', artist: 'Dax Orlo', album: 'Northern Lights', duration: 200, how: 'none', found: null, count: 1 },
    { track: 3017, title: 'Fever Glass', artist: 'Mira Solenne', album: 'Copper Sky', duration: 200, how: 'song', found: { track: 9017, title: 'Fever Glass (Live)', artist: 'Mira Solenne', album: 'Live', duration: 230 }, count: 1 },
  ],
  ...over,
})

let done: string[] = []
let closed = 0

beforeEach(async () => {
  await changeLanguage('de', false)
  done = []
  closed = 0
  server.answers = {
    'POST /api/v1/playlists/7/mirrors/preview': preview(),
    'POST /api/v1/playlists/7/mirrors': { playlist: 7, mirrors: [] },
    'POST /api/v1/playlists/7/move': { playlist: 7, server: 2, found: 5, entries: 6, mirrors: [] },
  }
  server.fails = {}
  server.install()
  const plainFetch = server.fetch()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url.startsWith('/api/v1/catalog/search')) {
        server.calls.push({ method: 'GET', url, body: undefined })
        return new Response(JSON.stringify({ total: 1, tracks: [{ id: 9021, title: 'Opus Drei', artist: 'Dax Orlo', album: 'Nordlicht', album_id: 1, cover: false, year: 2010, duration: 201, song: 9021, versions: 1 }] }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      return plainFetch(url, init)
    }),
  )
})
afterEach(() => {
  page.close()
  vi.unstubAllGlobals()
})

async function open(mode: 'copy' | 'move'): Promise<void> {
  await page.show(<CopyDialog playlistId={7} name="Sommerfest" mode={mode} targets={[jelly]} sourceName="Studio" onClose={() => (closed += 1)} onDone={(text) => done.push(text)} />)
  await until(() => document.querySelector('[data-testid="mirror-count"]'))
}

describe('copying to another server', () => {
  it('shows what was found, the unsure and the missing ones', async () => {
    await open('copy')
    expect(plain(document.querySelector('[data-testid="mirror-count"]')!.textContent)).toBe('5 von 6 gefundendavon 1 unsicher1 nicht gefunden')
    const rows = [...document.querySelectorAll('[data-testid="mirror-row-state"]')].map((one) => plain(one.textContent))
    expect(rows).toEqual(['Nicht gefunden', 'Unsicher: „Fever Glass (Live)“ von Mira Solenne'])
    expect(server.called('POST', '/api/v1/playlists/7/mirrors/preview')!.body).toEqual({ server: 2, choices: {} })
  })

  it('matches an entry by hand through a search of the target, leaves another out, and sends both choices', async () => {
    await open('copy')
    await page.click(page.button('„Op. 3 (No. 2)“ anders zuordnen'))
    await until(() => document.querySelector('[data-testid="track-picker"]'))
    await until(() => page.button('„Opus Drei“ nehmen'))
    expect(server.calls.some((call) => call.url.startsWith('/api/v1/catalog/search') && call.url.includes('server=2'))).toBe(true)
    await page.click(page.button('„Opus Drei“ nehmen'))
    await until(() => server.calls.filter((call) => call.url === '/api/v1/playlists/7/mirrors/preview').length === 2)
    await page.click(page.button('„Fever Glass“ weglassen'))
    await until(() => server.calls.filter((call) => call.url === '/api/v1/playlists/7/mirrors/preview').length === 3)
    expect(server.calls.filter((call) => call.url === '/api/v1/playlists/7/mirrors/preview').at(-1)!.body).toEqual({ server: 2, choices: { 3021: 9021, 3017: null } })
    await page.click(page.button('Kopieren'))
    await until(() => done.length === 1)
    expect(server.called('POST', '/api/v1/playlists/7/mirrors')!.body).toEqual({ server: 2, choices: { 3021: 9021, 3017: null }, once: false })
    expect(done.map(plain)).toEqual(['Auf Probe gespiegelt.'])
  })

  it('copies once as a new playlist', async () => {
    server.answers['POST /api/v1/playlists/7/mirrors'] = { playlist: 9, mirrors: [] }
    await open('copy')
    await page.click(document.querySelector<HTMLButtonElement>('[data-testid="mirror-once"]'))
    await page.click(page.button('Einmal kopieren'))
    await until(() => done.length === 1)
    expect(server.called('POST', '/api/v1/playlists/7/mirrors')!.body).toEqual({ server: 2, choices: {}, once: true })
    expect(done.map(plain)).toEqual(['Als neue Playlist auf Probe kopiert.'])
  })
})

describe('moving to another server', () => {
  it('keeps the old copy as a mirror by default', async () => {
    await open('move')
    expect(page.text).toContain('Probe (Jellyfin) wird die Quelle')
    await page.click(page.button('Verschieben'))
    await until(() => done.length === 1)
    expect(server.called('POST', '/api/v1/playlists/7/move')!.body).toEqual({ server: 2, choices: {}, old: 'keep' })
  })

  it('asks before the old copy is deleted, and sends nothing without the answer', async () => {
    await open('move')
    await page.click(document.querySelector<HTMLButtonElement>('[data-testid="mirror-delete-old"]'))
    await page.click(page.button('Verschieben'))
    expect(plain(page.dialog!.textContent)).toContain('verschwindet auf Studio sofort. Das lässt sich dort nicht zurückholen')
    await page.inDialog('Abbrechen')
    expect(server.called('POST', '/api/v1/playlists/7/move')).toBeUndefined()
    await page.click(page.button('Verschieben'))
    await page.inDialog('Verschieben und löschen')
    await until(() => done.length === 1)
    expect(server.called('POST', '/api/v1/playlists/7/move')!.body).toEqual({ server: 2, choices: {}, old: 'delete' })
  })

  it('says why it failed', async () => {
    server.fails['POST /api/v1/playlists/7/move'] = { status: 409, code: 'smart_stays' }
    await open('move')
    await page.click(page.button('Verschieben'))
    await until(() => document.querySelector('[role="alert"]'))
    expect(page.text).toContain('Eine Smart-Playlist bleibt auf ihrem Server.')
  })
})

describe('the notes of a mirror', () => {
  const mirror = (over: Partial<Mirror> = {}): Mirror => ({ server: 2, kind: 'jellyfin', name: 'Probe', state: 'synced', error: null, synced_at: null, absent: 0, extras: [], ...over })
  let changed = 0
  let toasts: string[] = []
  async function notes(mirrors: Mirror[]): Promise<void> {
    changed = 0
    toasts = []
    await page.show(<MirrorNotes playlistId={7} name="Sommerfest" mirrors={mirrors} source={1} canEdit canManage onChanged={() => (changed += 1)} onToast={(text) => toasts.push(text)} />)
  }

  it('says when a mirror was deleted on its server and makes it anew', async () => {
    server.answers['POST /api/v1/playlists/7/mirrors/2/remake'] = { mirrors: [] }
    await notes([mirror({ state: 'gone' })])
    expect(plain(document.querySelector('[data-testid="mirror-gone"]')!.textContent)).toContain('Der Spiegel von „Sommerfest“ wurde auf Probe gelöscht. Hier und in der Quelle bleibt alles.')
    await page.click(page.button('Wieder anlegen'))
    await until(() => changed === 1)
    expect(server.changes()).toEqual(['POST /api/v1/playlists/7/mirrors/2/remake'])
  })

  it('takes a track only in the mirror over through a search of the source', async () => {
    server.answers['POST /api/v1/playlists/7/mirrors/2/extras/5/take'] = { version: 3, op: 'add', entries: [] }
    await notes([mirror({ extras: [{ id: 5, title: 'Glasfluss', artist: 'Ystasul', album: 'Mondgarten', duration: 301 }] })])
    expect(plain(document.querySelector('[data-testid="mirror-extras"]')!.textContent)).toContain('1 Titel nur in Probe')
    await page.click(page.button('„Glasfluss“ übernehmen als …'))
    await until(() => page.button('„Opus Drei“ nehmen'))
    expect(server.calls.some((call) => call.url.startsWith('/api/v1/catalog/search') && call.url.includes('server=1'))).toBe(true)
    await page.click(page.button('„Opus Drei“ nehmen'))
    await until(() => changed === 1)
    expect(server.called('POST', '/api/v1/playlists/7/mirrors/2/extras/5/take')!.body).toEqual({ track: 9021 })
  })

  it('asks before a track only in the mirror is dropped, and sends nothing without the answer', async () => {
    server.answers['DELETE /api/v1/playlists/7/mirrors/2/extras/5'] = 204
    await notes([mirror({ extras: [{ id: 5, title: 'Glasfluss', artist: 'Ystasul', album: 'Mondgarten', duration: 301 }] })])
    await page.click(page.button('„Glasfluss“ verwerfen'))
    expect(plain(page.dialog!.textContent)).toContain('„Glasfluss“ wird aus der Playlist auf Probe entfernt. Das lässt sich nicht zurückholen.')
    await page.inDialog('Abbrechen')
    expect(server.changes()).toEqual([])
    await page.click(page.button('„Glasfluss“ verwerfen'))
    await page.inDialog('Verwerfen')
    await until(() => changed === 1)
    expect(server.changes()).toEqual(['DELETE /api/v1/playlists/7/mirrors/2/extras/5'])
  })
})
