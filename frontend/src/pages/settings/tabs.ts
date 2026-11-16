/**
 * The tabs of the settings and of "My account" as they stand in the address: `?tab=…` for a tab, and for the media
 * servers `?tab=servers&sub=…` for one of them. A value of an older version (`?tab=plex`, the scrolling parts before
 * the tabs) leads to the tab that holds it now; an unknown one to the first tab.
 */
import { BellRing, HardDrive, KeyRound, Languages, Mail, MonitorSmartphone, Palette, Plug, ScrollText, Server, ShieldCheck, User, UserRoundPlus, Users } from 'lucide-react'

export const SERVER_TABS = ['servers', 'signin', 'accounts', 'guests', 'mail', 'push', 'backup', 'languages', 'log', 'api'] as const
export type ServerTab = (typeof SERVER_TABS)[number]
export const SERVER_ICONS = { servers: Server, signin: KeyRound, accounts: Users, guests: UserRoundPlus, mail: Mail, push: BellRing, backup: HardDrive, languages: Languages, log: ScrollText, api: Plug }

export const ACCOUNT_TABS = ['profile', 'security', 'looks', 'devices'] as const
export type AccountTab = (typeof ACCOUNT_TABS)[number]
export const ACCOUNT_ICONS = { profile: User, security: ShieldCheck, looks: Palette, devices: MonitorSmartphone }

/** What the parts were called before they were tabs, and where they are now. */
const OLD_SERVER: Record<string, ServerTab> = { plex: 'servers' }
const OLD_ACCOUNT: Record<string, AccountTab> = { personal: 'profile' }

/** The tab of the server settings an address asks for. */
export function serverTab(asked: string | null): ServerTab {
  if (asked && (SERVER_TABS as readonly string[]).includes(asked)) return asked as ServerTab
  return asked && Object.hasOwn(OLD_SERVER, asked) ? OLD_SERVER[asked] : SERVER_TABS[0]
}

/** The tab of "My account" an address asks for; back from a sign-in provider (`linked`, `error`) it is Security,
 * where the providers are. */
export function accountTab(params: URLSearchParams): AccountTab {
  const asked = params.get('tab')
  if (asked && (ACCOUNT_TABS as readonly string[]).includes(asked)) return asked as AccountTab
  if (asked && Object.hasOwn(OLD_ACCOUNT, asked)) return OLD_ACCOUNT[asked]
  return params.get('linked') || params.get('error') ? 'security' : ACCOUNT_TABS[0]
}

/** The address of a tab, with the rest of what the address held (`linked`, `error`) left out. */
export function tabParams(tab: string, sub?: string | null): Record<string, string> {
  return sub ? { tab, sub } : { tab }
}
