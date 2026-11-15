"""The data model.

Everything lives in SQLite. The schema carries a version number (``db.SCHEMA_VERSION``); a change to a table here
comes with a migration step in ``db.MIGRATIONS`` and a new recorded schema in ``tests/schema/``.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    DDL,
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """SQLite forgets the time zone. Stored as UTC, read back as UTC with the zone attached."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)


OPERATOR = "operator"
MEMBER = "member"
ROLES = (OPERATOR, MEMBER)

SIGN_IN_PASSWORD = "password"
SIGN_IN_OIDC = "oidc"


class Account(Base):
    """A person. The first account is the operator; the others come by invitation or through OIDC.

    The operator manages the server and its accounts."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Lower case, the name people sign in with. What people see is the display name, when there is one.
    name: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(80), default="")
    #: The version whose "What's new" the account has read or put away.
    whats_new_seen: Mapped[str] = mapped_column(String(32), default="")
    role: Mapped[str] = mapped_column(String(16), default=MEMBER)
    sign_in: Mapped[str] = mapped_column(String(16), default=SIGN_IN_PASSWORD)
    #: Argon2id. Empty for accounts that sign in through OIDC only.
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    #: When the address was confirmed by a link sent to it (or came from the provider); empty: one the operator typed.
    email_confirmed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: The address came from a sign-in provider (``services/oidc_store.py``): it goes with the last link, and it is
    #: never confirmed by that alone.
    email_from_provider: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    #: The interface language; empty: the browser's.
    language: Mapped[str] = mapped_column(String(16), default="")
    #: What the person chose for themselves (``routers/auth.py``, ``PROFILE``): only what it chose is kept.
    profile: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: Set when the operator blocked the account: no sign-in, sessions and tokens end at once.
    blocked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: The second factor (``services/totp.py``): the seed encrypted with the server secret, empty while off; the
    #: recovery codes as a JSON list of keyed hashes; the time step of the last code taken (no replay).
    totp_secret_enc: Mapped[str] = mapped_column(Text, default="")
    totp_recovery: Mapped[str] = mapped_column(Text, default="")
    totp_last_step: Mapped[int] = mapped_column(Integer, default=0)
    #: The profile picture (``services/avatars.py``): a square WebP drawn anew, loaded only when asked for.
    avatar: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    avatar_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: Whether the account ever signed in: its very first sign-in is no "new device" (``services/notices.py``).
    signed_in_before: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    #: The user handle its passkeys carry (``services/passkeys.py``): random, the same for all of its passkeys, so a
    #: passkey names its account without its name or id. Hex; empty until the first passkey.
    passkey_handle: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    #: Not a column: whether the session that asks came in through a provider that checks the second factor itself
    #: (``AuthSession.second_factor_by_provider``), set by ``deps.require_account`` for ``totp.setup_required``.
    second_factor_by_provider: ClassVar[bool] = False


#: What a session may do (``deps.require_account``): everything; only set up the second factor right after signing in;
#: only confirm that the recovery codes are kept. The last two never become ``full`` themselves: a new session does.
STAGE_FULL = "full"
STAGE_SETUP = "setup"
STAGE_CODES = "codes"


def _session_uid() -> str:
    return secrets.token_hex(16)


class AuthSession(Base):
    """A browser session. Only the hash of the token is stored; the token itself lives in the cookie.

    ``uid`` names the session in the list of signed-in devices (never the token or its hash). ``remember``: the
    person ticked "stay signed in on this device", the session lasts until it was not used for ``session_days``;
    without, it ends with the browser and after twelve hours at the latest."""

    __tablename__ = "auth_sessions"
    __table_args__ = (Index("uq_auth_sessions_uid", "uid", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    uid: Mapped[str] = mapped_column(String(32), default=_session_uid, server_default=text("''"))
    remember: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    stage: Mapped[str] = mapped_column(String(8), default=STAGE_FULL, server_default=text("'full'"))
    #: The session came in through a provider whose entry checks the second factor itself: "require a second factor"
    #: does not send it to set one up; the same account signing in with its password still is.
    second_factor_by_provider: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))


class OidcProvider(Base):
    """A sign-in provider (OpenID Connect), one row each, the columns as the shared module names them.
    Read and written only through ``services/oidc_store.py`` for the shared module ``vendor/nexoidc``.

    Not part of a numbered schema step: an addition (``db.ADDITIONS``) that 1.0.1 opens without knowing it, so that
    the way back to 1.0.1 stays open for one version."""

    __tablename__ = "oidc_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Part of the callback address ``/api/v1/oidc/<slug>/callback``; fixed once made.
    slug: Mapped[str] = mapped_column(String(40), unique=True)
    #: The name on the sign-in button.
    label: Mapped[str] = mapped_column(String(64))
    #: Stored without blanks and without a slash at the end.
    issuer: Mapped[str] = mapped_column(String(500))
    client_id: Mapped[str] = mapped_column(String(255))
    #: Encrypted with the server secret under the context ``oidc-provider:<id>``; empty for a public client.
    client_secret_enc: Mapped[str] = mapped_column(Text, default="")
    scopes: Mapped[str] = mapped_column(String(500), default="openid profile email")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    #: "New people get an account": off from the start.
    auto_create: Mapped[bool] = mapped_column(Boolean, default=False)
    #: "The provider checks the second factor itself": on from the start.
    trusts_second_factor: Mapped[bool] = mapped_column(Boolean, default=True)
    #: "" by hand, "authentik" made by the authentik button.
    managed: Mapped[str] = mapped_column(String(20), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    #: A callback path the provider knew before (only a migration sets it); "" is the standard one. nexmix has no
    #: older version, so it stays empty.
    redirect_path: Mapped[str] = mapped_column(String(300), default="")
    #: The redirect address the authentik button last registered at authentik for this entry, "" for none.
    registered_redirect: Mapped[str] = mapped_column(String(500), default="", server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class OidcLink(Base):
    """One identity at one provider, bound to one account. Found only by ``(provider_id, subject)``, never by an
    address: ``email`` is for display."""

    __tablename__ = "oidc_links"
    __table_args__ = (
        UniqueConstraint("provider_id", "subject"),
        UniqueConstraint("provider_id", "account_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("oidc_providers.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(255))
    account_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    #: The ``iss`` of the token that made the link (with Entra ``common`` the real tenant).
    issuer: Mapped[str] = mapped_column(String(500))
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class Passkey(Base):
    """A passkey of an account (WebAuthn, ``services/passkeys.py``): signs in without password and code, and counts
    as a second factor. Only the public key is kept; ``sign_count`` is the authenticator's counter as last seen."""

    __tablename__ = "passkeys"
    __table_args__ = (
        Index("uq_passkeys_uid", "uid", unique=True),
        Index("uq_passkeys_credential", "credential_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    credential_id: Mapped[bytes] = mapped_column(LargeBinary)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class Invite(Base):
    """A link that brings somebody in with a new account. Only the hash of the token is stored; used once, then gone."""

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)


class PasswordReset(Base):
    """A link with which a person sets a new password (``services/resets.py``). Only the hash of the token is stored;
    used once, then gone. At most one per account: a new one replaces the one before."""

    __tablename__ = "password_resets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    #: The operator who sent it; empty when the person asked for it themselves ("Forgot your password?").
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)


class EmailConfirmation(Base):
    """A new mail address waiting for the link sent to it (``services/emails.py``). Only the hash of the token is
    stored; used once, then gone. At most one per account: a new one replaces the one before."""

    __tablename__ = "email_confirmations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    address: Mapped[str] = mapped_column(String(255))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)


class ApiKey(Base):
    """A key an account made for programs (``/api/v1``). Only the SHA-256 is stored.

    ``scope`` is what the key may do: ``read``."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    scope: Mapped[str] = mapped_column(String(8))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    blocked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class Device(Base):
    """A device or app of an account (``services/devices.py``): it speaks for the account with a token in
    ``Authorization: Bearer``, next to the session cookie of a browser. Only the SHA-256 of the token is stored.
    Revoking sets ``revoked_at``; the row stays for the list."""

    __tablename__ = "devices"
    __table_args__ = (Index("uq_devices_uid", "uid", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String(32), default=_session_uid)
    account_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


# --- Web Push ----------------------------------------------------------------------------------------


class PushDevice(Base):
    """A browser or an app on the home screen that gets notices (``services/push.py``). What the browser handed
    over (the address at its push service and the two keys) and the name of the device are sealed with the server
    secret: the address is a key in itself, whoever has it may send to the device. ``endpoint_key`` is a keyed
    hash of the address, so that the same browser signing up twice stays one device."""

    __tablename__ = "push_devices"
    __table_args__ = (
        UniqueConstraint("uid", name="uq_push_devices_uid"),
        UniqueConstraint("user_id", "endpoint_key", name="uq_push_devices_user_endpoint"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint_key: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    #: The last message the push service took for this device.
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class Job(Base):
    """Work for the background, kept in the database so that it survives a restart (``services/jobs.py``). At most one
    job of a kind is waiting for the same thing (``ref``)."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_state", "state", "id"),
        Index("uq_jobs_waiting", "kind", "ref", unique=True, sqlite_where=text("state = 'queued'")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    #: What the job is about (a number the kind of job gives), to find it and to keep two waiting for the same.
    ref: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[Any] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(String(8), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    error: Mapped[str] = mapped_column(String(300), default="", server_default=text("''"))


# --- Media servers and the catalogue ---------------------------------------------------------------------------------

#: The kinds of media server nexmix speaks with (``services/sources``).
SERVER_PLEX = "plex"
SERVER_JELLYFIN = "jellyfin"
SERVER_KINDS = (SERVER_PLEX, SERVER_JELLYFIN)


class MediaServer(Base):
    """A media server the playlists live on (``services/media.py``), one row per server: Plex today, others later. The
    token is sealed with the server secret (context ``plex`` for a Plex server: the context names the kind of secret)
    and never leaves the server.

    A server let go of stays as a row without its token (``active`` false): the numbers its tracks were given
    (``server_items``) and the copies of the playlists on it (``server_playlists``) wait there, and connected again it
    takes them up instead of making everything anew.

    ``serial`` changes whenever the server's address, token or library changes: a catalogue run that began under
    another serial is thrown away instead of switched on. ``generation`` names the catalogue rows that are live; a full
    run writes the next one and switches over in one step, so that nobody ever sees half a catalogue."""

    __tablename__ = "media_servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: ``plex`` or ``jellyfin`` (``SERVER_KINDS``).
    kind: Mapped[str] = mapped_column(String(16), default=SERVER_PLEX)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    serial: Mapped[int] = mapped_column(Integer, default=1)
    #: How it was connected: ``plextv`` (signed in with a PIN) or ``manual`` (address and token typed).
    via: Mapped[str] = mapped_column(String(8), default="")
    name: Mapped[str] = mapped_column(String(200), default="")
    #: The server's own id (Plex's ``machineIdentifier``): the same server is found again by it.
    machine_id: Mapped[str] = mapped_column(String(100), default="")
    owned: Mapped[bool] = mapped_column(Boolean, default=True)
    url: Mapped[str] = mapped_column(String(500), default="")
    token_enc: Mapped[str] = mapped_column(Text, default="")
    section_key: Mapped[str] = mapped_column(String(16), default="")
    section_title: Mapped[str] = mapped_column(String(200), default="")
    connected_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    #: The last run that went through (full or partial), and the last full one.
    synced_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    full_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: The highest ``updatedAt``/``addedAt`` seen per Plex type (``"8"``, ``"9"``, ``"10"``), on Plex's own clock.
    marks: Mapped[Any] = mapped_column(JSON, nullable=True)
    #: Sonic analysis: the library's setting as Plex reports it (None: unknown), and the last sample of tracks.
    analysis_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    analysis_sampled: Mapped[int] = mapped_column(Integer, default=0)
    analysis_found: Mapped[int] = mapped_column(Integer, default=0)
    analysis_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: Jellyfin: the user the playlists will belong to there (its id and its name as it was chosen).
    user_id: Mapped[str] = mapped_column(String(64), default="", server_default="")
    user_name: Mapped[str] = mapped_column(String(200), default="", server_default="")


class ServerItem(Base):
    """The number nexmix gives a track, album or artist of a server: servers name their things in their own way (Plex
    by numbers, others by text), and two servers may use the same name for different things. Every catalogue row, every
    entry of a playlist and every common state of a sync holds these numbers. A number is never given twice, not even
    after its row is gone (``AUTOINCREMENT``); the things of the Plex server connected when nexmix learnt about several
    servers kept their ``ratingKey`` as their number."""

    __tablename__ = "server_items"
    __table_args__ = (
        UniqueConstraint("server_id", "kind", "ext_key", name="uq_server_items_key"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("media_servers.id", ondelete="CASCADE"))
    #: ``track``, ``album`` or ``artist``.
    kind: Mapped[str] = mapped_column(String(8))
    #: The server's own name for it (Plex: the ``ratingKey`` as text).
    ext_key: Mapped[str] = mapped_column(String(64))


class SyncRun(Base):
    """One run of the catalogue of one server (``services/catalog.py``): full or partial, how far it got, what it
    found, and why it failed (a code, never a text from the server)."""

    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_state", "state", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: ``full`` or ``partial``.
    kind: Mapped[str] = mapped_column(String(8))
    #: Why it ran: ``first``, ``manual``, ``nightly``, ``schedule``, ``many``, ``recount``.
    reason: Mapped[str] = mapped_column(String(16), default="")
    #: ``running``, ``done``, ``failed``, ``stopped``.
    state: Mapped[str] = mapped_column(String(8), default="running")
    serial: Mapped[int] = mapped_column(Integer, default=0)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    artists: Mapped[int] = mapped_column(Integer, default=0)
    albums: Mapped[int] = mapped_column(Integer, default=0)
    tracks: Mapped[int] = mapped_column(Integer, default=0)
    songs: Mapped[int] = mapped_column(Integer, default=0)
    #: A partial run: entries taken in, and entries gone from the server.
    changed: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    #: Where a running one stands: the step, and the share of the whole run done, in thousandths.
    step: Mapped[str] = mapped_column(String(16), default="")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(40), default="")
    #: Seconds per step, for the operator's curiosity and the measurement of large libraries.
    timings: Mapped[Any] = mapped_column(JSON, nullable=True)
    #: The server whose catalogue it fetched.
    server_id: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class CatalogArtist(Base):
    """An artist of a server's music library, by its number (``server_items``); ``generation`` keeps a catalogue being
    fetched apart from the live one. Times are the server's own (seconds, its clock)."""

    __tablename__ = "artists"

    server_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    thumb: Mapped[str] = mapped_column(String(300), default="")
    added_at: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[int] = mapped_column(Integer, default=0)


class CatalogAlbum(Base):
    __tablename__ = "albums"

    server_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artist_id: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(500), default="")
    #: The album's year as the server has it (a reissue carries its own).
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumb: Mapped[str] = mapped_column(String(300), default="")
    added_at: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[int] = mapped_column(Integer, default=0)


class CatalogTrack(Base):
    """A track. ``song_key`` puts the versions of one song together (``services/songs.py``); ``shown`` marks the
    version the search shows for it, ``variant`` one that is never shown first (live, karaoke ...); ``original_year``
    is the earliest album year among the versions."""

    __tablename__ = "tracks"
    __table_args__ = (
        Index("ix_tracks_song", "server_id", "generation", "song_key"),
        Index("ix_tracks_album", "server_id", "generation", "album_id"),
    )

    server_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    album_id: Mapped[int] = mapped_column(Integer, default=0)
    artist_id: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(500), default="")
    #: Who performs it, when the server names somebody (Plex's ``originalTitle``); empty: the album's artist.
    artist: Mapped[str] = mapped_column(String(500), default="")
    #: Milliseconds.
    duration: Mapped[int] = mapped_column(Integer, default=0)
    rating_count: Mapped[int] = mapped_column(Integer, default=0)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    added_at: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[int] = mapped_column(Integer, default=0)
    song_key: Mapped[str] = mapped_column(String(700), default="")
    variant: Mapped[bool] = mapped_column(Boolean, default=False)
    shown: Mapped[bool] = mapped_column(Boolean, default=True)
    original_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The server's ``guid`` of the track: one of Plex's agents (``plex://track/..``) names the same recording on every
    #: server of Plex.
    guid: Mapped[str] = mapped_column(String(200), default="", server_default="")
    #: The MusicBrainz ids the server knows for it: of the release's track (what Plex's ``mbid://`` names) and of the
    #: recording. Empty: none.
    mbid_track: Mapped[str] = mapped_column(String(36), default="", server_default="")
    mbid_recording: Mapped[str] = mapped_column(String(36), default="", server_default="")


class CatalogTag(Base):
    """A mood, genre or style, by its name as the server writes it."""

    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("server_id", "generation", "kind", "name", name="uq_tags_name"),)

    server_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: ``mood``, ``genre`` or ``style``.
    kind: Mapped[str] = mapped_column(String(8))
    name: Mapped[str] = mapped_column(String(200))


class TrackTag(Base):
    __tablename__ = "track_tags"

    server_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tag_id: Mapped[int] = mapped_column(Integer, primary_key=True)


class AlbumTag(Base):
    __tablename__ = "album_tags"

    server_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tag_id: Mapped[int] = mapped_column(Integer, primary_key=True)


class ArtistTag(Base):
    __tablename__ = "artist_tags"

    server_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tag_id: Mapped[int] = mapped_column(Integer, primary_key=True)


#: The search index of the tracks: each server's live tracks, each as one folded text (title, artists, album; see
#: ``services/search.fold``), cut in threes so that any part of a word of three letters or more is found. The row id is
#: the track's number (``server_items``), ``server_id`` the server it belongs to. Made with the tables
#: (``after_create``), kept by ``services/catalog_view.reindex``.
TRACK_SEARCH_DDL = "CREATE VIRTUAL TABLE track_search USING fts5(text, server_id UNINDEXED, tokenize = 'trigram')"
event.listen(Base.metadata, "after_create", DDL(TRACK_SEARCH_DDL))


# --- Playlists -------------------------------------------------------------------------------------------------------

#: What a share lets the other person do: see the playlist, or change it as well.
SHARE_READ = "read"
SHARE_EDIT = "edit"
SHARE_RIGHTS = (SHARE_READ, SHARE_EDIT)


class Playlist(Base):
    """A playlist. It belongs to the account that made it; members see their own and the ones shared with them, the
    operator sees all. ``version`` grows with every change of its entries or its name, so that a browser that missed an
    event notices and loads it again. A deleted one lies in the trash (``deleted_at``) for 30 days. An owner whose
    account is gone leaves the playlist to the operator (``owner_id`` empty)."""

    __tablename__ = "playlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
                                                 index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)
    #: The server the playlist takes its tracks from (``media_servers``): its catalogue is searched, its tracks are the
    #: entries. None while no server was connected yet.
    source_server_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class PlaylistItem(Base):
    """One entry of a playlist: a track of the catalogue (Plex's ``ratingKey``) at a place in the order. A track is in a
    playlist at most once (Plex refuses a second time); another version of the same song is another track.

    What the catalogue says about the track is kept beside it (title, artist, album, length), so that an entry whose
    track went from Plex still says what it was. An entry taken out stays a while as ``removed_at`` (the undo brings it
    back where it was, with the person who added it)."""

    __tablename__ = "playlist_items"
    __table_args__ = (
        Index("ix_playlist_items_order", "playlist_id", "position"),
        Index("uq_playlist_items_track", "playlist_id", "track_id", unique=True,
              sqlite_where=text("removed_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlists.id", ondelete="CASCADE"))
    track_id: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer, default=0)
    added_by_user: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    #: The name of who added it, as it was then: what is shown once the account is gone.
    added_by_name: Mapped[str] = mapped_column(String(80), default="")
    added_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    artist: Mapped[str] = mapped_column(String(500), default="")
    album: Mapped[str] = mapped_column(String(500), default="")
    album_id: Mapped[int] = mapped_column(Integer, default=0)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Milliseconds.
    duration: Mapped[int] = mapped_column(Integer, default=0)
    #: Put in through Plex (Plexamp, Plex Web), found by the sync with Plex: ``added_at`` is when nexmix saw it.
    from_plex: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    #: The track's ``guid`` in Plex when it was put in: with it the entry finds its track on another server.
    guid: Mapped[str] = mapped_column(String(200), default="", server_default="")
    #: Why the entry is in nexmix only (``services/plex_sync.py``): ``refused`` (Plex would not take its track) or
    #: ``elsewhere`` (the connected server has no such track); empty: nothing to say.
    plex_hint: Mapped[str] = mapped_column(String(16), default="", server_default="")


class PlaylistShare(Base):
    """A playlist shared with another account: to see it (``read``) or to change it as well (``edit``)."""

    __tablename__ = "playlist_shares"

    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlists.id", ondelete="CASCADE"), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    access: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class HiddenPlaylist(Base):
    """A playlist one account put out of its own view ("Hide"): its overview leaves it out, nothing else changes. Not
    in Plex, not for anybody else, not its rights; "Show again" deletes the row."""

    __tablename__ = "hidden_playlists"

    account_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlists.id", ondelete="CASCADE"), primary_key=True,
                                             index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


# --- Suggestions -----------------------------------------------------------------------------------------------------


class HiddenSuggestion(Base):
    """A song somebody asked not to be suggested any more ("not again"), by its song key (``services/songs.py``): the
    key outlives the track ids of a catalogue run, and every version of the song stays out. Per account; ``guest_id``
    is kept free for a guest of an invitation (no key to a table yet)."""

    __tablename__ = "hidden_suggestions"
    __table_args__ = (
        Index("uq_hidden_suggestions_account", "account_id", "song_key", unique=True,
              sqlite_where=text("account_id IS NOT NULL")),
        Index("uq_hidden_suggestions_guest", "guest_id", "song_key", unique=True,
              sqlite_where=text("guest_id IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    guest_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    song_key: Mapped[str] = mapped_column(String(700))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


# --- Guests ----------------------------------------------------------------------------------------------------------


class GuestInvite(Base):
    """An invitation to one playlist for guests without an account (``services/guests.py``): a code for the link and the
    QR picture, and what its guests may do. The code is stored twice: as a hash, to find the invitation by it, and
    sealed with the server secret, so that the people who invite can show the link again.

    It waits until it is started (``started_at``), lasts until ``expires_at`` and ends early when somebody ends it
    (``ended_at``); with either end every guest of it is out at once. At most one per playlist that has not ended."""

    __tablename__ = "guest_invites"
    __table_args__ = (
        Index("uq_guest_invites_open", "playlist_id", unique=True, sqlite_where=text("ended_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlists.id", ondelete="CASCADE"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    code_enc: Mapped[str] = mapped_column(Text)
    may_add: Mapped[bool] = mapped_column(Boolean, default=True)
    may_sort: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Tracks each guest may have in the playlist at once.
    max_tracks: Mapped[int] = mapped_column(Integer, default=10)
    hours: Mapped[int] = mapped_column(Integer, default=24)
    #: Guests may listen to a part of a track before they add it.
    preview: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)


class Guest(Base):
    """A guest of one invitation: a name and the hash of the token in its cookie. Taken out (``removed_at``) it stays
    out; with the end of the invitation it goes, its name stays on the entries as text."""

    __tablename__ = "guests"
    __table_args__ = (UniqueConstraint("invite_id", "name_key", name="uq_guests_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invite_id: Mapped[int] = mapped_column(ForeignKey("guest_invites.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(40))
    #: The name as two readers would take it to be the same (``services/guests.name_key``): one per invitation.
    name_key: Mapped[str] = mapped_column(String(160))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    joined_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class GuestEntry(Base):
    """Which entry of a playlist a guest added: what counts against its limit, and what it may take out again."""

    __tablename__ = "guest_entries"

    item_id: Mapped[int] = mapped_column(ForeignKey("playlist_items.id", ondelete="CASCADE"), primary_key=True)
    guest_id: Mapped[int] = mapped_column(ForeignKey("guests.id", ondelete="CASCADE"), index=True)


class ServerPlaylist(Base):
    """The copy of a playlist on one media server (``services/sync.py``): which one it is there, the last state both
    sides agreed on (``base``), and how the sync stands. One row per playlist and server: the playlist's source server,
    and the servers it lay on before (a server let go of, or one the playlists moved away from), whose copies are taken
    up again when that server is back (``resume``). A playlist deleted for good leaves its rows without
    ``playlist_id`` until the copies are gone too, so that they are never taken in again as new ones."""

    __tablename__ = "server_playlists"
    __table_args__ = (
        Index("uq_server_playlists_playlist", "playlist_id", "server_id", unique=True,
              sqlite_where=text("playlist_id IS NOT NULL")),
        Index("uq_server_playlists_key", "server_id", "ext_key", unique=True, sqlite_where=text("ext_key IS NOT NULL")),
        Index("ix_server_playlists_due", "due_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    playlist_id: Mapped[int | None] = mapped_column(ForeignKey("playlists.id", ondelete="SET NULL"), nullable=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("media_servers.id", ondelete="CASCADE"))
    #: The server's own key of the playlist (Plex: its ``ratingKey`` as text) and its ``guid``; None while it is not
    #: made there.
    ext_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    guid: Mapped[str] = mapped_column(String(200), default="")
    #: A smart playlist of the server: read only, its entries as the server lists them, ``smart_total`` of them in all.
    smart: Mapped[bool] = mapped_column(Boolean, default=False)
    smart_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: ``pending`` (to be written or read), ``synced``, ``waiting`` (server not reached, tried again), ``error``.
    state: Mapped[str] = mapped_column(String(8), default="pending")
    error: Mapped[str] = mapped_column(String(40), default="")
    #: Tries in a row that failed, for the growing wait before the next one.
    tries: Mapped[int] = mapped_column(Integer, default=0)
    #: When the next sync of this copy is due (a change in nexmix waits two seconds for more); None: none due.
    due_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: Set while the server is asked to make the playlist, with the name it is made with: a fault or a stop in between
    #: finds the one it made instead of making a second.
    creating_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    creating_title: Mapped[str] = mapped_column(String(200), default="", server_default="")
    #: The last state both sides agreed on: ``{"title": str, "items": [[track, entry id, server's item id], ...]}``.
    base: Mapped[Any] = mapped_column(JSON, nullable=True)
    #: Set while a write to the server is under way: what it is to bring (the name, the order with the server's ids as
    #: they were, what the server is to lose and to get). A fault or a stop in between finds it, and the next sync
    #: finishes it.
    writing: Mapped[Any] = mapped_column(JSON, nullable=True)
    #: Tracks the server refused, by number: ``{"tries": n, "next": time}`` (offered again from then on; no ``next``:
    #: never).
    refused: Mapped[Any] = mapped_column(JSON, nullable=True)
    #: What the list of the server said last (``updated_at``, ``leaf_count``), and whether to read it again anyway (it
    #: changed within two seconds of being asked: a second change in the same second would not show).
    seen: Mapped[Any] = mapped_column(JSON, nullable=True)
    recheck: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Both sides sorted at the same time: the server's order, kept for the undo (``{"order": [...], "at": ...}``).
    note: Mapped[Any] = mapped_column(JSON, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: The playlist went to the trash because it was deleted on the server.
    gone_there: Mapped[bool] = mapped_column(Boolean, default=False)
    #: A copy left on a server before: once the server is back, the next sync takes it up only when the server still
    #: lists it under its key (and ``guid``), else the copy is forgotten and the playlist made anew there.
    resume: Mapped[bool] = mapped_column(Boolean, default=False)
    #: The entries are to be found on this server first (the playlist came here from another server): ``all`` every
    #: entry, ``since`` the ones put in before the server was connected; empty: nothing to do.
    settle: Mapped[str] = mapped_column(String(8), default="", server_default="")


__all__ = [
    "MEMBER",
    "OPERATOR",
    "ROLES",
    "SERVER_JELLYFIN",
    "SERVER_KINDS",
    "SERVER_PLEX",
    "SIGN_IN_OIDC",
    "SIGN_IN_PASSWORD",
    "STAGE_CODES",
    "STAGE_FULL",
    "STAGE_SETUP",
    "Account",
    "AlbumTag",
    "ApiKey",
    "ArtistTag",
    "AuthSession",
    "Base",
    "CatalogAlbum",
    "CatalogArtist",
    "CatalogTag",
    "CatalogTrack",
    "Device",
    "EmailConfirmation",
    "Guest",
    "GuestEntry",
    "GuestInvite",
    "HiddenSuggestion",
    "Invite",
    "Job",
    "MediaServer",
    "OidcLink",
    "OidcProvider",
    "Passkey",
    "PasswordReset",
    "Playlist",
    "PlaylistItem",
    "PlaylistShare",
    "PushDevice",
    "ServerItem",
    "ServerPlaylist",
    "Setting",
    "SyncRun",
    "TrackTag",
    "UtcDateTime",
    "utcnow",
]
