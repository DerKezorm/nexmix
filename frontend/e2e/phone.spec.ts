/**
 * On a phone (390 px wide) no page is wider than the screen: the playlists, every tab of both parts of the settings,
 * about, the sign-in page and the invitation, for the operator and for a member, in German and in English (German
 * words are the longer ones). The log is given a line without a single space, the kind that once stretched the
 * settings to 731 px.
 */
import { expect, request, test, type Browser, type BrowserContext, type Page } from '@playwright/test'

const TAB = { 'X-Nexmix-Client': 'tab-e2e-phone0' }
const PHONE = { width: 390, height: 844 }
/** A fresh name each time the set-up runs (a worker that starts again after a failure runs it once more). */
const MEMBER = { name: `phoebe${Math.floor(Math.random() * 1e6)}`, password: 'e2e phone member password' }
/** One log line with no space in it: an address, a token, a path. */
const LONG = 'x'.repeat(40) + '/' + 'a1b2c3d4'.repeat(40)

/** How much wider than the screen the page is (0 when it fits), and which elements stick out. */
async function overflow(page: Page): Promise<{ extra: number; culprits: string[] }> {
  return page.evaluate(() => {
    const width = window.innerWidth
    const culprits: string[] = []
    for (const element of Array.from(document.querySelectorAll('body *'))) {
      const box = element.getBoundingClientRect()
      if (box.width > 0 && box.right > width + 0.5) {
        const name = element.tagName.toLowerCase() + (element.id ? `#${element.id}` : '') + (typeof element.className === 'string' && element.className ? `.${element.className.split(' ').slice(0, 3).join('.')}` : '')
        culprits.push(`${name} → ${Math.round(box.right)}`)
      }
    }
    return { extra: Math.max(0, document.documentElement.scrollWidth - width), culprits: culprits.slice(0, 8) }
  })
}

async function fits(page: Page, path: string, ready: (page: Page) => Promise<void>): Promise<void> {
  await page.goto(path)
  await ready(page)
  // Fonts and late cards (the log comes after a short wait) are in before the page is measured.
  await page.evaluate(() => document.fonts.ready)
  await page.waitForTimeout(400)
  const found = await overflow(page)
  expect(found, `${path} at ${PHONE.width} px`).toEqual({ extra: 0, culprits: [] })
}

async function phone(browser: Browser, locale: string, storageState?: string): Promise<BrowserContext> {
  const context = await browser.newContext({ viewport: PHONE, locale, storageState: storageState ?? { cookies: [], origins: [] } })
  // The log always holds the long line, whatever the server wrote so far.
  await context.route(
    (url) => url.pathname === '/api/v1/logs',
    (route) =>
      route.request().method() === 'GET'
        ? route.fulfill({ json: [{ time: '2026-11-13 10:00:00', level: 'WARNING', logger: 'nexmix.test', message: LONG }] })
        : route.continue(),
  )
  return context
}

async function heading(page: Page): Promise<void> {
  await expect(page.locator('main h1').first()).toBeVisible()
}

let inviteToken = ''

test.beforeAll(async ({ baseURL }) => {
  const operator = await request.newContext({ baseURL, storageState: process.env.E2E_STATE, extraHTTPHeaders: TAB })
  const made = await operator.post('/api/v1/invites', { data: { days: 1 } })
  expect(made.ok(), await made.text()).toBe(true)
  const token = new URL((await made.json()).link).pathname.split('/').pop()!
  const outside = await request.newContext({ baseURL, extraHTTPHeaders: TAB })
  const joined = await outside.post(`/api/v1/invite/${token}`, { data: { name: MEMBER.name, password: MEMBER.password } })
  expect(joined.ok(), await joined.text()).toBe(true)
  await outside.dispose()
  // A second, open invitation for its page.
  const open = await operator.post('/api/v1/invites', { data: { days: 1 } })
  inviteToken = new URL((await open.json()).link).pathname.split('/').pop()!
  await operator.dispose()
})

// The operator speaks English again for every other test.
test.afterAll(async ({ baseURL }) => {
  const operator = await request.newContext({ baseURL, storageState: process.env.E2E_STATE, extraHTTPHeaders: TAB })
  await operator.put('/api/v1/me/language', { data: { language: 'en' } })
  await operator.dispose()
})

for (const [language, locale] of [
  ['de', 'de-DE'],
  ['en', 'en-US'],
] as const) {
  test(`every page of the operator fits a phone (${language})`, async ({ browser }) => {
    const context = await phone(browser, locale, process.env.E2E_STATE)
    const page = await context.newPage()
    expect((await page.request.put('/api/v1/me/language', { data: { language }, headers: TAB })).ok()).toBe(true)
    await fits(page, '/', heading)
    for (const tab of ['servers', 'signin', 'accounts', 'guests', 'mail', 'push', 'backup', 'languages', 'api'])
      await fits(page, `/settings?tab=${tab}`, async (p) => expect(p.locator(`[data-testid="tabs"] [data-tab="${tab}"]`)).toHaveAttribute('aria-selected', 'true'))
    await fits(page, '/settings?tab=log', async (p) => {
      await expect(p.locator('#log')).toBeVisible()
      await expect(p.getByRole('log')).toContainText(LONG.slice(0, 30))
    })
    for (const tab of ['profile', 'security', 'looks', 'devices']) await fits(page, `/account?tab=${tab}`, heading)
    await fits(page, '/about', async (p) => expect(p.getByTestId('about-facts')).toBeVisible())
    await context.close()
  })

  test(`every page of a member fits a phone (${language})`, async ({ browser }) => {
    const context = await phone(browser, locale)
    const page = await context.newPage()
    const signedIn = await page.request.post('/api/v1/auth/login', { data: MEMBER, headers: TAB })
    expect(signedIn.ok(), await signedIn.text()).toBe(true)
    expect((await page.request.put('/api/v1/me/language', { data: { language }, headers: TAB })).ok()).toBe(true)
    await fits(page, '/', heading)
    // A member has one part of the settings: the address of the other leads there.
    await fits(page, '/settings', async (p) => {
      await expect(p).toHaveURL(/\/account$/)
      await heading(p)
    })
    for (const tab of ['profile', 'security', 'looks', 'devices']) await fits(page, `/account?tab=${tab}`, heading)
    await fits(page, '/about', async (p) => expect(p.getByTestId('about-facts')).toBeVisible())
    await context.close()
  })

  test(`the sign-in page and the invitation fit a phone (${language})`, async ({ browser }) => {
    const context = await phone(browser, locale)
    const page = await context.newPage()
    await fits(page, '/login', async (p) => expect(p.locator('.authcard')).toBeVisible())
    await fits(page, `/invite/${inviteToken}`, async (p) => expect(p.locator('.authcard input').first()).toBeVisible())
    await context.close()
  })
}
