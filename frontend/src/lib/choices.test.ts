import { describe, expect, it } from 'vitest'

import type { Preview } from '../api/mirrors'
import { withChoices } from './choices'

const preview: Preview = {
  server: 2,
  entries: 10,
  found: 7,
  unsure: 2,
  missing: 3,
  more: 0,
  rows: [
    { track: 1, title: 'Eins', artist: 'A', album: 'X', duration: 100, how: 'none', found: null, count: 3 },
    { track: 2, title: 'Zwei', artist: 'A', album: 'X', duration: 100, how: 'song', found: { track: 20, title: 'Zwei (Live)', artist: 'A', album: 'Y', duration: 110 }, count: 2 },
  ],
}

describe('choices by hand counted in the browser', () => {
  it('leaves the counts as they are without a choice', () => {
    expect(withChoices(preview, {})).toEqual(preview)
  })

  it('counts a track chosen for a row not found as found, for every entry of it', () => {
    const found = withChoices(preview, { 1: 99 })
    expect([found.found, found.unsure, found.missing]).toEqual([10, 2, 0])
  })

  it('counts an unsure row chosen by hand as sure, and one left out as not found', () => {
    expect([withChoices(preview, { 2: 21 }).found, withChoices(preview, { 2: 21 }).unsure]).toEqual([7, 0])
    const left = withChoices(preview, { 2: null })
    expect([left.found, left.unsure, left.missing]).toEqual([5, 0, 5])
  })

  it('counts a row not found and left out as not found once', () => {
    const left = withChoices(preview, { 1: null })
    expect([left.found, left.missing]).toEqual([7, 3])
  })
})
