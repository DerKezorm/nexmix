/**
 * Copying and moving a playlist between servers from end to end, against the stand-in Plex and the stand-in Jellyfin
 * with the same made-up music (but one track Jellyfin lacks): a playlist of Plex is copied to Jellyfin after the
 * preview ("3 of 4 found"), the track Jellyfin lacks matched by hand through a search there; the playlist shows its
 * source and its mirror, the mirror holds all four tracks. Then it moves to Jellyfin: Jellyfin is the source, Plex the
 * mirror, both in step. Runs in Chromium, Firefox and WebKit; each browser connects Jellyfin anew and leaves nothing
 * behind.
 */
import { expect, request, test, type APIRequestContext } from '@playwright/test'

import { TAB } from './global-setup'

async function letGo(api: APIRequestContext): Promise<void> {
  for (const one of (await (await api.get('/api/v1/jellyfin')).json()).servers as { id: number }[]) await api.delete(`/api/v1/jellyfin/${one.id}`, { headers: TAB })
}

async function cleanUp(api: APIRequestContext): Promise<void> {
  for (const one of (await (await api.get('/api/v1/playlists')).json()).playlists as { id: number; name: string }[]) {
    if (one.name.startsWith('Spiegel ')) {
      await api.delete(`/api/v1/playlists/${one.id}`, { headers: TAB })
      await api.delete(`/api/v1/playlists/${one.id}?purge=true`, { headers: TAB })
    }
  }
}

test.afterEach(async ({ baseURL }) => {
  const operator = await request.newContext({ baseURL, storageState: process.env.E2E_STATE, extraHTTPHeaders: TAB })
  // The playlists first: a server still the source of one is not let go of.
  await cleanUp(operator)
  await letGo(operator)
  await operator.dispose()
  const jellyfin = await request.newContext({ baseURL: process.env.E2E_JELLYFIN })
  await jellyfin.post('/fake/untwin')
  await jellyfin.dispose()
  const plex = await request.newContext({ baseURL: process.env.E2E_PLEX })
  await plex.post('/fake/playlists/reset')
  await plex.dispose()
})

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

/** Jellyfin with the twins of Plex's music, connected by Quick Connect, its catalogue there. Returns its id. */
async function jellyfinReady(api: APIRequestContext): Promise<number> {
  const jellyfin = await request.newContext({ baseURL: process.env.E2E_JELLYFIN })
  expect((await jellyfin.post('/fake/twin')).ok()).toBe(true)
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
  await expect(async () => {
    const status = await (await api.get(`/api/v1/catalog/status?server=${id}`)).json()
    expect(status.ready).toBe(true)
    expect(status.counts.tracks).toBeGreaterThan(40)
  }).toPass({ timeout: 60_000 })
  return id
}

async function trackOf(api: APIRequestContext, title: string, album: string): Promise<number> {
  const found = await (await api.get('/api/v1/catalog/search', { params: { q: title, kind: 'tracks', server: 1 } })).json()
  return (found.tracks as { id: number; title: string; album: string }[]).find((one) => one.title === title && one.album === album)!.id
}

test('a playlist is copied from Plex to Jellyfin with its preview and a track matched by hand, then moved there', async ({ page, browserName }) => {
  test.setTimeout(180_000)
  const api = page.request
  await letGo(api)
  await plexReady(api)
  const jelly = await jellyfinReady(api)
  const name = `Spiegel ${browserName}`
  const tracks = [
    await trackOf(api, 'Northern Lights', 'Paper Lanterns'),
    await trackOf(api, 'Glasshouse', 'Paper Lanterns'),
    await trackOf(api, 'Paper Planes', 'Lamps'),
    await trackOf(api, 'Op. 3 (No. 2)', 'Northern Lights'),
  ]
  const made = await (await api.post('/api/v1/playlists', { data: { name, server: 1 }, headers: TAB })).json()
  expect((await api.post(`/api/v1/playlists/${made.id}/items`, { data: { tracks }, headers: TAB })).ok()).toBe(true)

  // Copy to Jellyfin: the preview finds three of four; the fourth is matched by hand.
  await page.goto(`/playlists/${made.id}`)
  await page.getByTestId('copy-open').click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByTestId('mirror-count')).toContainText('3 of 4 found')
  await expect(dialog.getByTestId('mirror-row')).toHaveCount(1)
  await expect(dialog.getByTestId('mirror-row')).toContainText('Op. 3 (No. 2)')
  await expect(dialog.getByTestId('mirror-row-state')).toHaveText('Not found')
  await dialog.getByRole('button', { name: /^Match .*Op\. 3 \(No\. 2\).* otherwise$/ }).click()
  await dialog.getByRole('textbox', { name: /^Look for a track for/ }).fill('Eisblume')
  await dialog.getByRole('button', { name: /^Take .*Eisblume/ }).click()
  await expect(dialog.getByTestId('mirror-count')).toContainText('4 of 4 found')
  await dialog.getByTestId('mirror-go').click()
  await expect(page.getByTestId('toast')).toContainText(/Mirrored on ⁨?Probe⁩?\./)

  // The badges: Plex the source, Jellyfin the mirror, in step; Jellyfin holds all four.
  await expect(page.getByTestId('source-chip')).toHaveText('Source: Studio')
  await expect(page.getByTestId('mirror-chip')).toHaveAttribute('aria-label', 'Mirror on Probe: in step', { timeout: 30_000 })
  const jellyfin = await request.newContext({ baseURL: process.env.E2E_JELLYFIN })
  await expect(async () => {
    const list = await (await jellyfin.get(`/fake/list?name=${encodeURIComponent(name)}`)).json()
    expect(list.tracks).toEqual(['Northern Lights', 'Glasshouse', 'Paper Planes', 'Eisblume'])
  }).toPass({ timeout: 30_000 })

  // Move to Jellyfin: Jellyfin is the source, Plex the mirror.
  await page.getByTestId('move-open').click()
  await expect(dialog.getByTestId('mirror-count')).toContainText('4 of 4 found')
  await dialog.getByTestId('mirror-go').click()
  await expect(page.getByTestId('toast')).toContainText(/Moved to ⁨?Probe⁩?\./)
  await expect(page.getByTestId('source-chip')).toHaveText('Source: Probe')
  await expect(page.getByTestId('mirror-chip')).toContainText('Mirror: Studio')
  const now = await (await api.get(`/api/v1/playlists/${made.id}`)).json()
  expect(now.server).toBe(jelly)
  expect((now.items as { title: string }[]).map((one) => one.title)).toEqual(['Northern Lights', 'Glasshouse', 'Paper Planes', 'Eisblume'])

  await expect(page.getByTestId('mirror-chip')).toHaveAttribute('aria-label', 'Mirror on Studio: in step', { timeout: 30_000 })
  await jellyfin.dispose()
})
