/**
 * One playlist: the search on the left, the playlist in the middle (head with mosaic, name, length, people, moods, "fill
 * by mood", and the entries), the suggestions on the right. Every track has ▶ for a preview, one at a time. Every change goes to the server as an operation and comes back with the new version; the changes of
 * others come as events (`/events`) and show with a short glow. A change the page missed (a version that is not the
 * next) loads the playlist anew. Adding and taking out can be undone from the sentence at the foot.
 *
 * The head says how the playlist stands in Plex (saved, being saved, waiting for Plex, failed with "try again"), with
 * "sync now"; a note says when it was sorted in Plex and here at the same time, with the undo that takes Plex's order.
 * A smart playlist of Plex is only shown.
 */
import { ArrowRightLeft, Copy, Share2, Trash2, Users } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'

import { ApiError } from '../api/client'
import type { Mirror } from '../api/mirrors'
import type { MediaServer } from '../api/servers'
import { applyChange, playlistsApi, type Change, type Entry, type Playlist, type PlexSync } from '../api/playlists'
import { Mosaic } from '../components/Mosaic'
import { People } from '../components/People'
import { Spark } from '../components/Spark'
import { serverName, SyncLine } from '../components/PlexSync'
import { useConfirm } from '../components/useConfirm'
import type { TrackHit } from '../lib/catalogSearch'
import { useCatalogSource } from '../lib/catalogSource'
import { useCatalogStatus } from '../lib/catalogStatus'
import { kindName, useServers } from '../lib/servers'
import { errorText } from '../lib/errors'
import { length, moodName } from '../lib/music'
import { editorTiming as timing } from '../lib/editorTiming'
import { newest } from '../lib/newest'
import { iso } from '../lib/say'
import { useAuth } from '../state/auth'
import { check as checkPreview, onProblem, stop as stopPreview } from '../lib/preview'
import { CopyDialog, type CopyMode } from './editor/CopyDialog'
import { MirrorChips, MirrorDialog, MirrorNotes, SourceChip } from './editor/MirrorStrip'
import { MoodDialog } from './editor/MoodDialog'
import { SearchColumn, type InList } from './editor/SearchColumn'
import { InviteDialog } from './editor/InviteDialog'
import { ShareDialog } from './editor/ShareDialog'
import { SuggestColumn, type Seed } from './editor/SuggestColumn'
import { TrackList } from './editor/TrackList'


type Toast = { text: string; undo?: () => Promise<void> }

/** Where a playlist is and where it could go: its mirrors, the servers it could move to (ready, keeping playlists, not
 * its source), the ones it could be copied to (not mirrored there already), and the name of its source. */
function placesOf(playlist: Playlist | null, servers: MediaServer[] | null, source: MediaServer | null) {
  const mirrors = playlist?.mirrors ?? []
  const elsewhere = (servers ?? []).filter((one) => one.active && one.ready && one.capabilities.playlists && one.id !== playlist?.server)
  const copyTargets = elsewhere.filter((one) => !mirrors.some((mirror) => mirror.server === one.id && mirror.state !== 'gone'))
  const sourceName = source ? source.name || kindName(source.kind) : serverName(playlist?.plex?.kind)
  return { mirrors, elsewhere, copyTargets, sourceName }
}

const codeOf = (error: unknown) => (error instanceof ApiError ? error.code : 'internal_error')

export function PlaylistEditor() {
  const { t, i18n } = useTranslation()
  const params = useParams()
  const playlistId = Number(params.id)
  const { me } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [playlist, setPlaylist] = useState<Playlist | null>(null)
  // The catalogue of the playlist's own server: asked once the playlist says which.
  const { status } = useCatalogStatus(playlist?.server, playlist === null)
  const catalog = useCatalogSource(status?.ready ? (status.search ?? 'browser') : null, playlist?.server)
  // What the playlist's server can do: a server without moods gets a word instead of an empty filter.
  const servers = useServers()
  const source = servers?.find((one) => one.id === playlist?.server) ?? null
  const noMoods = source && !source.capabilities.moods ? t('editor.noMoods', { kind: kindName(source.kind) }) : null
  const { mirrors, elsewhere, copyTargets, sourceName } = placesOf(playlist, servers, source)
  const [missing, setMissing] = useState<string | null>(null)
  const [fresh, setFresh] = useState<Set<number>>(new Set())
  const [toast, setToast] = useState<Toast | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [sharing, setSharing] = useState(false)
  const [inviting, setInviting] = useState(false)
  // Counts the guests that came or went (an event), so that an open invitation dialog reads its list anew.
  const [guestsSeen, setGuestsSeen] = useState(0)
  // The order of what tells the number of guests: events, the playlist read anew, the answers of the dialog.
  const [guestOrder] = useState(newest)
  const [focusEntry, setFocusEntry] = useState<number | null>(null)
  const [syncing, setSyncing] = useState(false)
  const [name, setName] = useState('')
  const naming = useRef(false)
  const nameInput = useRef<HTMLInputElement>(null)
  const list = useRef<HTMLDivElement>(null)
  const current = useRef<Playlist | null>(null)
  const { ask, dialog } = useConfirm()
  const [seed, setSeed] = useState<Seed | null>(null)
  const [filling, setFilling] = useState(false)
  const [copying, setCopying] = useState<CopyMode | null>(null)
  const [mirrorOpen, setMirrorOpen] = useState<number | null>(null)

  // Listening in: whether it is on, a word when a preview could not play, and silence when the page goes.
  useEffect(() => {
    void checkPreview()
    const off = onProblem((code, values) => {
      const minutes = Math.max(1, Math.ceil(Number(values?.retry_after ?? 60) / 60))
      setToast({ text: code === 'preview_off' ? t('preview.off') : code === 'preview_too_often' ? t('preview.tooOften', { minutes }) : t('preview.failed') })
    })
    return () => {
      off()
      stopPreview()
    }
  }, [t])

  const show = useCallback((next: Playlist) => {
    current.current = next
    setPlaylist(next)
    if (!naming.current) setName(next.name)
  }, [])

  const reload = useCallback(async () => {
    try {
      const ticket = guestOrder.ticket()
      const next = await playlistsApi.read(playlistId)
      // Guests counted before newer news of them (an event, the end of the invitation) keep the newer number.
      show(guestOrder.take(ticket) || !current.current ? next : { ...next, guests: current.current.guests })
      setMissing(null)
    } catch (error) {
      setMissing(codeOf(error))
    }
  }, [playlistId, show, guestOrder])

  useEffect(() => {
    void reload()
  }, [reload])

  // A new playlist starts with its name chosen, ready to be typed over.
  // Once: until the address has forgotten the mark, a change of the playlist (a live event, the answer to the new name)
  // would otherwise take the focus back from wherever it went in the meantime.
  const fresh_ = (location.state as { fresh?: boolean } | null)?.fresh
  const named = useRef(false)
  useEffect(() => {
    if (fresh_ && playlist && nameInput.current && !named.current) {
      named.current = true
      nameInput.current.focus()
      nameInput.current.select()
      navigate(location.pathname, { replace: true, state: null })
    }
  }, [fresh_, playlist, navigate, location.pathname])

  /** A change made here or by somebody else goes to the server in a moment: the head says so until the sync answers. */
  const pending = useCallback(() => {
    const now = current.current
    if (!now?.plex || now.plex.state === 'off' || now.plex.smart) return
    show({ ...now, plex: { ...now.plex, state: 'pending', error: null } })
  }, [show])

  const glow = useCallback((ids: number[]) => {
    if (!ids.length) return
    setFresh(new Set(ids))
    window.setTimeout(() => setFresh(new Set()), timing.glow)
  }, [])

  /** A change from the server (an answer or an event): the next version is applied, a gap loads anew. */
  const apply = useCallback(
    (change: Change, own: boolean) => {
      const now = current.current
      if (!now) return
      const next = applyChange(now, change)
      if (next === 'same') return
      if (next === null) {
        void reload()
        return
      }
      show(next)
      if (change.op !== 'sync') pending()
      const changed = change.op === 'move' && !own ? [(change as Change & { entry?: number }).entry ?? 0] : change.entries.map((entry) => entry.id)
      glow(changed.filter(Boolean))
    },
    [reload, show, glow, pending],
  )

  // The live events of this playlist.
  useEffect(() => {
    if (typeof EventSource === 'undefined' || !Number.isFinite(playlistId)) return
    let source: EventSource | null = null
    let timer = 0
    let closed = false
    const parse = (event: Event) => {
      try {
        return JSON.parse((event as MessageEvent).data) as Record<string, unknown>
      } catch {
        return {}
      }
    }
    const connect = () => {
      if (closed) return
      source = new EventSource(playlistsApi.events(playlistId))
      source.addEventListener('hello', (event) => {
        const data = parse(event)
        if (current.current && data.version !== current.current.version) void reload()
      })
      source.addEventListener('items', (event) => apply(parse(event) as unknown as Change, false))
      source.addEventListener('playlist', (event) => {
        const data = parse(event) as { op?: string; version?: number; name?: string }
        const now = current.current
        if (data.op === 'rename' && now && data.version === now.version + 1 && typeof data.name === 'string') {
          show({ ...now, name: data.name, version: data.version })
        } else if (data.op === 'deleted' || data.op === 'purged') {
          closed = true
          source?.close()
          navigate('/', { state: { notice: t('editor.goneTrash') } })
        } else if (now && data.version !== undefined && data.version > now.version) void reload()
      })
      source.addEventListener('access', () => void reload())
      source.addEventListener('guests', (event) => {
        const data = parse(event) as { guests?: number | null }
        if (guestOrder.take(guestOrder.ticket()) && current.current) show({ ...current.current, guests: data.guests ?? null })
        setGuestsSeen((value) => value + 1)
      })
      source.addEventListener('mirrors', (event) => {
        const now = current.current
        const data = parse(event) as { mirrors?: Mirror[] }
        if (now && Array.isArray(data.mirrors)) show({ ...now, mirrors: data.mirrors })
      })
      source.addEventListener('plex', (event) => {
        const now = current.current
        const data = parse(event) as unknown as PlexSync
        if (now && data && typeof data.state === 'string') show({ ...now, plex: data })
      })
      source.addEventListener('gone', () => {
        closed = true
        source?.close()
        navigate('/', { state: { notice: t('editor.goneShare') } })
      })
      source.onerror = () => {
        if (source?.readyState === EventSource.CLOSED) {
          window.clearTimeout(timer)
          timer = window.setTimeout(connect, 5000)
        }
      }
    }
    connect()
    return () => {
      closed = true
      window.clearTimeout(timer)
      source?.close()
    }
  }, [playlistId, apply, reload, show, navigate, t, guestOrder])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(null), timing.toast)
    return () => window.clearTimeout(timer)
  }, [toast])

  const run = useCallback(
    async (work: () => Promise<Change>): Promise<Change | null> => {
      setProblem(null)
      try {
        const change = await work()
        apply(change, true)
        return change
      } catch (error) {
        const code = codeOf(error)
        if (code === 'not_found') setMissing(code)
        else setProblem(errorText(code))
        return null
      }
    },
    [apply],
  )

  const inList: InList = useMemo(
    () => ({ songs: new Set((playlist?.items ?? []).map((entry) => entry.song).filter((song): song is number => song !== null)), tracks: new Set((playlist?.items ?? []).map((entry) => entry.track)) }),
    [playlist],
  )

  const add = useCallback(
    async (tracks: TrackHit[]) => {
      if (!tracks.length) {
        setToast({ text: t('editor.already') })
        return
      }
      const change = await run(() => playlistsApi.add(playlistId, tracks.map((track) => track.id)))
      if (!change) return
      if (change.op === 'none') {
        setToast({ text: t('editor.already') })
        return
      }
      const added = change.entries
      window.setTimeout(() => {
        const body = list.current
        if (body) body.scrollTop = body.scrollHeight
      }, 0)
      setToast({
        text: added.length === 1 ? t('editor.added', { title: iso(added[0].title) }) : t('editor.addedMany', { count: added.length }),
        undo: async () => {
          await run(() => playlistsApi.remove(playlistId, added.map((entry) => entry.id)))
        },
      })
    },
    [playlistId, run, t],
  )

  /** A whole album or "add all": every track whose song is not in the playlist yet, each song once. */
  const addAll = useCallback(
    (tracks: TrackHit[]) => {
      const songs = new Set(inList.songs)
      const chosen: TrackHit[] = []
      for (const track of tracks) {
        if (inList.tracks.has(track.id) || songs.has(track.song)) continue
        songs.add(track.song)
        chosen.push(track)
      }
      void add(chosen)
    },
    [add, inList],
  )

  const remove = useCallback(
    async (entry: Entry, index: number) => {
      const before = index > 0 ? (current.current?.items[index - 1]?.id ?? null) : null
      const change = await run(() => playlistsApi.remove(playlistId, [entry.id]))
      if (!change || change.op === 'none') return
      setToast({
        text: t('editor.removed', { title: iso(entry.title) }),
        undo: async () => {
          await run(() => playlistsApi.bringBack(playlistId, [entry.id], before))
        },
      })
    },
    [playlistId, run, t],
  )

  const move = useCallback(
    async (entry: Entry, after: number | null) => {
      setFocusEntry(entry.id)
      await run(() => playlistsApi.move(playlistId, entry.id, after))
    },
    [playlistId, run],
  )

  // The name: saved a moment after the last key, on leaving the field and on Enter.
  const saveName = useCallback(async () => {
    const now = current.current
    const wanted = name.trim()
    if (!now) return
    if (!wanted) {
      setName(now.name)
      return
    }
    if (wanted === now.name) return
    try {
      const answer = await playlistsApi.rename(playlistId, wanted)
      const latest = current.current
      if (latest && answer.version === latest.version + 1) show({ ...latest, name: answer.name, version: answer.version })
      else if (latest && answer.version > latest.version) void reload()
      pending()
    } catch (error) {
      setProblem(errorText(codeOf(error)))
    }
  }, [name, playlistId, show, reload, pending])

  /** "Sync now" and "try again": Plex is read at once and the changes written. */
  const syncNow = useCallback(async () => {
    setSyncing(true)
    try {
      const plex = await playlistsApi.sync(playlistId)
      const now = current.current
      if (now) show({ ...now, plex })
    } catch (error) {
      setProblem(errorText(codeOf(error)))
    } finally {
      setSyncing(false)
    }
  }, [playlistId, show])

  /** The note "sorted in Plex at the same time": undo takes Plex's order, keep drops the note. */
  const takePlexOrder = useCallback(async () => {
    const change = await run(() => playlistsApi.takePlexOrder(playlistId))
    const now = current.current
    if (change && now) {
      show({ ...now, plex: { ...now.plex, note: null } })
      setToast({ text: t('sync.noteUndone', { server: serverName(now.plex.kind) }) })
    }
  }, [playlistId, run, show, t])
  /** The note "many missing in the server at once": keep drops the note, take over takes them out here too (asked first). */
  const keepLoss = useCallback(async () => {
    try {
      const plex = await playlistsApi.keepLoss(playlistId)
      const now = current.current
      if (now) show({ ...now, plex })
    } catch (error) {
      setProblem(errorText(codeOf(error)))
    }
  }, [playlistId, show])
  const takeLoss = useCallback(async () => {
    const now = current.current
    if (!now) return
    const gone = now.items.filter((entry) => entry.plex_hint === 'missing')
    const first = now.items.findIndex((entry) => entry.plex_hint === 'missing')
    const before = first > 0 ? now.items[first - 1].id : null
    const change = await run(() => playlistsApi.takeLoss(playlistId))
    const latest = current.current
    if (latest) show({ ...latest, plex: { ...latest.plex, loss: null } })
    if (!change || change.op === 'none') return
    setToast({
      text: t('sync.lossRemoved'),
      undo: async () => {
        await run(() => playlistsApi.bringBack(playlistId, gone.map((entry) => entry.id), before))
      },
    })
  }, [playlistId, run, show, t])
  /** The note "changed in Music Assistant at the same time": seen, it goes. */
  const seenConcurrent = useCallback(async () => {
    try {
      const plex = await playlistsApi.seenConcurrent(playlistId)
      const now = current.current
      if (now) show({ ...now, plex })
    } catch (error) {
      setProblem(errorText(codeOf(error)))
    }
  }, [playlistId, show])
  const keepOrder = useCallback(async () => {
    try {
      const plex = await playlistsApi.keepOrder(playlistId)
      const now = current.current
      if (now) show({ ...now, plex })
    } catch (error) {
      setProblem(errorText(codeOf(error)))
    }
  }, [playlistId, show])
  useEffect(() => {
    if (!naming.current) return
    const timer = window.setTimeout(() => void saveName(), timing.rename)
    return () => window.clearTimeout(timer)
  }, [name, saveName])

  if (missing)
    return (
      <div className="view">
        <p className="empty" data-testid="playlist-missing">
          {missing === 'not_found' ? t('editor.notFound') : errorText(missing)} <Link to="/">{t('editor.toOverview')}</Link>
        </p>
      </div>
    )
  if (!playlist) return null
  const access = playlist.access
  const smart = Boolean(playlist.smart)
  const canEdit = !smart && (access === 'owner' || access === 'operator' || access === 'edit')
  const canManage = access === 'owner' || access === 'operator'
  const mine = playlist.owner?.id === me?.id
  const ownerName = playlist.owner ? playlist.owner.display_name || playlist.owner.name : ''

  return (
    <div className="editor" data-testid="editor">
      <SearchColumn noMoods={noMoods} source={catalog.source} facets={catalog.facets} version={catalog.version} failed={catalog.failed} inList={inList} canEdit={canEdit} onAdd={(hits) => void add(hits)} onAddAll={addAll} />
      <section className="col list" aria-label={t('editor.listArea')}>
        <div className="plhead" data-testid="playlist-head">
          <Mosaic covers={playlist.covers} size={80} />
          <div className="info">
            {canEdit ? (
              <input
                ref={nameInput}
                className="name"
                value={name}
                aria-label={t('editor.nameLabel')}
                maxLength={200}
                dir="auto"
                onFocus={() => (naming.current = true)}
                onChange={(event) => {
                  naming.current = true
                  setName(event.target.value)
                }}
                onBlur={() => {
                  naming.current = false
                  void saveName()
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') event.currentTarget.blur()
                }}
              />
            ) : (
              <h1 className="name" dir="auto">
                {playlist.name}
              </h1>
            )}
            <div className="stats">
              <span data-testid="playlist-meta">{t('playlists.meta', { count: playlist.count, length: length(playlist.duration) })}</span>
              <People people={playlist.people} />
              {!mine && ownerName && <span>{t('editor.from', { name: iso(ownerName) })}</span>}
              {access === 'read' && !smart && <span className="chip">{t('editor.readOnly')}</span>}
              {smart && (
                <span className="chip" data-testid="smart-chip">
                  {t('sync.smart')}
                </span>
              )}
              {playlist.plex && <SyncLine plex={playlist.plex} onNow={() => void syncNow()} onRetry={() => void syncNow()} working={syncing} canSync={access === 'owner' || access === 'operator' || access === 'edit'} />}
            </div>
            {mirrors.length > 0 && source && (
              <div className="moodbar" data-testid="server-badges">
                <SourceChip name={source.name} kind={source.kind} />
                <MirrorChips mirrors={mirrors} onOpen={setMirrorOpen} />
              </div>
            )}
            {playlist.moods.length > 0 && (
              <div className="moodbar">
                {playlist.moods.map((mood) => (
                  <span className="chip" key={mood}>
                    {moodName(mood)}
                  </span>
                ))}
              </div>
            )}
            {(canEdit || canManage) && (
              <div className="actions">
                {canEdit && (
                  <button type="button" className="btn" onClick={() => setFilling(true)}>
                    <Spark />
                    {t('mood.open')}
                  </button>
                )}
                {canEdit && (
                  <button type="button" className="btn" onClick={() => setInviting(true)} data-testid="invite-open">
                    <Users aria-hidden />
                    {t('guestInvite.title')}
                    {playlist.guests !== null && playlist.guests !== undefined && (
                      <span className="chip on" style={{ padding: '1px 7px' }} data-testid="guest-count">
                        {playlist.guests}
                      </span>
                    )}
                  </button>
                )}
                {canManage && !smart && copyTargets.length > 0 && (
                  <button type="button" className="btn" onClick={() => setCopying('copy')} data-testid="copy-open">
                    <Copy aria-hidden />
                    {t('mirror.copyOpen')}
                  </button>
                )}
                {canManage && !smart && elsewhere.length > 0 && (
                  <button type="button" className="btn" onClick={() => setCopying('move')} data-testid="move-open">
                    <ArrowRightLeft aria-hidden />
                    {t('mirror.moveOpen')}
                  </button>
                )}
                {canManage && <span className="grow" aria-hidden />}
                {canManage && (
                  <button type="button" className="round" title={t('editor.share')} aria-label={t('editor.share')} onClick={() => setSharing(true)}>
                    <Share2 aria-hidden />
                    {playlist.shares > 0 && (
                      <span className="count" aria-hidden>
                        {playlist.shares}
                      </span>
                    )}
                  </button>
                )}
                {canManage && !smart && (
                  <button
                    type="button"
                    className="round"
                    title={t('editor.delete')}
                    aria-label={t('editor.delete')}
                    onClick={() =>
                      ask({
                        title: t('editor.deleteTitle'),
                        text:
                          (playlist.plex && playlist.plex.state !== 'off' ? t('sync.deleteText', { name: iso(playlist.name), days: 30, server: serverName(playlist.plex.kind) }) : t('editor.deleteText', { name: iso(playlist.name), days: 30 })) +
                          (mirrors.length ? ' ' + t('mirror.deleteMirrors', { servers: mirrors.map((one) => one.name || kindName(one.kind)).join(', ') }) : ''),
                        confirm: t('editor.deleteConfirm'),
                        danger: false,
                        run: async () => {
                          await playlistsApi.trash(playlistId)
                          navigate('/', { state: { notice: t('editor.trashed', { name: iso(playlist.name) }) } })
                        },
                      })
                    }
                  >
                    <Trash2 aria-hidden />
                  </button>
                )}
              </div>
            )}
            {playlist.plex?.note && (
              <div className="note" data-testid="plex-note" role="status">
                <b>{t('sync.noteTitle', { server: serverName(playlist.plex.kind) })}</b>
                {t('sync.noteText', { server: serverName(playlist.plex.kind) })}
                {canEdit && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    <button type="button" className="btn" onClick={() => void takePlexOrder()}>
                      {t('sync.noteUndo')}
                    </button>
                    <button type="button" className="btn ghost" onClick={() => void keepOrder()}>
                      {t('sync.noteKeep')}
                    </button>
                  </div>
                )}
              </div>
            )}
            {playlist.plex?.loss && (
              <div className="note" data-testid="loss-note" role="status">
                <b>{t('sync.lossTitle', { server: serverName(playlist.plex.kind), count: playlist.plex.loss.count })}</b>
                {t('sync.lossText', { server: serverName(playlist.plex.kind) })}
                {canEdit && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    <button type="button" className="btn" onClick={() => void keepLoss()}>
                      {t('sync.lossKeep')}
                    </button>
                    <button
                      type="button"
                      className="btn ghost"
                      onClick={() =>
                        ask({
                          title: t('sync.lossRemoveTitle'),
                          text: t('sync.lossRemoveText', { server: serverName(playlist.plex.kind), name: iso(playlist.name) }),
                          confirm: t('sync.lossRemove'),
                          danger: true,
                          run: takeLoss,
                        })
                      }
                    >
                      {t('sync.lossRemove')}
                    </button>
                  </div>
                )}
              </div>
            )}
            {playlist.plex?.concurrent && (
              <div className="note" data-testid="concurrent-note" role="status">
                <b>{t('sync.concurrentTitle', { count: playlist.plex.concurrent.count })}</b>
                {t('sync.concurrentText', { count: playlist.plex.concurrent.count })}
                {canEdit && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    <button type="button" className="btn" onClick={() => void seenConcurrent()}>
                      {t('sync.concurrentSeen')}
                    </button>
                  </div>
                )}
              </div>
            )}
            {problem && (
              <p role="alert" className="mt-2 text-sm text-bad">
                {problem}
              </p>
            )}
          </div>
        </div>
        <div className="col-body soft-scroll" ref={list}>
          <div className="mirrornotes">
            <MirrorNotes playlistId={playlistId} name={playlist.name} mirrors={mirrors} source={playlist.server ?? null} canEdit={canEdit} canManage={canManage} onChanged={() => void reload()} onToast={(text) => setToast({ text })} />
          </div>
          <TrackList
            items={playlist.items}
            server={serverName(playlist.plex?.kind)}
            mirrorNames={Object.fromEntries(mirrors.map((one) => [one.server, one.name || kindName(one.kind)]))}
            canEdit={canEdit}
            fresh={fresh}
            focusEntry={focusEntry}
            onRemove={(entry, index) => void remove(entry, index)}
            onMove={(entry, after) => void move(entry, after)}
            onSimilar={(entry) => setSeed({ track: entry.track, title: entry.title })}
            empty={
              <div className="empty" data-testid="tracks-empty">
                <b>{t('editor.emptyTitle')}</b>
                <br />
                {smart ? t('sync.smartEmpty') : canEdit ? t('editor.emptyText') : t('editor.emptyRead')}
              </div>
            }
          />
          {smart && playlist.plex?.total != null && playlist.plex.total > playlist.items.length && (
            <p className="empty" data-testid="smart-shown">
              {t('sync.smartShown', { shown: playlist.items.length.toLocaleString(i18n.language), count: playlist.plex.total.toLocaleString(i18n.language) })}
            </p>
          )}
        </div>
      </section>
      <SuggestColumn
        source={catalog.source}
        ready={catalog.facets !== null}
        playlistId={playlistId}
        items={playlist.items}
        songs={inList.songs}
        canEdit={canEdit}
        seed={seed}
        analysis={status?.analysis}
        operator={me?.role === 'operator'}
        onSeed={setSeed}
        onAdd={(hits) => void add(hits)}
        onMood={() => setFilling(true)}
        onToast={setToast}
        noMoods={noMoods !== null}
      />
      {filling && <MoodDialog noMoods={noMoods} source={catalog.source} facets={catalog.facets} playlistId={playlistId} songs={inList.songs} onTake={(hits) => void add(hits)} onClose={() => setFilling(false)} />}
      {inviting && (
        <InviteDialog
          playlistId={playlistId}
          name={playlist.name}
          operator={me?.role === 'operator'}
          refreshKey={guestsSeen}
          order={guestOrder}
          onClose={() => setInviting(false)}
          onChanged={(guests) => current.current && show({ ...current.current, guests })}
          onToast={(text) => setToast({ text })}
        />
      )}
      {copying && (
        <CopyDialog
          playlistId={playlistId}
          name={playlist.name}
          mode={copying}
          targets={copying === 'copy' ? copyTargets : elsewhere}
          sourceName={sourceName}
          onClose={() => setCopying(null)}
          onDone={(text, copy) => {
            setCopying(null)
            setToast({ text })
            if (copy) navigate(`/playlists/${copy}`)
            else void reload()
          }}
        />
      )}
      {mirrorOpen !== null && mirrors.find((one) => one.server === mirrorOpen) && (
        <MirrorDialog
          playlistId={playlistId}
          mirror={mirrors.find((one) => one.server === mirrorOpen) as Mirror}
          items={playlist.items}
          canEdit={canEdit}
          canManage={canManage}
          onClose={() => setMirrorOpen(null)}
          onChanged={() => void reload()}
        />
      )}
      {sharing && <ShareDialog playlistId={playlistId} name={playlist.name} onClose={() => setSharing(false)} onChanged={(count) => current.current && show({ ...current.current, shares: count })} />}
      {toast && (
        <div className="toast" role="status" data-testid="toast">
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
      {dialog}
    </div>
  )
}
