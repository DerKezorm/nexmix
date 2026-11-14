import { createContext, useContext } from 'react'

import type { Me } from '../api/client'

/** The signed-in account, given by the sign-in state to everything below it; null outside it (a guest's page, a test
 * that shows one part alone). */
export const SignedIn = createContext<Me | null>(null)

export function useSignedIn(): Me | null {
  return useContext(SignedIn)
}
