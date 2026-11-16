/**
 * What the end-to-end tests of more than one server share: Plex and Jellyfin connected through their stand-ins (the same
 * made-up music, but one track Jellyfin lacks), letting go of Jellyfin, clearing away the playlists a test made (their
 * names start with "Spiegel "), and a track found by its title and album.
 */
import { expect, request, type APIRequestContext } from '@playwright/test'

import { TAB } from './global-setup'

export async function letGo(api: APIRequestContext): Promise<void> {
  for (const one of (await (await api.get('/api/v1/jellyfin')).json()).servers as { id: number }[]) await api.delete(`/api/v1/jellyfin/${one.id}`, { headers: TAB })
}

export async function cleanUp(api: APIRequestContext): Promise<void> {
  for (const one of (await (await api.get('/api/v1/playlists')).json()).playlists as { id: number; name: string }[]) {
    if (one.name.startsWith('Spiegel ')) {
      await api.delete(`/api/v1/playlists/${one.id}`, { headers: TAB })
      await api.delete(`/api/v1/playlists/${one.id}?purge=true`, { headers: TAB })
    }
  }
}

/** Plex connected through the stand-in and its catalogue there (once per run, whichever test comes first). */
export async function plexReady(api: APIRequestContext): Promise<void> {
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
export async function jellyfinReady(api: APIRequestContext): Promise<number> {
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

export async function trackOf(api: APIRequestContext, title: string, album: string): Promise<number> {
  const found = await (await api.get('/api/v1/catalog/search', { params: { q: title, kind: 'tracks', server: 1 } })).json()
  return (found.tracks as { id: number; title: string; album: string }[]).find((one) => one.title === title && one.album === album)!.id
}
