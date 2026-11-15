/**
 * What the dialogs of copying and moving share: the count of the preview ("N of M found, K of them worth a look"), the
 * list of the entries not found or found unsure, each with "match otherwise" (a search in the target's catalogue) and
 * "leave out", and the search itself. Choices are kept by the entry's track; the preview is asked anew after each.
 */
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api } from '../../api/client'
import type { Choices, Preview, PreviewRow } from '../../api/mirrors'
import type { TrackHit, TrackResult } from '../../lib/catalogSearch'
import { clock } from '../../lib/music'
import { iso } from '../../lib/say'

/** A search in the catalogue of one server for a track to choose. */
export function TrackPicker({ server, start, onPick, onCancel, label }: { server: number; start: string; onPick: (hit: TrackHit) => void; onCancel: () => void; label: string }) {
  const { t } = useTranslation()
  const [query, setQuery] = useState(start)
  const [hits, setHits] = useState<TrackHit[] | null>(null)
  useEffect(() => {
    const wanted = query.trim()
    if (wanted.length < 2) {
      setHits(null)
      return
    }
    let gone = false
    const timer = window.setTimeout(() => {
      api<TrackResult>('/api/v1/catalog/search', { query: { q: wanted, kind: 'tracks', server } })
        .then((found) => !gone && setHits((found.tracks ?? []).slice(0, 8)))
        .catch(() => !gone && setHits([]))
    }, 250)
    return () => {
      gone = true
      window.clearTimeout(timer)
    }
  }, [query, server])
  return (
    <div className="picker" data-testid="track-picker">
      <div className="flex gap-2">
        <input className="input grow" value={query} aria-label={label} placeholder={t('mirror.searchPlaceholder')} onChange={(event) => setQuery(event.target.value)} data-autofocus />
        <button type="button" className="btn ghost" onClick={onCancel}>
          {t('common.cancel')}
        </button>
      </div>
      {hits && hits.length === 0 && <p className="mt-2 text-sm text-muted">{t('mirror.searchNone')}</p>}
      {hits?.map((hit) => (
        <div className="pick" key={hit.id}>
          <div className="grow min-w-0">
            <b dir="auto">{hit.title}</b>
            <small dir="auto">
              {hit.artist} · {hit.album} · {clock(hit.duration)}
            </small>
          </div>
          <button type="button" className="btn" onClick={() => onPick(hit)} aria-label={t('mirror.takeLabel', { title: hit.title })}>
            {t('mirror.take')}
          </button>
        </div>
      ))}
    </div>
  )
}

export function PreviewCount({ preview }: { preview: Preview }) {
  const { t, i18n } = useTranslation()
  const number = (value: number) => value.toLocaleString(i18n.language)
  return (
    <p className="previewcount" data-testid="mirror-count" role="status">
      <b>{t('mirror.found', { found: number(preview.found), count: number(preview.entries) })}</b>
      {preview.unsure > 0 && <span>{t('mirror.unsure', { count: preview.unsure, number: number(preview.unsure) })}</span>}
      {preview.missing > 0 && <span>{t('mirror.missingCount', { count: preview.missing, number: number(preview.missing) })}</span>}
    </p>
  )
}

/** The entries not found or found unsure, each to be matched by hand or left out. */
export function PreviewRows({ preview, target, choices, onChoose }: { preview: Preview; target: number; choices: Choices; onChoose: (track: number, choice: number | null | undefined) => void }) {
  const { t } = useTranslation()
  const [picking, setPicking] = useState<number | null>(null)
  const [named, setNamed] = useState<Record<number, TrackHit>>({})
  if (!preview.rows.length) return <p className="text-sm text-muted">{t('mirror.allFound')}</p>
  return (
    <div className="previewrows soft-scroll" data-testid="mirror-rows">
      {preview.rows.map((row: PreviewRow) => {
        const chosen = choices[row.track]
        const hand = chosen !== undefined && chosen !== null ? named[row.track] : null
        const left = chosen === null || row.how === 'skip'
        const unsure = row.found !== null && row.how !== 'none' && row.how !== 'skip'
        return (
          <div className="prow" key={row.track} data-testid="mirror-row">
            <div className="min-w-0 grow">
              <b dir="auto">{row.title}</b>
              <small dir="auto">
                {row.artist} · {row.album} · {clock(row.duration)}
                {row.count > 1 && ` · ${t('mirror.inPlaylists', { count: row.count })}`}
              </small>
              <span className={`chip static ${left ? '' : unsure || hand ? 'on' : ''}`} data-testid="mirror-row-state">
                {hand
                  ? t('mirror.byHand', { title: iso(hand.title) })
                  : left
                    ? t('mirror.left')
                    : unsure && row.found
                      ? t('mirror.unsureMatch', { title: iso(row.found.title), artist: iso(row.found.artist) })
                      : t('mirror.notFound')}
              </span>
            </div>
            <div className="flex flex-wrap gap-2">
              <button type="button" className="btn ghost" onClick={() => setPicking(picking === row.track ? null : row.track)} aria-label={t('mirror.otherLabel', { title: row.title })}>
                {t('mirror.other')}
              </button>
              {!left ? (
                <button type="button" className="btn ghost" onClick={() => onChoose(row.track, null)} aria-label={t('mirror.leaveLabel', { title: row.title })}>
                  {t('mirror.leave')}
                </button>
              ) : (
                <button type="button" className="btn ghost" onClick={() => onChoose(row.track, undefined)}>
                  {t('mirror.undoLeave')}
                </button>
              )}
            </div>
            {picking === row.track && (
              <TrackPicker
                server={target}
                start={row.title}
                label={t('mirror.searchLabel', { title: row.title })}
                onCancel={() => setPicking(null)}
                onPick={(hit) => {
                  setNamed((now) => ({ ...now, [row.track]: hit }))
                  setPicking(null)
                  onChoose(row.track, hit.id)
                }}
              />
            )}
          </div>
        )
      })}
      {preview.more > 0 && <p className="text-sm text-muted">{t('mirror.more', { count: preview.more })}</p>}
    </div>
  )
}
