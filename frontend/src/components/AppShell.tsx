import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'

import type { NoticeLink } from '../lib/say'
import { useAuth } from '../state/auth'
import { AccountMenu } from './AccountMenu'
import { LogoMark } from './Logo'
import { ThemeToggle } from './ThemeToggle'
import { WhatsNewBanner } from './WhatsNew'

/** A short sentence after something was done ("Gespeichert."), handed over by the page that did it as `notice` in the
 * navigation state; shown for a moment, then gone, and not again after a reload. */
function Toast() {
  const location = useLocation()
  const navigate = useNavigate()
  const [shown, setShown] = useState<{ text: string; link?: NoticeLink } | null>(null)
  const state = location.state as { notice?: unknown; link?: unknown } | null
  const notice = state?.notice
  const link = state?.link as NoticeLink | undefined
  useEffect(() => {
    if (typeof notice !== 'string') return
    setShown({ text: notice, ...(link && typeof link.to === 'string' && typeof link.label === 'string' ? { link } : {}) })
    navigate(location.pathname + location.search, { replace: true, state: null })
  }, [notice, link, navigate, location.pathname, location.search])
  useEffect(() => {
    if (!shown) return
    // A sentence with a way on stays longer: there is something to press.
    const timer = window.setTimeout(() => setShown(null), shown.link ? 8000 : 3500)
    return () => window.clearTimeout(timer)
  }, [shown])
  if (!shown) return null
  return (
    <div role="status" className="rise fixed inset-x-0 bottom-8 z-40 flex justify-center px-4">
      <p className="rounded-full bg-ink px-4 py-2.5 text-sm font-semibold text-paper shadow-soft">
        {shown.text}
        {shown.link && (
          <>
            {' '}
            <Link to={shown.link.to} className="underline underline-offset-2" onClick={() => setShown(null)}>
              {shown.link.label}
            </Link>
          </>
        )}
      </p>
    </div>
  )
}

/**
 * The frame: the head with the mark, the areas (playlists for everybody, settings for the operator), the account menu
 * and the button for light and dark, and below it the view.
 */
export function AppShell() {
  const { t } = useTranslation()
  const { me } = useAuth()
  const { pathname } = useLocation()
  const areas = [
    { to: '/', label: 'app.playlists', end: true },
    ...(me?.role === 'operator' ? [{ to: '/settings', label: 'app.settings', end: false }] : []),
  ]
  return (
    <>
      <header className="top">
        <Link className="brand" to="/" aria-label="nexmix">
          <LogoMark size={34} />
          <span className="wordmark">
            NEX<span>MIX</span>
          </span>
        </Link>
        <nav className="tabs" aria-label={t('app.areas')}>
          {areas.map(({ to, label, end }) => (
            // "My account" is the other part of the settings: their tab stays marked there too.
            <NavLink key={to} to={to} end={end} className={({ isActive }) => (isActive || (to === '/settings' && pathname.startsWith('/account')) || (to === '/' && pathname.startsWith('/playlists/')) ? 'on' : undefined)}>
              {t(label)}
            </NavLink>
          ))}
        </nav>
        <div className="top-end">
          <AccountMenu />
          <ThemeToggle />
        </div>
      </header>
      <main>
        <WhatsNewBanner />
        <Outlet />
      </main>
      <Toast />
    </>
  )
}
