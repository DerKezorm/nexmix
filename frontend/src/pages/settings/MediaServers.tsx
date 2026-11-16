/**
 * Settings, Server: the media servers. The Plex card as it always was (the first Plex server); a card for each Plex
 * server connected beside it, for each Jellyfin server and for each Music Assistant; and "Add a server" with the choice
 * of Plex, Jellyfin or Music Assistant.
 * Disconnecting asks first and says what goes. With nothing connected yet, the Plex card stands first as before. With
 * more than one server, each ready one has "take all playlists over" (``TransferCard``).
 */
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { busy, catalogApi } from '../../api/plex'
import { jellyfinApi, maApi, serversApi, type JellyfinServer, type MaServer, type MediaServer } from '../../api/servers'
import { useConfirm } from '../../components/useConfirm'
import { useCatalogStatus } from '../../lib/catalogStatus'
import { iso } from '../../lib/say'
import { JellyfinCard, JellyfinConnect, ServerCatalog } from './JellyfinCard'
import { MaCard, MaConnect } from './MaCard'
import { PlexCard } from './PlexCard'
import { TransferCard } from './TransferCard'
import { Button, Card, Feedback, useAction } from './ui'

/** A Plex server connected beside the first one: its catalogue, sync now, disconnect. */
function OtherPlexCard({ server, onChanged }: { server: MediaServer; onChanged: () => Promise<void> }) {
  const { t, i18n } = useTranslation()
  const { status, refresh } = useCatalogStatus(server.id)
  const action = useAction()
  const { ask, dialog } = useConfirm()
  const full = status?.running?.kind === 'full' || status?.queued === 'full'
  return (
    <Card id={`plex-${server.id}`} title={t('servers.plexTitle', { name: iso(server.name) })} text={t('servers.plexText')}>
      <dl className="kv">
        <dt>{t('servers.library')}</dt>
        <dd>{server.library ?? '…'}</dd>
      </dl>
      <ServerCatalog status={status} />
      <div className="flex flex-wrap gap-2" style={{ marginTop: 14 }}>
        <Button
          onClick={() =>
            void action.run(async () => {
              await catalogApi.sync(server.id)
              await refresh()
            }, t('plex.syncStarted'))
          }
          busy={action.busy}
          disabled={busy(status) && full}
        >
          {t('plex.syncNow')}
        </Button>
        <Button
          ghost
          disabled={action.busy}
          onClick={() =>
            ask({
              title: t('servers.disconnectTitle', { name: iso(server.name) }),
              text: t('servers.disconnectText', { tracks: (status?.counts.tracks ?? 0).toLocaleString(i18n.language) }),
              confirm: t('servers.disconnect'),
              run: async () => {
                await serversApi.disconnect(server.id)
                await onChanged()
              },
            })
          }
        >
          {t('servers.disconnect')}
        </Button>
      </div>
      <Feedback problem={action.problem} done={action.done} values={action.values} />
      {dialog}
    </Card>
  )
}

type Adding = 'choose' | 'plex' | 'jellyfin' | 'music_assistant' | null

export function MediaServers() {
  const { t } = useTranslation()
  const [servers, setServers] = useState<MediaServer[] | null>(null)
  const [jellyfins, setJellyfins] = useState<JellyfinServer[]>([])
  const [assistants, setAssistants] = useState<MaServer[]>([])
  const [adding, setAdding] = useState<Adding>(null)
  // The Plex card reads its server anew when a server comes or goes.
  const [round, setRound] = useState(0)

  const load = useCallback(async () => {
    try {
      // A Music Assistant that cannot be listed leaves the other cards as they are.
      const [all, jelly, ma] = await Promise.all([serversApi.list(), jellyfinApi.list(), maApi.list().catch(() => null)])
      setServers(Array.isArray(all?.servers) ? all.servers : [])
      setJellyfins(Array.isArray(jelly?.servers) ? jelly.servers : [])
      setAssistants(Array.isArray(ma?.servers) ? ma.servers : [])
    } catch {
      setServers([])
      setJellyfins([])
      setAssistants([])
    }
  }, [])
  useEffect(() => {
    void load()
  }, [load])

  const changed = async () => {
    await load()
    setRound((now) => now + 1)
  }

  const active = (servers ?? []).filter((one) => one.active)
  const plexes = active.filter((one) => one.kind === 'plex')
  // The Plex card stands for the first Plex server; with nothing connected at all it is the way in, as before.
  const plexCard = plexes.length > 0 || active.length === 0
  const others = plexes.slice(1)
  // "Take all over" where a playlist could come from another server.
  const targets = active.length > 1 ? active.filter((one) => one.ready && one.capabilities.playlists) : []

  return (
    <>
      {plexCard && <PlexCard key={round} />}
      {others.map((server) => (
        <OtherPlexCard key={server.id} server={server} onChanged={changed} />
      ))}
      {jellyfins.map((server) => (
        <JellyfinCard key={server.id} server={server} onChanged={changed} />
      ))}
      {assistants.map((server) => (
        <MaCard key={server.id} server={server} onChanged={changed} />
      ))}
      {targets.map((server) => (
        <TransferCard key={`transfer-${server.id}`} target={server} others={active.filter((one) => one.id !== server.id)} />
      ))}
      {adding === 'plex' && (
        <PlexCard
          alongside
          onDone={() => {
            setAdding(null)
            void changed()
          }}
        />
      )}
      {adding === 'jellyfin' && (
        <JellyfinConnect
          onConnected={async () => {
            setAdding(null)
            await changed()
          }}
          onCancel={() => setAdding(null)}
        />
      )}
      {adding === 'music_assistant' && (
        <MaConnect
          onConnected={async () => {
            setAdding(null)
            await changed()
          }}
          onCancel={() => setAdding(null)}
        />
      )}
      {servers !== null && adding !== 'plex' && adding !== 'jellyfin' && adding !== 'music_assistant' && (
        <Card id="servers-add" title={t('servers.addTitle')} text={t('servers.addText')}>
          <div className="flex flex-wrap gap-2" data-testid="servers-add">
            <Button onClick={() => setAdding('plex')}>{t('servers.addPlex')}</Button>
            <Button onClick={() => setAdding('jellyfin')}>{t('servers.addJellyfin')}</Button>
            <Button onClick={() => setAdding('music_assistant')}>{t('servers.addMa')}</Button>
          </div>
        </Card>
      )}
    </>
  )
}
