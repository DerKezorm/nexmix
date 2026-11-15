"""Operator settings in the database, with defaults."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Setting

DEFAULTS: dict[str, Any] = {
    "log_mode": "normal",
    "log_mode_until": None,
    #: off | daily | weekly. Daily in the night unless the operator decides otherwise.
    "backup_schedule": "daily",
    "backup_keep": 7,
    #: The address people use to reach nexmix: invitation links, the redirect address of the sign-in providers.
    #: Empty means ``NEXMIX_PUBLIC_URL``, and without that the request's own.
    "public_url": "",
    #: Members may sign in with a password. The operator always may: the emergency exit when OIDC fails.
    "password_login": True,
    #: Every account needs a second factor (a code from an app or a passkey): right after the password, an account
    #: without one sets it up before it gets anywhere. Off from the start, so the first account reaches the overview
    #: straight away; "Ready for the internet?" recommends it for a server reachable from outside. A value the operator
    #: saved stays as it is. Turning it off asks for the password.
    "two_factor_required": False,
    #: Keys for programs (``/api/v1``): closed until the operator opens it.
    "api_keys_allowed": False,
    #: Ask GitHub once a day whether a newer nexmix is out (services/updates). Only the question goes out, so on.
    "update_check": True,
    #: Invitation mail: without a host nothing is sent, the link to copy is enough.
    "smtp_host": "",
    "smtp_port": 587,
    #: starttls | tls | none
    "smtp_security": "starttls",
    "smtp_user": "",
    "smtp_password_enc": "",
    "smtp_from": "",
    #: Web Push (``services/webpush.py``, ``services/push.py``): nexmix's own key pair, made at the first need and
    #: sealed with the server's secret; who the push services may contact (empty: the public https address, else a
    #: placeholder); and push services beyond the known ones, as host names.
    "push_key_enc": "",
    "push_contact": "",
    "push_hosts": [],
    #: The identifier nexmix gives itself at Plex, made at the first sign-in (``services/plex_link.py``).
    "plex_client_id": "",
    #: The part of nexmix's device id at Jellyfin that is this installation's own (``services/jellyfin_link.py``).
    "jellyfin_device": "",
    #: When the whole catalogue is fetched again each night, on the server's clock (``HH:MM``); empty: never.
    "catalog_full_at": "03:30",
    #: Listening in: a short piece of a track, played through nexmix (``services/preview.py``). On from the start.
    "preview_allowed": True,
    #: What a new invitation for guests starts with (``services/guests.py``); each invitation may say otherwise.
    "guest_preview": True,
    "guest_max": 10,
    "guest_hours": 24,
}


def get(db: Session, key: str) -> Any:
    row = db.get(Setting, key)
    if row is None:
        return DEFAULTS.get(key)
    return row.value


def get_all(db: Session) -> dict[str, Any]:
    values = dict(DEFAULTS)
    for row in db.scalars(select(Setting)):
        values[row.key] = row.value
    return values


def save(db: Session, changes: dict[str, Any], *, commit: bool = True) -> None:
    """Writes the settings; ``commit=False`` leaves them in the transaction of the caller. Safe to run at the same
    moment as another writer of the same key."""
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise KeyError(key)
        row = db.get(Setting, key)
        if row is None:
            # A key that is not there yet: two writers at the same moment (two starts on an empty folder, two
            # presses on "save") both see it missing, so the insert itself decides, and the later one wins.
            db.execute(sqlite_insert(Setting).values(key=key, value=value)
                       .on_conflict_do_update(index_elements=[Setting.key], set_={"value": value}))
        else:
            row.value = value
    if commit:
        db.commit()
    else:
        db.flush()


def normalize_public_url(value: str) -> str:
    """``scheme://host[:port]`` and nothing else, or empty. ``ValueError`` for anything people could not type into
    a browser: no other schemes, no credentials, no path, no query."""
    text = value.strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("scheme or host")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError("path, query or credentials")
    try:
        parts.port  # noqa: B018 - raises ValueError when the port is not a number
    except ValueError as exc:
        raise ValueError("port") from exc
    return f"{parts.scheme.lower()}://{parts.netloc}"


def public_url(db: Session) -> str:
    """The public address without a trailing slash: the setting, otherwise the environment, otherwise empty."""
    stored = str(get(db, "public_url") or "").strip().rstrip("/")
    return stored or get_settings().public_url.strip().rstrip("/")
