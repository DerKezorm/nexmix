/**
 * "Rename" from the menu of a playlist's card: the name of the editor's head as a field of its own. An empty name or
 * the same one sends nothing; one press, one request.
 */
import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { playlistsApi } from '../../api/playlists'
import { Dialog } from '../../components/Dialog'
import { errorText } from '../../lib/errors'
import { iso } from '../../lib/say'

export function RenameDialog({ playlistId, name, onClose, onDone }: { playlistId: number; name: string; onClose: () => void; onDone: (name: string) => void }) {
  const { t } = useTranslation()
  const [value, setValue] = useState(name)
  const [problem, setProblem] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const sending = useRef(false)
  const wanted = value.trim()
  const save = async () => {
    if (sending.current || !wanted) return
    if (wanted === name) {
      onClose()
      return
    }
    sending.current = true
    setBusy(true)
    setProblem(null)
    try {
      const answer = await playlistsApi.rename(playlistId, wanted)
      onDone(answer.name)
    } catch (error) {
      setProblem(errorText((error as { code?: string }).code ?? 'internal_error'))
      sending.current = false
      setBusy(false)
    }
  }
  return (
    <Dialog title={t('playlists.renameTitle')} sub={t('playlists.renameText', { name: iso(name) })} onClose={onClose}>
      <form
        onSubmit={(event) => {
          event.preventDefault()
          void save()
        }}
      >
        <label className="block">
          <span className="text-sm font-semibold">{t('editor.nameLabel')}</span>
          <input className="input mt-1 w-full" value={value} maxLength={200} dir="auto" data-autofocus onChange={(event) => setValue(event.target.value)} data-testid="rename-input" />
        </label>
        {problem && (
          <p role="alert" className="mt-2 text-sm text-bad">
            {problem}
          </p>
        )}
        <div className="dlg-foot">
          <span className="grow" />
          <button type="button" className="btn" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" data-primary disabled={busy || !wanted}>
            {t('common.save')}
          </button>
        </div>
      </form>
    </Dialog>
  )
}
