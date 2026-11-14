/**
 * Plex from end to end, against the stand-in plex.tv and Plex Media Server (`backend/tests/fakeplex`, a made-up
 * library): the operator signs in through Plex's page in a new window, chooses the server, the catalogue is fetched
 * in the background, the playlists page tells, a cover comes through the server (and only for the signed-in), and
 * disconnecting asks first.
 */
import { expect, request, test } from '@playwright/test'

test('the operator connects Plex, the catalogue comes, and disconnecting asks first', async ({ page, baseURL }) => {
  test.setTimeout(90_000)
  await page.goto('/settings')
  const card = page.locator('#plex')
  await expect(card.locator('h2')).toHaveText('Plex')
  const [popup] = await Promise.all([page.waitForEvent('popup'), card.getByRole('button', { name: 'Connect with Plex' }).click()])
  // Plex's page (the stand-in) agrees and closes itself.
  await popup.waitForEvent('close', { timeout: 15_000 }).catch(() => undefined)
  await expect(card.getByText('Choose the server with your music.')).toBeVisible({ timeout: 15_000 })
  await card.getByRole('button', { name: 'Choose: Studio' }).click()
  // The name of the server stands between isolating marks (a name from elsewhere never turns the sentence around).
  await expect(card.getByTestId('plex-steps')).toContainText(/Server “⁨?Studio⁩?”/)
  await expect(card.getByTestId('plex-catalog')).toHaveText('5 artists · 11 albums · 32 tracks', { timeout: 30_000 })
  await expect(card.getByTestId('plex-analysis')).toContainText('Sonic analysis is off')

  await page.goto('/')
  await expect(page.locator('.lead p')).toContainText(/32 tracks from your Plex library “⁨?Music⁩?”/)
  await expect(page.getByTestId('playlists-none')).toContainText('No playlist yet')

  const cover = await page.request.get('/api/v1/catalog/cover/2001?s=80')
  expect(cover.status()).toBe(200)
  expect(cover.headers()['content-type']).toBe('image/jpeg')
  const stranger = await request.newContext({ baseURL, storageState: { cookies: [], origins: [] } })
  expect((await stranger.get('/api/v1/catalog/cover/2001?s=80')).status()).toBe(401)
  await stranger.dispose()

  await page.goto('/settings')
  await card.getByRole('button', { name: 'Disconnect Plex' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('nexmix forgets the token and the catalogue of 32 tracks.')
  await dialog.getByRole('button', { name: 'Cancel' }).click()
  await expect(card.getByTestId('plex-catalog')).toBeVisible()
  await card.getByRole('button', { name: 'Disconnect Plex' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Disconnect Plex' }).click()
  await expect(card.getByRole('button', { name: 'Connect with Plex' })).toBeVisible()
})
