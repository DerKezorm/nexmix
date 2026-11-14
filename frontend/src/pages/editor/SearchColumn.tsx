/**
 * The left column of the editor: tracks, albums and artists of the catalogue, the filters (mood, decade, genre), the
 * view of one album or artist with "add all", and how long the search took. A song in the playlist (in any version) is
 * "in" in the list of tracks; in an album or an artist only the very track is, so another version can be chosen there.
 */
import { Check, Plus, Search } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { AlbumDetail, AlbumHit, ArtistDetail, ArtistHit, Facets, TrackHit } from '../../lib/catalogSearch'
import type { CatalogSource } from '../../lib/catalogSource'
import { Cover } from '../../components/Mosaic'
import { PlayButton } from '../../components/PlayButton'
import { errorText } from '../../lib/errors'
import { clock, decadeName, moodName, trackLine } from '../../lib/music'

type Tab = 'tracks' | 'albums' | 'artists'
type View = { kind: 'album'; id: number } | { kind: 'artist'; id: number }

type Results =
  | { tab: 'tracks'; total: number; tracks: TrackHit[]; ms: number }
  | { tab: 'albums'; total: number; albums: AlbumHit[]; ms: number }
  | { tab: 'artists'; total: number; artists: ArtistHit[]; ms: number }
  | { tab: 'album'; detail: AlbumDetail | null }
  | { tab: 'artist'; detail: ArtistDetail | null }

export type InList = { songs: Set<number>; tracks: Set<number> }

function TrackRow({ hit, inside, canEdit, onAdd, extra }: { hit: TrackHit; inside: boolean; canEdit: boolean; onAdd: (hit: TrackHit) => void; extra?: string }) {
  const { t } = useTranslation()
  return (
    <div className={`row${inside ? ' in' : ''}`} data-testid="search-track">
      <Cover album={hit.album_id} has={hit.cover} />
      <div className="min-w-0">
        <div className="t" data-in={inside ? t('editor.in') : undefined} dir="auto">
          {hit.title}
        </div>
        <div className="s" dir="auto">
          {trackLine(hit.artist, hit.album, hit.year)}
        </div>
        {extra && (
          <div className="s" style={{ fontSize: '11.5px' }}>
            {extra}
          </div>
        )}
      </div>
      <div className="acts">
        <span className="dur">{clock(hit.duration)}</span>
        <PlayButton track={hit.id} title={hit.title} />
        {inside ? (
          <span className="round done" aria-label={t('editor.in')}>
            <Check aria-hidden />
          </span>
        ) : canEdit ? (
          <button type="button" className="round add" title={t('editor.add')} aria-label={t('editor.addTrack', { title: hit.title })} onClick={() => onAdd(hit)}>
            <Plus aria-hidden />
          </button>
        ) : null}
      </div>
    </div>
  )
}

export function SearchColumn({
  source,
  facets,
  version,
  failed,
  inList,
  canEdit,
  onAdd,
  onAddAll,
}: {
  source: CatalogSource | null
  facets: Facets | null
  version: number
  failed: string | null
  inList: InList
  canEdit: boolean
  onAdd: (hits: TrackHit[]) => void
  onAddAll: (hits: TrackHit[]) => void
}) {
  const { t, i18n } = useTranslation()
  const [tab, setTab] = useState<Tab>('tracks')
  const [query, setQuery] = useState('')
  const [mood, setMood] = useState('')
  const [decade, setDecade] = useState('')
  const [genre, setGenre] = useState('')
  const [views, setViews] = useState<View[]>([])
  const [results, setResults] = useState<Results | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const body = useRef<HTMLDivElement>(null)
  const asked = useRef(0)
  const view = views[views.length - 1] ?? null

  useEffect(() => {
    if (!source || !facets) return
    const number = ++asked.current
    const run = async () => {
      try {
        let found: Results
        if (view?.kind === 'album') found = { tab: 'album', detail: await source.album(view.id) }
        else if (view?.kind === 'artist') found = { tab: 'artist', detail: await source.artist(view.id) }
        else if (tab === 'albums') found = { tab: 'albums', ...(await source.albums(query)) }
        else if (tab === 'artists') found = { tab: 'artists', ...(await source.artists(query)) }
        else found = { tab: 'tracks', ...(await source.tracks(query, { mood, decade: decade ? Number(decade) : 0, genre })) }
        if (number === asked.current) {
          setResults(found)
          setProblem(null)
        }
      } catch (error) {
        if (number === asked.current) setProblem(errorText((error as { code?: string }).code ?? 'internal_error'))
      }
    }
    // The server is asked a moment after the last key; the browser's own catalogue at once.
    if (source.mode === 'server') {
      const timer = window.setTimeout(() => void run(), 150)
      return () => window.clearTimeout(timer)
    }
    void run()
  }, [source, facets, version, tab, query, mood, decade, genre, view])

  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey) return
      if (target && /INPUT|TEXTAREA|SELECT/.test(target.tagName)) return
      if (target?.isContentEditable || document.querySelector('[role="dialog"]')) return
      event.preventDefault()
      input.current?.focus()
      input.current?.select()
    }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [])

  const moodOptions = useMemo(() => {
    if (!facets) return []
    const floor = Math.max(1, Math.min(200, Math.ceil(facets.tracks * 0.004)))
    return facets.moods
      .filter((one) => one.tracks >= floor)
      .map((one) => ({ value: one.name, label: moodName(one.name) }))
      .sort((a, b) => a.label.localeCompare(b.label, i18n.language))
  }, [facets, i18n.language])

  const chooseTab = (next: Tab) => {
    setTab(next)
    setViews([])
    input.current?.focus()
  }
  const filter = (set: (value: string) => void) => (event: React.ChangeEvent<HTMLSelectElement>) => {
    set(event.target.value)
    setTab('tracks')
    setViews([])
  }
  const open = (next: View) => {
    setViews((now) => [...now, next])
    if (body.current) body.current.scrollTop = 0
  }
  const back = () => setViews((now) => now.slice(0, -1))
  const ms = (value: number) => (value < 1 ? '<1' : String(Math.round(value)))
  const hits = (list: TrackHit[], exact: boolean) =>
    list.map((hit) => (
      <TrackRow
        key={hit.id}
        hit={hit}
        inside={inList.tracks.has(hit.id) || (!exact && inList.songs.has(hit.song))}
        canEdit={canEdit}
        onAdd={(one) => onAdd([one])}
        extra={!exact && hit.versions > 1 ? t('editor.versions', { count: hit.versions }) : undefined}
      />
    ))

  let timing = null
  let list = null
  const nothing = <div className="empty">{t('editor.nothing')}</div>
  if (results?.tab === 'tracks') {
    timing = (
      <>
        <span>{t('editor.songsIn', { songs: results.total.toLocaleString(i18n.language), tracks: (facets?.tracks ?? 0).toLocaleString(i18n.language) })}</span>
        <span>
          {t('editor.took')} <b>{ms(results.ms)} ms</b>
        </span>
      </>
    )
    list = (
      <>
        {!query && !mood && !decade && !genre && <div className="group-title">{t('editor.popular')}</div>}
        {results.tracks.length ? hits(results.tracks, false) : nothing}
      </>
    )
  } else if (results?.tab === 'albums') {
    timing = (
      <>
        <span>{t(query ? 'editor.albums' : 'editor.albumsRecent', { count: results.total })}</span>
        <span>
          {t('editor.took')} <b>{ms(results.ms)} ms</b>
        </span>
      </>
    )
    list = results.albums.length
      ? results.albums.map((album) => (
          <div className="row album" key={album.id} role="button" tabIndex={0} onClick={() => open({ kind: 'album', id: album.id })} onKeyDown={(event) => event.key === 'Enter' && open({ kind: 'album', id: album.id })} data-testid="search-album">
            <Cover album={album.id} has={album.cover} />
            <div className="min-w-0">
              <div className="t" dir="auto">
                {album.title}
              </div>
              <div className="s" dir="auto">
                {[album.artist, album.year ? String(album.year) : '', t('editor.tracks', { count: album.tracks })].filter(Boolean).join(' · ')}
              </div>
            </div>
            <div className="acts">
              {canEdit && (
                <button
                  type="button"
                  className="round add"
                  title={t('editor.addAlbum')}
                  aria-label={t('editor.addAlbumNamed', { title: album.title })}
                  onClick={async (event) => {
                    event.stopPropagation()
                    const detail = await source?.album(album.id)
                    if (detail) onAddAll(detail.tracks)
                  }}
                >
                  <Plus aria-hidden />
                </button>
              )}
            </div>
          </div>
        ))
      : nothing
  } else if (results?.tab === 'artists') {
    timing = (
      <>
        <span>{t('editor.artists', { count: results.total })}</span>
        <span>
          {t('editor.took')} <b>{ms(results.ms)} ms</b>
        </span>
      </>
    )
    list = results.artists.length
      ? results.artists.map((artist) => (
          <div className="row album" key={artist.id} role="button" tabIndex={0} onClick={() => open({ kind: 'artist', id: artist.id })} onKeyDown={(event) => event.key === 'Enter' && open({ kind: 'artist', id: artist.id })} data-testid="search-artist">
            <Cover album={artist.cover} has={artist.cover !== null} />
            <div className="min-w-0">
              <div className="t" dir="auto">
                {artist.name}
              </div>
              <div className="s">{artist.moods.slice(0, 3).map(moodName).join(' · ')}</div>
            </div>
            <div className="acts" />
          </div>
        ))
      : nothing
  } else if (results?.tab === 'album') {
    const detail = results.detail
    list = detail ? (
      <>
        <div className="albumhead" data-testid="album-view">
          <Cover album={detail.album.id} has={detail.album.cover} size={200} />
          <div className="min-w-0 flex-1">
            <div className="t font-bold" dir="auto">
              {detail.album.title}
            </div>
            <div className="s text-muted" dir="auto">
              {[detail.album.artist, detail.album.year ? String(detail.album.year) : '', t('editor.tracks', { count: detail.album.tracks })].filter(Boolean).join(' · ')}
            </div>
            <div className="mt-2 flex gap-1.5">
              <button type="button" className="btn" style={{ padding: '5px 10px' }} onClick={back}>
                {t('editor.back')}
              </button>
              {canEdit && (
                <button type="button" className="btn primary" style={{ padding: '5px 10px' }} onClick={() => onAddAll(detail.tracks)}>
                  {t('editor.addAll')}
                </button>
              )}
            </div>
          </div>
        </div>
        {hits(detail.tracks, true)}
      </>
    ) : (
      nothing
    )
  } else if (results?.tab === 'artist') {
    const detail = results.detail
    list = detail ? (
      <>
        <div className="albumhead" data-testid="artist-view">
          <div className="flex-1">
            <div className="font-display text-lg font-bold" dir="auto">
              {detail.artist.name}
            </div>
            <div className="text-muted" style={{ fontSize: '12.5px' }}>
              {detail.artist.moods.slice(0, 4).map(moodName).join(' · ')}
            </div>
            <button type="button" className="btn mt-2" style={{ padding: '5px 10px' }} onClick={back}>
              {t('editor.back')}
            </button>
          </div>
        </div>
        <div className="group-title">{t('editor.topTracks')}</div>
        {hits(detail.tracks, true)}
        <div className="group-title">{t('editor.albumsTitle')}</div>
        {detail.albums.map((album) => (
          <div className="row album" key={album.id} role="button" tabIndex={0} onClick={() => open({ kind: 'album', id: album.id })} onKeyDown={(event) => event.key === 'Enter' && open({ kind: 'album', id: album.id })}>
            <Cover album={album.id} has={album.cover} />
            <div className="min-w-0">
              <div className="t" dir="auto">
                {album.title}
              </div>
              <div className="s">{[album.year ? String(album.year) : '', t('editor.tracks', { count: album.tracks })].filter(Boolean).join(' · ')}</div>
            </div>
            <div className="acts" />
          </div>
        ))}
      </>
    ) : (
      nothing
    )
  }

  const tabs: [Tab, string][] = [
    ['tracks', t('editor.tabTracks')],
    ['albums', t('editor.tabAlbums')],
    ['artists', t('editor.tabArtists')],
  ]
  return (
    <section className="col add" aria-label={t('editor.searchArea')}>
      <div className="col-head">
        <div className="seg small" role="group" aria-label={t('editor.searchArea')}>
          {tabs.map(([key, label]) => (
            <button key={key} type="button" className={tab === key && !view ? 'on' : ''} aria-pressed={tab === key && !view} onClick={() => chooseTab(key)}>
              {label}
            </button>
          ))}
        </div>
        <div className="searchbox">
          <Search aria-hidden />
          <input
            ref={input}
            type="search"
            placeholder={t('editor.searchPlaceholder')}
            aria-label={t('editor.searchLabel')}
            autoComplete="off"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value)
              setViews([])
            }}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                setQuery('')
                setViews([])
              }
            }}
          />
          <kbd>/</kbd>
        </div>
        <div className="filters">
          <select className={`chip${mood ? ' on' : ''}`} aria-label={t('editor.mood')} value={mood} onChange={filter(setMood)}>
            <option value="">{t('editor.mood')}</option>
            {(facets?.groups ?? []).map((group) => (
              <option key={group.key} value={`g:${group.key}`}>
                {t(`moodGroups.${group.key}`)}
              </option>
            ))}
            <option disabled>──────</option>
            {moodOptions.map((one) => (
              <option key={one.value} value={one.value}>
                {one.label}
              </option>
            ))}
          </select>
          <select className={`chip${decade ? ' on' : ''}`} aria-label={t('editor.decade')} value={decade} onChange={filter(setDecade)}>
            <option value="">{t('editor.decade')}</option>
            {(facets?.decades ?? []).map((one) => (
              <option key={one} value={one}>
                {decadeName(one)}
              </option>
            ))}
          </select>
          <select className={`chip${genre ? ' on' : ''}`} aria-label={t('editor.genre')} value={genre} onChange={filter(setGenre)}>
            <option value="">{t('editor.genre')}</option>
            {(facets?.genres ?? []).map((one) => (
              <option key={one} value={one}>
                {one}
              </option>
            ))}
          </select>
        </div>
        <div className="timing" data-testid="timing">
          {timing}
        </div>
      </div>
      <div className="col-body soft-scroll" ref={body} data-testid="results">
        {failed ? <div className="empty">{errorText(failed)}</div> : problem ? <div className="empty">{problem}</div> : !facets ? <div className="empty">{t('editor.catalogLoading')}</div> : list}
      </div>
    </section>
  )
}
