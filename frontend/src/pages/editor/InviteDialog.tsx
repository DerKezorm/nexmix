/**
 * "Invite guests": the link and its QR code, what the guests may do, who is there. Opening the dialog makes an invitation
 * that waits (its code is shown at once); "Start invitation" lets guests in, "Done" saves a change of a running one.
 * Ending it and taking a guest out ask first and say that it cannot be undone. Answers about the guests count only while
 * they are the newest (`order`): one that left before an event or before "End invitation" never sets the number back.
 */
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../../api/client'
import { invitesApi, type InviteRights, type InviteView } from '../../api/guests'
import { Avatar } from '../../components/Avatar'
import { Dialog } from '../../components/Dialog'
import { useConfirm } from '../../components/useConfirm'
import { copyText } from '../../lib/copy'
import { errorText } from '../../lib/errors'
import { newest, type Newest } from '../../lib/newest'
import { iso } from '../../lib/say'

type Props = {
  playlistId: number
  name: string
  operator: boolean
  /** The dialog was closed; `count`: the guests now (null: no running invitation). */
  onClose: () => void
  onChanged: (guests: number | null) => void
  onToast: (text: string) => void
  /** Changes of guests that came as events while the dialog is open. */
  refreshKey: number
  /** The order of everything that tells the number of guests, shared with the page that has the events. */
  order?: Newest
}

const codeOf = (error: unknown) => (error instanceof ApiError ? error.code : 'internal_error')

function Switch({ label, hint, checked, onChange, disabled = false }: { label: string; hint?: string; checked: boolean; onChange: (value: boolean) => void; disabled?: boolean }) {
  return (
    <label className={'toggle ' + (disabled ? 'opacity-60' : 'cursor-pointer')}>
      <span>
        {label}
        {hint && <small>{hint}</small>}
      </span>
      <span className={`sw ${checked ? 'on' : ''}`}>
        <button type="button" role="switch" aria-checked={checked} aria-label={label} disabled={disabled} onClick={() => onChange(!checked)} />
        <span aria-hidden />
      </span>
    </label>
  )
}

function Count({ label, value, min, max, onChange }: { label: string; value: number; min: number; max: number; onChange: (value: number) => void }) {
  return (
    <label className="toggle">
      <span>{label}</span>
      <input
        className="num"
        type="number"
        inputMode="numeric"
        min={min}
        max={max}
        value={Number.isFinite(value) ? value : ''}
        aria-label={label}
        onChange={(event) => onChange(event.target.valueAsNumber)}
        onBlur={() => onChange(Math.max(min, Math.min(max, Math.round(Number.isFinite(value) ? value : min))))}
      />
    </label>
  )
}

export function InviteDialog({ playlistId, name, operator, onClose, onChanged, onToast, refreshKey, order: shared }: Props) {
  const { t, i18n } = useTranslation()
  const [own] = useState(newest)
  const order = shared ?? own
  const [view, setView] = useState<InviteView | null>(null)
  const [rights, setRights] = useState<InviteRights | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const { ask, dialog } = useConfirm()

  /** An answer of the server, taken when nothing newer came while it was on its way. */
  const took = useCallback(
    (next: InviteView, ticket: number, keepRights = false) => {
      if (!order.take(ticket)) return
      setView(next)
      const invite = next.invite
      if (invite)
        setRights((before) =>
          keepRights && before
            ? before
            : { may_add: invite.may_add, may_sort: invite.may_sort, max: invite.max, hours: invite.hours, preview: invite.preview },
        )
      onChanged(next.invite?.state === 'running' ? next.guests.length : null)
    },
    [onChanged, order],
  )

  // Opening makes an invitation that waits, or shows the one there is.
  useEffect(() => {
    const ticket = order.ticket()
    invitesApi
      .prepare(playlistId)
      .then((next) => took(next, ticket))
      .catch((error) => setProblem(errorText(codeOf(error))))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playlistId])

  // A guest came or went: the list anew, the switches as they are.
  useEffect(() => {
    if (!refreshKey) return
    const ticket = order.ticket()
    invitesApi
      .read(playlistId)
      .then((next) => took(next, ticket, true))
      .catch(() => undefined)
  }, [refreshKey, playlistId, took, order])

  const invite = view?.invite ?? null
  const running = invite?.state === 'running'
  const link = invite?.link ?? ''

  const save = async () => {
    if (!rights || busy) return
    setBusy(true)
    setProblem(null)
    try {
      const values = {
        ...rights,
        max: Math.max(1, Math.min(100, Math.round(rights.max || 1))),
        hours: Math.max(1, Math.min(720, Math.round(rights.hours || 1))),
      }
      const ticket = order.ticket()
      const next = await invitesApi.change(playlistId, {
        ...values,
        start: !running,
      })
      took(next, ticket)
      onToast(running ? t('guestInvite.saved') : t('guestInvite.started'))
      onClose()
    } catch (error) {
      setProblem(errorText(codeOf(error)))
    } finally {
      setBusy(false)
    }
  }

  const end = () =>
    ask({
      title: t('guestInvite.endTitle'),
      text: t('guestInvite.endText'),
      confirm: t('guestInvite.end'),
      run: async () => {
        const ticket = order.ticket()
        await invitesApi.end(playlistId)
        if (order.take(ticket)) onChanged(null)
        onToast(t('guestInvite.ended'))
        onClose()
      },
    })

  const kick = (guest: { id: number; name: string }) =>
    ask({
      title: t('guestInvite.removeTitle', { name: iso(guest.name) }),
      text: t('guestInvite.removeText', { name: iso(guest.name) }),
      confirm: t('guestInvite.remove'),
      run: async () => {
        await invitesApi.removeGuest(playlistId, guest.id)
        const ticket = order.ticket()
        took(await invitesApi.read(playlistId), ticket, true)
      },
    })

  const until = invite?.expires_at
    ? new Date(invite.expires_at).toLocaleString(i18n.language, {
        weekday: 'short',
        hour: '2-digit',
        minute: '2-digit',
      })
    : ''
  const reach = view?.address.reach ?? 'internet'

  return (
    <Dialog title={t('guestInvite.title')} sub={t('guestInvite.text')} onClose={onClose} className="!max-w-[640px]">
      <div className="mood-body soft-scroll">
      {problem && (
        <p role="alert" className="mb-3 text-sm text-bad">
          {problem}
        </p>
      )}
      {invite && rights && (
        <div className="invite" data-testid="invite">
          <div className="qr">{invite.qr && <img src={'data:image/svg+xml;utf8,' + encodeURIComponent(invite.qr)} alt={t('guestInvite.qr', { name: iso(name) })} />}</div>
          <div className="min-w-0 w-full">
            <div className="linkbox">
              <input className="input" readOnly value={link} aria-label={t('guestInvite.link')} data-testid="invite-link" onFocus={(event) => event.currentTarget.select()} />
              <button
                type="button"
                className="btn"
                onClick={async () => {
                  if (await copyText(link)) onToast(t('guestInvite.copied'))
                }}
              >
                {t('guestInvite.copy')}
              </button>
            </div>
            <div className="mt-1.5 text-[12.5px] text-muted" data-testid="invite-valid">
              {running ? t('guestInvite.validUntil', { time: until }) : t('guestInvite.validFor', { count: rights.hours })}
            </div>
            {reach !== 'internet' && (
              <div className="note warn" role="note" data-testid="invite-reach">
                {reach === 'machine' ? t('guestInvite.reachMachine') : t('guestInvite.reachHome')}
                {!view?.address.configured && operator && <> {t('guestInvite.reachSet')}</>}
              </div>
            )}
            <div className="mt-3">
              <Switch label={t('guestInvite.mayAdd')} checked={rights.may_add} onChange={(may_add) => setRights({ ...rights, may_add })} />
              <Switch label={t('guestInvite.maySort')} hint={t('guestInvite.maySortHint')} checked={rights.may_sort} onChange={(may_sort) => setRights({ ...rights, may_sort })} />
              <Count label={t('guestInvite.max')} value={rights.max} min={1} max={100} onChange={(max) => setRights({ ...rights, max })} />
              <Count label={t('guestInvite.hours')} value={rights.hours} min={1} max={720} onChange={(hours) => setRights({ ...rights, hours })} />
              {view?.listening === false ? (
                <Switch label={t('guestInvite.preview')} hint={t('guestInvite.previewOffServer')} checked={false} disabled onChange={() => undefined} />
              ) : (
                <Switch label={t('guestInvite.preview')} hint={t('guestInvite.previewHint')} checked={rights.preview} onChange={(preview) => setRights({ ...rights, preview })} />
              )}
            </div>
          </div>
        </div>
      )}
      {view && view.guests.length > 0 && (
        <div className="field">
          <span className="lab">{t('guestInvite.there')}</span>
          <div className="guests" data-testid="invite-guests">
            {view.guests.map((guest) => (
              <div className="g" key={guest.id}>
                <Avatar person={{ id: 0, name: guest.name, avatar: null }} size={26} />
                <span className="grow" dir="auto">
                  <b>{guest.name}</b> <span className="text-muted">· {t('guestInvite.added', { count: guest.count })}</span>
                </span>
                <button type="button" className="btn ghost" aria-label={t('guestInvite.removeNamed', { name: guest.name })} onClick={() => kick(guest)}>
                  {t('guestInvite.remove')}
                </button>
              </div>
            ))}
          </div>
        </div>
      )}
      </div>
      <div className="dfoot">
        <span className="grow">{running ? t('guestInvite.active') : t('guestInvite.notActive')}</span>
        {invite && running && (
          <button type="button" className="btn" onClick={end}>
            {t('guestInvite.end')}
          </button>
        )}
        <button type="button" className="btn primary" data-primary disabled={!rights || busy} onClick={() => void save()}>
          {running ? t('common.done') : t('guestInvite.start')}
        </button>
      </div>
      {dialog}
    </Dialog>
  )
}
