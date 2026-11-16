/**
 * The server part of the settings as the design shows it, against a stand-in of the server: the role is called
 * "Betreiber" everywhere (as the sign-in blueprint says it), the accounts card with its sentence, "Person einladen", the
 * providers an account is linked to and its role on the right; the sign-in providers card with "aktiv" in the accent
 * and quiet buttons; the authentik fields one below the other; and the interface card that links to the description
 * only where the server serves it.
 */
import type { Me, OidcProvider } from '../../api/client'
import { changeLanguage } from '../../i18n'
import de from '../../i18n/de.json'
import en from '../../i18n/en.json'
import { Page, Server, plain } from '../../test/harness'
import { AccountsCard, InterfaceCard, useServerSettings } from './ServerCards'
import { SettingsFrame } from './SettingsFrame'
import { SignInPart } from './SignInCards'

const me: Me = {
  id: 1, name: 'jule', display_name: 'Jule', role: 'operator', sign_in: 'password', email: '', language: 'de', email_confirmed: false,
  email_pending: '', oidc_linked: false, two_factor: true, totp: true, passkeys: 0, two_factor_recovery_left: 8, avatar: null, version: '0.1.0',
  whats_new_seen: '0.1.0', session_stage: 'full', profile: { theme: 'system', timezone: 'Europe/Berlin', timezone_source: 'browser', notify_login: true },
}
const auth = { status: 'signedIn', me: me as Me | null, setMe: vi.fn(), refresh: vi.fn(async () => undefined), signOut: vi.fn(async () => undefined) }
vi.mock('../../state/auth', () => ({ useAuth: () => auth }))

const server = new Server()
const page = new Page()
const row = (over: Record<string, unknown>) => ({ ...me, locked: false, blocked: false, has_password: true, created_at: '2026-10-01T10:00:00Z', last_seen_at: null, ...over })

const AUTHENTIK: OidcProvider = {
  id: 3, slug: 'authentik', label: 'authentik', issuer: 'https://auth.example.com/application/o/nexmix/', client_id: 'nexmix',
  has_secret: true, scopes: 'openid profile email', enabled: true, auto_create: false, trusts_second_factor: true, managed: 'authentik',
  position: 0, redirect_uri: 'https://mix.example.com/api/v1/oidc/authentik/callback', links: 1, editable: true,
}
const POCKET: OidcProvider = { ...AUTHENTIK, id: 4, slug: 'pocket-id', label: 'Pocket ID', issuer: 'https://id.example.com', managed: '', enabled: false, position: 1 }
const SETTINGS = {
  public_url: 'https://mix.example.com', password_login: true, two_factor_required: false, backup_schedule: 'off', backup_keep: 7, smtp_host: '',
  smtp_port: 587, smtp_security: 'starttls', smtp_user: '', smtp_password_set: false, smtp_from: '', api_keys_allowed: false, update_check: true,
  api_docs: false,
}

beforeEach(async () => {
  await changeLanguage('de', false)
  auth.me = me
  server.answers = {
    'GET /api/v1/accounts': [row({}), row({ id: 2, name: 'anna', display_name: 'Anna', role: 'member' }), row({ id: 3, name: 'jonas', display_name: '', role: 'member', blocked: true })],
    'GET /api/v1/invites': [],
    'GET /api/v1/oidc/admin/links': [
      { account_id: 1, provider_id: 3, slug: 'authentik', label: 'authentik' },
      { account_id: 2, provider_id: 4, slug: 'pocket-id', label: 'Pocket ID' },
    ],
    'GET /api/v1/settings': SETTINGS,
    'GET /api/v1/oidc/admin/providers': [AUTHENTIK, POCKET],
    'GET /api/v1/oidc/authentik/address': { public_url: 'https://mix.example.com', seen: 'https://mix.example.com', stale: false },
  }
  server.fails = {}
  server.install()
})
afterEach(() => {
  // The first test reads the texts only and shows no page.
  if (page.root) page.close()
  vi.unstubAllGlobals()
})

function flat(tree: Record<string, unknown>): string[] {
  return Object.values(tree).flatMap((value) => (value && typeof value === 'object' ? flat(value as Record<string, unknown>) : [String(value)]))
}

describe('the role', () => {
  it('is "Betreiber" in every German text and "operator" in English, never "Verwalter" or "admin"', () => {
    expect(flat(de).filter((text) => /verwalter/i.test(text))).toEqual([])
    expect(flat(en).filter((text) => /\badmin(istrator)?s?\b|\bmanager\b/i.test(text))).toEqual([])
    expect(de.me.role).toEqual({ operator: 'Betreiber', member: 'Mitglied' })
    expect(en.me.role).toEqual({ operator: 'Operator', member: 'Member' })
  })

  it('names the server part as the operator\'s in the head of the settings', async () => {
    await page.show(<SettingsFrame part="server" tabs={[]} active="servers">{null}</SettingsFrame>, '/settings')
    expect(document.querySelector('.lead p')?.textContent).toBe('Server: nur für Betreiber.')
  })
})

describe('the accounts card', () => {
  it('says what the design says, and invites a person with a quiet button', async () => {
    await page.show(<AccountsCard />, '/settings')
    const card = document.querySelector('#accounts')!
    expect(card.querySelector('h2')!.textContent).toBe('Konten')
    expect(card.querySelector(':scope > p')!.textContent).toBe(
      'Feste Nutzer haben ein eigenes Konto in nexmix, mit Passwort oder über einen Anmeldeanbieter. Ein Plex-Konto brauchen sie nicht. Gäste einer Playlist haben kein Konto.',
    )
    const invite = page.button('Person einladen')!
    expect(invite).toBeTruthy()
    expect(invite.className).not.toContain('primary')
    expect(invite.querySelector('svg')).toBeTruthy()
  })

  it('shows per account the picture, the name with its providers and the role on the right; the rest is in the menu', async () => {
    await page.show(<AccountsCard />, '/settings')
    const rows = [...document.querySelectorAll('[data-testid="accounts"] > li')]
    expect(rows).toHaveLength(3)
    const role = (item: Element) => item.querySelector('[data-testid="account-role"]')!
    expect(rows.map((item) => role(item).textContent)).toEqual(['Betreiber', 'Mitglied', 'Mitglied'])
    // The role is the last thing but the menu, as a chip.
    for (const item of rows) expect(role(item).classList.contains('chip')).toBe(true)
    expect(rows[0].querySelector('[data-testid="account-providers"]')!.textContent).toBe('authentik')
    expect(rows[1].querySelector('[data-testid="account-providers"]')!.textContent).toBe('Pocket ID')
    expect(rows[2].querySelector('[data-testid="account-providers"]')).toBeNull()
    expect(plain(rows[2].textContent)).toContain('gesperrt')
    // No button in the row but the menu (the own row has none); sending a password link is one of its entries.
    expect(rows[0].querySelectorAll('button')).toHaveLength(0)
    expect([...rows[1].querySelectorAll('button')].map((b) => b.getAttribute('aria-label'))).toEqual(['Mehr für anna'])
    await page.click(page.button('Mehr für anna'))
    expect([...rows[1].querySelectorAll('[data-account-actions] span button')].map((b) => b.textContent)).toEqual([
      'Link zum Zurücksetzen schicken', 'Zum Betreiber machen', 'Verknüpfung lösen: Pocket ID', 'Zweiten Faktor entfernen', 'Überall abmelden', 'Sperren', 'Konto löschen',
    ])
  })

  it('speaks English too', async () => {
    await changeLanguage('en', false)
    await page.show(<AccountsCard />, '/settings')
    expect(document.querySelector('#accounts > p')!.textContent).toBe(
      'Regular users have their own account in nexmix, with a password or through a sign-in provider. They need no Plex account. Guests of a playlist have no account.',
    )
    expect(page.button('Invite a person')).toBeTruthy()
    expect([...document.querySelectorAll('[data-testid="account-role"]')].map((item) => item.textContent)).toEqual(['Operator', 'Member', 'Member'])
  })
})

function SignIn() {
  const settings = useServerSettings()
  return settings.settings ? <SignInPart server={settings} /> : null
}

describe('the sign-in provider card', () => {
  it('marks "aktiv" in the accent, "aus" and "über authentik eingerichtet" quietly, with quiet buttons', async () => {
    await page.show(<SignIn />, '/settings')
    const rows = [...document.querySelectorAll('[data-testid="provider-list"] > li')]
    expect(rows).toHaveLength(2)
    const state = (item: Element) => item.querySelector('[data-testid="provider-state"]')!
    expect(state(rows[0]).textContent).toBe('aktiv')
    expect(state(rows[0]).className.split(' ')).toEqual(expect.arrayContaining(['chip', 'on']))
    expect(state(rows[1]).textContent).toBe('aus')
    expect(state(rows[1]).classList.contains('on')).toBe(false)
    const managed = [...rows[0].querySelectorAll('.chip')].find((chip) => chip.textContent === 'über authentik eingerichtet')!
    expect(managed.classList.contains('on')).toBe(false)
    for (const name of ['Bearbeiten', 'Entfernen']) {
      const button = page.button(name, rows[0])!
      expect(button.classList.contains('ghost'), name).toBe(true)
      expect(button.classList.contains('danger') || button.classList.contains('primary'), name).toBe(false)
    }
    const add = page.button('Anbieter hinzufügen')!
    expect(add.classList.contains('primary')).toBe(false)
    expect(add.querySelector('svg')).toBeTruthy()
  })

  it('has the fields of "authentik in einem Schritt" one below the other', async () => {
    await page.show(<SignIn />, '/settings')
    const card = document.querySelector('#sign-in-authentik')!
    const form = card.querySelector('form')!
    expect(form.className).not.toMatch(/grid-cols/)
    const fields = [...form.querySelectorAll(':scope > .field')]
    expect(fields).toHaveLength(2)
    expect(card.querySelector('a.btn.ghost svg')).toBeNull()
  })
})

function Interface() {
  const settings = useServerSettings()
  return settings.settings ? <InterfaceCard server={settings} /> : null
}

describe('the interface card', () => {
  it('links to no description while the server does not serve it, and says how to turn it on', async () => {
    await page.show(<Interface />, '/settings')
    const card = document.querySelector('#interface')!
    const hrefs = [...card.querySelectorAll('a')].map((a) => a.getAttribute('href'))
    expect(hrefs).toEqual([])
    expect(card.textContent).not.toContain('/api/v1/docs')
    expect(card.textContent).not.toContain('openapi.json')
    expect(card.querySelector('[data-testid="docs-off"]')!.textContent).toContain('NEXMIX_API_DOCS=1')
    // How programs sign in is said either way.
    expect(card.textContent).toContain('Authorization: Bearer')
  })

  it('links to the description and its machine-readable form where the server serves them', async () => {
    server.answers['GET /api/v1/settings'] = { ...SETTINGS, api_docs: true }
    await page.show(<Interface />, '/settings')
    const card = document.querySelector('#interface')!
    expect([...card.querySelectorAll('a')].map((a) => a.getAttribute('href'))).toEqual(['/api/v1/docs', '/api/v1/openapi.json'])
    expect(card.querySelector('[data-testid="docs-off"]')).toBeNull()
  })

  it('says it in English too', async () => {
    await changeLanguage('en', false)
    await page.show(<Interface />, '/settings')
    expect(document.querySelector('[data-testid="docs-off"]')!.textContent).toBe(
      'The description is off from the start, so that a server on the internet does not show its routes to anybody. Turn it on with the environment variable NEXMIX_API_DOCS=1 and a restart.',
    )
  })
})
