"""The connector to a Plex Media Server: its catalogue, its playlists, its sound and its pictures, at the level of
single requests (``services/plex.py`` is the HTTP client beneath it). Everything that is Plex's own way stays here.

**Playlists**, as Plex Media Server 1.43 answered:

* ``GET /playlists?playlistType=audio`` lists them with ``ratingKey``, ``guid``, ``title``, ``smart``, ``leafCount``
  and ``updatedAt`` (whole seconds on Plex's clock, set 0.1 to 0.7 seconds after a change; ``leafCount`` of a smart
  playlist is stale, its ``totalSize`` is not). Entries added or taken out change ``updatedAt`` there, a new name
  changes ``title`` only, entries moved change nothing in the list.
* ``GET /playlists/<key>/items`` gives the entries in order, each a track with its ``playlistItemID``; at once, right
  after a change. A smart playlist's entries have no ``playlistItemID``.
* ``POST /playlists?type=audio&smart=0&title=..&uri=server://<machine>/com.plexapp.plugins.library`` makes an empty one
  (without ``uri`` Plex says 400). ``PUT /playlists/<key>/items?uri=../library/metadata/<k1,k2,..>`` adds at the end,
  in that order, and answers ``leafCountAdded`` and ``leafCountRequested``: a track already in it is left out without
  a word. Taken out and put in again, an entry gets a new ``playlistItemID``.
* ``DELETE /playlists/<key>/items/<item>``; ``PUT /playlists/<key>/items/<item>/move`` without ``after`` to the start,
  with ``after=<item>`` right after it. ``after=0`` is not the start, and an ``after`` that is gone puts the entry after
  the first one, answering 200: a place is only ever an item of the state just read. An item that is gone: 404.
* ``PUT /playlists/<key>?title=..`` answers with an empty body; ``DELETE /playlists/<key>`` with 204.
* ``GET /playlists/<key>`` sets the playlist's ``updatedAt`` to now (a smart one's too): never asked (``meta``).

**The catalogue**: the lists of artists, albums and tracks in pages of 5,000 (the genres come with the lists), moods
and styles of albums and artists by asking for many at once (``/library/metadata/<k1,k2,...>``), the moods of the
tracks through the library's mood filter (one list per mood), a sample of tracks for the sonic analysis. A partial run
asks per type what Plex changed or added since the last run began (``updatedAt>>=``/``addedAt>>=``, 60 seconds back
for what Plex writes a little late), always on Plex's clock: the ``Date`` of its answers, never ours. A time Plex gives
an entry is never trusted beyond that clock. When more than 5,000 entries are new since then (Plex sometimes touches
tens of thousands at once), a full run instead; what was already there in the minute looked back does not count, and
a new entry counts once. What is gone shows in the totals; when they differ, the light list of keys says which.

**Pictures** go only with fixed parameters (``plex.PlexServer.cover``): ``format=webp`` made a real Plex restart.

Every key and id that goes into an address is a number checked here; the machine id of the server only letters,
digits and dashes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any, Self

from ..media import NUMBER_LIMIT
from ..plex import (
    ALBUM,
    ARTIST,
    BULK,
    SOUND_START,
    SOUND_STOP,
    THUMB,
    TRACK,
    PlexError,
    PlexServer,
    sound_headers,
    sound_params,
    tags_of,
)
from .base import Capabilities, Entry, Listed

if TYPE_CHECKING:
    from ..catalog import Writer

CAPABILITIES = Capabilities(moods=True, sound=True, smart_playlists=True, duplicates=False, changes="both")

#: Tracks per request when adding: the address stays under 3,000 characters and Plex answers within 0.15 s.
CHUNK = 300
MACHINE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
LIBRARY = "com.plexapp.plugins.library"
#: The entries of a smart playlist nexmix shows at most.
SMART_LIMIT = 5000
#: How far back a partial run looks beyond where the last run began.
OVERLAP = 60
#: More changes than this in a partial run: a full run instead.
PARTIAL_LIMIT = 5000
#: The key of the marks that holds Plex's clock when the last run began.
CLOCK = "clock"
SAMPLE = 200
TYPES = (ARTIST, ALBUM, TRACK)
#: The kind of thing behind each of Plex's types, as nexmix numbers them.
KINDS = {ARTIST: "artist", ALBUM: "album", TRACK: "track"}
_PLEX_TYPE = {ARTIST: "artist", ALBUM: "album", TRACK: "track"}


def _int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number < NUMBER_LIMIT else None


def _key(value: Any) -> int:
    number = _int(value)
    if number is None:
        raise PlexError("bad_path", 400)
    return number


def _machine(machine: str) -> str:
    if not MACHINE.fullmatch(machine or ""):
        raise PlexError("plex_error")
    return machine


def uri(machine: str, tracks: Sequence[str] = ()) -> str:
    """The address of tracks of the server, as Plex wants it for a playlist (none: the library itself)."""
    base = f"server://{_machine(machine)}/{LIBRARY}"
    if not tracks:
        return base
    return f"{base}/library/metadata/{','.join(str(_key(track)) for track in tracks)}"


def _container(answer: Any) -> dict[str, Any]:
    try:
        payload = answer.json()
    except ValueError:
        return {}
    container = payload.get("MediaContainer") if isinstance(payload, dict) else None
    return container if isinstance(container, dict) else {}


def _listed(entry: dict[str, Any]) -> Listed | None:
    key = _int(entry.get("ratingKey"))
    if key is None or str(entry.get("playlistType") or "audio") != "audio":
        return None
    return Listed(
        key=str(key), guid=str(entry.get("guid") or "")[:200], title=str(entry.get("title") or "").strip()[:200],
        smart=bool(entry.get("smart")) and str(entry.get("smart")).lower() not in ("0", "false"),
        leaf_count=_int(entry.get("leafCount")) or 0, updated_at=_int(entry.get("updatedAt")) or 0,
    )


def _entry(entry: dict[str, Any], smart: bool) -> Entry | None:
    track = _int(entry.get("ratingKey"))
    item = _int(entry.get("playlistItemID"))
    if track is None or (item is None and not smart) or str(entry.get("type") or "track") != "track":
        return None
    own = str(entry.get("originalTitle") or "").strip()
    album = _int(entry.get("parentRatingKey"))
    return Entry(
        item=item, track=str(track), title=str(entry.get("title") or "").strip()[:500],
        artist=(own or str(entry.get("grandparentTitle") or "").strip())[:500],
        album=str(entry.get("parentTitle") or "").strip()[:500], album_key=str(album) if album else "",
        year=_int(entry.get("parentYear")), duration=max(0, _int(entry.get("duration")) or 0),
        guid=str(entry.get("guid") or "")[:200],
    )


class PlexSource:
    """One Plex server, opened for a while (``with``): what nexmix asks of it, in Plex's keys as text."""

    kind = "plex"
    capabilities = CAPABILITIES

    def __init__(self, url: str, token: str, client_id: str, machine: str, section: str, **options: Any) -> None:
        self.server = PlexServer(url, token, client_id, **options)
        self.machine = machine
        self.section = section

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.server.close()

    @property
    def clock(self) -> int | None:
        """Plex's clock at its last answer (seconds)."""
        return self.server.clock

    @property
    def began(self) -> int | None:
        """Plex's clock at its first answer since it was opened."""
        return self.server.began

    # --- Playlists ---------------------------------------------------------------------------------------------

    def listing(self) -> list[Listed]:
        container = self.server.get("/playlists", {"playlistType": "audio"}, timeout=30.0)
        found = []
        for entry in container.get("Metadata") or []:
            if isinstance(entry, dict) and str(entry.get("type") or "playlist") == "playlist":
                one = _listed(entry)
                if one is not None:
                    found.append(one)
        return found

    def meta(self, key: str) -> Listed:
        """One playlist, taken from the list of them; ``PlexError("plex_not_found")`` when Plex has it no longer.

        Never ``GET /playlists/<key>``: asking for the playlist itself sets its ``updatedAt`` anew (measured on Plex
        1.43, smart and ordinary ones alike), and a sync that asked so would find it changed at every look. The list
        and the entries leave it as it is. A playlist missing from the list is gone when its entries answer 404 too."""
        number = _key(key)
        for one in self.listing():
            if one.key == str(number):
                return one
        container = self.server.get(f"/playlists/{number}/items", start=0, size=0, timeout=30.0)
        return Listed(key=str(number), guid="", title=str(container.get("title") or "").strip()[:200],
                      smart=bool(container.get("smart")) and str(container.get("smart")).lower() not in ("0", "false"),
                      leaf_count=_int(container.get("leafCount")) or 0, updated_at=0)

    def items(self, key: str) -> list[Entry]:
        """Every entry in order, in one answer (pages could each see another state of a list being changed)."""
        container = self.server.get(f"/playlists/{_key(key)}/items", timeout=120.0)
        found: list[Entry] = []
        seen: set[str] = set()
        for entry in container.get("Metadata") or []:
            if isinstance(entry, dict):
                one = _entry(entry, smart=False)
                if one is not None and one.track not in seen:
                    seen.add(one.track)
                    found.append(one)
        return found

    def smart_items(self, key: str, limit: int | None = None) -> tuple[list[Entry], int]:
        """The first ``limit`` entries of a smart playlist (``SMART_LIMIT``) and how many it has in all
        (``totalSize``)."""
        limit = SMART_LIMIT if limit is None else limit
        path = f"/playlists/{_key(key)}/items"
        total = _int(self.server.get(path, start=0, size=0, timeout=60.0).get("totalSize")) or 0
        container = self.server.get(path, start=0, size=limit, timeout=120.0) if total else {}
        found: list[Entry] = []
        seen: set[str] = set()
        for entry in container.get("Metadata") or []:
            if isinstance(entry, dict):
                one = _entry(entry, smart=True)
                if one is not None and one.track not in seen:
                    seen.add(one.track)
                    found.append(one)
        return found[:limit], max(total, len(found))

    def create(self, title: str) -> Listed:
        """An empty playlist named ``title``."""
        answer = self.server._send("POST", "/playlists", {"type": "audio", "title": title[:200], "smart": 0,
                                                           "uri": uri(self.machine)}, timeout=30.0)
        for entry in _container(answer).get("Metadata") or []:
            if isinstance(entry, dict):
                one = _listed(entry)
                if one is not None:
                    return one
        raise PlexError("plex_error")

    def add(self, key: str, tracks: Sequence[str], answered: Callable[[list[str], int | None], None] | None = None,
            ) -> int:
        """The tracks at the end, in their order, ``CHUNK`` to a request; how many Plex took (one already in it is
        left out without a word). ``answered``: called after every part Plex answered, with the part and how many of
        it Plex says it took."""
        added = 0
        for start in range(0, len(tracks), CHUNK):
            part = [str(track) for track in tracks[start:start + CHUNK]]
            answer = self.server._send("PUT", f"/playlists/{_key(key)}/items", {"uri": uri(self.machine, part)},
                                       timeout=60.0)
            took = _int(_container(answer).get("leafCountAdded"))
            added += took if took is not None else 0
            if answered is not None:
                answered(part, took)
        return added

    def remove(self, key: str, item: int) -> bool:
        """False: the entry was gone already."""
        try:
            self.server._send("DELETE", f"/playlists/{_key(key)}/items/{_key(item)}", timeout=30.0)
        except PlexError as exc:
            if exc.code == "plex_not_found":
                return False
            raise
        return True

    def move(self, key: str, item: int, after: int | None) -> bool:
        """The entry right after ``after`` (an item of the state just read), None: to the start (never ``after=0``).
        False: the entry was gone."""
        params = {} if after is None else {"after": _key(after)}
        try:
            self.server._send("PUT", f"/playlists/{_key(key)}/items/{_key(item)}/move", params, timeout=30.0)
        except PlexError as exc:
            if exc.code == "plex_not_found":
                return False
            raise
        return True

    def rename(self, key: str, title: str) -> None:
        self.server._send("PUT", f"/playlists/{_key(key)}", {"title": title[:200]}, timeout=30.0)

    def delete(self, key: str) -> bool:
        """False: Plex had it no longer."""
        try:
            self.server._send("DELETE", f"/playlists/{_key(key)}", timeout=30.0)
        except PlexError as exc:
            if exc.code == "plex_not_found":
                return False
            raise
        return True

    def existing(self, tracks: Iterable[str]) -> set[str]:
        """Which of the tracks the library of Plex still has (asked a hundred at a time)."""
        found: set[str] = set()
        chosen = sorted({_key(track) for track in tracks})
        for start in range(0, len(chosen), 100):
            try:
                entries = self.server.bulk(chosen[start:start + 100])
            except PlexError as exc:
                if exc.code != "plex_not_found":
                    raise
                entries = []
            for entry in entries:
                key = _int(entry.get("ratingKey"))
                if key is not None and str(entry.get("type") or "track") == "track":
                    found.add(str(key))
        return found

    # --- Sound and pictures ------------------------------------------------------------------------------------

    def similar(self, track: str, limit: int, max_distance: float) -> list[tuple[str, float]]:
        """The tracks that sound closest to ``track`` by Plex's sonic analysis, with their distance."""
        return [(str(key), distance) for key, distance in self.server.nearest(_key(track), limit, max_distance)]

    @staticmethod
    def sound_start(url: str, token: str, client_id: str, track: str, offset: int, session: str,
                    bitrate: int) -> tuple[str, dict[str, str], dict[str, Any]]:
        """The request for a preview: Plex's converter (``universal/start.mp3``) from ``offset`` seconds of the track,
        as MP3 at ``bitrate``; the address, its headers (the token among them) and its parameters. Only a track key,
        never a path from outside."""
        return (f"{url}{SOUND_START}", sound_headers(token, client_id),
                sound_params(_key(track), offset, session, bitrate))

    @staticmethod
    def sound_stop(url: str, token: str, client_id: str, session: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        """The request that ends a preview's session at the converter."""
        return f"{url}{SOUND_STOP}", sound_headers(token, client_id), {"session": session}

    def cover(self, thumb: str, size: int) -> tuple[bytes, str]:
        if not THUMB.fullmatch(thumb or ""):
            raise PlexError("plex_not_found", 404)
        return self.server.cover(thumb, size)

    # --- The catalogue -----------------------------------------------------------------------------------------

    def explain(self, exc: PlexError) -> PlexError:
        """Plex knew a path of the library no longer: when the library itself is gone from the server, say that."""
        if exc.code != "plex_not_found":
            return exc
        try:
            sections = self.server.music_sections()
        except PlexError:
            return exc
        if all(one.key != self.section for one in sections):
            return PlexError("library_gone", 409)
        return exc

    def full(self, writer: Writer) -> dict[str, Any]:
        """The whole catalogue into the writer's generation; returns the marks for the next partial run and the sample
        of the sonic analysis."""
        marks: dict[str, int] = {}
        try:
            writer.step("artists")
            artists = self._list(writer, ARTIST, marks)
            writer.step("albums")
            albums = self._list(writer, ALBUM, marks)
            writer.step("tracks")
            tracks = self._list(writer, TRACK, marks)
            writer.step("album_tags")
            self._bulk_tags(writer, ALBUM, sorted(albums), ("mood", "style", "genre"))
            writer.step("artist_tags")
            self._bulk_tags(writer, ARTIST, sorted(artists), ("mood", "style", "genre"))
            writer.step("track_moods")
            self._track_moods(writer, tracks)
            writer.step("analysis")
            analysis = self.measure_analysis(list(tracks)) if tracks else {}
        except PlexError as exc:
            raise self.explain(exc) from exc
        marks = _cap(marks, self.server.clock)
        # Whatever Plex had written when the run began was there to be fetched: the next partial run looks from here.
        if self.server.began is not None:
            marks[CLOCK] = self.server.began
        return {"marks": marks, "analysis": analysis,
                "counts": {"artists": len(artists), "albums": len(albums), "tracks": len(tracks)}}

    def _list(self, writer: Writer, of_type: int, marks: dict[str, int]) -> dict[int, int]:
        """One type, page by page, into the generation; returns the numbers of the entries by Plex's key."""
        found: dict[int, int] = {}
        taken = 0
        kind = KINDS[of_type]
        for entries, total in self.server.pages(self.section, of_type):
            chosen = [entry for entry in entries if entry.get("type") in (None, _PLEX_TYPE[of_type])]
            writer.numbers.ensure(kind, (_int(entry.get("ratingKey")) for entry in chosen))
            if of_type != ARTIST:
                parent = "artist" if of_type == ALBUM else "album"
                writer.numbers.ensure(parent, (_int(entry.get("parentRatingKey")) for entry in chosen))
            if of_type == TRACK:
                writer.numbers.ensure("artist", (_int(entry.get("grandparentRatingKey")) for entry in chosen))
            rows = []
            for entry in chosen:
                key = _int(entry.get("ratingKey"))
                if key is None or key in found:
                    continue
                row = self.row(writer, of_type, entry)
                rows.append(row)
                found[key] = row["id"]
                marks[str(of_type)] = max(marks.get(str(of_type), 0), _moment(entry))
                if of_type in (ALBUM, TRACK):
                    for name in tags_of(entry, "Genre"):
                        writer.link(of_type, row["id"], "genre", name)
            writer.rows(of_type, rows)
            taken += len(entries)
            writer.share(taken / total if total else 1.0)
            writer.check()
        writer.commit()
        return found

    def row(self, writer: Writer, of_type: int, entry: dict[str, Any]) -> dict[str, Any]:
        """A catalogue row of an entry of Plex, by nexmix's numbers."""
        numbers = writer.numbers
        own = numbers.of(KINDS[of_type], _int(entry.get("ratingKey")))
        if of_type == ARTIST:
            return {"id": own, "title": _text(entry.get("title")), "thumb": _thumb(entry.get("thumb")),
                    "added_at": _int(entry.get("addedAt")) or 0, "updated_at": _int(entry.get("updatedAt")) or 0}
        if of_type == ALBUM:
            return {"id": own, "artist_id": numbers.of("artist", _int(entry.get("parentRatingKey"))) or 0,
                    "title": _text(entry.get("title")), "year": _int(entry.get("year")) or None,
                    "thumb": _thumb(entry.get("thumb")), "added_at": _int(entry.get("addedAt")) or 0,
                    "updated_at": _int(entry.get("updatedAt")) or 0}
        album_artist = _text(entry.get("grandparentTitle"))
        performer = _text(entry.get("originalTitle"))
        return {
            "id": own, "album_id": numbers.of("album", _int(entry.get("parentRatingKey"))) or 0,
            "artist_id": numbers.of("artist", _int(entry.get("grandparentRatingKey"))) or 0,
            "title": _text(entry.get("title")),
            # Kept only where it says something the album's artist does not.
            "artist": performer if performer and performer != album_artist else "",
            "duration": max(0, _int(entry.get("duration")) or 0),
            "rating_count": max(0, _plain_int(entry.get("ratingCount")) or 0),
            "view_count": max(0, _plain_int(entry.get("viewCount")) or 0),
            "year": _int(entry.get("parentYear")) or None,
            "disc": _plain_int(entry.get("parentIndex")), "number": _plain_int(entry.get("index")),
            "added_at": _int(entry.get("addedAt")) or 0, "updated_at": _int(entry.get("updatedAt")) or 0,
            "song_key": "", "variant": False, "shown": True, "original_year": None,
            "guid": _text(entry.get("guid"))[:200],
        }

    def _bulk_tags(self, writer: Writer, of_type: int, keys: list[int], kinds: tuple[str, ...]) -> None:
        kind = KINDS[of_type]
        for start in range(0, len(keys), BULK):
            for entry in self.server.bulk(keys[start:start + BULK]):
                number = writer.numbers.of(kind, _int(entry.get("ratingKey")))
                if number is not None:
                    for tag in kinds:
                        for name in tags_of(entry, tag.capitalize()):
                            writer.link(of_type, number, tag, name)
            writer.share((start + BULK) / len(keys) if keys else 1.0)
            writer.check()

    def _track_moods(self, writer: Writer, tracks: dict[int, int]) -> None:
        values = self.server.tag_values(self.section, "mood", TRACK)
        for number, (key, name) in enumerate(values, 1):
            for item in self.server.keys(self.section, TRACK, {"mood": key}):
                if item in tracks:
                    writer.link(TRACK, tracks[item], "mood", name)
            writer.share(number / len(values))
            writer.check()

    def measure_analysis(self, track_keys: list[int]) -> dict[str, Any]:
        """The share of tracks Plex analysed, from an evenly spread sample: there is no filter for it."""
        from ... import clock

        ordered = sorted(track_keys)
        step = max(1, len(ordered) // SAMPLE)
        sample = ordered[::step][:SAMPLE]
        found = 0
        for start in range(0, len(sample), BULK):
            found += sum(1 for entry in self.server.bulk(sample[start:start + BULK])
                         if entry.get("musicAnalysisVersion"))
        return {"analysis_enabled": self.server.music_analysis_enabled(self.section), "analysis_sampled": len(sample),
                "analysis_found": found, "analysis_at": clock.now()}

    # --- The partial run

    def _count(self, of_type: int, since: int) -> int:
        """How many entries of a type Plex changed or added after ``since``. A new entry carries both times: of the two
        counts the larger stands for both, so it counts once."""
        return max(self.server.count(self.section, of_type, {"updatedAt>>": since}),
                   self.server.count(self.section, of_type, {"addedAt>>": since}))

    def changed(self, marks: dict[str, int]) -> tuple[int, dict[int, dict[int, dict[str, Any]]]]:
        """How many entries Plex changed or added per type since the last run; the entries themselves (with the minute
        before) only when the count stays under the limit. The minute looked back never counts against the limit: a
        burst in it was taken in by the run before. The entries are by Plex's key."""
        tops: dict[int, int] = {}
        fresh = 0
        for of_type in TYPES:
            top = _top(marks, of_type)
            count = self._count(of_type, top)
            if self.server.clock is not None and top > self.server.clock:
                # The answer told Plex's clock, and the mark lies beyond it (Plex's clock went back, or a mark of an
                # older nexmix came from an entry dated ahead): from the clock, never from a time to come.
                top = self.server.clock
                count = self._count(of_type, top)
            tops[of_type] = top
            fresh += count
        found: dict[int, dict[int, dict[str, Any]]] = {of_type: {} for of_type in TYPES}
        if fresh > PARTIAL_LIMIT:
            return fresh, found
        since = {of_type: max(0, top - OVERLAP) for of_type, top in tops.items()}
        if sum(self._count(of_type, since[of_type]) for of_type in TYPES) > PARTIAL_LIMIT:
            # A burst in the minute looked back: the full run after it has it already, it is not fetched again.
            since = tops
        for of_type in TYPES:
            for one in ({"updatedAt>>": since[of_type]}, {"addedAt>>": since[of_type]}):
                for entries, _total in self.server.pages(self.section, of_type, one):
                    for entry in entries:
                        key = _int(entry.get("ratingKey"))
                        if key is not None and entry.get("type") in (None, _PLEX_TYPE[of_type]):
                            found[of_type][key] = entry
        return fresh, found

    def partial(self, writer: Writer, marks: dict[str, int], analysis_due: bool) -> dict[str, Any]:
        """What changed since the last run, ready to be written: the rows of the changed entries with their tags, the
        numbers gone from Plex per type, the marks for the next run and, when due, the sample of the analysis.
        ``TooMany`` (from ``catalog``) when a full run is needed instead."""
        from ..catalog import TooMany

        try:
            writer.step("changes")
            fresh, changed = self.changed(marks)
            if fresh > PARTIAL_LIMIT:
                raise TooMany
            # The tags of what changed, from the request that brings many at once.
            for of_type in TYPES:
                keys = list(changed[of_type])
                for start in range(0, len(keys), BULK):
                    for entry in self.server.bulk(keys[start:start + BULK]):
                        key = _int(entry.get("ratingKey"))
                        if key is not None and key in changed[of_type]:
                            changed[of_type][key] = {**changed[of_type][key], **_tag_part(entry)}
            writer.step("removed")
            gone: dict[int, set[int]] = {}
            missing: dict[int, set[int]] = {}
            for of_type in TYPES:
                kind = KINDS[of_type]
                ours = {int(key) for key in (writer.numbers.key(kind, number) for number in writer.ours(of_type))
                        if key is not None and key.isdigit()} | set(changed[of_type])
                if self.server.count(self.section, of_type) == len(ours):
                    continue
                theirs = self.server.keys(self.section, of_type)
                gone[of_type] = ours - theirs
                missing[of_type] = theirs - ours
            for of_type, keys_missing in missing.items():
                chosen = sorted(keys_missing)
                for start in range(0, len(chosen), BULK):
                    for entry in self.server.bulk(chosen[start:start + BULK]):
                        key = _int(entry.get("ratingKey"))
                        if key is not None:
                            changed[of_type][key] = entry
            analysis: dict[str, Any] = {}
            if analysis_due:
                writer.step("analysis")
                every = {int(key) for key in (writer.numbers.key("track", number) for number in writer.ours(TRACK))
                         if key is not None and key.isdigit()} | set(changed[TRACK])
                analysis = self.measure_analysis(sorted(every)) if every else {}
        except PlexError as exc:
            raise self.explain(exc) from exc
        for of_type in TYPES:
            kind = KINDS[of_type]
            writer.numbers.ensure(kind, changed[of_type])
            if of_type != ARTIST:
                writer.numbers.ensure("artist" if of_type == ALBUM else "album",
                                      (_int(entry.get("parentRatingKey")) for entry in changed[of_type].values()))
            if of_type == TRACK:
                writer.numbers.ensure("artist", (_int(entry.get("grandparentRatingKey"))
                                                 for entry in changed[TRACK].values()))
        rows: dict[int, dict[int, dict[str, Any]]] = {}
        tags: dict[int, dict[int, dict[str, list[str]]]] = {}
        for of_type in TYPES:
            rows[of_type], tags[of_type] = {}, {}
            for entry in changed[of_type].values():
                row = self.row(writer, of_type, entry)
                rows[of_type][row["id"]] = row
                tags[of_type][row["id"]] = {kind: tags_of(entry, kind.capitalize())
                                            for kind in ("mood", "style", "genre")}
                marks[str(of_type)] = max(marks.get(str(of_type), 0), _moment(entry))
        removed = {of_type: {number for number in (writer.numbers.of(KINDS[of_type], key) for key in keys)
                             if number is not None}
                   for of_type, keys in gone.items()}
        marks = _cap(marks, self.server.clock)
        if self.server.began is not None:
            marks[CLOCK] = self.server.began
        else:
            marks.pop(CLOCK, None)
        return {"rows": rows, "tags": tags, "gone": removed, "marks": marks, "analysis": analysis}


# --- Values out of Plex's entries ------------------------------------------------------------------------------------


def _plain_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _thumb(value: Any) -> str:
    text = str(value or "")
    return text if THUMB.match(text) else ""


def _text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _moment(entry: dict[str, Any]) -> int:
    """The last time Plex touched the entry; ``addedAt`` where it has no ``updatedAt``."""
    return max(_plain_int(entry.get("updatedAt")) or 0, _plain_int(entry.get("addedAt")) or 0)


def _cap(marks: dict[str, int], now: int | None) -> dict[str, int]:
    """Plex's times are never trusted beyond Plex's own clock: an entry dated ten years ahead must not move a mark."""
    if now is None:
        return marks
    return {key: min(int(value), now) for key, value in marks.items()}


def _top(marks: dict[str, int], of_type: int) -> int:
    """Where a partial run looks from: Plex's clock when the last run began (whatever Plex wrote up to a minute before
    was there to be fetched then), else the highest time seen of the type."""
    return int(marks.get(CLOCK) or 0) or int(marks.get(str(of_type)) or 0)


def _tag_part(entry: dict[str, Any]) -> dict[str, Any]:
    return {name: entry[name] for name in ("Mood", "Style", "Genre") if name in entry}
