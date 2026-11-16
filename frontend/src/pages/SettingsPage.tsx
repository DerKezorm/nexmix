/**
 * Settings, the server part, for the operator only, in tabs as in the other nex apps: Media servers (with a second row,
 * one tab per server, "Add a server" and "Take all over"), Sign-in (with "Ready for the internet?" above its cards, as
 * in the other nex apps), Accounts, Guests, Mail, Web Push, Backup, Languages, Log, API. Each tab shows only its
 * cards; the tab stands in the address (`?tab=signin`, `?tab=servers&sub=…`). Everything personal is the other part,
 * "My account" (`AccountPage`). A member has no use for this page and is sent to their account.
 */
import { useTranslation } from 'react-i18next'
import { Navigate, useSearchParams } from 'react-router-dom'

import { useAuth } from '../state/auth'
import { MediaServers } from './settings/MediaServers'
import { PushServerCard } from './settings/PushCards'
import { ReadinessCard } from './settings/ReadinessCards'
import { GuestsCard } from './settings/GuestsCard'
import { AccountsCard, ApiKeysCard, BackupsCard, InterfaceCard, LanguagesCard, LogCard, MailCard, useServerSettings, type Server } from './settings/ServerCards'
import { SettingsFrame } from './settings/SettingsFrame'
import { SignInPart } from './settings/SignInCards'
import { StorageCard } from './settings/StorageCards'
import { SERVER_ICONS, SERVER_TABS, serverTab, type ServerTab } from './settings/tabs'
import type { Tab } from './settings/ui'

export function SettingsPage() {
  const { t } = useTranslation()
  const { me } = useAuth()
  const server = useServerSettings()
  const [params, setParams] = useSearchParams()
  if (!me) return null
  if (me.role !== 'operator') return <Navigate to="/account" replace />
  const active = serverTab(params.get('tab'))
  const tabs: Tab<ServerTab>[] = SERVER_TABS.map((value) => ({ value, label: t(`settings.parts.${value}`), icon: SERVER_ICONS[value] }))
  return (
    <SettingsFrame part="server" tabs={tabs} active={active}>
      {active === 'servers' ? (
        <MediaServers sub={params.get('sub')} onSub={(sub) => setParams({ tab: 'servers', sub }, { replace: true })} />
      ) : (
        <ServerPart part={active} server={server} />
      )}
    </SettingsFrame>
  )
}

/** One tab of the server, but the media servers. */
function ServerPart({ part, server }: { part: Exclude<ServerTab, 'servers'>; server: Server }) {
  switch (part) {
    case 'signin':
      // As in the other nex apps: nexmix's own look at the server first (its points speak of the cards below), then
      // the sign-in.
      return (
        <>
          <ReadinessCard />
          <SignInPart server={server} />
        </>
      )
    case 'accounts':
      return <AccountsCard />
    case 'guests':
      return <GuestsCard server={server} />
    case 'mail':
      return <MailCard server={server} />
    case 'push':
      return <PushServerCard />
    case 'backup':
      return (
        <>
          <BackupsCard server={server} />
          <StorageCard />
        </>
      )
    case 'languages':
      return <LanguagesCard />
    case 'log':
      return <LogCard />
    case 'api':
      return (
        <>
          <InterfaceCard server={server} />
          <ApiKeysCard server={server} />
        </>
      )
  }
}
