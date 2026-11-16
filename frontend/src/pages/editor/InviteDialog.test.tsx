/**
 * The dialog "Invite guests" against a stand-in server: opening it makes an invitation that waits and shows its link and
 * QR code; the switches and numbers go with "Start invitation"; a link only the home network reaches is said; ending
 * the invitation and taking a guest out ask first, say that it cannot be undone, and send nothing without the answer.
 */
import type { InviteView } from '../../api/guests'
import { changeLanguage } from '../../i18n'
import { act, useState } from 'react'

import { newest } from '../../lib/newest'
import { Page, Server, plain } from '../../test/harness'
import { until } from '../../test/wait'
import { InviteDialog } from './InviteDialog'

const server = new Server()
const page = new Page()

const waiting = (over: Partial<InviteView> = {}): InviteView => ({
  invite: { code: 'K7MX-QR2H-PA4N', link: 'http://192.168.1.20:8620/j/K7MX-QR2H-PA4N', qr: '<svg xmlns="http://www.w3.org/2000/svg"></svg>', state: 'waiting', may_add: true, may_sort: false, max: 10, hours: 24, preview: true, started_at: null, expires_at: null },
  guests: [],
  defaults: { may_add: true, may_sort: false, max: 10, hours: 24, preview: true },
  address: { url: 'http://192.168.1.20:8620', configured: false, reach: 'home' },
  listening: true,
  ...over,
})

const running = (): InviteView => {
  const view = waiting({ guests: [{ id: 4, name: 'Lena', count: 2, joined_at: '2026-11-13T18:00:00Z' }] })
  view.invite = { ...view.invite!, state: 'running', started_at: '2026-11-13T18:00:00Z', expires_at: '2026-11-14T18:00:00Z' }
  return view
}

let closed = 0
let counts: (number | null)[] = []
let toasts: string[] = []

beforeEach(async () => {
  await changeLanguage('de', false)
  closed = 0
  counts = []
  toasts = []
  server.answers = { 'POST /api/v1/playlists/7/invite': waiting() }
  server.fails = {}
  server.install()
})
afterEach(() => {
  page.close()
  vi.unstubAllGlobals()
})

async function open(operator = true): Promise<void> {
  await page.show(
    <InviteDialog playlistId={7} name="Grillabend" operator={operator} refreshKey={0} onClose={() => (closed += 1)} onChanged={(count) => counts.push(count)} onToast={(text) => toasts.push(text)} />,
  )
  await until(() => document.querySelector('[data-testid="invite"]'))
}

const switchOf = (label: string) => document.querySelector<HTMLButtonElement>(`button[role="switch"][aria-label="${label}"]`)
const numberOf = (label: string) => document.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)

describe('inviting guests', () => {
  it('makes an invitation that waits and shows its link, its QR code and the warning about the address', async () => {
    await open()
    expect(server.called('POST', '/api/v1/playlists/7/invite')).toBeTruthy()
    expect(document.querySelector<HTMLInputElement>('[data-testid="invite-link"]')!.value).toBe('http://192.168.1.20:8620/j/K7MX-QR2H-PA4N')
    expect(document.querySelector<HTMLImageElement>('.qr img')!.src).toContain('data:image/svg+xml')
    expect(plain(document.querySelector('[data-testid="invite-reach"]')!.textContent)).toBe(
      'Handys außerhalb deines WLANs erreichen diese Adresse nicht. Die öffentliche Adresse stellst du unter Einstellungen › Server › Anmeldung ein.',
    )
    expect(plain(document.querySelector('[data-testid="invite-valid"]')!.textContent)).toBe('Gültig für 24 Stunden · danach ist die Einladung für alle Gäste vorbei.')
    expect(page.text).toContain('Noch nicht aktiv.')
    expect(page.button('Einladung beenden')).toBeUndefined()
    expect(page.text).toContain('30 Sekunden ab gut einem Drittel des Titels, über nexmix geleitet. Dein Plex-Schlüssel bleibt auf dem Server.')
  })

  it('says the warning for a member without the hint where the operator sets the address, and none for the internet', async () => {
    await open(false)
    expect(plain(document.querySelector('[data-testid="invite-reach"]')!.textContent)).toBe('Handys außerhalb deines WLANs erreichen diese Adresse nicht.')
    page.close()
    server.answers['POST /api/v1/playlists/7/invite'] = waiting({ address: { url: 'https://mix.example.com', configured: true, reach: 'internet' } })
    await open()
    expect(document.querySelector('[data-testid="invite-reach"]')).toBeNull()
  })

  it('sends the rights with "Start invitation" and closes', async () => {
    server.answers['PUT /api/v1/playlists/7/invite'] = running()
    await open()
    await page.click(switchOf('Darf sortieren und fremde Titel entfernen'))
    await page.click(switchOf('Reinhören erlauben'))
    await page.type(numberOf('Höchstens Titel je Gast'), '3')
    await page.type(numberOf('Gültig (Stunden)'), '6')
    expect(server.changes()).toEqual(['POST /api/v1/playlists/7/invite'])
    await page.click(page.button('Einladung starten'))
    expect(server.called('PUT', '/api/v1/playlists/7/invite')!.body).toEqual({ may_add: true, may_sort: true, max: 3, hours: 6, preview: false, start: true })
    expect(toasts).toEqual(['Einladung aktiv.'])
    expect(closed).toBe(1)
    expect(counts.at(-1)).toBe(1)
  })

  it('shows listening in as off, without a way to turn it on, while the operator has it off for everybody', async () => {
    server.answers['POST /api/v1/playlists/7/invite'] = waiting({ listening: false })
    server.answers['PUT /api/v1/playlists/7/invite'] = running()
    await open()
    const off = switchOf('Reinhören erlauben')!
    expect(off.disabled).toBe(true)
    expect(off.getAttribute('aria-checked')).toBe('false')
    expect(plain(off.closest('label')!.textContent)).toContain('Reinhören ist auf diesem Server ausgeschaltet (Einstellungen › Server › Medienserver).')
    await page.click(off)
    expect(off.getAttribute('aria-checked')).toBe('false')
  })

  it('asks before ending and sends nothing without the answer', async () => {
    server.answers['POST /api/v1/playlists/7/invite'] = running()
    server.answers['DELETE /api/v1/playlists/7/invite'] = 204
    await open()
    expect(page.text).toContain('Einladung ist aktiv.')
    await page.click(page.button('Einladung beenden'))
    expect(plain(page.dialog!.textContent)).toContain('Das lässt sich nicht zurückholen')
    await page.inDialog('Abbrechen')
    expect(server.changes()).toEqual(['POST /api/v1/playlists/7/invite'])
    await page.click(page.button('Einladung beenden'))
    await page.inDialog('Einladung beenden')
    expect(server.changes()).toEqual(['POST /api/v1/playlists/7/invite', 'DELETE /api/v1/playlists/7/invite'])
    expect(toasts).toEqual(['Einladung beendet. Gäste kommen nicht mehr rein.'])
    expect(counts.at(-1)).toBeNull()
  })

  it('shows who is in with their tracks and asks before taking a guest out', async () => {
    server.answers['POST /api/v1/playlists/7/invite'] = running()
    server.answers['DELETE /api/v1/playlists/7/invite/guests/4'] = 204
    server.answers['GET /api/v1/playlists/7/invite'] = { ...running(), guests: [] }
    await open()
    expect(plain(document.querySelector('[data-testid="invite-guests"]')!.textContent)).toContain('Lena · 2 Titel hinzugefügt')
    await page.click(page.button('Lena entfernen'))
    const asked = plain(page.dialog!.textContent)
    expect(asked).toContain(
      'Lena ist sofort draußen und kommt in diesem Browser nicht wieder herein. Mit dem Link kann Lena als neuer Gast beitreten, solange die Einladung läuft. Wer ganz draußen bleiben soll: Einladung beenden. Das lässt sich nicht zurückholen.',
    )
    expect(asked).toContain('Die Titel, die Lena hinzugefügt hat, bleiben in der Playlist.')
    await page.inDialog('Abbrechen')
    expect(server.changes()).toEqual(['POST /api/v1/playlists/7/invite'])
    await page.click(page.button('Lena entfernen'))
    await page.inDialog('Entfernen')
    expect(server.changes()).toEqual(['POST /api/v1/playlists/7/invite', 'DELETE /api/v1/playlists/7/invite/guests/4'])
    await until(() => !document.querySelector('[data-testid="invite-guests"]'))
  })

  it('never lets an answer that left before the end set the number of guests back', async () => {
    server.answers['POST /api/v1/playlists/7/invite'] = running()
    server.answers['DELETE /api/v1/playlists/7/invite'] = 204
    const answer = server.fetch()
    let release: () => void = () => undefined
    const held = new Promise<void>((resolve) => (release = resolve))
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        // The list read after a guest came: slow, it comes back only after the end.
        if ((init?.method ?? 'GET') === 'GET' && url === '/api/v1/playlists/7/invite') {
          await held
          return new Response(JSON.stringify(running()), { status: 200, headers: { 'Content-Type': 'application/json' } })
        }
        return answer(url, init)
      }),
    )
    const order = newest()
    const dialog = (key: number) => (
      <InviteDialog playlistId={7} name="Grillabend" operator refreshKey={key} order={order} onClose={() => (closed += 1)} onChanged={(count) => counts.push(count)} onToast={(text) => toasts.push(text)} />
    )
    let guestCame: () => void = () => undefined
    function Editor() {
      const [key, setKey] = useState(0)
      guestCame = () => setKey(1)
      return dialog(key)
    }
    await page.show(<Editor />)
    await until(() => document.querySelector('[data-testid="invite"]'))
    expect(counts).toEqual([1])
    // A guest came: the list is read again, and that answer is slow. (Clicked by hand: the page never goes idle.)
    await act(async () => guestCame())
    await act(async () => page.button('Einladung beenden')!.click())
    await until(() => page.dialog?.textContent?.includes('Das lässt sich nicht zurückholen'))
    await act(async () => page.button('Einladung beenden', page.dialog!)!.click())
    await until(() => counts.length === 2)
    expect(counts).toEqual([1, null])
    await act(async () => release())
    await act(async () => new Promise((resolve) => setTimeout(resolve, 50)))
    expect(counts).toEqual([1, null])
  })

  it('takes a newer answer, never an older one', () => {
    const order = newest()
    const first = order.ticket()
    const second = order.ticket()
    expect(order.take(second)).toBe(true)
    expect(order.take(first)).toBe(false)
    expect(order.take(order.ticket())).toBe(true)
  })

  it('says a refusal of the server', async () => {
    server.fails['POST /api/v1/playlists/7/invite'] = { status: 403, code: 'read_only' }
    await page.show(<InviteDialog playlistId={7} name="Grillabend" operator refreshKey={0} onClose={() => undefined} onChanged={() => undefined} onToast={() => undefined} />)
    await until(() => document.querySelector('[role="alert"]'))
    expect(document.querySelector('[data-testid="invite"]')).toBeNull()
  })
})
