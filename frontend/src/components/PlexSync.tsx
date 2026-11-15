/**
 * How a playlist stands in Plex: the cloud on the card of the overview (`SyncMark`), and the line in the head of the
 * playlist with "sync now" and, after a failure, "try again" (`SyncLine`). Nothing while Plex is not connected. A
 * playlist whose server takes no playlists from nexmix (Jellyfin, for now) says that it lives in nexmix only.
 */
import { CloudAlert, CloudCheck, CloudOff, HardDrive } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { PlexSync } from '../api/playlists'
import { errorText } from '../lib/errors'

export function SyncMark({ plex }: { plex: PlexSync }) {
  const { t } = useTranslation()
  if (plex.state === 'off') return null
  if (plex.state === 'local')
    return (
      <span className="sync" data-testid="sync-mark">
        <HardDrive aria-hidden />
        {t('sync.card.local')}
      </span>
    )
  if (plex.state === 'synced')
    return (
      <span className="sync" data-testid="sync-mark">
        <CloudCheck aria-hidden />
        {t('sync.card.synced')}
      </span>
    )
  if (plex.state === 'pending')
    return (
      <span className="sync busy" data-testid="sync-mark">
        <span className="spin small" aria-hidden />
        {t('sync.card.pending')}
      </span>
    )
  if (plex.state === 'waiting')
    return (
      <span className="sync waiting" data-testid="sync-mark">
        <CloudOff aria-hidden />
        {t('sync.card.waiting')}
      </span>
    )
  return (
    <span className="sync error" data-testid="sync-mark">
      <CloudAlert aria-hidden />
      {t('sync.card.error')}
    </span>
  )
}

/** `canSync`: the person may change the playlist; only then "sync now" and "try again" (the server says no to anybody else). */
export function SyncLine({
  plex,
  onNow,
  onRetry,
  working,
  canSync = true,
}: {
  plex: PlexSync
  onNow: () => void
  onRetry: () => void
  working: boolean
  canSync?: boolean
}) {
  const { t } = useTranslation()
  if (plex.state === 'off') return null
  if (plex.state === 'local')
    return (
      <span className="sync" data-testid="sync-state">
        <HardDrive aria-hidden />
        {t('sync.local')}
      </span>
    )
  if (plex.state === 'pending')
    return (
      <span className="sync busy" data-testid="sync-state">
        <span className="spin small" aria-hidden />
        {t('sync.pending')}
      </span>
    )
  if (plex.state === 'waiting')
    return (
      <span className="sync waiting" data-testid="sync-state" role="status">
        <CloudOff aria-hidden />
        {t('sync.waiting')}
      </span>
    )
  if (plex.state === 'error')
    return (
      <span className="inline-flex flex-wrap items-center gap-2">
        <span className="sync error" data-testid="sync-state" role="alert">
          <CloudAlert aria-hidden />
          {t('sync.error', { reason: errorText(plex.error ?? 'internal_error') })}
        </span>
        {canSync && (
          <button type="button" className="linkbtn" onClick={onRetry} disabled={working}>
            {t('sync.retry')}
          </button>
        )}
      </span>
    )
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <span className="sync" data-testid="sync-state">
        <CloudCheck aria-hidden />
        {t('sync.synced')}
      </span>
      {canSync && (
        <button type="button" className="linkbtn" onClick={onNow} disabled={working} aria-label={t('sync.nowLabel')}>
          {t('sync.now')}
        </button>
      )}
    </span>
  )
}
