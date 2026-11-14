/**
 * The sign-in providers as buttons, in the order of the operator's list, with the line "or" above them: on the sign-in
 * page "Sign in with …", on the invitation page "Continue with …" (the invitation travels along to the provider).
 * Words from the shared sign-in module (`oidc.login.*`, `oidc.invite.button`), the same in every app that uses it.
 */
import { KeyRound } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { providerStart, type ProviderButton } from '../api/client'

export function OrLine() {
  const { t } = useTranslation()
  return (
    <div className="or">
      <span>{t('oidc.login.or')}</span>
    </div>
  )
}

export function ProviderButtons({
  providers,
  invite,
  next,
}: {
  providers: ProviderButton[]
  /** The invitation's key: the buttons say "Continue with" and take it along. */
  invite?: string
  /** The page the sign-in is for: it goes along to the provider and back. */
  next?: string
}) {
  const { t } = useTranslation()
  if (providers.length === 0) return null
  return (
    <div className="providers" data-testid="provider-buttons">
      {providers.map((provider) => (
        <button key={provider.slug} type="button" onClick={() => window.location.assign(providerStart(provider.slug, { invite, next }))} className="btn prov">
          <KeyRound size={17} strokeWidth={1.9} aria-hidden />
          {invite ? t('oidc.invite.button', { name: provider.label }) : t('oidc.login.button', { name: provider.label })}
        </button>
      ))}
    </div>
  )
}
