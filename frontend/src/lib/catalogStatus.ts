/**
 * Where the catalogue stands, asked once and then every two seconds while a run is under way or waiting (the
 * progress of a full run); quiet otherwise.
 */
import { useCallback, useEffect, useState } from 'react'

import { busy, catalogApi, type CatalogStatus } from '../api/plex'

/** How long to wait between two asks while a run is under way; the tests shorten it. */
export const timing = { poll: 2000 }

/** `server`: the catalogue of that media server (a playlist's), none: the first one connected; `wait`: not yet asked (the
 * playlist that names the server is still on its way). */
export function useCatalogStatus(server?: number | null, wait = false): { status: CatalogStatus | null; failed: boolean; refresh: () => Promise<void> } {
  const [status, setStatus] = useState<CatalogStatus | null>(null)
  const [failed, setFailed] = useState(false)
  const refresh = useCallback(async () => {
    try {
      setStatus(await catalogApi.status(server))
      setFailed(false)
    } catch {
      setFailed(true)
    }
  }, [server])
  useEffect(() => {
    if (!wait) void refresh()
  }, [refresh, wait])
  useEffect(() => {
    if (!busy(status)) return
    const timer = window.setTimeout(() => void refresh(), timing.poll)
    return () => window.clearTimeout(timer)
  }, [status, refresh])
  return { status, failed, refresh }
}

/** How many tracks the catalogue holds, for a question that names them: the status as shown, or (not there yet, the
 * page just opened) asked now, so that a dialog never says 0 for a catalogue that has tracks. */
export async function tracksOf(status: CatalogStatus | null, server?: number | null): Promise<number> {
  if (status) return status.counts.tracks
  try {
    return (await catalogApi.status(server)).counts.tracks
  } catch {
    return 0
  }
}

/** The share of a run done, in whole percent. */
export function percentOf(status: CatalogStatus | null): number {
  return Math.max(0, Math.min(100, Math.floor((status?.running?.progress ?? 0) / 10)))
}
