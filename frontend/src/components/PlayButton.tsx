import type { CSSProperties } from 'react'
import { useTranslation } from 'react-i18next'

import { play, usePreview } from '../lib/preview'

const PLAY = (
  <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden>
    <path d="M8 5.5v13l11-6.5z" />
  </svg>
)
const STOP = (
  <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden>
    <rect x="7" y="7" width="10" height="10" rx="1.5" />
  </svg>
)

/** ▶ for a preview of a track, ■ while it plays, the ring filling with the seconds. Not there while listening in is
 * switched off. */
export function PlayButton({ track, title }: { track: number; title: string }) {
  const { t } = useTranslation()
  const now = usePreview()
  if (now.allowed === false) return null
  const on = now.track === track
  return (
    <button
      type="button"
      className={`round play${on ? ' on' : ''}`}
      title={t('preview.button')}
      aria-label={on ? t('preview.stop', { title }) : t('preview.play', { title })}
      aria-pressed={on}
      data-testid="play"
      data-track={track}
      style={on ? ({ '--p': now.progress } as CSSProperties) : undefined}
      onClick={(event) => {
        event.stopPropagation()
        event.preventDefault()
        play(track)
      }}
    >
      {on ? STOP : PLAY}
      <span className="ring" aria-hidden />
    </button>
  )
}
