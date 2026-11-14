import { avatarUrl } from '../api/client'
import { useSignedIn } from '../state/signedIn'

/** The colours of the circles; the first is the accent, kept for the one who looks at the page. */
const COLORS = ['#5b2ee6', '#e0553d', '#1f9d63', '#c27a00', '#2f7bff', '#c43d8f', '#0f8f9a', '#7a5c2e']

/** A colour from the name, so a person keeps the colour of their circle everywhere. */
function colorOf(name: string): string {
  let hash = 0
  for (const char of name) hash = (hash * 31 + char.codePointAt(0)!) >>> 0
  return COLORS[1 + (hash % (COLORS.length - 1))]
}

/** A person as their picture, or a round letter in their colour (the accent for oneself). */
export function Avatar({ person, size = 26 }: { person: { id: number; name: string; display_name?: string; avatar: string | null }; size?: number }) {
  const me = useSignedIn()
  const shown = person.display_name || person.name
  const url = avatarUrl(person)
  const box = { width: size, height: size }
  if (url) return <img src={url} alt="" title={shown} style={box} className="shrink-0 rounded-full object-cover" />
  const own = me !== null && person.id !== 0 && person.id === me.id
  return (
    <span
      title={shown}
      className="inline-flex shrink-0 items-center justify-center rounded-full font-bold text-white"
      style={{ ...box, fontSize: size >= 40 ? Math.round(size * 0.42) : size >= 26 ? 12 : 10, background: own ? COLORS[0] : colorOf(person.name) }}
    >
      {(shown.trim()[0] ?? '?').toUpperCase()}
    </span>
  )
}
