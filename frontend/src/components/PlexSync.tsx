/**
 * How a playlist stands on its media server (Plex or Jellyfin): the cloud on the card of the overview (`SyncMark`),
 * and the line in the head of the playlist with "sync now" and, after a failure, "try again" (`SyncLine`). Nothing
 * while no server is connected.
 */
import { CloudAlert, CloudCheck, CloudOff } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { PlexSync } from '../api/playlists'
import { errorText } from '../lib/errors'

/** The name of the playlist's server kind, as the texts say it. */
// eslint-disable-next-line react-refresh/only-export-components
export function serverName(kind: string | null | undefined): string {
  return kind === 'jellyfin' ? 'Jellyfin' : 'Plex'
}

export function SyncMark({ plex }: { plex: PlexSync }) {
  const { t } = useTranslation()
  const server = serverName(plex.kind)
  if (plex.state === 'off') return null
  if (plex.state === 'synced')
    return (
      <span className="sync" data-testid="sync-mark">
        <CloudCheck aria-hidden />
        {t('sync.card.synced', { server })}
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
        {t('sync.card.waiting', { server })}
      </span>
    )
  return (
    <span className="sync error" data-testid="sync-mark">
      <CloudAlert aria-hidden />
      {t('sync.card.error', { server })}
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
  const server = serverName(plex.kind)
  if (plex.state === 'off') return null
  if (plex.state === 'pending')
    return (
      <span className="sync busy" data-testid="sync-state">
        <span className="spin small" aria-hidden />
        {t('sync.pending', { server })}
      </span>
    )
  if (plex.state === 'waiting')
    return (
      <span className="sync waiting" data-testid="sync-state" role="status">
        <CloudOff aria-hidden />
        {t('sync.waiting', { server })}
      </span>
    )
  if (plex.state === 'error')
    return (
      <span className="inline-flex flex-wrap items-center gap-2">
        <span className="sync error" data-testid="sync-state" role="alert">
          <CloudAlert aria-hidden />
          {t('sync.error', { server, reason: errorText(plex.error ?? 'internal_error') })}
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
        {t('sync.synced', { server })}
      </span>
      {canSync && (
        <button type="button" className="linkbtn" onClick={onNow} disabled={working} aria-label={t('sync.nowLabel', { server })}>
          {t('sync.now')}
        </button>
      )}
    </span>
  )
}
