/**
 * Copying and moving a playlist between servers from end to end, against the stand-in Plex and the stand-in Jellyfin
 * with the same made-up music (but one track Jellyfin lacks): a playlist of Plex is copied to Jellyfin after the
 * preview ("3 of 4 found"), the track Jellyfin lacks matched by hand through a search there; the playlist shows its
 * source and its mirror, the mirror holds all four tracks. Then it moves to Jellyfin: Jellyfin is the source, Plex the
 * mirror, both in step. Runs in Chromium, Firefox and WebKit; each browser connects Jellyfin anew and leaves nothing
 * behind.
 */
import { expect, request, test } from '@playwright/test'

import { TAB } from './global-setup'
import { cleanUp, jellyfinReady, letGo, plexReady, trackOf } from './servers'

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

test('a playlist is copied to Jellyfin only once: a new playlist of its own there, the track Jellyfin lacks left out', async ({ page, browserName }) => {
  test.setTimeout(180_000)
  const api = page.request
  await letGo(api)
  await plexReady(api)
  const jelly = await jellyfinReady(api)
  const name = `Spiegel einmal ${browserName}`
  const tracks = [
    await trackOf(api, 'Northern Lights', 'Paper Lanterns'),
    await trackOf(api, 'Paper Planes', 'Lamps'),
    await trackOf(api, 'Op. 3 (No. 2)', 'Northern Lights'),
  ]
  const made = await (await api.post('/api/v1/playlists', { data: { name, server: 1 }, headers: TAB })).json()
  expect((await api.post(`/api/v1/playlists/${made.id}/items`, { data: { tracks }, headers: TAB })).ok()).toBe(true)

  await page.goto(`/playlists/${made.id}`)
  await page.getByTestId('copy-open').click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByTestId('mirror-count')).toContainText('2 of 3 found')
  await dialog.getByTestId('mirror-once').click()
  await expect(dialog.getByTestId('mirror-go')).toHaveText('Copy once')
  await dialog.getByRole('button', { name: /^Leave out .*Op\. 3 \(No\. 2\)/ }).click()
  await dialog.getByTestId('mirror-go').click()
  await expect(page.getByTestId('toast')).toContainText(/Copied to ⁨?Probe⁩? as a new playlist\./)

  // A playlist of its own with Jellyfin as its source, the two tracks found, nothing tying it to the first one.
  let copy = 0
  await expect(async () => {
    const all = (await (await api.get('/api/v1/playlists')).json()).playlists as { id: number; name: string; server: number }[]
    const found = all.find((one) => one.id !== made.id && one.server === jelly && one.name.startsWith(name))
    expect(found).toBeTruthy()
    copy = found!.id
  }).toPass({ timeout: 15_000 })
  const now = await (await api.get(`/api/v1/playlists/${copy}`)).json()
  expect((now.items as { title: string }[]).map((one) => one.title)).toEqual(['Northern Lights', 'Paper Planes'])
  expect((await (await api.get(`/api/v1/playlists/${made.id}/mirrors`)).json()).mirrors).toEqual([])
  const jellyfin = await request.newContext({ baseURL: process.env.E2E_JELLYFIN })
  await expect.poll(async () => (await (await jellyfin.get(`/fake/list?name=${encodeURIComponent(now.name)}`)).json()).tracks, { timeout: 30_000 }).toEqual(['Northern Lights', 'Paper Planes'])
  await jellyfin.dispose()
  // The first one stays on Plex alone, with all three.
  expect((await (await api.get(`/api/v1/playlists/${made.id}`)).json()).count).toBe(3)
})

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
