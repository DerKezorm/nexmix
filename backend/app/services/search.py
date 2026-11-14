"""The search of the catalogue, the same rules as in the browser (``frontend/src/lib/catalogSearch.ts``), word for word.

The browser searches the compact catalogue (``catalog_view``) itself; past ``SERVER_FROM`` tracks the server searches
for it (``GET /api/v1/catalog/search``), and programs always do. Both ways must find the same in the same order: the
tests hold them to a shared list of queries (``tests/fixtures/search``).

**Words** in any order, each a part of the title, the track's artist, the album's artist or the album, folded the
same way (``fold``: lower case, accents gone, ß as ss, everything but letters and digits a single space).

**Filters**: a mood (one, or a group of them, ``g:<group>``) on the track's effective moods (its own count 1, the
album's 0.7, the artist's 0.4: the strongest level that names the mood); a decade of the original year (the earliest
album year of the song, else the album's); a genre of the album.

**Rank**: the song's popularity (``log10(1 + listeners)``, the most of any version), twice the mood's weight, 8 when the
title is the query, else 4 when it begins with it, 2.5 when the artist begins with the first word. Ties: the lower id.

**One song once**: of the versions found, the one shown for the song when it is among them (``songs.py``), else the best
found. The song ranks with the best score among its versions. ``versions``: how many the library holds.
"""

from __future__ import annotations

import heapq
import json
import math
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Groups of moods for the filter, in the order the interface lists them.
MOOD_GROUPS: list[tuple[str, list[str]]] = [
    (str(key), [str(name) for name in names])
    for key, names in json.loads((Path(__file__).with_name("mood_groups.json")).read_text(encoding="utf-8"))
]
GROUP_KEYS = frozenset(key for key, _names in MOOD_GROUPS)

TRACK_LIMIT = 80
ALBUM_LIMIT = 60
ALBUMS_RECENT = 40
ARTIST_LIMIT = 80
ARTIST_TOP = 15

#: The weight of a mood by where it is named.
TRACK_WEIGHT, ALBUM_WEIGHT, ARTIST_WEIGHT = 1.0, 0.7, 0.4


def fold(text: Any) -> str:
    """Lower case, ß as ss, accents gone (NFKD without marks), anything but letters and digits a single space.

    The browser's ``fold`` does the same with ``\\p{M}``, ``\\p{L}`` and ``\\p{N}``."""
    lowered = unicodedata.normalize("NFKD", str(text or "").lower().replace("ß", "ss"))
    out = []
    for char in lowered:
        kind = unicodedata.category(char)[0]
        if kind == "M":
            continue
        out.append(char if kind in "LN" else " ")
    return " ".join("".join(out).split())


def words(query: Any) -> list[str]:
    return [word for word in fold(query).split(" ") if word]


@dataclass
class Index:
    """The catalogue prepared for searching: one entry per track (lists by position), per album and per artist."""

    moods: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    # Tracks.
    ids: list[int] = field(default_factory=list)
    title: list[str] = field(default_factory=list)
    artist: list[str] = field(default_factory=list)
    album: list[int] = field(default_factory=list)
    duration: list[int] = field(default_factory=list)
    rating: list[int] = field(default_factory=list)
    disc: list[int] = field(default_factory=list)
    number: list[int] = field(default_factory=list)
    song: list[int] = field(default_factory=list)
    original: list[int] = field(default_factory=list)
    hay: list[str] = field(default_factory=list)
    title_fold: list[str] = field(default_factory=list)
    shown_fold: list[str] = field(default_factory=list)
    mood_weights: list[dict[int, float]] = field(default_factory=list)
    song_pop: dict[int, int] = field(default_factory=dict)
    song_versions: dict[int, int] = field(default_factory=dict)
    #: Per song: how often its versions were played, together (the suggestions' "discoveries").
    song_plays: dict[int, int] = field(default_factory=dict)
    position: dict[int, int] = field(default_factory=dict)
    # Albums.
    album_ids: list[int] = field(default_factory=list)
    album_title: list[str] = field(default_factory=list)
    album_artist: list[int] = field(default_factory=list)
    album_year: list[int] = field(default_factory=list)
    album_cover: list[bool] = field(default_factory=list)
    album_genres: list[frozenset[int]] = field(default_factory=list)
    album_styles: list[list[int]] = field(default_factory=list)
    album_added: list[int] = field(default_factory=list)
    album_fold: list[str] = field(default_factory=list)
    album_title_fold: list[str] = field(default_factory=list)
    album_tracks: list[list[int]] = field(default_factory=list)
    album_position: dict[int, int] = field(default_factory=dict)
    # Artists.
    artist_ids: list[int] = field(default_factory=list)
    artist_name: list[str] = field(default_factory=list)
    artist_moods: list[list[int]] = field(default_factory=list)
    artist_fold: list[str] = field(default_factory=list)
    artist_albums: list[list[int]] = field(default_factory=list)
    artist_tracks: list[list[int]] = field(default_factory=list)
    artist_position: dict[int, int] = field(default_factory=dict)
    # For the server's own shortcuts: the tracks per mood, per decade, per genre, and the songs by popularity.
    by_mood: dict[int, list[int]] = field(default_factory=dict)
    by_decade: dict[int, list[int]] = field(default_factory=dict)
    by_genre: dict[int, list[int]] = field(default_factory=dict)
    popular: list[int] = field(default_factory=list)
    #: Per track: the song's popularity as it counts in the rank, the decade of its original year.
    pop_score: list[float] = field(default_factory=list)
    decade: list[int] = field(default_factory=list)
    #: Per mood: (track, weight) for every track that has it.
    mood_pairs: dict[int, list[tuple[int, float]]] = field(default_factory=dict)
    #: Per song: its tracks.
    members: dict[int, list[int]] = field(default_factory=dict)
    #: The strongest weight per track for a mood filter, kept per filter (the index never changes).
    mood_cache: dict[str, dict[int, float]] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.ids)


def prepare(catalog: dict[str, Any]) -> Index:
    """The compact catalogue (``catalog_view.compact``) prepared for searching, as the browser prepares it."""
    ix = Index(moods=list(catalog["moods"]), genres=list(catalog["genres"]), styles=list(catalog.get("styles", [])))
    for position, (artist_id, name, moods, _styles) in enumerate(catalog["artists"]):
        ix.artist_ids.append(artist_id)
        ix.artist_name.append(name)
        ix.artist_moods.append(list(moods))
        ix.artist_fold.append(fold(name))
        ix.artist_albums.append([])
        ix.artist_tracks.append([])
        ix.artist_position[artist_id] = position
    album_moods: list[list[int]] = []
    for position, (album_id, title, artist, year, cover, genres, styles, moods, added) in enumerate(catalog["albums"]):
        ix.album_ids.append(album_id)
        ix.album_title.append(title)
        ix.album_artist.append(artist)
        ix.album_year.append(year)
        ix.album_cover.append(bool(cover))
        ix.album_genres.append(frozenset(genres))
        ix.album_styles.append(list(styles))
        ix.album_added.append(added)
        name = ix.artist_name[artist] if artist >= 0 else ""
        ix.album_fold.append(fold(f"{title} {name}"))
        ix.album_title_fold.append(fold(title))
        ix.album_tracks.append([])
        ix.album_position[album_id] = position
        album_moods.append(list(moods))
        if artist >= 0:
            ix.artist_albums[artist].append(position)
    for position, row in enumerate(catalog["tracks"]):
        track_id, title, album, own, duration, rating, views, moods, disc, number, song, original = row
        artist = ix.album_artist[album] if album >= 0 else -1
        album_artist = ix.artist_name[artist] if artist >= 0 else ""
        shown = own or album_artist
        weights: dict[int, float] = {}
        for mood in ix.artist_moods[artist] if artist >= 0 else ():
            weights[mood] = ARTIST_WEIGHT
        for mood in album_moods[album] if album >= 0 else ():
            weights[mood] = ALBUM_WEIGHT
        for mood in moods:
            weights[mood] = TRACK_WEIGHT
        album_title = ix.album_title[album] if album >= 0 else ""
        ix.ids.append(track_id)
        ix.title.append(title)
        ix.artist.append(shown)
        ix.album.append(album)
        ix.duration.append(duration)
        ix.rating.append(rating)
        ix.disc.append(disc)
        ix.number.append(number)
        ix.song.append(song)
        year = original or (ix.album_year[album] if album >= 0 else 0)
        ix.original.append(year)
        ix.hay.append(fold(" ".join(part for part in (title, own, album_artist, album_title) if part)))
        ix.title_fold.append(fold(title))
        ix.shown_fold.append(fold(shown))
        ix.mood_weights.append(weights)
        ix.song_pop[song] = max(ix.song_pop.get(song, 0), rating)
        ix.song_versions[song] = ix.song_versions.get(song, 0) + 1
        ix.song_plays[song] = ix.song_plays.get(song, 0) + views
        ix.members.setdefault(song, []).append(position)
        ix.position[track_id] = position
        if album >= 0:
            ix.album_tracks[album].append(position)
        if artist >= 0:
            ix.artist_tracks[artist].append(position)
        for mood, weight in weights.items():
            ix.by_mood.setdefault(mood, []).append(position)
            ix.mood_pairs.setdefault(mood, []).append((position, weight))
        ix.by_decade.setdefault(year // 10 * 10, []).append(position)
        ix.decade.append(year // 10 * 10)
        for genre in ix.album_genres[album] if album >= 0 else ():
            ix.by_genre.setdefault(genre, []).append(position)
    ix.pop_score = [math.log10(1 + ix.song_pop[song]) for song in ix.song]
    shown_first = sorted(
        (position for position in range(ix.size) if ix.song[position] == ix.ids[position]),
        key=lambda position: (-math.log10(1 + ix.song_pop[ix.song[position]]), ix.ids[position]),
    )
    ix.popular = shown_first
    return ix


# --- Filters ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    mood: str = ""
    decade: int = 0
    genre: str = ""

    @property
    def any(self) -> bool:
        return bool(self.mood or self.decade or self.genre)


def mood_set(ix: Index, mood: str) -> set[int] | None:
    """The moods a filter value stands for (by their place in ``ix.moods``); None without a filter. An unknown name or
    group stands for no mood at all: nothing is found."""
    if not mood:
        return None
    names: Iterable[str]
    if mood.startswith("g:"):
        names = next((group for key, group in MOOD_GROUPS if key == mood[2:]), [])
    else:
        names = [mood]
    places = {name: position for position, name in enumerate(ix.moods)}
    return {places[name] for name in names if name in places}


def genre_of(ix: Index, genre: str) -> int | None:
    """The place of a genre; -1 for one the catalogue does not know (nothing is found), None without a filter."""
    if not genre:
        return None
    try:
        return ix.genres.index(genre)
    except ValueError:
        return -1


# --- Tracks ----------------------------------------------------------------------------------------------------------


def track_view(ix: Index, position: int) -> dict[str, Any]:
    album = ix.album[position]
    year = ix.album_year[album] if album >= 0 else 0
    return {
        "id": ix.ids[position],
        "title": ix.title[position],
        "artist": ix.artist[position],
        "album": ix.album_title[album] if album >= 0 else "",
        "album_id": ix.album_ids[album] if album >= 0 else None,
        "cover": bool(ix.album_cover[album]) if album >= 0 else False,
        "year": year or None,
        "duration": ix.duration[position],
        "song": ix.song[position],
        "versions": ix.song_versions[ix.song[position]],
    }


def _mood_weights(ix: Index, mood: str, moods: set[int]) -> dict[int, float]:
    """The strongest weight of a wanted mood per track that has one (kept: the same filter costs once)."""
    found = ix.mood_cache.get(mood)
    if found is None:
        found = {}
        for one in moods:
            for position, weight in ix.mood_pairs.get(one, ()):
                if weight > found.get(position, 0.0):
                    found[position] = weight
        if len(ix.mood_cache) > 400:
            ix.mood_cache.clear()
        ix.mood_cache[mood] = found
    return found


def search_tracks(ix: Index, query: str, filters: Filters | None = None, limit: int = TRACK_LIMIT,
                  candidates: Iterable[int] | None = None) -> dict[str, Any]:
    """``{"total": songs found, "tracks": [...]}``. ``candidates``: the positions worth looking at (the server narrows
    them by the search index; every track left out must fail the words); None: all. Their order does not matter.

    First the best score per song over every track found (one number per song: that is all the loop keeps); then,
    only for the songs that can reach the first ``limit``, the version to show (the one shown for the song when found,
    else the best found, ties by the lower id), which also breaks ties between songs."""
    filters = filters or Filters()
    toks = words(query)
    moods = mood_set(ix, filters.mood)
    genre = genre_of(ix, filters.genre)
    decade = filters.decade
    if not toks and not filters.any and candidates is None:
        return _popular(ix, limit)
    joined = " ".join(toks)
    mood_of = _mood_weights(ix, filters.mood, moods) if moods is not None else None
    pool: Iterable[int]
    if candidates is not None:
        pool = candidates if mood_of is None else [p for p in candidates if p in mood_of]
    elif mood_of is not None:
        pool = mood_of.keys()
    elif decade:
        pool = ix.by_decade.get(decade, ())
    elif genre is not None:
        pool = ix.by_genre.get(genre, ())
    else:
        pool = range(ix.size)
    song_of, pop, decades, album_of, hay = ix.song, ix.pop_score, ix.decade, ix.album, ix.hay
    genres, title_fold, shown_fold = ix.album_genres, ix.title_fold, ix.shown_fold
    first = toks[0] if toks else ""

    def score_of(position: int) -> float | None:
        """The score of a track, None when it is not found: the same steps as the loop below."""
        if decade and decades[position] != decade:
            return None
        if genre is not None:
            album = album_of[position]
            if album < 0 or genre not in genres[album]:
                return None
        if mood_of is not None and position not in mood_of:
            return None
        text = hay[position]
        if any(tok not in text for tok in toks):
            return None
        score = pop[position] + (mood_of[position] if mood_of is not None else 0.0) * 2
        if toks:
            title = title_fold[position]
            if title == joined:
                score += 8
            elif title.startswith(joined):
                score += 4
            if shown_fold[position].startswith(first):
                score += 2.5
        return score

    best: dict[int, float] = {}
    for position in pool:
        if decade and decades[position] != decade:
            continue
        if genre is not None:
            album = album_of[position]
            if album < 0 or genre not in genres[album]:
                continue
        if toks:
            text = hay[position]
            if any(tok not in text for tok in toks):
                continue
        score = pop[position] + (mood_of[position] if mood_of is not None else 0.0) * 2
        if toks:
            title = title_fold[position]
            if title == joined:
                score += 8
            elif title.startswith(joined):
                score += 4
            if shown_fold[position].startswith(first):
                score += 2.5
        song = song_of[position]
        if score > best.get(song, -1.0):
            best[song] = score
    if not best:
        return {"total": 0, "tracks": []}
    threshold = heapq.nlargest(limit, best.values())[-1]
    shown_of: dict[int, int] = {}
    for song, score in best.items():
        if score < threshold:
            continue
        chosen: int | None = None
        chosen_score = -1.0
        for position in ix.members[song]:
            found = score_of(position)
            if found is None:
                continue
            if ix.ids[position] == song:
                chosen, chosen_score = position, float("inf")
                break
            if found > chosen_score or (found == chosen_score and chosen is not None
                                        and ix.ids[position] < ix.ids[chosen]):
                chosen, chosen_score = position, found
        assert chosen is not None
        shown_of[song] = chosen
    ranked = sorted(shown_of, key=lambda song: (-best[song], ix.ids[shown_of[song]]))[:limit]
    return {"total": len(best), "tracks": [track_view(ix, shown_of[song]) for song in ranked]}


def _popular(ix: Index, limit: int) -> dict[str, Any]:
    """No words, no filter: the songs by popularity (what the general way finds, without looking at every track)."""
    return {"total": len(ix.popular), "tracks": [track_view(ix, position) for position in ix.popular[:limit]]}


# --- Albums and artists ----------------------------------------------------------------------------------------------


def album_view(ix: Index, position: int) -> dict[str, Any]:
    artist = ix.album_artist[position]
    return {
        "id": ix.album_ids[position],
        "title": ix.album_title[position],
        "artist": ix.artist_name[artist] if artist >= 0 else "",
        "artist_id": ix.artist_ids[artist] if artist >= 0 else None,
        "year": ix.album_year[position] or None,
        "cover": ix.album_cover[position],
        "tracks": len(ix.album_tracks[position]),
    }


def search_albums(ix: Index, query: str) -> dict[str, Any]:
    toks = words(query)
    if not toks:
        recent = sorted(range(len(ix.album_ids)), key=lambda p: (-ix.album_added[p], ix.album_ids[p]))
        return {"total": len(ix.album_ids), "albums": [album_view(ix, p) for p in recent[:ALBUMS_RECENT]]}
    joined = " ".join(toks)
    found = [p for p in range(len(ix.album_ids)) if all(tok in ix.album_fold[p] for tok in toks)]
    found.sort(key=lambda p: (0 if ix.album_title_fold[p].startswith(joined) else 1, ix.album_title_fold[p],
                              ix.album_ids[p]))
    return {"total": len(found), "albums": [album_view(ix, p) for p in found[:ALBUM_LIMIT]]}


def _artist_cover(ix: Index, position: int) -> int | None:
    albums = sorted((p for p in ix.artist_albums[position] if ix.album_cover[p]),
                    key=lambda p: (-ix.album_year[p], ix.album_ids[p]))
    return ix.album_ids[albums[0]] if albums else None


def artist_view(ix: Index, position: int) -> dict[str, Any]:
    return {
        "id": ix.artist_ids[position],
        "name": ix.artist_name[position],
        "moods": [ix.moods[mood] for mood in ix.artist_moods[position][:4]],
        "cover": _artist_cover(ix, position),
        "albums": len(ix.artist_albums[position]),
    }


def search_artists(ix: Index, query: str) -> dict[str, Any]:
    toks = words(query)
    joined = " ".join(toks)
    found = [p for p in range(len(ix.artist_ids)) if all(tok in ix.artist_fold[p] for tok in toks)]
    found.sort(key=lambda p: (0 if toks and ix.artist_fold[p].startswith(joined) else 1, ix.artist_fold[p],
                              ix.artist_ids[p]))
    return {"total": len(found), "artists": [artist_view(ix, p) for p in found[:ARTIST_LIMIT]]}


def album_detail(ix: Index, album_id: int) -> dict[str, Any] | None:
    position = ix.album_position.get(album_id)
    if position is None:
        return None
    tracks = sorted(ix.album_tracks[position], key=lambda p: (ix.disc[p], ix.number[p], ix.ids[p]))
    return {"album": album_view(ix, position), "tracks": [track_view(ix, p) for p in tracks]}


def artist_detail(ix: Index, artist_id: int) -> dict[str, Any] | None:
    position = ix.artist_position.get(artist_id)
    if position is None:
        return None
    per_song: dict[int, int] = {}
    for p in ix.artist_tracks[position]:
        song = ix.song[p]
        held = per_song.get(song)
        if held is None or (ix.ids[held] != song and (ix.ids[p] == song or ix.ids[p] < ix.ids[held])):
            per_song[song] = p
    top = sorted(per_song.values(), key=lambda p: (-ix.song_pop[ix.song[p]], ix.ids[p]))[:ARTIST_TOP]
    albums = sorted(ix.artist_albums[position], key=lambda p: (-ix.album_year[p], ix.album_ids[p]))
    return {
        "artist": artist_view(ix, position),
        "tracks": [track_view(ix, p) for p in top],
        "albums": [album_view(ix, p) for p in albums],
    }
