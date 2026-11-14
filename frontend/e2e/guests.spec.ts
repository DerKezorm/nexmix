/**
 * Guests from end to end, with two browsers: the host invites from the playlist, a guest opens the link on a phone
 * (390 px), joins with a name, searches and adds a track that the host sees at once with the guest's name; the host
 * takes the guest out (the phone says so at once), a second guest joins, and ending the invitation shows the phone the
 * end. The guest gets suggestions from the list and listens in. Against the stand-in Plex and its made-up library.
 */
import { expect, request, test, type APIRequestContext, type Page } from '@playwright/test'

import { TAB } from './global-setup'

const PHONE = { width: 390, height: 844 }

async function catalogue(api: APIRequestContext): Promise<void> {
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

async function tracks(api: APIRequestContext, query: string): Promise<number[]> {
  const found = await (await api.get('/api/v1/catalog/search', { params: { q: query } })).json()
  return (found.tracks as { id: number }[]).map((track) => track.id)
}

/** How much wider than the screen the page is. */
async function wider(page: Page): Promise<number> {
  return page.evaluate(() => Math.max(0, document.documentElement.scrollWidth - window.innerWidth))
}

test('a guest joins by the link, adds a track the host sees live, is taken out, and the invitation ends', async ({ page, browser, browserName }) => {
  test.setTimeout(120_000)
  const operator = page.request
  await catalogue(operator)
  const made = await (await operator.post('/api/v1/playlists', { data: { name: 'Garden party guests' }, headers: TAB })).json()
  const first = (await tracks(operator, 'northern lights'))[0]
  await operator.post(`/api/v1/playlists/${made.id}/items`, { data: { tracks: [first] }, headers: TAB })

  // The host invites: the dialog shows a link and a QR code at once; the invitation runs once started.
  await page.goto(`/playlists/${made.id}`)
  await page.getByTestId('invite-open').click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByTestId('invite-link')).toHaveValue(/\/j\/[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$/)
  await expect(dialog.locator('.qr img')).toBeVisible()
  await expect(dialog.getByTestId('invite-reach')).toContainText('Only this computer')
  await expect(dialog).toContainText('Not on yet.')
  await dialog.getByRole('switch', { name: 'May sort and remove tracks of others' }).click()
  await dialog.getByLabel('Tracks per guest at most').fill('3')
  await dialog.getByRole('button', { name: 'Start invitation' }).click()
  await expect(page.getByTestId('toast')).toContainText('The invitation is on.')
  await expect(page.getByTestId('guest-count')).toHaveText('0')
  const link = await (async () => {
    const view = await (await operator.get(`/api/v1/playlists/${made.id}/invite`)).json()
    return new URL(view.invite.link).pathname
  })()

  // A guest on a phone: no account, a fresh browser.
  const phone = await browser.newContext({ storageState: { cookies: [], origins: [] }, locale: 'en-US', viewport: PHONE, isMobile: true, hasTouch: true })
  const guest = await phone.newPage()
  await guest.goto(link)
  const join = guest.getByTestId('guest-join')
  await expect(join).toContainText('Garden party guests')
  // The host has no display name: the guest never sees the name to sign in with.
  await expect(join).toContainText('Someone from the household invites you to add music.')
  await expect(join).not.toContainText('tester')
  await guest.getByPlaceholder("What's your name?").fill('Lena')
  await guest.getByRole('button', { name: 'Join' }).click()
  await expect(guest.getByTestId('guest-title')).toHaveText('Garden party guests')
  await expect(guest.getByTestId('guest-quota')).toContainText('You added 0 of 3 tracks')
  await expect(page.getByTestId('guest-count')).toHaveText('1')
  expect(await wider(guest)).toBe(0)

  // Search and add; the host's page shows it with the guest's name without a reload.
  await guest.getByRole('button', { name: 'Search', exact: true }).click()
  await guest.getByRole('searchbox', { name: 'Title or artist' }).fill('copper sky')
  const hit = guest.getByTestId('guest-hits').locator('.row').first()
  await expect(hit).toContainText('Copper Sky')
  await hit.getByRole('button', { name: /^Add/ }).click()
  await expect(guest.getByTestId('toast')).toContainText('is in')
  await expect(page.getByTestId('track-row')).toHaveCount(2)
  await expect(page.getByTestId('track-row').last()).toContainText('Copper Sky')
  await expect(page.getByTestId('track-row').last()).toContainText('Lena')
  await guest.getByRole('button', { name: 'List', exact: true }).click()
  await expect(guest.getByTestId('guest-quota')).toContainText('You added 1 of 3 tracks')
  await expect(guest.getByTestId('guest-list').locator('.row')).toHaveCount(2)
  // With the right to sort, the guest moves the host's track down.
  await guest.getByRole('button', { name: /^Move “Northern Lights” down/ }).click()
  await expect(page.getByTestId('track-row').first()).toContainText('Copper Sky')
  // The suggestions come from the browser.
  await guest.getByRole('button', { name: 'Suggestions', exact: true }).click()
  await expect(guest.locator('.group-title')).toHaveText('Fits the list')
  const idea = guest.getByTestId('guest-ideas').locator('.row').first()
  await expect(idea).toBeVisible()
  expect(await wider(guest)).toBe(0)
  // Listening in, with the invitation's yes: the guest's own preview through the server, and ■ stops it.
  if (!(browserName === 'webkit' && process.platform === 'win32')) {
    const listened = guest.waitForResponse((response) => /\/api\/v1\/preview\/\d+$/.test(new URL(response.url()).pathname))
    await idea.getByTestId('play').click()
    expect((await listened).status()).toBe(200)
    await expect(idea.getByTestId('play')).toHaveClass(/\bon\b/)
    await idea.getByTestId('play').click()
    await expect(idea.getByTestId('play')).not.toHaveClass(/\bon\b/)
  }

  // The host takes Lena out after asking: her phone says so at once, her track stays.
  await page.getByTestId('invite-open').click()
  await expect(dialog.getByTestId('invite-guests')).toContainText('Lena')
  await expect(dialog.getByTestId('invite-guests')).toContainText('added 1 track')
  await dialog.getByRole('button', { name: 'Remove Lena' }).click()
  const confirm = page.getByRole('dialog').filter({ hasText: 'cannot be undone' })
  await expect(confirm).toContainText(/The tracks \W*Lena\W* added stay in the playlist\./)
  await confirm.getByRole('button', { name: 'Remove' }).click()
  await expect(guest.getByTestId('guest-over')).toHaveAttribute('data-over', 'removed', { timeout: 10_000 })
  await expect(guest.getByTestId('guest-over')).toContainText('You are no longer in')
  await expect(page.getByTestId('track-row')).toHaveCount(2)
  await guest.goto(link)
  await expect(guest.getByTestId('guest-over')).toHaveAttribute('data-over', 'removed')

  // A second guest, then the end of the invitation: the phone shows the end at once.
  const second = await browser.newContext({ storageState: { cookies: [], origins: [] }, locale: 'en-US', viewport: PHONE, isMobile: true, hasTouch: true })
  const tim = await second.newPage()
  await tim.goto(link)
  await tim.getByPlaceholder("What's your name?").fill('Tim')
  await tim.getByPlaceholder("What's your name?").press('Enter')
  await expect(tim.getByTestId('guest-title')).toHaveText('Garden party guests')
  await expect(dialog.getByTestId('invite-guests')).toContainText('Tim')
  await dialog.getByRole('button', { name: 'End invitation' }).click()
  const ending = page.getByRole('dialog').filter({ hasText: 'cannot be undone' })
  await ending.getByRole('button', { name: 'End invitation' }).click()
  await expect(page.getByTestId('toast')).toContainText('Invitation ended.')
  await expect(tim.getByTestId('guest-over')).toHaveAttribute('data-over', 'ended', { timeout: 10_000 })
  await expect(tim.getByTestId('guest-over')).toContainText('The invitation is over')
  await expect(page.getByTestId('guest-count')).toHaveCount(0)
  expect(await wider(tim)).toBe(0)

  // Nothing else is open to a guest's cookie.
  expect((await tim.request.get('/api/v1/playlists')).status()).toBe(401)
  expect((await tim.request.get('/api/v1/guest')).status()).toBe(403)

  await phone.close()
  await second.close()
  await operator.delete(`/api/v1/playlists/${made.id}`, { headers: TAB })
})
