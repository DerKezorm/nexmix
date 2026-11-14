/**
 * The frame of the settings: the title, the two parts in one `.seg` (Server for the operator, My account for
 * everybody) and the cards of the part below it. A part inside a page is a link by name (`?tab=signin`): the page
 * scrolls to it.
 */
import { useEffect, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate, useSearchParams } from 'react-router-dom'

import { PageHead } from '../../components/PageHead'
import { useAuth } from '../../state/auth'

export type SettingsPart = 'server' | 'account'

/** Scrolls to the card named in the address (`?tab=`), once the page is drawn. */
function useScrollToPart(): void {
  const [params] = useSearchParams()
  const asked = params.get('tab')
  useEffect(() => {
    if (!asked) return
    const place = document.getElementById(`part-${asked}`)
    if (place && typeof place.scrollIntoView === 'function') place.scrollIntoView({ block: 'start' })
  }, [asked])
}

export function SettingsFrame({ part, children }: { part: SettingsPart; children: ReactNode }) {
  const { t } = useTranslation()
  const { me } = useAuth()
  const navigate = useNavigate()
  useScrollToPart()
  const operator = me?.role === 'operator'
  const parts: { value: SettingsPart; label: string; to: string }[] = [
    { value: 'server', label: t('settings.tabs.server'), to: '/settings' },
    { value: 'account', label: t('settings.tabs.account'), to: '/account' },
  ]
  return (
    <div className="view">
      <PageHead title={t('settings.title')} text={part === 'server' ? t('settings.serverText') : undefined} />
      {operator && (
        <div className="seg mb-[18px]" role="tablist" aria-label={t('settings.title')}>
          {parts.map(({ value, label, to }) => (
            <button key={value} type="button" role="tab" aria-selected={value === part} className={value === part ? 'on' : undefined} onClick={() => value !== part && navigate(to)}>
              {label}
            </button>
          ))}
        </div>
      )}
      <div className="settings">{children}</div>
    </div>
  )
}

/** A named place inside a part: the target of `?tab=`. It adds no box of its own. */
export function Part({ name, children }: { name: string; children: ReactNode }) {
  return (
    <div id={`part-${name}`} className="contents">
      {children}
    </div>
  )
}
