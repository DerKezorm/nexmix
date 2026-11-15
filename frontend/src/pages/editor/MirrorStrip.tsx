/**
 * The servers of a playlist in its head: the source and every mirror with how it stands; a mirror opens its own small
 * dialog (the entries not there, each to be matched by hand, and "let go of the mirror", asked first). Below, the notes
 * a mirror needs: deleted on its server ("make anew" or "let go"), and the tracks only in it ("take over as …" through
 * a search in the source's catalogue, or "drop", asked first).
 */
import { CloudAlert, CloudCheck, CloudOff, Copy } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { mirrorsApi, type Mirror } from '../../api/mirrors'
import type { Entry } from '../../api/playlists'
import { Dialog } from '../../components/Dialog'
import { useConfirm } from '../../components/useConfirm'
import { errorText } from '../../lib/errors'
import { clock } from '../../lib/music'
import { iso } from '../../lib/say'
import { kindName } from '../../lib/servers'
import { TrackPicker } from './MirrorParts'

function MirrorChip({ mirror, onOpen }: { mirror: Mirror; onOpen: () => void }) {
  const { t } = useTranslation()
  const server = mirror.name || kindName(mirror.kind)
  const icon = mirror.state === 'synced' ? <CloudCheck aria-hidden /> : mirror.state === 'waiting' || mirror.state === 'off' ? <CloudOff aria-hidden /> : mirror.state === 'pending' ? <span className="spin small" aria-hidden /> : <CloudAlert aria-hidden />
  return (
    <button type="button" className={`chip mirror ${mirror.state}`} onClick={onOpen} data-testid="mirror-chip" aria-label={t('mirror.chipLabel', { server, state: t(`mirror.state.${mirror.state}`) })}>
      <Copy aria-hidden />
      {t('mirror.chip', { server })}
      {icon}
    </button>
  )
}

export function SourceChip({ name, kind }: { name: string; kind: string }) {
  const { t } = useTranslation()
  return (
    <span className="chip static source" data-testid="source-chip" title={t('mirror.sourceTitle')}>
      {t('mirror.source', { server: name || kindName(kind) })}
    </span>
  )
}

export function MirrorChips({ mirrors, onOpen }: { mirrors: Mirror[]; onOpen: (server: number) => void }) {
  return (
    <>
      {mirrors.map((mirror) => (
        <MirrorChip key={mirror.server} mirror={mirror} onOpen={() => onOpen(mirror.server)} />
      ))}
    </>
  )
}

/** One mirror: how it stands, the entries not there (each matched by hand), and letting go. */
export function MirrorDialog({ playlistId, mirror, items, canEdit, canManage, onClose, onChanged }: { playlistId: number; mirror: Mirror; items: Entry[]; canEdit: boolean; canManage: boolean; onClose: () => void; onChanged: () => void }) {
  const { t } = useTranslation()
  const [picking, setPicking] = useState<number | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const { ask, dialog } = useConfirm()
  const server = mirror.name || kindName(mirror.kind)
  const absent = items.filter((entry) => (entry.absent ?? []).includes(mirror.server))
  const run = async (work: () => Promise<unknown>) => {
    setProblem(null)
    try {
      await work()
      onChanged()
    } catch (error) {
      setProblem(errorText((error as { code?: string }).code ?? 'internal_error'))
    }
  }
  return (
    <Dialog title={t('mirror.dialogTitle', { server: iso(server) })} sub={t(`mirror.stateLong.${mirror.state}`, { server: iso(server), reason: errorText(mirror.error ?? 'internal_error') })} onClose={onClose}>
      <p className="text-sm font-semibold">{absent.length ? t('mirror.absentTitle', { count: absent.length, server: iso(server) }) : t('mirror.absentNone', { server: iso(server) })}</p>
      <div className="previewrows soft-scroll" data-testid="mirror-absent">
        {absent.map((entry) => (
          <div className="prow" key={entry.id}>
            <div className="min-w-0 grow">
              <b dir="auto">{entry.title}</b>
              <small dir="auto">
                {entry.artist} · {entry.album} · {clock(entry.duration)}
              </small>
            </div>
            {canEdit && (
              <button type="button" className="btn ghost" onClick={() => setPicking(picking === entry.id ? null : entry.id)} aria-label={t('mirror.otherLabel', { title: entry.title })}>
                {t('mirror.match')}
              </button>
            )}
            {picking === entry.id && (
              <TrackPicker
                server={mirror.server}
                start={entry.title}
                label={t('mirror.searchLabel', { title: entry.title })}
                onCancel={() => setPicking(null)}
                onPick={(hit) => {
                  setPicking(null)
                  void run(() => mirrorsApi.choose(playlistId, mirror.server, entry.id, hit.id))
                }}
              />
            )}
          </div>
        ))}
      </div>
      {problem && (
        <p role="alert" className="mt-2 text-sm text-bad">
          {problem}
        </p>
      )}
      <div className="mt-4 flex flex-wrap justify-end gap-2">
        {canManage && (
          <button
            type="button"
            className="btn ghost"
            data-testid="mirror-forget"
            onClick={() =>
              ask({
                title: t('mirror.forgetTitle', { server: iso(server) }),
                text: t('mirror.forgetText', { server: iso(server) }),
                confirm: t('mirror.forget'),
                run: async () => {
                  await mirrorsApi.forget(playlistId, mirror.server)
                  onChanged()
                  onClose()
                },
              })
            }
          >
            {t('mirror.forget')}
          </button>
        )}
        <button type="button" className="btn primary" data-primary onClick={onClose}>
          {t('common.done')}
        </button>
      </div>
      {dialog}
    </Dialog>
  )
}

/** The notes a mirror needs at the playlist: deleted on its server, tracks only in it. */
export function MirrorNotes({ playlistId, name, mirrors, source, canEdit, canManage, onChanged, onToast }: { playlistId: number; name: string; mirrors: Mirror[]; source: number | null; canEdit: boolean; canManage: boolean; onChanged: () => void; onToast: (text: string) => void }) {
  const { t } = useTranslation()
  const [taking, setTaking] = useState<string | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const { ask, dialog } = useConfirm()
  const run = async (work: () => Promise<unknown>, done?: string) => {
    setProblem(null)
    try {
      await work()
      onChanged()
      if (done) onToast(done)
    } catch (error) {
      setProblem(errorText((error as { code?: string }).code ?? 'internal_error'))
    }
  }
  const notes = mirrors.filter((mirror) => mirror.state === 'gone' || mirror.extras.length > 0)
  if (!notes.length && !problem) return null
  return (
    <>
      {notes.map((mirror) => {
        const server = mirror.name || kindName(mirror.kind)
        if (mirror.state === 'gone')
          return (
            <div className="note" role="status" key={`gone-${mirror.server}`} data-testid="mirror-gone">
              <b>{t('mirror.goneTitle', { server: iso(server) })}</b>
              {t('mirror.goneText', { server: iso(server), name: iso(name) })}
              {canManage && (
                <div className="mt-2 flex flex-wrap gap-2">
                  <button type="button" className="btn" onClick={() => void run(() => mirrorsApi.remake(playlistId, mirror.server), t('mirror.remade', { server: iso(server) }))}>
                    {t('mirror.remake')}
                  </button>
                  <button
                    type="button"
                    className="btn ghost"
                    onClick={() =>
                      ask({
                        title: t('mirror.forgetTitle', { server: iso(server) }),
                        text: t('mirror.forgetText', { server: iso(server) }),
                        confirm: t('mirror.forget'),
                        run: () => run(() => mirrorsApi.forget(playlistId, mirror.server)),
                      })
                    }
                  >
                    {t('mirror.forget')}
                  </button>
                </div>
              )}
            </div>
          )
        return (
          <div className="note" role="status" key={`extras-${mirror.server}`} data-testid="mirror-extras">
            <b>{t('mirror.onlyTitle', { server: iso(server), count: mirror.extras.length })}</b>
            {t('mirror.onlyText', { server: iso(server) })}
            {mirror.extras.map((extra) => {
              const key = `${mirror.server}-${extra.id}`
              return (
                <div className="prow" key={key} data-testid="mirror-extra">
                  <div className="min-w-0 grow">
                    <b dir="auto">{extra.title}</b>
                    <small dir="auto">
                      {extra.artist} · {extra.album} · {clock(extra.duration)}
                    </small>
                  </div>
                  {canEdit && (
                    <div className="flex flex-wrap gap-2">
                      <button type="button" className="btn" onClick={() => setTaking(taking === key ? null : key)} aria-label={t('mirror.takeAsLabel', { title: extra.title })}>
                        {t('mirror.takeAs')}
                      </button>
                      <button
                        type="button"
                        className="btn ghost"
                        aria-label={t('mirror.dropLabel', { title: extra.title })}
                        onClick={() =>
                          ask({
                            title: t('mirror.dropTitle'),
                            text: t('mirror.dropText', { title: iso(extra.title), server: iso(server) }),
                            confirm: t('mirror.drop'),
                            run: () => run(() => mirrorsApi.dropExtra(playlistId, mirror.server, extra.id), t('mirror.dropped')),
                          })
                        }
                      >
                        {t('mirror.drop')}
                      </button>
                    </div>
                  )}
                  {taking === key && source !== null && (
                    <TrackPicker
                      server={source}
                      start={extra.title}
                      label={t('mirror.searchLabel', { title: extra.title })}
                      onCancel={() => setTaking(null)}
                      onPick={(hit) => {
                        setTaking(null)
                        void run(() => mirrorsApi.takeExtra(playlistId, mirror.server, extra.id, hit.id), t('mirror.taken', { title: iso(hit.title) }))
                      }}
                    />
                  )}
                </div>
              )
            })}
          </div>
        )
      })}
      {problem && (
        <p role="alert" className="mt-2 text-sm text-bad">
          {problem}
        </p>
      )}
      {dialog}
    </>
  )
}
