"""Guests: people without an account who join one playlist by a link or a QR code and a name.

**The invitation** belongs to a playlist (``GuestInvite``). The owner, whoever may change the playlist and the operator
make it, start it, change what its guests may do and end it. Its code is random, ten letters and digits without the
ones a font mixes up, and stored as a hash (to find it) and sealed with the server secret (to show the link again). A
playlist has at most one invitation that has not ended. It lasts ``hours`` from its start; ended or run out, every
guest of it is out at once, their cookies are worth nothing, and the next tidy-up takes the guests away (their names
stay on the entries as text).

An invitation lives on its inviter: when that person may no longer change the playlist (the share taken back or set
to read, the operator role gone, the account blocked or deleted), the invitation is over for every guest at the next
request, and ``recheck_inviters`` ends it at once where the interface takes the right away.

**A guest** (``Guest``) has a name and a cookie (``GUEST_COOKIE``): a random token, only its hash is stored, valid for
this one invitation. A guest taken out by the host stays out in that browser; with the link, another browser comes in
as a new guest while the invitation runs (only ending it keeps everybody out).

**What a guest reaches** is this playlist (its entries with the names on them, the inviter's name, nothing of other
accounts), the catalogue and the covers. It adds tracks while the invitation allows it and up to its limit, takes its
own entries out again, and with the right to sort also moves entries and takes out those of others. Each change takes
the write lock first (the version of the playlist goes up as the first statement) and checks the invitation, the guest,
the right and the limit after it, so that ten requests at once never pass the limit together.

**Brakes** in memory (``Brake``): wrong codes per sender, the page of the link, its covers and joining per invitation,
joining also per invitation and sender, changes, reads and covers per guest. A wrong code never holds up a right one,
so a stranger guessing behind the address of a party cannot keep the party out; the codes cannot be guessed (31^12)
and wrong ones are slowed per sender. Whoever has the code can join many times from one address, but not use up the
invitation for the others from there.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import ipaddress
import logging
import secrets
import threading
import unicodedata
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import segno
from fastapi import Request
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import clock
from ..config import get_settings
from ..errors import error
from ..models import Account, Guest, GuestEntry, GuestInvite, Playlist, PlaylistItem
from ..security import decrypt_secret, encrypt_secret, server_key
from . import playlists, preview, settings_service
from .events import hub, playlist_topic

logger = logging.getLogger("nexmix.guests")

#: No 0/O, no 1/I/L: a code read off a phone and typed must not depend on the font.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 12
GUEST_COOKIE = "nexmix_guest" + get_settings().cookie_name_suffix()
#: The cookie is sent only to the interface.
COOKIE_PATH = "/api/v1"
NAME_LENGTH = 24
MAX_HOURS = 720
MAX_TRACKS = 100
#: Guests one invitation takes; past that it is full (a flood, not a party).
MAX_GUESTS = 300
#: Tracks one request of a guest adds at most.
MAX_ADD = 20
#: An invitation made but never started goes after this long.
WAITING_DAYS = 7
SEALED = "guest-invite"


# --- Brakes ----------------------------------------------------------------------------------------------------------


class Brake:
    """At most ``limit`` times within ``seconds`` per key; kept in memory (a restart forgets, which costs nothing)."""

    def __init__(self, limit: int, seconds: float) -> None:
        self.limit = limit
        self.seconds = seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, moment: float) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        while hits and moment - hits[0] >= self.seconds:
            hits.popleft()
        return hits

    def _wait(self, hits: deque[float], moment: float, limit: int) -> int:
        if len(hits) < limit:
            return 0
        # The oldest that still counts against the limit; the ones before it go first.
        return max(1, int(hits[len(hits) - limit] + self.seconds - moment + 0.999))

    def wait(self, key: str, limit: int | None = None) -> int:
        """Seconds until the key may again; 0 while it may. ``limit``: another limit for this key."""
        moment = clock.monotonic()
        with self._lock:
            return self._wait(self._recent(key, moment), moment, self.limit if limit is None else limit)

    def note(self, key: str, limit: int | None = None) -> None:
        """One more for the key; past its limit nothing more is kept (a flood costs no memory)."""
        moment = clock.monotonic()
        with self._lock:
            hits = self._recent(key, moment)
            if len(hits) < (self.limit if limit is None else limit):
                hits.append(moment)
            if len(self._hits) > 5000:
                for stale in [k for k, hits in self._hits.items() if not hits or moment - hits[-1] >= self.seconds]:
                    del self._hits[stale]

    def take(self, key: str) -> None:
        """One more, or 429 with ``Retry-After``."""
        moment = clock.monotonic()
        with self._lock:
            hits = self._recent(key, moment)
            wait = self._wait(hits, moment, self.limit)
            if wait:
                raise _slow(wait)
            hits.append(moment)

    def forget(self) -> None:
        with self._lock:
            self._hits.clear()


def _slow(wait: int) -> Exception:
    exc = error("too_many_attempts", "Too many attempts. Wait a moment.", 429, retry_after=wait)
    exc.headers = {"Retry-After": str(wait)}
    return exc


#: Wrong codes, per sender (``deps.address_keys``): this many within ten minutes, then every wrong code of the sender
#: is 429 until the oldest is ten minutes old. Behind a proxy nexmix was not told to believe, the sender is the proxy
#: and the last address it names, and the proxy as a whole has ``MISS_PROXY_LIMIT``: made-up addresses buy no more.
#: A right code is never held up here: a party shares one address (a home network, a carrier's), and one stranger
#: among them must not keep them out. Guessing stays slow all the same: a code is one of 31^12.
MISS_LIMIT = 30
MISS_PROXY_LIMIT = 300
MISS = Brake(MISS_LIMIT, 600)
#: Per invitation, for whoever has its code: reading the page of the link, its covers, guests joining (a flood of
#: names, not a party). Nothing of these counts against another invitation.
DOOR = Brake(1000, 600)
DOOR_COVER = Brake(3000, 600)
JOIN_CODE = Brake(MAX_GUESTS, 600)
#: Joining, per invitation and sender: whoever has the code cannot use up the room of the whole invitation from one
#: address. A party behind one address (a home network) still gets this many guests in within ten minutes, and the
#: next ones a little later; behind a proxy nexmix was not told to believe, the proxy as a whole has the room of the
#: invitation. Many addresses with the code can still fill the door: then the host ends the invitation and makes a
#: new one (the old code is dead at once).
JOIN_SENDER_LIMIT = 60
JOIN_SENDER = Brake(JOIN_SENDER_LIMIT, 600)
#: Per guest: changes, reads, covers.
ACT = Brake(60, 60)
READ = Brake(240, 60)
COVER = Brake(900, 60)
#: Live streams one guest may hold at once.
STREAMS = 3
_streams: dict[int, int] = {}
_streams_lock = threading.Lock()

ALL = (MISS, DOOR, DOOR_COVER, JOIN_CODE, JOIN_SENDER, ACT, READ, COVER)


def forget() -> None:
    """For the tests."""
    for brake in ALL:
        brake.forget()
    with _streams_lock:
        _streams.clear()


def open_stream(guest_id: int) -> bool:
    with _streams_lock:
        if _streams.get(guest_id, 0) >= STREAMS:
            return False
        _streams[guest_id] = _streams.get(guest_id, 0) + 1
        return True


def close_stream(guest_id: int) -> None:
    with _streams_lock:
        left = _streams.get(guest_id, 0) - 1
        if left > 0:
            _streams[guest_id] = left
        else:
            _streams.pop(guest_id, None)


# --- Codes, the link, the QR picture ----------------------------------------------------------------------------------


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def normalise(code: str) -> str:
    """What was typed or read, without blanks and dashes, in capitals."""
    return "".join(ch for ch in str(code or "") if not ch.isspace() and ch != "-").upper()


def well_formed(code: str) -> bool:
    return len(code) == CODE_LENGTH and all(ch in ALPHABET for ch in code)


def digest(code: str) -> str:
    """Keyed with the server secret: a copy of the database (a backup) alone does not lead to the codes."""
    return hmac.new(server_key(), b"guest-code:" + normalise(code).encode("utf-8"), hashlib.sha256).hexdigest()


def shown(code: str) -> str:
    """``K7MX-QR2H-PA4N``: groups of four, easier to read out."""
    return "-".join(code[start:start + 4] for start in range(0, len(code), 4))


def qr_svg(text: str) -> str:
    """Drawn here, never by a service on the internet: the link is the way into the playlist. With the SVG namespace, so
    that a browser shows it as a ``data:`` picture."""
    out = io.BytesIO()
    segno.make(text, error="m", micro=False).save(out, kind="svg", scale=10, border=2, dark="#1b1830",
                                                  light="#ffffff", xmldecl=False, svgns=True)
    return out.getvalue().decode("utf-8")


#: Names that only a home network knows.
HOME_SUFFIXES = (".local", ".lan", ".home", ".internal", ".intranet", ".localdomain", ".home.arpa", ".corp",
                 ".private", ".fritz.box", ".box")


def reach(url: str) -> str:
    """Who can open an address: ``internet``, only the ``home`` network, or only this ``machine``."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".localhost"):
        return "machine"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host.endswith(HOME_SUFFIXES):
            return "home"
        return "internet"
    if address.is_loopback or address.is_unspecified:
        return "machine"
    if address.is_private or address.is_link_local or address.is_reserved:
        return "home"
    return "internet"


def base_url(db: Session, request: Request) -> tuple[str, bool]:
    """The address the link starts with, and whether the operator set it (else it is the one of this request)."""
    configured = settings_service.public_url(db)
    if configured:
        return configured, True
    return str(request.base_url).rstrip("/"), False


# --- Names -----------------------------------------------------------------------------------------------------------


#: Letters that show nothing although they are not blanks (Hangul fillers, the Braille blank, invisible marks).
_BLANK_LOOKING = frozenset("ᅟᅠㅤﾠ⠀͏឴឵᠋᠌᠍᠏")
_JOINER = "‍"
#: Asks for the picture of the sign before it (an emoji); after a letter it means nothing.
_EMOJI_STYLE = frozenset("︎️")
#: Scripts whose letters look alike: a name takes its letters from one of them.
_LOOKALIKE_SCRIPTS = ("LATIN", "CYRILLIC", "GREEK")
#: Marks on one letter at most (a Vietnamese letter has two).
_MARKS = 2


def _emoji(char: str) -> bool:
    return unicodedata.category(char) in ("So", "Sk")


def _script(char: str) -> str | None:
    name = unicodedata.name(char, "")
    return next((script for script in _LOOKALIKE_SCRIPTS if name.startswith(script + " ")), None)


def clean_name(raw: Any) -> str | None:
    """A name of 1 to ``NAME_LENGTH`` characters, or None. Composed and compatibility-folded (NFKC: a full-width or
    decomposed letter becomes the usual one), blanks folded; no control, format, private or unassigned characters, no
    letters that show nothing, no tower of marks, no mix of Latin, Cyrillic and Greek letters (``Аnna`` with a
    Cyrillic A), at least one letter, digit or sign. A joiner is welcome between emoji, the emoji style after one."""
    text = " ".join(unicodedata.normalize("NFKC", str(raw or "")).split())
    if not text or len(text) > NAME_LENGTH:
        return None
    marks = 0
    scripts: set[str] = set()
    for index, char in enumerate(text):
        kind = unicodedata.category(char)
        before = text[index - 1] if index else ""
        if char == _JOINER:
            # Inside an emoji sequence only: an emoji (perhaps in emoji style) before, an emoji after.
            base = text[index - 2] if before in _EMOJI_STYLE and index > 1 else before
            if not base or not _emoji(base) or index + 1 >= len(text) or not _emoji(text[index + 1]):
                return None
            continue
        if char in _EMOJI_STYLE:
            if not before or not _emoji(before):
                return None
            continue
        if kind.startswith("C") or kind in ("Zl", "Zp") or char in _BLANK_LOOKING:
            return None
        if kind.startswith("M"):
            marks += 1
            if not before or before == " " or marks > _MARKS:
                return None
            continue
        marks = 0
        script = _script(char) if kind.startswith("L") else None
        if script:
            scripts.add(script)
    if len(scripts) > 1:
        return None
    if not any(unicodedata.category(char)[0] in "LNS" for char in text):
        return None
    return text


def name_key(name: str) -> str:
    """What two names have in common when they are the same to a reader: case and width do not count."""
    return unicodedata.normalize("NFKC", name).casefold()


def _candidates(name: str) -> Iterator[str]:
    yield name
    for number in range(2, 1000):
        yield f"{name[:NAME_LENGTH - 4].rstrip()} {number}"
    while True:
        yield f"{name[:NAME_LENGTH - 8].rstrip()} {secrets.randbelow(10**6)}"


# --- The state of an invitation ---------------------------------------------------------------------------------------


def running(invite: GuestInvite | None, now: datetime | None = None) -> bool:
    """Started, not ended, not run out."""
    moment = now or clock.now()
    return (invite is not None and invite.started_at is not None and invite.ended_at is None
            and invite.expires_at is not None and invite.expires_at > moment)


def inviter_may(db: Session, invite: GuestInvite, playlist: Playlist | None = None) -> bool:
    """Whether the person who made the invitation may still change the playlist: an account that is there, not
    blocked, and owner, operator or shared with to change. Asked at every request of a guest: whoever loses that right
    takes the guests of its invitations with it at once."""
    if invite.created_by is None:
        return False
    who = db.get(Account, invite.created_by, populate_existing=True)
    if who is None or who.blocked_at is not None:
        return False
    found = playlist if playlist is not None else db.get(Playlist, invite.playlist_id)
    return found is not None and playlists.access_of(db, who, found) in playlists.CAN_EDIT


def _end(db: Session, invite: GuestInvite, at: datetime | None = None) -> None:
    invite.ended_at = at or clock.now()
    db.commit()
    _publish_invite(invite.id, "ended")


def open_invite(db: Session, playlist_id: int) -> GuestInvite | None:
    """The invitation of a playlist that has not ended (waiting or running); one that ran out, or whose inviter may no
    longer change the playlist, is ended on the way."""
    invite = db.scalar(select(GuestInvite).where(GuestInvite.playlist_id == playlist_id,
                                                 GuestInvite.ended_at.is_(None))
                       .execution_options(populate_existing=True))
    if invite is not None and invite.started_at is not None and not running(invite):
        _end(db, invite, invite.expires_at)
        return None
    if invite is not None and not inviter_may(db, invite):
        _end(db, invite)
        logger.info("Guest invitation ended, its inviter may no longer change the playlist playlist=%s", playlist_id)
        return None
    return invite


def recheck_inviters(db: Session) -> int:
    """Ends every invitation whose inviter may no longer change its playlist (a share taken back or set to read, a
    role, an account blocked or deleted), so that open pages of its guests hear it at once; returns how many."""
    ended = 0
    for invite in list(db.scalars(select(GuestInvite).where(GuestInvite.ended_at.is_(None)))):
        if not inviter_may(db, invite):
            _end(db, invite)
            _publish_guests(db, invite.playlist_id)
            logger.info("Guest invitation ended, its inviter may no longer change the playlist playlist=%s",
                        invite.playlist_id)
            ended += 1
    return ended


def live_count(db: Session, playlist_id: int) -> int | None:
    """How many guests a running invitation of the playlist has; None without one (for the cards)."""
    invite = db.scalar(select(GuestInvite).where(GuestInvite.playlist_id == playlist_id,
                                                 GuestInvite.ended_at.is_(None)))
    if not running(invite):
        return None
    assert invite is not None
    if not inviter_may(db, invite):
        return None
    return int(db.scalar(select(func.count()).select_from(Guest)
                         .where(Guest.invite_id == invite.id, Guest.removed_at.is_(None))) or 0)


def invite_topic(invite_id: int) -> str:
    return f"invite:{int(invite_id)}"


def guest_topic(guest_id: int) -> str:
    return f"guest:{int(guest_id)}"


def _publish_invite(invite_id: int, event: str, payload: dict[str, Any] | None = None) -> None:
    hub.publish(invite_topic(invite_id), event, payload or {})


def _publish_guests(db: Session, playlist_id: int) -> None:
    """The people who have the playlist open learn how many guests there are now."""
    hub.publish(playlist_topic(playlist_id), "guests", {"guests": live_count(db, playlist_id)})


def defaults(db: Session) -> dict[str, Any]:
    values = settings_service.get_all(db)
    return {
        "may_add": True,
        "may_sort": False,
        "max": max(1, min(MAX_TRACKS, int(values["guest_max"]))),
        "hours": max(1, min(MAX_HOURS, int(values["guest_hours"]))),
        "preview": bool(values["guest_preview"]),
    }


def _contributions(db: Session, guest_ids: list[int]) -> dict[int, int]:
    """Entries each guest has in the playlist now (taken-out ones do not count)."""
    if not guest_ids:
        return {}
    rows = db.execute(select(GuestEntry.guest_id, func.count())
                      .join(PlaylistItem, PlaylistItem.id == GuestEntry.item_id)
                      .where(GuestEntry.guest_id.in_(guest_ids), PlaylistItem.removed_at.is_(None))
                      .group_by(GuestEntry.guest_id)).all()
    return {int(guest): int(count) for guest, count in rows}


def host_view(db: Session, request: Request, playlist: Playlist) -> dict[str, Any]:
    """What the dialog of the people who invite shows: the invitation (code, link, QR, rights), the guests with their
    contributions, the defaults, and where the link leads."""
    base, configured = base_url(db, request)
    invite = open_invite(db, playlist.id)
    found: dict[str, Any] = {
        "invite": None,
        "guests": [],
        "defaults": defaults(db),
        "address": {"url": base, "configured": configured, "reach": reach(base)},
        # The operator's switch: off, no guest listens in whatever the invitation says.
        "listening": preview.allowed(db),
    }
    if invite is None:
        return found
    code = decrypt_secret(invite.code_enc, SEALED)
    link = f"{base}/j/{shown(code)}" if code else ""
    found["invite"] = {
        "code": shown(code) if code else "",
        "link": link,
        "qr": qr_svg(link) if link else "",
        "state": "running" if invite.started_at is not None else "waiting",
        "may_add": invite.may_add,
        "may_sort": invite.may_sort,
        "max": invite.max_tracks,
        "hours": invite.hours,
        "preview": invite.preview,
        "started_at": invite.started_at.isoformat() if invite.started_at else None,
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
    }
    people = list(db.scalars(select(Guest).where(Guest.invite_id == invite.id, Guest.removed_at.is_(None))
                             .order_by(Guest.joined_at, Guest.id)))
    counts = _contributions(db, [guest.id for guest in people])
    found["guests"] = [{"id": guest.id, "name": guest.name, "count": counts.get(guest.id, 0),
                        "joined_at": guest.joined_at.isoformat()} for guest in people]
    return found


# --- For the people who invite ----------------------------------------------------------------------------------------


def _editable(db: Session, account: Account, playlist_id: int) -> Playlist:
    """The playlist, when the account may change it (owner, ``edit``, operator) and it is not in the trash."""
    playlist, _level = playlists.load(db, account, playlist_id, "edit")
    return playlist


def prepare(db: Session, request: Request, account: Account, playlist_id: int) -> dict[str, Any]:
    """An invitation that waits to be started, with the defaults, unless the playlist has one that has not ended."""
    playlist = _editable(db, account, playlist_id)
    if open_invite(db, playlist_id) is None:
        values = defaults(db)
        code = new_code()
        db.add(GuestInvite(playlist_id=playlist_id, created_by=account.id, code_hash=digest(code),
                           code_enc=encrypt_secret(code, SEALED), may_add=values["may_add"],
                           may_sort=values["may_sort"], max_tracks=values["max"], hours=values["hours"],
                           preview=values["preview"], created_at=clock.now()))
        try:
            db.commit()
        except IntegrityError:
            # Two at once: the other one made it.
            db.rollback()
        else:
            logger.info("Guest invitation made playlist=%s", playlist_id)
    return host_view(db, request, playlist)


def change(db: Session, request: Request, account: Account, playlist_id: int, values: dict[str, Any],
           start: bool) -> dict[str, Any]:
    """Changes what the guests may do, and starts the invitation when asked. The hours count from the start."""
    playlist = _editable(db, account, playlist_id)
    invite = open_invite(db, playlist_id)
    if invite is None:
        raise error("no_invite", "There is no invitation to change. Make one first.", 404)
    if "may_add" in values:
        invite.may_add = bool(values["may_add"])
    if "may_sort" in values:
        invite.may_sort = bool(values["may_sort"])
    if "max" in values:
        invite.max_tracks = max(1, min(MAX_TRACKS, int(values["max"])))
    if "hours" in values:
        invite.hours = max(1, min(MAX_HOURS, int(values["hours"])))
    if "preview" in values:
        invite.preview = bool(values["preview"])
    now = clock.now()
    started = False
    if start and invite.started_at is None:
        invite.started_at = now
        started = True
    if invite.started_at is not None:
        invite.expires_at = invite.started_at + timedelta(hours=invite.hours)
    db.commit()
    if started:
        logger.info("Guest invitation started playlist=%s hours=%s", playlist_id, invite.hours)
    if invite.started_at is not None:
        _publish_invite(invite.id, "invite")
        if not running(invite, now):
            # Fewer hours than have passed since the start: over now.
            open_invite(db, playlist_id)
        _publish_guests(db, playlist_id)
    return host_view(db, request, playlist)


def end(db: Session, account: Account, playlist_id: int) -> None:
    """Ends the invitation (waiting or running): every guest is out at once, the code leads nowhere any more."""
    _editable(db, account, playlist_id)
    invite = db.scalar(select(GuestInvite).where(GuestInvite.playlist_id == playlist_id,
                                                 GuestInvite.ended_at.is_(None)))
    if invite is None:
        return
    invite.ended_at = clock.now()
    db.commit()
    logger.info("Guest invitation ended playlist=%s", playlist_id)
    _publish_invite(invite.id, "ended")
    _publish_guests(db, playlist_id)


def end_with_playlist(db: Session, playlist_id: int) -> None:
    """The playlist went to the trash: its invitation ends too."""
    invite = db.scalar(select(GuestInvite).where(GuestInvite.playlist_id == playlist_id,
                                                 GuestInvite.ended_at.is_(None)))
    if invite is None:
        return
    invite.ended_at = clock.now()
    db.commit()
    _publish_invite(invite.id, "ended")


def remove_guest(db: Session, account: Account, playlist_id: int, guest_id: int) -> None:
    """Takes one guest out at once; what it added stays."""
    _editable(db, account, playlist_id)
    guest = db.scalar(select(Guest).join(GuestInvite, GuestInvite.id == Guest.invite_id)
                      .where(Guest.id == guest_id, GuestInvite.playlist_id == playlist_id,
                             GuestInvite.ended_at.is_(None)))
    if guest is None:
        raise error("not_found", "Not found.", 404)
    if guest.removed_at is None:
        guest.removed_at = clock.now()
        db.commit()
        logger.info("Guest taken out playlist=%s guest=%s", playlist_id, guest_id)
    hub.publish(guest_topic(guest_id), "removed", {})
    _publish_guests(db, playlist_id)


# --- Joining ---------------------------------------------------------------------------------------------------------


def _miss_keys(request: Request) -> list[tuple[str, int]]:
    """The counts a wrong code of this request goes to, each with its limit: the sender, and behind a proxy nexmix was
    not told to believe also the proxy as a whole (``deps.address_keys``)."""
    from ..deps import address_keys

    return [(key, MISS_PROXY_LIMIT if key.startswith("proxy:") else MISS_LIMIT) for key, _free in address_keys(request)]


def _unknown() -> Exception:
    return error("invite_unknown", "This invitation is not valid (any more).", 404)


def find(db: Session, request: Request, code: str, brake: Brake | None = DOOR) -> tuple[GuestInvite, Playlist]:
    """The running invitation a code names. A wrong code counts against its sender (``MISS``); past the limit every
    wrong code of the sender is 429 instead of 404. A right code is never held up by the sender: a party shares one
    address, and a stranger among them must not keep them out. It counts against its invitation instead (``brake``:
    the page of the link or its covers; joining counts on its own)."""
    wanted = normalise(code)
    invite = None
    if well_formed(wanted):
        invite = db.scalar(select(GuestInvite).where(GuestInvite.code_hash == digest(wanted))
                           .execution_options(populate_existing=True))
    playlist = db.get(Playlist, invite.playlist_id, populate_existing=True) if invite is not None else None
    if (invite is None or playlist is None or playlist.deleted_at is not None or not running(invite)
            or not inviter_may(db, invite, playlist)):
        keys = _miss_keys(request)
        wait = max(MISS.wait(key, limit) for key, limit in keys)
        if wait:
            raise _slow(wait)
        for key, limit in keys:
            MISS.note(key, limit)
        raise _unknown()
    if brake is not None:
        brake.take("i:" + str(invite.id))
    return invite, playlist


def _cookie_guest(db: Session, request: Request) -> Guest | None:
    token = request.cookies.get(GUEST_COOKIE)
    if not token or len(token) > 200:
        return None
    return db.scalar(select(Guest).where(Guest.token_hash == hashlib.sha256(token.encode("utf-8")).hexdigest())
                     .execution_options(populate_existing=True))


def shown_name(account: Account | None) -> str:
    """How a guest sees a person of the household: by the display name only, never by the name to sign in with;
    empty without one (the page says "someone from the household")."""
    return (account.display_name or "").strip() if account is not None else ""


def _inviter(db: Session, invite: GuestInvite) -> str:
    return shown_name(db.get(Account, invite.created_by) if invite.created_by is not None else None)


def door(db: Session, request: Request, code: str) -> dict[str, Any]:
    """What the page of the link shows before the name: the playlist, who invites, and whether this browser is in."""
    invite, playlist = find(db, request, code)
    guest = _cookie_guest(db, request)
    if guest is not None and guest.invite_id == invite.id and guest.removed_at is not None:
        raise error("guest_removed", "The host has taken you out of this playlist.", 403)
    items = playlists.live_items(db, playlist.id)
    summary = playlists.summary(db, items, server_id=playlist.source_server_id)
    return {
        "playlist": {"name": playlist.name, "count": summary["count"], "duration": summary["duration"],
                     "covers": summary["covers"]},
        "inviter": _inviter(db, invite),
        "joined": guest is not None and guest.invite_id == invite.id,
        "name": guest.name if guest is not None and guest.invite_id == invite.id else None,
    }


def join(db: Session, request: Request, code: str, raw_name: Any) -> tuple[Guest, str | None, datetime]:
    """A new guest of the invitation, or the one this browser already is. Returns the guest, the token for the cookie
    (None when the browser keeps the one it has) and when the cookie ends.

    The new guest goes in as the first statement of its own transaction (SQLite's write lock), and only then are the
    guests counted: ten at once never pass ``MAX_GUESTS`` together. Two guests never get the same name (``name_key``,
    a unique index per invitation): the second of two at once takes the next number."""
    invite, playlist = find(db, request, code, None)
    present = _cookie_guest(db, request)
    if present is not None and present.invite_id == invite.id:
        if present.removed_at is not None:
            raise error("guest_removed", "The host has taken you out of this playlist.", 403)
        assert invite.expires_at is not None
        return present, None, invite.expires_at
    name = clean_name(raw_name)
    if name is None:
        raise error("bad_name", f"A name is 1 to {NAME_LENGTH} visible characters long, without control characters.",
                    422, fields=["name"], max=NAME_LENGTH)
    invite_id, playlist_id, ends = invite.id, playlist.id, invite.expires_at
    assert ends is not None
    senders = [(f"i:{invite_id}|{key}", JOIN_CODE.limit if key.startswith("proxy:") else JOIN_SENDER_LIMIT)
               for key, _limit in _miss_keys(request)]
    wait = max(JOIN_SENDER.wait(key, limit) for key, limit in senders)
    if wait:
        raise _slow(wait)
    JOIN_CODE.take("i:" + str(invite_id))
    for key, limit in senders:
        JOIN_SENDER.note(key, limit)
    taken = set(db.scalars(select(Guest.name_key).where(Guest.invite_id == invite_id)))
    token = secrets.token_urlsafe(32)
    for candidate in _candidates(name):
        key = name_key(candidate)
        if key in taken:
            continue
        # A fresh transaction whose first statement writes: it waits for the lock instead of failing on a stale read.
        db.rollback()
        guest = Guest(invite_id=invite_id, name=candidate, name_key=key,
                      token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(), joined_at=clock.now())
        db.add(guest)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            taken.add(key)
            continue
        break
    count = int(db.scalar(select(func.count()).select_from(Guest)
                          .where(Guest.invite_id == invite_id, Guest.removed_at.is_(None))) or 0)
    if count > MAX_GUESTS:
        db.rollback()
        raise error("invite_full", "This invitation has as many guests as it takes.", 409, max=MAX_GUESTS)
    db.commit()
    logger.info("Guest joined playlist=%s guest=%s", playlist_id, guest.id)
    _publish_guests(db, playlist_id)
    return guest, token, ends


def join_cover(db: Session, request: Request, code: str, album_id: int) -> None:
    """The covers on the page of the link: only those of the playlist's mosaic, only with a running invitation. They
    count against a brake of their own, not against reading the page or joining."""
    _invite, playlist = find(db, request, code, DOOR_COVER)
    summary = playlists.summary(db, playlists.live_items(db, playlist.id), server_id=playlist.source_server_id)
    if album_id not in summary["covers"]:
        raise error("not_found", "Not found.", 404)


# --- The guest of a request ------------------------------------------------------------------------------------------


@dataclass
class Caller:
    guest: Guest
    invite: GuestInvite
    playlist: Playlist


def caller(db: Session, request: Request) -> Caller:
    """The guest the cookie names, its running invitation and the playlist; 401 without a cookie that names one, 403
    ``guest_removed`` or ``invite_ended`` when it is out."""
    guest = _cookie_guest(db, request)
    if guest is None:
        raise error("guest_required", "Open the link of the invitation first.", 401)
    return _check(db, guest)


def _check(db: Session, guest: Guest) -> Caller:
    if guest.removed_at is not None:
        raise error("guest_removed", "The host has taken you out of this playlist.", 403)
    invite = db.get(GuestInvite, guest.invite_id, populate_existing=True)
    playlist = db.get(Playlist, invite.playlist_id, populate_existing=True) if invite is not None else None
    if (invite is None or playlist is None or playlist.deleted_at is not None or not running(invite)
            or not inviter_may(db, invite, playlist)):
        raise error("invite_ended", "This invitation has ended.", 403)
    return Caller(guest, invite, playlist)


def still_in(guest_id: int) -> bool:
    """For the live stream: whether the guest is still in."""
    from ..db import SessionLocal

    with SessionLocal() as db:
        guest = db.get(Guest, guest_id)
        if guest is None:
            return False
        try:
            _check(db, guest)
        except Exception:  # noqa: BLE001 - any refusal means out
            return False
    return True


def own_entries(db: Session, guest_id: int) -> set[int]:
    return set(db.scalars(select(GuestEntry.item_id).where(GuestEntry.guest_id == guest_id)))


def _count(db: Session, guest_id: int) -> int:
    return _contributions(db, [guest_id]).get(guest_id, 0)


def _guest_entries(db: Session, item_ids: list[int]) -> set[int]:
    """Which of these entries a guest added (of an invitation not yet tidied up)."""
    found: set[int] = set()
    for start in range(0, len(item_ids), 500):
        found.update(db.scalars(select(GuestEntry.item_id).where(GuestEntry.item_id.in_(item_ids[start:start + 500]))))
    return found


def _by(item: PlaylistItem, people: dict[int, Account], by_guests: set[int]) -> str:
    """The name a guest sees at an entry: a guest's name as it typed it; a person of the household by the display name
    only; anything else (an account since deleted, whose name on the entry may be the one to sign in with) empty."""
    if item.added_by_user is not None:
        return shown_name(people.get(item.added_by_user))
    return item.added_by_name if item.id in by_guests else ""


def view(db: Session, who: Caller) -> dict[str, Any]:
    """The playlist as the guest sees it: entries with the names on them (no account behind them), the inviter, what
    the guest may do, how much it added. Nothing of other playlists, accounts or settings."""
    from . import catalog_view

    items = playlists.live_items(db, who.playlist.id)
    server = who.playlist.source_server_id
    known = playlists.catalogue_of(db, (item.track_id for item in items), server_id=server)
    people = playlists.people_of(db, (item.added_by_user for item in items))
    mine = own_entries(db, who.guest.id)
    by_guests = _guest_entries(db, [item.id for item in items if item.added_by_user is None])
    summary = playlists.summary(db, items, known, people, server_id=server)
    entries = []
    for item in items:
        found = playlists.item_view(item, known.get(item.track_id), people)
        entries.append({
            "id": found["id"], "track": found["track"], "title": found["title"], "artist": found["artist"],
            "album": found["album"], "album_id": found["album_id"], "cover": found["cover"], "year": found["year"],
            "duration": found["duration"], "song": found["song"], "missing": found["missing"],
            "by": _by(item, people, by_guests), "mine": item.id in mine,
        })
    from . import catalog

    # The catalogue the guest searches is the one of the playlist's server.
    state = catalog.status(db, server) if server is not None else {"ready": False, "counts": {"tracks": 0}}
    tracks = int(state["counts"]["tracks"])
    return {
        "me": {"id": who.guest.id, "name": who.guest.name, "count": sum(1 for entry in entries if entry["mine"])},
        "invite": {"may_add": who.invite.may_add, "may_sort": who.invite.may_sort, "max": who.invite.max_tracks,
                   # Listening in needs the invitation's yes and the operator's switch.
                   "preview": who.invite.preview and preview.allowed(db),
                   "expires_at": who.invite.expires_at.isoformat() if who.invite.expires_at else None},
        "playlist": {"name": who.playlist.name, "inviter": _inviter(db, who.invite),
                     "version": who.playlist.version, "count": summary["count"], "duration": summary["duration"],
                     "covers": summary["covers"], "items": entries},
        # Where the guest's page searches: in the browser with the whole catalogue, or for a large one on the server.
        "catalog": {"ready": bool(state["ready"]), "search": catalog_view.search_mode(tracks)},
    }


# --- What a guest changes --------------------------------------------------------------------------------------------


def _begin(db: Session, guest_id: int) -> Caller:
    """The write lock first (the version of the playlist goes up), then the guest, its invitation and the playlist."""
    db.rollback()
    invite_id = select(Guest.invite_id).where(Guest.id == guest_id).scalar_subquery()
    playlist_id = select(GuestInvite.playlist_id).where(GuestInvite.id == invite_id).scalar_subquery()
    taken = db.execute(update(Playlist).where(Playlist.id == playlist_id)
                       .values(version=Playlist.version + 1, updated_at=clock.now())).rowcount
    if taken != 1:
        db.rollback()
        raise error("invite_ended", "This invitation has ended.", 403)
    guest = db.get(Guest, guest_id, populate_existing=True)
    try:
        if guest is None:
            raise error("guest_required", "Open the link of the invitation first.", 401)
        return _check(db, guest)
    except Exception:
        db.rollback()
        raise


def _nothing(db: Session, guest_id: int, **extra: Any) -> dict[str, Any]:
    db.rollback()
    return {"op": "none", **extra}


def add(db: Session, guest_id: int, tracks: list[int]) -> dict[str, Any]:
    """Tracks at the end, while the invitation lets the guest add and up to its limit (all or none); ones already in
    the playlist and ones the catalogue does not know are left out (``skipped``)."""
    wanted = list(dict.fromkeys(int(track) for track in tracks))[:MAX_ADD]
    who = _begin(db, guest_id)
    if not who.invite.may_add:
        db.rollback()
        raise error("guest_no_adding", "This invitation does not let guests add tracks.", 403)
    ordered = playlists.live_items(db, who.playlist.id)
    there = {item.track_id for item in ordered}
    known = playlists.catalogue_of(db, wanted, server_id=who.playlist.source_server_id)
    fresh = [track for track in wanted if track in known and track not in there]
    skipped = [track for track in wanted if track not in fresh]
    if not fresh:
        return _nothing(db, guest_id, skipped=skipped)
    have = _count(db, guest_id)
    if have + len(fresh) > who.invite.max_tracks:
        db.rollback()
        raise error("guest_limit", "You have added as many tracks as this invitation allows.", 409,
                    max=who.invite.max_tracks, count=have)
    if len(ordered) + len(fresh) > playlists.MAX_ITEMS:
        db.rollback()
        raise error("playlist_full", "A playlist holds at most 5000 tracks.", 409, max=playlists.MAX_ITEMS)
    now = clock.now()
    new = []
    for track in fresh:
        found = known[track]
        item = PlaylistItem(playlist_id=who.playlist.id, track_id=track, position=len(ordered) + len(new),
                            added_by_user=None, added_by_name=who.guest.name, added_at=now, title=found.title[:500],
                            artist=found.artist[:500], album=found.album[:500], album_id=found.album_id,
                            year=found.year, duration=found.duration, guid=found.guid[:200])
        db.add(item)
        new.append(item)
    try:
        db.flush()
        for item in new:
            db.add(GuestEntry(item_id=item.id, guest_id=guest_id))
        db.flush()
    except IntegrityError:
        db.rollback()
        raise error("busy", "The server is busy. Try again in a moment.", 503) from None
    change = playlists._changed(db, who.guest, who.playlist, "add", ordered + new, new, skipped=skipped)
    return {"op": "add", "version": change["version"], "added": [item.id for item in new], "skipped": skipped}


def _may_touch(who: Caller, mine: set[int], entries: list[int]) -> None:
    if who.invite.may_sort:
        return
    if any(entry not in mine for entry in entries):
        raise error("guest_not_yours", "Without the right to sort, a guest takes out only its own tracks.", 403)


def remove(db: Session, guest_id: int, entries: list[int]) -> dict[str, Any]:
    """Takes entries out: its own always, those of others with the right to sort. Ones already gone are no error."""
    wanted = {int(entry) for entry in entries}
    who = _begin(db, guest_id)
    ordered = playlists.live_items(db, who.playlist.id)
    going = [item for item in ordered if item.id in wanted]
    if not going:
        return _nothing(db, guest_id, removed=[])
    try:
        _may_touch(who, own_entries(db, guest_id), [item.id for item in going])
    except Exception:
        db.rollback()
        raise
    now = clock.now()
    for item in going:
        item.removed_at = now
    kept = [item for item in ordered if item.id not in wanted]
    db.flush()
    playlists.reorder(db, kept)
    change = playlists._changed(db, who.guest, who.playlist, "remove", kept, [],
                                removed=[item.id for item in going])
    return {"op": "remove", "version": change["version"], "removed": change["removed"]}


def bring_back(db: Session, guest_id: int, entries: list[int], after: int | None) -> dict[str, Any]:
    """The undo of taking out: the same rule as taking out, and the guest's own entries count against its limit."""
    wanted = [int(entry) for entry in dict.fromkeys(entries)]
    who = _begin(db, guest_id)
    if not who.invite.may_add:
        # Bringing back is adding again: without that right a guest takes out, it does not put in.
        db.rollback()
        raise error("guest_no_adding", "This invitation does not let guests add tracks.", 403)
    mine = own_entries(db, guest_id)
    try:
        _may_touch(who, mine, wanted)
    except Exception:
        db.rollback()
        raise
    ordered = playlists.live_items(db, who.playlist.id)
    there = {item.track_id for item in ordered}
    found = {item.id: item for item in db.scalars(
        select(PlaylistItem).where(PlaylistItem.playlist_id == who.playlist.id, PlaylistItem.id.in_(wanted),
                                   PlaylistItem.removed_at.is_not(None)).execution_options(populate_existing=True))}
    back: list[PlaylistItem] = []
    for entry in wanted:
        item = found.get(entry)
        if item is not None and item.track_id not in there:
            back.append(item)
            there.add(item.track_id)
    if not back:
        return _nothing(db, guest_id)
    own_back = sum(1 for item in back if item.id in mine)
    have = _count(db, guest_id)
    if own_back and have + own_back > who.invite.max_tracks:
        db.rollback()
        raise error("guest_limit", "You have added as many tracks as this invitation allows.", 409,
                    max=who.invite.max_tracks, count=have)
    ids = [item.id for item in ordered]
    if after is None:
        at = 0
    elif after in ids:
        at = ids.index(after) + 1
    else:
        at = min(back[0].position, len(ordered))
    for item in back:
        item.removed_at = None
    db.flush()
    result = ordered[:at] + back + ordered[at:]
    playlists.reorder(db, result)
    change = playlists._changed(db, who.guest, who.playlist, "restore", result, back)
    return {"op": "restore", "version": change["version"], "restored": [item.id for item in back]}


def move(db: Session, guest_id: int, entry: int, after: int | None) -> dict[str, Any]:
    """Moves an entry, with the right to sort only."""
    who = _begin(db, guest_id)
    if not who.invite.may_sort:
        db.rollback()
        raise error("guest_no_sorting", "This invitation does not let guests sort.", 403)
    ordered = playlists.live_items(db, who.playlist.id)
    ids = [item.id for item in ordered]
    if entry not in ids or (after is not None and after not in ids) or after == entry:
        return _nothing(db, guest_id, moved=False)
    item = ordered[ids.index(entry)]
    rest = [one for one in ordered if one.id != entry]
    at = 0 if after is None else [one.id for one in rest].index(after) + 1
    result = rest[:at] + [item] + rest[at:]
    if [one.id for one in result] == ids:
        return _nothing(db, guest_id, moved=False)
    playlists.reorder(db, result)
    change = playlists._changed(db, who.guest, who.playlist, "move", result, [], moved=True, entry=entry, after=after)
    return {"op": "move", "version": change["version"], "moved": True}


def leave(db: Session, guest_id: int) -> None:
    """The guest leaves by itself: like being taken out, without a host."""
    guest = db.get(Guest, guest_id)
    if guest is not None and guest.removed_at is None:
        guest.removed_at = clock.now()
        db.commit()
        invite = db.get(GuestInvite, guest.invite_id)
        if invite is not None:
            _publish_guests(db, invite.playlist_id)


# --- Listening to a part of a track -----------------------------------------------------------------------------------


def _as_guest(db: Session, request: Request) -> bool:
    """Whether a request listens in as a guest: it has the guest cookie and no account's credentials (no device token,
    no session that is one). A browser signed in as an account listens as that account, guest cookie or not; a guest
    is never sent to the account's way, where its missing session would count as a wrong guess at the door."""
    from ..deps import bearer_token
    from ..security import SESSION_COOKIE, session_account

    if not request.cookies.get(GUEST_COOKIE) or bearer_token(request) is not None:
        return False
    return session_account(db, request.cookies.get(SESSION_COOKIE)) is None


def preview_listener(request: Request, db: Session, track_id: int | None) -> preview.Listener:
    """``preview.may_listen`` with guests: a guest of a running invitation that allows listening listens as itself
    (``guest:<id>``, its own limits); a guest taken out or of an ended invitation hears 403 ``guest_removed`` or
    ``invite_ended``, one whose invitation does not allow it 403 ``preview_off``. Every other request goes the account's
    way. The operator's switch and the track in the catalogue are checked after it (``preview.prepare``)."""
    if not _as_guest(db, request):
        return preview.account_listener(request, db, track_id)
    guest = _cookie_guest(db, request)
    if guest is None:
        raise error("guest_required", "Open the link of the invitation first.", 401)
    who = _check(db, guest)
    if not who.invite.preview:
        raise error("preview_off", "Listening in is switched off for this invitation.", 403)
    # A guest hears tracks of its playlist's server only (0: none connected, so none).
    return preview.Listener(f"guest:{who.guest.id}", "guest", who.playlist.source_server_id or 0)


def hang_in() -> None:
    """Guests listen in through the preview route (``preview.may_listen``)."""
    preview.may_listen = preview_listener


hang_in()


# --- Tidying up ------------------------------------------------------------------------------------------------------


def purge(db: Session, now: datetime | None = None) -> int:
    """Invitations that ran out end; ended ones go with their guests and cookies (the names stay on the entries); ones
    never started go after ``WAITING_DAYS``."""
    moment = now or clock.now()
    ran_out = list(db.scalars(select(GuestInvite).where(GuestInvite.ended_at.is_(None),
                                                         GuestInvite.expires_at.is_not(None),
                                                         GuestInvite.expires_at <= moment)))
    for invite in ran_out:
        invite.ended_at = invite.expires_at
    db.flush()
    gone = db.execute(delete(GuestInvite).where(
        (GuestInvite.ended_at.is_not(None))
        | (GuestInvite.started_at.is_(None) & (GuestInvite.created_at <= moment - timedelta(days=WAITING_DAYS)))
    )).rowcount
    db.commit()
    for invite in ran_out:
        _publish_invite(invite.id, "ended")
    if gone:
        logger.info("Guest invitations tidied up count=%s", gone)
    return int(gone or 0)
