/**
 * The playlists' sync with Plex from end to end, against the stand-in Plex: a playlist made here shows up in Plex and
 * its head says it is saved there; what Plexamp adds there comes to the open playlist; a rename in Plex comes at once
 * through Plex's stream of events; deleted in Plex, it goes to the trash and says so. Chromium only.
 */
import { expect, request, test, type APIRequestContext } from '@playwright/test'

import { TAB } from './global-setup'

type FakeList = { key: number; title: string; smart: boolean; tracks: number[] }

async function plex(path: string, method: 'GET' | 'POST' = 'POST'): Promise<unknown> {
  const context = await request.newContext({ baseURL: process.env.E2E_PLEX })
  const answer = method === 'GET' ? await context.get(path) : await context.post(path)
  expect(answer.ok(), await answer.text()).toBe(true)
  const text = await answer.text()
  const body = text ? JSON.parse(text) : null
  await context.dispose()
  return body
}

const lists = async () => (await plex('/fake/playlists', 'GET')) as FakeList[]

async function catalogue(api: APIRequestContext): Promise<void> {
  const status = await (await api.get('/api/v1/catalog/status')).json()
  if (!status.ready) {
    if (!status.connected) {
      const pin = await (await api.post('/api/v1/plex/pin', { headers: TAB })).json()
      await plex('/fake/approve')
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
}

// Plex and its playlists go again afterwards: the test of connecting Plex begins with nothing there.
test.afterAll(async ({ baseURL }) => {
  const operator = await request.newContext({ baseURL, storageState: process.env.E2E_STATE, extraHTTPHeaders: TAB })
  for (const one of (await (await operator.get('/api/v1/playlists')).json()).playlists as { id: number; smart: boolean }[])
    if (!one.smart) await operator.delete(`/api/v1/playlists/${one.id}`)
  for (const one of (await (await operator.get('/api/v1/playlists?trash=true')).json()).playlists as { id: number }[]) await operator.delete(`/api/v1/playlists/${one.id}?purge=true`)
  await operator.delete('/api/v1/plex')
  await operator.dispose()
  await plex('/fake/playlists/reset')
})

test('a playlist goes to Plex, comes back changed by Plexamp, follows a rename and a delete there', async ({ page }) => {
  test.setTimeout(150_000)
  const api = page.request
  await catalogue(api)
  const made = await (await api.post('/api/v1/playlists', { data: { name: 'Synced end to end' }, headers: TAB })).json()
  await api.post(`/api/v1/playlists/${made.id}/items`, { data: { tracks: [3001, 3003] }, headers: TAB })

  await page.goto(`/playlists/${made.id}`)
  await expect(page.getByTestId('sync-state')).toHaveText('Saved in Plex', { timeout: 20_000 })
  let found: FakeList | undefined
  await expect(async () => {
    found = (await lists()).find((one) => one.title === 'Synced end to end')
    expect(found?.tracks).toEqual([3001, 3003])
  }).toPass({ timeout: 15_000 })

  // Plexamp adds a track: the open playlist shows it, added by Plex.
  await plex(`/fake/playlists/add?key=${found!.key}&track=3013`)
  const rows = page.getByTestId('track-row')
  await expect(rows).toHaveCount(3, { timeout: 30_000 })
  await expect(rows.nth(2)).toContainText('Plex')

  // Renamed in Plex: the stream of events makes nexmix look at once.
  await plex(`/fake/playlists/rename?key=${found!.key}&title=${encodeURIComponent('Renamed in Plexamp')}`)
  await expect(page.locator('input.name')).toHaveValue('Renamed in Plexamp', { timeout: 15_000 })

  // A change here reaches Plex.
  await page.getByRole('button', { name: /Remove .*/ }).first().click()
  await expect(async () => {
    const now = (await lists()).find((one) => one.key === found!.key)
    expect(now?.tracks).toEqual([3003, 3013])
  }).toPass({ timeout: 20_000 })

  // Deleted in Plex: into the trash, and the open playlist says so.
  await plex(`/fake/playlists/delete?key=${found!.key}`)
  await expect(page).toHaveURL(/\/$/, { timeout: 30_000 })
  await expect(page.getByTestId('trash')).toContainText('Deleted in Plex', { timeout: 15_000 })
})
