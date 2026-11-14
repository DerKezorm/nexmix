/**
 * The page of an invitation's link (`/j/:code`), made for a phone: first the playlist and the name field ("Join"), then
 * the guest's view with three tabs (the list, the search, suggestions), and a friendly page when the invitation is over
 * or the guest was taken out. Everything goes through `/api/v1/join` and `/api/v1/guest` with the guest cookie; changes
 * of others come as events and the page reads its view anew. The covers come through the guest's own route.
 */
import { ChevronDown, ChevronUp, List, Search, X, Check, Plus } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useParams } from 'react-router-dom'

import { api, ApiError } from '../api/client'
import { guestApi, joinApi, type Door, type GuestEntry, type GuestState } from '../api/guests'
import { Avatar } from '../components/Avatar'
import { LogoMark } from '../components/Logo'
import { Cover, Mosaic } from '../components/Mosaic'
import { Spark } from '../components/Spark'
import { PlayButton } from '../components/PlayButton'
import type { TrackHit, TrackResult } from '../lib/catalogSearch'
import { browserSource, type CatalogSource } from '../lib/catalogSource'
import { CoverSource } from '../lib/coverUrl'
import { errorText } from '../lib/errors'
import { guestAsk } from '../lib/guestSuggest'
import { length } from '../lib/music'
import { useReasonText } from '../lib/reason'
import { onProblem, stop as stopPreview } from '../lib/preview'
import { iso } from '../lib/say'
import type { Suggestion } from '../lib/suggest'

type Over = 'ended' | 'removed' | 'unknown'
type Tab = 'list' | 'search' | 'suggest'

const codeOf = (error: unknown) => (error instanceof ApiError ? error.code : 'internal_error')
const NAME_LENGTH = 24
/** How long the sentence at the foot stays. */
const guestTiming = { toast: 4000, reconnect: 5000 }

function overOf(code: string): Over | null {
  if (code === 'guest_removed') return 'removed'
  if (code === 'invite_ended') return 'ended'
  if (code === 'invite_unknown' || code === 'guest_required') return 'unknown'
  return null
}

/** The search and the suggestions for a guest: the catalogue in this browser, or for a very large one the server's
 * search of tracks and its suggestions for this playlist. */
function guestSource(mode: 'browser' | 'server'): CatalogSource {
  if (mode === 'browser') return browserSource(guestApi.catalog)
  const none = async () => null
  return {
    mode: 'server',
    load: async () => ({ facets: { moods: [], groups: [], genres: [], decades: [], tracks: 0 }, changed: false }),
    tracks: async (query) => {
      const started = performance.now()
      const found = await api<TrackResult>(guestApi.search, { query: { q: query } })
      return { ...found, ms: performance.now() - started }
    },
    albums: async () => ({ total: 0, albums: [], ms: 0 }),
    artists: async () => ({ total: 0, artists: [], ms: 0 }),
    album: none,
    artist: none,
    // The server knows the playlist of the guest's invitation and seeds from it itself.
    suggest: async () => {
      const started = performance.now()
      const found = await api<{ tracks: Suggestion[] }>(guestApi.suggestions)
      return { tracks: found.tracks, ms: performance.now() - started }
    },
    // A guest does not fill by mood.
    build: async () => ({ tracks: [], duration: 0 }),
    dispose: () => undefined,
  }
}

function Frame({ children, top, tabs }: { children: ReactNode; top?: ReactNode; tabs?: ReactNode }) {
  return (
    <div className="phone-wrap">
      <div className="phone" data-testid="guest-phone">
        {top}
        <div className="ph-body soft-scroll">{children}</div>
        {tabs}
      </div>
    </div>
  )
}

function Brand() {
  return (
    <div className="flex items-center justify-center gap-2" style={{ marginTop: 20 }}>
      <LogoMark size={26} />
      <span className="wordmark" style={{ fontSize: 16 }}>
        NEX<span>MIX</span>
      </span>
    </div>
  )
}

function OverPage({ over }: { over: Over }) {
  const { t } = useTranslation()
  return (
    <Frame>
      <div className="join" data-testid="guest-over" data-over={over}>
        <Brand />
        <h2 style={{ marginTop: 40 }}>{t(`guest.over.${over}.title`)}</h2>
        <p>{t(`guest.over.${over}.text`)}</p>
      </div>
    </Frame>
  )
}

function JoinPage({ code, door, onJoined }: { code: string; door: Door; onJoined: () => void }) {
  const { t } = useTranslation()
  const [name, setName] = useState('')
  const [problem, setProblem] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const field = useRef<HTMLInputElement>(null)
  useEffect(() => field.current?.focus(), [])
  const join = async () => {
    const wanted = name.trim()
    if (!wanted) return field.current?.focus()
    setBusy(true)
    setProblem(null)
    try {
      await joinApi.join(code, wanted)
      onJoined()
    } catch (error) {
      const failed = codeOf(error)
      setProblem(failed === 'bad_name' ? t('guest.badName', { max: NAME_LENGTH }) : errorText(failed, error instanceof ApiError ? error.values : {}))
      setBusy(false)
    }
  }
  return (
    <CoverSource.Provider value={joinApi.cover(code)}>
      <Frame>
        <form
          className="join"
          data-testid="guest-join"
          onSubmit={(event) => {
            event.preventDefault()
            void join()
          }}
        >
          <Brand />
          <Mosaic covers={door.playlist.covers} size={200} />
          <h2 dir="auto">{door.playlist.name}</h2>
          <p>
            {door.inviter ? t('guest.invites', { name: iso(door.inviter) }) : t('guest.invitesSomeone')}
            <br />
            {t('playlists.meta', { count: door.playlist.count, length: length(door.playlist.duration) })}
          </p>
          <input ref={field} value={name} onChange={(event) => setName(event.target.value)} placeholder={t('guest.namePlaceholder')} aria-label={t('guest.namePlaceholder')} autoComplete="off" maxLength={NAME_LENGTH} dir="auto" />
          {problem && (
            <p role="alert" className="text-sm text-bad">
              {problem}
            </p>
          )}
          <button type="submit" className="btn primary" disabled={busy}>
            {t('guest.join')}
          </button>
          <p style={{ fontSize: 12, marginTop: 14 }}>{t('guest.privacy')}</p>
        </form>
      </Frame>
    </CoverSource.Provider>
  )
}

type Toast = { text: string; undo?: () => Promise<void> }

/** A person of the household without a display name: guests never see the name to sign in with. */
function Someone() {
  const { t } = useTranslation()
  return <>{t('guest.someone')}</>
}

function Row({ entry, title, artist, by, children }: { entry: { album_id: number | null; cover: boolean }; title: string; artist: string; by?: string; children: ReactNode }) {
  return (
    <div className="row">
      <Cover album={entry.album_id} has={entry.cover} />
      <div className="min-w-0">
        <div className="t" dir="auto">
          {title}
        </div>
        <div className="s" dir="auto">
          {artist}
        </div>
        {by !== undefined && (
          <div className="s by" dir="auto">
            <Avatar person={{ id: 0, name: by || '?', avatar: null }} size={20} />
            {by || <Someone />}
          </div>
        )}
      </div>
      <div className="acts">{children}</div>
    </div>
  )
}

function GuestView({ state, reload, setState, onOver }: { state: GuestState; reload: () => Promise<void>; setState: (state: GuestState) => void; onOver: (over: Over) => void }) {
  const { t } = useTranslation()
  const reasonText = useReasonText()
  const [tab, setTab] = useState<Tab>('list')
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<TrackHit[]>([])
  const [ideas, setIdeas] = useState<Suggestion[] | null>(null)
  const [source, setSource] = useState<CatalogSource | null>(null)
  const [loaded, setLoaded] = useState(0)
  const [failed, setFailed] = useState<string | null>(null)
  const [toast, setToast] = useState<Toast | null>(null)
  const [busy, setBusy] = useState(false)
  const search = useRef<HTMLInputElement>(null)
  const mode = state.catalog.ready ? state.catalog.search : null
  const { me, invite, playlist } = state
  const full = me.count >= invite.max

  // The catalogue for the search and the suggestions, once.
  useEffect(() => {
    if (mode === null) return
    const made = guestSource(mode)
    setSource(made)
    let gone = false
    made.load().then(
      () => !gone && setLoaded((value) => value + 1),
      (error) => !gone && setFailed(codeOf(error)),
    )
    return () => {
      gone = true
      made.dispose()
    }
  }, [mode])

  // Listening in: one preview at a time on the page (as in the editor); why one did not play, said at the foot.
  useEffect(() => {
    const off = onProblem((code, values) => {
      const minutes = Math.max(1, Math.ceil(Number(values?.retry_after ?? 60) / 60))
      setToast({ text: code === 'preview_too_often' ? t('preview.tooOften', { minutes }) : t('guest.listenFailed') })
    })
    return () => {
      off()
      stopPreview()
    }
  }, [t])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(null), guestTiming.toast)
    return () => window.clearTimeout(timer)
  }, [toast])

  useEffect(() => {
    if (tab !== 'search' || !source || !loaded) return
    let gone = false
    source.tracks(query, {}).then(
      (found) => !gone && setHits(found.tracks.slice(0, 40)),
      () => undefined,
    )
    return () => {
      gone = true
    }
  }, [tab, query, source, loaded])

  const tracks = useMemo(() => playlist.items.map((entry) => entry.track), [playlist.items])
  const asked = useMemo(() => guestAsk(playlist.items), [playlist.items])
  useEffect(() => {
    if (tab !== 'suggest' || !source || !loaded) return
    let gone = false
    source.suggest(asked).then(
      (found) => !gone && setIdeas(found.tracks),
      () => !gone && setIdeas([]),
    )
    return () => {
      gone = true
    }
  }, [tab, source, loaded, asked])

  useEffect(() => {
    if (tab === 'search') search.current?.focus()
  }, [tab])

  const songs = useMemo(() => new Set(playlist.items.map((entry) => entry.song).filter((song): song is number => song !== null)), [playlist.items])

  const run = async (work: () => Promise<{ state: GuestState; op: string }>, done?: (state: GuestState) => Toast | null) => {
    if (busy) return
    setBusy(true)
    try {
      const change = await work()
      setState(change.state)
      const said = done?.(change.state)
      if (said && change.op !== 'none') setToast(said)
    } catch (error) {
      const failed = codeOf(error)
      const over = overOf(failed)
      if (over) onOver(over)
      else {
        setToast({ text: errorText(failed, error instanceof ApiError ? error.values : {}) })
        void reload()
      }
    } finally {
      setBusy(false)
    }
  }

  const add = (hit: { id: number; title: string }) => run(() => guestApi.add([hit.id]), () => ({ text: t('guest.added', { title: iso(hit.title) }) }))

  const remove = (entry: GuestEntry, index: number) => {
    const after = index > 0 ? (playlist.items[index - 1]?.id ?? null) : null
    void run(
      () => guestApi.remove([entry.id]),
      // Bringing back is adding again: only with the right to add.
      () => ({ text: t('guest.removed', { title: iso(entry.title) }), undo: invite.may_add ? async () => run(() => guestApi.bringBack([entry.id], after)) : undefined }),
    )
  }

  const move = (index: number, by: number) => {
    const items = playlist.items
    const target = index + by
    if (target < 0 || target >= items.length) return
    const rest = items.filter((_, at) => at !== index)
    const after = target === 0 ? null : rest[target - 1].id
    void run(() => guestApi.move(items[index].id, after))
  }

  const addButton = (hit: TrackHit) =>
    tracks.includes(hit.id) || songs.has(hit.song) ? (
      <span className="round done" aria-label={t('guest.in')} title={t('guest.in')}>
        <Check aria-hidden />
      </span>
    ) : invite.may_add ? (
      <button type="button" className="round add" disabled={full || busy} aria-label={t('guest.add', { title: hit.title })} title={full ? t('guest.full') : t('guest.addShort')} onClick={() => void add(hit)}>
        <Plus aria-hidden />
      </button>
    ) : null

  let body: ReactNode
  if (tab === 'list') {
    body = (
      <>
        {invite.may_add && (
          <div className="quota" data-testid="guest-quota">
            {t('guest.quota', { count: me.count, max: invite.max })}
            <span className="quota-bar">
              <i style={{ width: `${Math.min(100, (me.count / Math.max(1, invite.max)) * 100)}%` }} />
            </span>
          </div>
        )}
        {playlist.items.length === 0 && <div className="empty">{t('guest.empty')}</div>}
        <div data-testid="guest-list">
          {playlist.items.map((entry, index) => (
            <Row key={entry.id} entry={entry} title={entry.title} artist={entry.artist} by={entry.by}>
              {invite.preview && !entry.missing && <PlayButton track={entry.track} title={entry.title} />}
              {invite.may_sort && (
                <>
                  <button type="button" className="round" disabled={busy || index === 0} aria-label={t('guest.up', { title: entry.title })} onClick={() => move(index, -1)}>
                    <ChevronUp aria-hidden />
                  </button>
                  <button type="button" className="round" disabled={busy || index === playlist.items.length - 1} aria-label={t('guest.down', { title: entry.title })} onClick={() => move(index, 1)}>
                    <ChevronDown aria-hidden />
                  </button>
                </>
              )}
              {(entry.mine || invite.may_sort) && (
                <button type="button" className="round" disabled={busy} aria-label={t('guest.remove', { title: entry.title })} onClick={() => remove(entry, index)}>
                  <X aria-hidden />
                </button>
              )}
            </Row>
          ))}
        </div>
      </>
    )
  } else if (tab === 'search') {
    body = (
      <>
        <div className="searchbox" style={{ marginTop: 0 }}>
          <Search aria-hidden />
          <input ref={search} type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t('guest.searchPlaceholder')} aria-label={t('guest.searchPlaceholder')} autoComplete="off" />
        </div>
        {failed && <div className="empty">{errorText(failed)}</div>}
        {!failed && !loaded && <div className="empty">{t('editor.catalogLoading')}</div>}
        {loaded > 0 && hits.length === 0 && <div className="empty">{t('editor.nothing')}</div>}
        <div data-testid="guest-hits">
          {hits.map((hit) => (
            <Row key={hit.id} entry={{ album_id: hit.album_id, cover: hit.cover }} title={hit.title} artist={hit.artist}>
              {invite.preview && <PlayButton track={hit.id} title={hit.title} />}
              {addButton(hit)}
            </Row>
          ))}
        </div>
      </>
    )
  } else {
    body = (
      <>
        <div className="group-title">{t('guest.fits')}</div>
        {ideas !== null && ideas.length === 0 && <div className="empty">{t('guest.noSuggestions')}</div>}
        <div data-testid="guest-ideas">
          {(ideas ?? []).map((idea) => (
            <div className="row" key={idea.id}>
              <Cover album={idea.album_id} has={idea.cover} />
              <div className="min-w-0">
                <div className="t" dir="auto">
                  {idea.title}
                </div>
                <div className="s" dir="auto">
                  {idea.artist}
                </div>
                {reasonText(idea.reason) && <div className="why">{reasonText(idea.reason)}</div>}
              </div>
              <div className="acts">
                {invite.preview && <PlayButton track={idea.id} title={idea.title} />}
                {addButton(idea)}
              </div>
            </div>
          ))}
        </div>
      </>
    )
  }

  const tabs: [Tab, string, ReactNode][] = [
    ['list', t('guest.tabList'), <List key="l" aria-hidden />],
    ['search', t('guest.tabSearch'), <Search key="s" aria-hidden />],
    ['suggest', t('guest.tabSuggest'), <Spark key="i" size={22} />],
  ]
  return (
    <CoverSource.Provider value={guestApi.cover}>
      <Frame
        top={
          <div className="ph-top">
            <Avatar person={{ id: 0, name: me.name, avatar: null }} size={26} />
            <h2 dir="auto" data-testid="guest-title">
              {playlist.name}
            </h2>
          </div>
        }
        tabs={
          <nav className="ph-tabs" aria-label={t('guest.tabs')}>
            {tabs.map(([key, label, icon]) => (
              <button key={key} type="button" className={tab === key ? 'on' : ''} aria-current={tab === key ? 'page' : undefined} onClick={() => setTab(key)}>
                {icon}
                {label}
              </button>
            ))}
          </nav>
        }
      >
        {body}
      </Frame>
      {toast && (
        <div className="toast over-tabs" role="status" data-testid="toast">
          <span>{toast.text}</span>
          {toast.undo && (
            <button
              type="button"
              onClick={() => {
                const undo = toast.undo
                setToast(null)
                void undo?.()
              }}
            >
              {t('editor.undo')}
            </button>
          )}
        </div>
      )}
    </CoverSource.Provider>
  )
}

export function GuestPage() {
  const { t } = useTranslation()
  const code = useParams().code ?? ''
  const [door, setDoor] = useState<Door | null>(null)
  const [state, setState] = useState<GuestState | null>(null)
  const [over, setOver] = useState<Over | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const version = useRef(0)

  const take = useCallback((next: GuestState) => {
    version.current = next.playlist.version
    setState(next)
  }, [])

  const reload = useCallback(async () => {
    try {
      take(await guestApi.state())
    } catch (error) {
      setOver(overOf(codeOf(error)) ?? 'unknown')
    }
  }, [take])

  useEffect(() => {
    let gone = false
    joinApi.look(code).then(
      (found) => {
        if (gone) return
        if (found.joined) void reload()
        else setDoor(found)
      },
      async (error) => {
        if (gone) return
        const failed = codeOf(error)
        if (failed === 'invite_unknown') {
          // The invitation is over: this browser's guest, if it was one, hears why.
          try {
            await guestApi.state()
            setOver('unknown')
          } catch (inner) {
            setOver(overOf(codeOf(inner)) ?? 'unknown')
          }
        } else if (overOf(failed)) setOver(overOf(failed))
        else setProblem(errorText(failed, error instanceof ApiError ? error.values : {}))
      },
    )
    return () => {
      gone = true
    }
  }, [code, reload])

  // The changes of others, the end of the invitation, being taken out.
  const joined = state !== null
  useEffect(() => {
    if (!joined || typeof EventSource === 'undefined') return
    let source: EventSource | null = null
    let timer = 0
    let closed = false
    const connect = () => {
      if (closed) return
      source = new EventSource(guestApi.events)
      const fresh = (event: Event) => {
        let data: { version?: number }
        try {
          data = JSON.parse((event as MessageEvent).data)
        } catch {
          data = {}
        }
        if (data.version === undefined || data.version !== version.current) void reload()
      }
      source.addEventListener('hello', fresh)
      source.addEventListener('items', fresh)
      source.addEventListener('playlist', fresh)
      source.addEventListener('invite', () => void reload())
      source.addEventListener('gone', () => {
        closed = true
        source?.close()
        void reload()
      })
      source.onerror = () => {
        if (source?.readyState === EventSource.CLOSED) {
          window.clearTimeout(timer)
          timer = window.setTimeout(() => {
            void reload()
            connect()
          }, guestTiming.reconnect)
        }
      }
    }
    connect()
    return () => {
      closed = true
      window.clearTimeout(timer)
      source?.close()
    }
  }, [joined, reload])

  if (over) return <OverPage over={over} />
  if (problem)
    return (
      <Frame>
        <div className="join">
          <Brand />
          <p role="alert" style={{ marginTop: 40 }}>
            {problem}
          </p>
          <button type="button" className="btn" onClick={() => window.location.reload()}>
            {t('guest.again')}
          </button>
        </div>
      </Frame>
    )
  if (state) return <GuestView state={state} reload={reload} setState={take} onOver={setOver} />
  if (door) return <JoinPage code={code} door={door} onJoined={() => void reload()} />
  return null
}
