import type { ReactNode } from 'react'

import { useCoverUrl } from '../lib/coverUrl'

/** The face of a playlist: four covers of its first albums, one large cover when there are fewer, an empty square
 * when there is none. */
export function Mosaic({ covers, size = 200, children }: { covers: number[]; size?: 80 | 200 | 400; children?: ReactNode }) {
  const cover = useCoverUrl()
  if (covers.length >= 4)
    return (
      <div className="mosaic">
        {covers.slice(0, 4).map((album) => (
          <img key={album} src={cover(album, size)} alt="" loading="lazy" />
        ))}
        {children}
      </div>
    )
  return (
    <div className="mosaic one">
      {covers.length > 0 && <img src={cover(covers[0], 400)} alt="" loading="lazy" />}
      {children}
    </div>
  )
}

/** A cover of an album for a row, or the grey square without one. */
export function Cover({ album, has, size = 80, className = '' }: { album: number | null; has: boolean; size?: 80 | 200 | 400; className?: string }) {
  const cover = useCoverUrl()
  return has && album ? <img className={className} src={cover(album, size)} alt="" loading="lazy" /> : <div className={`noimg ${className}`} />
}
