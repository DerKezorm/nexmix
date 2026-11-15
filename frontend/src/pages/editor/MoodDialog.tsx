/**
 * "Fill by mood": moods in their groups (with how many tracks have each), decades, a length and a slider between
 * discoveries and hits. nexmix proposes songs (`CatalogSource.build`), each with a tick and ▶; "Mix again" draws anew,
 * "Take" adds the ticked ones in one step (one undo). At most two per artist, each song once, never one of the playlist
 * or one hidden; the same artist not twice in a row.
 */
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { suggestionsApi } from '../../api/suggestions'
import { Dialog } from '../../components/Dialog'
import { Cover } from '../../components/Mosaic'
import { PlayButton } from '../../components/PlayButton'
import type { Facets, TrackHit } from '../../lib/catalogSearch'
import type { CatalogSource } from '../../lib/catalogSource'
import { errorText } from '../../lib/errors'
import { clock, decadeName, length, moodName } from '../../lib/music'
import { stop } from '../../lib/preview'

const randomSeed = () => Math.floor(Math.random() * 4294967296)

export function MoodDialog({
  source,
  facets,
  playlistId,
  songs,
  onTake,
  onClose,
  noMoods = null,
}: {
  /** Set when the playlist's server has no moods: the dialog says so instead of offering none. */
  noMoods?: string | null
  source: CatalogSource | null
  facets: Facets | null
  playlistId: number
  songs: Set<number>
  onTake: (hits: TrackHit[]) => void
  onClose: () => void
}) {
  const { t, i18n } = useTranslation()
  const [moods, setMoods] = useState<Set<string>>(new Set())
  const [decades, setDecades] = useState<Set<number>>(new Set())
  const [minutes, setMinutes] = useState(60)
  const [hits, setHits] = useState(55)
  const [picks, setPicks] = useState<TrackHit[]>([])
  const [off, setOff] = useState<Set<number>>(new Set())
  const [hidden, setHidden] = useState<number[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [made, setMade] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)

  useEffect(() => {
    let gone = false
    if (source?.mode !== 'browser') {
      setHidden([])
      return
    }
    suggestionsApi
      .hidden()
      .then((answer) => !gone && setHidden(answer.songs ?? []))
      .catch(() => !gone && setHidden([]))
    return () => {
      gone = true
    }
  }, [source])

  // A preview started here ends with the dialog.
  useEffect(() => () => stop(), [])

  const counts = useMemo(() => new Map((facets?.moods ?? []).map((one) => [one.name, one.tracks])), [facets])
  const groups = (facets?.groups ?? [])
    .map((group) => ({ key: group.key, moods: group.moods.filter((name) => (counts.get(name) ?? 0) > 0) }))
    .filter((group) => group.moods.length > 0)
  const number = (value: number) => value.toLocaleString(i18n.language)

  const toggle = <T,>(set: (change: (now: Set<T>) => Set<T>) => void, value: T) =>
    set((now) => {
      const next = new Set(now)
      if (next.has(value)) next.delete(value)
      else next.add(value)
      return next
    })

  const generate = async () => {
    if (!source || !moods.size || busy) return
    setBusy(true)
    setProblem(null)
    try {
      const found = await source.build({
        moods: [...moods],
        decades: [...decades],
        minutes,
        hits,
        exclude: [...songs],
        hidden: hidden ?? [],
        seed: randomSeed(),
        playlist: playlistId,
      })
      stop()
      setPicks(found.tracks)
      setOff(new Set())
      setMade(true)
    } catch (error) {
      setProblem(errorText((error as { code?: string }).code ?? 'internal_error'))
    } finally {
      setBusy(false)
    }
  }

  const chosen = picks.filter((track) => !off.has(track.id))
  const take = () => {
    stop()
    onClose()
    onTake(chosen)
  }

  if (noMoods)
    return (
      <Dialog title={t('mood.title')} onClose={onClose} className="max-w-[640px]!">
        <p className="text-sm" data-testid="mood-none">
          {noMoods}
        </p>
        <div className="dlg-foot">
          <span className="grow" />
          <button type="button" className="btn" onClick={onClose}>
            {t('common.close')}
          </button>
        </div>
      </Dialog>
    )

  return (
    <Dialog title={t('mood.title')} sub={t('mood.text')} onClose={onClose} className="max-w-[640px]!">
      <div className="mood-body soft-scroll">
      <div className="moodgroups" data-testid="mood-groups">
        {groups.map((group) => (
          <div className="moodgroup" key={group.key}>
            <h4>{t(`moodGroups.${group.key}`)}</h4>
            <div className="chips">
              {group.moods.map((name) => (
                <button key={name} type="button" className={`chip${moods.has(name) ? ' on' : ''}`} aria-pressed={moods.has(name)} onClick={() => toggle(setMoods, name)}>
                  {moodName(name)} <span className="n">{number(counts.get(name) ?? 0)}</span>
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
      <div className="field">
        <span className="lab">{t('mood.decades')}</span>
        <div className="chips">
          {(facets?.decades ?? []).map((decade) => (
            <button key={decade} type="button" className={`chip${decades.has(decade) ? ' on' : ''}`} aria-pressed={decades.has(decade)} onClick={() => toggle(setDecades, decade)}>
              {decadeName(decade)}
            </button>
          ))}
          <span className="muted all">{t('mood.allDecades')}</span>
        </div>
      </div>
      <div className="field mood-ranges">
        <div>
          <label className="rlab" htmlFor="mood-minutes">
            {t('mood.length')} <span>{t('music.minutes', { minutes })}</span>
          </label>
          <input id="mood-minutes" type="range" min={15} max={240} step={15} value={minutes} onChange={(event) => setMinutes(Number(event.target.value))} />
        </div>
        <div>
          <label className="rlab" htmlFor="mood-hits">
            {t('mood.knownOrNew')}
          </label>
          <input id="mood-hits" type="range" min={0} max={100} value={hits} onChange={(event) => setHits(Number(event.target.value))} />
          <div className="rangecap">
            <span>{t('mood.discoveries')}</span>
            <span>{t('mood.hits')}</span>
          </div>
        </div>
      </div>
      {picks.length > 0 && (
        <div className="field">
          <span className="lab" data-testid="mood-summary">
            {t('mood.proposal', { count: chosen.length, length: length(chosen.reduce((sum, track) => sum + track.duration, 0)) })}
          </span>
          <div className="proposal soft-scroll" data-testid="mood-proposal">
            {picks.map((track) => (
              <label className="row" key={track.id} data-testid="mood-pick">
                <input type="checkbox" checked={!off.has(track.id)} onChange={() => toggle(setOff, track.id)} aria-label={track.title} />
                <Cover album={track.album_id} has={track.cover} />
                <div className="min-w-0">
                  <div className="t" dir="auto">
                    {track.title}
                  </div>
                  <div className="s" dir="auto">
                    {[track.artist, track.year ? String(track.year) : ''].filter(Boolean).join(' · ')}
                  </div>
                </div>
                <div className="acts">
                  <span className="dur">{clock(track.duration)}</span>
                  <PlayButton track={track.id} title={track.title} />
                </div>
              </label>
            ))}
          </div>
        </div>
      )}
      {made && picks.length === 0 && (
        <p className="mt-2 text-sm text-muted" data-testid="mood-nothing">
          {t('mood.nothing')}
        </p>
      )}
      {problem && (
        <p role="alert" className="mt-2 text-sm text-bad">
          {problem}
        </p>
      )}
      </div>
      <div className="dlg-foot">
        <span className="grow" data-testid="mood-chosen">
          {moods.size ? t('mood.chosen', { count: moods.size }) : t('mood.pickOne')}
        </span>
        <button type="button" className="btn" disabled={!moods.size || busy || !source} onClick={() => void generate()}>
          {picks.length ? t('mood.again') : t('mood.make')}
        </button>
        {picks.length > 0 && (
          <button type="button" className="btn primary" data-primary disabled={!chosen.length} onClick={take}>
            {t('mood.take')}
          </button>
        )}
      </div>
    </Dialog>
  )
}
