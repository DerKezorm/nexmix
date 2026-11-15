/**
 * Settings, the server part, for the operator only: the cards one below the other (the media servers, the look at the server, the
 * sign-in cards, accounts, mail, Web Push, backup, languages, log, API). Everything personal is the other part, "My account"
 * (`AccountPage`). A part can be named in the address (`?tab=signin`): the page scrolls to it. A member has no use for
 * this page and is sent to their account.
 */
import { Navigate } from 'react-router-dom'

import { useAuth } from '../state/auth'
import { MediaServers } from './settings/MediaServers'
import { PushServerCard } from './settings/PushCards'
import { ReadinessCard } from './settings/ReadinessCards'
import { GuestsCard } from './settings/GuestsCard'
import { AccountsCard, ApiKeysCard, BackupsCard, InterfaceCard, LanguagesCard, LogCard, MailCard, useServerSettings } from './settings/ServerCards'
import { Part, SettingsFrame } from './settings/SettingsFrame'
import { SignInPart } from './settings/SignInCards'
import { StorageCard } from './settings/StorageCards'

export function SettingsPage() {
  const { me } = useAuth()
  const server = useServerSettings()
  if (!me) return null
  if (me.role !== 'operator') return <Navigate to="/account" replace />
  return (
    <SettingsFrame part="server">
      <Part name="plex">
        <MediaServers />
      </Part>
      <Part name="signin">
        <SignInPart server={server} />
      </Part>
      <Part name="accounts">
        <AccountsCard />
      </Part>
      <Part name="guests">
        <GuestsCard server={server} />
      </Part>
      <ReadinessCard />
      <Part name="mail">
        <MailCard server={server} />
      </Part>
      <Part name="push">
        <PushServerCard />
      </Part>
      <Part name="backup">
        <BackupsCard server={server} />
        <StorageCard />
      </Part>
      <Part name="languages">
        <LanguagesCard />
      </Part>
      <Part name="log">
        <LogCard />
      </Part>
      <Part name="api">
        <InterfaceCard server={server} />
        <ApiKeysCard server={server} />
      </Part>
    </SettingsFrame>
  )
}
