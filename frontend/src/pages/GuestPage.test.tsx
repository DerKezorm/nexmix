/**
 * The page of an invitation's link against a stand-in server and the made-up catalogue: the playlist and the name field
 * before joining (nothing sent without a name), the list with the guest's share and its own entries to take out (with
 * the undo), the search with "in" and adding up to the limit, sorting only with the right, and the friendly page when the
 * invitation is over, the guest was taken out, or the code leads nowhere.
 */
import { Route, Routes } from 'react-router-dom'

import type { Door, GuestEntry, GuestState } from '../api/guests'
import { changeLanguage } from '../i18n'
import { Page, Server, plain } from '../test/harness'
import { until } from '../test/wait'
import { GuestPage } from './GuestPage'

const fs = (await import(/* @vite-ignore */ ['node', 'fs'].join(':'))) as unknown as { readFileSync(path: string, encoding: 'utf-8'): string }
const CATALOG = JSON.parse(fs.readFileSync('../backend/tests/fixtures/search/catalog.json', 'utf-8'))

const server = new Server()
const page = new Page()
const CODE = 'K7MX-QR2H-PA4N'

const door = (over: Partial<Door> = {}): Door => ({ playlist: { name: 'Grillabend', count: 2, duration: 446, covers: [201] }, inviter: 'Olivia', joined: false, name: null, ...over })

const entry = (id: number, track: number, title: string, by: string, mine = false): GuestEntry => ({
  id, track, title, artist: 'Someone', album: 'Some Album', album_id: 201, cover: true, year: 1984, duration: 200, song: track, missing: false, by, mine,
})

function state(over: Partial<GuestState['invite']> = {}, items: GuestEntry[] = [entry(1, 301, 'Northern Lights', 'Olivia'), entry(2, 302, 'Glasshouse', 'Lena', true)]): GuestState {
  return {
    me: { id: 9, name: 'Lena', count: items.filter((one) => one.mine).length },
    invite: { may_add: true, may_sort: false, max: 3, preview: false, expires_at: '2026-11-14T18:00:00Z', ...over },
    playlist: { name: 'Grillabend', inviter: 'Olivia', version: 4, count: items.length, duration: 400, covers: [201], items },
    catalog: { ready: true, search: 'browser' },
  }
}

beforeEach(async () => {
  await changeLanguage('de', false)
  server.answers = {
    [`GET /api/v1/join/${CODE}`]: door(),
    'GET /api/v1/guest': state(),
    'GET /api/v1/guest/catalog': CATALOG,
  }
  server.fails = {}
  server.install()
})
afterEach(() => {
  page.close()
  vi.unstubAllGlobals()
})

async function open(): Promise<void> {
  await page.show(
    <Routes>
      <Route path="/j/:code" element={<GuestPage />} />
    </Routes>,
    `/j/${CODE}`,
  )
}

const listRows = () => [...document.querySelectorAll<HTMLElement>('[data-testid="guest-list"] .row')]
const tab = (name: string) => [...document.querySelectorAll<HTMLButtonElement>('.ph-tabs button')].find((button) => button.textContent === name)

describe('the page of the link', () => {
  it('shows the playlist and who invites, and joins with a name only', async () => {
    server.answers[`POST /api/v1/join/${CODE}`] = { name: 'Lena' }
    await open()
    await until(() => document.querySelector('[data-testid="guest-join"]'))
    const join = plain(document.querySelector('[data-testid="guest-join"]')!.textContent)
    expect(join).toContain('Grillabend')
    expect(join).toContain('Olivia lädt dich ein, Musik beizusteuern.')
    expect(join).toContain('2 Titel · 7 Min.')
    expect(join).toContain('Dein Name steht an den Titeln, die du hinzufügst. Mehr speichert nexmix nicht.')
    expect(document.querySelector<HTMLImageElement>('.mosaic img')!.getAttribute('src')).toBe(`/api/v1/join/${CODE}/cover/201?s=400`)
    const name = document.querySelector<HTMLInputElement>('input[placeholder="Wie heißt du?"]')!
    expect(name.maxLength).toBe(24)
    await page.click(page.button('Mitmachen'))
    expect(server.changes()).toEqual([])
    await page.type(name, '  Lena ')
    await page.click(page.button('Mitmachen'))
    expect(server.called('POST', `/api/v1/join/${CODE}`)!.body).toEqual({ name: 'Lena' })
    await until(() => document.querySelector('[data-testid="guest-title"]'))
    expect(document.querySelector('[data-testid="guest-title"]')!.textContent).toBe('Grillabend')
  })

  it('says "someone from the household" for a person without a display name', async () => {
    server.answers[`GET /api/v1/join/${CODE}`] = door({ inviter: '' })
    await open()
    await until(() => document.querySelector('[data-testid="guest-join"]'))
    expect(plain(document.querySelector('[data-testid="guest-join"]')!.textContent)).toContain('Jemand aus dem Haushalt lädt dich ein, Musik beizusteuern.')
    page.close()
    server.answers[`GET /api/v1/join/${CODE}`] = door({ joined: true, name: 'Lena' })
    server.answers['GET /api/v1/guest'] = state({}, [entry(1, 301, 'Northern Lights', ''), entry(2, 302, 'Glasshouse', 'Lena', true)])
    await open()
    await until(() => document.querySelector('[data-testid="guest-list"] .row'))
    expect(listRows().map((row) => plain([...row.querySelector('.by')!.childNodes].at(-1)!.textContent))).toEqual(['jemand aus dem Haushalt', 'Lena'])
  })

  it('offers no undo of taking out where the invitation does not let guests add', async () => {
    server.answers[`GET /api/v1/join/${CODE}`] = door({ joined: true, name: 'Lena' })
    server.answers['GET /api/v1/guest'] = state({ may_add: false })
    server.answers['POST /api/v1/guest/items/remove'] = { op: 'remove', state: state({ may_add: false }, [entry(1, 301, 'Northern Lights', 'Olivia')]) }
    await open()
    await until(() => document.querySelector('[data-testid="guest-list"] .row'))
    await page.click(page.button('„Glasshouse“ entfernen'))
    expect(plain(document.querySelector('[data-testid="toast"]')!.textContent)).toContain('„Glasshouse“ entfernt')
    expect(page.button('Rückgängig')).toBeUndefined()
  })

  it('says what is wrong with a name the server refuses', async () => {
    server.fails[`POST /api/v1/join/${CODE}`] = { status: 422, code: 'bad_name', values: { max: 24 } }
    await open()
    await until(() => document.querySelector('[data-testid="guest-join"]'))
    await page.type(document.querySelector<HTMLInputElement>('input[placeholder="Wie heißt du?"]'), 'x')
    await page.click(page.button('Mitmachen'))
    expect(plain(document.querySelector('[role="alert"]')!.textContent)).toBe('Ein Name hat 1 bis 24 Zeichen, ohne Steuerzeichen.')
  })

  it('shows the list with the share of the guest; only its own entries can be taken out, with an undo', async () => {
    server.answers[`GET /api/v1/join/${CODE}`] = door({ joined: true, name: 'Lena' })
    const after = state({}, [entry(1, 301, 'Northern Lights', 'Olivia')])
    server.answers['POST /api/v1/guest/items/remove'] = { op: 'remove', state: after }
    server.answers['POST /api/v1/guest/items/restore'] = { op: 'restore', state: state() }
    await open()
    await until(() => document.querySelector('[data-testid="guest-quota"]'))
    expect(plain(document.querySelector('[data-testid="guest-quota"]')!.textContent)).toBe('Du hast 1 von 3 Titeln beigesteuert')
    // A guest has the one playlist, no cards and no menu "…" of a card.
    expect(document.querySelector('[data-testid="card-menu"], [role="menu"], [data-testid="playlist-card"]')).toBeNull()
    expect(listRows().map((row) => plain([...row.querySelector('.by')!.childNodes].at(-1)!.textContent))).toEqual(['Olivia', 'Lena'])
    expect(page.button('„Northern Lights“ entfernen')).toBeUndefined()
    expect(page.button('„Northern Lights“ nach unten')).toBeUndefined()
    await page.click(page.button('„Glasshouse“ entfernen'))
    expect(server.called('POST', '/api/v1/guest/items/remove')!.body).toEqual({ entries: [2] })
    expect(listRows()).toHaveLength(1)
    expect(plain(document.querySelector('[data-testid="toast"]')!.textContent)).toContain('„Glasshouse“ entfernt')
    await page.click(page.button('Rückgängig'))
    expect(server.called('POST', '/api/v1/guest/items/restore')!.body).toEqual({ entries: [2], after: 1 })
    expect(listRows()).toHaveLength(2)
  })

  it('sorts and takes out every entry only with the right to sort', async () => {
    server.answers[`GET /api/v1/join/${CODE}`] = door({ joined: true })
    server.answers['GET /api/v1/guest'] = state({ may_sort: true })
    server.answers['POST /api/v1/guest/items/move'] = { op: 'move', state: state({ may_sort: true }) }
    await open()
    await until(() => document.querySelector('[data-testid="guest-list"] .row'))
    expect(page.button('„Northern Lights“ entfernen')).toBeTruthy()
    expect(page.button('„Northern Lights“ nach oben')!.disabled).toBe(true)
    await page.click(page.button('„Northern Lights“ nach unten'))
    expect(server.called('POST', '/api/v1/guest/items/move')!.body).toEqual({ entry: 1, after: 2 })
    await page.click(page.button('„Glasshouse“ nach oben'))
    expect(server.calls.filter((call) => call.url === '/api/v1/guest/items/move').at(-1)!.body).toEqual({ entry: 2, after: null })
  })

  it('searches the catalogue, marks what is in, and adds until the limit', async () => {
    server.answers[`GET /api/v1/join/${CODE}`] = door({ joined: true })
    const full = state({}, [entry(1, 301, 'Northern Lights', 'Olivia'), entry(2, 302, 'Glasshouse', 'Lena', true), entry(3, 303, 'Night Drive', 'Lena', true), entry(4, 304, 'X', 'Lena', true)])
    server.answers['POST /api/v1/guest/items'] = { op: 'add', state: full }
    await open()
    await until(() => tab('Suchen'))
    await page.click(tab('Suchen'))
    const search = await until(() => document.querySelector<HTMLInputElement>('input[type="search"]'))
    expect(search.placeholder).toBe('Titel oder Künstler')
    await page.type(search, 'glass')
    const hit = await until(() => [...document.querySelectorAll<HTMLElement>('[data-testid="guest-hits"] .row')].find((row) => row.textContent?.includes('Glasshouse')))
    expect(hit.querySelector('.round.done')).toBeTruthy()
    await page.type(search, 'night')
    const night = await until(() => [...document.querySelectorAll<HTMLElement>('[data-testid="guest-hits"] .row')].find((row) => row.textContent?.includes('Night Drive')))
    await page.click(night.querySelector<HTMLButtonElement>('button.add'))
    expect(server.called('POST', '/api/v1/guest/items')!.body).toEqual({ tracks: [303] })
    expect(plain(document.querySelector('[data-testid="toast"]')!.textContent)).toBe('„Night Drive“ ist drin')
    // Now at the limit: every add button waits.
    await page.type(search, 'lights')
    await until(() => document.querySelector('[data-testid="guest-hits"] .row'))
    const adds = [...document.querySelectorAll<HTMLButtonElement>('[data-testid="guest-hits"] button.add')]
    expect(adds.length).toBeGreaterThan(0)
    for (const button of adds) expect(button.disabled).toBe(true)
  })

  it('shows no add button where the invitation does not let guests add, and no share line', async () => {
    server.answers[`GET /api/v1/join/${CODE}`] = door({ joined: true })
    server.answers['GET /api/v1/guest'] = state({ may_add: false })
    await open()
    await until(() => document.querySelector('[data-testid="guest-list"]'))
    expect(document.querySelector('[data-testid="guest-quota"]')).toBeNull()
    await page.click(tab('Suchen'))
    await until(() => document.querySelector('[data-testid="guest-hits"] .row'))
    expect(document.querySelector('[data-testid="guest-hits"] button.add')).toBeNull()
  })

  it('suggests what fits the list, with the moods it shares', async () => {
    server.answers[`GET /api/v1/join/${CODE}`] = door({ joined: true })
    await open()
    await until(() => tab('Vorschläge'))
    await page.click(tab('Vorschläge'))
    await until(() => document.querySelector('[data-testid="guest-ideas"] .row'))
    expect(document.querySelector('.group-title')!.textContent).toBe('Passt zur Liste')
    const titles = [...document.querySelectorAll('[data-testid="guest-ideas"] .row .t')].map((one) => one.textContent)
    expect(titles).not.toContain('Northern Lights')
    expect(titles).not.toContain('Glasshouse')
  })

  it('suggests by the editor’s rules from this list only, and asks no voice of other playlists', async () => {
    const { prepare } = await import('../lib/catalogSearch')
    const { seedPositions, suggest } = await import('../lib/suggest')
    const ix = prepare(CATALOG)
    server.answers[`GET /api/v1/join/${CODE}`] = door({ joined: true })
    await open()
    await until(() => tab('Vorschläge'))
    await page.click(tab('Vorschläge'))
    await until(() => document.querySelector('[data-testid="guest-ideas"] .row'))
    const songs = [301, 302].map((track) => ix.song[ix.position.get(track)!])
    const wanted = suggest(ix, seedPositions(ix, [301, 302]), { mode: 'fit', exclude: songs, limit: 15 }).map((one) => one.title)
    expect([...document.querySelectorAll('[data-testid="guest-ideas"] .row .t')].map((one) => one.textContent)).toEqual(wanted)
    expect(document.querySelector('[data-testid="guest-ideas"] .why')!.textContent).not.toBe('')
    expect(server.calls.filter((call) => call.url.startsWith('/api/v1/suggestions'))).toEqual([])
  })

  it('asks the server for the suggestions where the catalogue is too large for the browser', async () => {
    server.answers[`GET /api/v1/join/${CODE}`] = door({ joined: true })
    server.answers['GET /api/v1/guest'] = { ...state(), catalog: { ready: true, search: 'server' } }
    server.answers['GET /api/v1/guest/suggestions'] = {
      tracks: [{ id: 317, song: 317, title: 'Paper Moon', artist: 'Someone Else', album: 'X', album_id: 201, cover: false, year: 1984, duration: 200, reason: { moods: ['Mellow'], style: null, decade: 1980, together: 0, sound: false } }],
      seed: [301, 302],
      took_ms: 3,
    }
    await open()
    await until(() => tab('Vorschläge'))
    await page.click(tab('Vorschläge'))
    const row = await until(() => document.querySelector('[data-testid="guest-ideas"] .row'))
    expect(row.querySelector('.t')!.textContent).toBe('Paper Moon')
    expect(plain(row.querySelector('.why')!.textContent)).toBe('Entspannt · 80er')
    expect(server.called('GET', '/api/v1/guest/suggestions')).toBeTruthy()
  })

  it('shows nothing of the sync with Plex: no state, no "sync now"', async () => {
    server.answers[`GET /api/v1/join/${CODE}`] = door({ joined: true })
    await open()
    await until(() => document.querySelector('[data-testid="guest-list"] .row'))
    const text = plain(document.body.textContent)
    for (const word of ['Plex', 'Jetzt abgleichen', 'Erneut versuchen']) expect(text).not.toContain(word)
    expect(server.calls.filter((call) => call.url.includes('/sync'))).toEqual([])
  })

  it('offers listening only where the invitation allows it', async () => {
    server.answers[`GET /api/v1/join/${CODE}`] = door({ joined: true })
    await open()
    await until(() => document.querySelector('[data-testid="guest-list"] .row'))
    expect(document.querySelector('.round.play')).toBeNull()
    page.close()
    server.answers['GET /api/v1/guest'] = state({ preview: true })
    await open()
    await until(() => document.querySelector('[data-testid="guest-list"] .row'))
    expect(document.querySelectorAll('[data-testid="guest-list"] .round.play')).toHaveLength(2)
  })

  it('listens through the server with the player of the editor, and says why a preview did not play', async () => {
    const { act } = await import('react')
    const { setAudioFactory, stop } = await import('../lib/preview')
    const audio = Object.assign(new EventTarget(), {
      preload: '',
      currentTime: 0,
      src: null as string | null,
      setAttribute(_name: string, value: string) { this.src = value },
      getAttribute() { return this.src },
      removeAttribute() { this.src = null },
      play: () => Promise.resolve(),
      pause: () => undefined,
      load: () => undefined,
    })
    setAudioFactory(() => audio as unknown as HTMLAudioElement)
    server.answers[`GET /api/v1/join/${CODE}`] = door({ joined: true })
    server.answers['GET /api/v1/guest'] = state({ preview: true })
    server.answers['GET /api/v1/preview'] = { allowed: true, seconds: 30, per_hour: 120, left: 0, retry_after: 600 }
    await open()
    await until(() => document.querySelector('[data-testid="guest-list"] .row'))
    await page.click(page.button('„Northern Lights“ reinhören'))
    expect(audio.src).toBe('/api/v1/preview/301')
    expect(page.button('Hörprobe von „Northern Lights“ stoppen')).toBeTruthy()
    await act(async () => audio.dispatchEvent(new Event('error')))
    await until(() => document.querySelector('[data-testid="toast"]'))
    expect(plain(document.querySelector('[data-testid="toast"]')!.textContent)).toBe('Genug reingehört für diese Stunde. In 10 Min. geht es wieder.')
    // Leaving the page lets the sound go.
    await page.click(page.button('„Glasshouse“ reinhören'))
    page.close()
    expect(audio.src).toBeNull()
    stop()
  })

  it.each([
    ['invite_ended', 'ended', 'Die Einladung ist vorbei'],
    ['guest_removed', 'removed', 'Du bist nicht mehr dabei'],
    ['guest_required', 'unknown', 'Diese Einladung gilt nicht'],
  ])('a code that leads nowhere any more: the guest hears why (%s)', async (code, over, title) => {
    server.fails[`GET /api/v1/join/${CODE}`] = { status: 404, code: 'invite_unknown' }
    server.fails['GET /api/v1/guest'] = { status: code === 'guest_required' ? 401 : 403, code }
    await open()
    const shown = await until(() => document.querySelector<HTMLElement>('[data-testid="guest-over"]'))
    expect(shown.dataset.over).toBe(over)
    expect(plain(shown.textContent)).toContain(title)
  })

  it('a guest taken out sees it on the page of the link', async () => {
    server.fails[`GET /api/v1/join/${CODE}`] = { status: 403, code: 'guest_removed' }
    await open()
    const shown = await until(() => document.querySelector<HTMLElement>('[data-testid="guest-over"]'))
    expect(shown.dataset.over).toBe('removed')
  })

  it('a change refused because the invitation ended shows the end', async () => {
    server.answers[`GET /api/v1/join/${CODE}`] = door({ joined: true })
    server.fails['POST /api/v1/guest/items/remove'] = { status: 403, code: 'invite_ended' }
    await open()
    await until(() => page.button('„Glasshouse“ entfernen'))
    await page.click(page.button('„Glasshouse“ entfernen'))
    const shown = await until(() => document.querySelector<HTMLElement>('[data-testid="guest-over"]'))
    expect(shown.dataset.over).toBe('ended')
  })
})
