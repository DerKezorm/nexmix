"""A made-up pair of music libraries for ``match.py``: one library, and the same music as a second server would hold it,
with the differences two servers really show, at fixed shares, and the truth known for every track. Names are made of
syllables, nothing is real.

What the second library does differently: some tracks are missing, some albums carry another name (with their tracks
at the same places), some titles carry an edition note ("- Remastered 2011", "(Deluxe Edition)") or their guest in
another spelling, some lengths lie up to two seconds apart, some artists lose or gain a leading "The", and each library
has versions of its own (a live album) that are no match for anything."""

from __future__ import annotations

import random
from dataclasses import dataclass

from app.services import songs
from app.services.match import Candidate, Wanted

SYLLABLES = ["ka", "lo", "mi", "ren", "sa", "tu", "vo", "nel", "ri", "ma", "do", "fen", "ga", "lu", "ost", "ber",
             "wen", "ti", "ar", "quo", "zel", "pa", "un", "ix"]


def _word(rng: random.Random, low: int = 2, high: int = 3) -> str:
    return "".join(rng.choice(SYLLABLES) for _ in range(rng.randint(low, high))).capitalize()


def _name(rng: random.Random, words: int) -> str:
    return " ".join(_word(rng) for _ in range(words))


@dataclass
class Pair:
    source: list[Wanted]
    target: list[Candidate]
    #: For each source track (by its place in ``source``), the target's id of the same recording (None: not there).
    truth: list[int | None]


def made_up_pair(tracks: int = 50_000, seed: int = 11) -> Pair:
    rng = random.Random(seed)
    source: list[Wanted] = []
    target: list[Candidate] = []
    truth: list[int | None] = []
    next_id = 1
    artists = [_name(rng, rng.randint(1, 2)) for _ in range(max(10, tracks // 35))]
    made = 0
    while made < tracks:
        artist = rng.choice(artists)
        album = _name(rng, rng.randint(1, 3))
        count = min(rng.randint(6, 16), tracks - made)
        renamed = rng.random() < 0.012
        target_album = _name(rng, 2) if renamed else (album + " (Deluxe Edition)" if rng.random() < 0.02 else album)
        target_artist = ("The " + artist) if rng.random() < 0.03 else artist
        year = rng.randint(1960, 2025)
        rows = []
        for number in range(1, count + 1):
            title = _name(rng, rng.randint(1, 4))
            duration = rng.randint(120_000, 400_000)
            mbid = f"mb-{made + number:06d}" if rng.random() < 0.25 else ""
            rows.append((number, title, duration, mbid))
        shape = tuple((1, number, duration) for number, _title, duration, _mbid in rows)
        for number, title, duration, mbid in rows:
            source.append(Wanted(title=title, artist=artist, album=album, duration=duration, mbid=mbid,
                                 album_artist=artist, number=number, disc=1, shape=shape))
            if rng.random() < 0.028:
                truth.append(None)
                continue
            there = title
            roll = rng.random()
            if roll < 0.03:
                there = f"{title} - Remastered {year + 20}"
            elif roll < 0.05:
                there = title.upper()
            elif roll < 0.07:
                there = f"{title} (feat. {_word(rng)})"
            elif renamed and roll < 0.5:
                there = f"{title} ({_word(rng)} Mix)"
            jitter = rng.randint(-2000, 2000) if rng.random() < 0.1 else 0
            ident = next_id
            next_id += 1
            target.append(Candidate(id=ident, title=there, album=target_album, album_artist=target_artist,
                                    duration=duration + jitter,
                                    song_key=songs.song_key(ident, there, target_artist),
                                    mbid=mbid if mbid and rng.random() < 0.9 else "", number=number, disc=1))
            truth.append(ident)
        made += count
        # A live album of the same artist on the target only, its titles those of the album: never the answer.
        if rng.random() < 0.05:
            live = f"{album} Live"
            for number, title, duration, _mbid in rows[:4]:
                ident = next_id
                next_id += 1
                target.append(Candidate(id=ident, title=f"{title} (Live)", album=live, album_artist=target_artist,
                                        duration=duration + 25_000,
                                        song_key=songs.song_key(ident, f"{title} (Live)", target_artist),
                                        shown=False, number=number, disc=1))
    rng.shuffle(target)
    return Pair(source=source, target=target, truth=truth)
