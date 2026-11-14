"""A made-up catalogue written straight into the catalogue tables, for the search and the playlists: names with accents
and an ß, songs with many versions (best-of, live, karaoke), a track's own artist, moods on all three levels, genres,
original years that cross a decade, the same title by two artists. Every name is invented.

``install()`` writes it as the live generation (with the songs worked out by ``catalog.compute_songs`` and the search
index made), as a finished run would leave it."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, insert

from app import clock
from app.db import SessionLocal
from app.models import (
    AlbumTag,
    ArtistTag,
    CatalogAlbum,
    CatalogArtist,
    CatalogTag,
    CatalogTrack,
    PlexConnection,
    TrackTag,
)
from app.services import catalog, catalog_view

GENERATION = 1

#: (id, name, moods, styles)
ARTISTS: list[tuple[int, str, list[str], list[str]]] = [
    (101, "The Velvet Harbour", ["Brooding", "Atmospheric"], ["Dream Pop"]),
    (102, "Zoë Brandt", ["Romantic", "Warm"], ["Chanson"]),
    (103, "Café Ölberg", ["Cheerful"], ["Indie Pop"]),
    (104, "Dax Orlo", ["Energetic"], ["Synth Pop"]),
    (105, "Various Artists", [], []),
    (106, "Mira Solenne", ["Mellow"], ["Ambient"]),
]

#: (id, artist, title, year, genres, moods, styles, added)
ALBUMS: list[tuple[int, int, str, int | None, list[str], list[str], list[str], int]] = [
    (201, 101, "Paper Lanterns", 1984, ["Rock"], ["Melancholy"], ["Dream Pop"], 1_000),
    (202, 101, "Greatest Hits", 2005, ["Rock", "Pop"], [], [], 2_000),
    (203, 101, "Harbour Lights Live", 1991, ["Rock"], ["Energetic"], [], 3_000),
    (204, 101, "Sing Along (Karaoke Edition)", 2010, ["Pop"], [], [], 4_000),
    (205, 102, "Straße der Träume", 1979, ["Chanson"], ["Romantic", "Bittersweet"], ["Chanson"], 5_000),
    (206, 102, "Mädchen im Regen", 1996, ["Chanson", "Pop"], ["Wistful"], [], 6_000),
    (207, 103, "Señorita Lluvia", 2018, ["Pop"], ["Summery"], ["Indie Pop"], 7_000),
    (208, 104, "Northern Lights", 2012, ["Electronic"], ["Energetic"], ["Synth Pop"], 8_000),
    (209, 105, "Summer Mix 2022", 2022, ["Pop"], [], [], 9_000),
    (210, 106, "Quiet Rooms", 2001, ["Ambient"], ["Calm/Peaceful", "Reflective"], ["Ambient"], 10_000),
    # Added last, but older: its "Glow" is the version shown, though another one has the lower id.
    (211, 104, "Early Tapes", 1999, ["Electronic"], [], [], 11_000),
]

#: (id, album, title, own artist, seconds, listeners, plays, moods, disc, number)
TRACKS: list[tuple[int, int, str, str, int, int, int, list[str], int, int]] = [
    (301, 201, "Northern Lights", "", 245, 52_000, 3, ["Energetic"], 1, 1),
    (302, 201, "Glasshouse", "", 201, 31_000, 0, ["Mellow"], 1, 2),
    (303, 201, "Night Drive", "", 312, 9_000, 0, [], 1, 3),
    (304, 201, "Lanterns", "", 188, 4_000, 0, [], 1, 4),
    (305, 202, "Northern Lights", "", 246, 52_400, 40, ["Energetic"], 1, 1),
    (306, 202, "Glasshouse", "", 201, 31_000, 0, [], 1, 2),
    (307, 202, "Drive", "", 170, 2_000, 0, [], 1, 3),
    (308, 203, "Northern Lights (Live)", "", 280, 52_300, 0, [], 1, 1),
    (309, 203, "Night Drive – Live", "", 330, 9_100, 0, [], 1, 2),
    (310, 204, "Northern Lights", "", 246, 52_450, 0, [], 1, 1),
    (311, 205, "Straße der Träume", "", 214, 18_000, 5, ["Nostalgic"], 1, 1),
    (312, 205, "Ärger im Café", "", 176, 6_000, 0, ["Playful"], 1, 2),
    (313, 205, "Abendlied", "Zoë Brandt & Dax Orlo", 233, 7_500, 0, ["Tender"], 1, 3),
    (314, 206, "Mädchen im Regen", "", 199, 12_000, 1, ["Wistful"], 1, 1),
    (315, 206, "Straße der Träume", "", 216, 11_000, 0, [], 1, 2),
    (316, 206, "Ölberg", "", 150, 300, 0, [], 1, 3),
    (317, 207, "Señorita Lluvia", "", 205, 25_000, 0, ["Summery", "Fun"], 1, 1),
    (318, 207, "Lluvia de Verano", "", 190, 3_000, 0, [], 1, 2),
    (319, 207, "Café con Leche", "", 222, 8_000, 0, ["Cheerful"], 1, 3),
    (320, 208, "Northern Lights", "", 260, 2_000, 0, [], 1, 1),
    (321, 208, "Night Drive", "", 301, 600, 0, [], 1, 2),
    (322, 208, "Glow", "", 199, 1_500, 0, ["Energetic"], 1, 3),
    (323, 209, "Señorita Lluvia", "Café Ölberg", 205, 25_100, 0, [], 1, 1),
    (324, 209, "Paper Planes", "Juno Reyes", 180, 4_400, 0, ["Summery"], 1, 2),
    (325, 209, "A", "Dax Orlo", 60, 100, 0, [], 1, 3),
    (326, 210, "Room One", "", 400, 900, 0, ["Calm/Peaceful"], 1, 1),
    (327, 210, "Room Two", "", 380, 850, 0, [], 1, 2),
    (328, 210, "Glasshouse", "", 260, 700, 0, ["Mellow"], 1, 3),
    (329, 210, "Intro", "", 40, 100, 0, [], 1, 4),
    (330, 202, "Intro", "", 35, 100, 0, [], 1, 4),
    (331, 211, "Glow", "", 199, 1_400, 0, [], 1, 1),
]


def install() -> None:
    now = clock.now()
    tags: dict[tuple[str, str], int] = {}

    def tag(kind: str, name: str) -> int:
        return tags.setdefault((kind, name), len(tags) + 1)

    links: dict[type[Any], set[tuple[int, int]]] = {ArtistTag: set(), AlbumTag: set(), TrackTag: set()}
    for artist_id, _name, moods, styles in ARTISTS:
        for name in moods:
            links[ArtistTag].add((artist_id, tag("mood", name)))
        for name in styles:
            links[ArtistTag].add((artist_id, tag("style", name)))
    for album_id, _artist, _title, _year, genres, moods, styles, _added in ALBUMS:
        for kind, names in (("genre", genres), ("mood", moods), ("style", styles)):
            for name in names:
                links[AlbumTag].add((album_id, tag(kind, name)))
    for track in TRACKS:
        for name in track[7]:
            links[TrackTag].add((track[0], tag("mood", name)))
    album_artist = {album[0]: album[1] for album in ALBUMS}
    album_year = {album[0]: album[3] for album in ALBUMS}
    with SessionLocal() as db:
        db.execute(delete(PlexConnection))
        db.add(PlexConnection(id=1, serial=1, via="manual", server_name="Studio", machine_id="m", owned=True,
                              url="http://127.0.0.1:32400", token_enc="-", section_key="7", section_title="Music",
                              connected_at=now, generation=GENERATION, synced_at=now, full_at=now, marks={},
                              analysis_sampled=0, analysis_found=0))
        db.execute(insert(CatalogArtist), [
            {"generation": GENERATION, "id": artist_id, "title": name, "thumb": "", "added_at": 0, "updated_at": 0}
            for artist_id, name, _moods, _styles in ARTISTS])
        db.execute(insert(CatalogAlbum), [
            {"generation": GENERATION, "id": album_id, "artist_id": artist, "title": title, "year": year,
             "thumb": f"/library/metadata/{album_id}/thumb/1" if album_id != 209 else "", "added_at": added,
             "updated_at": added}
            for album_id, artist, title, year, _genres, _moods, _styles, added in ALBUMS])
        db.execute(insert(CatalogTrack), [
            {"generation": GENERATION, "id": track_id, "album_id": album, "artist_id": album_artist[album],
             "title": title, "artist": own, "duration": seconds * 1000, "rating_count": listeners,
             "view_count": plays, "year": album_year[album], "disc": disc, "number": number, "added_at": 0,
             "updated_at": 0, "song_key": "", "variant": False, "shown": True, "original_year": None}
            for track_id, album, title, own, seconds, listeners, plays, _moods, disc, number in TRACKS])
        db.execute(insert(CatalogTag), [{"generation": GENERATION, "id": number, "kind": kind, "name": name}
                                        for (kind, name), number in tags.items()])
        for model, pairs in links.items():
            if pairs:
                db.execute(insert(model), [{"generation": GENERATION, "item_id": item, "tag_id": tag_id}
                                           for item, tag_id in sorted(pairs)])
        catalog.compute_songs(db, GENERATION)
        db.commit()
        catalog_view.reindex(db)
    catalog_view.forget()
