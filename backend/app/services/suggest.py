"""Suggestions and filling by mood, the same rules as in the browser (``frontend/src/lib/suggest.ts``), word for word.

The browser holds the compact catalogue and works these out itself; past ``NEXMIX_SERVER_SEARCH_FROM`` tracks the
server does it for the browser (``GET /api/v1/suggestions``, ``/suggestions/mood``), and programs always do. Both ways
must suggest the same: the tests hold them to a shared list of cases (``tests/fixtures/suggest``).

**Suggestions** (``suggest``) start from a few tracks, the seed (the last five of the playlist, or one track for
"similar to this one"). Their profile is the share of each effective mood (track 1, album 0.7, artist 0.4), of each
style and genre of their albums and of each decade of their original years. Every song of the catalogue, in the
version shown for it, scores by how much it has of that profile::

    mood · 0.55 + style · 1.6 + genre · 0.6 + decade · 0.9 (the next decades count 0.4)
    + together · 1.2 (how often it stands in the same playlists as the seed, against the most often)
    + sound · 2.4 (how close it sounds by Plex's sonic analysis)

and below 0.6 it is out. Then the kind: "fitting" adds a little of its popularity, "known" much of it, "discoveries"
favour what was never played and hold back the well known. A song of a seed's album loses 0.8, of a seed's artist 0.25.
At most two songs of one artist (one for discoveries), never a song of the seed or of the playlist, never one the
person asked not to see.
Each comes with its reason: the two strongest shared moods, a shared style, the decade, and whether the two voices
spoke for it.

**Filling by mood** (``build``) takes the songs with enough of the chosen moods (more than 0.4), from the chosen
decades, between 1:30 and 10 minutes long. The slider between discoveries and hits weighs popularity against what was
played, and a little chance mixes them (``mulberry32``, the same numbers in both languages for the same seed). At most
two songs per artist, each song once, until the length is reached; the same artist never twice in a row where
another one can stand between.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .search import Index, track_view

LIMIT = 14
MODES = ("fit", "known", "new")
#: The weight of each part of the profile and of the two voices from the server.
MOOD, STYLE, GENRE, DECADE, TOGETHER, SOUND = 0.55, 1.6, 0.6, 0.9, 1.2, 2.4
NEIGHBOUR_DECADE = 0.4
FLOOR = 0.6
#: A distance of Plex's sonic analysis at which a track no longer counts as close.
SOUND_RANGE = 0.6
SAME_ALBUM, SAME_ARTIST = 0.8, 0.25
SHORTEST, BUILD_SHORTEST, BUILD_LONGEST = 60, 90, 600
UINT = 0xFFFFFFFF


@dataclass
class Profile:
    mood: dict[int, float] = field(default_factory=dict)
    style: dict[int, float] = field(default_factory=dict)
    genre: dict[int, float] = field(default_factory=dict)
    decade: dict[int, float] = field(default_factory=dict)
    artists: set[int] = field(default_factory=set)
    albums: set[int] = field(default_factory=set)


def profile(ix: Index, seeds: Iterable[int]) -> Profile:
    """The seed's share of each mood, style, genre and decade (``seeds``: places of tracks in the index)."""
    chosen = list(seeds)
    found = Profile()
    for position in chosen:
        for mood, weight in ix.mood_weights[position].items():
            found.mood[mood] = found.mood.get(mood, 0.0) + weight
        album = ix.album[position]
        if album >= 0:
            for style in ix.album_styles[album]:
                found.style[style] = found.style.get(style, 0.0) + 1
            for genre in sorted(ix.album_genres[album]):
                found.genre[genre] = found.genre.get(genre, 0.0) + 1
            found.albums.add(album)
            if ix.album_artist[album] >= 0:
                found.artists.add(ix.album_artist[album])
        decade = ix.decade[position]
        if decade:
            found.decade[decade] = found.decade.get(decade, 0.0) + 1
    size = max(1, len(chosen))
    for part in (found.mood, found.style, found.genre, found.decade):
        for key in part:
            part[key] = part[key] / size
    return found


def artist_of(ix: Index, position: int) -> int:
    album = ix.album[position]
    return ix.album_artist[album] if album >= 0 else -1


def reason(ix: Index, position: int, seed: Profile, together: int = 0, sound: bool = False) -> dict[str, Any]:
    """Why a song is suggested: the two strongest moods it shares with the seed, a style it shares, its decade."""
    shared = [mood for mood in ix.mood_weights[position] if mood in seed.mood]
    shared.sort(key=lambda mood: (-seed.mood[mood], mood))
    album = ix.album[position]
    style = next((one for one in ix.album_styles[album] if one in seed.style), None) if album >= 0 else None
    return {
        "moods": [ix.moods[mood] for mood in shared[:2]],
        "style": ix.styles[style] if style is not None and style < len(ix.styles) else None,
        "decade": ix.decade[position] or None,
        "together": together,
        "sound": sound,
    }


def suggest(ix: Index, seeds: Iterable[int], *, mode: str = "fit", exclude: Iterable[int] = (),
            hidden: Iterable[int] = (), together: Mapping[int, int] | None = None,
            sound: Mapping[int, float] | None = None, limit: int = LIMIT) -> list[dict[str, Any]]:
    """The songs to suggest for ``seeds`` (places of tracks), best first, each as a track with its ``reason``.
    ``exclude`` and ``hidden`` are songs (the id of the version shown); ``together`` the number of playlists a song
    shares with the seed, ``sound`` its distance by the sonic analysis."""
    chosen = list(seeds)
    if not chosen:
        return []
    seed = profile(ix, chosen)
    out = set(exclude) | set(hidden) | {ix.song[position] for position in chosen}
    together = together or {}
    sound = sound or {}
    most = max(together.values(), default=0)
    scored: list[tuple[float, int]] = []
    for position in ix.popular:
        song = ix.song[position]
        if song in out or ix.duration[position] < SHORTEST:
            continue
        mood = 0.0
        for one, weight in ix.mood_weights[position].items():
            share = seed.mood.get(one)
            if share:
                mood += share * weight
        album = ix.album[position]
        style = 0.0
        genre = 0.0
        if album >= 0:
            for one in ix.album_styles[album]:
                style += seed.style.get(one, 0.0)
            for one in sorted(ix.album_genres[album]):
                genre += seed.genre.get(one, 0.0)
        decade = ix.decade[position]
        near = 0.0
        if decade:
            near = seed.decade.get(decade, 0.0) + NEIGHBOUR_DECADE * (
                seed.decade.get(decade - 10, 0.0) + seed.decade.get(decade + 10, 0.0))
        score = mood * MOOD + style * STYLE + genre * GENRE + near * DECADE
        if most and together.get(song):
            score += together[song] / most * TOGETHER
        if song in sound:
            score += max(0.0, 1 - sound[song] / SOUND_RANGE) * SOUND
        if score < FLOOR:
            continue
        pop = ix.pop_score[position]
        if mode == "known":
            score += pop * 0.55
        elif mode == "new":
            score += (0.5 if ix.song_plays.get(song, 0) == 0 else -0.4) - pop * 0.12
        else:
            score += pop * 0.18
        if album in seed.albums:
            score -= SAME_ALBUM
        if artist_of(ix, position) in seed.artists:
            score -= SAME_ARTIST
        scored.append((score, position))
    scored.sort(key=lambda pair: (-pair[0], ix.ids[pair[1]]))
    cap = 1 if mode == "new" else 2
    per_artist: dict[int, int] = {}
    found: list[dict[str, Any]] = []
    for _score, position in scored:
        artist = artist_of(ix, position)
        if artist >= 0:
            if per_artist.get(artist, 0) >= cap:
                continue
            per_artist[artist] = per_artist.get(artist, 0) + 1
        song = ix.song[position]
        found.append({**track_view(ix, position),
                      "reason": reason(ix, position, seed, together.get(song, 0), song in sound)})
        if len(found) >= limit:
            break
    return found


def mulberry32(seed: int) -> Callable[[], float]:
    """Numbers in [0, 1) from a seed, the same as ``mulberry32`` in the browser."""
    state = seed & UINT

    def next_number() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & UINT
        value = state
        value = ((value ^ (value >> 15)) * (value | 1)) & UINT
        value = (value ^ ((value + (((value ^ (value >> 7)) * (value | 61)) & UINT)) & UINT)) & UINT
        return ((value ^ (value >> 14)) & UINT) / 4294967296

    return next_number


def build(ix: Index, moods: Iterable[str], *, decades: Iterable[int] = (), minutes: int = 60, hits: int = 55,
          exclude: Iterable[int] = (), hidden: Iterable[int] = (), seed: int = 1,
          per_artist: int = 2) -> list[dict[str, Any]]:
    """Songs of the chosen moods (names), for about ``minutes``; ``hits`` from 0 (discoveries) to 100 (hits)."""
    places = {name: place for place, name in enumerate(ix.moods)}
    wanted = {places[name] for name in moods if name in places}
    if not wanted:
        return []
    chosen_decades = {int(decade) for decade in decades}
    out = set(exclude) | set(hidden)
    chance = mulberry32(seed)
    scored: list[tuple[float, int]] = []
    for position in range(ix.size):
        song = ix.song[position]
        if song != ix.ids[position] or song in out:
            continue
        if ix.duration[position] < BUILD_SHORTEST or ix.duration[position] > BUILD_LONGEST:
            continue
        if chosen_decades and ix.decade[position] not in chosen_decades:
            continue
        mood = 0.0
        for one, weight in ix.mood_weights[position].items():
            if one in wanted:
                mood += weight
        if mood <= 0.4:
            continue
        pop = ix.pop_score[position] / 6
        played = 0.3 if ix.song_plays.get(song, 0) > 0 else 0.0
        score = mood + pop * (hits / 50) * 1.2 - (1 - hits / 100) * played + chance() * 0.35
        scored.append((score, position))
    scored.sort(key=lambda pair: (-pair[0], ix.ids[pair[1]]))
    counts: dict[int, int] = {}
    picked: list[int] = []
    total = 0
    for _score, position in scored:
        artist = artist_of(ix, position)
        if artist >= 0 and counts.get(artist, 0) >= per_artist:
            continue
        if artist >= 0:
            counts[artist] = counts.get(artist, 0) + 1
        picked.append(position)
        total += ix.duration[position]
        if total >= minutes * 60:
            break
    # The same artist not twice in a row: the next one of another artist moves up.
    for index in range(1, len(picked)):
        before = artist_of(ix, picked[index - 1])
        if before >= 0 and artist_of(ix, picked[index]) == before:
            later = next((other for other in range(index + 1, len(picked))
                          if artist_of(ix, picked[other]) != before), None)
            if later is not None:
                picked[index], picked[later] = picked[later], picked[index]
    return [track_view(ix, position) for position in picked]


def seed_positions(ix: Index, tracks: Iterable[int]) -> list[int]:
    """The places of the tracks the catalogue knows, in the order given."""
    return [ix.position[track] for track in tracks if track in ix.position]
