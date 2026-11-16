"""The connector to a Music Assistant server: its library as the catalogue, its own playlists, a preview and the
covers (``services/music_assistant.py`` is the websocket beneath it).

**The catalogue** is Music Assistant's library, only the tracks of the chosen providers (``provider`` filter): the
albums with their year and picture, then the tracks in pages of 1,000, then the tracks of each of Music Assistant's
genres (its own list of about a hundred; a genre of a file outside that list has none). A track's name lacks what
Music Assistant took out into ``version`` ("Live", "2019 Remaster"): the title is put together again, so that the songs
(``songs.py``) see what the other servers show. Music Assistant has no moods, no styles, no count of listeners and no
analysis of the sound. MusicBrainz ids are not in the lists (only the recording's, track by track: ``recordings``).
Nothing says what changed: a partial run compares the totals and asks for a full run when they differ, and the event
``music_sync_completed`` (``ma_events``) asks for one at once. A full run of 50,000 tracks takes some seconds.

**Playlists** are Music Assistant's own (provider ``builtin``, ``is_editable``); dynamic and read-only ones are never
listed. They all belong to the operator in nexmix. As Music Assistant 2.10.9 answered (measured on a throwaway server):

* An entry has no id of its own and names the track by its provider (``<provider>://track/<path>``); it is found in the
  library by that mapping (``music/get_library_item``, kept for a while). An entry whose track is not in the library
  stays in the playlist untouched and is not shown.
* Adding (``library://track/<n>``) and taking out are tasks that end later; their result is asked until they end
  (``tasks/get``). A track that stands in the playlist already, or that Music Assistant does not know, is passed over
  without a word: only reading again says what was taken.
* **Taking out goes by place**, 1 for the first, worked out when the task runs: 0 takes the last entry, -1 the one
  before, a place after the end makes the whole task fail. So places come from a reading of the moment (with
  ``force_refresh``: without it a playlist reads as it was before an entry was taken out, for minutes), are checked
  (1 to the length), and the playlist is read again afterwards. Somebody may change it in Music Assistant in between:
  then whatever was taken that was not meant is put back (``_wrongly_taken``), and the sync merges again
  (``Raced``) instead of taking out by place once more.
* There is **no moving**: a new order takes out everything from the first entry out of place and puts it back in the
  new order (``arrange``). **Renaming** does not take (Music Assistant answers with the new name and keeps the old):
  the sync makes a new playlist under the new name, copies everything over (``copy``) and deletes the old one.
  **Deleting** is ``music/library/remove_item`` (``music/playlists/remove`` leaves the file behind, and the playlist
  comes back at the next scan).
* A name may stand twice (Music Assistant then names the file ``<name> (1)``): nexmix makes the names it gives unique
  (``unique_title``) and takes a name with such a number for its own (``same_title``).

Before every step that takes entries out, ``guard`` is told what must still be there afterwards: the sync keeps it,
so that a step broken off half way (a fault, a stop) is put right at the next sync instead of read as entries taken
out in Music Assistant. A step by place goes only from the reading the sync merged, while nobody else's task at the
playlist is under way (``_window``); somebody else's task that added there while it ran is told (``_unseen``: the entry
may be gone, Music Assistant names none), and then everything missing that was not meant goes back. A task nobody saw
end is ``TaskOpen`` (kept by the sync until ``task_ended``). Only an admin's token may run and cancel tasks: without one
nothing goes by place.
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

from ... import clock
from .. import music_assistant as client
from ..music_assistant import COVER_SIZE, PICTURE_ID, Connection, MaError, TaskOpen, picture
from ..plex import ALBUM as ALBUM_TYPE
from ..plex import ARTIST as ARTIST_TYPE
from ..plex import TRACK as TRACK_TYPE
from .base import Capabilities, Entry, Listed, NotSupported, Raced

if TYPE_CHECKING:
    from ..catalog import Writer

logger = logging.getLogger("nexmix.musicassistant")

CAPABILITIES = Capabilities(moods=False, sound=False, smart_playlists=False, duplicates=False, changes="events",
                            entries="track", rename="copy", moves="tail", unique_names=True)

#: Entries of one page of a list, and tracks to one request when adding.
PAGE = 1000
CHUNK = 500
#: A library id of Music Assistant: digits.
NUMBER = re.compile(r"\A[1-9][0-9]{0,15}\Z")
#: How long a task (adding, taking out) may take before nexmix gives up waiting, and how often it asks.
TASK_SECONDS = 60.0
TASK_EVERY = 0.05
#: The keys of the marks a run keeps: the totals of the library when it ran.
TRACKS, ALBUMS = "tracks", "albums"
#: The longest name Music Assistant keeps (it names a file by it).
NAME_LENGTH = 200
VARIOUS = "various artists"
#: Tracks of the library found by their provider's mapping, kept per server: ``{(server, provider, item): _Known}``.
KNOWN_MOST = 200_000
#: How long "not in the library" is believed.
UNKNOWN_SECONDS = 600.0

#: How a task may end.
SUCCESS, PARTIAL, FAILED, CANCELLED = "success", "partial_success", "failed", "cancelled"
ENDED = (SUCCESS, PARTIAL, FAILED)
#: The ``task_domain`` of a task that takes entries out of a playlist (every other one at a playlist may add).
REMOVE_DOMAIN = "playlist_remove_tracks"


def _text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _plain(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _year(value: Any) -> int | None:
    number = _plain(value)
    return number if number is not None and 1000 <= number <= 3000 else None


def number(value: Any) -> str:
    """A library id as nexmix sends it back: digits only; ``bad_path`` otherwise."""
    text = str(value or "").strip()
    if not NUMBER.fullmatch(text):
        raise MaError("bad_path", 400)
    return text


def _id(value: Any) -> str:
    text = str(value or "").strip()
    return text if NUMBER.fullmatch(text) else ""


def title_of(entry: dict[str, Any]) -> str:
    """The title as the file has it: Music Assistant's ``name`` with what it took out into ``version`` put back."""
    name = _text(entry.get("name"))
    version = _text(entry.get("version"), 200)
    if version and version.lower() not in name.lower():
        return f"{name} ({version})"[:500]
    return name


def _picture(entry: dict[str, Any] | None) -> str:
    """The id of a picture of a track's album or an album (``image``, else the first of ``metadata.images``)."""
    if not isinstance(entry, dict):
        return ""
    found = entry.get("image")
    candidates = [found] if isinstance(found, dict) else []
    meta = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
    candidates += [one for one in meta.get("images") or [] if isinstance(one, dict)]
    for one in candidates:
        ident = str(one.get("proxy_id") or "")
        if PICTURE_ID.fullmatch(ident) and one.get("type", "thumb") == "thumb":
            return ident
    return ""


def _people(entry: dict[str, Any]) -> list[tuple[str, str]]:
    found = []
    for one in entry.get("artists") or []:
        if isinstance(one, dict) and _text(one.get("name")):
            found.append((_id(one.get("item_id")), _text(one.get("name"))))
    return found


def _artist_key(ident: str, name: str) -> str:
    return ident or (f"name:{name.lower()}"[:200] if name else "")


@dataclass(frozen=True)
class _Known:
    """A track of the library, as an entry of a playlist needs it."""

    track: str
    title: str
    artist: str
    album: str
    album_key: str
    year: int | None
    duration: int


#: ``{(server, provider, item): library track or the moment it was not found}``, and the tracks so found by their id.
_known: dict[tuple[int, str, str], str | float] = {}
_tracks: dict[tuple[int, str], _Known] = {}
_known_lock = threading.Lock()


def forget() -> None:
    with _known_lock:
        _known.clear()
        _tracks.clear()


def _keep(server: int, mark: tuple[str, str], known: _Known | None) -> None:
    with _known_lock:
        if len(_known) > KNOWN_MOST:
            _known.clear()
            _tracks.clear()
        _known[(server, *mark)] = known.track if known is not None else clock.monotonic()
        if known is not None:
            _tracks[(server, known.track)] = known


@dataclass(frozen=True)
class Raw:
    """An entry of a playlist as Music Assistant reads it: its place (1 for the first), how it names its track, and the
    library's track (None: not in the library)."""

    place: int
    uri: str
    provider: str
    item: str
    track: str | None

    @property
    def ident(self) -> str:
        """What the entry is, whichever way Music Assistant writes it back: the library's track, else its URI."""
        return f"t:{self.track}" if self.track else f"u:{self.uri}"

    @property
    def adding(self) -> str:
        """How the entry is put in again: by the library's track (a URI of the provider as Music Assistant reads it
        back is not always taken), else by its URI."""
        return f"library://track/{self.track}" if self.track else self.uri

    @property
    def kept(self) -> list[str]:
        """The entry as the sync keeps it until a step is done (``guard``)."""
        return [self.ident, self.adding]


class MusicAssistantSource:
    """One Music Assistant server, opened for a while (``with``): what nexmix asks of it, in its library ids. The
    websocket is the server's shared one (``music_assistant.connection``), opened at the first question."""

    kind = "music_assistant"
    capabilities = CAPABILITIES

    def __init__(self, server_id: int, connect: Callable[[], Connection], providers: Sequence[str], *,
                 stop: Callable[[], bool] | None = None, timeout: float = 30.0, **_options: Any) -> None:
        self.server_id = server_id
        self._connect = connect
        self._connection: Connection | None = None
        self.providers = [str(one) for one in providers if str(one)]
        self._stop = stop
        self.timeout = timeout
        #: Music Assistant tells no time: the sync does without (``Listed.updated_at`` 0).
        self.clock: int | None = None
        self.began: int | None = None
        #: The tasks this connector started (anybody else's are told apart by that).
        self._own: set[str] = set()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        # The websocket is the server's, shared: it stays open.
        self._connection = None

    def call(self, command: str, args: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        if self._stop is not None and self._stop():
            raise MaError("stopped", 503)
        if self._connection is None or not self._connection.alive:
            self._connection = self._connect()
        return self._connection.call(command, args or {}, timeout or self.timeout)

    # --- Tasks --------------------------------------------------------------------------------------------------

    def _ask_task(self, command: str, ident: str) -> Any:
        """A question about a task under way. Music Assistant away meanwhile (the websocket broke, no answer): the
        task may still run, nobody knows (``TaskOpen``)."""
        try:
            return self.call(command, {"task_id": ident}, timeout=15.0)
        except MaError as exc:
            if exc.code in client.AWAY:
                raise TaskOpen(ident) from exc
            raise

    def _task(self, started: Any) -> str:
        """Waits for a task of Music Assistant to end; its status (``success``, ``partial_success``, ``failed``).
        Music Assistant runs its tasks one after another (a few at once), so nexmix's task is asked to run next
        (``tasks/run``): it may wait behind others, a lookup of metadata that hangs without the internet among them.
        A task that has not ended in ``TASK_SECONDS`` is cancelled; one cancelled before it ran did nothing
        (``ma_timeout``). One whose end nobody saw (cancelling refused, still running after a while longer, the
        websocket broken meanwhile) may run later: ``TaskOpen``, and the sync keeps it until Music Assistant says it
        ended (``task_ended``). A step by place must never run later, unseen."""
        if not isinstance(started, dict) or not started.get("id"):
            raise MaError("ma_error")
        ident = str(started["id"])[:64]
        self._own.add(ident)
        status = str(started.get("status") or "")
        if status not in ENDED:
            try:
                self._ask_task("tasks/run", ident)
            except TaskOpen:
                raise
            except MaError:
                pass
        deadline = clock.monotonic() + TASK_SECONDS
        cancelled = False
        while status not in ENDED:
            if status == CANCELLED:
                raise MaError("ma_timeout", 504)
            if clock.monotonic() > deadline:
                if cancelled:
                    logger.warning("Music Assistant task did not end, kept until it does")
                    raise TaskOpen(ident)
                cancelled = True
                deadline = clock.monotonic() + TASK_SECONDS / 2
                try:
                    self._ask_task("tasks/cancel", ident)
                except TaskOpen:
                    raise
                except MaError as exc:
                    # Refused (a token without the right, a task running already): not cancelled, it may still run.
                    logger.warning("Music Assistant did not cancel a task code=%s", exc.code)
            client.pause(TASK_EVERY)
            try:
                found = self._ask_task("tasks/get", ident)
            except TaskOpen:
                raise
            except MaError as exc:
                if exc.code == "ma_not_found":
                    # Music Assistant forgot it (it started anew): it never runs any more.
                    raise MaError("ma_timeout", 504) from exc
                raise TaskOpen(ident) from exc
            if not isinstance(found, dict):
                raise TaskOpen(ident)
            status = str(found.get("status") or "")
        if status != SUCCESS:
            logger.info("Music Assistant task did not go through whole status=%s", status)
        return status

    def task_ended(self, ident: str) -> bool:
        """Whether a task nexmix lost sight of has ended for sure: it ended, it was cancelled, or Music Assistant no
        longer knows it (it started anew: a task never outlives that). A task still waiting is asked to be cancelled
        once more (it may be refused again); a fault of the question is raised."""
        try:
            found = self.call("tasks/get", {"task_id": ident}, timeout=15.0)
        except MaError as exc:
            if exc.code == "ma_not_found":
                return True
            raise
        status = str(found.get("status") or "") if isinstance(found, dict) else ""
        if status in ENDED or status == CANCELLED:
            return True
        try:
            self.call("tasks/cancel", {"task_id": ident}, timeout=15.0)
        except MaError as exc:
            if exc.code in client.AWAY:
                raise
            return False
        again = self.call("tasks/get", {"task_id": ident}, timeout=15.0)
        return isinstance(again, dict) and str(again.get("status") or "") in (*ENDED, CANCELLED)

    def empty(self, key: str) -> bool:
        """Whether the playlist holds no entry at all, read now (the list's count is always 0 with Music Assistant)."""
        return not self.raw(key)

    def kept_now(self, key: str) -> list[list[str]]:
        """Every entry of the playlist now, as a guard keeps it (``Raw.kept``)."""
        return [one.kept for one in self.raw(key)]

    # --- Playlists ----------------------------------------------------------------------------------------------

    @staticmethod
    def _ours(entry: Any) -> bool:
        """A playlist of Music Assistant's own that may be changed: provider ``builtin``, editable, not dynamic."""
        if not isinstance(entry, dict) or not _id(entry.get("item_id")):
            return False
        if entry.get("is_editable") is not True or entry.get("is_dynamic") is True:
            return False
        mappings = [one for one in entry.get("provider_mappings") or [] if isinstance(one, dict)]
        return bool(mappings) and all(one.get("provider_domain") == "builtin" for one in mappings)

    @staticmethod
    def _listed(entry: dict[str, Any]) -> Listed:
        return Listed(key=_id(entry.get("item_id")), guid="", title=_text(entry.get("name"), NAME_LENGTH), smart=False,
                      leaf_count=0, updated_at=0)

    def listing(self) -> list[Listed]:
        """Music Assistant's own playlists that may be changed, in pages."""
        found: list[Listed] = []
        for offset in itertools.count(0, PAGE):
            page = self.call("music/playlists/library_items", {"limit": PAGE, "offset": offset}, timeout=60.0)
            entries = [one for one in page if isinstance(one, dict)] if isinstance(page, list) else []
            found.extend(self._listed(one) for one in entries if self._ours(one))
            if len(entries) < PAGE:
                return found
        return found

    def meta(self, key: str) -> Listed:
        """One playlist; ``ma_not_found`` once Music Assistant has it no longer (or it is none of its own)."""
        # Never a lookup of metadata on the way (it would wait in Music Assistant's tasks, before nexmix's own).
        found = self.call("music/playlists/get", {"item_id": number(key), "provider_instance_id_or_domain": "library",
                                                  "allow_update_metadata": False})
        if not self._ours(found):
            raise MaError("ma_not_found", 404)
        return self._listed(found)

    def gone(self, key: str) -> bool:
        """Whether Music Assistant has the playlist no longer: only when ``music/playlists/get`` says it does not know
        it (error 2). One it still gives, though no longer as one of its own that may be changed (no provider's
        mapping, made read-only), is no deletion."""
        try:
            self.call("music/playlists/get", {"item_id": number(key), "provider_instance_id_or_domain": "library",
                                              "allow_update_metadata": False})
        except MaError as exc:
            if exc.code == "ma_not_found":
                return True
            raise
        return False

    def unique_title(self, title: str) -> str:
        """A name no other playlist of Music Assistant has (``<name> (2)``, ``(3)`` …)."""
        wanted = _text(title, NAME_LENGTH) or "nexmix"
        taken = {one.title.casefold() for one in self.listing()}
        if wanted.casefold() not in taken:
            return wanted
        for count in itertools.count(2):
            mark = f" ({count})"
            made = wanted[:NAME_LENGTH - len(mark)] + mark
            if made.casefold() not in taken:
                return made
        return wanted

    @staticmethod
    def same_title(wanted: str, there: str) -> bool:
        """Whether the name Music Assistant has is the one nexmix gave: the same, or made unique with a number (the
        name cut short to make room for it, when it was long)."""
        name = _text(wanted, NAME_LENGTH)
        if there == name:
            return True
        found = re.fullmatch(r"(.+) \((\d{1,4})\)", there or "")
        if found is None or not name.startswith(found.group(1)):
            return False
        return found.group(1) == name or len(there) == NAME_LENGTH

    def create(self, title: str) -> Listed:
        """An empty playlist of Music Assistant's own named ``title`` (the caller made it unique)."""
        name = _text(title, NAME_LENGTH) or "nexmix"
        found = self.call("music/playlists/create_playlist", {"name": name})
        if not isinstance(found, dict) or not _id(found.get("item_id")):
            raise MaError("ma_error")
        return Listed(key=_id(found.get("item_id")), guid="", title=_text(found.get("name"), NAME_LENGTH) or name,
                      smart=False, leaf_count=0, updated_at=0)

    def rename(self, key: str, title: str) -> None:
        raise NotSupported("rename")

    def delete(self, key: str) -> bool:
        """Deleted for good (the file goes too). False: Music Assistant had it no longer."""
        try:
            self.call("music/library/remove_item", {"media_type": "playlist", "library_item_id": int(number(key))})
        except MaError as exc:
            if exc.code == "ma_not_found":
                return False
            raise
        return True

    def raw(self, key: str) -> list[Raw]:
        """Every entry in order as Music Assistant reads it now (``force_refresh``), each with its library track
        (None: not in the library)."""
        found = self.call("music/playlists/playlist_tracks", {
            "item_id": number(key), "provider_instance_id_or_domain": "library", "force_refresh": True}, timeout=120.0)
        entries = [one for one in found if isinstance(one, dict)] if isinstance(found, list) else []
        raws: list[Raw] = []
        for index, entry in enumerate(entries):
            mappings = [one for one in entry.get("provider_mappings") or [] if isinstance(one, dict)]
            provider = _text(mappings[0].get("provider_instance") if mappings else entry.get("provider"), 200)
            item = str((mappings[0].get("item_id") if mappings else entry.get("item_id")) or "")[:2000]
            place = _plain(entry.get("position"))
            uri = str(entry.get("uri") or "")[:2100]
            raws.append(Raw(place if place is not None else index + 1, uri, provider, item, None))
        known = self._resolve(raws)
        return [Raw(one.place, one.uri, one.provider, one.item, known.get((one.provider, one.item)))
                for one in raws]

    def _resolve(self, raws: list[Raw]) -> dict[tuple[str, str], str | None]:
        """The library's track of each entry, by its provider's mapping: kept ones, else asked
        (``music/get_library_item``)."""
        found: dict[tuple[str, str], str | None] = {}
        now = clock.monotonic()
        asking: list[tuple[str, str]] = []
        for one in raws:
            mark = (one.provider, one.item)
            if mark in found:
                continue
            if one.provider == "library" and _id(one.item):
                found[mark] = _id(one.item)
                continue
            with _known_lock:
                kept = _known.get((self.server_id, *mark))
            if isinstance(kept, str):
                found[mark] = kept
            elif isinstance(kept, float) and now - kept < UNKNOWN_SECONDS:
                found[mark] = None
            else:
                asking.append(mark)
                found[mark] = None
        for mark in asking:
            if not mark[0] or not mark[1]:
                continue
            track = self.call("music/get_library_item", {"media_type": "track", "item_id": mark[1],
                                                         "provider_instance_id_or_domain": mark[0]})
            known = self._known(track)
            _keep(self.server_id, mark, known)
            found[mark] = known.track if known is not None else None
        return found

    @staticmethod
    def _known(track: Any) -> _Known | None:
        if not isinstance(track, dict) or not _id(track.get("item_id")):
            return None
        album = track.get("album") if isinstance(track.get("album"), dict) else {}
        people = _people(track)
        album_people = _people(album) if album else []
        album_artist = album_people[0][1] if album_people else (people[0][1] if people else "")
        performer = ", ".join(name for _ident, name in people)[:500]
        return _Known(track=_id(track.get("item_id")), title=title_of(track),
                      artist=performer or album_artist, album=_text(album.get("name")),
                      album_key=_id(album.get("item_id")), year=_year(album.get("year")),
                      duration=max(0, (_plain(track.get("duration")) or 0) * 1000))

    def items(self, key: str) -> list[Entry]:
        """The entries whose track is in the library, in order, each once (at its first place)."""
        raws = self.raw(key)
        entries: list[Entry] = []
        seen: set[str] = set()
        for one in raws:
            if one.track is None or one.track in seen:
                continue
            seen.add(one.track)
            with _known_lock:
                known = _tracks.get((self.server_id, one.track))
            if known is None:
                known = self._fetch(one.track)
                if known is not None:
                    with _known_lock:
                        _tracks[(self.server_id, known.track)] = known
            entries.append(Entry(item=None, track=one.track, title=known.title if known else "",
                                 artist=known.artist if known else "", album=known.album if known else "",
                                 album_key=known.album_key if known else "", year=known.year if known else None,
                                 duration=known.duration if known else 0, guid="", ref=one.track))
        return entries

    def _fetch(self, track: str) -> _Known | None:
        try:
            found = self.call("music/tracks/get", {"item_id": number(track), "allow_update_metadata": False,
                                                   "provider_instance_id_or_domain": "library"})
        except MaError as exc:
            if exc.code == "ma_not_found":
                return None
            raise
        return self._known(found)

    def smart_items(self, key: str, limit: int | None = None) -> tuple[list[Entry], int]:
        raise NotSupported("smart playlists")

    def _add_uris(self, key: str, uris: Sequence[str]) -> None:
        for start in range(0, len(uris), CHUNK):
            part = list(uris[start:start + CHUNK])
            started = self.call("music/playlists/add_playlist_tracks", {"db_playlist_id": int(number(key)),
                                                                         "uris": part})
            self._task(started)

    def add(self, key: str, tracks: Sequence[str], answered: Callable[[list[str], int | None], None] | None = None,
            ) -> int:
        """The tracks at the end, in their order, ``CHUNK`` to a task. Music Assistant passes over a track it has
        there already or does not know without a word: ``answered`` gets None, and the caller reads again."""
        chosen = [number(track) for track in tracks]
        for start in range(0, len(chosen), CHUNK):
            part = chosen[start:start + CHUNK]
            self._add_uris(key, [f"library://track/{track}" for track in part])
            if answered is not None:
                answered(part, None)
        return len(chosen)

    def _take_out(self, key: str, places: list[int], length: int) -> str:
        """Entries out by place, checked: 1 to ``length``, each once. Never 0 or below (Music Assistant counts those
        from the end), never past the end (the whole task would fail)."""
        chosen = sorted(set(places))
        if not chosen:
            return SUCCESS
        if chosen[0] < 1 or chosen[-1] > length:
            raise MaError("ma_bad_place")
        # A failed task took nothing out (a place past the end fails it whole): the caller reads again.
        return self._task(self.call("music/playlists/remove_playlist_tracks", {
            "db_playlist_id": int(number(key)), "positions_to_remove": chosen}))

    @staticmethod
    def _places_ok(raws: list[Raw]) -> bool:
        return [one.place for one in raws] == list(range(1, len(raws) + 1))

    def _settle(self, key: str, should: list[Raw], guard: Callable[[list[list[str]] | None], None] | None) -> None:
        """After a step by place went otherwise than planned: whatever of ``should`` the playlist lacks goes back in,
        and the guard is let go of only once all of it is there again (else the next sync puts it back,
        ``restore``)."""
        there = {one.ident for one in self.raw(key)}
        missing = [one for one in should if one.ident not in there]
        if missing:
            self._add_uris(key, [one.adding for one in missing])
            there = {one.ident for one in self.raw(key)}
            missing = [one for one in should if one.ident not in there]
        if guard is not None:
            guard([one.kept for one in should] if missing else None)

    def _playlist_tasks(self, key: str) -> dict[str, tuple[str, str, int]] | None:
        """The tasks Music Assistant keeps of the playlist (``tasks/list``, its own and anybody else's, by their
        ``metadata``): ``{id: (status, task_domain, item_count)}``. None: Music Assistant would not tell."""
        try:
            found = self.call("tasks/list", {}, timeout=15.0)
        except MaError as exc:
            if exc.code in client.AWAY:
                raise
            logger.info("Music Assistant tasks not listed code=%s", exc.code)
            return None
        tasks: dict[str, tuple[str, str, int]] = {}
        for one in found if isinstance(found, list) else []:
            meta = one.get("metadata") if isinstance(one, dict) and isinstance(one.get("metadata"), dict) else None
            if meta is None or str(meta.get("playlist_id") or "") != str(key) or not one.get("id"):
                continue
            tasks[str(one["id"])[:64]] = (str(one.get("status") or ""), str(meta.get("task_domain") or ""),
                                          max(0, _plain(meta.get("item_count")) or 0))
        return tasks

    def _window(self, key: str, before: list[Raw],
                expected: Sequence[str] | None) -> dict[str, tuple[str, str, int]] | None:
        """Right before a step by place: the playlist as read now must be the one the sync merged (``expected``, the
        library's tracks in order, each once), and nobody else's task may be under way at it. Else the step waits and
        the sync merges again (``Raced``). Returns the playlist's tasks as they stand, to tell afterwards whose ran in
        between."""
        if expected is not None:
            now = list(dict.fromkeys(one.track for one in before if one.track is not None))
            if now != [number(one) for one in expected]:
                logger.info("Music Assistant playlist changed since it was read, merged again first")
                raise Raced
        tasks = self._playlist_tasks(key)
        if tasks and any(status not in (*ENDED, CANCELLED) and ident not in self._own
                         for ident, (status, _domain, _count) in tasks.items()):
            logger.info("Music Assistant playlist being changed by somebody else, merged again first")
            raise Raced
        return tasks

    def _foreign(self, key: str, watch: dict[str, tuple[str, str, int]] | None) -> int:
        """How many entries somebody else's tasks added to the playlist while a step by place ran (between the reading
        before and the one after; by their ``item_count``). 0: none, or Music Assistant would not tell."""
        if watch is None:
            return 0
        now = self._playlist_tasks(key)
        added = 0
        for ident, (status, domain, count) in (now or {}).items():
            if ident in self._own or (ident in watch and watch[ident][0] in (*ENDED, CANCELLED)):
                continue
            if status in (SUCCESS, PARTIAL) and domain != REMOVE_DOMAIN:
                added += max(count, 1)
        return added

    @staticmethod
    def _unseen(added: int, before: list[Raw], after: list[Raw], noticed: Callable[[int], None] | None) -> None:
        """Somebody else's tasks added ``added`` entries while a step by place ran: an entry added then may have stood
        at a place this step took out, and nexmix never saw it. When fewer new entries show than were added,
        ``noticed`` hears how many may be gone (the playlist says so; Music Assistant names no entry, so which one is
        not known)."""
        if not added or noticed is None:
            return
        seen = len({one.ident for one in after} - {one.ident for one in before})
        if seen < added:
            logger.warning("Music Assistant playlist changed while it was written by place, entries may be gone "
                           "count=%s", added - seen)
            noticed(added - seen)

    def _may_place(self) -> None:
        """Only with a token that may cancel a task (an admin's): a step by place nobody can cancel may run later,
        unseen. ``ma_not_admin`` otherwise (the card says so)."""
        if self._connection is None or not self._connection.alive:
            self._connection = self._connect()
        if not self._connection.admin:
            raise MaError(client.NOT_ADMIN, 409)

    def remove_many(self, key: str, refs: Sequence[str],
                    guard: Callable[[list[list[str]] | None], None] | None = None,
                    expected: Sequence[str] | None = None, noticed: Callable[[int], None] | None = None) -> None:
        """The entries of these tracks out, by their places in a reading of the moment (``_window``: the one the sync
        merged, nobody else's task under way). ``guard`` hears what must still be there afterwards before anything is
        taken out, and None once it is. Read again afterwards: when anything else is missing, what this step may have
        taken by mistake goes back in, and ``Raced`` says the playlist changed meanwhile; an entry added there
        meanwhile that may be gone is told (``noticed``)."""
        going = {number(ref) for ref in refs}
        self._may_place()
        before = self.raw(key)
        if not self._places_ok(before):
            raise MaError("ma_error")
        places = [one.place for one in before if one.track is not None and one.track in going]
        if not places:
            return
        watch = self._window(key, before, expected)
        staying = [one for one in before if one.place not in set(places)]
        if guard is not None:
            guard([one.kept for one in staying])
        try:
            status = self._take_out(key, places, len(before))
        except TaskOpen as exc:
            # It may run later: what it meant to take out is never put back for it.
            exc.meant = [one.ident for one in before if one.place in set(places)]
            raise
        if status in (FAILED, CANCELLED):
            # Nothing was taken out: the playlist was shorter by then.
            if guard is not None:
                guard(None)
            raise Raced
        after = self.raw(key)
        added = self._foreign(key, watch)
        self._unseen(added, before, after, noticed)
        if [one.ident for one in after] == [one.ident for one in staying]:
            if guard is not None:
                guard(None)
            return
        there = {one.ident for one in after}
        if added:
            # Somebody else added meanwhile (an entry taken out and put in again moves it to the end): the places may
            # have named anything, no explanation by entries taken out holds. Everything missing that was not meant
            # goes back (adding wins).
            wrong = {one.ident for one in staying if one.ident not in there}
        else:
            wrong = {one.ident for one in _wrongly_taken(before, after, places)}
        # Gone by somebody else's hand meanwhile: that stays so; what this step took by mistake goes back.
        foreign = {one.ident for one in staying if one.ident not in there and one.ident not in wrong}
        if wrong:
            logger.warning("Music Assistant playlist changed while entries were taken out, put back count=%s",
                           len(wrong))
        self._settle(key, [one for one in staying if one.ident not in foreign], guard)
        raise Raced

    def arrange(self, key: str, wanted: Sequence[str],
                guard: Callable[[list[list[str]] | None], None] | None = None,
                expected: Sequence[str] | None = None, noticed: Callable[[int], None] | None = None) -> bool:
        """The tracks ``wanted`` in that order; entries it does not name keep their place after the entry they
        followed. Music Assistant cannot move: everything from the first entry out of place is taken out and put back
        in the new order (only from the reading the sync merged, nobody else's task under way: ``_window``). False:
        the order was right already."""
        from .. import merge

        self._may_place()
        before = self.raw(key)
        if not self._places_ok(before):
            raise MaError("ma_error")
        if len({one.ident for one in before}) != len(before):
            # A track twice: Music Assistant would not take it twice again.
            raise MaError("ma_twice")
        place_of = {one.track: place for place, one in enumerate(before) if one.track is not None}
        order = merge.arrange(list(range(len(before))),
                              [place_of[track] for track in dict.fromkeys(wanted) if track in place_of])
        new = [before[place] for place in order]
        first = next((place for place, (one, two) in enumerate(zip(before, new, strict=True)) if one != two), None)
        if first is None:
            return False
        watch = self._window(key, before, expected)
        if guard is not None:
            guard([one.kept for one in before])
        if self._take_out(key, list(range(first + 1, len(before) + 1)), len(before)) in (FAILED, CANCELLED):
            if guard is not None:
                guard(None)
            raise Raced
        self._add_uris(key, [one.adding for one in new[first:]])
        after = self.raw(key)
        self._unseen(self._foreign(key, watch), before, after, noticed)
        if [one.ident for one in after] != [one.ident for one in new]:
            logger.warning("Music Assistant playlist changed while it was sorted, everything put back")
            self._settle(key, before, guard)
            raise Raced
        if guard is not None:
            guard(None)
        return True

    def restore(self, key: str, kept: Sequence[Any]) -> int:
        """What a step broken off half way had still to bring back (``Raw.kept``): every entry the playlist lacks, put
        back at the end. Returns how many."""
        there = {one.ident for one in self.raw(key)}
        missing: list[str] = []
        for one in kept:
            if isinstance(one, list | tuple) and len(one) == 2 and str(one[0]) not in there and str(one[1]):
                missing.append(str(one[1]))
        missing = list(dict.fromkeys(missing))
        if missing:
            self._add_uris(key, missing)
        return len(missing)

    def copy(self, key: str, into: str) -> list[str]:
        """Every entry of one playlist into another (empty) one, in order, also the ones whose track is not in the
        library (Music Assistant passes those over when their file is gone: they stay behind with the old one).
        Returns the entries the copy lacks (passed over): the caller keeps the library's ones as missing and never
        tries them again."""
        raws = self.raw(key)
        self._add_uris(into, [one.adding for one in raws])
        there = {one.ident for one in self.raw(into)}
        lacking = [one for one in raws if one.ident not in there]
        outside = sum(1 for one in lacking if one.track is None)
        if outside:
            logger.info("Entries outside the library left behind with the old playlist count=%s", outside)
        return lacking

    def top_up(self, key: str, into: str, but: Iterable[str] = ()) -> int:
        """What ``key`` holds that ``into`` lacks, put at the end of ``into`` (somebody added it in Music Assistant
        while it was copied); ``but`` (``Raw.ident``): what the copy passed over already. Returns how many."""
        there = {one.ident for one in self.raw(into)} | set(but)
        extra = list(dict.fromkeys(one.adding for one in self.raw(key) if one.ident not in there))
        if extra:
            self._add_uris(into, extra)
        return len(extra)

    def reorder(self, key: str, steps: Sequence[tuple[str, str | None]]) -> bool:
        raise NotSupported("moves")

    def existing(self, tracks: Iterable[str]) -> set[str]:
        """Which of the tracks the library still has, asked one by one. Any fault is raised: what was not asked is
        not known."""
        found: set[str] = set()
        for track in sorted({number(one) for one in tracks}):
            if self._fetch(track) is not None:
                found.add(track)
        return found

    def owned(self, keys: Iterable[str]) -> set[str]:
        """Every playlist of Music Assistant's own is nexmix's operator's (Music Assistant keeps no owner)."""
        return {str(key) for key in keys}

    def similar(self, track: str, limit: int, max_distance: float) -> list[tuple[str, float]]:
        raise NotSupported("sound")

    def recordings(self, tracks: Iterable[str]) -> dict[str, str]:
        """The MusicBrainz recording ids of some tracks (only the single track tells them), for finding them on
        another server."""
        found: dict[str, str] = {}
        for track in sorted({number(one) for one in tracks}):
            try:
                entry = self.call("music/tracks/get", {"item_id": track, "provider_instance_id_or_domain": "library",
                                                       "allow_update_metadata": False})
            except MaError as exc:
                if exc.code == "ma_not_found":
                    continue
                raise
            for pair in (entry.get("external_ids") or []) if isinstance(entry, dict) else []:
                if isinstance(pair, list | tuple) and len(pair) == 2 and pair[0] == "musicbrainz_recordingid":
                    value = str(pair[1] or "").lower()
                    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value):
                        found[track] = value
        return found

    def preview(self, track: str) -> str:
        """The path of a preview of the track (``/preview?token=…``): the first 30 seconds, good for a minute."""
        found = self.call("music/tracks/preview", {"provider_instance_id_or_domain": "library",
                                                   "item_id": number(track)}, timeout=15.0)
        return str(found or "")

    # --- Pictures -----------------------------------------------------------------------------------------------

    def cover(self, picture_id: str, size: int, url: str = "") -> tuple[bytes, str]:
        mapped = COVER_SIZE.get(size)
        if mapped is None:
            raise MaError("bad_path", 400)
        return picture(url, picture_id, mapped)

    # --- The catalogue ------------------------------------------------------------------------------------------

    def _filter(self) -> dict[str, Any]:
        return {"provider": list(self.providers)} if self.providers else {}

    def _pages(self, command: str, extra: dict[str, Any] | None = None) -> Iterable[list[dict[str, Any]]]:
        for offset in itertools.count(0, PAGE):
            page = self.call(command, {"limit": PAGE, "offset": offset, **self._filter(), **(extra or {})},
                             timeout=120.0)
            entries = [one for one in page if isinstance(one, dict)] if isinstance(page, list) else []
            yield entries
            if len(entries) < PAGE:
                return

    def _totals(self) -> dict[str, int]:
        return {TRACKS: int(self.call("music/tracks/count") or 0), ALBUMS: int(self.call("music/albums/count") or 0)}

    def full(self, writer: Writer) -> dict[str, Any]:
        """The whole catalogue of the chosen providers into the writer's generation; returns the marks for the next
        partial run (the totals of the library)."""
        if not self.providers:
            raise MaError("ma_no_provider", 409)
        totals = self._totals()
        artists: dict[str, str] = {}
        writer.step("albums")
        albums: dict[str, dict[str, Any]] = {}
        album_artist: dict[str, str] = {}
        for entries in self._pages("music/albums/library_items"):
            self._albums(writer, entries, artists, albums, album_artist)
            writer.share(len(albums) / totals[ALBUMS] if totals[ALBUMS] else 1.0)
            writer.check()
        writer.commit()
        writer.step("tracks")
        taken = 0
        seen: set[str] = set()
        for entries in self._pages("music/tracks/library_items"):
            rows = self._tracks(writer, entries, artists, albums, album_artist, seen)
            writer.rows(TRACK_TYPE, rows)
            taken += len(rows)
            writer.share(taken / totals[TRACKS] if totals[TRACKS] else 1.0)
            writer.check()
        writer.commit()
        writer.step("album_tags")
        self._genres(writer, seen)
        writer.step("artists")
        writer.rows(ARTIST_TYPE, self._artist_rows(writer, artists))
        writer.commit()
        return {"marks": totals, "analysis": {}, "counts": {"artists": len(artists), "albums": len(albums),
                                                           "tracks": taken}}

    def _albums(self, writer: Writer, entries: list[dict[str, Any]], artists: dict[str, str],
                into: dict[str, dict[str, Any]], album_artist: dict[str, str]) -> None:
        chosen = [entry for entry in entries if _id(entry.get("item_id"))]
        writer.numbers.ensure("album", (_id(entry.get("item_id")) for entry in chosen))
        rows = []
        for entry in chosen:
            key = _id(entry.get("item_id"))
            if key in into:
                continue
            people = _people(entry)
            ident, name = people[0] if people else ("", "")
            artist = _artist_key(ident, name)
            if artist:
                artists.setdefault(artist, name)
                writer.numbers.ensure("artist", [artist])
                album_artist[key] = artist
            row = {"id": writer.numbers.of("album", key),
                   "artist_id": (writer.numbers.of("artist", artist) or 0) if artist else 0,
                   "title": _text(entry.get("name")), "year": _year(entry.get("year")), "thumb": _picture(entry),
                   "added_at": 0, "updated_at": 0}
            rows.append(row)
            into[key] = row
        writer.rows(ALBUM_TYPE, rows)

    def _tracks(self, writer: Writer, entries: list[dict[str, Any]], artists: dict[str, str],
                albums: dict[str, dict[str, Any]], album_artist: dict[str, str],
                seen: set[str]) -> list[dict[str, Any]]:
        chosen = [entry for entry in entries if _id(entry.get("item_id"))]
        writer.numbers.ensure("track", (_id(entry.get("item_id")) for entry in chosen))
        rows = []
        for entry in chosen:
            key = _id(entry.get("item_id"))
            if key in seen:
                continue
            seen.add(key)
            album = entry.get("album") if isinstance(entry.get("album"), dict) else {}
            album_key = _id(album.get("item_id"))
            people = _people(entry)
            owner = album_artist.get(album_key, "")
            owner_name = artists.get(owner, "") if owner else ""
            if not owner and people:
                owner = _artist_key(*people[0])
                owner_name = people[0][1]
                artists.setdefault(owner, owner_name)
            if owner:
                writer.numbers.ensure("artist", [owner])
            names = [name for _ident, name in people]
            if owner_name.lower() == VARIOUS and names:
                performer = names[0]
            else:
                performer = ", ".join(names)[:500]
            if album_key and album_key not in albums:
                # An album the list of albums did not bring (another provider's): known by what the track says.
                writer.numbers.ensure("album", [album_key])
                row = {"id": writer.numbers.of("album", album_key),
                       "artist_id": (writer.numbers.of("artist", owner) or 0) if owner else 0,
                       "title": _text(album.get("name")), "year": _year(album.get("year")),
                       "thumb": _picture(album), "added_at": 0, "updated_at": 0}
                albums[album_key] = row
                writer.rows(ALBUM_TYPE, [row])
            year = albums[album_key]["year"] if album_key in albums else _year(album.get("year"))
            rows.append({
                "id": writer.numbers.of("track", key),
                "album_id": (writer.numbers.of("album", album_key) or 0) if album_key else 0,
                "artist_id": (writer.numbers.of("artist", owner) or 0) if owner else 0,
                "title": title_of(entry),
                "artist": performer if performer and performer != owner_name else "",
                "duration": max(0, (_plain(entry.get("duration")) or 0) * 1000),
                "rating_count": 0, "view_count": 0, "year": year,
                "disc": _plain(entry.get("disc_number")) or None, "number": _plain(entry.get("track_number")) or None,
                "added_at": 0, "updated_at": 0, "song_key": "", "variant": False, "shown": True,
                "original_year": None, "guid": "", "mbid_track": "", "mbid_recording": "",
            })
        return rows

    def _genres(self, writer: Writer, seen: set[str]) -> None:
        """The tracks of each of Music Assistant's genres (by its own list), linked by name."""
        found = self.call("music/genres/library_items", {"limit": PAGE}, timeout=60.0)
        genres = [one for one in found if isinstance(one, dict)] if isinstance(found, list) else []
        for place, genre in enumerate(genres):
            ident, name = _plain(genre.get("item_id")), _text(genre.get("name"), 200)
            if ident is None or not name:
                continue
            for entries in self._pages("music/tracks/library_items", {"genre": ident}):
                for entry in entries:
                    key = _id(entry.get("item_id"))
                    if key in seen:
                        number_of = writer.numbers.of("track", key)
                        if number_of is not None:
                            writer.link(TRACK_TYPE, number_of, "genre", name)
            writer.share((place + 1) / len(genres))
            writer.check()
        writer.tags.write(writer.db)
        writer.commit()

    @staticmethod
    def _artist_rows(writer: Writer, artists: dict[str, str]) -> list[dict[str, Any]]:
        writer.numbers.ensure("artist", artists)
        return [{"id": writer.numbers.of("artist", key), "title": _text(name), "thumb": "", "added_at": 0,
                 "updated_at": 0} for key, name in artists.items()]

    def partial(self, writer: Writer, marks: dict[str, int], analysis_due: bool) -> dict[str, Any]:
        """Music Assistant says nothing of what changed: the totals of the library are compared with the last run's,
        and when they differ a full run follows (``TooMany``). Same totals: nothing to take in."""
        from ..catalog import TooMany

        writer.step("changes")
        totals = self._totals()
        if not marks or any(int(marks.get(name) or -1) != value for name, value in totals.items()):
            raise TooMany
        rows: dict[int, dict[int, dict[str, Any]]] = {ARTIST_TYPE: {}, ALBUM_TYPE: {}, TRACK_TYPE: {}}
        tags: dict[int, dict[int, dict[str, list[str]]]] = {ARTIST_TYPE: {}, ALBUM_TYPE: {}, TRACK_TYPE: {}}
        return {"rows": rows, "tags": tags, "gone": {}, "marks": totals, "analysis": {}}


def _wrongly_taken(before: list[Raw], after: list[Raw], places: list[int]) -> list[Raw]:
    """What taking out ``places`` of ``before`` may have taken by mistake, now that the playlist reads ``after``:
    somebody else took entries out meanwhile (before or after this step), so the places named other entries. Every way
    the entries missing now can be told apart into "taken by somebody else before", "by this step" and "by somebody
    else after" is tried; what this step took in any of them, beside what it meant to take, goes back. When nothing
    explains what happened (entries added in between, moved, too many to try), everything missing that was not meant
    goes back: an entry put back once too often is better than one lost."""
    meant = {one.ident for one in before if one.place in set(places)}
    there = {one.ident for one in after}
    missing = [one for one in before if one.ident not in there]
    lost = {one.ident for one in missing}
    if len(missing) > 12 or not lost:
        return [one for one in missing if one.ident not in meant]
    taken_by_us: set[str] = set()
    explained = False
    names = [one.ident for one in before]
    for size in range(len(missing) + 1):
        for earlier in itertools.combinations(sorted(lost), size):
            gone_first = set(earlier)
            rest = [uri for uri in names if uri not in gone_first]
            if max(places) > len(rest):
                continue
            ours = {rest[place - 1] for place in sorted(set(places))}
            if ours & gone_first or not ours <= lost:
                continue
            later = lost - gone_first - ours
            expected = [uri for uri in rest if uri not in ours and uri not in later]
            if [uri for uri in [one.ident for one in after] if uri in set(names)] != expected:
                continue
            explained = True
            taken_by_us |= ours - meant
    if not explained:
        return [one for one in missing if one.ident not in meant]
    return [one for one in before if one.ident in taken_by_us]
