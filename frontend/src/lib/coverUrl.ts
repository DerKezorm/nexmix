/**
 * Where a cover is fetched: the catalogue's route for an account (`/api/v1/catalog/cover`), the guest's own for a guest
 * (`/api/v1/guest/cover`), the link's for the page before the name. The covers below a provider take its address.
 */
import { createContext, useContext } from 'react'

import { catalogApi } from '../api/plex'

export type CoverUrl = (album: number, size: 80 | 200 | 400) => string

export const CoverSource = createContext<CoverUrl>(catalogApi.cover)

export function useCoverUrl(): CoverUrl {
  return useContext(CoverSource)
}
