/**
 * Suggestions, filling by mood and listening in from end to end, against the stand-in Plex and its made-up library: the
 * right column (add, "do not suggest again" with its undo, "similar to"), the dialog up to "take" and its undo, ▶ that
 * starts and stops a preview (silence from the stand-in's converter, played by the browser, and Plex told to stop), and
 * the column below the search on a phone. The previews play in Chromium and Firefox (WebKit on Windows asks for no media
 * at all); WebKit takes part in the rest.
 */
import { expect, request, test, type APIRequestContext } from '@playwright/test'

import { TAB } from './global-setup'

test.afterAll(async ({ baseURL }) => {
  const operator = await request.newContext({ baseURL, storageState: process.env.E2E_STATE, extraHTTPHeaders: TAB })
  for (const one of (await (await operator.get('/api/v1/playlists')).json()).playlists as { id: number }[]) await operator.delete(`/api/v1/playlists/${one.id}`)
  for (const one of (await (await operator.get('/api/v1/playlists?trash=true')).json()).playlists as { id: number }[]) await operator.delete(`/api/v1/playlists/${one.id}?purge=true`)
  await operator.delete('/api/v1/plex')
  await operator.dispose()
})

/** Plex connected through the stand-in and the catalogue there. */
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

async function playlist(api: APIRequestContext, name: string, tracks: number[]): Promise<number> {
  const made = await (await api.post('/api/v1/playlists', { data: { name }, headers: TAB })).json()
  await api.post(`/api/v1/playlists/${made.id}/items`, { data: { tracks }, headers: TAB })
  return made.id as number
}

test('suggestions are added, hidden and brought back, and a mood fills the list in one step', async ({ page, browserName }) => {
  test.setTimeout(120_000)
  await catalogue(page.request)
  const id = await playlist(page.request, `Suggestions ${browserName}`, [3001, 3013, 3026])
  await page.goto(`/playlists/${id}`)
  const column = page.getByTestId('suggestions')
  await expect(column.getByTestId('seed-label')).toHaveText('Fitting the last 3 tracks of the list')
  const rows = page.getByTestId('track-row')
  await expect(rows).toHaveCount(3)
  const suggestions = column.getByTestId('suggestion')
  await expect(suggestions.first()).toBeVisible()
  await expect(column.locator('.why').first()).not.toBeEmpty()
  // The sonic analysis is off at the stand-in: the column says what it would bring, the operator gets the way there.
  await expect(column.getByTestId('sonic-note')).toContainText('It is switched off in your library.')
  await expect(column.getByTestId('sonic-note').getByRole('link', { name: 'More about it' })).toBeVisible()

  // Added: the song leaves the suggestions and joins the list.
  const first = await suggestions.first().locator('.t').innerText()
  await suggestions.first().getByRole('button', { name: `Add “${first}”` }).click()
  await expect(page.getByTestId('toast')).toContainText('added')
  await expect(rows).toHaveCount(4)
  await expect(rows.last()).toContainText(first)
  await expect(column.getByTestId('suggestion').filter({ hasText: first })).toHaveCount(0)

  // Not again, and back with the undo.
  const next = await suggestions.first().locator('.t').innerText()
  await suggestions.first().getByRole('button', { name: `Do not suggest “${next}” again` }).click()
  await expect(page.getByTestId('toast')).toContainText('Will not be suggested again')
  await expect(column.getByTestId('suggestion').filter({ hasText: next })).toHaveCount(0)
  expect(((await (await page.request.get('/api/v1/suggestions/hidden')).json()).songs as number[]).length).toBe(1)
  await page.getByTestId('toast').getByRole('button', { name: 'Undo' }).click()
  await expect(column.getByTestId('suggestion').filter({ hasText: next })).toHaveCount(1)
  await expect.poll(async () => ((await (await page.request.get('/api/v1/suggestions/hidden')).json()).songs as number[]).length).toBe(0)

  // Similar to one track of the list, and back to the last five.
  await rows.first().hover()
  await rows.first().getByRole('button', { name: 'Suggestions for “Northern Lights”' }).click()
  await expect(column.getByTestId('seed-label')).toContainText('Similar to Northern Lights')
  await column.getByRole('button', { name: /back to the last 5/ }).click()
  await expect(column.getByTestId('seed-label')).toHaveText('Fitting the last 4 tracks of the list')

  // Fill by mood: a proposal, one tick let go, the rest in one step, and one undo takes all of it out.
  await page.locator('.plhead').getByRole('button', { name: 'Fill by mood' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('Choose at least one mood')
  await expect(dialog.getByRole('button', { name: 'Make a proposal' })).toBeDisabled()
  for (const mood of await dialog.locator('.moodgroup .chip').all()) await mood.click()
  await dialog.getByRole('button', { name: 'Make a proposal' }).click()
  const picks = dialog.getByTestId('mood-pick')
  await expect(picks.first()).toBeVisible()
  const proposed = await picks.count()
  const before = await rows.count()
  if (proposed > 1) await picks.first().getByRole('checkbox').uncheck()
  const taken = proposed > 1 ? proposed - 1 : proposed
  await expect(dialog.getByTestId('mood-summary')).toContainText(`Proposal: ${taken} track`)
  await dialog.getByRole('button', { name: 'Add them' }).click()
  await expect(dialog).toHaveCount(0)
  await expect(rows).toHaveCount(before + taken)
  await page.getByTestId('toast').getByRole('button', { name: 'Undo' }).click()
  await expect(rows).toHaveCount(before)
})

test('▶ plays a preview from the server and ■ stops it, and Plex hears of the end', async ({ page, browserName }) => {
  test.skip(browserName === 'webkit' && process.platform === 'win32', 'the WebKit build of the test runner asks for no media at all on Windows')
  test.setTimeout(60_000)
  await catalogue(page.request)
  const id = await playlist(page.request, `Preview ${browserName}`, [3001, 3013])
  await page.goto(`/playlists/${id}`)
  const row = page.getByTestId('track-row').first()
  const play = row.getByRole('button', { name: 'Listen in to “Northern Lights”' })
  const plex = await request.newContext({ baseURL: process.env.E2E_PLEX })
  const sound = async () => (await (await plex.get('/fake/sound')).json()) as { starts: { session: string; offset: string }[]; stops: string[] }
  const begun = (await sound()).starts.length
  const answered = page.waitForResponse((response) => response.url().includes('/api/v1/preview/3001'))
  await play.click()
  // WebKit on Linux plays the sound but names every answer to its media 0 (measured with Playwright 1.63); the ring
  // and the start at the stand-in Plex below show that the server answered.
  const status = (await answered).status()
  if (browserName !== 'webkit') expect(status).toBe(200)
  const stop = row.getByRole('button', { name: 'Stop listening to “Northern Lights”' })
  await expect(stop).toHaveClass(/\bon\b/)
  // The browser plays it: the ring fills with the seconds.
  await expect.poll(async () => Number(await stop.evaluate((button) => (button as HTMLElement).style.getPropertyValue('--p') || '0')), { timeout: 15_000 }).toBeGreaterThan(0)
  const started = (await sound()).starts.slice(begun)
  expect(started.length).toBeGreaterThanOrEqual(1)
  expect(started[0].offset).toBe('70')
  await stop.click()
  await expect(row.getByRole('button', { name: 'Listen in to “Northern Lights”' })).not.toHaveClass(/\bon\b/)
  // The browser let go of the sound: the server ended the conversion at Plex.
  await expect.poll(async () => (await sound()).stops.includes(started[started.length - 1].session), { timeout: 15_000 }).toBe(true)
  // Only one at a time: another ▶ ends the first.
  await play.click()
  await row.page().getByTestId('track-row').nth(1).getByTestId('play').click()
  await expect(page.locator('[data-testid="play"].on[data-track="3001"]')).toHaveCount(0)
  await expect(page.locator('[data-testid="play"].on[data-track="3013"]').first()).toBeVisible()
  await plex.dispose()
})

test('a preview asked for in parts, as Safari does, has its length and plays from the one kept on the server', async ({ page, browserName }) => {
  // Safari asks in parts; the WebKit builds of the test runner do not: on Windows they ask for no media at all, on
  // Linux for the whole sound without a Range (measured with Playwright 1.63), which is the stream of the test above.
  // Chromium and Firefox ask with a Range and walk the path Safari takes.
  test.skip(browserName === 'webkit', 'the WebKit builds of the test runner ask for media without a Range, unlike Safari')
  test.setTimeout(60_000)
  await catalogue(page.request)
  await page.goto('/')
  const plex = await request.newContext({ baseURL: process.env.E2E_PLEX })
  const starts = async () => ((await (await plex.get('/fake/sound')).json()) as { starts: unknown[] }).starts.length
  const begun = await starts()
  // A track of its own per browser: the preview kept for the account is the account's, whichever browser asks.
  const track = { chromium: 3024, firefox: 3027, webkit: 3029 }[browserName] ?? 3026
  const asked: string[] = []
  page.on('request', (one) => {
    if (one.url().includes('/api/v1/preview/')) asked.push(`${new URL(one.url()).pathname} ${one.headers()['range'] ?? '-'}`)
  })
  // Safari's first question: two bytes, and the whole length in the answer.
  const probe = await page.evaluate(async (track) => {
    const answer = await fetch(`/api/v1/preview/${track}`, { headers: { Range: 'bytes=0-1' } })
    return { status: answer.status, range: answer.headers.get('content-range'), length: answer.headers.get('content-length'), size: (await answer.arrayBuffer()).byteLength }
  }, track)
  expect(probe.status).toBe(206)
  expect(probe.range).toMatch(/^bytes 0-1\/\d+$/)
  expect([probe.length, probe.size]).toEqual(['2', 2])
  // The browser plays the same preview with its length known, from the server's memory: Plex converted it once.
  const played = await page.evaluate(
    (track) =>
      new Promise<{ duration?: number; time?: number; error?: string }>((resolve) => {
        const audio = new Audio(`/api/v1/preview/${track}`)
        audio.muted = true
        audio.addEventListener('timeupdate', () => {
          if (audio.currentTime > 0.3) {
            resolve({ duration: audio.duration, time: audio.currentTime })
            audio.pause()
          }
        })
        audio.addEventListener('error', () => resolve({ error: `media ${audio.error?.code}` }))
        audio.play().catch((error) => resolve({ error: String(error) }))
        setTimeout(() => resolve({ error: 'no sound within 20 s' }), 20_000)
      }),
    track,
  )
  expect(played.error).toBeUndefined()
  expect(Number.isFinite(played.duration)).toBe(true)
  expect(played.duration!).toBeGreaterThan(20)
  expect((await starts()) - begun, asked.join(' | ')).toBe(1)
  await plex.dispose()
})

test('on a phone the suggestions come below the search, and nothing runs over the edge', async ({ browser, baseURL, browserName }) => {
  test.setTimeout(60_000)
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, storageState: process.env.E2E_STATE, locale: 'en-US' })
  const page = await context.newPage()
  await catalogue(page.request)
  const id = await playlist(page.request, `Phone suggestions ${browserName}`, [3001, 3013, 3026])
  await page.goto(`${baseURL}/playlists/${id}`)
  await expect(page.getByTestId('suggestion').first()).toBeVisible()
  const list = (await page.locator('.col.list').boundingBox())!
  const search = (await page.locator('.col.add').boundingBox())!
  const suggestions = (await page.locator('.col.sugg').boundingBox())!
  expect(list.y).toBeLessThan(search.y)
  expect(search.y).toBeLessThan(suggestions.y)
  await expect(page.getByTestId('track-row').first().getByRole('button', { name: /^Suggestions for/ })).toBeHidden()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await page.locator('.plhead').getByRole('button', { name: 'Fill by mood' }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await context.close()
})
