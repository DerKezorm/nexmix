/**
 * Before any test: the operator account on the fresh data folder, and the operator's session saved for every test
 * (`storageState`). A second factor is not required from the start, so the first account is in right away. Name and
 * password are made up for the run and live only in the temporary data folder.
 */
import { request, type APIRequestContext, type FullConfig } from '@playwright/test'

export const OPERATOR = { name: 'tester', password: 'e2e test operator password' }
/** The tab header every change needs. */
export const TAB = { 'X-Nexmix-Client': 'tab-e2esetup0' }

async function ok(answer: Awaited<ReturnType<APIRequestContext['post']>>, what: string): Promise<void> {
  if (!answer.ok()) throw new Error(`${what} failed: ${answer.status()} ${await answer.text()}`)
}

export default async function globalSetup(config: FullConfig): Promise<void> {
  const baseURL = config.projects[0].use.baseURL!
  const context = await request.newContext({ baseURL, extraHTTPHeaders: TAB })
  const made = await context.post('/api/v1/setup', { data: { name: OPERATOR.name, password: OPERATOR.password, language: 'en', code: 'e2e-setup-code' } })
  await ok(made, 'setup')
  const { session_stage: stage } = (await made.json()) as { session_stage: string }
  if (stage !== 'full') throw new Error(`setup ended in the stage ${stage}, not in a full session`)
  await ok(await context.get('/api/v1/playlists'), 'overview')
  await context.storageState({ path: process.env.E2E_STATE! })
  await context.dispose()
}
