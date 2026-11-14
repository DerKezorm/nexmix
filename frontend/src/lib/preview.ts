/**
 * Listening in: one preview at a time in this browser, about 30 seconds from a good third of the track, played from
 * `/api/v1/preview/<track>` (the server asks Plex; the Plex token never comes here). A second ▶ stops the first; the same
 * ▶ again stops it. The ring of the button fills with the seconds played.
 *
 * Stopping takes the address away from the audio element, so the browser closes the connection and the server tells
 * Plex to end the conversion. Previews are no media of the page: the lock screen and the media keys get nothing to
 * show or control (the Media Session is left empty).
 *
 * The state lives here, once for the page, and the buttons read it (`usePreview`).
 */
import { useSyncExternalStore } from 'react'

import { previewApi, type PreviewState } from '../api/suggestions'

/** What is played of a preview, in seconds; a little more comes, in case the start takes a moment. */
export const SECONDS = 30
const GRACE_MS = 1500

export type Playing = { track: number | null; progress: number; allowed: boolean | null }

let state: Playing = { track: null, progress: 0, allowed: null }
const watchers = new Set<() => void>()
const problems = new Set<(code: string, values?: Record<string, unknown>) => void>()
let element: HTMLAudioElement | null = null
let timer = 0
let make: () => HTMLAudioElement = () => new Audio()

function set(next: Partial<Playing>) {
  state = { ...state, ...next }
  for (const watcher of watchers) watcher()
}

/** For the tests: the audio element to use. */
export function setAudioFactory(factory: () => HTMLAudioElement) {
  element = null
  make = factory
  state = { track: null, progress: 0, allowed: null }
}

/** No controls of the lock screen or the media keys for a preview. */
function quiet() {
  const session = typeof navigator !== 'undefined' ? navigator.mediaSession : undefined
  if (!session) return
  try {
    session.metadata = null
    session.playbackState = 'none'
  } catch {
    // An old browser without these: nothing to keep quiet.
  }
  for (const action of ['play', 'pause', 'stop', 'seekbackward', 'seekforward', 'seekto', 'previoustrack', 'nexttrack'] as MediaSessionAction[]) {
    try {
      session.setActionHandler(action, null)
    } catch {
      // An action this browser does not know.
    }
  }
}

function audio(): HTMLAudioElement {
  if (element) return element
  const made = make()
  made.preload = 'none'
  made.addEventListener('timeupdate', () => {
    if (state.track === null) return
    if (made.currentTime >= SECONDS) {
      stop()
      return
    }
    set({ progress: Math.min(100, (made.currentTime / SECONDS) * 100) })
  })
  made.addEventListener('ended', () => stop())
  made.addEventListener('error', () => {
    // An error after the address was taken away on purpose is none.
    if (state.track === null || !made.getAttribute('src')) return
    stop()
    void explain()
  })
  element = made
  return made
}

/** Why a preview did not play: listening in switched off, the hour used up, or something else. */
async function explain() {
  let found: PreviewState | null
  try {
    found = await previewApi.state()
  } catch {
    found = null
  }
  if (found && !found.allowed) {
    set({ allowed: false })
    tell('preview_off')
  } else if (found && found.left === 0) tell('preview_too_often', { retry_after: found.retry_after ?? 0 })
  else tell('preview_failed')
}

function tell(code: string, values?: Record<string, unknown>) {
  for (const problem of problems) problem(code, values)
}

export function play(track: number) {
  if (state.track === track) {
    stop()
    return
  }
  stop()
  const player = audio()
  player.setAttribute('src', previewApi.sound(track))
  set({ track, progress: 0 })
  quiet()
  const started = player.play()
  if (started && typeof started.catch === 'function')
    started.catch((error: unknown) => {
      // A preview stopped before it began, or another one that took its place: no trouble.
      if ((error as { name?: string })?.name === 'AbortError' || state.track !== track) return
      stop()
      void explain()
    })
  timer = window.setTimeout(() => {
    if (state.track === track) stop()
  }, SECONDS * 1000 + GRACE_MS)
}

export function stop() {
  window.clearTimeout(timer)
  if (element) {
    element.pause()
    element.removeAttribute('src')
    // Without an address the browser lets the connection go: the server ends the conversion at Plex.
    element.load()
  }
  if (state.track !== null || state.progress !== 0) set({ track: null, progress: 0 })
}

/** Whether listening in is on, asked once a page (and again after a problem). */
export async function check(): Promise<boolean | null> {
  try {
    const found = await previewApi.state()
    set({ allowed: found.allowed })
  } catch {
    // Not signed in, or the server is away: the buttons stay as they are.
  }
  return state.allowed
}

export function onProblem(handler: (code: string, values?: Record<string, unknown>) => void): () => void {
  problems.add(handler)
  return () => problems.delete(handler)
}

function subscribe(watcher: () => void) {
  watchers.add(watcher)
  return () => watchers.delete(watcher)
}

export function usePreview(): Playing {
  return useSyncExternalStore(subscribe, () => state)
}
