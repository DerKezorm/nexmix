/**
 * The media servers as everybody signed in may see them (`/api/v1/servers`): what each can do decides what the
 * editor offers (no mood filter where a server has no moods). Asked once per page.
 */
import { useEffect, useState } from 'react'

import { serversApi, type MediaServer } from '../api/servers'

export function useServers(): MediaServer[] | null {
  const [servers, setServers] = useState<MediaServer[] | null>(null)
  useEffect(() => {
    let gone = false
    serversApi
      .list()
      .then((found) => !gone && setServers(Array.isArray(found?.servers) ? found.servers : []))
      .catch(() => !gone && setServers([]))
    return () => {
      gone = true
    }
  }, [])
  return servers
}

/** The name of a kind of server, as people know it. */
export function kindName(kind: string | null | undefined): string {
  if (kind === 'jellyfin') return 'Jellyfin'
  if (kind === 'music_assistant') return 'Music Assistant'
  return 'Plex'
}
