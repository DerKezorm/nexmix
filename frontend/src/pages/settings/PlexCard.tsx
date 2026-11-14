/**
 * Settings, Server: the Plex card. Three steps with their ticks (signed in through plex.tv, the server, the library),
 * then the catalogue: its counts, when it was last synced, how the search works, the moods, the time of the nightly
 * whole sync, how the playlists stand in Plex (with "sync playlists now"), the buttons to sync now, to choose another
 * library and to disconnect (both ask first and say what goes), the switch "allow listening in", and the note about
 * Plex's sonic analysis as it stands.
 *
 * Signing in: "Connect with Plex" opens Plex's own page in a new window and asks the server every two seconds whether
 * the operator agreed there (for ten minutes at most); then the servers with music, then the library. The address and
 * the token can be given by hand instead. The token never comes back to the browser.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError, api } from '../../api/client'
import { busy, catalogApi, plexApi, type CatalogStatus, type PinStart, type PlaylistSync, type PlexLibrary, type PlexServerChoice, type PlexState } from '../../api/plex'
import { useConfirm } from '../../components/useConfirm'
import { percentOf, useCatalogStatus } from '../../lib/catalogStatus'
import { errorText } from '../../lib/errors'
import { iso } from '../../lib/say'
import { Button, Card, Feedback, Input, Toggle, useAction } from './ui'

/** How long to wait between two asks for the PIN; the tests shorten it. */
// eslint-disable-next-line react-refresh/only-export-components
export const pinTiming = { poll: 2000 }
const WINDOW = 'nexmix-plex'

type Flow =
  | { stage: 'idle' }
  | { stage: 'pin'; pin: PinStart; blocked: boolean; left: number }
  | { stage: 'expired' }
  | { stage: 'servers'; pinId: string; servers: PlexServerChoice[]; offered: number }
  | { stage: 'libraries'; libraries: PlexLibrary[] }
  | { stage: 'byHand' }

type Step = { label: string; state: 'done' | 'now' | 'todo' }

function Steps({ steps }: { steps: Step[] }) {
  return (
    <div className="steps" data-testid="plex-steps">
      {steps.map((step, index) => (
        <span key={step.label} className="contents">
          {index > 0 && (
            <span className="sep" aria-hidden>
              ›
            </span>
          )}
          <span className={`st ${step.state === 'done' ? '' : step.state}`} aria-current={step.state === 'now' ? 'step' : undefined}>
            <i aria-hidden>{step.state === 'done' ? '✓' : index + 1}</i>
            {step.label}
          </span>
        </span>
      ))}
    </div>
  )
}

/** Opens Plex's page for the PIN: a window opened at the click (a browser opens none later on), sent to the page
 * once the PIN is there. Nothing in it may reach back into this tab. */
function openWindow(): Window | null {
  try {
    const opened = window.open('', WINDOW, 'popup,width=820,height=760')
    if (opened) opened.opener = null
    return opened ?? null
  } catch {
    return null
  }
}

function when(moment: string | null, language: string): string {
  if (!moment) return ''
  try {
    return new Intl.DateTimeFormat(language, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(moment))
  } catch {
    return moment
  }
}

export function PlexCard() {
  const { t, i18n } = useTranslation()
  const [plex, setPlex] = useState<PlexState | null>(null)
  const { status, refresh } = useCatalogStatus()
  const [flow, setFlow] = useState<Flow>({ stage: 'idle' })
  const [url, setUrl] = useState('')
  const [token, setToken] = useState('')
  const action = useAction()
  const { ask, dialog } = useConfirm()
  const opened = useRef<Window | null>(null)

  const load = useCallback(async () => {
    try {
      setPlex(await plexApi.state())
    } catch {
      setPlex({ connected: false })
    }
  }, [])
  useEffect(() => {
    void load()
  }, [load])

  // While the operator is at Plex: asked every two seconds whether the token is there.
  const pinId = flow.stage === 'pin' ? flow.pin.id : null
  useEffect(() => {
    if (!pinId) return
    let stopped = false
    const timer = window.setTimeout(async () => {
      try {
        const found = await plexApi.poll(pinId)
        if (stopped) return
        if (found.done) {
          setFlow({ stage: 'servers', pinId, servers: found.servers, offered: found.offered })
          opened.current = null
        } else setFlow((now) => (now.stage === 'pin' && now.pin.id === pinId ? { ...now, left: found.left } : now))
      } catch (error) {
        if (stopped) return
        if (error instanceof ApiError && error.code === 'pin_expired') setFlow({ stage: 'expired' })
        else setFlow((now) => (now.stage === 'pin' ? { ...now } : now))
      }
    }, pinTiming.poll)
    return () => {
      stopped = true
      window.clearTimeout(timer)
    }
  }, [pinId, flow])

  const begin = () =>
    void action.run(async () => {
      const target = openWindow()
      opened.current = target
      try {
        const pin = await plexApi.begin()
        if (target) target.location.href = pin.url
        setFlow({ stage: 'pin', pin, blocked: !target, left: pin.seconds })
      } catch (error) {
        target?.close()
        throw error
      }
    })

  const afterConnect = async (answer: PlexState & { libraries: PlexLibrary[] }) => {
    setPlex(answer)
    if (answer.connected && !answer.library) setFlow({ stage: 'libraries', libraries: answer.libraries })
    else setFlow({ stage: 'idle' })
    await refresh()
  }

  const chooseServer = (index: number) =>
    void action.run(async () => {
      if (flow.stage !== 'servers') return
      await afterConnect(await plexApi.connectChosen(flow.pinId, index))
    })

  const connectByHand = () =>
    void action.run(async () => {
      await afterConnect(await plexApi.connectByHand(url.trim(), token.trim()))
      setToken('')
    })

  const showLibraries = () =>
    void action.run(async () => {
      const { libraries } = await plexApi.libraries()
      setFlow({ stage: 'libraries', libraries })
    })

  const library = plex?.connected ? plex.library : null
  const tracks = status?.counts.tracks ?? 0

  const chooseLibrary = (choice: PlexLibrary) => {
    const go = async () => {
      setPlex(await plexApi.chooseLibrary(choice.key))
      setFlow({ stage: 'idle' })
      await refresh()
    }
    if (library && library.key !== choice.key) {
      ask({
        title: t('plex.libraryTitle'),
        text: t('plex.libraryText', { old: iso(library.title), new: iso(choice.title) }),
        confirm: t('plex.libraryConfirm'),
        run: go,
      })
      return
    }
    void action.run(go)
  }

  const disconnect = () =>
    ask({
      title: t('plex.disconnectTitle'),
      text: t('plex.disconnectText', { tracks: tracks.toLocaleString(i18n.language) }),
      confirm: t('plex.disconnect'),
      run: async () => {
        await plexApi.disconnect()
        setPlex({ connected: false })
        setFlow({ stage: 'idle' })
        await refresh()
      },
    })

  const syncNow = () =>
    void action.run(async () => {
      await catalogApi.sync()
      await refresh()
    }, t('plex.syncStarted'))

  if (!plex) return null
  const connected = plex.connected
  const viaHand = (connected && plex.via === 'manual') || flow.stage === 'byHand'
  const signedIn = connected || flow.stage === 'servers' || flow.stage === 'libraries'
  const steps: Step[] = [
    { label: viaHand ? t('plex.stepByHand') : t('plex.stepSignedIn'), state: signedIn ? 'done' : flow.stage === 'pin' || flow.stage === 'byHand' ? 'now' : 'todo' },
    {
      label: connected ? t('plex.stepServerNamed', { name: iso(plex.server_name) }) : t('plex.stepServer'),
      state: connected ? 'done' : flow.stage === 'servers' ? 'now' : 'todo',
    },
    {
      label: library ? t('plex.stepLibraryNamed', { name: iso(library.title) }) : t('plex.stepLibrary'),
      state: library ? 'done' : flow.stage === 'libraries' ? 'now' : 'todo',
    },
  ]
  const text = connected ? (plex.via === 'manual' ? t('plex.textByHand') : t('plex.textConnected')) : t('plex.textNew')

  return (
    <Card id="plex" title={t('plex.title')} text={text}>
      <Steps steps={steps} />
      {!connected && flow.stage === 'idle' && (
        <div className="flex flex-wrap gap-2 pt-1">
          <Button primary onClick={begin} busy={action.busy}>
            {t('plex.connect')}
          </Button>
          <Button ghost onClick={() => setFlow({ stage: 'byHand' })}>
            {t('plex.byHand')}
          </Button>
        </div>
      )}
      {flow.stage === 'pin' && (
        <Waiting
          flow={flow}
          onCancel={() => {
            // What the sign-in found is forgotten at the server at once, not after ten minutes.
            void plexApi.cancel(flow.pin.id).catch(() => undefined)
            setFlow({ stage: 'idle' })
          }}
        />
      )}
      {flow.stage === 'expired' && (
        <div className="space-y-3">
          <p role="alert" className="alert !mt-0">
            {t('plex.expired')}
          </p>
          <Button primary onClick={begin} busy={action.busy}>
            {t('plex.connect')}
          </Button>
        </div>
      )}
      {flow.stage === 'servers' && (
        <div data-testid="plex-servers">
          <p className="text-sm font-semibold">{t('plex.chooseServer')}</p>
          {flow.servers.length === 0 && <p className="alert">{t('plex.noServers')}</p>}
          {flow.servers.map((server) => (
            <div key={server.index} className="pick">
              <div className="grow">
                <b>{server.name}</b>
                <small>
                  {server.owned ? t('plex.own') : t('plex.shared')} · {t('plex.libraryCount', { count: server.libraries.length })}
                </small>
              </div>
              <Button onClick={() => chooseServer(server.index)} busy={action.busy} label={`${t('plex.choose')}: ${server.name}`}>
                {t('plex.choose')}
              </Button>
            </div>
          ))}
        </div>
      )}
      {flow.stage === 'libraries' && (
        <div data-testid="plex-libraries">
          <p className="text-sm font-semibold">{t('plex.chooseLibrary')}</p>
          {flow.libraries.map((one) => (
            <div key={one.key} className="pick">
              <div className="grow">
                <b>{one.title}</b>
              </div>
              {library?.key === one.key ? (
                <span className="chip static on">{t('plex.current')}</span>
              ) : (
                <Button onClick={() => chooseLibrary(one)} busy={action.busy} label={`${t('plex.choose')}: ${one.title}`}>
                  {t('plex.choose')}
                </Button>
              )}
            </div>
          ))}
          {connected && library && (
            <div className="pt-2">
              <Button ghost onClick={() => setFlow({ stage: 'idle' })}>
                {t('common.cancel')}
              </Button>
            </div>
          )}
        </div>
      )}
      {flow.stage === 'byHand' && (
        <form
          className="space-y-1"
          onSubmit={(event) => {
            event.preventDefault()
            connectByHand()
          }}
        >
          <p className="text-sm text-muted">{t('plex.byHandText')}</p>
          <Input label={t('plex.address')} value={url} onChange={setUrl} placeholder="http://plex:32400" hint={t('plex.addressHint')} maxLength={300} />
          <Input label={t('plex.token')} value={token} onChange={setToken} type="password" autoComplete="off" maxLength={300} />
          <div className="flex flex-wrap gap-2 pt-1">
            <Button primary type="submit" busy={action.busy} disabled={!url.trim() || !token.trim()}>
              {t('plex.connectByHand')}
            </Button>
            <Button ghost onClick={() => setFlow({ stage: 'idle' })}>
              {t('common.cancel')}
            </Button>
          </div>
        </form>
      )}
      {connected && library && flow.stage === 'idle' && (
        <Connected status={status} onSync={syncNow} onLibraries={showLibraries} onDisconnect={disconnect} working={action.busy} />
      )}
      <Feedback problem={action.problem} done={action.done} values={action.values} />
      {dialog}
    </Card>
  )
}

function Waiting({ flow, onCancel }: { flow: Extract<Flow, { stage: 'pin' }>; onCancel: () => void }) {
  const { t } = useTranslation()
  // Plex's page in a window of its own that cannot reach back into this tab.
  const again = () => void window.open(flow.pin.url, '_blank', 'noopener,noreferrer,popup,width=820,height=760')
  return (
    <div className="space-y-3" data-testid="plex-waiting">
      <p className="flex items-center gap-2 text-sm">
        <span className="spin small" aria-hidden />
        <span>{t('plex.waiting')}</span>
      </p>
      {flow.blocked && (
        <p className="text-sm">
          {t('plex.blocked')}{' '}
          <a href={flow.pin.url} target="_blank" rel="noopener noreferrer" className="font-semibold text-accent underline">
            {t('plex.openPlex')}
          </a>
        </p>
      )}
      <p className="text-xs text-muted">{t('plex.waitingLeft', { minutes: Math.max(1, Math.ceil(flow.left / 60)) })}</p>
      <div className="flex flex-wrap gap-2">
        <Button onClick={again}>{t('plex.openAgain')}</Button>
        <Button ghost onClick={onCancel}>
          {t('common.cancel')}
        </Button>
      </div>
    </div>
  )
}

function Connected({ status, onSync, onLibraries, onDisconnect, working }: {
  status: CatalogStatus | null
  onSync: () => void
  onLibraries: () => void
  onDisconnect: () => void
  working: boolean
}) {
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
    <div data-testid="plex-connected">
      <dl className="kv" style={{ marginTop: 14 }}>
        <dt>{t('plex.catalog')}</dt>
        <dd data-testid="plex-catalog">
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
        <dd>{t('plex.searchValue')}</dd>
        <dt>{t('plex.moods')}</dt>
        <dd>{t('plex.moodsValue', { count: counts?.moods ?? 0 })}</dd>
        <dt>{t('plex.nightly')}</dt>
        <dd>
          <Nightly value={status?.nightly ?? '03:30'} />
        </dd>
        <Playlists />
      </dl>
      {failed && (
        <p role="alert" className="alert" data-testid="plex-failed">
          {t('plex.failed', { reason: errorText(failed.error ?? 'internal_error') })}
          {status?.ready && status.synced_at && <> {t('plex.kept', { when: when(status.synced_at, i18n.language) })}</>}
        </p>
      )}
      <div className="flex flex-wrap gap-2" style={{ marginTop: 14 }}>
        <Button onClick={onSync} busy={working} disabled={busy(status) && full}>
          {t('plex.syncNow')}
        </Button>
        <Button ghost onClick={onLibraries} disabled={working}>
          {t('plex.otherLibrary')}
        </Button>
        <Button ghost onClick={onDisconnect} disabled={working}>
          {t('plex.disconnect')}
        </Button>
      </div>
      <PreviewSwitch />
      <Analysis status={status} />
    </div>
  )
}

/** How the playlists stand in Plex: how many are saved there, waiting or failed, the smart ones, the last look. */
function Playlists() {
  const { t, i18n } = useTranslation()
  const [found, setFound] = useState<PlaylistSync | null>(null)
  const [started, setStarted] = useState(false)
  const load = useCallback(async () => {
    try {
      setFound(await plexApi.playlists())
    } catch {
      setFound(null)
    }
  }, [])
  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(), 15000)
    return () => window.clearInterval(timer)
  }, [load])
  if (!found || typeof found.playlists !== 'number') return null
  const parts = [t('sync.plex.counts', { synced: found.synced, count: found.playlists })]
  if (found.waiting) parts.push(t('sync.plex.waiting', { count: found.waiting }))
  if (found.error) parts.push(t('sync.plex.error', { count: found.error }))
  if (found.smart) parts.push(t('sync.plex.smart', { count: found.smart }))
  const at = found.polled_at ? when(found.polled_at, i18n.language) : ''
  return (
    <>
      <dt>{t('sync.plex.title')}</dt>
      <dd data-testid="plex-playlists">
        {found.playlists ? parts.join(' · ') : t('sync.plex.none')}{' '}
        <button
          type="button"
          className="linkbtn"
          onClick={async () => {
            try {
              setFound(await plexApi.syncPlaylists())
              setStarted(true)
            } catch {
              setStarted(false)
            }
          }}
        >
          {t('sync.plex.syncNow')}
        </button>
        {started && <span className="text-xs text-muted"> {t('sync.plex.syncStarted')}</span>}
      </dd>
      <dt>{t('sync.plex.looked')}</dt>
      <dd data-testid="plex-playlists-looked">
        {!at ? t('sync.plex.notYet') : found.poll_error ? t('sync.plex.lookedFailed', { when: at, reason: errorText(found.poll_error) }) : t('sync.plex.lookedValue', { when: at, minutes: found.order_minutes ?? 2 })}
      </dd>
    </>
  )
}

function Nightly({ value }: { value: string }) {
  const { t } = useTranslation()
  const [time, setTime] = useState(value)
  const [saved, setSaved] = useState(value)
  /** What the last change came to: saved, or refused (the old time is back). */
  const [note, setNote] = useState<'saved' | 'failed' | null>(null)
  useEffect(() => {
    setTime(value)
    setSaved(value)
  }, [value])
  const save = async (next: string) => {
    if (next === saved || (next && !/^\d{2}:\d{2}$/.test(next))) return
    setNote(null)
    try {
      await api('/api/v1/settings', { method: 'PUT', body: { catalog_full_at: next } })
      setSaved(next)
      setNote('saved')
    } catch {
      setTime(saved)
      setNote('failed')
    }
  }
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      {t('plex.nightlyAt')}
      <input type="time" className="input time-input" value={time} aria-label={t('plex.nightlyLabel')} onChange={(event) => setTime(event.target.value)} onBlur={() => void save(time)} />
      {note === 'saved' && (
        <span role="status" className="text-xs font-semibold text-ok">
          ✓ {t('settings.saved')}
        </span>
      )}
      {note === 'failed' && (
        <span role="alert" className="text-xs text-bad">
          {t('plex.nightlyFailed')}
        </span>
      )}
    </span>
  )
}

/** "Allow listening in": a piece of a track, played through nexmix, for everybody who may see a playlist. */
function PreviewSwitch() {
  const { t } = useTranslation()
  const [on, setOn] = useState<boolean | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  useEffect(() => {
    let gone = false
    api<{ preview_allowed?: boolean }>('/api/v1/settings')
      .then((values) => !gone && setOn(values.preview_allowed !== false))
      .catch(() => !gone && setOn(null))
    return () => {
      gone = true
    }
  }, [])
  if (on === null) return null
  const change = async (next: boolean) => {
    setProblem(null)
    setOn(next)
    try {
      const saved = await api<{ preview_allowed: boolean }>('/api/v1/settings', { method: 'PUT', body: { preview_allowed: next } })
      setOn(saved.preview_allowed)
    } catch (error) {
      setOn(!next)
      setProblem(errorText(error instanceof ApiError ? error.code : 'internal_error'))
    }
  }
  return (
    <div className="mt-3" data-testid="preview-switch">
      <Toggle label={t('plex.previewAllowed')} hint={t('plex.previewAllowedHint')} checked={on} onChange={(next) => void change(next)} />
      {problem && (
        <p role="alert" className="text-sm text-bad">
          {problem}
        </p>
      )}
    </div>
  )
}

function Analysis({ status }: { status: CatalogStatus | null }) {
  const { t } = useTranslation()
  const state = status?.analysis.state
  if (!state || state === 'unknown') return null
  const [title, text] =
    state === 'off'
      ? [t('plex.analysisOff'), t('plex.analysisOffText')]
      : state === 'running'
        ? [t('plex.analysisRunning', { percent: status?.analysis.percent ?? 0 }), t('plex.analysisRunningText')]
        : [t('plex.analysisOn'), t('plex.analysisOnText')]
  return (
    <div className="note" data-testid="plex-analysis">
      <b>{title}</b>
      {text}
    </div>
  )
}
