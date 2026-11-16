import type { Choices, Preview, PreviewRow } from '../api/mirrors'

/** Choices by hand as the server reads them, the ones taken back left out. */
export function chosen(choices: Choices): Choices {
  const found: Choices = {}
  for (const [track, choice] of Object.entries(choices)) if (choice !== undefined) found[Number(track)] = choice
  return found
}

const UNSURE = new Set(['song', 'title'])

/** Whether a row was found by an unsure step (it names the track it found). */
export function isUnsure(row: PreviewRow): boolean {
  return row.found !== null && UNSURE.has(row.how)
}

/**
 * The counts of a preview with the choices by hand, counted here and not asked of the server again (over every
 * playlist of a server a preview takes seconds): a row given a track counts as found and sure, a row left out as not
 * found, for every entry of that track. An unsure row counts as found from the start when the preview takes unsure
 * ones (`preselect`, copying), else only once its track is chosen (moving, taking all over). The copy itself decides
 * on the server.
 */
export function withChoices(preview: Preview, choices: Choices): Preview {
  const preselect = preview.preselect !== false
  let found = preview.found
  let unsure = preview.unsure
  for (const row of preview.rows) {
    const choice = choices[row.track]
    if (choice === undefined) continue
    if (isUnsure(row)) unsure -= row.count
    if (row.found !== null && (preselect || !isUnsure(row))) found -= row.count
    if (choice !== null) found += row.count
  }
  return { ...preview, found, unsure, missing: preview.entries - found }
}

/** The choices that take every unsure finding not chosen otherwise (moving, taking all over). */
export function takeUnsure(preview: Preview, choices: Choices): Choices {
  const next = { ...choices }
  for (const row of preview.rows) if (isUnsure(row) && next[row.track] === undefined && row.found) next[row.track] = row.found.track
  return next
}
