/**
 * Settings › Server: "Take all playlists over" at a server's card. The dialog shows the same preview as copying one
 * playlist, over every playlist of the other servers at once (how many found, how many unsure, every entry not found
 * or unsure to be matched by hand or left out), with the choice of mirroring them here or moving them here (the copies
 * before kept as mirrors or deleted, which is asked first), and, when moving, letting go of a server once its
 * playlists are all here. The run goes on in the background with its progress; it can be stopped after the playlist
 * under way, and a restart of nexmix goes on where it was.
 */
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { transferApi, type Choices, type Looking, type Preview, type Transfer } from '../../api/mirrors'
import type { MediaServer } from '../../api/servers'
import { Dialog } from '../../components/Dialog'
import { useConfirm } from '../../components/useConfirm'
import { errorText } from '../../lib/errors'
import { iso } from '../../lib/say'
import { kindName } from '../../lib/servers'
import { chosen, withChoices } from '../../lib/choices'
import { PreviewCount, PreviewRows } from '../editor/MirrorParts'
import { Button, Card, useAction } from './ui'

const POLL_MS = 2000
/** How often the dialog asks how far the preview got. */
const LOOK_MS = 500

function TransferDialog({ target, others, startWith, onClose, onStarted }: { target: MediaServer; others: MediaServer[]; startWith?: number | null; onClose: () => void; onStarted: (row: Transfer) => void }) {
  const { t } = useTranslation()
  const [mode, setMode] = useState<'mirror' | 'move'>(startWith ? 'move' : 'mirror')
  const [old, setOld] = useState<'keep' | 'delete'>('keep')
  const [letGo, setLetGo] = useState<number | null>(startWith ?? null)
  const [choices, setChoices] = useState<Choices>({})
  const [preview, setPreview] = useState<Preview | null>(null)
  const [progress, setProgress] = useState<Looking | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [working, setWorking] = useState(false)
  const { ask, dialog } = useConfirm()
  const name = `${target.name} (${kindName(target.kind)})`

  // The preview is worked out on the server in the background (over every playlist it takes seconds): asked once per
  // mode and server let go of, then read until it is done. Choices by hand are counted here (`withChoices`).
  useEffect(() => {
    let gone = false
    let timer = 0
    setPreview(null)
    setProgress(null)
    setProblem(null)
    const fail = (error: { code?: string } | undefined) => !gone && setProblem(errorText(error?.code ?? 'internal_error'))
    const take = (found: Looking | null) => {
      if (gone) return
      if (found?.state === 'done' && found.result) {
        setPreview(found.result)
        setProgress(null)
      } else if (found?.state === 'running') {
        setProgress(found)
        timer = window.setTimeout(() => transferApi.looking(target.id).then((now) => take(now.preview), fail), LOOK_MS)
      } else fail({ code: found?.error ?? 'internal_error' })
    }
    transferApi.look(target.id, mode, mode === 'move' ? letGo : null).then((begun) => take(begun.preview), fail)
    return () => {
      gone = true
      window.clearTimeout(timer)
    }
  }, [target.id, mode, letGo])

  const choose = useCallback((track: number, choice: number | null | undefined) => {
    setChoices((now) => {
      const next = { ...now }
      if (choice === undefined) delete next[track]
      else next[track] = choice
      return next
    })
  }, [])

  const go = async () => {
    setWorking(true)
    setProblem(null)
    try {
      const started = await transferApi.start(target.id, { mode, old, choices: chosen(choices), source: mode === 'move' ? letGo : null, disconnect: mode === 'move' ? letGo : null })
      onStarted(started.transfer)
    } catch (error) {
      setProblem(errorText((error as { code?: string }).code ?? 'internal_error'))
    } finally {
      setWorking(false)
    }
  }
  const start = () => {
    if (mode === 'move' && (old === 'delete' || letGo !== null)) {
      const server = others.find((one) => one.id === letGo)
      ask({
        title: t('transfer.confirmTitle'),
        text: [old === 'delete' ? t('transfer.confirmDelete') : '', server ? t('transfer.confirmLetGo', { server: iso(server.name) }) : ''].filter(Boolean).join(' '),
        confirm: t('transfer.start'),
        run: go,
      })
    } else void go()
  }

  return (
    <Dialog title={t('transfer.title', { server: iso(name) })} sub={t('transfer.text', { server: iso(name) })} onClose={onClose} wide>
      <div className="seg small" role="group" aria-label={t('transfer.mode')} data-testid="transfer-mode">
        <button type="button" className={mode === 'mirror' ? 'on' : ''} aria-pressed={mode === 'mirror'} onClick={() => setMode('mirror')}>
          {t('transfer.mirror')}
        </button>
        <button type="button" className={mode === 'move' ? 'on' : ''} aria-pressed={mode === 'move'} onClick={() => setMode('move')}>
          {t('transfer.move')}
        </button>
      </div>
      <p className="mt-2 text-sm text-muted">{mode === 'mirror' ? t('transfer.mirrorHint', { server: iso(name) }) : t('transfer.moveHint', { server: iso(name) })}</p>
      {mode === 'move' && (
        <>
          <div className="seg small mt-2" role="group" aria-label={t('transfer.old')}>
            <button type="button" className={old === 'keep' ? 'on' : ''} aria-pressed={old === 'keep'} onClick={() => setOld('keep')}>
              {t('transfer.keepOld')}
            </button>
            <button type="button" className={old === 'delete' ? 'on' : ''} aria-pressed={old === 'delete'} onClick={() => setOld('delete')}>
              {t('transfer.deleteOld')}
            </button>
          </div>
          <label className="mt-2 flex items-center gap-2 text-sm">
            <span>{t('transfer.letGo')}</span>
            <select className="chip" value={letGo ?? ''} onChange={(event) => setLetGo(event.target.value ? Number(event.target.value) : null)} data-testid="transfer-let-go">
              <option value="">{t('transfer.letGoNone')}</option>
              {others.map((one) => (
                <option key={one.id} value={one.id}>
                  {one.name} ({kindName(one.kind)})
                </option>
              ))}
            </select>
          </label>
        </>
      )}
      {preview && (
        <div className="mt-3">
          <p className="text-sm" data-testid="transfer-playlists">
            {t('transfer.playlists', { count: preview.playlists ?? 0 })}
            {(preview.smart ?? 0) > 0 && ` ${t('transfer.smart', { count: preview.smart ?? 0 })}`}
          </p>
          <PreviewCount preview={withChoices(preview, choices)} />
          <PreviewRows preview={preview} target={target.id} choices={choices} onChoose={choose} />
        </div>
      )}
      {!preview && !problem && (
        <div className="mt-3" data-testid="transfer-looking">
          <p className="text-sm text-muted">{progress && progress.total > 0 ? t('transfer.looking', { done: progress.done, total: progress.total }) : t('mirror.looking')}</p>
          {progress && progress.total > 0 && (
            <span className="meter mt-2" role="progressbar" aria-valuemin={0} aria-valuemax={progress.total} aria-valuenow={progress.done} aria-label={t('mirror.looking')}>
              <i style={{ width: `${Math.round((100 * progress.done) / progress.total)}%` }} />
            </span>
          )}
        </div>
      )}
      {problem && (
        <p role="alert" className="mt-3 text-sm text-bad">
          {problem}
        </p>
      )}
      <div className="mt-4 flex justify-end gap-2">
        <button type="button" className="btn ghost" onClick={onClose}>
          {t('common.cancel')}
        </button>
        <button type="button" className="btn primary" data-primary disabled={working || !preview || !preview.playlists} onClick={start} data-testid="transfer-go">
          {t('transfer.start')}
        </button>
      </div>
      {dialog}
    </Dialog>
  )
}

/** The card: the last run and its progress, "take all over", and "stop". */
export function TransferCard({ target, others }: { target: MediaServer; others: MediaServer[] }) {
  const { t, i18n } = useTranslation()
  const [row, setRow] = useState<Transfer | null>(null)
  const [open, setOpen] = useState(false)
  const action = useAction()
  const load = useCallback(async () => {
    try {
      setRow((await transferApi.state(target.id)).transfer)
    } catch {
      setRow(null)
    }
  }, [target.id])
  useEffect(() => {
    void load()
  }, [load])
  useEffect(() => {
    if (row?.state !== 'running') return
    const timer = window.setInterval(() => void load(), POLL_MS)
    return () => window.clearInterval(timer)
  }, [row?.state, load])
  const number = (value: number) => value.toLocaleString(i18n.language)
  const percent = row && row.total ? Math.round((100 * (row.done + row.skipped)) / row.total) : 0
  const name = `${target.name} (${kindName(target.kind)})`
  return (
    <Card id={`transfer-${target.id}`} title={t('transfer.cardTitle', { server: iso(name) })} text={t('transfer.cardText', { server: iso(name) })}>
      {row && (
        <div data-testid="transfer-state">
          <p className="text-sm">
            {t(`transfer.state.${row.state}`, { done: number(row.done), total: number(row.total), skipped: number(row.skipped) })}
            {row.error && ` ${errorText(row.error)}`}
          </p>
          {row.state === 'running' && (
            <span className="meter mt-2" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} aria-label={t('transfer.cardTitle', { server: name })}>
              <i style={{ width: `${percent}%` }} />
            </span>
          )}
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        {row?.state === 'running' ? (
          <Button ghost disabled={row.stopping || action.busy} onClick={() => void action.run(async () => setRow((await transferApi.stop(target.id)).transfer))}>
            {row.stopping ? t('transfer.stopping') : t('transfer.stop')}
          </Button>
        ) : (
          <Button onClick={() => setOpen(true)} disabled={!target.ready}>
            {t('transfer.open')}
          </Button>
        )}
      </div>
      {open && (
        <TransferDialog
          target={target}
          others={others}
          onClose={() => setOpen(false)}
          onStarted={(started) => {
            setOpen(false)
            setRow(started)
          }}
        />
      )}
    </Card>
  )
}
