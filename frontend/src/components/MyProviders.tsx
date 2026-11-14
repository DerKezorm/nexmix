/**
 * My account, Security, Sign-in providers: one row per active provider with its state and "Link" or
 * "Unlink". Linking asks for the own password first and then goes to the provider; the way back lands on this page
 * with `?linked=<short name>`, said here by the provider's name. An account that signs in through a provider only
 * keeps its last link. Where the provider knows the account by another address, the row offers it (only an
 * account through the provider only follows it by itself); taken, it is not confirmed yet: the mail intake counts it
 * only once the person opened the link nexmix sends.
 */
import { KeyRound } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'

import { ApiError, oidcApi, type Me, type MyProvider } from '../api/client'
import { errorText } from '../lib/errors'
import { Button, Card, Input } from '../pages/settings/ui'
import { useAuth } from '../state/auth'
import { useConfirm } from './useConfirm'

function codeOf(error: unknown): string {
  return error instanceof ApiError ? error.code : 'internal_error'
}

export function MyProviders({ me }: { me: Me }) {
  const { t } = useTranslation()
  const { refresh } = useAuth()
  const [params] = useSearchParams()
  const [providers, setProviders] = useState<MyProvider[] | null>(null)
  const [linking, setLinking] = useState<string | null>(null)
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(params.get('error'))
  const [done, setDone] = useState<string | null>(null)
  const { ask, dialog } = useConfirm()
  const linked = params.get('linked')

  const load = useCallback(async () => setProviders(await oidcApi.mine()), [])
  useEffect(() => {
    void load().catch(() => setProviders([]))
  }, [load])

  if (providers === null || (providers.length === 0 && me.sign_in !== 'oidc')) return null
  // Back from the provider: said by the name on the button, never the short name of the address.
  const backFrom = linked ? providers.find((entry) => entry.slug === linked) : undefined
  const linkedCount = providers.filter((entry) => entry.linked).length

  const run = async (work: () => Promise<unknown>, success: string) => {
    setBusy(true)
    setProblem(null)
    setDone(null)
    try {
      await work()
      await load()
      setDone(success)
    } catch (error) {
      setProblem(codeOf(error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title={t('oidc.account.title')} text={t('oidc.account.text')}>
      {backFrom && !done && !problem && (
        <p role="status" className="text-sm font-semibold text-accent">
          ✓ {t('me.oidc.linkedTo', { name: backFrom.label })}
        </p>
      )}
      <ul data-testid="my-providers">
        {providers.map((provider) => {
          const lastLink = me.sign_in === 'oidc' && provider.linked && linkedCount === 1
          return (
            <li key={provider.slug} className="border-b border-line py-[9px] last:border-b-0">
              <div className="flex flex-wrap items-center gap-3">
                {/* As the design: the key, the name, and whether it is linked as a mark in the accent. */}
                <KeyRound size={17} className="shrink-0" aria-hidden />
                <span className="inline-flex min-w-0 items-center gap-1.5">
                  <span className="min-w-0 truncate font-semibold">{provider.label}</span>
                  <span className={`chip ${provider.linked ? 'on' : ''}`} data-testid="link-state">
                    {provider.linked ? t('oidc.account.linked') : t('oidc.account.notLinked')}
                  </span>
                </span>
                <span className="flex-1" aria-hidden />
                {provider.linked && !lastLink && (
                  <Button
                    busy={busy}
                    onClick={() =>
                      ask({
                        title: t('confirm.oidcLink.title'),
                        text: t('confirm.oidcLink.text', { name: provider.label }),
                        confirm: t('confirm.oidcLink.confirm'),
                        run: () => run(() => oidcApi.unlink(provider.slug), t('me.oidc.unlinkedFrom', { name: provider.label })),
                      })
                    }
                  >
                    {t('oidc.account.unlink')}
                  </Button>
                )}
                {!provider.linked && me.sign_in === 'password' && linking !== provider.slug && (
                  <Button
                    busy={busy}
                    onClick={() => {
                      setLinking(provider.slug)
                      setPassword('')
                    }}
                  >
                    {t('oidc.account.link')}
                  </Button>
                )}
              </div>
              {lastLink && <p className="mt-1 text-xs text-muted">{t('oidc.account.only', { name: provider.label })}</p>}
              {provider.linked && provider.address && me.sign_in === 'password' && (
                <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-ink-2">
                  <span className="min-w-0 flex-1">{t('me.oidc.addressOffer', { provider: provider.label, address: provider.address })}</span>
                  <Button
                    small
                    busy={busy}
                    onClick={() =>
                      void run(async () => {
                        await oidcApi.takeAddress(provider.slug)
                        await refresh()
                      }, t('me.oidc.addressTaken'))
                    }
                  >
                    {t('me.oidc.takeAddress')}
                  </Button>
                </div>
              )}
              {linking === provider.slug && (
                <form
                  className="mt-3 flex flex-wrap items-end gap-2"
                  onSubmit={(event) => {
                    event.preventDefault()
                    setBusy(true)
                    setProblem(null)
                    oidcApi.link(provider.slug, password).then(
                      ({ url }) => window.location.assign(url),
                      (error) => {
                        setProblem(codeOf(error))
                        setBusy(false)
                      },
                    )
                  }}
                >
                  <Input label={t('auth.password')} type="password" value={password} onChange={setPassword} autoComplete="current-password" autoFocus className="min-w-48 flex-1" />
                  <Button type="submit" small busy={busy} disabled={!password}>
                    {t('oidc.account.link')}
                  </Button>
                  <Button small onClick={() => setLinking(null)}>
                    {t('common.cancel')}
                  </Button>
                </form>
              )}
            </li>
          )
        })}
      </ul>
      <div aria-live="polite">
        {problem && (
          <p role="alert" className="rounded-xl border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-bad">
            {errorText(problem)}
          </p>
        )}
        {done && (
          <p role="status" className="text-sm font-semibold text-accent">
            ✓ {done}
          </p>
        )}
      </div>
      {dialog}
    </Card>
  )
}
