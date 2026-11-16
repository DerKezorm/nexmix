/**
 * What may be done with a playlist and where it could go, the same in the editor and in the menu of its card: the
 * rights follow from the access of the account and whether it is a smart playlist; the places from its mirrors and the
 * media servers connected; the question before it goes to the trash is one text for both.
 */
import type { TFunction } from 'i18next'

import type { Mirror } from '../../api/mirrors'
import type { Card } from '../../api/playlists'
import type { MediaServer } from '../../api/servers'
import { serverName } from '../../components/PlexSync'
import { iso } from '../../lib/say'
import { kindName } from '../../lib/servers'

/** How long a playlist stays in the trash, as the server keeps it. */
export const TRASH_DAYS = 30

/** What an account may do with a playlist: change it (name, entries, guests) and manage it (share, copy, move, delete). */
export function rightsOf(playlist: Pick<Card, 'access' | 'smart'>) {
  const { access } = playlist
  const smart = Boolean(playlist.smart)
  const canEdit = !smart && (access === 'owner' || access === 'operator' || access === 'edit')
  const canManage = access === 'owner' || access === 'operator'
  return { smart, canEdit, canManage, canShare: canManage, canCopyOrMove: canManage && !smart, canDelete: canManage && !smart }
}

/** Where a playlist is and where it could go: its mirrors, the servers it could move to (ready, keeping playlists, not
 * its source), the ones it could be copied to (not mirrored there already), and the name of its source. */
export function placesOf(playlist: Pick<Card, 'server' | 'mirrors' | 'plex'> | null, servers: MediaServer[] | null, source: MediaServer | null) {
  const mirrors = playlist?.mirrors ?? []
  const elsewhere = (servers ?? []).filter((one) => one.active && one.ready && one.capabilities.playlists && one.id !== playlist?.server)
  const copyTargets = elsewhere.filter((one) => !mirrors.some((mirror) => mirror.server === one.id && mirror.state !== 'gone'))
  const sourceName = source ? source.name || kindName(source.kind) : serverName(playlist?.plex?.kind)
  return { mirrors, elsewhere, copyTargets, sourceName }
}

/** The question before a playlist goes to the trash: how long it stays there, what happens on its server and to its
 * mirrors. It can be brought back, so the button is no red one. */
export function deleteQuestion(t: TFunction, playlist: Pick<Card, 'name' | 'plex'>, mirrors: Mirror[]) {
  const onServer = playlist.plex && playlist.plex.state !== 'off'
  return {
    title: t('editor.deleteTitle'),
    text:
      (onServer ? t('sync.deleteText', { name: iso(playlist.name), days: TRASH_DAYS, server: serverName(playlist.plex.kind) }) : t('editor.deleteText', { name: iso(playlist.name), days: TRASH_DAYS })) +
      (mirrors.length ? ' ' + t('mirror.deleteMirrors', { servers: mirrors.map((one) => one.name || kindName(one.kind)).join(', ') }) : ''),
    confirm: t('editor.deleteConfirm'),
    danger: false,
  }
}
