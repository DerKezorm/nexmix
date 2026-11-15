/**
 * The right column of the editor: songs that fit the last five of the playlist, or one track ("similar to this"), in
 * three kinds (fitting, known, discoveries), each with its reason, ▶, + and "not again". Worked out where the catalogue
 * is (`CatalogSource.suggest`: the browser, or for a large library the server), with the two voices of the server
 * ("often together" from the playlists one may see, "sounds alike" from Plex's sonic analysis while it is on) and the
 * songs this person asked not to see again. Below them, a word about the sonic analysis while it is off or still
 * running.
 */
import { Plus, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import type { Analysis } from '../../api/plex'
import type { Entry } from '../../api/playlists'
import { suggestionsApi } from '../../api/suggestions'
import { Cover } from '../../components/Mosaic'
import { PlayButton } from '../../components/PlayButton'
import { Spark } from '../../components/Spark'
import type { TrackHit } from '../../lib/catalogSearch'
import type { CatalogSource } from '../../lib/catalogSource'
import { errorText } from '../../lib/errors'
import { useReasonText } from '../../lib/reason'
import type { Mode, Suggestion } from '../../lib/suggest'

export type Seed = { track: number; title: string }
type Voices = { together: [number, number][]; sound: [number, number][] }
type Toast = { text: string; undo?: () => Promise<void> }

export function SuggestColumn({
  source,
  ready,
  playlistId,
  items,
  songs,
  canEdit,
  seed,
  analysis,
  operator,
  onSeed,
  onAdd,
  onMood,
  onToast,
  noMoods = false,
}: {
  /** The playlist's server has no moods and no styles: the foot names what the suggestions come from. */
  noMoods?: boolean
  source: CatalogSource | null
  /** The catalogue's filters are there (and so the catalogue). */
  ready: boolean
  playlistId: number
  items: Entry[]
  /** The songs of the playlist: never suggested. */
  songs: Set<number>
  canEdit: boolean
  seed: Seed | null
  analysis: Analysis | undefined
  operator: boolean
  onSeed: (seed: Seed | null) => void
  onAdd: (hits: TrackHit[]) => void
  onMood: () => void
  onToast: (toast: Toast) => void
}) {
  const { t } = useTranslation()
  const reasonText = useReasonText()
  const [mode, setMode] = useState<Mode>('fit')
  const [hidden, setHidden] = useState<number[] | null>(null)
  const [voices, setVoices] = useState<Voices>({ together: [], sound: [] })
  const [found, setFound] = useState<{ tracks: Suggestion[]; ms: number } | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [round, setRound] = useState(0)
  const asked = useRef(0)
  const local = source?.mode === 'browser'

  const seeds = useMemo(() => {
    const present = items.filter((entry) => !entry.missing)
    if (seed) return { tracks: [seed.track], songs: present.filter((entry) => entry.track === seed.track).map((entry) => entry.song ?? 0).filter(Boolean) }
    const last = present.slice(-5)
    return { tracks: last.map((entry) => entry.track), songs: last.map((entry) => entry.song).filter((song): song is number => song !== null) }
  }, [items, seed])
  const seedKey = seeds.tracks.join(',')
  const songKey = useMemo(() => [...songs].sort((a, b) => a - b).join(','), [songs])
  const listening = analysis?.state === 'running' || analysis?.state === 'on'

  // The songs not to suggest again: once, where the browser works the suggestions out (the server knows them itself).
  useEffect(() => {
    if (!local) return
    let gone = false
    suggestionsApi
      .hidden()
      .then((answer) => !gone && setHidden(answer.songs ?? []))
      .catch(() => !gone && setHidden([]))
    return () => {
      gone = true
    }
  }, [local])

  // The two voices of the server for this seed.
  useEffect(() => {
    if (!local || !seedKey) {
      setVoices({ together: [], sound: [] })
      return
    }
    let gone = false
    const tracks = seedKey.split(',').map(Number)
    const timer = window.setTimeout(async () => {
      const [together, sound] = await Promise.all([
        seeds.songs.length ? suggestionsApi.together(seeds.songs).catch(() => null) : Promise.resolve(null),
        listening ? suggestionsApi.sonic(tracks).catch(() => null) : Promise.resolve(null),
      ])
      if (gone) return
      setVoices({
        together: (together?.songs ?? []).map((one) => [one.song, one.count]),
        sound: (sound?.songs ?? []).map((one) => [one.song, one.distance]),
      })
    }, 120)
    return () => {
      gone = true
      window.clearTimeout(timer)
    }
    // The songs of the seed follow from its tracks.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [local, seedKey, listening])

  useEffect(() => {
    if (!source || !ready || !seedKey || (local && hidden === null)) {
      setFound(null)
      return
    }
    const number = ++asked.current
    const run = async () => {
      try {
        const answer = await source.suggest({
          seed: seedKey.split(',').map(Number),
          mode,
          exclude: songKey ? songKey.split(',').map(Number) : [],
          hidden: hidden ?? [],
          together: voices.together,
          sound: voices.sound,
          playlist: playlistId,
        })
        if (number === asked.current) {
          setFound(answer)
          setProblem(null)
        }
      } catch (error) {
        if (number === asked.current) setProblem(errorText((error as { code?: string }).code ?? 'internal_error'))
      }
    }
    if (local) void run()
    else {
      const timer = window.setTimeout(() => void run(), 200)
      return () => window.clearTimeout(timer)
    }
  }, [source, ready, local, seedKey, songKey, mode, hidden, voices, playlistId, round])

  const hide = async (track: Suggestion) => {
    try {
      const answer = await suggestionsApi.hide(track.id)
      setHidden(answer.songs ?? [])
      setRound((value) => value + 1)
      onToast({
        text: t('suggest.hidden'),
        undo: async () => {
          const back = await suggestionsApi.unhide(track.id)
          setHidden(back.songs ?? [])
          setRound((value) => value + 1)
        },
      })
    } catch (error) {
      setProblem(errorText((error as { code?: string }).code ?? 'internal_error'))
    }
  }

  const modes: [Mode, string][] = [
    ['fit', t('suggest.fit')],
    ['known', t('suggest.known')],
    ['new', t('suggest.new')],
  ]

  let body
  if (!seedKey)
    body = (
      <div className="mood-cta" data-testid="suggest-empty">
        <h3>{t('suggest.noSeedTitle')}</h3>
        <p>{t('suggest.noSeedText')}</p>
        {canEdit && (
          <button type="button" className="btn primary" onClick={onMood}>
            <Spark />
            {t('mood.open')}
          </button>
        )}
      </div>
    )
  else if (problem) body = <div className="empty">{problem}</div>
  else if (!found) body = <div className="empty">{t('editor.catalogLoading')}</div>
  else
    body = (
      <>
        {found.tracks.length === 0 && <div className="empty">{t('suggest.none')}</div>}
        {found.tracks.map((track) => (
          <div className="row" key={track.id} data-testid="suggestion">
            <Cover album={track.album_id} has={track.cover} />
            <div className="min-w-0">
              <div className="t" dir="auto">
                {track.title}
              </div>
              <div className="s" dir="auto">
                {track.artist}
              </div>
              <div className="why">{reasonText(track.reason)}</div>
            </div>
            <div className="acts">
              <PlayButton track={track.id} title={track.title} />
              {canEdit && (
                <button type="button" className="round add" title={t('editor.add')} aria-label={t('editor.addTrack', { title: track.title })} onClick={() => onAdd([track])}>
                  <Plus aria-hidden />
                </button>
              )}
              <button type="button" className="round quiet" title={t('suggest.hide')} aria-label={t('suggest.hideTrack', { title: track.title })} onClick={() => void hide(track)}>
                <X aria-hidden />
              </button>
            </div>
          </div>
        ))}
        <div className="timing suggest-timing">
          <span>{voices.sound.length ? t('suggest.basisSound') : noMoods ? t('suggest.basisNoMoods') : t('suggest.basis')}</span>
          <span>
            {t('suggest.took')} <b>{Math.round(found.ms)} ms</b>
          </span>
        </div>
        {analysis?.state === 'off' && (
          <div className="mood-cta" data-testid="sonic-note">
            <h3>{t('suggest.soundTitle')}</h3>
            <p>{t('suggest.soundOff')}</p>
            {operator && (
              <Link className="btn" to="/settings">
                {t('suggest.more')}
              </Link>
            )}
          </div>
        )}
        {analysis?.state === 'running' && !voices.sound.length && (
          <div className="mood-cta" data-testid="sonic-note">
            <h3>{t('suggest.soundTitle')}</h3>
            <p>{t('suggest.soundRunning', { percent: analysis.percent ?? 0 })}</p>
          </div>
        )}
      </>
    )

  return (
    <section className="col sugg" aria-label={t('suggest.title')} data-testid="suggestions">
      <div className="col-head">
        <h2>
          <Spark /> {t('suggest.title')}
        </h2>
        <div className="seedlabel muted" data-testid="seed-label">
          {seed ? (
            <>
              {t('suggest.similarTo')} <b dir="auto">{seed.title}</b>{' '}
              <button type="button" className="chip" onClick={() => onSeed(null)}>
                {t('suggest.backToLast')} <span className="x">×</span>
              </button>
            </>
          ) : seeds.tracks.length ? (
            t('suggest.fromLast', { count: seeds.tracks.length })
          ) : null}
        </div>
        <div className="sugg-tools">
          <div className="seg small" role="group" aria-label={t('suggest.kind')}>
            {modes.map(([key, label]) => (
              <button key={key} type="button" className={mode === key ? 'on' : ''} aria-pressed={mode === key} onClick={() => setMode(key)}>
                {label}
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="col-body soft-scroll">{body}</div>
    </section>
  )
}
