/**
 * The editor's suggestions, "fill by mood" and listening in, against a stand-in server and the made-up catalogue of the
 * server's tests: the column (seed, kinds, reason, add, "not again" with its undo, "similar to"), the two voices asked
 * of the server, the note about the sonic analysis, the dialog up to "take" (one step, one undo; nothing without it),
 * and ▶ (one preview at a time, stop, the ring, no ▶ while listening in is off).
 */
import { act } from 'react'
import { Route, Routes } from 'react-router-dom'

import type { Me } from '../../api/client'
import type { Change, Entry, Playlist } from '../../api/playlists'
import { changeLanguage } from '../../i18n'
import { prepare, type Compact } from '../../lib/catalogSearch'
import { setAudioFactory, stop } from '../../lib/preview'
import { seedPositions, suggest } from '../../lib/suggest'
import { Page, Server, plain } from '../../test/harness'
import { until } from '../../test/wait'
import { PlaylistEditor } from '../PlaylistEditor'

const me = { id: 1, name: 'jule', display_name: 'Jule', role: 'member' } as unknown as Me
vi.mock('../../state/auth', () => ({ useAuth: () => ({ me }) }))

const fs = (await import(/* @vite-ignore */ ['node', 'fs'].join(':'))) as unknown as { readFileSync(path: string, encoding: 'utf-8'): string }
const CATALOG = JSON.parse(fs.readFileSync('../backend/tests/fixtures/search/catalog.json', 'utf-8')) as Compact
const ix = prepare(CATALOG)

const server = new Server()
const page = new Page()
const jule = { id: 1, name: 'jule', display_name: 'Jule', avatar: null }

const entry = (id: number, track: number, title: string): Entry => ({
  id, track, title, artist: 'Someone', album: 'Some Album', album_id: 201, cover: true, year: 1984, duration: 200,
  song: track, missing: false, added_by: jule, added_by_name: 'Jule', added_at: '2026-11-13T10:00:00Z',
})

const ITEMS = [entry(1, 301, 'Northern Lights'), entry(2, 311, 'Straße der Träume'), entry(3, 317, 'Señorita Lluvia')]

function playlist(over: Partial<Playlist> = {}): Playlist {
  return {
    id: 7, name: 'Grillabend', description: '', owner: jule, access: 'owner', shares: 0, version: 5,
    created_at: '2026-11-13T10:00:00Z', updated_at: '2026-11-13T10:00:00Z', deleted_at: null, purge_at: null, days_left: null,
    count: 3, duration: 600, covers: [201], people: [jule], contributors: 1, moods: ['Romantic'], items: ITEMS,
    smart: false, plex: { state: 'synced', error: null, smart: false, total: null, synced_at: '2026-11-13T10:00:00Z', gone_in_plex: false, note: null },
    ...over,
  }
}

const STATUS = { connected: true, library: true, library_title: 'Music', ready: true, counts: { artists: 6, albums: 11, tracks: 31, songs: 23, moods: 17 }, synced_at: null, full_at: null, running: null, queued: null, last: null, analysis: { state: 'off', percent: null }, nightly: '', search: 'browser' }

/** An audio element as far as the previews use it. */
class FakeAudio extends EventTarget {
  preload = ''
  currentTime = 0
  attributes = new Map<string, string>()
  played: string[] = []
  paused = true
  setAttribute(name: string, value: string) {
    this.attributes.set(name, value)
  }
  getAttribute(name: string) {
    return this.attributes.get(name) ?? null
  }
  removeAttribute(name: string) {
    this.attributes.delete(name)
  }
  play() {
    this.paused = false
    this.played.push(this.getAttribute('src') ?? '')
    return Promise.resolve()
  }
  pause() {
    this.paused = true
  }
  load() {}
}

let audio: FakeAudio

beforeEach(async () => {
  await changeLanguage('de', false)
  audio = new FakeAudio()
  setAudioFactory(() => audio as unknown as HTMLAudioElement)
  server.answers = {
    'GET /api/v1/catalog/status': STATUS,
    'GET /api/v1/catalog': CATALOG,
    'GET /api/v1/playlists/7': playlist(),
    'GET /api/v1/suggestions/hidden': { songs: [] },
    'GET /api/v1/preview': { allowed: true, seconds: 30, per_hour: 120, left: 120, retry_after: null },
  }
  server.fails = {}
  server.install()
})
afterEach(() => {
  stop()
  page.close()
  vi.unstubAllGlobals()
})

async function open(): Promise<void> {
  await page.show(
    <Routes>
      <Route path="/playlists/:id" element={<PlaylistEditor />} />
      <Route path="/settings" element={<p data-testid="settings">settings</p>} />
    </Routes>,
    '/playlists/7',
  )
  await until(() => document.querySelector('[data-testid="suggestion"]'))
}

const titles = () => [...document.querySelectorAll('[data-testid="suggestion"] .t')].map((title) => title.textContent)
const expected = (seed: number[], mode: 'fit' | 'known' | 'new' = 'fit') => suggest(ix, seedPositions(ix, seed), { mode, exclude: [301, 311, 317] }).map((track) => track.title)
const column = () => document.querySelector<HTMLElement>('[data-testid="suggestions"]')!

describe('the suggestions', () => {
  it('fit the last tracks of the list, with their reason, ▶ and +', async () => {
    await open()
    expect(plain(column().querySelector('h2')!.textContent)).toBe(' Vorschläge')
    expect(plain(document.querySelector('[data-testid="seed-label"]')!.textContent)).toBe('Passend zu den letzten 3 Titeln der Liste')
    expect(titles()).toEqual(expected([301, 311, 317]))
    const first = document.querySelector<HTMLElement>('[data-testid="suggestion"]')!
    expect(first.querySelector('.why')!.textContent).not.toBe('')
    expect(first.querySelector('[data-testid="play"]')).toBeTruthy()
    expect(first.querySelector('button.add')).toBeTruthy()
    expect(plain(column().textContent)).toContain('aus Stimmung, Stil, Genre und Jahrzehnt')
    // The voice "often together" is asked of the server, for the songs of the seed.
    await until(() => server.calls.find((call) => call.url === '/api/v1/suggestions/together?songs=301&songs=311&songs=317'))
    // The sonic analysis is off: Plex is not asked, the column says what it would bring (no link for a member).
    expect(server.calls.some((call) => call.url.startsWith('/api/v1/suggestions/sonic'))).toBe(false)
    expect(plain(document.querySelector('[data-testid="sonic-note"]')!.textContent)).toContain('Sie ist in deiner Bibliothek ausgeschaltet.')
    expect(document.querySelector('[data-testid="sonic-note"] a')).toBeNull()
  })

  it('never suggests a song of the list, also one older than the last five', async () => {
    const items = [entry(10, 302, 'Glasshouse'), entry(1, 301, 'Northern Lights'), entry(2, 311, 'Straße der Träume'), entry(3, 317, 'Señorita Lluvia'), entry(4, 312, 'Ärger im Café'), entry(5, 313, 'Abendlied')]
    server.answers['GET /api/v1/playlists/7'] = playlist({ items, count: 6 })
    const seed = [301, 311, 317, 312, 313]
    // Without the rest of the list, Glasshouse would be among them.
    expect(suggest(ix, seedPositions(ix, seed), { exclude: seed, limit: 50 }).map((track) => track.song)).toContain(302)
    await open()
    expect(plain(document.querySelector('[data-testid="seed-label"]')!.textContent)).toBe('Passend zu den letzten 5 Titeln der Liste')
    expect(titles()).toEqual(suggest(ix, seedPositions(ix, seed), { exclude: [302, ...seed] }).map((track) => track.title))
    expect(titles()).not.toContain('Glasshouse')
  })

  it('reads the reason in words', async () => {
    server.answers['GET /api/v1/playlists/7'] = playlist({ items: [entry(2, 311, 'Straße der Träume')] })
    await open()
    const row = [...document.querySelectorAll<HTMLElement>('[data-testid="suggestion"]')].find((one) => one.querySelector('.t')!.textContent === 'Ärger im Café')!
    expect(row.querySelector('.why')!.textContent).toBe('Bittersüß, Romantisch · Chanson · 70er')
  })

  it('switches the kind', async () => {
    await open()
    await page.click(page.button('Entdeckungen'))
    await until(() => (JSON.stringify(titles()) === JSON.stringify(expected([301, 311, 317], 'new')) ? true : null))
    expect(page.button('Entdeckungen')!.getAttribute('aria-pressed')).toBe('true')
  })

  it('adds a suggestion with +', async () => {
    const [first] = suggest(ix, seedPositions(ix, [301, 311, 317]), { exclude: [301, 311, 317] })
    server.answers['POST /api/v1/playlists/7/items'] = { version: 6, op: 'add', entries: [entry(9, first.id, first.title)], order: [1, 2, 3, 9] } satisfies Change
    await open()
    await page.click(document.querySelector<HTMLButtonElement>('[data-testid="suggestion"] button.add'))
    expect(server.called('POST', '/api/v1/playlists/7/items')!.body).toEqual({ tracks: [first.id] })
  })

  it('does not suggest a song again, and the undo brings it back', async () => {
    const [first] = suggest(ix, seedPositions(ix, [301, 311, 317]), { exclude: [301, 311, 317] })
    server.answers['POST /api/v1/suggestions/hidden'] = { songs: [first.song] }
    server.answers[`DELETE /api/v1/suggestions/hidden/${first.id}`] = { songs: [] }
    await open()
    await page.click(page.button(`„${first.title}“ nicht mehr vorschlagen`))
    expect(server.called('POST', '/api/v1/suggestions/hidden')!.body).toEqual({ track: first.id })
    await until(() => (!titles().includes(first.title) ? true : null))
    expect(plain(document.querySelector('[data-testid="toast"]')!.textContent)).toBe('Wird nicht mehr vorgeschlagenRückgängig')
    await page.click(page.button('Rückgängig'))
    expect(server.changes()).toEqual(['POST /api/v1/suggestions/hidden', `DELETE /api/v1/suggestions/hidden/${first.id}`])
    await until(() => (titles()[0] === first.title ? true : null))
  })

  it('suggests for one track of the list and goes back to the last five', async () => {
    await open()
    await page.click(page.button('Vorschläge zu „Straße der Träume“'))
    await until(() => (plain(document.querySelector('[data-testid="seed-label"]')!.textContent).startsWith('Ähnlich zu') ? true : null))
    expect(plain(document.querySelector('[data-testid="seed-label"]')!.textContent)).toBe('Ähnlich zu Straße der Träume zurück zu den letzten 5 ×')
    await until(() => (JSON.stringify(titles()) === JSON.stringify(expected([311])) ? true : null))
    await page.click(page.button('zurück zu den letzten 5 ×'))
    expect(plain(document.querySelector('[data-testid="seed-label"]')!.textContent)).toBe('Passend zu den letzten 3 Titeln der Liste')
  })

  it('asks Plex for what sounds alike while the analysis runs, and says how far it is', async () => {
    server.answers['GET /api/v1/catalog/status'] = { ...STATUS, analysis: { state: 'running', percent: 40 } }
    await open()
    await until(() => server.calls.find((call) => call.url === '/api/v1/suggestions/sonic?track=301&track=311&track=317'))
    expect(plain(document.querySelector('[data-testid="sonic-note"]')!.textContent)).toContain('40 % sind fertig')
  })

  it('starts with a mood when the list is empty', async () => {
    server.answers['GET /api/v1/playlists/7'] = playlist({ items: [], count: 0, duration: 0, moods: [] })
    await page.show(
      <Routes>
        <Route path="/playlists/:id" element={<PlaylistEditor />} />
      </Routes>,
      '/playlists/7',
    )
    const empty = await until(() => document.querySelector<HTMLElement>('[data-testid="suggest-empty"]'))
    expect(plain(empty.textContent)).toContain('Noch keine Grundlage')
    await page.click(page.button('Mit Stimmung füllen', empty))
    expect(page.dialog).toBeTruthy()
  })
})

describe('fill by mood', () => {
  it('proposes, lets ticks go, and takes the rest in one step with one undo', async () => {
    await open()
    await page.click(page.button('Mit Stimmung füllen', document.querySelector('.plhead')!))
    const dialog = page.dialog!
    expect(plain(dialog.querySelector('h2')!.textContent)).toBe('Mit Stimmung füllen')
    expect(plain(dialog.textContent)).toContain('Mindestens eine Stimmung wählen')
    expect(page.button('Vorschlag erzeugen', dialog)!.disabled).toBe(true)
    // The groups with their moods and how many tracks have each.
    const groups = [...dialog.querySelectorAll('.moodgroup h4')].map((title) => title.textContent)
    expect(groups).toEqual(['Ruhig', 'Gefühlvoll', 'Gute Laune', 'Party', 'Dunkel und hart'])
    const summery = [...dialog.querySelectorAll<HTMLButtonElement>('.moodgroup .chip')].find((chip) => chip.textContent!.startsWith('Sommerlich'))!
    expect(plain(summery.textContent)).toMatch(/^Sommerlich \d+$/)
    await page.click(summery)
    await page.click([...dialog.querySelectorAll<HTMLButtonElement>('.moodgroup .chip')].find((chip) => chip.textContent!.startsWith('Fröhlich'))!)
    expect(plain(dialog.querySelector('[data-testid="mood-chosen"]')!.textContent)).toBe('2 Stimmungen gewählt')
    await page.click(page.button('Vorschlag erzeugen', dialog))
    const picks = await until(() => {
      const found = [...dialog.querySelectorAll<HTMLElement>('[data-testid="mood-pick"]')]
      return found.length ? found : null
    })
    expect(picks.every((pick) => pick.querySelector('[data-testid="play"]'))).toBe(true)
    // Nothing of the playlist is proposed.
    const proposed = picks.map((pick) => pick.querySelector('.t')!.textContent)
    expect(proposed).not.toContain('Señorita Lluvia')
    expect(server.changes()).toEqual([])
    const tracks = picks.map((pick) => Number(pick.querySelector<HTMLElement>('[data-testid="play"]')!.dataset.track))
    if (picks.length > 1) await page.click(picks[0].querySelector<HTMLInputElement>('input[type="checkbox"]'))
    const kept = picks.length > 1 ? tracks.slice(1) : tracks
    expect(plain(dialog.querySelector('[data-testid="mood-summary"]')!.textContent)).toMatch(new RegExp(`^Vorschlag: ${kept.length} Titel · `))
    expect(page.button('Neu mischen', dialog)).toBeTruthy()
    server.answers['POST /api/v1/playlists/7/items'] = { version: 6, op: 'add', entries: kept.map((track, index) => entry(20 + index, track, `T${track}`)), order: [1, 2, 3, ...kept.map((_track, index) => 20 + index)] } satisfies Change
    server.answers['POST /api/v1/playlists/7/items/remove'] = { version: 7, op: 'remove', entries: [], order: [1, 2, 3] } satisfies Change
    await page.inDialog('Übernehmen')
    expect(page.dialog).toBeNull()
    expect(server.called('POST', '/api/v1/playlists/7/items')!.body).toEqual({ tracks: kept })
    expect(plain(document.querySelector('[data-testid="toast"]')!.textContent)).toMatch(/hinzugefügtRückgängig$/)
    await page.click(page.button('Rückgängig'))
    expect(server.called('POST', '/api/v1/playlists/7/items/remove')!.body).toEqual({ entries: kept.map((_track, index) => 20 + index) })
  })

  it('adds nothing when it is closed', async () => {
    await open()
    await page.click(page.button('Mit Stimmung füllen', document.querySelector('.plhead')!))
    await page.click([...page.dialog!.querySelectorAll<HTMLButtonElement>('.moodgroup .chip')][0])
    await page.click(page.button('Vorschlag erzeugen', page.dialog!))
    await until(() => page.dialog!.querySelector('[data-testid="mood-pick"], [data-testid="mood-nothing"]'))
    await page.click(page.button('Schließen', page.dialog!))
    expect(page.dialog).toBeNull()
    expect(server.changes()).toEqual([])
  })

  it('is not there for a playlist shared to see', async () => {
    server.answers['GET /api/v1/playlists/7'] = playlist({ access: 'read' })
    await open()
    expect(page.button('Mit Stimmung füllen', document.querySelector('.plhead')!)).toBeUndefined()
    expect(document.querySelector('[data-testid="suggestion"] button.add')).toBeNull()
  })
})

describe('listening in', () => {
  const play = (index: number) => document.querySelectorAll<HTMLButtonElement>('[data-testid="track-row"] [data-testid="play"]')[index]

  it('plays one preview at a time, stops it, and fills the ring', async () => {
    await open()
    await page.click(play(0))
    expect(audio.getAttribute('src')).toBe('/api/v1/preview/301')
    expect(audio.played).toEqual(['/api/v1/preview/301'])
    expect(play(0).classList.contains('on')).toBe(true)
    expect(play(0).getAttribute('aria-label')).toBe('Hörprobe von „Northern Lights“ stoppen')
    // Half way: the ring is half full.
    audio.currentTime = 15
    await act(async () => audio.dispatchEvent(new Event('timeupdate')))
    expect(play(0).style.getPropertyValue('--p')).toBe('50')
    // Another ▶ takes over: the first one stops, only one plays.
    await page.click(play(1))
    expect(audio.getAttribute('src')).toBe('/api/v1/preview/311')
    // Wherever the track stands (the list, the search), its ▶ shows it; no other one does.
    const on = [...document.querySelectorAll<HTMLElement>('[data-testid="play"].on')].map((button) => button.dataset.track)
    expect(on.length).toBeGreaterThan(0)
    expect(new Set(on)).toEqual(new Set(['311']))
    expect(play(0).classList.contains('on')).toBe(false)
    // The same ▶ again stops it: no address left, the browser lets the connection go.
    await page.click(play(1))
    expect(audio.getAttribute('src')).toBeNull()
    expect(audio.paused).toBe(true)
    expect(document.querySelectorAll('[data-testid="play"].on').length).toBe(0)
  })

  it('stops by itself after 30 seconds', async () => {
    await open()
    await page.click(play(0))
    audio.currentTime = 30.2
    await act(async () => audio.dispatchEvent(new Event('timeupdate')))
    expect(audio.getAttribute('src')).toBeNull()
    expect(play(0).classList.contains('on')).toBe(false)
  })

  it('stops when the page goes', async () => {
    await open()
    await page.click(play(0))
    page.close()
    expect(audio.getAttribute('src')).toBeNull()
    await page.show(<p />)
  })

  it('says why a preview did not play', async () => {
    await open()
    server.answers['GET /api/v1/preview'] = { allowed: true, seconds: 30, per_hour: 120, left: 0, retry_after: 600 }
    await page.click(play(0))
    await act(async () => audio.dispatchEvent(new Event('error')))
    await until(() => document.querySelector('[data-testid="toast"]'))
    expect(plain(document.querySelector('[data-testid="toast"]')!.textContent)).toBe('Genug reingehört für diese Stunde. In 10 Min. geht es wieder.')
    expect(play(0).classList.contains('on')).toBe(false)
  })

  it('shows no ▶ while listening in is switched off', async () => {
    server.answers['GET /api/v1/preview'] = { allowed: false, seconds: 30, per_hour: 120, left: 120, retry_after: null }
    await open()
    await until(() => (document.querySelectorAll('[data-testid="play"]').length === 0 ? true : null))
  })
})
