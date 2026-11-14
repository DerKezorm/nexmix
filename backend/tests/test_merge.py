"""The three-way merge as a pure function: the rules one by one, the moves that sort Plex, and random runs of changes
on both sides (nothing either side kept or added goes missing, nothing removed comes back unasked, no track twice,
both sides end the same)."""

from __future__ import annotations

import random

import pytest

from app.services import merge
from app.services.merge import Kept

LOCAL, REMOTE = 1000, 2000


def base_of(tracks: list[int]) -> list[Kept]:
    return [Kept(track, track + LOCAL, track + REMOTE) for track in tracks]


def side(tracks: list[int], offset: int, fresh: tuple[int, ...] = ()) -> list[tuple[int, int | None]]:
    """A side holding ``tracks``; the ones in ``fresh`` carry a new id (taken out and put in again there)."""
    return [(track, track + offset + (500 if track in fresh else 0)) for track in tracks]


def run(base: list[int], local: list[int], remote: list[int], *, local_fresh: tuple[int, ...] = (),
        remote_fresh: tuple[int, ...] = ()) -> merge.Merged:
    return merge.merge(base_of(base), side(local, LOCAL, local_fresh), side(remote, REMOTE, remote_fresh))


# --- The rules ---------------------------------------------------------------------------------------------------------


def test_nothing_changed_is_nothing_to_do() -> None:
    found = run([1, 2, 3], [1, 2, 3], [1, 2, 3])
    assert found.order == [1, 2, 3]
    assert not (found.local_adds or found.local_removes or found.remote_adds or found.remote_removes)
    assert found.conflict is None


def test_both_add_and_both_keep_what_the_other_added() -> None:
    found = run([1, 2, 3], [1, 2, 3, 4], [1, 2, 3, 5])
    assert found.order == [1, 2, 3, 5, 4] or found.order == [1, 2, 3, 4, 5]
    assert sorted(found.order) == [1, 2, 3, 4, 5]
    assert found.local_adds == [5] and found.remote_adds == [4]


def test_both_add_the_same_track_and_it_is_there_once() -> None:
    found = run([1, 2], [1, 2, 7], [1, 2, 7])
    assert found.order == [1, 2, 7]
    assert not found.local_adds and not found.remote_adds


def test_both_take_out_the_same_and_it_is_gone() -> None:
    found = run([1, 2, 3], [1, 3], [1, 3])
    assert found.order == [1, 3]
    assert not found.local_removes and not found.remote_removes


def test_one_side_takes_out_and_the_other_follows() -> None:
    found = run([1, 2, 3], [1, 2, 3], [1, 3])
    assert found.order == [1, 3] and found.local_removes == [2]
    found = run([1, 2, 3], [2, 3], [1, 2, 3])
    assert found.order == [2, 3] and found.remote_removes == [1]


def test_adding_wins_over_taking_out() -> None:
    # nexmix took 2 out, Plex took it out and put it in again (a new playlistItemID): it stays.
    found = run([1, 2, 3], [1, 3], [1, 3, 2], remote_fresh=(2,))
    assert 2 in found.order and found.local_adds == [2]
    # Plex took 2 out, nexmix took it out and put it in again (a new entry): it stays.
    found = run([1, 2, 3], [1, 3, 2], [1, 3], local_fresh=(2,))
    assert 2 in found.order and found.remote_adds == [2]
    # Plex added 9 (new), nexmix never had it: it stays.
    found = run([1], [], [1, 9])
    assert found.order == [9]


def test_only_plex_sorted_and_its_order_counts() -> None:
    found = run([1, 2, 3, 4], [1, 2, 3, 4], [4, 3, 2, 1])
    assert found.order == [4, 3, 2, 1]
    assert found.remote_sorted and not found.local_sorted and found.conflict is None


def test_only_nexmix_sorted_and_its_order_counts() -> None:
    found = run([1, 2, 3, 4], [2, 1, 3, 4], [1, 2, 3, 4, 5])
    assert found.order[:4] == [2, 1, 3, 4] and found.order[-1] == 5


def test_both_sorted_differently_and_nexmix_wins_with_the_note() -> None:
    found = run([1, 2, 3, 4], [4, 1, 2, 3], [2, 1, 3, 4])
    assert found.order == [4, 1, 2, 3]
    assert found.conflict == [2, 1, 3, 4]


def test_both_sorted_alike_is_no_conflict() -> None:
    found = run([1, 2, 3], [3, 2, 1], [3, 2, 1])
    assert found.order == [3, 2, 1] and found.conflict is None


def test_an_entry_added_on_one_side_lands_after_the_one_it_followed() -> None:
    found = run([1, 2, 3], [1, 2, 3], [1, 8, 2, 3])
    assert found.order == [1, 8, 2, 3]
    found = run([1, 2, 3], [9, 1, 2, 3], [1, 2, 3])
    assert found.order == [9, 1, 2, 3]


def test_a_write_that_broke_off_counts_nothing_of_plex_order_or_of_what_it_had_still_to_add() -> None:
    # nexmix wanted [3, 1, 2, 4]; Plex got half of it: 4 not yet added, the order half way.
    base = [Kept(3, 3 + LOCAL, 3 + REMOTE), Kept(1, 1 + LOCAL, 1 + REMOTE), Kept(2, 2 + LOCAL, 2 + REMOTE),
            Kept(4, 4 + LOCAL, None)]
    found = merge.merge(base, side([3, 1, 2, 4], LOCAL), side([1, 3, 2, 7], REMOTE),
                        remote_order_counts=False, remote_unwritten={4})
    assert [track for track in found.order if track != 7] == [3, 1, 2, 4] and 7 in found.order
    assert found.remote_adds == [4] and not found.local_removes


def test_while_a_write_is_open_what_plex_lacks_of_the_rest_was_taken_out_there() -> None:
    base = [Kept(1, 1 + LOCAL, 1 + REMOTE), Kept(2, 2 + LOCAL, 2 + REMOTE), Kept(4, 4 + LOCAL, None)]
    found = merge.merge(base, side([1, 2, 4], LOCAL), side([2], REMOTE),
                        remote_order_counts=False, remote_unwritten={4})
    assert found.order == [2, 4] and found.local_removes == [1]


def test_a_removal_still_to_be_written_is_no_add_of_plex() -> None:
    # nexmix took 2 out; the write broke off before the delete: Plex still has 2 with the id of the base.
    base = [Kept(1, 1 + LOCAL, 1 + REMOTE), Kept(2, 2 + LOCAL, 2 + REMOTE), Kept(3, 3 + LOCAL, 3 + REMOTE)]
    found = merge.merge(base, side([1, 3], LOCAL), side([1, 2, 3], REMOTE), remote_order_counts=False)
    assert found.order == [1, 3] and found.remote_removes == [2]
    # Put in again in Plex meanwhile (a new id): adding wins.
    found = merge.merge(base, side([1, 3], LOCAL), [(1, 1 + REMOTE), (2, 99), (3, 3 + REMOTE)],
                        remote_order_counts=False)
    assert found.order == [1, 2, 3] and found.local_adds == [2]


def test_the_name() -> None:
    assert merge.name("Alt", "Alt", "Neu in Plex") == "Neu in Plex"
    assert merge.name("Alt", "Neu hier", "Alt") == "Neu hier"
    assert merge.name("Alt", "Neu hier", "Neu in Plex") == "Neu hier"
    assert merge.name(None, "Hier", "Dort") == "Hier"
    assert merge.name("Alt", "Alt", "   ") == "Alt"


def test_arrange_sorts_what_it_names_and_keeps_the_rest_in_place() -> None:
    assert merge.arrange([1, 2, 3, 4], [4, 3, 2, 1]) == [4, 3, 2, 1]
    assert merge.arrange([1, 2, 9, 3], [3, 2, 1]) == [3, 2, 9, 1]
    assert merge.arrange([1, 2], [5, 2, 1]) == [2, 1]


# --- Sorting Plex --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("wanted", [[1, 2, 3, 4, 5], [5, 4, 3, 2, 1], [2, 1, 3, 4, 5], [1, 3, 4, 5, 2],
                                    [5, 1, 2, 3, 4], [3, 5, 1, 4, 2]])
def test_the_moves_sort_plex_and_are_as_few_as_can_be(wanted: list[int]) -> None:
    current = [(100 + track, track) for track in [1, 2, 3, 4, 5]]
    steps = merge.moves(current, wanted)
    assert [track for _item, track in merge.apply_moves(current, steps)] == wanted
    assert len(steps) <= len(wanted) - 1
    if wanted == [3, 5, 1, 4, 2]:
        # The longest run already in order (1, 4 or 3, 4 ...) stays: three moves, not five.
        assert len(steps) == 3
    if wanted == [1, 2, 3, 4, 5]:
        assert steps == []
    if wanted == [5, 1, 2, 3, 4]:
        # One move, to the start: without a place (never "after=0").
        assert steps == [(105, None)]


def test_a_move_names_as_its_place_only_entries_it_was_given() -> None:
    current = [(11, 1), (12, 2), (13, 3), (19, 9)]
    for item, after in merge.moves(current, [3, 9, 1, 2]):
        assert item in {11, 12, 13, 19}
        assert after is None or after in {11, 12, 13, 19}


# --- Random runs ---------------------------------------------------------------------------------------------------------


def _change(rng: random.Random, tracks: list[int], fresh: set[int], pool: list[int]) -> None:
    """One change on a side, as a person would make it: add, take out, move, or take out and put in again."""
    kind = rng.choice(("add", "remove", "move", "again"))
    if kind == "add":
        new = [track for track in pool if track not in tracks]
        if new:
            track = rng.choice(new)
            tracks.insert(rng.randint(0, len(tracks)), track)
            fresh.add(track)
    elif kind == "remove" and tracks:
        track = rng.choice(tracks)
        tracks.remove(track)
        fresh.discard(track)
    elif kind == "move" and len(tracks) > 1:
        track = tracks.pop(rng.randrange(len(tracks)))
        tracks.insert(rng.randint(0, len(tracks)), track)
    elif kind == "again" and tracks:
        track = rng.choice(tracks)
        tracks.remove(track)
        tracks.append(track)
        fresh.add(track)


@pytest.mark.parametrize("seed", range(400))
def test_random_changes_on_both_sides_lose_nothing_and_end_alike(seed: int) -> None:
    rng = random.Random(seed)
    pool = list(range(1, 25))
    base = rng.sample(pool, rng.randint(0, 12))
    local, remote = list(base), list(base)
    local_fresh: set[int] = set()
    remote_fresh: set[int] = set()
    for _step in range(rng.randint(0, 6)):
        _change(rng, local, local_fresh, pool)
    for _step in range(rng.randint(0, 6)):
        _change(rng, remote, remote_fresh, pool)
    found = merge.merge(base_of(base), side(local, LOCAL, tuple(local_fresh)),
                        side(remote, REMOTE, tuple(remote_fresh)))
    order = found.order
    # No track twice.
    assert len(order) == len(set(order))
    result = set(order)
    in_base = set(base)
    for track in local:
        kept_by_other = track in remote or track not in in_base or track in local_fresh
        if kept_by_other:
            assert track in result, ("lost a track nexmix holds", track)
    for track in remote:
        if track in local or track not in in_base or track in remote_fresh:
            assert track in result, ("lost a track Plex holds", track)
    for track in in_base:
        if track not in local and track not in remote_fresh:
            assert track not in result, ("came back after nexmix took it out", track)
        if track not in remote and track not in local_fresh:
            assert track not in result, ("came back after Plex took it out", track)
    # Nothing from nowhere.
    assert result <= set(local) | set(remote)
    # Each side, given what the merge says it lacks and has too much, ends as the merge.
    for held, adds, removes in ((local, found.local_adds, found.local_removes),
                                (remote, found.remote_adds, found.remote_removes)):
        after = [track for track in held if track not in removes] + list(adds)
        assert sorted(after) == sorted(order)
    # Plex sorted by the moves ends in the merged order.
    plex = [(index + 1, track) for index, track in enumerate(remote) if track in result]
    plex += [(500 + index, track) for index, track in enumerate(found.remote_adds)]
    assert [track for _item, track in merge.apply_moves(plex, merge.moves(plex, order))] == order
    # Only Plex sorted: Plex's order of what it kept stands.
    if found.remote_sorted and not found.local_sorted:
        kept = [track for track in remote if track in result]
        assert [track for track in order if track in set(kept)] == kept


def test_a_long_list_merges_fast() -> None:
    import time

    base = list(range(1, 5001))
    local = base[:]
    remote = list(reversed(base))
    started = time.perf_counter()
    found = merge.merge(base_of(base), side(local, LOCAL), side(remote, REMOTE))
    steps = merge.moves([(track + REMOTE, track) for track in remote], found.order)
    assert found.order == remote and steps == []
    assert time.perf_counter() - started < 2.0
