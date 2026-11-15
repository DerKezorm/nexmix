import type { Choices } from '../api/mirrors'

/** Choices by hand as the server reads them, the ones taken back left out. */
export function chosen(choices: Choices): Choices {
  const found: Choices = {}
  for (const [track, choice] of Object.entries(choices)) if (choice !== undefined) found[Number(track)] = choice
  return found
}
