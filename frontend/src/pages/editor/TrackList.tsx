/**
 * The entries of the playlist in their order: grip, number, cover, title, artist, album and year, who added it, length,
 * "suggestions for this track", ▶ for a preview, and the button that takes it out. Sorting by dragging (a mouse anywhere on the row or the grip; a finger on the grip,
 * or anywhere after holding the row a moment) and with Alt and the arrow keys on a row that has the focus.
 */
import { X } from 'lucide-react'
import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

import type { Entry } from '../../api/playlists'
import { Cover } from '../../components/Mosaic'
import { PersonFace } from '../../components/People'
import { PlayButton } from '../../components/PlayButton'
import { clock, trackLine } from '../../lib/music'
import { dropAfter, HOLD_MS, SLOP } from '../../lib/reorder'

const GRIP = (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden>
    <circle cx="9" cy="6" r="1.6" />
    <circle cx="15" cy="6" r="1.6" />
    <circle cx="9" cy="12" r="1.6" />
    <circle cx="15" cy="12" r="1.6" />
    <circle cx="9" cy="18" r="1.6" />
    <circle cx="15" cy="18" r="1.6" />
  </svg>
)

const SIMILAR = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" aria-hidden>
    <circle cx="8" cy="12" r="4.5" />
    <circle cx="16" cy="12" r="4.5" />
  </svg>
)

type Drag = { from: number; over: number; pointer: number }

export function TrackList({
  items,
  canEdit,
  fresh,
  focusEntry,
  onRemove,
  onMove,
  onSimilar,
  empty,
  server = 'Plex',
  mirrorNames = {},
}: {
  items: Entry[]
  /** The name of the playlist's server, for the hints of entries it lost or refused. */
  server?: string
  /** The names of the servers of the playlist's mirrors: an entry without a track on one says so. */
  mirrorNames?: Record<number, string>
  canEdit: boolean
  fresh: Set<number>
  focusEntry: number | null
  onRemove: (entry: Entry, index: number) => void
  onMove: (entry: Entry, after: number | null) => void
  /** "Suggestions for this track". */
  onSimilar?: (entry: Entry) => void
  empty: ReactNode
}) {
  const { t } = useTranslation()
  const box = useRef<HTMLDivElement>(null)
  const [drag, setDrag] = useState<Drag | null>(null)
  const dragRef = useRef<Drag | null>(null)
  const pending = useRef<{ index: number; x: number; y: number; pointer: number; timer: number; touch: boolean } | null>(null)
  const latest = useRef({ items, onMove })
  latest.current = { items, onMove }

  useEffect(() => {
    if (focusEntry === null) return
    box.current?.querySelector<HTMLElement>(`[data-entry="${focusEntry}"]`)?.focus()
  }, [focusEntry, items])

  const overAt = (y: number) => {
    const rows = Array.from(box.current?.querySelectorAll<HTMLElement>('.trow') ?? [])
    for (let index = 0; index < rows.length; index++) {
      const rect = rows[index].getBoundingClientRect()
      if (y < rect.top + rect.height / 2) return index
    }
    return rows.length
  }

  const begin = (index: number, pointer: number) => {
    const next = { from: index, over: index, pointer }
    dragRef.current = next
    setDrag(next)
  }

  // While dragging: the page follows the pointer, a finger does not scroll the page, and letting go places the entry.
  useEffect(() => {
    if (!drag) return
    const scroller = box.current?.closest<HTMLElement>('.col-body') ?? null
    const move = (event: PointerEvent) => {
      const now = dragRef.current
      if (!now || event.pointerId !== now.pointer) return
      event.preventDefault()
      if (scroller) {
        const rect = scroller.getBoundingClientRect()
        if (event.clientY < rect.top + 40) scroller.scrollTop -= 14
        else if (event.clientY > rect.bottom - 40) scroller.scrollTop += 14
      }
      const over = overAt(event.clientY)
      if (over !== now.over) {
        const next = { ...now, over }
        dragRef.current = next
        setDrag(next)
      }
    }
    const end = (event: PointerEvent) => {
      const now = dragRef.current
      if (!now || event.pointerId !== now.pointer) return
      dragRef.current = null
      setDrag(null)
      if (event.type === 'pointercancel') return
      const { items: list, onMove: move } = latest.current
      const after = dropAfter(list, now.from, now.over)
      if (after !== undefined) move(list[now.from], after)
    }
    const still = (event: TouchEvent) => event.preventDefault()
    window.addEventListener('pointermove', move, { passive: false })
    window.addEventListener('pointerup', end)
    window.addEventListener('pointercancel', end)
    document.addEventListener('touchmove', still, { passive: false })
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', end)
      window.removeEventListener('pointercancel', end)
      document.removeEventListener('touchmove', still)
    }
  }, [drag])

  // A press that may become a drag: a mouse that moves a few pixels, a finger that holds still for a moment.
  useEffect(() => {
    const move = (event: PointerEvent) => {
      const press = pending.current
      if (!press || event.pointerId !== press.pointer) return
      const far = Math.abs(event.clientX - press.x) > SLOP || Math.abs(event.clientY - press.y) > SLOP
      if (!far) return
      window.clearTimeout(press.timer)
      pending.current = null
      // A finger that moves before the moment is up scrolls the page; a mouse that moves drags.
      if (!press.touch) begin(press.index, press.pointer)
    }
    const up = (event: PointerEvent) => {
      const press = pending.current
      if (!press || event.pointerId !== press.pointer) return
      window.clearTimeout(press.timer)
      pending.current = null
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', up)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      window.removeEventListener('pointercancel', up)
    }
  }, [])

  const press = (index: number) => (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!canEdit || event.button !== 0 || dragRef.current) return
    const target = event.target as HTMLElement
    if (target.closest('button, a, input')) return
    if (target.closest('.grip')) {
      event.preventDefault()
      begin(index, event.pointerId)
      return
    }
    const touch = event.pointerType !== 'mouse'
    const timer = touch ? window.setTimeout(() => {
      const held = pending.current
      pending.current = null
      if (held) begin(held.index, held.pointer)
    }, HOLD_MS) : 0
    pending.current = { index, x: event.clientX, y: event.clientY, pointer: event.pointerId, timer, touch }
  }

  const keys = (index: number) => (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!canEdit || !event.altKey || (event.key !== 'ArrowUp' && event.key !== 'ArrowDown')) return
    event.preventDefault()
    if (event.key === 'ArrowUp' && index > 0) onMove(items[index], index >= 2 ? items[index - 2].id : null)
    if (event.key === 'ArrowDown' && index < items.length - 1) onMove(items[index], items[index + 1].id)
  }

  if (!items.length) return <>{empty}</>
  return (
    <div ref={box} data-testid="tracks">
      {items.map((entry, index) => {
        const name = entry.added_by ? entry.added_by.display_name || entry.added_by.name : entry.added_by_name
        const marks = [
          'trow',
          fresh.has(entry.id) ? 'fresh' : '',
          (entry.missing && !entry.from_plex) || entry.plex_hint ? 'missing' : '',
          drag?.from === index ? 'dragging' : '',
          drag && drag.over === index && drag.over !== drag.from && drag.over !== drag.from + 1 ? 'over' : '',
          drag && index === items.length - 1 && drag.over === items.length && drag.from !== items.length - 1 ? 'over-end' : '',
        ]
        return (
          <div
            key={entry.id}
            className={marks.filter(Boolean).join(' ')}
            data-entry={entry.id}
            data-testid="track-row"
            tabIndex={0}
            aria-label={t('editor.rowLabel', { number: index + 1, title: entry.title })}
            onPointerDown={press(index)}
            onKeyDown={keys(index)}
          >
            <span className="grip" title={canEdit ? t('editor.grip') : undefined}>
              {canEdit ? GRIP : null}
            </span>
            <span className="nr">{index + 1}</span>
            <Cover album={entry.album_id} has={entry.cover} />
            <div className="min-w-0">
              <div className="t" dir="auto">
                {entry.title}
              </div>
              <div className="s" dir="auto">
                {entry.plex_hint
                  ? t(entry.plex_hint === 'refused' ? 'editor.plexRefused' : entry.plex_hint === 'missing' ? 'editor.plexMissing' : 'editor.plexElsewhere', { server })
                  : entry.missing && !entry.from_plex
                    ? t('editor.missing')
                    : trackLine(entry.artist, entry.album, entry.year)}
              </div>
              {(entry.absent ?? []).length > 0 && (
                <div className="s absent" data-testid="entry-absent">
                  {t('mirror.notOn', { server: (entry.absent ?? []).map((one) => mirrorNames[one] ?? '').filter(Boolean).join(', ') })}
                </div>
              )}
            </div>
            <div className="acts">
              {name && (
                <span className="by">
                  <PersonFace person={entry.added_by ?? { id: null, name, display_name: name, avatar: null }} />
                  <span className="nm">{name}</span>
                </span>
              )}
              <span className="dur">{clock(entry.duration)}</span>
              {!entry.missing && onSimilar && (
                <button type="button" className="round similar" title={t('suggest.similarButton')} aria-label={t('suggest.similarTrack', { title: entry.title })} onClick={() => onSimilar(entry)}>
                  {SIMILAR}
                </button>
              )}
              {!entry.missing && <PlayButton track={entry.track} title={entry.title} />}
              {canEdit && (
                <button type="button" className="round rm" title={t('editor.remove')} aria-label={t('editor.removeTrack', { title: entry.title })} onClick={() => onRemove(entry, index)}>
                  <X aria-hidden />
                </button>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
