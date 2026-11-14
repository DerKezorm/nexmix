/**
 * The sign-in page and the invitation page are one card in the middle: the mark and the name, the title, the form, the
 * line "or", a button per provider, the operator's way in when the password is off, and the note for the guests of a
 * playlist at the end. The invitation names who invited and asks for the password twice.
 */
import { Route, Routes } from 'react-router-dom'

import { changeLanguage } from '../i18n'
import { Page, Server } from '../test/harness'
import { InvitePage, LoginPage } from './AuthPages'

const auth = { status: 'signedOut' as string, me: null, setMe: vi.fn(), refresh: vi.fn(async () => undefined), signOut: vi.fn(async () => undefined) }
vi.mock('../state/auth', () => ({ useAuth: () => auth, safeNext: (next: string | null) => next ?? '/' }))

const server = new Server()
const page = new Page()

beforeEach(async () => {
  await changeLanguage('de', false)
  server.answers = {
    'GET /api/v1/auth/methods': { password: true, providers: [{ slug: 'authentik', label: 'authentik' }], passkeys: false },
    'GET /api/v1/invite/abcdefghijklmnopqrstu': { min_password: 12, signed_in_as: null, invited_by: 'Jule' },
  }
  server.fails = {}
  server.install()
})
afterEach(() => {
  page.close()
  vi.unstubAllGlobals()
})

describe('the sign-in page', () => {
  it('is the card with the form, "or", the provider buttons and the note for guests, in this order', async () => {
    await page.show(<LoginPage />, '/login')
    const card = document.querySelector('.authpage .authcard')!
    expect(card.querySelector('.wordmark')!.textContent).toBe('NEXMIX')
    expect(card.querySelector('h1')!.textContent).toBe('Anmelden')
    const order = ['form.authform', '.or', '.providers', '.guestnote'].map((selector) => card.querySelector(selector))
    expect(order.every(Boolean)).toBe(true)
    const position = (element: Element | null) => [...card.querySelectorAll('*')].indexOf(element!)
    expect(order.map(position)).toEqual([...order.map(position)].sort((a, b) => a - b))
    expect(card.querySelector('.or')!.textContent).toBe('oder')
    expect(card.querySelector('.providers button')!.textContent).toBe('Mit authentik anmelden')
    expect(card.querySelector('.guestnote')!.textContent).toBe('Gast auf einer Party? Dafür brauchst du kein Konto: Scann den Code oder öffne den Link, den du bekommen hast.')
  })

  it('shows only the buttons while the password is off, and the operator\'s way in below them', async () => {
    server.answers['GET /api/v1/auth/methods'] = { password: false, providers: [{ slug: 'authentik', label: 'authentik' }], passkeys: false }
    await page.show(<LoginPage />, '/login')
    expect(document.querySelector('form.authform')).toBeNull()
    expect(document.querySelector('.operator')!.textContent).toBe('Als Betreiber mit Passwort anmelden')
  })

  it('says the same in English', async () => {
    await changeLanguage('en', false)
    await page.show(<LoginPage />, '/login')
    expect(document.querySelector('.providers button')!.textContent).toBe('Sign in with authentik')
    expect(document.querySelector('.guestnote')!.textContent).toContain('Guest at a party?')
  })
})

describe('the invitation page', () => {
  function app() {
    return (
      <Routes>
        <Route path="/invite/:token" element={<InvitePage />} />
      </Routes>
    )
  }

  it('names who invited, asks for the password twice and sends nothing while they differ', async () => {
    await page.show(app(), '/invite/abcdefghijklmnopqrstu')
    expect(document.querySelector('h1')!.textContent).toBe('Willkommen bei nexmix')
    expect(document.querySelector('.authcard > p')!.textContent).toBe('Jule hat dich als Mitglied eingeladen. Wähl einen Namen und ein Passwort, oder melde dich über einen Anbieter an.')
    const inputs = [...document.querySelectorAll<HTMLInputElement>('form.authform input')]
    expect(inputs.length).toBe(3)
    expect(document.querySelector('form.authform')!.textContent).toContain('Passwort wiederholen')
    expect(document.querySelector('.providers button')!.textContent).toBe('Weiter mit authentik')
    await page.type(inputs[0], 'anna')
    await page.type(inputs[1], 'first long password')
    await page.type(inputs[2], 'another long password')
    await page.click(page.button('Konto anlegen'))
    expect(server.changes()).toEqual([])
    expect(document.querySelector('[role="alert"]')).toBeTruthy()
  })

  it('still has a text when the one who invited is gone, and carries the note for guests', async () => {
    server.answers['GET /api/v1/invite/abcdefghijklmnopqrstu'] = { min_password: 12, signed_in_as: null, invited_by: null }
    await page.show(app(), '/invite/abcdefghijklmnopqrstu')
    expect(document.querySelector('.authcard > p')!.textContent).toMatch(/^Du bist als Mitglied eingeladen/)
    expect(document.querySelector('.guestnote')).toBeTruthy()
  })
})
