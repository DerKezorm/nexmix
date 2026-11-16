/**
 * Music Assistant from end to end, against the stand-in Music Assistant behind a real websocket and its made-up library
 * (the made-up music of the stand-in Plex, and a track of its own): the operator connects it in the settings by address
 * and token, chooses the provider, the catalogue comes; a playlist is made with Music Assistant as its source and a track
 * added: it is saved there, and a track added in Music Assistant's own interface shows up in the open playlist. Then a
 * playlist of Plex is copied to Music Assistant. Runs in Chromium, Firefox and WebKit; each browser connects anew and
 * leaves nothing behind.
 */
import { expect, request, test, type APIRequestContext } from '@playwright/test'

import { TAB } from './global-setup'

async function letGo(api: APIRequestContext): Promise<void> {
  for (const one of (await (await api.get('/api/v1/music-assistant')).json()).servers as { id: number }[]) await api.delete(`/api/v1/music-assistant/${one.id}`, { headers: TAB })
}

test.afterEach(async ({ baseURL }) => {
  const operator = await request.newContext({ baseURL, storageState: process.env.E2E_STATE, extraHTTPHeaders: TAB })
  for (const one of (await (await operator.get('/api/v1/playlists')).json()).playlists as { id: number; name: string }[]) {
    if (one.name.startsWith('MA ')) {
      await operator.delete(`/api/v1/playlists/${one.id}`)
      await operator.delete(`/api/v1/playlists/${one.id}?purge=true`)
    }
  }
  await letGo(operator)
  await operator.dispose()
})

async function token(): Promise<string> {
  const ma = await request.newContext({ baseURL: process.env.E2E_MA })
  const found = (await (await ma.get('/fake/token')).json()).token as string
  await ma.dispose()
  return found
}

/** Plex connected through the stand-in and its catalogue there (once per run, whichever test comes first). */
async function plexReady(api: APIRequestContext): Promise<void> {
  const status = await (await api.get('/api/v1/catalog/status')).json()
  if (status.ready) return
  if (!status.connected) {
    const pin = await (await api.post('/api/v1/plex/pin', { headers: TAB })).json()
    const plex = await request.newContext({ baseURL: process.env.E2E_PLEX })
    await plex.post('/fake/approve')
    await plex.dispose()
    let servers: { index: number }[] = []
    await expect(async () => {
      const state = await (await api.get(`/api/v1/plex/pin/${pin.id}`)).json()
      expect(state.done).toBe(true)
      servers = state.servers
    }).toPass({ timeout: 15_000 })
    const connected = await api.post('/api/v1/plex/connect', { data: { pin_id: pin.id, server: servers[0].index }, headers: TAB })
    expect(connected.ok(), await connected.text()).toBe(true)
  }
  await expect(async () => expect((await (await api.get('/api/v1/catalog/status')).json()).ready).toBe(true)).toPass({ timeout: 60_000 })
}

test('Music Assistant is connected by its token, a playlist takes its tracks from it and is saved there', async ({ page, browserName }) => {
  test.setTimeout(120_000)
  await letGo(page.request)
  const ma = await request.newContext({ baseURL: process.env.E2E_MA })

  // The settings: add a server, Music Assistant, the address and the token, then the provider.
  await page.goto('/settings')
  await page.getByTestId('servers-add').getByRole('button', { name: 'Music Assistant', exact: true }).click()
  const box = page.locator('#ma-new')
  await box.getByLabel('Address').fill(process.env.E2E_MA!)
  await box.getByLabel('Token').fill(await token())
  await box.getByRole('button', { name: 'Connect' }).click()
  const providers = page.getByTestId('ma-providers')
  const connected = page.getByTestId('ma-connected')
  await expect(providers.or(connected)).toBeVisible({ timeout: 15_000 })
  if (await providers.isVisible()) {
    await providers.getByRole('checkbox', { name: 'Musik' }).check()
    await providers.getByRole('button', { name: 'Use these providers' }).click()
  }
  const card = page.locator('[id^="ma-"]').filter({ has: connected })
  await expect(card.getByTestId('server-catalog')).toContainText('tracks', { timeout: 60_000 })
  await expect(card.getByTestId('ma-token-until')).toContainText('valid until')
  // Nothing of the token or the address in the page.
  const html = await page.content()
  expect(html).not.toContain(await token())
  const servers = (await (await page.request.get('/api/v1/servers')).json()).servers as { id: number; kind: string }[]
  const assistant = servers.find((one) => one.kind === 'music_assistant')!.id

  // A playlist with Music Assistant as its source.
  const name = `MA ${browserName}`
  const made = await (await page.request.post('/api/v1/playlists', { data: { name, server: assistant }, headers: TAB })).json()
  await page.goto(`/playlists/${made.id}`)
  await expect(page.getByTestId('no-moods')).toContainText('No moods')
  await page.getByLabel('Search the library').fill('small hours')
  const hit = page.getByTestId('search-track').filter({ hasText: 'Small Hours' }).first()
  await expect(hit).toBeVisible()
  await hit.getByRole('button', { name: 'Add “Small Hours”' }).click()
  await expect(page.getByTestId('track-row').filter({ hasText: 'Small Hours' })).toHaveCount(1)

  // Saved in Music Assistant, and there the stand-in holds it.
  await expect(page.getByTestId('sync-state')).toHaveText('Saved in Music Assistant', { timeout: 20_000 })
  await expect.poll(async () => (await (await ma.get(`/fake/list?name=${encodeURIComponent(name)}`)).json()).tracks, { timeout: 20_000 }).toEqual(['Small Hours'])
  // Added in Music Assistant's own interface: it shows up in the open playlist at once (its event).
  expect((await ma.get(`/fake/ui-add?playlist=${encodeURIComponent(name)}&track=${encodeURIComponent('Fever Glass')}`)).ok()).toBe(true)
  await expect(page.getByTestId('track-row').filter({ hasText: 'Fever Glass' })).toHaveCount(1, { timeout: 30_000 })
  await ma.dispose()
})

test('a token of a user without full rights in Music Assistant is refused, and nothing is kept', async ({ page }) => {
  test.setTimeout(60_000)
  await letGo(page.request)
  const ma = await request.newContext({ baseURL: process.env.E2E_MA })
  const weak = (await (await ma.get('/fake/token?role=user')).json()).token as string
  await ma.dispose()
  await page.goto('/settings')
  await page.getByTestId('servers-add').getByRole('button', { name: 'Music Assistant', exact: true }).click()
  const box = page.locator('#ma-new')
  await box.getByLabel('Address').fill(process.env.E2E_MA!)
  await box.getByLabel('Token').fill(weak)
  await box.getByRole('button', { name: 'Connect' }).click()
  await expect(box).toContainText('without full rights', { timeout: 15_000 })
  await expect(page.getByTestId('ma-connected')).toHaveCount(0)
  // The token stays in the field to be corrected; the refusal itself never repeats it, and nothing is kept.
  expect((await box.textContent()) ?? '').not.toContain(weak)
  expect(await (await page.request.get('/api/v1/music-assistant')).text()).not.toContain(weak)
  const servers = (await (await page.request.get('/api/v1/servers')).json()).servers as { kind: string; active: boolean }[]
  expect(servers.filter((one) => one.kind === 'music_assistant' && one.active)).toEqual([])
})

test('a track replaced in Music Assistant while nexmix takes one out there is told at the playlist, until seen', async ({ page, browserName }) => {
  test.setTimeout(120_000)
  const api = page.request
  await letGo(api)
  const connected = await (await api.post('/api/v1/music-assistant/connect', { data: { url: process.env.E2E_MA, token: await token() }, headers: TAB })).json()
  const assistant = connected.server.id as number
  if (!connected.server.library) {
    const musik = (connected.providers as { handle: string; name: string }[]).find((one) => one.name === 'Musik')!
    expect((await api.put(`/api/v1/music-assistant/${assistant}/providers`, { data: { handles: [musik.handle] }, headers: TAB })).ok()).toBe(true)
  }
  await expect(async () => expect((await (await api.get(`/api/v1/catalog/status?server=${assistant}`)).json()).ready).toBe(true)).toPass({ timeout: 60_000 })
  const search = async (title: string) => {
    const found = await (await api.get('/api/v1/catalog/search', { params: { q: title, kind: 'tracks', server: assistant } })).json()
    return (found.tracks as { id: number; title: string }[]).find((one) => one.title === title)!.id
  }
  const titles = ['Fever Glass', 'Op. 3 (No. 2)', 'Night Drive (Part 2)', 'Small Hours']
  const tracks = []
  for (const title of titles) tracks.push(await search(title))
  const name = `MA Rennen ${browserName}`
  const made = await (await api.post('/api/v1/playlists', { data: { name, server: assistant }, headers: TAB })).json()
  expect((await api.post(`/api/v1/playlists/${made.id}/items`, { data: { tracks }, headers: TAB })).ok()).toBe(true)
  await page.goto(`/playlists/${made.id}`)
  await expect(page.getByTestId('sync-state')).toHaveText('Saved in Music Assistant', { timeout: 20_000 })
  const ma = await request.newContext({ baseURL: process.env.E2E_MA })
  await expect.poll(async () => (await (await ma.get(`/fake/list?name=${encodeURIComponent(name)}`)).json()).tracks, { timeout: 20_000 }).toEqual(titles)

  // Somebody replaces the first track in Music Assistant right when nexmix's taking out of the last one runs there.
  expect((await ma.get(`/fake/race-next?playlist=${encodeURIComponent(name)}&take=Fever%20Glass&add=Untitled`)).ok()).toBe(true)
  const entries = (await (await api.get(`/api/v1/playlists/${made.id}`)).json()).items as { id: number; title: string }[]
  expect((await api.post(`/api/v1/playlists/${made.id}/items/remove`, { data: { entries: [entries[3].id] }, headers: TAB })).ok()).toBe(true)
  await expect.poll(async () => (await (await api.get(`/api/v1/playlists/${made.id}`)).json()).plex.concurrent?.count ?? 0, { timeout: 40_000 }).toBe(1)

  // The playlist says so; "Seen" takes the note away for good.
  await page.reload()
  const note = page.getByTestId('concurrent-note')
  await expect(note).toContainText('Changed in Music Assistant at the same time, please check')
  await expect(note).toContainText('a track was added in Music Assistant')
  await note.getByRole('button', { name: 'Seen' }).click()
  await expect(note).toHaveCount(0)
  expect((await (await api.get(`/api/v1/playlists/${made.id}`)).json()).plex.concurrent ?? null).toBeNull()
  await page.reload()
  await expect(page.getByTestId('track-row').first()).toBeVisible()
  await expect(page.getByTestId('concurrent-note')).toHaveCount(0)
  await ma.dispose()
})

test('a playlist of Plex is copied to Music Assistant', async ({ page, browserName }) => {
  test.setTimeout(150_000)
  const api = page.request
  await letGo(api)
  await plexReady(api)
  const connected = await (await api.post('/api/v1/music-assistant/connect', { data: { url: process.env.E2E_MA, token: await token() }, headers: TAB })).json()
  const assistant = connected.server.id as number
  if (!connected.server.library) {
    const musik = (connected.providers as { handle: string; name: string }[]).find((one) => one.name === 'Musik')!
    expect((await api.put(`/api/v1/music-assistant/${assistant}/providers`, { data: { handles: [musik.handle] }, headers: TAB })).ok()).toBe(true)
  }
  await expect(async () => expect((await (await api.get(`/api/v1/catalog/status?server=${assistant}`)).json()).ready).toBe(true)).toPass({ timeout: 60_000 })
  const search = async (title: string, album: string) => {
    const found = await (await api.get('/api/v1/catalog/search', { params: { q: title, kind: 'tracks', server: 1 } })).json()
    return (found.tracks as { id: number; title: string; album: string }[]).find((one) => one.title === title && one.album === album)!.id
  }
  const tracks = [await search('Northern Lights', 'Paper Lanterns'), await search('Glasshouse', 'Paper Lanterns'), await search('Fever Glass', 'Copper Sky')]
  const name = `MA Kopie ${browserName}`
  const made = await (await api.post('/api/v1/playlists', { data: { name, server: 1 }, headers: TAB })).json()
  expect((await api.post(`/api/v1/playlists/${made.id}/items`, { data: { tracks }, headers: TAB })).ok()).toBe(true)

  await page.goto(`/playlists/${made.id}`)
  await page.getByTestId('copy-open').click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByTestId('mirror-count')).toContainText('3 of 3 found')
  await dialog.getByTestId('mirror-go').click()
  await expect(page.getByTestId('mirror-chip')).toHaveAttribute('aria-label', /: in step$/, { timeout: 30_000 })
  const ma = await request.newContext({ baseURL: process.env.E2E_MA })
  await expect(async () => {
    const list = await (await ma.get(`/fake/list?name=${encodeURIComponent(name)}`)).json()
    expect(list.tracks).toEqual(['Northern Lights', 'Glasshouse', 'Fever Glass'])
  }).toPass({ timeout: 30_000 })
  await ma.dispose()
})
