/**
 * Settings, Server: a card per connected Music Assistant server, and the way to connect one.
 *
 * Connecting: the address and a long-lived token made in Music Assistant (profile › tokens). Then the providers of
 * its library whose tracks count; a provider that is a Plex or Jellyfin server nexmix knows directly too is named
 * (its tracks would stand twice). No token, no address and no id of Music Assistant comes back to the browser: a
 * provider is named by nexmix's own handle.
 *
 * Connected: the steps with their ticks, when the token runs out (a warning 30 days before, an alert once it ran out,
 * with "connect again"), the catalogue, that Music Assistant has no moods, how its playlists stand, and the buttons to
 * sync now, to choose other providers, to connect anew with a new token and to disconnect (asks first and says what
 * goes).
 */
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { busy, catalogApi } from '../../api/plex'
import { maApi, type MaProvider, type MaServer } from '../../api/servers'
import { useConfirm } from '../../components/useConfirm'
import { useCatalogStatus } from '../../lib/catalogStatus'
import { iso } from '../../lib/say'
import { ServerCatalog } from './JellyfinCard'
import { ServerPlaylists, Steps } from './PlexCard'
import { Button, Card, Feedback, Input, useAction } from './ui'

function day(moment: string | null, language: string): string {
  if (!moment) return ''
  try {
    return new Intl.DateTimeFormat(language, { dateStyle: 'medium' }).format(new Date(moment))
  } catch {
    return moment
  }
}

/** The providers to choose from, ticked as they are chosen; saved with one button. */
function ProviderChoice({ server, offered, doubled, onSaved, onCancel }: { server: MaServer; offered: MaProvider[]; doubled: string[]; onSaved: () => Promise<void>; onCancel?: () => void }) {
  const { t } = useTranslation()
  const [chosen, setChosen] = useState<string[]>(offered.filter((one) => one.chosen).map((one) => one.handle))
  const action = useAction()
  const { ask, dialog } = useConfirm()
  const save = () => {
    const go = async () => {
      await maApi.choose(server.id, chosen)
      await onSaved()
    }
    if (server.providers > 0) {
      ask({ title: t('ma.providersTitle'), text: t('ma.providersText'), confirm: t('ma.providersConfirm'), run: go })
      return
    }
    void action.run(go)
  }
  return (
    <div data-testid="ma-providers">
      <p className="text-sm font-semibold">{t('ma.chooseProviders')}</p>
      {offered.map((one) => (
        <label key={one.handle} className="pick cursor-pointer">
          <input
            type="checkbox"
            checked={chosen.includes(one.handle)}
            onChange={(event) =>
              setChosen((now) => (event.target.checked ? [...now, one.handle] : now.filter((handle) => handle !== one.handle)))
            }
            aria-label={one.name}
          />
          <div className="grow">
            <b>{one.name}</b>
            <small>{t('ma.providerKind', { kind: one.kind })}</small>
          </div>
        </label>
      ))}
      {doubled.length > 0 && (
        <p className="alert" role="note" data-testid="ma-doubled">
          {t('ma.doubled', { names: doubled.join(', ') })}
        </p>
      )}
      <div className="flex flex-wrap gap-2 pt-2">
        <Button primary onClick={save} busy={action.busy} disabled={!chosen.length}>
          {t('ma.saveProviders')}
        </Button>
        {onCancel && (
          <Button ghost onClick={onCancel}>
            {t('common.cancel')}
          </Button>
        )}
      </div>
      <Feedback problem={action.problem} done={action.done} values={action.values} />
      {dialog}
    </div>
  )
}

/** When the token runs out: the date, a warning from 30 days before, an alert once it ran out. */
function TokenLine({ server }: { server: MaServer }) {
  const { t, i18n } = useTranslation()
  const token = server.token
  if (!token.expires_at) return null
  if (token.expired)
    return (
      <p role="alert" className="alert" data-testid="ma-token">
        {t('ma.tokenExpired', { day: day(token.expires_at, i18n.language) })}
      </p>
    )
  if (token.warn)
    return (
      <p role="alert" className="alert" data-testid="ma-token">
        {t('ma.tokenSoon', { count: token.days_left ?? 0, day: day(token.expires_at, i18n.language) })}
      </p>
    )
  return null
}

export function MaCard({ server, onChanged }: { server: MaServer; onChanged: () => Promise<void> | void }) {
  const { t, i18n } = useTranslation()
  const { status, refresh } = useCatalogStatus(server.id)
  const [choosing, setChoosing] = useState<{ offered: MaProvider[]; doubled: string[] } | null>(null)
  const [renewing, setRenewing] = useState(false)
  const action = useAction()
  const { ask, dialog } = useConfirm()

  const open = useCallback(async () => {
    const found = await maApi.providers(server.id)
    setChoosing({ offered: found.providers ?? [], doubled: found.doubled ?? [] })
  }, [server.id])

  // Connected without a provider: the choice is offered at once.
  const missing = server.providers === 0
  useEffect(() => {
    if (missing) void open().catch(() => undefined)
  }, [missing, open])

  const disconnect = () =>
    ask({
      title: t('ma.disconnectTitle'),
      text: t('ma.disconnectText', { tracks: (status?.counts.tracks ?? 0).toLocaleString(i18n.language) }),
      confirm: t('ma.disconnect'),
      run: async () => {
        await maApi.disconnect(server.id)
        await onChanged()
      },
    })

  const syncNow = () =>
    void action.run(async () => {
      await catalogApi.sync(server.id)
      await refresh()
    }, t('plex.syncStarted'))

  const steps = [
    { label: t('ma.stepToken'), state: 'done' as const },
    { label: server.library ? t('ma.stepProvidersNamed', { names: iso(server.library) }) : t('ma.stepProviders'), state: server.library ? ('done' as const) : ('now' as const) },
  ]

  return (
    <Card id={`ma-${server.id}`} title={t('ma.title')} text={server.name && server.name !== 'Music Assistant' ? t('ma.textConnected', { name: iso(server.name) }) : t('ma.textConnectedPlain')}>
      <Steps steps={steps} />
      <TokenLine server={server} />
      {renewing && (
        <MaConnect
          renew
          onConnected={async () => {
            setRenewing(false)
            await onChanged()
          }}
          onCancel={() => setRenewing(false)}
        />
      )}
      {choosing && (
        <ProviderChoice
          server={server}
          offered={choosing.offered}
          doubled={choosing.doubled}
          onSaved={async () => {
            setChoosing(null)
            await onChanged()
            await refresh()
          }}
          onCancel={server.providers ? () => setChoosing(null) : undefined}
        />
      )}
      {server.library && !choosing && !renewing && (
        <div data-testid="ma-connected">
          <ServerCatalog
            kind="music_assistant"
            status={status}
            extra={
              <>
                <dt>{t('ma.token')}</dt>
                <dd data-testid="ma-token-until">{server.token.expires_at ? t('ma.tokenUntil', { day: day(server.token.expires_at, i18n.language) }) : t('ma.tokenOpen')}</dd>
                <dt>{t('plex.moods')}</dt>
                <dd>{t('ma.moodsValue')}</dd>
                <ServerPlaylists kind="music_assistant" load={() => maApi.playlists(server.id)} syncAll={() => maApi.syncPlaylists(server.id)} />
              </>
            }
          />
          {server.doubled.length > 0 && (
            <p className="alert" role="note" data-testid="ma-doubled">
              {t('ma.doubled', { names: server.doubled.join(', ') })}
            </p>
          )}
          <div className="flex flex-wrap gap-2" style={{ marginTop: 14 }}>
            <Button onClick={syncNow} busy={action.busy} disabled={busy(status) && (status?.running?.kind === 'full' || status?.queued === 'full')}>
              {t('plex.syncNow')}
            </Button>
            <Button ghost onClick={() => void action.run(open)} disabled={action.busy}>
              {t('ma.otherProviders')}
            </Button>
            <Button ghost onClick={() => setRenewing(true)} disabled={action.busy}>
              {t('ma.newToken')}
            </Button>
            <Button ghost onClick={disconnect} disabled={action.busy}>
              {t('ma.disconnect')}
            </Button>
          </div>
        </div>
      )}
      <Feedback problem={action.problem} done={action.done} values={action.values} />
      {dialog}
    </Card>
  )
}

/** Connecting a Music Assistant server (or the same one with a new token): its address and a long-lived token. */
export function MaConnect({ onConnected, onCancel, renew = false }: { onConnected: () => Promise<void> | void; onCancel: () => void; renew?: boolean }) {
  const { t } = useTranslation()
  const [url, setUrl] = useState('')
  const [token, setToken] = useState('')
  const action = useAction()
  const connect = () =>
    void action.run(async () => {
      await maApi.connect(url.trim(), token.trim())
      setToken('')
      await onConnected()
    })
  const form = (
    <form
      className="space-y-1"
      data-testid="ma-connect"
      onSubmit={(event) => {
        event.preventDefault()
        connect()
      }}
    >
      <Input label={t('ma.address')} value={url} onChange={setUrl} placeholder="http://music-assistant:8095" hint={t('ma.addressHint')} maxLength={300} />
      <Input label={t('ma.tokenLabel')} value={token} onChange={setToken} type="password" autoComplete="off" hint={t('ma.tokenHint')} maxLength={4096} />
      <div className="flex flex-wrap gap-2 pt-1">
        <Button primary type="submit" busy={action.busy} disabled={!url.trim() || !token.trim()}>
          {t('ma.connect')}
        </Button>
        <Button ghost onClick={onCancel}>
          {t('common.cancel')}
        </Button>
      </div>
      <Feedback problem={action.problem} done={action.done} values={action.values} />
    </form>
  )
  if (renew) return form
  return (
    <Card id="ma-new" title={t('ma.title')} text={t('ma.textNew')}>
      {form}
    </Card>
  )
}
