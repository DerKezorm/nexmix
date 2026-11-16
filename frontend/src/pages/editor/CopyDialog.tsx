/**
 * "Copy to …" and "Move to …": the target server, then the preview (how many entries are found there, how many of
 * them only by an unsure step) with every entry not found or found unsure, each to be matched by hand or left out.
 * Copying makes a mirror kept in step both ways, or ("copy once") a new playlist there. Moving makes the target the
 * source; the copy before stays a mirror, or is deleted there, which is asked first.
 */
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { mirrorsApi, type Choices, type Preview } from '../../api/mirrors'
import type { MediaServer } from '../../api/servers'
import { Dialog } from '../../components/Dialog'
import { useConfirm } from '../../components/useConfirm'
import { errorText } from '../../lib/errors'
import { iso } from '../../lib/say'
import { kindName } from '../../lib/servers'
import { chosen, withChoices } from '../../lib/choices'
import { PreviewCount, PreviewRows } from './MirrorParts'

export type CopyMode = 'copy' | 'move'

export function CopyDialog({
  playlistId,
  name,
  mode,
  targets,
  sourceName,
  onClose,
  onDone,
}: {
  playlistId: number
  name: string
  mode: CopyMode
  targets: MediaServer[]
  sourceName: string
  onClose: () => void
  onDone: (text: string, copy?: number) => void
}) {
  const { t } = useTranslation()
  const [target, setTarget] = useState<number | null>(targets[0]?.id ?? null)
  const [once, setOnce] = useState(false)
  const [old, setOld] = useState<'keep' | 'delete'>('keep')
  const [choices, setChoices] = useState<Choices>({})
  const [preview, setPreview] = useState<Preview | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [working, setWorking] = useState(false)
  const { ask, dialog } = useConfirm()
  const server = targets.find((one) => one.id === target) ?? null
  const label = server ? `${server.name} (${kindName(server.kind)})` : ''

  // Asked once per target; choices by hand are counted here (`withChoices`), not by a new preview, and belong to the
  // target they were made for (a new target starts without).
  useEffect(() => {
    if (target === null) return
    let gone = false
    setProblem(null)
    mirrorsApi
      .preview(playlistId, target, {}, mode === 'move')
      .then((found) => !gone && setPreview(found))
      .catch((error) => !gone && setProblem(errorText(error?.code ?? 'internal_error')))
    return () => {
      gone = true
    }
  }, [playlistId, target, mode])

  const choose = useCallback((track: number, choice: number | null | undefined) => {
    setChoices((now) => {
      const next = { ...now }
      if (choice === undefined) delete next[track]
      else next[track] = choice
      return next
    })
  }, [])

  const go = async () => {
    if (target === null || !server) return
    setWorking(true)
    setProblem(null)
    try {
      if (mode === 'copy') {
        const done = await mirrorsApi.copy(playlistId, target, chosen(choices), once)
        onDone(once ? t('mirror.copiedOnce', { server: iso(server.name) }) : t('mirror.copied', { server: iso(server.name) }), once ? done.playlist : undefined)
      } else {
        await mirrorsApi.move(playlistId, target, chosen(choices), old)
        onDone(t('mirror.moved', { server: iso(server.name) }))
      }
    } catch (error) {
      setProblem(errorText((error as { code?: string }).code ?? 'internal_error'))
    } finally {
      setWorking(false)
    }
  }

  const start = () => {
    if (mode === 'move' && old === 'delete')
      ask({
        title: t('mirror.deleteOldTitle'),
        text: t('mirror.deleteOldText', { name: iso(name), server: iso(sourceName) }),
        confirm: t('mirror.moveConfirm'),
        run: go,
      })
    else void go()
  }

  const title = mode === 'copy' ? t('mirror.copyTitle') : t('mirror.moveTitle')
  const sub = mode === 'copy' ? t('mirror.copyText', { name: iso(name) }) : t('mirror.moveText', { name: iso(name), server: iso(sourceName) })
  return (
    <Dialog title={title} sub={sub} onClose={onClose} wide>
      <div className="formrow">
        <span className="text-sm font-semibold">{t('mirror.target')}</span>
        <div className="seg small" role="group" aria-label={t('mirror.target')} data-testid="mirror-targets">
          {targets.map((one) => (
            <button key={one.id} type="button" className={one.id === target ? 'on' : ''} aria-pressed={one.id === target} onClick={() => {
                if (one.id !== target) setChoices({})
                setTarget(one.id)
              }}>
              {one.name} ({kindName(one.kind)})
            </button>
          ))}
        </div>
      </div>
      {mode === 'copy' ? (
        <div className="seg small mt-3" role="group" aria-label={t('mirror.howCopy')}>
          <button type="button" className={!once ? 'on' : ''} aria-pressed={!once} onClick={() => setOnce(false)}>
            {t('mirror.linked')}
          </button>
          <button type="button" className={once ? 'on' : ''} aria-pressed={once} onClick={() => setOnce(true)} data-testid="mirror-once">
            {t('mirror.once')}
          </button>
        </div>
      ) : (
        <div className="seg small mt-3" role="group" aria-label={t('mirror.oldCopy', { server: iso(sourceName) })}>
          <button type="button" className={old === 'keep' ? 'on' : ''} aria-pressed={old === 'keep'} onClick={() => setOld('keep')}>
            {t('mirror.keepOld', { server: iso(sourceName) })}
          </button>
          <button type="button" className={old === 'delete' ? 'on' : ''} aria-pressed={old === 'delete'} onClick={() => setOld('delete')} data-testid="mirror-delete-old">
            {t('mirror.deleteOld', { server: iso(sourceName) })}
          </button>
        </div>
      )}
      <p className="mt-2 text-sm text-muted">
        {mode === 'copy' ? (once ? t('mirror.onceHint', { server: iso(label) }) : t('mirror.linkedHint', { server: iso(label) })) : t('mirror.moveHint', { server: iso(label) })}
      </p>
      {preview && preview.server === target && (
        <div className="mt-3">
          <PreviewCount preview={withChoices(preview, choices)} />
          <PreviewRows preview={preview} target={preview.server} choices={choices} onChoose={choose} />
        </div>
      )}
      {!preview && !problem && <p className="mt-3 text-sm text-muted">{t('mirror.looking')}</p>}
      {problem && (
        <p role="alert" className="mt-3 text-sm text-bad">
          {problem}
        </p>
      )}
      <div className="mt-4 flex justify-end gap-2">
        <button type="button" className="btn ghost" onClick={onClose}>
          {t('common.cancel')}
        </button>
        <button type="button" className="btn primary" data-primary disabled={working || !preview || target === null} onClick={start} data-testid="mirror-go">
          {mode === 'copy' ? (once ? t('mirror.copyOnceButton') : t('mirror.copyButton')) : t('mirror.moveButton')}
        </button>
      </div>
      {dialog}
    </Dialog>
  )
}
