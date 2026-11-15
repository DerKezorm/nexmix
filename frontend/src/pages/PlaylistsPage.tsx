/**
 * The playlists: the title and the line below it; before the catalogue is there what to do about it (the operator is
 * led to the Plex card, a run shows its progress); then the cards of the playlists the account sees, the dashed card
 * for a new one, the ones it hid and the trash below them.
 *
 * What the overview leaves out is the account's own view (kept on the server): the playlists it hid one by one (each
 * card's menu, "Show again" below), and the smart playlists Plex makes by itself unless it asks for them (the chip
 * above the cards, off from the start). Nothing of it reaches Plex or anybody else.
 */
import { EyeOff, MoreHorizontal, Plus, Sparkles, Users } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Trans, useTranslation } from 'react-i18next'
import { Link, useNavigate } from 'react-router-dom'

import { authApi } from '../api/client'
import { playlistsApi, type Card } from '../api/playlists'
import { Dialog } from '../components/Dialog'
import { Mosaic } from '../components/Mosaic'
import { PageHead } from '../components/PageHead'
import { People } from '../components/People'
import { SyncMark } from '../components/PlexSync'
import { useConfirm } from '../components/useConfirm'
import { percentOf, useCatalogStatus } from '../lib/catalogStatus'
import { shortDate } from '../lib/dates'
import { errorText } from '../lib/errors'
import { length } from '../lib/music'
import { iso } from '../lib/say'
import { kindName, useServers } from '../lib/servers'
import { useAuth } from '../state/auth'

/** How often the open page of the playlists looks for changes of others. */
export const LOOK_MS = 20_000

const codeOf = (error: unknown) => (error as { code?: string })?.code ?? 'internal_error'

function PlaylistCard({ card, mine, onHide }: { card: Card; mine: boolean; onHide: (card: Card) => void }) {
  const { t } = useTranslation()
  const [menu, setMenu] = useState(false)
  useEffect(() => {
    if (!menu) return
    const away = (event: MouseEvent) => {
      if (!(event.target as Element).closest(`[data-card-menu="${card.id}"]`)) setMenu(false)
    }
    const escape = (event: KeyboardEvent) => event.key === 'Escape' && setMenu(false)
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', away)
      document.removeEventListener('keydown', escape)
    }
  }, [menu, card.id])
  const badge = !mine && card.owner ? t('playlists.fromBadge', { name: iso(card.owner.display_name || card.owner.name) }) : card.shares > 0 ? t('playlists.sharedBadge') : null
  return (
    <div className="pcell">
      <Link className="pcard" to={`/playlists/${card.id}`} data-testid="playlist-card">
        <Mosaic covers={card.covers}>
          {card.guests !== null && card.guests !== undefined && (
            <span className="badge" data-testid="guests-badge">
              <Users size={13} aria-hidden />
              {t('playlists.guestsBadge', { count: card.guests })}
            </span>
          )}
          {badge && (
            <span className="badge">
              <Users size={13} aria-hidden />
              {badge}
            </span>
          )}
          {card.smart && (
            <span className="badge right" data-testid="smart-badge">
              <Sparkles size={13} aria-hidden />
              {t('sync.smartBadge')}
            </span>
          )}
        </Mosaic>
        <div className="body">
          <h3 dir="auto">{card.name}</h3>
          <div className="meta">{t('playlists.meta', { count: card.count, length: length(card.duration) })}</div>
          <div className="foot">
            <People people={card.people} />
            <span className="flex-1" />
            {card.plex && <SyncMark plex={card.plex} />}
          </div>
        </div>
      </Link>
      <span className="pmenu" data-card-menu={card.id}>
        <button
          type="button"
          className="more"
          aria-expanded={menu}
          aria-haspopup="true"
          aria-label={t('playlists.more', { name: card.name })}
          onClick={() => setMenu(!menu)}
          data-testid="card-menu"
        >
          <MoreHorizontal size={16} aria-hidden />
        </button>
        {menu && (
          <span className="card rise absolute top-full right-0 z-40 mt-1 block w-48 p-1.5">
            <button
              type="button"
              onClick={() => {
                setMenu(false)
                onHide(card)
              }}
              className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm font-semibold text-ink-2 hover:bg-sheet-2"
              data-testid="card-hide"
            >
              <EyeOff size={15} aria-hidden />
              {t('playlists.hide')}
            </button>
          </span>
        )}
      </span>
    </div>
  )
}

function Hidden({ cards, onShow }: { cards: Card[]; onShow: (card: Card) => void }) {
  const { t } = useTranslation()
  if (!cards.length) return null
  return (
    <section className="trashlist" aria-labelledby="hidden-title" data-testid="hidden">
      <h2 id="hidden-title">{t('playlists.hidden.title')}</h2>
      <p>{t('playlists.hidden.text')}</p>
      {cards.map((card) => (
        <div className="trashrow" key={card.id} data-testid="hidden-row">
          <div className="grow">
            <b dir="auto">{card.name}</b>
            <small>
              {t('playlists.meta', { count: card.count, length: length(card.duration) })}
              {card.smart && <> · {t('playlists.hidden.smart')}</>}
            </small>
          </div>
          <button type="button" className="btn" onClick={() => onShow(card)}>
            {t('playlists.hidden.show')}
          </button>
        </div>
      ))}
    </section>
  )
}

function Trash({ cards, days, reload }: { cards: Card[]; days: number; reload: () => Promise<void> }) {
  const { t, i18n } = useTranslation()
  const { ask, dialog } = useConfirm()
  const [problem, setProblem] = useState<string | null>(null)
  if (!cards.length) return null
  return (
    <section className="trashlist" aria-labelledby="trash-title" data-testid="trash">
      <h2 id="trash-title">{t('playlists.trash.title')}</h2>
      <p>{t('playlists.trash.text', { days })}</p>
      {problem && (
        <p role="alert" className="text-sm text-bad">
          {problem}
        </p>
      )}
      {cards.map((card) => (
        <div className="trashrow" key={card.id} data-testid="trash-row">
          <div className="grow">
            <b dir="auto">{card.name}</b>
            <small>
              {t('playlists.trash.left', { date: shortDate(card.deleted_at ?? '', i18n.language), count: card.days_left ?? 0 })}
              {card.plex?.gone_in_plex && <> · {t('sync.goneInPlex')}</>}
            </small>
          </div>
          <button
            type="button"
            className="btn"
            onClick={async () => {
              setProblem(null)
              const restore = async () => {
                try {
                  await playlistsApi.restore(card.id)
                  await reload()
                } catch (error) {
                  setProblem(errorText(codeOf(error)))
                }
              }
              // A smart playlist deleted in Plex comes back as an ordinary one: asked first, and said so.
              if (card.smart)
                ask({
                  title: t('playlists.trash.smartTitle'),
                  text: t('playlists.trash.smartText', { name: iso(card.name), count: card.count }),
                  confirm: t('playlists.trash.restore'),
                  danger: false,
                  run: restore,
                })
              else await restore()
            }}
          >
            {t('playlists.trash.restore')}
          </button>
          <button
            type="button"
            className="btn danger"
            onClick={() =>
              ask({
                title: t('playlists.trash.purgeTitle'),
                text: t('playlists.trash.purgeText', { name: iso(card.name) }),
                confirm: t('playlists.trash.purge'),
                run: async () => {
                  await playlistsApi.purge(card.id)
                  await reload()
                },
              })
            }
          >
            {t('playlists.trash.purge')}
          </button>
        </div>
      ))}
      {dialog}
    </section>
  )
}

export function PlaylistsPage() {
  const { t, i18n } = useTranslation()
  const { me, setMe } = useAuth()
  const navigate = useNavigate()
  const { status } = useCatalogStatus()
  const [cards, setCards] = useState<Card[] | null>(null)
  const [trash, setTrash] = useState<{ cards: Card[]; days: number }>({ cards: [], days: 30 })
  const [problem, setProblem] = useState<string | null>(null)
  const [showSmart, setShowSmart] = useState<boolean>(Boolean(me?.profile?.show_smart_playlists))
  const making = useRef(false)
  const [busy, setBusy] = useState(false)
  // With more than one server ready, a new playlist asks where its tracks come from (the first connected first).
  const servers = useServers()
  const choices = (servers ?? []).filter((one) => one.active && one.ready)
  const [choosing, setChoosing] = useState<number | null>(null)
  const operator = me?.role === 'operator'
  const ready = Boolean(status?.ready)
  const reload = useCallback(async () => {
    try {
      const [live, gone] = await Promise.all([playlistsApi.list(), playlistsApi.list(true)])
      setCards(live.playlists)
      if (typeof live.show_smart_playlists === 'boolean') setShowSmart(live.show_smart_playlists)
      setTrash({ cards: gone.playlists, days: gone.trash_days })
      setProblem(null)
    } catch (error) {
      setProblem(errorText(codeOf(error)))
    }
  }, [])
  useEffect(() => {
    if (ready) void reload()
  }, [ready, reload])
  // What others change (a new playlist shared, entries from Plex or from guests) comes in while the page is open: a
  // look every LOOK_MS while it is in view, and at once when it comes back into view.
  useEffect(() => {
    if (!ready) return
    const look = () => {
      if (document.visibilityState === 'visible') void reload()
    }
    const timer = window.setInterval(look, LOOK_MS)
    document.addEventListener('visibilitychange', look)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', look)
    }
  }, [ready, reload])
  // One click, one playlist: a second click while the first is on its way does nothing.
  const make = async (server?: number) => {
    if (making.current) return
    if (server === undefined && choices.length > 1) {
      setChoosing((choices.find((one) => one.primary) ?? choices[0]).id)
      return
    }
    making.current = true
    setBusy(true)
    try {
      const made = await playlistsApi.create(t('playlists.newName'), server)
      navigate(`/playlists/${made.id}`, { state: { fresh: true } })
    } catch (error) {
      setProblem(errorText(codeOf(error)))
      making.current = false
      setBusy(false)
    }
  }
  // Hiding and the chip are this account's view only: changed here at once, kept on the server, put back on a failure.
  const setHidden = async (card: Card, hidden: boolean) => {
    setProblem(null)
    const flip = (value: boolean) => setCards((current) => current?.map((one) => (one.id === card.id ? { ...one, hidden: value } : one)) ?? current)
    flip(hidden)
    try {
      await (hidden ? playlistsApi.hide(card.id) : playlistsApi.show(card.id))
    } catch (error) {
      flip(!hidden)
      setProblem(errorText(codeOf(error)))
    }
  }
  const toggleSmart = async () => {
    const next = !showSmart
    setProblem(null)
    setShowSmart(next)
    try {
      const profile = await authApi.preferences({ show_smart_playlists: next })
      if (me) setMe({ ...me, profile })
    } catch (error) {
      setShowSmart(!next)
      setProblem(errorText(codeOf(error)))
    }
  }
  const smartCount = cards?.filter((card) => card.smart).length ?? 0
  const shown = cards?.filter((card) => !card.hidden && (showSmart || !card.smart)) ?? null
  const hidden = cards?.filter((card) => card.hidden && (showSmart || !card.smart)) ?? []
  const settings = <Link to="/settings?tab=plex" />
  const tracks = (status?.counts.tracks ?? 0).toLocaleString(i18n.language)
  const library = iso(status?.library_title ?? '')
  const head = ready ? t('playlists.textReady', { tracks, library }) : t('playlists.text')
  let body
  if (!status) body = null
  else if (!status.connected || !status.library)
    body = (
      <p className="empty" data-testid="playlists-empty">
        {operator ? <Trans i18nKey="playlists.emptyOperator" components={{ settings }} /> : t('playlists.notConnectedMember')}
      </p>
    )
  else if (!status.ready && (status.running || status.queued)) {
    const percent = percentOf(status)
    body = (
      <div className="empty mx-auto max-w-md" data-testid="playlists-loading">
        <span className="spin" aria-hidden />
        <p className="mt-3 text-base font-semibold text-ink">{t('playlists.loading')}</p>
        <p className="mt-1 text-sm">{t('playlists.loadingText')}</p>
        <span className="meter mt-4" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} aria-label={t('playlists.loading')}>
          <i style={{ width: `${percent}%` }} />
        </span>
        <p className="mt-1 text-xs">{percent} %</p>
      </div>
    )
  } else if (!status.ready)
    body = (
      <p className="empty" data-testid="playlists-failed">
        {operator ? (
          <Trans i18nKey="playlists.failedOperator" values={{ reason: errorText(status.last?.error ?? 'internal_error') }} components={{ settings }} />
        ) : (
          t('playlists.failedMember')
        )}
      </p>
    )
  else
    body = (
      <>
        {problem && (
          <p role="alert" className="mb-4 text-sm text-bad">
            {problem}
          </p>
        )}
        {shown && shown.length === 0 && hidden.length === 0 && (
          <div className="empty" data-testid="playlists-none">
            <p className="text-base font-semibold text-ink">{t('playlists.none')}</p>
            <p className="mt-1 text-sm">{t('playlists.noneText', { tracks, library })}</p>
          </div>
        )}
        {smartCount > 0 && (
          <div className="filters mt-0 mb-5" data-testid="overview-filters">
            <button type="button" className={`chip${showSmart ? ' on' : ''}`} aria-pressed={showSmart} onClick={() => void toggleSmart()} data-testid="smart-toggle">
              <Sparkles size={13} aria-hidden />
              {t('playlists.smartShow')} <span className="n">{smartCount}</span>
            </button>
          </div>
        )}
        {shown && (
          <div className="grid" data-testid="playlist-grid">
            {shown.map((card) => (
              <PlaylistCard key={card.id} card={card} mine={card.owner?.id === me?.id} onHide={(one) => void setHidden(one, true)} />
            ))}
            <button type="button" className="newcard" onClick={() => void make()} disabled={busy}>
              <span>
                <span className="plus" aria-hidden>
                  +
                </span>
                {t('playlists.new')}
              </span>
            </button>
          </div>
        )}
        <Hidden cards={hidden} onShow={(card) => void setHidden(card, false)} />
        <Trash cards={trash.cards} days={trash.days} reload={reload} />
      </>
    )
  return (
    <div className="view">
      <PageHead title={t('playlists.title')} text={head}>
        {ready && (
          <>
            <div className="flex-1" />
            <button type="button" className="btn primary" onClick={() => void make()} disabled={busy}>
              <Plus aria-hidden />
              {t('playlists.new')}
            </button>
          </>
        )}
      </PageHead>
      {body}
      {choosing !== null && (
        <Dialog title={t('playlists.chooseServer')} sub={t('playlists.chooseServerText')} onClose={() => setChoosing(null)}>
          <form
            onSubmit={(event) => {
              event.preventDefault()
              const chosen = choosing
              setChoosing(null)
              void make(chosen)
            }}
          >
            <div className="space-y-2" role="radiogroup" aria-label={t('playlists.chooseServer')} data-testid="server-choice">
              {choices.map((one) => (
                <label key={one.id} className="pick cursor-pointer">
                  <input type="radio" name="server" value={one.id} checked={choosing === one.id} onChange={() => setChoosing(one.id)} />
                  <span className="grow">
                    <b>{t('playlists.serverOption', { kind: kindName(one.kind), name: iso(one.name) })}</b>
                    {one.library && <small>{iso(one.library)}</small>}
                  </span>
                </label>
              ))}
            </div>
            <div className="dlg-foot">
              <span className="grow" />
              <button type="button" className="btn" onClick={() => setChoosing(null)}>
                {t('common.cancel')}
              </button>
              <button type="submit" className="btn primary" data-primary disabled={busy}>
                {t('playlists.make')}
              </button>
            </div>
          </form>
        </Dialog>
      )}
    </div>
  )
}
