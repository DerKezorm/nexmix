/**
 * The server's API, `/api/v1`. Every call names this browser tab in `X-Nexmix-Client` (changes without it are refused, the
 * wall against requests from other sites). Errors come back as `ApiError` with the server's code; the page builds
 * its sentence from `errors.<code>` in the language files.
 */

import i18n from 'i18next'

import type { AuthentikAddress } from '../vendor/nexoidc/oidc'

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    public readonly values: Record<string, unknown> = {},
  ) {
    super(code)
  }
}

const CLIENT_KEY = 'nexmix.client'
/** Sent on `window` when a request finds that the session is gone: the page goes back to the sign-in. */
export const SIGNED_OUT_EVENT = 'nexmix:signed-out'

function randomId(): string {
  const bytes = new Uint8Array(12)
  crypto.getRandomValues(bytes)
  return 'tab-' + Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

let clientId: string | null = null

/** One id per tab: sessionStorage survives a reload of the tab but is not shared with other tabs. */
export function tabId(): string {
  if (clientId) return clientId
  try {
    clientId = sessionStorage.getItem(CLIENT_KEY)
    if (!clientId) {
      clientId = randomId()
      sessionStorage.setItem(CLIENT_KEY, clientId)
    }
  } catch {
    clientId = randomId()
  }
  return clientId
}

type Options = {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  query?: Record<string, string | number | boolean | string[] | undefined>
  body?: unknown
  /** A file as the body (an upload), sent as it is. */
  raw?: Blob
  /** Files in a form (the browser's upload); the browser makes the boundary. */
  form?: FormData
  /** Stops the request (a cancelled upload). */
  signal?: AbortSignal
  /** The answer is a file, not JSON. */
  blob?: boolean
  /** Headers of its own, next to the ones every request carries (the password of an upload). */
  headers?: Record<string, string>
  /** Sent even while the page closes (the last draft). */
  keepalive?: boolean
}

const BUSY_TRIES = 3
const BUSY_WAIT_MS = 1500

export async function api<T>(path: string, options: Options = {}): Promise<T> {
  const repeatable = (options.method ?? 'GET') === 'GET'
  for (let attempt = 1; ; attempt++) {
    try {
      return await once<T>(path, options)
    } catch (error) {
      if (!(error instanceof ApiError && error.code === 'busy' && repeatable && attempt < BUSY_TRIES)) throw error
      await new Promise((resolve) => setTimeout(resolve, BUSY_WAIT_MS))
    }
  }
}

async function once<T>(path: string, options: Options): Promise<T> {
  const url = new URL(path, window.location.origin)
  for (const [key, value] of Object.entries(options.query ?? {})) {
    if (Array.isArray(value)) for (const item of value) url.searchParams.append(key, item)
    else if (value !== undefined) url.searchParams.set(key, String(value))
  }
  // The language the page is shown in: what an account without a language of its own starts with (its values).
  const headers: Record<string, string> = { ...options.headers, Accept: 'application/json', 'X-Nexmix-Client': tabId(), ...(i18n.language ? { 'X-Nexmix-Language': i18n.language } : {}) }
  let body: BodyInit | undefined
  if (options.raw) body = options.raw
  else if (options.form) body = options.form
  else if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.body)
  }
  let response: Response
  try {
    response = await fetch(url.pathname + url.search, { method: options.method ?? 'GET', headers, body, ...(options.keepalive ? { keepalive: true } : {}), ...(options.signal ? { signal: options.signal } : {}) })
  } catch (error) {
    // A request the page stopped on purpose is no network trouble.
    if (options.signal?.aborted || (error instanceof DOMException && error.name === 'AbortError')) throw new ApiError(0, 'cancelled')
    throw new ApiError(0, 'network')
  }
  if (response.status === 204) return undefined as T
  if (options.blob && response.ok) return (await response.blob()) as T
  const data = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = data?.detail
    if (detail && typeof detail === 'object' && typeof detail.code === 'string') {
      const { code, message: _message, ...values } = detail
      if (code === 'sign_in_required') window.dispatchEvent(new Event(SIGNED_OUT_EVENT))
      // How long the server asks to wait (a brake, a busy moment), in seconds, for the page to try again then.
      const wait = Number(response.headers?.get?.('Retry-After') ?? NaN)
      if (Number.isFinite(wait) && wait >= 0 && values.retry_after === undefined) values.retry_after = wait
      throw new ApiError(response.status, code, values)
    }
    throw new ApiError(response.status, response.status === 401 ? 'sign_in_required' : 'internal_error')
  }
  return data as T
}

// ---- Types ----------------------------------------------------------------------------------------------------------

export type ThemeChoice = 'system' | 'light' | 'dark'

export type Profile = {
  theme: ThemeChoice
  /** The time zone; what a notice of a new sign-in names its time in. */
  timezone: string
  /** Reported by a browser, or chosen by the person (then no browser changes it). */
  timezone_source: 'browser' | 'manual'
  /** A push and a mail when the account signs in from a new device. */
  notify_login: boolean
  /** The overview shows the smart playlists Plex makes by itself; off from the start. */
  show_smart_playlists?: boolean
}

export type Me = {
  id: number
  name: string
  display_name: string
  role: 'operator' | 'member'
  sign_in: 'password' | 'oidc'
  email: string
  /** Confirmed by a link sent to it; an address the operator typed or a sign-in provider sent is not. */
  email_confirmed: boolean
  /** A new address waiting for its link; `email` stays until it is opened. */
  email_pending: string
  language: string
  /** Linked to at least one sign-in provider (which ones: `oidcApi.mine`). */
  oidc_linked: boolean
  /** A second factor of any kind: a code from an app or a passkey. */
  two_factor: boolean
  /** A code from an app. */
  totp: boolean
  /** How many passkeys. */
  passkeys: number
  two_factor_recovery_left: number
  avatar: string | null
  version: string
  whats_new_seen: string
  profile: Profile
  mail?: boolean
  /** Whether this session may only set up the second factor, or (until set up) reaches only its own account page. */
  second_factor_setup_required?: boolean
  /** `setup`: right after the password, only the setup; `codes`: the recovery codes to confirm; `full`. */
  session_stage?: SessionStage
  /** The operator asks this account for a second factor: the last one cannot be turned off. */
  second_factor_required?: boolean
  /**
   * This session came in through a provider that checks the second factor, and the account (with a password) has none
   * of its own: its device tokens and API keys wait until it sets one up. Making one stays possible.
   */
  keys_wait_for_second_factor?: boolean
}

export type SessionStage = 'full' | 'setup' | 'codes'
/** The first step went through and the account has a second factor: which kinds it may give. */
export type SecondFactorWaiting = { second_factor: true; totp: boolean; passkey: boolean }

export type SetupState = { needs_setup: boolean; code_required: boolean; signed_in: boolean; version: string; min_password: number }
/** A button on the sign-in and invitation page: one per active provider, in the order of the operator's list. */
export type ProviderButton = { slug: string; label: string }
export type Methods = { password: boolean; providers: ProviderButton[]; passkeys?: boolean; forgot?: boolean; session_days?: number }
export type SignedSession = { id: string; device: string; phone: boolean; network: string; created_at: string; last_seen_at: string; remember: boolean; here: boolean }
export type Passkey = { id: string; name: string; created_at: string; last_used_at: string | null; credential: string }
export type Readiness = { points: { key: string; state: 'ok' | 'warn' | 'bad'; values: Record<string, unknown> }[]; open: number }

// ---- Calls ----------------------------------------------------------------------------------------------------------

export const authApi = {
  setupState: () => api<SetupState>('/api/v1/setup'),
  setup: (name: string, password: string, code: string, language: string) =>
    api<Me>('/api/v1/setup', { method: 'POST', body: { name, password, code, language } }),
  methods: () => api<Methods>('/api/v1/auth/methods'),
  login: (name: string, password: string, remember = true) => api<Me | SecondFactorWaiting>('/api/v1/auth/login', { method: 'POST', body: { name, password, remember } }),
  code: (code: string, remember?: boolean) => api<Me>('/api/v1/auth/login/totp', { method: 'POST', body: { code, remember } }),
  /** The recovery codes are kept: the session that set up the second factor becomes a full one. */
  setupDone: () => api<Me>('/api/v1/auth/setup/done', { method: 'POST' }),
  /** New recovery codes while they are being confirmed (a reload lost the ones shown). */
  setupCodes: () => api<{ recovery_codes: string[] }>('/api/v1/auth/setup/codes', { method: 'POST' }),
  sessions: () => api<SignedSession[]>('/api/v1/auth/sessions'),
  endSession: (id: string) => api<void>(`/api/v1/auth/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  logoutOthers: () => api<void>('/api/v1/auth/logout-all', { method: 'POST' }),
  cancelCode: () => api<void>('/api/v1/auth/login/totp/cancel', { method: 'POST' }),
  logout: () => api<void>('/api/v1/auth/logout', { method: 'POST' }),
  me: () => api<Me>('/api/v1/auth/me'),
  language: (language: string) => api<Me>('/api/v1/me/language', { method: 'PUT', body: { language } }),
  preferences: (change: Partial<Profile>) => api<Profile>('/api/v1/me/preferences', { method: 'PUT', body: change }),
  profile: (display_name: string) => api<Me>('/api/v1/me/profile', { method: 'PUT', body: { display_name } }),
  password: (current: string, next: string) => api<void>('/api/v1/auth/password', { method: 'PUT', body: { current, new: next } }),
  /** "Forgot your password?": a link to the address on record, if there is one; the answer is the same either way. */
  forgot: (name: string) => api<{ ok: true }>('/api/v1/auth/forgot', { method: 'POST', body: { name } }),
  whatsNewSeen: () => api<Me>('/api/v1/me/whats-new/seen', { method: 'POST' }),
}

// ---- Sign-in providers (the shared module `vendor/nexoidc`) -----------------------------------------------

/** An entry of the provider list as the operator sees it (never the secret). `managed`: "" by hand, "authentik" by the
 * authentik button. */
export type OidcProvider = {
  id: number
  slug: string
  label: string
  issuer: string
  client_id: string
  has_secret: boolean
  scopes: string
  enabled: boolean
  auto_create: boolean
  trusts_second_factor: boolean
  managed: '' | 'authentik' | 'nexsuite'
  position: number
  redirect_uri: string
  links: number
  editable: boolean
}
export type OidcProviderForm = {
  label: string
  slug?: string
  issuer: string
  client_id: string
  client_secret: string
  scopes: string
  enabled: boolean
  auto_create: boolean
  trusts_second_factor: boolean
}
export type ProviderImpact = { issuer_change: number; count: number; only: number; only_names: string[] }
export type AuthentikStep = { key: string; ok: boolean; detail: string; reason?: string; status?: number }
/** `count`: with the reason `issuer_change`, how many accounts would have to link again (nothing was changed). */
export type AuthentikResult = {
  ok: boolean
  steps: AuthentikStep[]
  links_dropped: number
  count?: number
  /** The public address the run stored (the card's suggestion, confirmed); it stays even when a later step failed. */
  public_url?: string
  /** With the reason `public_url_needed`: what the card shows about the address (the same as `oidcApi.address`). */
  address?: AuthentikAddress
}
/** What the server asks before a new public address leaves redirect addresses behind (409 `public_url_change`). */
export type AddressQuestion = { count: number; address: string; hand: number }
/** An active provider on the account page: linked or not, and the address it knows when it is not the account's. */
export type MyProvider = { slug: string; label: string; linked: boolean; address: string | null }
/** Which account is linked to which provider, for the marks in the operator's account list. */
export type AccountLink = { account_id: number; provider_id: number; slug: string; label: string }

export const oidcApi = {
  providers: () => api<OidcProvider[]>('/api/v1/oidc/admin/providers'),
  addProvider: (form: OidcProviderForm) => api<OidcProvider>('/api/v1/oidc/admin/providers', { method: 'POST', body: form }),
  saveProvider: (id: number, form: OidcProviderForm) => api<OidcProvider & { dropped: number }>(`/api/v1/oidc/admin/providers/${id}`, { method: 'PUT', body: form }),
  removeProvider: (id: number) => api<ProviderImpact>(`/api/v1/oidc/admin/providers/${id}`, { method: 'DELETE' }),
  providerImpact: (id: number, issuer = '') => api<ProviderImpact>(`/api/v1/oidc/admin/providers/${id}/impact`, { query: issuer ? { issuer } : {} }),
  orderProviders: (ids: number[]) => api<ProviderButton[]>('/api/v1/oidc/admin/providers/order', { method: 'PUT', body: { ids } }),
  links: () => api<AccountLink[]>('/api/v1/oidc/admin/links'),
  /** The operator takes an account's link away, with the own password. */
  unlinkAccount: (account: number, provider: number, current_password: string) =>
    api<void>(`/api/v1/oidc/admin/accounts/${account}/links/${provider}`, { method: 'DELETE', body: { current_password } }),
  /**
   * `confirm_issuer_change`: the answer to the question the button stops at when another issuer takes the links.
   * `public_url`: the suggested address ("Use it and set up"), only sent while nexmix has none.
   */
  authentik: (url: string, token: string, confirm_issuer_change = false, public_url = '') =>
    api<AuthentikResult>('/api/v1/oidc/authentik/setup', { method: 'POST', body: { url, token, confirm_issuer_change, ...(public_url ? { public_url } : {}) } }),
  /** What the authentik card shows about the public address. */
  address: () => api<AuthentikAddress>('/api/v1/oidc/authentik/address'),
  /** "Use it only": the address becomes the public one; 409 `public_url_change` asks first, `confirm` answers it. */
  saveAddress: (public_url: string, confirm = false) => api<AuthentikAddress>('/api/v1/oidc/authentik/address', { method: 'POST', body: { public_url, confirm } }),
  mine: () => api<MyProvider[]>('/api/v1/oidc/me'),
  link: (slug: string, password: string) => api<{ url: string }>(`/api/v1/oidc/${encodeURIComponent(slug)}/link`, { method: 'POST', body: { password } }),
  unlink: (slug: string) => api<void>(`/api/v1/oidc/${encodeURIComponent(slug)}/link`, { method: 'DELETE' }),
  /** The address the provider knows becomes the own one (not confirmed: the link that confirms it is a step of its own). */
  takeAddress: (slug: string) => api<{ email: string; email_confirmed: boolean }>('/api/v1/oidc/me/address', { method: 'POST', body: { slug } }),
}

/** The address a provider's button leads to (a full page load: the browser leaves for the provider). */
export function providerStart(slug: string, extra: { invite?: string; next?: string } = {}): string {
  const query = new URLSearchParams()
  if (extra.invite) query.set('invite', extra.invite)
  if (extra.next && extra.next !== '/') query.set('next', extra.next)
  const text = query.toString()
  return `/api/v1/oidc/${encodeURIComponent(slug)}/start${text ? `?${text}` : ''}`
}

export const passkeyApi = {
  list: () => api<Passkey[]>('/api/v1/auth/passkeys'),
  begin: () => api<{ options: string }>('/api/v1/auth/passkeys/begin', { method: 'POST' }),
  add: (credential: unknown, name: string, password: string) =>
    api<{ passkey: Passkey; recovery_codes: string[] | null; account: Me }>('/api/v1/auth/passkeys', { method: 'POST', body: { credential, name, password } }),
  remove: (id: string, password: string) => api<Me>(`/api/v1/auth/passkeys/${encodeURIComponent(id)}/remove`, { method: 'POST', body: { password } }),
  signInBegin: () => api<{ options: string }>('/api/v1/auth/passkey/begin', { method: 'POST' }),
  signIn: (credential: unknown, remember: boolean) => api<Me>('/api/v1/auth/passkey', { method: 'POST', body: { credential, remember } }),
}

export type ApiKey = {
  id: number
  name: string
  /** `read`: programs that read. */
  scope: 'read'
  prefix: string
  created_at: string
  last_used_at: string | null
  expires_at: string | null
  blocked: boolean
}
/** What the operator sees of a key: whose it is, what it is called and its first characters, never the key. */
export type AnyApiKey = ApiKey & { account: string }

/** API keys for programs that read. */
export const apiKeysApi = {
  list: () => api<{ allowed: boolean; keys: ApiKey[] }>('/api/v1/api-keys'),
  make: (name: string, days: number | null, scope: ApiKey['scope'] = 'read') =>
    api<{ key: ApiKey; secret: string }>('/api/v1/api-keys', { method: 'POST', body: { name, days, scope } }),
  remove: (id: number) => api<void>(`/api/v1/api-keys/${id}`, { method: 'DELETE' }),
  /** The operator allows API keys for everybody (the switch under Settings, API). */
  allow: () => api<unknown>('/api/v1/settings', { method: 'PUT', body: { api_keys_allowed: true } }),
  every: () => api<AnyApiKey[]>('/api/v1/admin/api-keys'),
  block: (id: number) => api<AnyApiKey>(`/api/v1/admin/api-keys/${id}/block`, { method: 'POST' }),
}

/** A device or app of the account: it speaks with a token of its own (`Authorization: Bearer`). */
export type PairedDevice = {
  id: string
  name: string
  prefix: string
  created_at: string
  last_used_at: string | null
}

export const devicesApi = {
  list: () => api<PairedDevice[]>('/api/v1/devices'),
  /** The token comes back once. */
  make: (name: string) => api<{ device: PairedDevice; token: string }>('/api/v1/devices', { method: 'POST', body: { name } }),
  revoke: (id: string) => api<void>(`/api/v1/devices/${encodeURIComponent(id)}`, { method: 'DELETE' }),
}

/** The password for a backup rides in a header, as base64 of its UTF-8: a header carries no umlauts. */
export function passwordHeader(password: string): string {
  let bytes = ''
  for (const byte of new TextEncoder().encode(password)) bytes += String.fromCharCode(byte)
  return btoa(bytes)
}

export function avatarUrl(person: { id: number; avatar: string | null }): string | null {
  return person.avatar ? `/api/v1/avatars/${person.id}?v=${encodeURIComponent(person.avatar)}` : null
}

// ---- Web Push -------------------------------------------------------------------------------------------------------

export type PushDevice = { id: string; name: string; phone: boolean; since: string; last: string | null }
export type PushState = { key: string; devices: PushDevice[] }
export type PushResult = { sent: number; gone: number; failed: number }
export type PushSettings = { devices: number; key: string; contact: string; contact_used: string; known: string[]; hosts: string[] }

/** Web Push: the own devices and a probe; the operator's card. */
export const pushApi = {
  state: () => api<PushState>('/api/v1/push'),
  add: (subscription: { endpoint: string; p256dh: string; auth: string; installed: boolean }) =>
    api<PushDevice>('/api/v1/push/devices', { method: 'POST', body: subscription }),
  lookup: (endpoint: string) => api<{ id: string | null }>('/api/v1/push/devices/lookup', { method: 'POST', body: { endpoint } }),
  rename: (id: string, name: string) => api<PushDevice>(`/api/v1/push/devices/${encodeURIComponent(id)}`, { method: 'PUT', body: { name } }),
  remove: (id: string) => api<void>(`/api/v1/push/devices/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  probe: () => api<PushResult>('/api/v1/push/test', { method: 'POST' }),
  settings: () => api<PushSettings>('/api/v1/settings/push'),
  saveSettings: (change: { contact?: string; hosts?: string[] }) => api<PushSettings>('/api/v1/settings/push', { method: 'PUT', body: change }),
  renew: (current_password: string) => api<PushSettings>('/api/v1/settings/push/renew', { method: 'POST', body: { current_password } }),
}

export const accountsApi = {
  remove: (id: number, body: { current_password: string; confirm_name: string }) =>
    api<void>(`/api/v1/accounts/${id}`, { method: 'DELETE', body }),
}
