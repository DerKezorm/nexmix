"""The three-way merge of a playlist kept in two places, nexmix and Plex, and the steps that bring Plex to a wanted
order. Pure functions: no database, no Plex, no clock.

**What is compared.** A playlist holds a track at most once (Plex refuses a second time), so the track (Plex's
``ratingKey``) names an entry on both sides. Each side gives its entries in order, each with the id that side gave the
entry (nexmix: the entry's id; Plex: the ``playlistItemID``). The *base* is the last state both sides agreed on, with
both ids. An entry whose id differs from the base was taken out and put in again on that side: it counts as added.

**The rules.**

* What one side added stays, whatever the other did: adding wins against taking out.
* What one side took out goes, unless the other side added it (again).
* The order of the side that sorted is taken; when only one side sorted, its order, else nexmix's. When both sorted,
  and differently, nexmix's order stands and Plex's is handed back (``Merged.conflict``), for the note "sorted in Plex
  at the same time" and its undo.
* Entries one side added are put after the entry they followed on that side; added at the end there, at the end.
* No track twice.
* A name changed on one side is taken; changed on both, nexmix's.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Kept:
    """An entry of the base: the track, the id nexmix gave it, the id Plex gave it (None: not known)."""

    track: int
    local: int | None
    remote: int | None


#: An entry of one side: (track, the id that side gave it). The id may be None (not known).
Entry = tuple[int, int | None]


@dataclass
class Merged:
    """The playlist both sides should hold, in order, and what that means for each side."""

    order: list[int]
    #: Tracks nexmix gets from Plex, and tracks nexmix takes out.
    local_adds: list[int] = field(default_factory=list)
    local_removes: list[int] = field(default_factory=list)
    #: Tracks Plex gets, and tracks Plex takes out.
    remote_adds: list[int] = field(default_factory=list)
    remote_removes: list[int] = field(default_factory=list)
    local_sorted: bool = False
    remote_sorted: bool = False
    #: Both sides sorted, each differently: Plex's order (all its tracks), for the note and its undo.
    conflict: list[int] | None = None


def _ids(side: Sequence[Entry]) -> dict[int, int | None]:
    found: dict[int, int | None] = {}
    for track, ident in side:
        found.setdefault(int(track), ident)
    return found


def _tracks(side: Sequence[Entry]) -> list[int]:
    seen: set[int] = set()
    found: list[int] = []
    for track, _ident in side:
        if int(track) not in seen:
            seen.add(int(track))
            found.append(int(track))
    return found


def _sorted(base_order: list[int], side_order: list[int], same: set[int]) -> bool:
    """Whether the side changed the order of the entries it kept from the base (``same``)."""
    return [track for track in base_order if track in same] != [track for track in side_order if track in same]


def place(skeleton: Sequence[int], extra: Sequence[int], guide: Sequence[int]) -> list[int]:
    """``skeleton`` in its order, and each track of ``extra`` after the track it follows in ``guide`` (the nearest one
    before it there that is already placed; none: at the start). Tracks of ``extra`` behind the last track of the
    skeleton in ``guide`` (added at the end) go at the end, and so do the ones not in ``guide``, in their order."""
    base = list(dict.fromkeys(skeleton))
    placed = set(base)
    wanted = [track for track in dict.fromkeys(extra) if track not in placed]
    wanted_set = set(wanted)
    # Behind the last track of the skeleton in the guide: added at the end there, so at the end here too.
    guide = list(guide)
    tail_from = max((number for number, track in enumerate(guide) if track in placed), default=-1) + 1
    tail = [track for track in guide[tail_from:] if track in wanted_set]
    tail_set = set(tail)
    # What goes right after a track (None: the start). Walking the guide in order, the last placed track before an
    # extra one is its place; an extra one placed becomes the place of the next.
    after: dict[int | None, list[int]] = {}
    last: int | None = None
    for track in guide[:tail_from]:
        if track in wanted_set and track not in placed:
            after.setdefault(last, []).append(track)
            placed.add(track)
            last = track
        elif track in placed:
            last = track
    result: list[int] = []

    def emit(start: int | None) -> None:
        stack = list(reversed(after.get(start, [])))
        while stack:
            track = stack.pop()
            result.append(track)
            stack.extend(reversed(after.get(track, [])))

    emit(None)
    for track in base:
        result.append(track)
        emit(track)
    result.extend(tail)
    placed |= tail_set
    result.extend(track for track in wanted if track not in placed)
    return result


def arrange(current: Sequence[int], wanted: Sequence[int]) -> list[int]:
    """``current`` sorted as ``wanted`` says: its tracks that ``wanted`` names in that order, the others after the
    track they followed in ``current``. Nothing comes or goes."""
    present = set(current)
    skeleton = [track for track in dict.fromkeys(wanted) if track in present]
    chosen = set(skeleton)
    return place(skeleton, [track for track in current if track not in chosen], current)


def merge(base: Sequence[Kept], local: Sequence[Entry], remote: Sequence[Entry], *,
          remote_order_counts: bool = True, remote_unwritten: Collection[int] = ()) -> Merged:
    """The merge of the two sides against the base. ``remote_order_counts`` False: Plex's order says nothing (a write
    to Plex broke off half way, the order there is half of nexmix's). ``remote_unwritten``: tracks of the base a write
    that broke off had still to put into Plex; Plex lacking them says nothing (they were not taken out there). Every
    other track of the base Plex lacks was taken out there, also while a write was open."""
    base_ids = {kept.track: kept for kept in base}
    unwritten = set(remote_unwritten)
    base_order = [kept.track for kept in base]
    local_ids, remote_ids = _ids(local), _ids(remote)
    local_order, remote_order = _tracks(local), _tracks(remote)

    def fresh_local(track: int) -> bool:
        kept = base_ids.get(track)
        return kept is None or (kept.local is not None and local_ids.get(track) not in (None, kept.local))

    def fresh_remote(track: int) -> bool:
        kept = base_ids.get(track)
        return kept is None or (kept.remote is not None and remote_ids.get(track) not in (None, kept.remote))

    def in_remote(track: int) -> bool:
        return track in remote_ids or (track in unwritten and track in base_ids)

    def stays(track: int) -> bool:
        here, there = track in local_ids, in_remote(track)
        if here and there:
            return True
        if here:
            # Plex took it out: it goes, unless nexmix put it in (again).
            return fresh_local(track)
        if track in remote_ids:
            # nexmix took it out: it goes, unless Plex put it in (again).
            return fresh_remote(track)
        return False

    keep = {track for track in dict.fromkeys([*local_order, *remote_order]) if stays(track)}

    same_local = {track for track in local_order if track in base_ids and not fresh_local(track)}
    same_remote = {track for track in remote_order if track in base_ids and not fresh_remote(track)}
    local_sorted = _sorted(base_order, local_order, same_local)
    remote_sorted = remote_order_counts and _sorted(base_order, remote_order, same_remote)

    conflict: list[int] | None = None
    if remote_sorted and not local_sorted:
        leader, follower = remote_order, local_order
    else:
        leader, follower = local_order, remote_order
        if remote_sorted and local_sorted:
            common = set(local_order) & set(remote_order)
            if [t for t in local_order if t in common] != [t for t in remote_order if t in common]:
                conflict = list(remote_order)
    skeleton = [track for track in leader if track in keep]
    order = place(skeleton, [track for track in follower if track in keep], follower)

    result = set(order)
    return Merged(
        order=order,
        local_adds=[track for track in order if track not in local_ids],
        local_removes=[track for track in local_order if track not in result],
        remote_adds=[track for track in order if track not in remote_ids],
        remote_removes=[track for track in remote_order if track not in result],
        local_sorted=local_sorted,
        remote_sorted=remote_sorted,
        conflict=conflict,
    )


def name(base: str | None, local: str, remote: str) -> str:
    """The name both sides should carry: a change on one side is taken, on both nexmix's."""
    if base is not None and local == base and remote != base and remote.strip():
        return remote
    return local


# --- Bringing Plex to the wanted order --------------------------------------------------------------------------------


def _longest_rising(values: Sequence[int]) -> set[int]:
    """The indexes of a longest strictly rising run of ``values`` (patience sorting): the entries that may stay."""
    tails: list[int] = []
    tail_index: list[int] = []
    before: list[int] = [-1] * len(values)
    for index, value in enumerate(values):
        low, high = 0, len(tails)
        while low < high:
            middle = (low + high) // 2
            if tails[middle] < value:
                low = middle + 1
            else:
                high = middle
        if low > 0:
            before[index] = tail_index[low - 1]
        if low == len(tails):
            tails.append(value)
            tail_index.append(index)
        else:
            tails[low] = value
            tail_index[low] = index
    found: set[int] = set()
    index = tail_index[-1] if tail_index else -1
    while index >= 0:
        found.add(index)
        index = before[index]
    return found


def moves(current: Sequence[tuple[int, int]], wanted: Sequence[int]) -> list[tuple[int, int | None]]:
    """The moves that sort ``current`` (``(item id, track)`` in Plex's order) as ``wanted`` (tracks): ``(item id, the
    item id it goes after)``, None for the start. Only the tracks both name are sorted; as few moves as there are
    entries outside the longest run already in order. Each move names as its place an entry of ``current`` only."""
    position = {track: number for number, track in enumerate(dict.fromkeys(wanted))}
    present = [(item, track) for item, track in current if track in position]
    stay = _longest_rising([position[track] for _item, track in present])
    staying = {present[index][1] for index in stay}
    item_of = {track: item for item, track in present}
    order = [track for track in position if track in item_of]
    found: list[tuple[int, int | None]] = []
    for number, track in enumerate(order):
        if track in staying:
            continue
        found.append((item_of[track], item_of[order[number - 1]] if number > 0 else None))
    return found


def apply_moves(current: Iterable[tuple[int, int]], steps: Iterable[tuple[int, int | None]]) -> list[tuple[int, int]]:
    """What Plex does with the moves (for the tests and for checking a plan): each item right after its place."""
    result = list(current)
    for item, after in steps:
        entry = next(one for one in result if one[0] == item)
        result.remove(entry)
        at = 0 if after is None else next(number for number, one in enumerate(result) if one[0] == after) + 1
        result.insert(at, entry)
    return result
