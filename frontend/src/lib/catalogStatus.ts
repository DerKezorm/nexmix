/**
 * Where the catalogue stands, asked once and then every two seconds while a run is under way or waiting (the
 * progress of a full run); quiet otherwise.
 */
import { useCallback, useEffect, useState } from 'react'

import { busy, catalogApi, type CatalogStatus } from '../api/plex'

/** How long to wait between two asks while a run is under way; the tests shorten it. */
export const timing = { poll: 2000 }

export function useCatalogStatus(): { status: CatalogStatus | null; failed: boolean; refresh: () => Promise<void> } {
  const [status, setStatus] = useState<CatalogStatus | null>(null)
  const [failed, setFailed] = useState(false)
  const refresh = useCallback(async () => {
    try {
      setStatus(await catalogApi.status())
      setFailed(false)
    } catch {
      setFailed(true)
    }
  }, [])
  useEffect(() => {
    void refresh()
  }, [refresh])
  useEffect(() => {
    if (!busy(status)) return
    const timer = window.setTimeout(() => void refresh(), timing.poll)
    return () => window.clearTimeout(timer)
  }, [status, refresh])
  return { status, failed, refresh }
}

/** The share of a run done, in whole percent. */
export function percentOf(status: CatalogStatus | null): number {
  return Math.max(0, Math.min(100, Math.floor((status?.running?.progress ?? 0) / 10)))
}
