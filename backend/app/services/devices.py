"""Devices: a phone, a later native app or a script speaks for an account with a token of its own.

The token travels in ``Authorization: Bearer`` next to the session cookie of a browser (``deps.require_account``). It
is made from a signed-in browser only (a device cannot pair another one), shown once, and stored as its SHA-256. A
device acts with the rights of its account, no more. It stops working when the account is blocked or
deleted, and when it is revoked; the second factor was met in the browser that made the token, so the device
does not ask for it again.

**The token.** ``nmd_`` and 43 random characters. It never reaches the log. A wrong one counts like a wrong password
for the sender (the brake in ``deps``).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import unicodedata
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..errors import error
from ..models import Account, Device, utcnow

logger = logging.getLogger("nexmix.auth")

TOKEN_PREFIX = "nmd_"
#: How many devices an account may hold (revoked ones do not count).
MAX_DEVICES = 30
NAME_MAX = 80
#: ``last_used_at`` is written at most this often.
USED_EVERY = timedelta(minutes=1)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


#: How many marks that sit on the letter before them (accents, and what is stacked into a streak) may follow each other.
#: "e" and two accents is a written letter somewhere; ten of them draw a line over the page.
MARKS_IN_A_ROW = 3
#: Characters that fill space and draw nothing, and that no category tells from a letter: the Hangul fillers, the blank
#: Braille cell, the Khmer inherent vowels.
_BLANK = frozenset("\u115f\u1160\u17b4\u17b5\u2800\u3164\uffa0")


def _draws(char: str) -> bool:
    """Whether a person sees something where this character stands: not a space, a control or format character, a mark
    that needs a letter before it, or one of the blanks."""
    return unicodedata.category(char)[0] not in "ZCM" and char not in _BLANK


def clean_name(value: str) -> str:
    """Spaces gathered, no control or invisible characters, at most ``NAME_MAX``; never empty. A name is text from
    outside and is shown to a person who decides on it: it must show something (not only blanks), and not draw outside
    its line (at most ``MARKS_IN_A_ROW`` stacked marks)."""
    if any(ord(char) < 32 or ord(char) == 127 or unicodedata.category(char) == "Cf" for char in value):
        raise error("device_name_invalid", "A device name cannot hold control or invisible characters.", 422)
    clean = " ".join(value.split())
    if not clean or len(clean) > NAME_MAX:
        raise error("device_name_invalid", f"Use 1 to {NAME_MAX} characters.", 422)
    marks = 0
    for char in clean:
        marks = marks + 1 if unicodedata.category(char) in ("Mn", "Me") else 0
        if marks > MARKS_IN_A_ROW:
            raise error("device_name_invalid", "A device name cannot stack more than three marks on a letter.", 422)
    if not any(_draws(char) for char in clean):
        raise error("device_name_invalid", "A device name must show at least one letter or digit.", 422)
    return clean


def add(db: Session, account_id: int, name: str) -> tuple[Device, str]:
    """A new device in the open transaction, not yet committed. The token is returned once and never stored."""
    name = clean_name(name)
    live = db.scalars(select(Device.id).where(Device.account_id == account_id, Device.revoked_at.is_(None))).all()
    if len(live) >= MAX_DEVICES:
        raise error("too_many_devices", f"An account holds at most {MAX_DEVICES} devices.", 409)
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = Device(account_id=account_id, name=name, token_hash=digest(token),
                 prefix=token[: len(TOKEN_PREFIX) + 4], created_at=utcnow())
    db.add(row)
    db.flush()
    return row, token


def make(db: Session, account: Account, name: str) -> tuple[Device, str]:
    """A new device; the token is returned once and never stored."""
    row, token = add(db, account.id, name)
    db.commit()
    db.refresh(row)
    logger.info("Device made device_id=%s", row.uid)
    return row, token


def listing(db: Session, account_id: int) -> list[Device]:
    """The live devices of an account, the most recently made first."""
    return list(db.scalars(select(Device).where(Device.account_id == account_id, Device.revoked_at.is_(None))
                           .order_by(Device.created_at.desc(), Device.id.desc())))


def revoke(db: Session, account_id: int, uid: str) -> bool:
    """Ends one device of the account. False when there is none (another account's looks the same)."""
    row = db.scalar(select(Device).where(Device.uid == uid, Device.account_id == account_id,
                                         Device.revoked_at.is_(None)))
    if row is None:
        return False
    row.revoked_at = clock.now()
    db.commit()
    logger.info("Device revoked device_id=%s", uid)
    return True


def authenticate(db: Session, token: str | None) -> tuple[Device, Account] | None:
    """The device and its account for a token, or None: no such token, revoked, or the account blocked.
    Whether a second factor is still to be set up is the caller's to check."""
    if not token or not token.startswith(TOKEN_PREFIX) or len(token) > 200:
        return None
    row = db.scalar(select(Device).where(Device.token_hash == digest(token)))
    if row is None or row.revoked_at is not None:
        return None
    account = db.get(Account, row.account_id)
    now = clock.now()
    # The lock after wrong passwords is for the password only: a token is a secret of its own, and anyone who knows a
    # name could otherwise cut off every device of the account. A block by the operator stops it all.
    if account is None or account.blocked_at is not None:
        return None
    if row.last_used_at is None or now - row.last_used_at >= USED_EVERY:
        row.last_used_at = now
        db.commit()
    db.expunge(row)
    return row, account
