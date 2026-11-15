/**
 * Settings, Server: a card per connected Jellyfin server, and the way to connect one.
 *
 * Connecting: the address, then Quick Connect (nexmix shows a code, the operator enters it in Jellyfin under Quick
 * Connect in the profile; the page asks every two seconds whether it was confirmed, ten minutes at most) or an API key
 * typed by hand. Then the music library and the Jellyfin user the playlists will belong to there. No token, no
 * address and no id of Jellyfin comes back to the browser: a library and a user are named by nexmix's own handles.
 *
 * Connected: the steps with their ticks, the catalogue (counts, progress of a run, the last sync, a failure), that
 * Jellyfin has no moods, that playlists live in nexmix only for now, and the buttons to sync now, to choose another
 * library or user and to disconnect (asks first and says what goes).
 */
import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../../api/client'
import { busy, catalogApi, type CatalogStatus } from '../../api/plex'
import { jellyfinApi, type Handle, type JellyfinServer, type JellyfinUser, type QuickStart } from '../../api/servers'
import { useConfirm } from '../../components/useConfirm'
import { percentOf, useCatalogStatus } from '../../lib/catalogStatus'
import { errorText } from '../../lib/errors'
import { iso } from '../../lib/say'
import { Steps } from './PlexCard'
import { Button, Card, Feedback, Input, useAction } from './ui'

/** How long to wait between two asks whether the code was confirmed; the tests shorten it. */
// eslint-disable-next-line react-refresh/only-export-components
export const quickTiming = { poll: 2000 }

function when(moment: string | null, language: string): string {
  if (!moment) return ''
  try {
    return new Intl.DateTimeFormat(language, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(moment))
  } catch {
    return moment
  }
}

/** The catalogue of one server, as the Plex card shows it: counts or the progress of a run, the last sync, a failure. */
export function ServerCatalog({ status, extra, kind = 'plex' }: { status: CatalogStatus | null; extra?: ReactNode; kind?: 'plex' | 'jellyfin' }) {
  const { t, i18n } = useTranslation()
  const number = (value: number) => value.toLocaleString(i18n.language)
  const full = status?.running?.kind === 'full' || status?.queued === 'full'
  const percent = percentOf(status)
  const counts = status?.counts
  let catalog: ReactNode = '…'
  if (status) {
    if (status.ready && counts) catalog = t('plex.catalogCounts', { artists: number(counts.artists), albums: number(counts.albums), tracks: number(counts.tracks) })
    else if (full) catalog = t('plex.catalogLoading', { percent })
    else catalog = t('plex.catalogWaiting')
  }
  const failed = status?.last?.state === 'failed' && !status.running ? status.last : null
  return (
    <>
      <dl className="kv" style={{ marginTop: 14 }}>
        <dt>{t('plex.catalog')}</dt>
        <dd data-testid="server-catalog">
          {catalog}
          {full && (
            <span className="meter mt-2" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} aria-label={t('plex.catalog')}>
              <i style={{ width: `${percent}%` }} />
            </span>
          )}
        </dd>
        <dt>{t('plex.lastSynced')}</dt>
        <dd>{status?.synced_at ? t('plex.lastSyncedValue', { when: when(status.synced_at, i18n.language) }) : t('plex.notYet')}</dd>
        <dt>{t('plex.search')}</dt>
        <dd>{kind === 'jellyfin' ? t('jellyfin.searchValue') : t('plex.searchValue')}</dd>
        {extra}
      </dl>
      {failed && (
        <p role="alert" className="alert" data-testid="server-failed">
          {t('plex.failed', { reason: errorText(failed.error ?? 'internal_error') })}
          {status?.ready && status.synced_at && <> {t('plex.kept', { when: when(status.synced_at, i18n.language) })}</>}
        </p>
      )}
    </>
  )
}

type Choosing = { what: 'library' | 'user'; libraries: Handle[]; users: JellyfinUser[] } | null

export function JellyfinCard({ server, onChanged }: { server: JellyfinServer; onChanged: () => Promise<void> | void }) {
  const { t, i18n } = useTranslation()
  const { status, refresh } = useCatalogStatus(server.id)
  const [choosing, setChoosing] = useState<Choosing>(null)
  const action = useAction()
  const { ask, dialog } = useConfirm()

  const open = useCallback(
    async (what: 'library' | 'user') => {
      const found = await jellyfinApi.choices(server.id)
      setChoosing({ what, libraries: found.libraries ?? [], users: found.users ?? [] })
    },
    [server.id],
  )

  // Connected without a library or a user: the choice is offered at once.
  const missing = !server.library ? 'library' : !server.user ? 'user' : null
  useEffect(() => {
    if (missing) void open(missing).catch(() => undefined)
  }, [missing, open])

  const chooseLibrary = (choice: Handle) => {
    const go = async () => {
      await jellyfinApi.chooseLibrary(server.id, choice.handle)
      setChoosing(null)
      await onChanged()
      await refresh()
    }
    if (server.library && server.library.handle !== choice.handle) {
      ask({
        title: t('jellyfin.libraryTitle'),
        text: t('jellyfin.libraryText', { old: iso(server.library.title), new: iso(choice.title) }),
        confirm: t('jellyfin.libraryConfirm'),
        run: go,
      })
      return
    }
    void action.run(go)
  }

  const chooseUser = (choice: JellyfinUser) =>
    void action.run(async () => {
      await jellyfinApi.chooseUser(server.id, choice.handle)
      setChoosing(null)
      await onChanged()
      await refresh()
    })

  const disconnect = () =>
    ask({
      title: t('jellyfin.disconnectTitle'),
      text: t('jellyfin.disconnectText', { tracks: (status?.counts.tracks ?? 0).toLocaleString(i18n.language) }),
      confirm: t('jellyfin.disconnect'),
      run: async () => {
        await jellyfinApi.disconnect(server.id)
        await onChanged()
      },
    })

  const syncNow = () =>
    void action.run(async () => {
      await catalogApi.sync(server.id)
      await refresh()
    }, t('plex.syncStarted'))

  const steps = [
    { label: server.via === 'key' ? t('jellyfin.stepKey') : t('jellyfin.stepQuick'), state: 'done' as const },
    { label: server.library ? t('jellyfin.stepLibraryNamed', { name: iso(server.library.title) }) : t('jellyfin.stepLibrary'), state: server.library ? ('done' as const) : ('now' as const) },
    { label: server.user ? t('jellyfin.stepUserNamed', { name: iso(server.user.name) }) : t('jellyfin.stepUser'), state: server.user ? ('done' as const) : server.library ? ('now' as const) : ('todo' as const) },
  ]

  return (
    <Card id={`jellyfin-${server.id}`} title={t('jellyfin.title')} text={t('jellyfin.textConnected', { name: iso(server.name) })}>
      <Steps steps={steps} />
      {choosing?.what === 'library' && (
        <div data-testid="jellyfin-libraries">
          <p className="text-sm font-semibold">{t('jellyfin.chooseLibrary')}</p>
          {choosing.libraries.map((one) => (
            <div key={one.handle} className="pick">
              <div className="grow">
                <b>{one.title}</b>
              </div>
              {server.library?.handle === one.handle ? (
                <span className="chip static on">{t('plex.current')}</span>
              ) : (
                <Button onClick={() => chooseLibrary(one)} busy={action.busy} label={`${t('plex.choose')}: ${one.title}`}>
                  {t('plex.choose')}
                </Button>
              )}
            </div>
          ))}
          {server.library && (
            <div className="pt-2">
              <Button ghost onClick={() => setChoosing(null)}>
                {t('common.cancel')}
              </Button>
            </div>
          )}
        </div>
      )}
      {choosing?.what === 'user' && (
        <div data-testid="jellyfin-users">
          <p className="text-sm font-semibold">{t('jellyfin.chooseUser')}</p>
          {choosing.users.map((one) => (
            <div key={one.handle} className="pick">
              <div className="grow">
                <b>{one.name}</b>
                {one.admin && <small>{t('jellyfin.admin')}</small>}
              </div>
              {server.user?.handle === one.handle ? (
                <span className="chip static on">{t('plex.current')}</span>
              ) : (
                <Button onClick={() => chooseUser(one)} busy={action.busy} label={`${t('plex.choose')}: ${one.name}`}>
                  {t('plex.choose')}
                </Button>
              )}
            </div>
          ))}
          {server.user && (
            <div className="pt-2">
              <Button ghost onClick={() => setChoosing(null)}>
                {t('common.cancel')}
              </Button>
            </div>
          )}
        </div>
      )}
      {server.library && server.user && !choosing && (
        <div data-testid="jellyfin-connected">
          <ServerCatalog
            kind="jellyfin"
            status={status}
            extra={
              <>
                <dt>{t('plex.moods')}</dt>
                <dd>{t('jellyfin.moodsValue')}</dd>
                <dt>{t('jellyfin.playlists')}</dt>
                <dd>{t('jellyfin.playlistsValue')}</dd>
              </>
            }
          />
          <div className="flex flex-wrap gap-2" style={{ marginTop: 14 }}>
            <Button onClick={syncNow} busy={action.busy} disabled={busy(status) && (status?.running?.kind === 'full' || status?.queued === 'full')}>
              {t('plex.syncNow')}
            </Button>
            <Button ghost onClick={() => void action.run(() => open('library'))} disabled={action.busy}>
              {t('jellyfin.otherLibrary')}
            </Button>
            <Button ghost onClick={() => void action.run(() => open('user'))} disabled={action.busy}>
              {t('jellyfin.otherUser')}
            </Button>
            <Button ghost onClick={disconnect} disabled={action.busy}>
              {t('jellyfin.disconnect')}
            </Button>
          </div>
        </div>
      )}
      <Feedback problem={action.problem} done={action.done} values={action.values} />
      {dialog}
    </Card>
  )
}

type Flow = { stage: 'address' } | { stage: 'code'; quick: QuickStart; left: number } | { stage: 'expired' } | { stage: 'key' }

/** Connecting a Jellyfin server: the address, then Quick Connect or an API key. */
export function JellyfinConnect({ onConnected, onCancel }: { onConnected: () => Promise<void> | void; onCancel: () => void }) {
  const { t } = useTranslation()
  const [flow, setFlow] = useState<Flow>({ stage: 'address' })
  const [url, setUrl] = useState('')
  const [key, setKey] = useState('')
  const action = useAction()

  const quickId = flow.stage === 'code' ? flow.quick.id : null
  useEffect(() => {
    if (!quickId) return
    let stopped = false
    const timer = window.setTimeout(async () => {
      try {
        const found = await jellyfinApi.quickState(quickId)
        if (stopped) return
        if (found.done) {
          await action.run(async () => {
            await jellyfinApi.connectQuick(quickId)
            await onConnected()
          })
          return
        }
        setFlow((now) => (now.stage === 'code' && now.quick.id === quickId ? { ...now, left: found.left } : now))
      } catch (error) {
        if (stopped) return
        if (error instanceof ApiError && error.code === 'quick_expired') setFlow({ stage: 'expired' })
        else setFlow((now) => (now.stage === 'code' ? { ...now } : now))
      }
    }, quickTiming.poll)
    return () => {
      stopped = true
      window.clearTimeout(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [quickId, flow])

  const begin = () =>
    void action.run(async () => {
      const quick = await jellyfinApi.quick(url.trim())
      setFlow({ stage: 'code', quick, left: quick.seconds })
    })

  const cancelCode = () => {
    if (flow.stage === 'code') void jellyfinApi.quickCancel(flow.quick.id).catch(() => undefined)
    setFlow({ stage: 'address' })
  }

  const byKey = () =>
    void action.run(async () => {
      await jellyfinApi.connectByKey(url.trim(), key.trim())
      setKey('')
      await onConnected()
    })

  return (
    <Card id="jellyfin-new" title={t('jellyfin.title')} text={t('jellyfin.textNew')}>
      {(flow.stage === 'address' || flow.stage === 'key') && (
        <form
          className="space-y-1"
          onSubmit={(event) => {
            event.preventDefault()
            if (flow.stage === 'key') byKey()
            else begin()
          }}
        >
          <Input label={t('jellyfin.address')} value={url} onChange={setUrl} placeholder="http://jellyfin:8096" hint={t('jellyfin.addressHint')} maxLength={300} />
          {flow.stage === 'key' && (
            <>
              <p className="text-sm text-muted">{t('jellyfin.byKeyText')}</p>
              <Input label={t('jellyfin.key')} value={key} onChange={setKey} type="password" autoComplete="off" maxLength={200} />
            </>
          )}
          <div className="flex flex-wrap gap-2 pt-1">
            {flow.stage === 'address' ? (
              <>
                <Button primary type="submit" busy={action.busy} disabled={!url.trim()}>
                  {t('jellyfin.quick')}
                </Button>
                <Button ghost onClick={() => setFlow({ stage: 'key' })}>
                  {t('jellyfin.byKey')}
                </Button>
              </>
            ) : (
              <Button primary type="submit" busy={action.busy} disabled={!url.trim() || !key.trim()}>
                {t('jellyfin.connectByKey')}
              </Button>
            )}
            <Button ghost onClick={onCancel}>
              {t('common.cancel')}
            </Button>
          </div>
        </form>
      )}
      {flow.stage === 'code' && (
        <div className="space-y-3" data-testid="jellyfin-code">
          <p className="text-sm">{t('jellyfin.codeText')}</p>
          <p className="font-display text-3xl font-bold tracking-[0.3em]" aria-label={t('jellyfin.codeLabel')} data-testid="jellyfin-code-value">
            {flow.quick.code}
          </p>
          <p className="flex items-center gap-2 text-xs text-muted">
            <span className="spin small" aria-hidden />
            {t('jellyfin.codeLeft', { minutes: Math.max(1, Math.ceil(flow.left / 60)) })}
          </p>
          <div className="flex flex-wrap gap-2">
            <Button ghost onClick={cancelCode}>
              {t('common.cancel')}
            </Button>
          </div>
        </div>
      )}
      {flow.stage === 'expired' && (
        <div className="space-y-3">
          <p role="alert" className="alert !mt-0">
            {errorText('quick_expired')}
          </p>
          <Button primary onClick={begin} busy={action.busy}>
            {t('jellyfin.newCode')}
          </Button>
        </div>
      )}
      <Feedback problem={action.problem} done={action.done} values={action.values} />
    </Card>
  )
}
