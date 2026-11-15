"""A small music library, made up from first to last name: five artists, eleven albums, thirty-odd tracks, and in it
every case the song rules know. Ids follow a pattern for the reader: artists 1001 on, albums 2001 on, tracks 3001 on."""

from __future__ import annotations

from dataclasses import dataclass, field

ARTIST, ALBUM, TRACK = 8, 9, 10
#: Plex's clock at the time the library was "added".
BASE = 1_780_000_000


@dataclass
class Item:
    key: int
    kind: int
    title: str
    parent: int = 0
    year: int | None = None
    added: int = BASE
    updated: int | None = BASE
    genres: list[str] = field(default_factory=list)
    moods: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    #: A track's own artist (``originalTitle``).
    original: str = ""
    duration: int = 200_000
    rating: int = 0
    views: int = 0
    index: int = 1
    disc: int = 1
    analysed: bool = False
    #: Plex's guid; empty: one of its agents, made from the key (``plex://track/..``).
    guid: str = ""
    #: The MusicBrainz id of the track among the agents' ids (``mbid://``, only with ``includeGuids``); empty: none.
    mbid: str = ""


def made_up_library() -> dict[int, Item]:
    items: list[Item] = []

    def artist(key: int, title: str, moods: list[str] | None = None, styles: list[str] | None = None,
               genres: list[str] | None = None) -> None:
        items.append(Item(key, ARTIST, title, moods=moods or [], styles=styles or [], genres=genres or []))

    def album(key: int, parent: int, title: str, year: int, *, genres: list[str] | None = None,
              moods: list[str] | None = None, styles: list[str] | None = None) -> None:
        items.append(Item(key, ALBUM, title, parent, year, genres=genres or ["Pop"], moods=moods or [],
                          styles=styles or []))

    def track(key: int, parent: int, title: str, *, rating: int = 1000, views: int = 0, original: str = "",
              moods: list[str] | None = None, duration: int = 200_000, index: int = 1,
              updated: int | None = BASE) -> None:
        items.append(Item(key, TRACK, title, parent, rating=rating, views=views, original=original,
                          moods=moods or [], duration=duration, index=index, genres=["Pop"], updated=updated))

    # The Velvet Harbour: an album, its remaster, a best-of, a karaoke album and a live album.
    artist(1001, "The Velvet Harbour", moods=["Brooding", "Atmospheric"], styles=["Dream Pop"], genres=["Rock"])
    album(2001, 1001, "Paper Lanterns", 2001, moods=["Melancholy"], styles=["Dream Pop"])
    track(3001, 2001, "Northern Lights", rating=52_000, moods=["Energetic"], index=1)
    track(3002, 2001, "Glasshouse (Intro)", rating=8_000, duration=12_000, index=2)
    track(3003, 2001, "Glasshouse", rating=31_000, moods=["Mellow"], index=3)
    track(3004, 2001, "Night Drive (Part 1)", rating=9_000, index=4)
    track(3005, 2001, "Night Drive (Part 2)", rating=8_500, index=5)
    track(3006, 2001, "Intro", rating=4_000, duration=40_000, index=6, updated=None)
    album(2002, 1001, "Paper Lanterns (Remastered)", 2014)
    track(3007, 2002, "Northern Lights - Remastered 2014", rating=52_010, index=1)
    track(3008, 2002, "Glasshouse", rating=31_000, index=2)
    album(2003, 1001, "Greatest Hits", 2019, genres=["Pop", "Rock"])
    track(3009, 2003, "Northern Lights", rating=52_400, views=40, moods=["Energetic"], index=1)
    track(3010, 2003, "Glasshouse (Live at the Pier)", rating=31_500, index=2)
    track(3011, 2003, "Intro", rating=3_000, duration=35_000, index=3)
    album(2004, 1001, "Sing Along with The Velvet Harbour (Karaoke Edition)", 2020)
    # Nearly as many listeners as the original, a few more even: the number alone must not choose it.
    track(3012, 2004, "Northern Lights", rating=52_450, index=1)
    album(2011, 1001, "Harbour Lights Live", 2005)
    track(3031, 2011, "Northern Lights (Live)", rating=52_300, index=1)
    track(3032, 2011, "Glasshouse – Live", rating=31_200, index=2)

    # Mira Solenne: "feat." in brackets and without, an acoustic and a radio edit after a dash.
    artist(1002, "Mira Solenne", moods=["Romantic"], styles=["Synth Pop"])
    album(2005, 1002, "Copper Sky", 2016, moods=["Warm"])
    track(3013, 2005, "Copper Sky (feat. Dax Orlo)", rating=18_000, original="Mira Solenne feat. Dax Orlo",
          moods=["Warm", "Romantic"])
    track(3014, 2005, "Slow Tide feat. Dax Orlo", rating=9_000, original="Mira Solenne & Dax Orlo", index=2)
    track(3015, 2005, "Slow Tide (Acoustic)", rating=9_100, index=3)
    track(3016, 2005, "Fever Glass – Radio Edit", rating=7_000, index=4)
    track(3017, 2005, "Fever Glass", rating=6_900, index=5)
    track(3018, 2005, "Untitled", rating=1_000, index=6)
    album(2009, 1002, "Copper Sky (Instrumentals)", 2017)
    track(3019, 2009, "Copper Sky", rating=18_050, index=1)

    # Dax Orlo: the same title as a song of The Velvet Harbour, another song.
    artist(1003, "Dax Orlo", moods=["Cool"])
    album(2006, 1003, "Northern Lights", 2010)
    track(3020, 2006, "Northern Lights", rating=2_000)
    track(3021, 2006, "Op. 3 (No. 2)", rating=500, index=2)
    track(3022, 2006, "Night Drive (Part 1)", rating=600, index=3)

    # A compilation: the song belongs to the artist named on the track.
    artist(1004, "Various Artists")
    album(2007, 1004, "Summer Mix 2022", 2022)
    track(3023, 2007, "Copper Sky", rating=18_000, original="Mira Solenne")
    track(3024, 2007, "Paper Planes", rating=4_400, original="Juno Reyes & The Lamps", index=2)
    track(3025, 2007, "Paper Planes (Extended Mix)", rating=4_300, original="Juno Reyes", index=3)

    # Juno Reyes: the song of the compilation, first released here.
    artist(1005, "Juno Reyes", moods=["Cheerful"], styles=["Indie Pop"])
    album(2008, 1005, "Lamps", 1998)
    track(3026, 2008, "Paper Planes", rating=4_500, moods=["Cheerful", "Playful"])
    track(3027, 2008, "Paper Planes [Demo]", rating=900, index=2)
    album(2010, 1005, "Lamps (Deluxe Edition)", 1998)
    track(3028, 2010, "Paper Planes", rating=4_500, index=1)
    track(3029, 2010, "Paper Planes (Sped Up)", rating=4_800, index=2)
    track(3030, 2010, "Paper Planes (Reprise)", rating=700, index=3)
    # Added one after another, an hour apart, the last one an hour before ``BASE``.
    for number, item in enumerate(items):
        item.added = BASE - 3600 * (len(items) - number)
        item.updated = item.added if item.updated is not None else None
    return {item.key: item for item in items}
