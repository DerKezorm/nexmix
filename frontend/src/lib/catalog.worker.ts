/// <reference lib="webworker" />
/** The catalogue in a worker of its own (`localCatalog.ts`): the page sends what it wants to know, with a number, and
 * gets the answer back with the same number. */
import { answer, CatalogError, LocalCatalog, type Ask } from './localCatalog'

const catalog = new LocalCatalog()

self.onmessage = async (event: MessageEvent<{ id: number; ask: Ask }>) => {
  const { id, ask } = event.data
  try {
    const { result, ms } = await answer(catalog, ask)
    self.postMessage({ id, result, ms })
  } catch (error) {
    self.postMessage({ id, error: error instanceof CatalogError ? error.code : 'internal_error' })
  }
}
