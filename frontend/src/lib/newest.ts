/**
 * Answers that may come back in another order than they were asked: each asks for a ticket before it goes, and counts
 * only when no newer one has counted meanwhile. An event takes its ticket when it comes, so an answer that left before
 * it never covers it again (the number of guests after "End invitation", say).
 */
export type Newest = {
  /** A ticket for a question about to go out, or for news that just came. */
  ticket: () => number
  /** Whether what came with this ticket is still the newest; when it is, it is from now on. */
  take: (ticket: number) => boolean
}

export function newest(): Newest {
  let issued = 0
  let taken = 0
  return {
    ticket: () => ++issued,
    take: (ticket) => {
      if (ticket < taken) return false
      taken = ticket
      return true
    },
  }
}
