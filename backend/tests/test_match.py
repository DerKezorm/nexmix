"""Finding a track on another server (``services/match.py``): the steps in their order, their tolerances, and never one
track for two entries, also over many random catalogues."""

from __future__ import annotations

import random

import pytest

from app.services import match, songs
from app.services.match import Candidate, Wanted


def candidate(ident: int, title: str = "Rafters", album: str = "Attic Tapes", artist: str = "Lumen Crane",
              duration: int = 200_000, **extra: object) -> Candidate:
    key = extra.pop("song_key", None) or songs.song_key(ident, title, artist)
    return Candidate(id=ident, title=title, album=album, album_artist=artist, duration=duration,
                     song_key=str(key), **extra)  # type: ignore[arg-type]


WANTED = Wanted(title="Rafters", artist="Lumen Crane", album="Attic Tapes", duration=200_000,
                guid="plex://track/abc", mbid="mb-1")


def test_each_step_in_its_order() -> None:
    # Every step has a candidate of its own; taking away the winner, the next step decides.
    shape = ((1, 1, 200_000), (1, 2, 180_000), (1, 3, 240_000), (1, 4, 150_000))
    wanted = Wanted(title="Rafters", artist="Lumen Crane", album="Attic Tapes", duration=200_000,
                    guid="plex://track/abc", mbid="mb-1", album_artist="Lumen Crane", number=1, disc=1, shape=shape)
    every = [
        candidate(1, guid="plex://track/abc", title="Something Else", album="Other", artist="Nobody"),
        candidate(2, mbid="mb-1", title="Something Else", album="Other", artist="Nobody"),
        candidate(3),
        candidate(4, album="Greatest Barns", duration=201_000),
        # The album under another name: its places hold tracks of the same lengths, the title spelt nearly alike.
        candidate(5, title="Rafter", album="Barn Sessions", number=1, disc=1, duration=201_000),
        candidate(6, title="Second", album="Barn Sessions", number=2, disc=1, duration=181_000),
        candidate(7, title="Third", album="Barn Sessions", number=3, disc=1, duration=239_000),
        candidate(8, album="Live at the Barn", artist="Lumen Crane", duration=260_000,
                  song_key=songs.song_key(0, "Rafters", "Lumen Crane")),
        candidate(9, album="Elsewhere", artist="A Choir", duration=201_500),
    ]
    order = []
    used: set[int] = {6, 7}
    finder = match.Finder(every)
    for _turn in range(len(every) + 1):
        found = finder.find(wanted, used)
        if found is None:
            break
        order.append((found.track.id, found.how))
        used.add(found.track.id)
    assert order == [(1, "guid"), (2, "mbid"), (3, "album"), (4, "length"), (5, "renamed"), (8, "song"),
                     (9, "title")]
    assert [match.Found(every[0], how).sure for how in match.STEPS] == [True, True, True, True, True, True, False,
                                                                        False, False, False]


def test_a_guid_only_its_own_server_knows_is_no_proof() -> None:
    local = Wanted(title="Rafters", artist="Lumen Crane", album="Attic Tapes", guid="local://4711")
    found = match.Finder([candidate(1, guid="local://4711", title="Other", album="Other", artist="Other"),
                          candidate(2)]).find(local)
    assert found is not None and (found.track.id, found.how) == (2, "album")


def test_on_the_album_the_closest_length_within_three_seconds() -> None:
    two = [candidate(1, duration=196_500), candidate(2, duration=202_900)]
    found = match.Finder(two).find(Wanted("Rafters", "Lumen Crane", "Attic Tapes", 200_000))
    assert found is not None and (found.track.id, found.how) == (2, "album")
    # One alone on the album counts whatever its length; of two, none further than three seconds.
    alone = match.Finder([candidate(1, duration=260_000)]).find(Wanted("Rafters", "Lumen Crane", "Attic Tapes",
                                                                       200_000))
    assert alone is not None and alone.how == "album"
    far = [candidate(1, duration=203_001), candidate(2, duration=196_999)]
    found = match.Finder(far).find(Wanted("Rafters", "Lumen Crane", "Attic Tapes", 200_000))
    assert found is None or found.how != "album"


@pytest.mark.parametrize(("delta", "how"), [(0, "length"), (3000, "length"), (3001, "song")])
def test_by_the_artist_within_three_seconds(delta: int, how: str) -> None:
    found = match.Finder([candidate(1, album="Elsewhere", duration=200_000 + delta)]).find(WANTED)
    assert found is not None and found.how == how


@pytest.mark.parametrize(("delta", "found_how"), [(2000, "title"), (2001, None)])
def test_the_title_alone_within_two_seconds(delta: int, found_how: str | None) -> None:
    other = candidate(1, album="Elsewhere", artist="A Choir", duration=200_000 + delta)
    found = match.Finder([other]).find(WANTED)
    assert (found.how if found else None) == found_how


def test_of_the_versions_of_a_song_the_one_shown_first() -> None:
    key = songs.song_key(0, "Rafters", "Lumen Crane")
    versions = [candidate(1, title="Rafters (Live)", album="Live", duration=200_000, shown=False, song_key=key),
                candidate(2, title="Rafters (Remastered)", album="Best Of", duration=250_000, shown=True,
                          song_key=key)]
    found = match.Finder(versions).find(Wanted("Rafters", "Lumen Crane", "Attic Tapes", 200_000))
    assert found is not None and (found.track.id, found.how) == (2, "song")


def test_nothing_alike_is_none() -> None:
    assert match.Finder([candidate(1, title="Dust and Light", artist="Other", album="Other")]).find(WANTED) is None


@pytest.mark.parametrize("seed", range(40))
def test_never_one_track_for_two_entries_in_random_catalogues(seed: int) -> None:
    chance = random.Random(seed)
    titles = ["Rafters", "Dust and Light", "Skylight", "Intro", "Northern Lights", "Glow"]
    artists = ["Lumen Crane", "Mira Solenne", "Dax Orlo"]
    albums = ["Attic Tapes", "Quiet Rooms", "Best Of"]
    pool = []
    for ident in range(1, chance.randint(1, 30)):
        title, artist = chance.choice(titles), chance.choice(artists)
        pool.append(candidate(ident, title=title, album=chance.choice(albums), artist=artist,
                              duration=chance.randint(150_000, 260_000), shown=chance.random() < 0.7,
                              guid=chance.choice(["", f"plex://track/{chance.randint(1, 8)}", "local://1"]),
                              mbid=chance.choice(["", f"mb-{chance.randint(1, 8)}"])))
    wanted = [Wanted(title=chance.choice(titles), artist=chance.choice(artists), album=chance.choice(albums),
                     duration=chance.randint(150_000, 260_000),
                     guid=chance.choice(["", f"plex://track/{chance.randint(1, 8)}"]),
                     mbid=chance.choice(["", f"mb-{chance.randint(1, 8)}"])) for _ in range(chance.randint(0, 25))]
    taken = {one.id for one in pool if chance.random() < 0.1}
    found = match.assign(wanted, pool, taken)
    ids = [hit.track.id for hit in found if hit is not None]
    assert len(ids) == len(set(ids)), "a track for two entries"
    assert not set(ids) & taken, "a track taken before"
    for want, hit in zip(wanted, found, strict=True):
        if hit is None:
            continue
        assert hit.how in match.STEPS
        if hit.how == "guid":
            assert hit.track.guid == want.guid and match.lasting(want.guid)
        elif hit.how == "mbid":
            assert hit.track.mbid == want.mbid
        elif hit.how in ("album", "edition", "length", "title"):
            assert match.title_key(hit.track.title) == match.title_key(want.title)
            limit = match.TITLE_TOLERANCE if hit.how == "title" else match.TOLERANCE
            if hit.how != "album":
                assert abs(hit.track.duration - want.duration) <= limit
