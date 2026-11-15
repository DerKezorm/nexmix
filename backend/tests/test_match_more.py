"""Finding a track on another server (``services/match.py``), the parts added for copying and moving playlists: titles,
albums and people written alike, an album under another name, ties, a sure finding before an unsure one, made-up
libraries with known gaps, and the hit rate on a made-up pair of 50,000 tracks (``match_pairs.py``)."""

from __future__ import annotations

import pytest

from app.services import match
from app.services.match import Candidate, Wanted

from .match_pairs import made_up_pair
from .test_match import candidate


@pytest.mark.parametrize(("one", "other"), [
    ("Northern Lights - Remastered 2014", "Northern Lights"),
    ("Northern Lights (2009 Remaster)", "northern lights"),
    ("Copper Sky (feat. Dax Orlo)", "Copper Sky"),
    ("Copper Sky [ft. Dax Orlo]", "COPPER SKY"),
    ("Slow Tide feat. Dax Orlo", "Slow Tide"),
    ("Slow Tide featuring Dax Orlo", "Slow Tide"),
    ("Fever Glass (Album Version)", "Fever Glass"),
    ("Fever Glass (Mono)", "Fëver Gläss"),
    ("Lichter (Explicit)", "Lichter"),
])
def test_titles_written_alike(one: str, other: str) -> None:
    assert match.title_key(one) == match.title_key(other)


@pytest.mark.parametrize(("one", "other"), [
    ("Northern Lights (Live)", "Northern Lights"),
    ("Slow Tide (Acoustic)", "Slow Tide"),
    ("Night Drive (Part 2)", "Night Drive (Part 1)"),
    ("Paper Planes (Extended Mix)", "Paper Planes"),
    ("Fever Glass - Radio Edit", "Fever Glass"),
])
def test_another_version_is_not_written_alike(one: str, other: str) -> None:
    assert match.title_key(one) != match.title_key(other)


def test_albums_and_people_written_alike() -> None:
    assert match.album_key("Paper Lanterns (Remastered)") == match.album_key("Paper Lanterns")
    assert match.album_key("Lamps (Deluxe Edition)") == match.album_key("lamps")
    assert match.album_key("Harbour Lights Live") != match.album_key("Harbour Lights")
    assert match.person_key("The Velvet Harbour") == match.person_key("velvet harbour")
    assert match.people_keys("Mira Solenne feat. Dax Orlo") >= {"mira solenne"}
    assert match.people_keys("Mira Solenne & Dax Orlo") >= {"mira solenne"}
    assert match.people_keys("Various Artists") == frozenset()


def test_the_album_named_exactly_comes_before_its_edition() -> None:
    two = [candidate(1, album="Attic Tapes (Remastered)", duration=200_000),
           candidate(2, album="Attic Tapes", duration=201_000)]
    found = match.Finder(two).find(Wanted("Rafters", "Lumen Crane", "Attic Tapes", 200_000))
    assert found is not None and (found.track.id, found.how) == (2, "album")
    only = match.Finder([two[0]]).find(Wanted("Rafters", "Lumen Crane", "Attic Tapes", 200_000))
    assert only is not None and (only.track.id, only.how) == (1, "edition") and only.sure


def test_two_compilations_are_not_the_same_artist() -> None:
    other = Candidate(id=1, title="Lichtstreifen", album="Club Mix", album_artist="Various Artists",
                      artist="Somebody Else", duration=222_000)
    found = match.Finder([other]).find(Wanted(title="Lichtstreifen", artist="Vomima", album="Club Naechte",
                                              duration=222_000, album_artist="Various Artists"))
    assert found is not None and found.how == "title" and not found.sure


def test_ties_go_to_the_lower_id() -> None:
    two = [candidate(7, album="Elsewhere", duration=201_000), candidate(3, album="Other Place", duration=199_000)]
    found = match.Finder(two).find(Wanted("Rafters", "Lumen Crane", "Attic Tapes", 200_000))
    assert found is not None and (found.track.id, found.how) == (3, "length")


def test_a_renamed_album_needs_most_of_its_places_to_agree() -> None:
    shape = tuple((1, number, 200_000 + number * 10_000) for number in range(1, 9))
    wanted = Wanted(title="Rafters", artist="Lumen Crane", album="Attic Tapes", duration=210_000,
                    album_artist="Lumen Crane", number=1, disc=1, shape=shape)
    few = [candidate(10 + number, title=f"Other {number}", album="Barn Sessions", number=number, disc=1,
                     duration=200_000 + number * 10_000) for number in range(1, 5)]
    # Four of eight places: not enough (60 %).
    assert match.Finder(few).find(wanted) is None
    more = few + [candidate(20 + number, title=f"Other {number}", album="Barn Sessions", number=number, disc=1,
                            duration=200_000 + number * 10_000) for number in range(5, 7)]
    found = match.Finder(more).find(wanted)
    assert found is not None and (found.track.id, found.how) == (11, "renamed") and found.sure
    # Never without the places of the album, and never two seconds beyond the tolerance.
    assert match.Finder(more).find(Wanted(title="Rafters", artist="Lumen Crane", album="Attic Tapes",
                                          duration=210_000, album_artist="Lumen Crane", number=1)) is None
    late = Wanted(title="Rafters", artist="Lumen Crane", album="Attic Tapes", duration=214_000,
                  album_artist="Lumen Crane", number=1, disc=1, shape=shape)
    assert match.Finder(more).find(late) is None


def test_a_sure_finding_comes_before_an_unsure_one() -> None:
    # The first entry would take track 1 by its title alone; the second is sure of it by its album.
    pool = [candidate(1, title="Rafters", album="Attic Tapes", artist="Lumen Crane", duration=200_000)]
    first = Wanted(title="Rafters", artist="Somebody", album="Elsewhere", duration=200_500)
    second = Wanted(title="Rafters", artist="Lumen Crane", album="Attic Tapes", duration=200_000)
    found = match.assign([first, second], pool)
    assert found[0] is None and found[1] is not None and found[1].how == "album"


def test_made_up_libraries_with_known_gaps() -> None:
    pair = made_up_pair(3000, seed=5)
    found = match.assign(pair.source, pair.target)
    for hit, truth in zip(found, pair.truth, strict=True):
        if truth is None:
            # Not there: nothing sure stands for it.
            assert hit is None or not hit.sure
        elif hit is not None and hit.sure:
            assert hit.track.id == truth
    assert sum(1 for hit in found if hit is not None and hit.how == "renamed") > 0


def test_the_hit_rate_on_a_made_up_pair_of_fifty_thousand_tracks() -> None:
    pair = made_up_pair(50_000)
    found = match.assign(pair.source, match.Finder(pair.target))
    there = sum(1 for truth in pair.truth if truth is not None)
    right = sum(1 for hit, truth in zip(found, pair.truth, strict=True) if hit is not None and hit.track.id == truth)
    sure = [(hit, truth) for hit, truth in zip(found, pair.truth, strict=True) if hit is not None and hit.sure]
    assert right / there >= 0.99
    assert sum(1 for hit, truth in sure if hit.track.id == truth) / len(sure) >= 0.999
    ids = [hit.track.id for hit in found if hit is not None]
    assert len(ids) == len(set(ids))
