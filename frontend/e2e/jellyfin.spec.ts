/**
 * Jellyfin from end to end, against the stand-in Jellyfin and its made-up library: the operator adds the server in the
 * settings by Quick Connect (the code shown, confirmed in the stand-in as one would in Jellyfin's profile), chooses the
 * user, the catalogue comes; a playlist is made with Jellyfin as its source, a track found and added, a preview heard.
 * The playlist is saved in Jellyfin ("Saved in Jellyfin", the stand-in holds it), a track added in the stand-in as from
 * a Jellyfin app shows up in the open playlist, and the filter of moods says Jellyfin has none. Runs in Chromium, Firefox
 * and WebKit; each browser connects anew (the one before lets go of the server at the end).
 */
import { expect, request, test, type APIRequestContext } from '@playwright/test'

import { TAB } from './global-setup'

/** Jellyfin let go of after each test: the next browser connects it again, and the tests of Plex find it gone. */
async function letGo(api: APIRequestContext): Promise<void> {
  for (const one of (await (await api.get('/api/v1/jellyfin')).json()).servers as { id: number }[]) await api.delete(`/api/v1/jellyfin/${one.id}`, { headers: TAB })
}

test.afterEach(async ({ baseURL }) => {
  const operator = await request.newContext({ baseURL, storageState: process.env.E2E_STATE, extraHTTPHeaders: TAB })
  for (const one of (await (await operator.get('/api/v1/playlists')).json()).playlists as { id: number; server: number | null; name: string }[]) {
    if (one.name.startsWith('Jellyfin ')) {
      await operator.delete(`/api/v1/playlists/${one.id}`)
      await operator.delete(`/api/v1/playlists/${one.id}?purge=true`)
    }
  }
  await letGo(operator)
  await operator.dispose()
})

/** Jellyfin connected by Quick Connect through the interface, the user Erika, its catalogue there. Returns its id. */
async function jellyfinReady(api: APIRequestContext): Promise<number> {
  const jellyfin = await request.newContext({ baseURL: process.env.E2E_JELLYFIN })
  const started = await (await api.post('/api/v1/jellyfin/quick', { data: { url: process.env.E2E_JELLYFIN }, headers: TAB })).json()
  expect((await jellyfin.post('/fake/confirm')).ok()).toBe(true)
  await jellyfin.dispose()
  await expect(async () => expect((await (await api.get(`/api/v1/jellyfin/quick/${started.id}`)).json()).done).toBe(true)).toPass({ timeout: 15_000 })
  const connected = await (await api.post('/api/v1/jellyfin/connect', { data: { quick: started.id }, headers: TAB })).json()
  const id = connected.server.id as number
  if (!connected.server.user) {
    const erika = (connected.users as { handle: string; name: string }[]).find((one) => one.name === 'Erika')!
    expect((await api.put(`/api/v1/jellyfin/${id}/user`, { data: { handle: erika.handle }, headers: TAB })).ok()).toBe(true)
  }
  await expect(async () => expect((await (await api.get(`/api/v1/catalog/status?server=${id}`)).json()).ready).toBe(true)).toPass({ timeout: 60_000 })
  return id
}

test('a guest joins a playlist of Jellyfin by its link, searches Jellyfin and adds a track that lands in Jellyfin', async ({ page, browser, browserName }) => {
  test.setTimeout(120_000)
  const api = page.request
  await letGo(api)
  const jelly = await jellyfinReady(api)
  const name = `Jellyfin Gäste ${browserName}`
  const made = await (await api.post('/api/v1/playlists', { data: { name, server: jelly }, headers: TAB })).json()
  const found = await (await api.get('/api/v1/catalog/search', { params: { q: 'teerseile', kind: 'tracks', server: jelly } })).json()
  const first = (found.tracks as { id: number; title: string }[]).find((one) => one.title === 'Teerseile')!.id
  expect((await api.post(`/api/v1/playlists/${made.id}/items`, { data: { tracks: [first] }, headers: TAB })).ok()).toBe(true)
  expect((await api.post(`/api/v1/playlists/${made.id}/invite`, { headers: TAB })).ok()).toBe(true)
  expect((await api.put(`/api/v1/playlists/${made.id}/invite`, { data: { start: true, may_add: true, max: 3, hours: 2 }, headers: TAB })).ok()).toBe(true)
  const link = new URL((await (await api.get(`/api/v1/playlists/${made.id}/invite`)).json()).invite.link).pathname
  await page.goto(`/playlists/${made.id}`)

  // A guest on a phone, no account: the search is Jellyfin's library.
  const phone = await browser.newContext({ storageState: { cookies: [], origins: [] }, locale: 'en-US', viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })
  const guest = await phone.newPage()
  await guest.goto(link)
  await guest.getByPlaceholder("What's your name?").fill('Mila')
  await guest.getByRole('button', { name: 'Join' }).click()
  await expect(guest.getByTestId('guest-title')).toHaveText(name)
  await guest.getByRole('button', { name: 'Search', exact: true }).click()
  await guest.getByRole('searchbox', { name: 'Title or artist' }).fill('möwenruf')
  const hit = guest.getByTestId('guest-hits').locator('.row').filter({ hasText: 'Möwenruf' }).first()
  await expect(hit).toBeVisible()
  await hit.getByRole('button', { name: /^Add/ }).click()
  await expect(guest.getByTestId('toast')).toContainText('is in')

  // The host sees it with the guest's name at once, and it is saved in Jellyfin.
  await expect(page.getByTestId('track-row').filter({ hasText: 'Möwenruf' })).toContainText('Mila')
  const jellyfin = await request.newContext({ baseURL: process.env.E2E_JELLYFIN })
  await expect.poll(async () => (await (await jellyfin.get(`/fake/list?name=${encodeURIComponent(name)}`)).json()).tracks, { timeout: 30_000 }).toEqual(['Teerseile', 'Möwenruf'])
  // Nothing of Jellyfin's ids or address reaches the guest.
  const html = await guest.content()
  expect(html).not.toContain(process.env.E2E_JELLYFIN!)
  await jellyfin.dispose()
  await phone.close()
})

/** A track of its own per browser: a preview heard a moment ago is kept for the account and not asked for again. */
const TRACK: Record<string, string> = { chromium: 'Glasfluss', firefox: 'Funkenregen', webkit: 'Sandkreise' }

test('Jellyfin is connected by Quick Connect, a playlist takes its tracks from it, and a preview plays', async ({ page, browserName }) => {
  test.setTimeout(120_000)
  const title = TRACK[browserName] ?? 'Glasfluss'
  await letGo(page.request)
  const jellyfin = await request.newContext({ baseURL: process.env.E2E_JELLYFIN })

  // The settings: add a server, Jellyfin, the address, Quick Connect.
  await page.goto('/settings')
  const add = page.getByTestId('servers-add')
  await add.getByRole('button', { name: 'Jellyfin', exact: true }).click()
  const box = page.locator('#jellyfin-new')
  await box.getByLabel('Address').fill(process.env.E2E_JELLYFIN!)
  await box.getByRole('button', { name: 'Connect with Quick Connect' }).click()
  const code = page.getByTestId('jellyfin-code-value')
  await expect(code).toHaveText(/^\d{6}$/)
  // Confirmed in Jellyfin, as the operator would in the profile.
  expect((await jellyfin.post('/fake/confirm')).ok()).toBe(true)
  // The first time the user is chosen; connected again, the server keeps the one chosen before.
  const users = page.getByTestId('jellyfin-users')
  const connected = page.getByTestId('jellyfin-connected')
  await expect(users.or(connected)).toBeVisible({ timeout: 15_000 })
  if (await users.isVisible()) await users.getByRole('button', { name: 'Choose: Erika' }).click()
  const card = page.locator('[id^="jellyfin-"]').filter({ has: connected })
  await expect(card.getByTestId('server-catalog')).toContainText('21 tracks', { timeout: 60_000 })
  await expect(card.getByTestId('jellyfin-playlists')).toBeVisible()
  // Nothing of the token, the address or Jellyfin's ids in the page.
  const html = await page.content()
  expect(html).not.toContain('"Token"')
  const servers = (await (await page.request.get('/api/v1/servers')).json()).servers as { id: number; kind: string }[]
  const jelly = servers.find((one) => one.kind === 'jellyfin')!.id

  // A playlist with Jellyfin as its source.
  await page.goto('/')
  await page.getByRole('button', { name: 'New playlist' }).first().click()
  const choice = page.getByTestId('server-choice')
  if (await choice.isVisible().catch(() => false)) {
    await choice.getByLabel(/^Jellyfin/).check()
    await page.getByRole('dialog').getByRole('button', { name: 'Make playlist' }).click()
  }
  await expect(page).toHaveURL(/\/playlists\/\d+$/)
  const id = Number(new URL(page.url()).pathname.split('/').pop())
  await page.getByLabel('Name of the playlist').fill(`Jellyfin ${browserName}`)
  await page.getByLabel('Name of the playlist').press('Enter')
  const made = await (await page.request.get(`/api/v1/playlists/${id}`)).json()
  expect(made.server).toBe(jelly)
  await expect(page.getByTestId('no-moods')).toContainText('No moods')

  // Search and add.
  await page.getByLabel('Search the library').fill(title.toLowerCase())
  const hit = page.getByTestId('search-track').filter({ hasText: title }).first()
  await expect(hit).toBeVisible()
  await hit.getByRole('button', { name: `Add “${title}”` }).click()
  const row = page.getByTestId('track-row').filter({ hasText: title })
  await expect(row).toHaveCount(1)
  await expect.poll(async () => (await (await page.request.get(`/api/v1/playlists/${id}`)).json()).count).toBe(1)

  // Saved in Jellyfin, and there the stand-in holds it.
  await expect(page.getByTestId('sync-state')).toHaveText('Saved in Jellyfin', { timeout: 20_000 })
  const name = `Jellyfin ${browserName}`
  await expect.poll(async () => (await (await jellyfin.get(`/fake/list?name=${encodeURIComponent(name)}`)).json()).tracks, { timeout: 20_000 }).toEqual([title])
  // Added in Jellyfin, as from a Jellyfin app: it shows up in the open playlist.
  const other = title === 'Möwenruf' ? 'Teerseile' : 'Möwenruf'
  expect((await jellyfin.post('/fake/list-add', { data: { playlist: name, track: other } })).ok()).toBe(true)
  await expect(page.getByTestId('track-row').filter({ hasText: other })).toHaveCount(1, { timeout: 40_000 })

  // Listen in: the server asks Jellyfin's stream.mp3, the browser plays it.
  const begun = ((await (await jellyfin.get('/fake/sound')).json()).starts as unknown[]).length
  if (browserName === 'webkit' && process.platform === 'win32') {
    // The WebKit build of the test runner asks for no media at all on Windows: the server's answer is checked instead.
    const track = (await (await page.request.get(`/api/v1/playlists/${id}`)).json()).items[0].track
    expect((await page.request.get(`/api/v1/preview/${track}`)).status()).toBe(200)
  } else {
    await row.getByTestId('play').first().click()
    const stop = row.getByRole('button', { name: /^Stop listening/ })
    await expect(stop).toHaveClass(/\bon\b/)
    await expect.poll(async () => Number(await stop.evaluate((button) => (button as HTMLElement).style.getPropertyValue('--p') || '0')), { timeout: 15_000 }).toBeGreaterThan(0)
    await stop.click()
  }
  const starts = ((await (await jellyfin.get('/fake/sound')).json()).starts as { startTimeTicks: string; audioBitRate: string }[]).slice(begun)
  expect(starts.length).toBeGreaterThanOrEqual(1)
  expect(starts[0].audioBitRate).toBe('192000')
  await jellyfin.dispose()
})
