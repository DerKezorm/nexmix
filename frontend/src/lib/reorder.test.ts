/** Where a dragged entry lands: the entry it then follows (null: the start), nothing when it stays. */
import type { Entry } from '../api/playlists'
import { dropAfter } from './reorder'

const items = [1, 2, 3, 4].map((id) => ({ id }) as Entry)

describe('dropping a dragged entry', () => {
  it('places it after the entry above the gap it is dropped into', () => {
    expect(dropAfter(items, 0, 3)).toBe(3)
    expect(dropAfter(items, 0, 4)).toBe(4)
    expect(dropAfter(items, 3, 0)).toBeNull()
    expect(dropAfter(items, 3, 1)).toBe(1)
    expect(dropAfter(items, 1, 3)).toBe(3)
  })

  it('does nothing when it is dropped where it was', () => {
    expect(dropAfter(items, 1, 1)).toBeUndefined()
    expect(dropAfter(items, 1, 2)).toBeUndefined()
    expect(dropAfter(items, 3, 4)).toBeUndefined()
  })
})
