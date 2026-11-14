import type { Person } from '../api/playlists'
import { Avatar } from './Avatar'

/** Who added to a playlist, as small round faces one over the other. */
export function People({ people, size = 20 }: { people: Person[]; size?: number }) {
  return (
    <span className="avs">
      {people.map((one, number) => (
        <PersonFace key={`${one.id ?? 'n'}-${one.name}-${number}`} person={one} size={size} />
      ))}
    </span>
  )
}

export function PersonFace({ person, size = 20 }: { person: Person; size?: number }) {
  return <Avatar person={{ id: person.id ?? 0, name: person.name, display_name: person.display_name, avatar: person.id ? person.avatar : null }} size={size} />
}
