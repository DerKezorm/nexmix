/**
 * The frame of the settings: the title, the two parts in one `.seg` (Server for the operator, My account for
 * everybody), then the tabs of the part as a row of round tabs (`TabRow`, as in the other nex apps), and only the cards
 * of the tab chosen below them. The tab stands in the address (`?tab=signin`), so a link can point at one; a value of
 * an older version leads to the tab that holds it now (`?tab=plex` to the media servers), and the address says so.
 */
import { useEffect, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate, useSearchParams } from 'react-router-dom'

import { PageHead } from '../../components/PageHead'
import { useAuth } from '../../state/auth'
import { TabRow, type Tab } from './ui'

export type SettingsPart = 'server' | 'account'

export function SettingsFrame<T extends string>({ part, tabs, active, sub, children }: { part: SettingsPart; tabs: Tab<T>[]; active: T; sub?: ReactNode; children: ReactNode }) {
  const { t } = useTranslation()
  const { me } = useAuth()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const asked = params.get('tab')
  // An older or unknown value: the address names the tab that is shown.
  useEffect(() => {
    if (asked !== null && asked !== active) setParams({ tab: active }, { replace: true })
  }, [asked, active, setParams])
  const operator = me?.role === 'operator'
  const parts: { value: SettingsPart; label: string; to: string }[] = [
    { value: 'server', label: t('settings.tabs.server'), to: '/settings' },
    { value: 'account', label: t('settings.tabs.account'), to: '/account' },
  ]
  const label = part === 'server' ? t('settings.tabs.server') : t('settings.tabs.account')
  return (
    <div className="view">
      <PageHead title={t('settings.title')} text={part === 'server' ? t('settings.serverText') : undefined} />
      {operator && (
        <div className="seg mb-[18px]" role="tablist" aria-label={t('settings.title')}>
          {parts.map(({ value, label: name, to }) => (
            <button key={value} type="button" role="tab" aria-selected={value === part} className={value === part ? 'on' : undefined} onClick={() => value !== part && navigate(to)}>
              {name}
            </button>
          ))}
        </div>
      )}
      {tabs.length > 1 && (
        <div className="settings-tabs">
          <TabRow tabs={tabs} active={active} label={label} onChange={(value) => value !== active && setParams({ tab: value }, { replace: true })} />
          {sub}
        </div>
      )}
      <div className="settings" role={tabs.length > 1 ? 'tabpanel' : undefined} aria-label={tabs.length > 1 ? tabs.find((tab) => tab.value === active)?.label : undefined}>
        {children}
      </div>
    </div>
  )
}
