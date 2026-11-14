/**
 * Playlists from end to end, against the stand-in Plex and its made-up library: make one, search, add a track and a
 * whole album, take one out and undo it, sort by dragging, share it with a member whose browser sees the next change at
 * once, delete it into the trash and bring it back; on a phone the page stays inside the screen. Runs in Chromium,
 * Firefox and WebKit; dragging with a finger in Chromium (the one that lets a test touch).
 */
import { expect, request, test, type APIRequestContext, type Browser, type Page } from '@playwright/test'

import { TAB } from './global-setup'

const PASSWORD = 'a made up e2e member password'

// Plex and the playlists go again after these tests: the test of connecting Plex begins with nothing there.
test.afterAll(async ({ baseURL }) => {
  const operator = await request.newContext({ baseURL, storageState: process.env.E2E_STATE, extraHTTPHeaders: TAB })
  for (const one of (await (await operator.get('/api/v1/playlists')).json()).playlists as { id: number }[]) await operator.delete(`/api/v1/playlists/${one.id}`)
  for (const one of (await (await operator.get('/api/v1/playlists?trash=true')).json()).playlists as { id: number }[]) await operator.delete(`/api/v1/playlists/${one.id}?purge=true`)
  await operator.delete('/api/v1/plex')
  await operator.dispose()
  // The copies in Plex go too, whether the sync came to it or not.
  const plex = await request.newContext({ baseURL: process.env.E2E_PLEX })
  await plex.post('/fake/playlists/reset')
  await plex.dispose()
})

/** Plex connected through the stand-in and the catalogue there: once per run, whichever test comes first. */
async function catalogue(api: APIRequestContext): Promise<void> {
  const status = await (await api.get('/api/v1/catalog/status')).json()
  if (!status.ready) {
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
}

/** A member with a password, and a browser of their own signed in. */
async function member(browser: Browser, baseURL: string, operator: APIRequestContext, name: string): Promise<{ page: Page; id: number; close: () => Promise<void> }> {
  const invite = await operator.post('/api/v1/invites', { data: { days: 1 }, headers: TAB })
  expect(invite.ok(), await invite.text()).toBe(true)
  const token = new URL((await invite.json()).link).pathname.split('/').pop()!
  const outside = await request.newContext({ baseURL })
  const joined = await outside.post(`/api/v1/invite/${token}`, { data: { name, password: PASSWORD }, headers: TAB })
  expect(joined.ok(), await joined.text()).toBe(true)
  await outside.dispose()
  const context = await browser.newContext({ storageState: { cookies: [], origins: [] }, locale: 'en-US' })
  const page = await context.newPage()
  const login = await page.request.post('/api/v1/auth/login', { data: { name, password: PASSWORD }, headers: TAB })
  expect(login.ok(), await login.text()).toBe(true)
  const me = await (await page.request.get('/api/v1/auth/me')).json()
  return { page, id: me.id, close: () => context.close() }
}

async function order(api: APIRequestContext, id: number): Promise<number[]> {
  return ((await (await api.get(`/api/v1/playlists/${id}`)).json()).items as { track: number }[]).map((entry) => entry.track)
}

test('a playlist is made, filled, sorted, shared live and brought back from the trash', async ({ page, browser, baseURL, browserName }) => {
  test.setTimeout(120_000)
  const operator = page.request
  await catalogue(operator)

  await page.goto('/')
  await page.getByRole('button', { name: 'New playlist' }).first().click()
  await expect(page).toHaveURL(/\/playlists\/\d+$/)
  const id = Number(page.url().split('/').pop())
  const name = page.getByLabel('Name of the playlist')
  await expect(name).toBeFocused()
  await name.fill(`Garden party ${browserName}`)
  await name.press('Enter')
  await expect.poll(async () => (await (await operator.get(`/api/v1/playlists/${id}`)).json()).name).toBe(`Garden party ${browserName}`)

  // Search and add; the song is "in" afterwards, in every version of the search.
  const search = page.getByRole('searchbox', { name: 'Search the library' })
  await page.keyboard.press('/')
  await expect(search).toBeFocused()
  await search.fill('northern lights')
  await expect(page.getByTestId('timing')).toContainText(/songs in 32 tracks/)
  const first = page.getByTestId('search-track').first()
  await expect(first).toContainText('Northern Lights')
  await first.getByRole('button', { name: /^Add/ }).click()
  await expect(page.getByTestId('toast')).toContainText('added')
  await expect(page.getByTestId('track-row')).toHaveCount(1)
  await expect(page.getByTestId('search-track').first()).toHaveClass(/\bin\b/)

  // A whole album: the songs already in stay out.
  await page.getByRole('button', { name: 'Albums', exact: true }).click()
  await search.fill('paper lanterns')
  await page.getByTestId('search-album').first().getByRole('button').click()
  await expect(page.getByTestId('toast')).toContainText('tracks added')
  const count = await page.getByTestId('track-row').count()
  expect(count).toBeGreaterThan(2)

  // Taken out, and back with the undo.
  const second = page.getByTestId('track-row').nth(1)
  const title = await second.locator('.t').innerText()
  await second.hover()
  await second.getByRole('button', { name: `Remove “${title}”` }).click()
  await expect(page.getByTestId('track-row')).toHaveCount(count - 1)
  await page.getByTestId('toast').getByRole('button', { name: 'Undo' }).click()
  await expect(page.getByTestId('track-row')).toHaveCount(count)
  await expect(page.getByTestId('track-row').nth(1).locator('.t')).toHaveText(title)

  // Dragging the last row to the top with the mouse.
  const before = await order(operator, id)
  const rows = page.getByTestId('track-row')
  const last = rows.nth(count - 1)
  const top = await rows.nth(0).boundingBox()
  const grip = await last.locator('.grip').boundingBox()
  await page.mouse.move(grip!.x + grip!.width / 2, grip!.y + grip!.height / 2)
  await page.mouse.down()
  await page.mouse.move(grip!.x + 5, top!.y + 10, { steps: 8 })
  await page.mouse.move(grip!.x + 5, top!.y + 4, { steps: 2 })
  await page.mouse.up()
  await expect.poll(() => order(operator, id)).toEqual([before[before.length - 1], ...before.slice(0, -1)])

  // Shared with a member to change: their browser sees the operator's next change at once.
  const mia = await member(browser, baseURL!, operator, `mia${browserName}`)
  await page.getByRole('button', { name: /^Share/ }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('group', { name: `Sharing with mia${browserName}` }).getByRole('button', { name: 'Edit' }).click()
  await expect(dialog.getByRole('group', { name: `Sharing with mia${browserName}` }).getByRole('button', { name: 'Edit' })).toHaveAttribute('aria-pressed', 'true')
  await dialog.getByRole('button', { name: 'Done' }).click()
  await mia.page.goto(`/playlists/${id}`)
  await expect(mia.page.getByTestId('track-row')).toHaveCount(count)
  // The stream is open once the page has said hello to it; a change now arrives as an event.
  await mia.page.waitForTimeout(500)
  await page.getByRole('button', { name: 'Tracks', exact: true }).click()
  await search.fill('copper sky')
  await page.getByTestId('search-track').first().getByRole('button', { name: /^Add/ }).click()
  await expect(mia.page.getByTestId('track-row')).toHaveCount(count + 1)
  await expect(mia.page.getByTestId('track-row').last()).toContainText('Copper Sky')

  // Into the trash after asking; the member's page leaves; back from the trash it is whole.
  await page.getByRole('button', { name: 'Delete' }).click()
  await expect(page.getByRole('dialog')).toContainText('lies in the trash for 30 days')
  await page.getByRole('dialog').getByRole('button', { name: 'Move to the trash' }).click()
  await expect(page).toHaveURL(/\/$/)
  await expect(mia.page).toHaveURL(/\/$/, { timeout: 10_000 })
  const trash = page.getByTestId('trash')
  await expect(trash).toContainText(`Garden party ${browserName}`)
  await trash.getByTestId('trash-row').filter({ hasText: `Garden party ${browserName}` }).getByRole('button', { name: 'Restore' }).click()
  await expect(page.getByTestId('playlist-card').filter({ hasText: `Garden party ${browserName}` })).toBeVisible()
  expect((await order(operator, id)).length).toBe(count + 1)
  await mia.close()
})

test('on a phone the list comes first and nothing runs over the edge', async ({ browser, baseURL, browserName }) => {
  test.setTimeout(60_000)
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, storageState: process.env.E2E_STATE, locale: 'en-US' })
  const page = await context.newPage()
  await catalogue(page.request)
  const made = await (await page.request.post('/api/v1/playlists', { data: { name: `Phone ${browserName}` }, headers: TAB })).json()
  await page.request.post(`/api/v1/playlists/${made.id}/items`, { data: { tracks: [3001, 3013, 3026] }, headers: TAB })
  await page.goto(`${baseURL}/playlists/${made.id}`)
  await expect(page.getByTestId('track-row')).toHaveCount(3)
  const list = await page.locator('.col.list').boundingBox()
  const add = await page.locator('.col.add').boundingBox()
  expect(list!.y).toBeLessThan(add!.y)
  const wide = await page.evaluate(() => document.documentElement.scrollWidth)
  expect(wide).toBeLessThanOrEqual(390)
  await page.goto(`${baseURL}/`)
  await expect(page.getByTestId('playlist-card').first()).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await context.close()
})

test('a finger sorts by holding a row and dragging it', async ({ browser, browserName }) => {
  test.skip(browserName !== 'chromium', 'touch is driven through the Chromium protocol')
  test.setTimeout(60_000)
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, storageState: process.env.E2E_STATE, locale: 'en-US' })
  const page = await context.newPage()
  await catalogue(page.request)
  const made = await (await page.request.post('/api/v1/playlists', { data: { name: 'Touch' }, headers: TAB })).json()
  await page.request.post(`/api/v1/playlists/${made.id}/items`, { data: { tracks: [3001, 3013, 3026] }, headers: TAB })
  await page.goto(`/playlists/${made.id}`)
  const rows = page.getByTestId('track-row')
  await expect(rows).toHaveCount(3)
  const from = (await rows.nth(2).boundingBox())!
  const to = (await rows.nth(0).boundingBox())!
  const cdp = await context.newCDPSession(page)
  const x = from.x + from.width / 2
  const point = (y: number) => [{ x, y }]
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: point(from.y + from.height / 2) })
  await page.waitForTimeout(600)
  for (let step = 1; step <= 8; step++) {
    const y = from.y + from.height / 2 - ((from.y + from.height / 2 - (to.y + 4)) * step) / 8
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: point(y) })
  }
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
  await expect.poll(() => order(page.request, made.id)).toEqual([3026, 3001, 3013])
  await context.close()
})

test('smart playlists of Plex stay out until asked for, and a hidden playlist comes back from the list below', async ({ page, browserName }) => {
  test.setTimeout(90_000)
  const operator = page.request
  await catalogue(operator)
  const smartName = `Smart ${browserName}`
  const plex = await request.newContext({ baseURL: process.env.E2E_PLEX })
  const made = await plex.post(`/fake/playlists/smart?title=${encodeURIComponent(smartName)}&tracks=3001,3003`)
  expect(made.ok(), await made.text()).toBe(true)
  const smartKey = ((await made.json()) as { key: number }).key
  await expect(async () => {
    await operator.post('/api/v1/plex/playlists/sync', { headers: TAB })
    const listed = (await (await operator.get('/api/v1/playlists')).json()).playlists as { name: string; smart: boolean }[]
    expect(listed.some((one) => one.name === smartName && one.smart)).toBe(true)
  }).toPass({ timeout: 30_000 })
  const own = await (await operator.post('/api/v1/playlists', { data: { name: `Hide me ${browserName}` }, headers: TAB })).json()

  await page.goto('/')
  const card = (name: string) => page.getByTestId('playlist-card').filter({ hasText: name })
  await expect(card(`Hide me ${browserName}`)).toBeVisible()
  await expect(card(smartName)).toHaveCount(0)
  const chip = page.getByTestId('smart-toggle')
  await expect(chip).toContainText('Show smart playlists from Plex')
  await expect(chip).toHaveAttribute('aria-pressed', 'false')
  await chip.click()
  await expect(card(smartName)).toBeVisible()
  // The choice is the account's, kept on the server: still there after a reload.
  await expect.poll(async () => (await (await operator.get('/api/v1/playlists')).json()).show_smart_playlists).toBe(true)
  await page.reload()
  await expect(card(smartName)).toBeVisible()
  await page.getByTestId('smart-toggle').click()
  await expect(card(smartName)).toHaveCount(0)

  // Hidden from its menu; back from the list below. Plex and the playlist itself see nothing of it.
  const cell = page.locator('.pcell').filter({ hasText: `Hide me ${browserName}` })
  await cell.getByRole('button', { name: `More for Hide me ${browserName}` }).click()
  await cell.getByRole('button', { name: 'Hide', exact: true }).click()
  await expect(card(`Hide me ${browserName}`)).toHaveCount(0)
  const hidden = page.getByTestId('hidden')
  await expect(hidden).toContainText('Hidden from your overview only.')
  await expect.poll(async () => (await (await operator.get(`/api/v1/playlists/${own.id}`)).json()).hidden).toBe(true)
  await page.reload()
  await expect(page.getByTestId('hidden-row').filter({ hasText: `Hide me ${browserName}` })).toBeVisible()
  await page.getByTestId('hidden-row').filter({ hasText: `Hide me ${browserName}` }).getByRole('button', { name: 'Show again' }).click()
  await expect(card(`Hide me ${browserName}`)).toBeVisible()
  await expect(page.getByTestId('hidden')).toHaveCount(0)

  // The smart playlist goes in Plex, so it goes to the trash here (a smart one is never deleted from nexmix), and
  // from there for good: the next tests begin without it.
  await plex.post(`/fake/playlists/delete?key=${smartKey}`)
  await plex.dispose()
  await expect(async () => {
    await operator.post('/api/v1/plex/playlists/sync', { headers: TAB })
    const listed = (await (await operator.get('/api/v1/playlists')).json()).playlists as { name: string }[]
    expect(listed.some((one) => one.name === smartName)).toBe(false)
  }).toPass({ timeout: 30_000 })
  for (const one of (await (await operator.get('/api/v1/playlists?trash=true')).json()).playlists as { id: number; name: string }[])
    if (one.name === smartName) await operator.delete(`/api/v1/playlists/${one.id}?purge=true`, { headers: TAB })
})
