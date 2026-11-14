import type { ReactNode } from 'react'
import { Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'

import { AppShell } from './components/AppShell'
import { AboutPage } from './pages/AboutPage'
import { AccountPage } from './pages/AccountPage'
import { GuestPage } from './pages/GuestPage'
import { ForgotPage, InvitePage, LoginPage, MailConfirmPage, ResetPage, SetupPage } from './pages/AuthPages'
import { PlaylistEditor } from './pages/PlaylistEditor'
import { PlaylistsPage } from './pages/PlaylistsPage'
import { SettingsPage } from './pages/SettingsPage'
import { useAuth } from './state/auth'

/** Everything behind the sign-in: without an account the page goes to the sign-in, and comes back after. An account
 * that still has to set up its second factor (the operator requires one) reaches its own account page only. */
function SignedIn({ children }: { children: ReactNode }) {
  const { status, me } = useAuth()
  const location = useLocation()
  if (status === 'loading') return null
  if (status === 'setup') return <Navigate to="/setup" replace />
  if (status === 'signedOut') return <Navigate to={`/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />
  // Right after the password, the sign-in page sets the second factor up before anything else opens.
  if (me?.session_stage && me.session_stage !== 'full') return <Navigate to={`/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />
  if (me?.second_factor_setup_required && location.pathname !== '/account') return <Navigate to="/account?tab=security" replace />
  return children
}

export default function App() {
  return (
    <Routes>
      <Route path="setup" element={<SetupPage />} />
      <Route path="login" element={<LoginPage />} />
      <Route path="invite/:token" element={<InvitePage />} />
      <Route path="forgot" element={<ForgotPage />} />
      <Route path="reset/:token" element={<ResetPage />} />
      <Route path="mail/:token" element={<MailConfirmPage />} />
      {/* A guest's way in: no account, the link of an invitation. */}
      <Route path="j/:code" element={<GuestPage />} />
      <Route
        element={
          <SignedIn>
            <Outlet />
          </SignedIn>
        }
      >
        <Route element={<AppShell />}>
          <Route index element={<PlaylistsPage />} />
          <Route path="playlists/:id" element={<PlaylistEditor />} />
          <Route path="account" element={<AccountPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="about" element={<AboutPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Route>
    </Routes>
  )
}
