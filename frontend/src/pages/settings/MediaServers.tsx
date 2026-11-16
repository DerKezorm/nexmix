/**
 * Settings, Server, Media servers. The Plex card as it always was (the first Plex server); a card for each Plex
 * server connected beside it, for each Jellyfin server and for each Music Assistant; and "Add a server" with the choice
 * of Plex, Jellyfin or Music Assistant.
 * Disconnecting asks first and says what goes. With nothing connected yet, the Plex card stands first as before. With
 * more than one server, each ready one has "take all playlists over" (``TransferCard``).
 *
 * With more than one server a second row of tabs shows one of them at a time: a tab per server (its name, its kind as
 * a sign), "Add a server" and "Take all over". Its value is the `sub` of the address (the server's number, `add`,
 * `transfer`); an unknown one is the first server. With one server or none there is no row, and the cards stand one
 * below the other as before.
 */
import { Disc3, MonitorPlay, Plus, Repeat, Speaker } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { busy, catalogApi } from '../../api/plex'
import { jellyfinApi, maApi, serversApi, type JellyfinServer, type MaServer, type MediaServer } from '../../api/servers'
import { useConfirm } from '../../components/useConfirm'
import { tracksOf, useCatalogStatus } from '../../lib/catalogStatus'
import { iso } from '../../lib/say'
import { kindName } from '../../lib/servers'
import { JellyfinCard, JellyfinConnect, ServerCatalog } from './JellyfinCard'
import { MaCard, MaConnect } from './MaCard'
import { PlexCard } from './PlexCard'
import { TransferCard } from './TransferCard'
import { Button, Card, Feedback, TabRow, useAction, type Tab } from './ui'

/** The sign of a kind of server in the second row. */
const KIND_ICON = { plex: Disc3, jellyfin: MonitorPlay, music_assistant: Speaker }

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
          onClick={async () =>
            ask({
              title: t('servers.disconnectTitle', { name: iso(server.name) }),
              text: t('servers.disconnectText', { tracks: (await tracksOf(status, server.id)).toLocaleString(i18n.language) }),
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

export function MediaServers({ sub = null, onSub }: { sub?: string | null; onSub?: (sub: string) => void }) {
  const { t } = useTranslation()
  // Without an address to keep it (a card shown by itself), the second row keeps its choice here.
  const [ownSub, setOwnSub] = useState<string | null>(null)
  const asked = onSub ? sub : ownSub
  const choose = onSub ?? setOwnSub
  const [servers, setServers] = useState<MediaServer[] | null>(null)
  const [jellyfins, setJellyfins] = useState<JellyfinServer[]>([])
  const [assistants, setAssistants] = useState<MaServer[]>([])
  const [adding, setAdding] = useState<Adding>(null)
  // The Plex card reads its server anew when a server comes or goes.
  const [round, setRound] = useState(0)

  const load = useCallback(async (): Promise<MediaServer[]> => {
    try {
      // A Music Assistant that cannot be listed leaves the other cards as they are.
      const [all, jelly, ma] = await Promise.all([serversApi.list(), jellyfinApi.list(), maApi.list().catch(() => null)])
      const found = Array.isArray(all?.servers) ? all.servers : []
      setServers(found)
      setJellyfins(Array.isArray(jelly?.servers) ? jelly.servers : [])
      setAssistants(Array.isArray(ma?.servers) ? ma.servers : [])
      return found
    } catch {
      setServers([])
      setJellyfins([])
      setAssistants([])
      return []
    }
  }, [])
  useEffect(() => {
    void load()
  }, [load])

  const changed = async () => {
    const found = await load()
    setRound((now) => now + 1)
    return found
  }
  const refresh = async () => {
    await changed()
  }
  // A server just connected: its tab is the one shown, its card goes on with what it still needs (a user, providers).
  const connected = async () => {
    const before = new Set((servers ?? []).filter((one) => one.active).map((one) => one.id))
    setAdding(null)
    const fresh = (await changed()).find((one) => one.active && !before.has(one.id))
    if (fresh) choose(String(fresh.id))
  }

  const active = (servers ?? []).filter((one) => one.active)
  const plexes = active.filter((one) => one.kind === 'plex')
  // The Plex card stands for the first Plex server; with nothing connected at all it is the way in, as before.
  const plexCard = plexes.length > 0 || active.length === 0
  const others = plexes.slice(1)
  // "Take all over" where a playlist could come from another server.
  const targets = active.length > 1 ? active.filter((one) => one.ready && one.capabilities.playlists) : []

  // The second row, with more than one server: what it offers, and the one shown (an unknown value is the first
  // server).
  const row: Tab<string>[] =
    active.length > 1
      ? [
          ...active.map((one) => ({ value: String(one.id), label: one.name || kindName(one.kind), icon: KIND_ICON[one.kind] ?? Disc3, title: kindName(one.kind) })),
          { value: 'add', label: t('servers.addTitle'), icon: Plus },
          ...(targets.length > 0 ? [{ value: 'transfer', label: t('servers.transferTab'), icon: Repeat }] : []),
        ]
      : []
  const shown = row.length === 0 ? null : row.some((tab) => tab.value === asked) ? (asked as string) : row[0].value
  const all = shown === null
  const shows = (id: number) => all || shown === String(id)
  const firstPlex = plexes[0]

  return (
    <>
      {row.length > 0 && <TabRow under tabs={row} active={shown as string} onChange={(value) => value !== shown && choose(value)} label={t('servers.part')} />}
      {plexCard && (all || (firstPlex && shown === String(firstPlex.id))) && <PlexCard key={round} onChanged={refresh} />}
      {others.filter((server) => shows(server.id)).map((server) => (
        <OtherPlexCard key={server.id} server={server} onChanged={refresh} />
      ))}
      {jellyfins.filter((server) => shows(server.id)).map((server) => (
        <JellyfinCard key={server.id} server={server} onChanged={refresh} />
      ))}
      {assistants.filter((server) => shows(server.id)).map((server) => (
        <MaCard key={server.id} server={server} onChanged={refresh} />
      ))}
      {(all || shown === 'transfer') &&
        targets.map((server) => <TransferCard key={`transfer-${server.id}`} target={server} others={active.filter((one) => one.id !== server.id)} />)}
      {(all || shown === 'add') && adding === 'plex' && (
        <PlexCard alongside onDone={() => void connected()} />
      )}
      {(all || shown === 'add') && adding === 'jellyfin' && (
        <JellyfinConnect onConnected={connected} onCancel={() => setAdding(null)} />
      )}
      {(all || shown === 'add') && adding === 'music_assistant' && (
        <MaConnect onConnected={connected} onCancel={() => setAdding(null)} />
      )}
      {servers !== null && (all || shown === 'add') && adding !== 'plex' && adding !== 'jellyfin' && adding !== 'music_assistant' && (
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
