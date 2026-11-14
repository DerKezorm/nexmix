"""The song rules: which tracks are versions of one song, which version is shown, and the original year. Every name is
made up; each case stands for a kind found in a real library of some 52,000 tracks."""

from __future__ import annotations

import pytest

from app.services.songs import (
    Version,
    artist_part,
    is_side_version,
    norm,
    song_key,
    songs,
    split_title,
)


def key(title: str, album_artist: str = "Mira Solenne", track_artist: str = "", track_id: int = 1) -> str:
    return song_key(track_id, title, album_artist, track_artist)


# --- The key -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("title", [
    "Copper Sky",
    "Copper Sky (Remastered 2011)",
    "Copper Sky [2009 Remaster]",
    "Copper Sky (Live)",
    "Copper Sky (Live at the Pier)",
    "Copper Sky (Radio Edit)",
    "Copper Sky (Extended Mix)",
    "Copper Sky (Acoustic Version)",
    "Copper Sky (Demo)",
    "Copper Sky (Instrumental)",
    "Copper Sky (Karaoke Version)",
    "Copper Sky (Clean)",
    "Copper Sky (feat. Dax Orlo)",
    "Copper Sky feat. Dax Orlo",
    "Copper Sky ft. Dax Orlo",
    "Copper Sky featuring Dax Orlo",
    "Copper Sky - Remastered 2014",
    "Copper Sky – Live",
    "Copper Sky — Single Version",
    "Copper Sky ‐ Mono",
    "copper   sky",
    "Cöpper Sky",
    "COPPER SKY!",
    "Copper Sky (Take 3)",
    "Copper Sky (Sped Up)",
])
def test_versions_of_one_song_share_its_key(title: str) -> None:
    assert key(title) == "copper sky|mira solenne"


@pytest.mark.parametrize(("one", "other"), [
    ("Glasshouse (Intro)", "Glasshouse"),
    ("Glasshouse (Interlude)", "Glasshouse"),
    ("Glasshouse (Outro)", "Glasshouse"),
    ("Glasshouse (Reprise)", "Glasshouse"),
    ("Night Drive (Part 1)", "Night Drive (Part 2)"),
    ("Night Drive (Pt. 1)", "Night Drive (Pt. 2)"),
    ("Night Drive (Teil 1)", "Night Drive (Teil 2)"),
    ("Etude (No. 1)", "Etude (No. 2)"),
    ("Etude (Nr. 1)", "Etude (Nr. 2)"),
    ("Sonata (Op. 27)", "Sonata (Op. 31)"),
    ("Waves (2)", "Waves (3)"),
    ("Waves (II)", "Waves (III)"),
    ("Night Drive (Part 1)", "Night Drive"),
])
def test_a_piece_of_its_own_keeps_its_addition_in_the_key(one: str, other: str) -> None:
    assert key(one) != key(other)


def test_the_kept_addition_is_the_same_whatever_the_writing() -> None:
    assert key("Night Drive (Part 1)") == key("Night Drive [part 1]") == key("Night Drive (PART 1) (Remastered)")


@pytest.mark.parametrize("title", ["Intro", "Outro", "Interlude", "Skit", "Prelude", "Reprise", "Untitled",
                                   "Bonus Track", "INTRO", "Intro (Live)", "Untitled - Demo"])
def test_a_general_title_is_a_piece_of_its_own_on_every_album(title: str) -> None:
    first = song_key(101, title, "The Velvet Harbour")
    second = song_key(102, title, "The Velvet Harbour")
    assert first != second
    assert first == "#101"


def test_the_same_title_by_another_artist_is_another_song() -> None:
    assert key("Northern Lights", "The Velvet Harbour") != key("Northern Lights", "Dax Orlo")


def test_a_leading_the_does_not_part_an_artist_from_itself() -> None:
    assert key("Northern Lights", "The Velvet Harbour") == key("Northern Lights", "Velvet Harbour")
    assert artist_part("The The") == "the"
    assert artist_part("Theodor Vale") == "theodor vale"


@pytest.mark.parametrize(("album_artist", "track_artist", "expected"), [
    ("Various Artists", "Mira Solenne", "mira solenne"),
    ("Various Artists", "Juno Reyes & The Lamps", "juno reyes"),
    ("Various Artists", "Juno Reyes feat. Dax Orlo", "juno reyes"),
    ("Various Artists", "Juno Reyes, Dax Orlo", "juno reyes"),
    ("Various Artists", "The Velvet Harbour", "velvet harbour"),
    ("VA", "Mira Solenne", "mira solenne"),
    ("Verschiedene Interpreten", "Mira Solenne", "mira solenne"),
    ("Various Artists", "", "various artists"),
    ("Mira Solenne", "Mira Solenne feat. Dax Orlo", "mira solenne"),
])
def test_a_compilation_takes_the_first_name_of_the_tracks_own_artist(album_artist: str, track_artist: str,
                                                                     expected: str) -> None:
    assert artist_part(album_artist, track_artist) == expected


def test_a_compilation_track_joins_the_song_of_its_artist() -> None:
    assert key("Paper Planes", "Various Artists", "Juno Reyes & The Lamps") == key("Paper Planes", "Juno Reyes")


def test_a_title_that_is_only_brackets_keeps_what_it_has() -> None:
    assert split_title("(Untitled Sketch)").core == "untitled sketch"
    assert split_title("[ ]").core in ("[ ]", "")
    assert key("(I Can Wait) Copper Sky") == key("Copper Sky")


def test_letters_beyond_latin_count_as_letters() -> None:
    assert norm("Nächte über Köln") == "nachte uber koln"
    assert norm("Straße") == "strasse"
    assert norm("夜の海") == "夜の海"
    assert key("夜の海") != key("朝の海")


@pytest.mark.parametrize("title", ["", " ", "   ", "\t"])
def test_an_empty_title_is_a_piece_of_its_own(title: str) -> None:
    assert key(title, track_id=1) == "#1"
    assert key(title, track_id=1) != key(title, track_id=2)


@pytest.mark.parametrize(("one", "other"), [
    ("Night Drive - Part 1", "Night Drive - Part 2"),
    ("Night Drive – Pt. 1", "Night Drive – Pt. 2"),
    ("Etude - No. 1", "Etude - No. 2"),
    ("Waves - II", "Waves - III"),
    ("Night Drive - Part 1", "Night Drive"),
])
def test_a_piece_of_its_own_after_a_dash_keeps_its_addition_too(one: str, other: str) -> None:
    assert key(one) != key(other)


def test_a_piece_named_after_a_dash_or_in_brackets_is_the_same_piece() -> None:
    assert key("Night Drive - Part 1") == key("Night Drive (Part 1)") == key("Night Drive - Part 1 - Remastered")
    assert key("Night Drive - Remastered 2011") == key("Night Drive")


@pytest.mark.parametrize(("one", "other"), [
    ("A Feat of Strength", "A Feat of Clay"),
    ("Feat of Engineering", "Feat of Clay"),
    ("The Fifth Featurette", "The Fifth"),
    ("Left Feat Dancing", "Left Feat"),
])
def test_feat_without_its_dot_is_a_word_of_the_title(one: str, other: str) -> None:
    assert key(one) != key(other)


@pytest.mark.parametrize("title", ["Copper Sky feat. Dax Orlo", "Copper Sky Feat. Dax Orlo", "Copper Sky ft. Dax Orlo",
                                   "Copper Sky featuring Dax Orlo", "Copper Sky (feat Dax Orlo)",
                                   "Copper Sky [ft Dax Orlo]"])
def test_a_guest_named_with_feat_ft_or_featuring_is_dropped(title: str) -> None:
    assert key(title) == "copper sky|mira solenne"


def test_a_band_named_with_feat_on_a_compilation_keeps_its_name() -> None:
    assert artist_part("Various Artists", "A Feat of Clay") == "a feat of clay"
    assert artist_part("Various Artists", "Juno Reyes feat. Dax Orlo") == "juno reyes"


def test_a_dash_without_spaces_is_part_of_the_title() -> None:
    assert key("Half-Light") != key("Half")
    assert split_title("Half-Light").core == "half light"


def test_a_title_starting_with_a_dash_part_is_not_emptied() -> None:
    assert split_title(" - Copper Sky").core == "copper sky"


# --- Side versions -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("title", [
    "Copper Sky (Instrumental)", "Copper Sky (Karaoke Version)", "Copper Sky (A Cappella)", "Copper Sky (Acapella)",
    "Copper Sky (Live)", "Copper Sky - Live at the Pier", "Copper Sky (Remix)", "Copper Sky (Club Mix)",
    "Copper Sky (Demo)", "Copper Sky [Acoustic]", "Copper Sky (Radio Edit)", "Copper Sky – Radio Edit",
    "Copper Sky (Clean)", "Copper Sky (Take 2)", "Copper Sky (Sped Up)", "Copper Sky (Slowed)",
    "Copper Sky (Remixes)", "Copper Sky (Remixed)",
])
def test_a_side_version_is_named_in_what_the_title_adds(title: str) -> None:
    assert is_side_version(title)


@pytest.mark.parametrize("title", [
    "Copper Sky", "Live Forever", "Mix Tape", "Take Me Home", "Edit Your Life", "Clean Slate",
    "Copper Sky (Remastered)", "Copper Sky (Deluxe)", "Copper Sky (Single Version)", "Copper Sky (Mono)",
    "Copper Sky (feat. Dax Orlo)", "Demolition",
])
def test_a_word_of_the_title_itself_makes_no_side_version(title: str) -> None:
    assert not is_side_version(title)


@pytest.mark.parametrize("album", ["Copper Sky (Instrumentals)", "Karaoke Hits Vol. 3", "Sing Along (Karaoke Edition)",
                                   "Instrumental Versions"])
def test_a_karaoke_or_instrumental_album_holds_side_versions(album: str) -> None:
    assert is_side_version("Copper Sky", album)


def test_a_live_album_alone_does_not_make_a_side_version() -> None:
    # Only karaoke and instrumental albums: a live album says so in its titles, or it is an album like any other.
    assert not is_side_version("Copper Sky", "Live Forever")


# --- The version shown, the original year ---------------------------------------------------------------------------


def shown(versions: list[Version]) -> int:
    found = songs(versions)
    chosen = [version.id for version in versions if found[version.id].shown]
    assert len(chosen) == 1
    return chosen[0]


def test_karaoke_never_wins_on_listeners() -> None:
    versions = [
        Version(1, "Northern Lights", "Paper Lanterns", "The Velvet Harbour", year=2001, rating_count=859_024),
        Version(2, "Northern Lights", "Karaoke Hits", "The Velvet Harbour", year=2020, rating_count=859_100),
    ]
    assert shown(versions) == 1


def test_the_earliest_year_wins_over_more_listeners() -> None:
    versions = [
        Version(1, "Northern Lights", "Greatest Hits", "The Velvet Harbour", year=2019, rating_count=60_000),
        Version(2, "Northern Lights", "Paper Lanterns", "The Velvet Harbour", year=2001, rating_count=52_000),
        Version(3, "Northern Lights - Remastered 2014", "Paper Lanterns (Remastered)", "The Velvet Harbour",
                year=2014, rating_count=52_010),
    ]
    assert shown(versions) == 2
    assert {song.original_year for song in songs(versions).values()} == {2001}


def test_a_live_version_from_an_earlier_year_is_still_not_shown() -> None:
    versions = [
        Version(1, "Copper Sky (Live)", "Early Nights", "Mira Solenne", year=1999, rating_count=99_000),
        Version(2, "Copper Sky", "Copper Sky", "Mira Solenne", year=2016, rating_count=18_000),
    ]
    assert shown(versions) == 2
    # The original year comes from all versions, the live one included.
    assert songs(versions)[2].original_year == 1999


def test_when_only_side_versions_exist_the_earliest_of_them_is_shown() -> None:
    versions = [
        Version(1, "Copper Sky (Live)", "Live B", "Mira Solenne", year=2012, rating_count=50),
        Version(2, "Copper Sky (Acoustic)", "Live A", "Mira Solenne", year=2008, rating_count=10),
    ]
    assert shown(versions) == 2


def test_listeners_then_plays_then_the_lower_id_decide_a_tie_of_years() -> None:
    same_year = [
        Version(5, "Paper Planes", "Lamps", "Juno Reyes", year=1998, rating_count=4_500, view_count=1),
        Version(4, "Paper Planes", "Lamps (Deluxe Edition)", "Juno Reyes", year=1998, rating_count=4_500, view_count=9),
    ]
    assert shown(same_year) == 4
    more_listeners = [
        Version(5, "Paper Planes", "Lamps", "Juno Reyes", year=1998, rating_count=4_600),
        Version(4, "Paper Planes", "Lamps (Deluxe Edition)", "Juno Reyes", year=1998, rating_count=4_500, view_count=9),
    ]
    assert shown(more_listeners) == 5
    tie = [Version(9, "Paper Planes", "A", "Juno Reyes", year=1998), Version(8, "Paper Planes", "B", "Juno Reyes", year=1998)]
    assert shown(tie) == 8


def test_a_version_without_a_year_comes_after_those_with_one() -> None:
    versions = [Version(1, "Waves", "Unknown", "Dax Orlo", year=None, rating_count=10_000),
                Version(2, "Waves", "Tides", "Dax Orlo", year=2003, rating_count=10)]
    assert shown(versions) == 2


def test_every_track_gets_a_song_and_each_song_one_version_shown() -> None:
    versions = [
        Version(1, "Intro", "A", "Dax Orlo", year=2001), Version(2, "Intro", "B", "Dax Orlo", year=2002),
        Version(3, "Waves", "A", "Dax Orlo", year=2001), Version(4, "Waves (Live)", "B", "Dax Orlo", year=2002),
        Version(5, "Waves", "C", "Mira Solenne", year=2003),
    ]
    found = songs(versions)
    assert set(found) == {1, 2, 3, 4, 5}
    assert sum(song.shown for song in found.values()) == 4
    assert found[4].side and not found[3].side
    assert found[3].key == found[4].key != found[5].key


def test_a_song_without_any_year_has_none() -> None:
    assert songs([Version(1, "Waves", "A", "Dax Orlo")])[1].original_year is None
