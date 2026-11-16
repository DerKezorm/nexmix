"""``match.py`` is strict where a sure finding could be another recording: a title of no song of its own ("Intro",
"Track 3") is never sure by its length alone, nor a track of a live album found on a studio album (or back), nor
"A & B" found as "A"; a renamed album takes the track at the entry's place only when its title is the entry's (nearly
written alike), another song or a remix there is only worth a look. Each stays findable, as unsure."""

from __future__ import annotations

import pytest

from app.services import match, songs
from app.services.match import Candidate, Wanted


def c(ident: int, title: str, album: str, album_artist: str, duration: int, *, artist: str = "",
      number: int | None = None, disc: int | None = 1) -> Candidate:
    return Candidate(id=ident, title=title, album=album, album_artist=album_artist, artist=artist,
                     duration=duration, song_key=songs.song_key(ident, title, album_artist, artist), number=number,
                     disc=disc)


@pytest.mark.parametrize("title", ["Intro", "Outro", "Interlude", "Skit", "Untitled", "Track 3", "07", "Titel 12"])
def test_a_title_of_no_song_of_its_own_on_another_album_is_never_sure(title: str) -> None:
    finder = match.Finder([c(1, title, "Second Light", "Brakkel", 61_000, number=1)])
    found = finder.find(Wanted(title=title, artist="Brakkel", album="First Light", duration=60_000,
                               album_artist="Brakkel"))
    assert found is not None and not found.sure and found.how in match.UNSURE


def test_a_title_of_no_song_of_its_own_on_its_own_album_is_sure() -> None:
    finder = match.Finder([c(1, "Intro", "First Light", "Brakkel", 61_000, number=1)])
    found = finder.find(Wanted(title="Intro", artist="Brakkel", album="First Light", duration=60_000,
                               album_artist="Brakkel"))
    assert found is not None and found.sure and found.how == "album"


def test_a_live_track_without_live_in_its_title_is_not_the_studio_one() -> None:
    finder = match.Finder([c(1, "Kupferstrand", "Wolkenfabrik", "Venrarik", 214_000, number=2)])
    found = finder.find(Wanted(title="Kupferstrand", artist="Venrarik", album="Live at the Harbour 1990",
                               duration=216_000, album_artist="Venrarik"))
    assert found is not None and not found.sure
    back = match.Finder([c(1, "Kupferstrand", "Unplugged in Bergen", "Venrarik", 214_000, number=2)])
    found = back.find(Wanted(title="Kupferstrand", artist="Venrarik", album="Wolkenfabrik", duration=215_000,
                             album_artist="Venrarik"))
    assert found is not None and not found.sure


def test_a_best_of_track_still_finds_the_studio_one_surely() -> None:
    finder = match.Finder([c(1, "Betonblumen", "Neonstadt", "Brakkel", 262_000, number=2),
                           c(2, "Betonblumen (Live)", "Live", "Brakkel", 263_000, number=2)])
    found = finder.find(Wanted(title="Betonblumen", artist="Brakkel", album="The Very Best Of", duration=261_000,
                               album_artist="Brakkel"))
    assert found is not None and found.track.id == 1 and found.sure


def test_a_duo_is_not_its_first_performer() -> None:
    finder = match.Finder([c(1, "Stille", "Duette", "Kafin & Orvenna", 200_000, number=1)])
    found = finder.find(Wanted(title="Stille", artist="Kafin", album="Allein", duration=201_000,
                               album_artist="Kafin"))
    assert found is not None and not found.sure


def test_a_guest_still_finds_the_track_surely() -> None:
    finder = match.Finder([c(1, "Nachtzug", "Wolkenfabrik", "Venrarik", 240_000, number=4)])
    found = finder.find(Wanted(title="Nachtzug (feat. Kafin)", artist="Venrarik feat. Kafin", album="Best Of",
                               duration=241_000, album_artist="Venrarik"))
    assert found is not None and found.sure and found.how == "length"


def test_another_album_of_the_same_shape_is_no_rename() -> None:
    # Two live albums of one tour: the same set list, lengths within three seconds at most places.
    berlin = [(1, n, 200_000 + n * 7_000) for n in range(1, 11)]
    paris = [c(100 + n, f"Song {n}", "Live in Paris", "Orvenna", 200_000 + n * 7_000 + 2_000, number=n)
             for n in range(1, 11)]
    paris[3] = c(104, "Ein anderes Lied", "Live in Paris", "Orvenna", 228_000 + 1_000, number=4)
    finder = match.Finder(paris)
    found = finder.find(Wanted(title="Song 4 (Berlin Version)", artist="Orvenna", album="Live in Berlin",
                               duration=228_000, album_artist="Orvenna", number=4, disc=1, shape=tuple(berlin)))
    assert found is None or not found.sure


def test_a_renamed_album_takes_a_remix_at_the_place_as_unsure_only() -> None:
    shape = ((1, 1, 200_000), (1, 2, 180_000), (1, 3, 240_000))
    finder = match.Finder([
        c(5, "Rafters (Barn Mix)", "Barn Sessions", "Lumen Crane", 201_000, number=1),
        c(6, "Second", "Barn Sessions", "Lumen Crane", 181_000, number=2),
        c(7, "Third", "Barn Sessions", "Lumen Crane", 239_000, number=3),
    ])
    wanted = Wanted(title="Rafters", artist="Lumen Crane", album="Attic Tapes", duration=200_000,
                    album_artist="Lumen Crane", number=1, disc=1, shape=shape)
    found = finder.find(wanted)
    assert found is not None and not found.sure
    # Spelt nearly alike: the same track, sure.
    finder = match.Finder([c(5, "Rafter", "Barn Sessions", "Lumen Crane", 201_000, number=1),
                           c(6, "Second", "Barn Sessions", "Lumen Crane", 181_000, number=2),
                           c(7, "Third", "Barn Sessions", "Lumen Crane", 239_000, number=3)])
    found = finder.find(wanted)
    assert found is not None and found.sure and found.how == "renamed"


@pytest.mark.parametrize(("one", "other", "alike"), [
    ("Rafters", "Rafter", True),
    ("Kupferstrand", "Kupfer Strand", True),
    ("Rafters", "Rafters (Barn Mix)", False),
    ("Rafters", "Rafters (Live)", False),
    ("Song 4", "Ein anderes Lied", False),
    ("Intro", "Intro", True),
])
def test_titles_nearly_alike(one: str, other: str, alike: bool) -> None:
    assert match.similar(one, other) is alike


def test_unsure_findings_never_take_a_track_a_sure_one_needs() -> None:
    pool = [c(1, "Intro", "Second Light", "Brakkel", 60_000, number=1)]
    loose = Wanted(title="Intro", artist="Brakkel", album="First Light", duration=60_000, album_artist="Brakkel")
    sure = Wanted(title="Intro", artist="Brakkel", album="Second Light", duration=60_000, album_artist="Brakkel")
    found = match.assign([loose, sure], pool)
    assert found[0] is None and found[1] is not None and found[1].how == "album"


def test_what_only_the_length_found_for_such_a_title_is_kept_as_loose() -> None:
    # Two and a half seconds apart: beyond the step on the title alone, within the one on the length.
    finder = match.Finder([c(1, "Intro", "Second Light", "Brakkel", 62_500, number=1)])
    found = finder.find(Wanted(title="Intro", artist="Brakkel", album="First Light", duration=60_000,
                               album_artist="Brakkel"))
    assert found is not None and found.how == "loose" and not found.sure
