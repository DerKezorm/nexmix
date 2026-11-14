/**
 * Sharing a playlist with members: each one not shared, to see, or to change. Taking a share back asks first (the
 * person no longer sees the playlist; it can be shared again any time). The operator sees every playlist anyway and is
 * not in the list.
 */
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { playlistsApi, type Shares } from '../../api/playlists'
import { Dialog } from '../../components/Dialog'
import { PersonFace } from '../../components/People'
import { useConfirm } from '../../components/useConfirm'
import { errorText } from '../../lib/errors'
import { iso } from '../../lib/say'

type Choice = 'none' | 'read' | 'edit'

export function ShareDialog({ playlistId, name, onClose, onChanged }: { playlistId: number; name: string; onClose: () => void; onChanged: (count: number) => void }) {
  const { t } = useTranslation()
  const [shares, setShares] = useState<Shares | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  const { ask, dialog } = useConfirm()
  useEffect(() => {
    playlistsApi
      .shares(playlistId)
      .then(setShares)
      .catch((error) => setProblem(errorText(error?.code ?? 'internal_error')))
  }, [playlistId])
  const took = (next: Shares) => {
    setShares(next)
    onChanged(next.shares.length)
  }
  const choose = async (account: number, who: string, choice: Choice, now: Choice) => {
    if (choice === now || busy !== null) return
    setProblem(null)
    if (choice === 'none') {
      ask({
        title: t('share.unshareTitle'),
        text: t('share.unshareText', { name: iso(who), playlist: iso(name) }),
        confirm: t('share.unshare'),
        run: async () => took(await playlistsApi.unshare(playlistId, account)),
      })
      return
    }
    setBusy(account)
    try {
      took(await playlistsApi.share(playlistId, account, choice))
    } catch (error) {
      setProblem(errorText((error as { code?: string }).code ?? 'internal_error'))
    } finally {
      setBusy(null)
    }
  }
  const options: [Choice, string][] = [
    ['none', t('share.none')],
    ['read', t('share.read')],
    ['edit', t('share.edit')],
  ]
  return (
    <Dialog title={t('share.title')} sub={t('share.text', { name: iso(name) })} onClose={onClose}>
      {problem && (
        <p role="alert" className="mb-3 text-sm text-bad">
          {problem}
        </p>
      )}
      {shares && shares.people.length === 0 && <p className="text-sm text-muted">{t('share.nobody')}</p>}
      <div data-testid="share-list">
        {shares?.people.map((person) => {
          const now: Choice = shares.shares.find((one) => one.person.id === person.id)?.access ?? 'none'
          const who = person.display_name || person.name
          return (
            <div className="sharerow" key={person.id}>
              <PersonFace person={person} size={26} />
              <b className="grow" dir="auto">
                {who}
              </b>
              <div className="seg small" role="group" aria-label={t('share.for', { name: who })}>
                {options.map(([choice, label]) => (
                  <button key={choice} type="button" className={now === choice ? 'on' : ''} aria-pressed={now === choice} disabled={busy === person.id} onClick={() => void choose(person.id ?? 0, who, choice, now)}>
                    {label}
                  </button>
                ))}
              </div>
            </div>
          )
        })}
      </div>
      <p className="mt-3 text-sm text-muted">{t('share.operator')}</p>
      <div className="mt-4 flex justify-end">
        <button type="button" className="btn primary" data-primary onClick={onClose}>
          {t('common.done')}
        </button>
      </div>
      {dialog}
    </Dialog>
  )
}
