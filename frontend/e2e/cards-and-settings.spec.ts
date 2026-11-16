/**
 * The menu "…" of a playlist's card and the settings in tabs, from end to end against the stand-in Plex and Jellyfin:
 * a playlist of Plex is copied to Jellyfin from the menu of its card, gone through with the keyboard, with the editor's
 * preview; the settings are gone through by their tabs (the tab in the address, an old link led to the right one, the
 * second row of the media servers with a tab per server); and the open menu and the tabs fit a phone. Runs in Chromium,
 * Firefox and WebKit, and leaves nothing behind.
 */
import { expect, request, test, type Page } from '@playwright/test'

import { TAB } from './global-setup'
import { cleanUp, jellyfinReady, letGo, plexReady, trackOf } from './servers'

test.afterEach(async ({ baseURL }) => {
  const operator = await request.newContext({ baseURL, storageState: process.env.E2E_STATE, extraHTTPHeaders: TAB })
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

/** The cell of a playlist's card on the overview, by its name. */
const cell = (page: Page, name: string) => page.locator('.pcell').filter({ has: page.locator('h3', { hasText: name }) })

test('a playlist is copied to Jellyfin from the menu of its card, with the keyboard and the editor\'s preview', async ({ page, browserName }) => {
  test.setTimeout(180_000)
  const api = page.request
  await letGo(api)
  await plexReady(api)
  const jelly = await jellyfinReady(api)
  const name = `Spiegel Karte ${browserName}`
  const tracks = [
    await trackOf(api, 'Northern Lights', 'Paper Lanterns'),
    await trackOf(api, 'Glasshouse', 'Paper Lanterns'),
    await trackOf(api, 'Paper Planes', 'Lamps'),
  ]
  const made = await (await api.post('/api/v1/playlists', { data: { name, server: 1 }, headers: TAB })).json()
  expect((await api.post(`/api/v1/playlists/${made.id}/items`, { data: { tracks }, headers: TAB })).ok()).toBe(true)

  await page.goto('/')
  const more = cell(page, name).getByTestId('card-menu')
  await expect(more).toHaveAttribute('aria-label', `More for ${name}`)
  // With the keyboard: the arrow opens the menu on its first entry, the arrows go down to "Copy to …".
  await more.focus()
  await page.keyboard.press('ArrowDown')
  const menu = page.getByRole('menu')
  await expect(menu).toBeVisible()
  await expect(menu.getByRole('menuitem')).toHaveText(['Open', 'Rename …', 'Share …', 'Invite guests …', 'Copy to …', 'Move to …', 'Hide', 'Delete …'])
  await expect(menu.getByRole('menuitem', { name: 'Open' })).toBeFocused()
  // Escape closes it and gives the focus back.
  await page.keyboard.press('Escape')
  await expect(menu).toHaveCount(0)
  await expect(more).toBeFocused()
  await page.keyboard.press('ArrowDown')
  for (let step = 0; step < 4; step++) await page.keyboard.press('ArrowDown')
  await expect(menu.getByRole('menuitem', { name: 'Copy to …' })).toBeFocused()
  await page.keyboard.press('Enter')

  // The editor's dialog: the target, the preview, the copy kept in step.
  const dialog = page.getByRole('dialog')
  await expect(dialog).toHaveAttribute('aria-label', 'Copy to …')
  await expect(dialog.getByTestId('mirror-targets')).toContainText('Probe (Jellyfin)')
  await expect(dialog.getByTestId('mirror-count')).toContainText('3 of 3 found')
  await dialog.getByTestId('mirror-go').click()
  await expect(page.getByTestId('toast')).toContainText(/Mirrored on ⁨?Probe⁩?\./)
  await expect(dialog).toHaveCount(0)

  // A mirror on Jellyfin, holding the three tracks.
  await expect(async () => {
    const mirrors = (await (await api.get(`/api/v1/playlists/${made.id}/mirrors`)).json()).mirrors as { server: number }[]
    expect(mirrors.map((one) => one.server)).toEqual([jelly])
  }).toPass({ timeout: 15_000 })
  const jellyfin = await request.newContext({ baseURL: process.env.E2E_JELLYFIN })
  await expect.poll(async () => (await (await jellyfin.get(`/fake/list?name=${encodeURIComponent(name)}`)).json()).tracks, { timeout: 30_000 }).toEqual(['Northern Lights', 'Glasshouse', 'Paper Planes'])
  await jellyfin.dispose()
  // Now on both servers, the menu offers no more copy, but still the move.
  await more.click()
  await expect(menu.getByRole('menuitem', { name: 'Copy to …' })).toHaveCount(0)
  await expect(menu.getByRole('menuitem', { name: 'Move to …' })).toBeVisible()
  // A click beside it closes it.
  await page.locator('h1').first().click()
  await expect(menu).toHaveCount(0)
})

test('the settings are gone through by their tabs, a tab per media server below', async ({ page }) => {
  test.setTimeout(150_000)
  const api = page.request
  await letGo(api)
  await plexReady(api)

  // One server: no second row, its card and "Add a server" one below the other.
  await page.goto('/settings')
  const tabs = page.getByTestId('tabs')
  await expect(tabs.getByRole('tab')).toHaveText(['Media servers', 'Sign-in', 'Accounts', 'Guests', 'Mail', 'Web Push', 'Backup', 'Languages', 'Log', 'API'])
  await expect(tabs.getByRole('tab', { name: 'Media servers' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByTestId('sub-tabs')).toHaveCount(0)
  await expect(page.locator('#plex')).toBeVisible()
  await expect(page.getByTestId('servers-add')).toBeVisible()

  // Sign-in: "Ready for the internet?" at the top, nothing of the media servers.
  await tabs.getByRole('tab', { name: 'Sign-in' }).click()
  await expect(page).toHaveURL(/\/settings\?tab=signin$/)
  await expect(page.locator('.settings .card > h2').first()).toHaveText('Ready for the internet?')
  await expect(page.locator('#plex')).toHaveCount(0)
  // The arrows go from tab to tab.
  await tabs.getByRole('tab', { name: 'Sign-in' }).focus()
  await page.keyboard.press('ArrowRight')
  await expect(page).toHaveURL(/\/settings\?tab=accounts$/)
  await expect(page.locator('#accounts')).toBeVisible()

  // A link of before: the Plex card was ?tab=plex.
  await page.goto('/settings?tab=plex')
  await expect(page).toHaveURL(/\/settings\?tab=servers$/)
  await expect(page.locator('#plex')).toBeVisible()
  await page.goto('/settings?tab=nothing-like-it')
  await expect(page).toHaveURL(/\/settings\?tab=servers$/)

  // Two servers: a tab per server with its kind, "Add a server" and "Take all over".
  await jellyfinReady(api)
  await page.goto('/settings?tab=servers')
  const sub = page.getByTestId('sub-tabs')
  await expect(sub.getByRole('tab')).toHaveText([/^Studio/, /^Probe/, 'Add a server', 'Take all over'])
  await expect(sub.getByRole('tab').nth(1)).toHaveAttribute('title', 'Jellyfin')
  await expect(page.locator('#plex')).toBeVisible()
  await expect(page.locator('[id^="jellyfin-"]')).toHaveCount(0)
  await sub.getByRole('tab', { name: /^Probe/ }).click()
  await expect(page).toHaveURL(/\/settings\?tab=servers&sub=\d+$/)
  await expect(page.locator('[id^="jellyfin-"]').getByTestId('jellyfin-connected')).toBeVisible()
  await expect(page.locator('#plex')).toHaveCount(0)
  await sub.getByRole('tab', { name: 'Take all over' }).click()
  await expect(page.locator('[id^="transfer-"]').first()).toBeVisible()
  await sub.getByRole('tab', { name: 'Add a server' }).click()
  await expect(page.getByTestId('servers-add')).toBeVisible()

  // My account the same way.
  await page.goto('/account')
  await expect(tabs.getByRole('tab')).toHaveText(['Profile', 'Security', 'Look', 'Devices and apps'])
  await tabs.getByRole('tab', { name: 'Security' }).click()
  await expect(page).toHaveURL(/\/account\?tab=security$/)
  await expect(page.locator('#security-card')).toBeVisible()
})

test('the open menu of a card and the tabs of the settings fit a phone', async ({ browser, baseURL }) => {
  test.setTimeout(60_000)
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: 'de-DE', storageState: process.env.E2E_STATE })
  const page = await context.newPage()
  const api = page.request
  await plexReady(api)
  const made = await (await api.post('/api/v1/playlists', { data: { name: 'Spiegel Handy', server: 1 }, headers: TAB })).json()
  expect(made.id).toBeGreaterThan(0)
  await page.goto('/')
  const card = cell(page, 'Spiegel Handy')
  await card.getByTestId('card-menu').click()
  const menu = page.getByRole('menu')
  await expect(menu).toBeVisible()
  const box = (await menu.boundingBox())!
  expect(box.x).toBeGreaterThanOrEqual(0)
  expect(box.x + box.width).toBeLessThanOrEqual(390)
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  for (const path of ['/settings', '/settings?tab=signin', '/account?tab=devices']) {
    await page.goto(path)
    await expect(page.getByTestId('tabs')).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth), path).toBeLessThanOrEqual(390)
  }
  await context.close()
  const operator = await request.newContext({ baseURL, storageState: process.env.E2E_STATE, extraHTTPHeaders: TAB })
  await operator.delete(`/api/v1/playlists/${made.id}`)
  await operator.delete(`/api/v1/playlists/${made.id}?purge=true`)
  await operator.dispose()
})
