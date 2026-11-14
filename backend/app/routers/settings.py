"""The operator's settings: address, sign-in, API keys, mail, backups; "Ready for the internet?".

Secrets (the mail password) are written encrypted and never read back: the answer only says whether one is set.
The sign-in providers live in ``routers/oidc.py``; the public address saved here asks first when the redirect address
of a provider changes with it (``authentik.public_url_guard``, the same question as the authentik card).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..deps import (
    DbSession,
    OperatorAccount,
    behind_unknown_proxy,
    confirm_operator,
)
from ..errors import error
from ..security import encrypt_secret
from ..services import accounts, mailer, readiness, settings_service, totp

logger = logging.getLogger("nexmix.settings")

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


class SettingsOut(BaseModel):
    public_url: str
    password_login: bool
    two_factor_required: bool
    backup_schedule: str
    backup_keep: int
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_user: str
    smtp_password_set: bool
    smtp_from: str
    api_keys_allowed: bool
    update_check: bool
    #: When the whole catalogue is fetched again each night (``HH:MM`` on the server's clock); empty: never.
    catalog_full_at: str = "03:30"
    #: Listening in to a piece of a track through nexmix (on from the start).
    preview_allowed: bool = True
    #: Whether this server serves the description of the interface (``NEXMIX_API_DOCS``, off from the start). Read
    #: only: the environment sets it, the settings page shows its links only then.
    api_docs: bool = False
    #: What a new invitation for guests starts with: listening allowed, tracks per guest, hours it lasts.
    guest_preview: bool = True
    guest_max: int = 10
    guest_hours: int = 24


class SettingsIn(BaseModel):
    public_url: str | None = Field(default=None, max_length=255)
    password_login: bool | None = None
    two_factor_required: bool | None = None
    #: The operator confirmed that the redirect address at the sign-in providers changes with the public address
    #: (``oidc.admin.addressChangeConfirm``); without it such a change answers 409 ``public_url_change``.
    confirm_address: bool = False
    #: The operator's password once more: asked when a change makes signing in weaker.
    current_password: str = Field(default="", max_length=200)
    backup_schedule: Literal["off", "daily", "weekly"] | None = None
    backup_keep: int | None = Field(default=None, ge=1, le=365)
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_security: Literal["tls", "starttls", "none"] | None = None
    smtp_user: str | None = Field(default=None, max_length=255)
    #: Empty removes the password; left out keeps it.
    smtp_password: str | None = Field(default=None, max_length=500)
    smtp_from: str | None = Field(default=None, max_length=255)
    api_keys_allowed: bool | None = None
    update_check: bool | None = None
    catalog_full_at: str | None = Field(default=None, max_length=5, pattern=r"^(?:(?:[01][0-9]|2[0-3]):[0-5][0-9])?$")
    preview_allowed: bool | None = None
    guest_preview: bool | None = None
    guest_max: int | None = Field(default=None, ge=1, le=100)
    guest_hours: int | None = Field(default=None, ge=1, le=720)


class TestMailIn(BaseModel):
    to: str = Field(max_length=255)


def _view(db: DbSession, request: Request) -> SettingsOut:
    values = settings_service.get_all(db)
    return SettingsOut(
        public_url=values["public_url"],
        password_login=values["password_login"],
        two_factor_required=bool(values["two_factor_required"]),
        backup_schedule=values["backup_schedule"],
        backup_keep=values["backup_keep"],
        smtp_host=values["smtp_host"],
        smtp_port=values["smtp_port"],
        smtp_security=values["smtp_security"],
        smtp_user=values["smtp_user"],
        smtp_password_set=bool(values["smtp_password_enc"]),
        smtp_from=values["smtp_from"],
        api_keys_allowed=bool(values["api_keys_allowed"]),
        update_check=bool(values["update_check"]),
        catalog_full_at=str(values["catalog_full_at"] or ""),
        preview_allowed=bool(values["preview_allowed"]),
        # What this app was built with (``create_app``), not only what the environment says.
        api_docs=bool(getattr(request.app.state, "api_docs", False)),
        guest_preview=bool(values["guest_preview"]),
        guest_max=int(values["guest_max"]),
        guest_hours=int(values["guest_hours"]),
    )


@router.get("", response_model=SettingsOut)
def read(request: Request, _operator: OperatorAccount, db: DbSession) -> SettingsOut:
    return _view(db, request)


#: Changes that make signing in weaker: each asks for the operator's password once more.
WEAKER = {"two_factor_required": False}


@router.put("", response_model=SettingsOut)
def save(payload: SettingsIn, request: Request, operator: OperatorAccount, db: DbSession) -> SettingsOut:
    sent = payload.model_dump(exclude_unset=True)
    current = settings_service.get_all(db)
    if any(key in sent and sent[key] is weaker and bool(current[key]) is not weaker for key, weaker in WEAKER.items()):
        # A stolen session must not be enough to take the second factor away from everybody.
        confirm_operator(request, db, operator, payload.current_password)
    changes: dict[str, Any] = {}
    for key, value in sent.items():
        if value is None or key in ("current_password", "confirm_address"):
            continue
        if key == "public_url":
            try:
                value = settings_service.normalize_public_url(value)
            except ValueError as exc:
                raise error("invalid_url", "Give an address like https://mix.example.com.", 422) from exc
            if value != settings_service.public_url(db):
                _ask_before_the_address(db, request, value, confirm=payload.confirm_address)
        elif key == "smtp_from":
            value = value.strip()
            if value and not accounts.EMAIL_PATTERN.match(value):
                raise error("invalid_email", "This is not a mail address.", 422)
        elif key == "two_factor_required" and value and not current["two_factor_required"] and (
            not totp.has_second_factor(db, operator) and not totp.provider_checks(db, operator)
        ):
            # Else the operator would be the first one sent to set it up, with nothing else in reach.
            raise error("own_second_factor_first", "Set up your own second factor first.", 409)
        elif key == "password_login" and not value and not _an_active_provider(db):
            # Without a provider nobody but the operator could sign in any more, and invitations would fail.
            raise error("provider_first", "Set up a sign-in provider first.", 409)
        elif key == "smtp_password":
            changes["smtp_password_enc"] = encrypt_secret(value)
            continue
        elif isinstance(value, str):
            value = value.strip()
        changes[key] = value
    settings_service.save(db, changes)
    logger.info("Settings changed keys=%s by=%s", ",".join(sorted(changes)), operator.name)
    return _view(db, request)


def _an_active_provider(db: DbSession) -> bool:
    from ..services.oidc_store import SqlStore
    from ..vendor.nexoidc import providers

    return bool(providers.public_list(SqlStore(db)))


def _ask_before_the_address(db: DbSession, request: Request, value: str, *, confirm: bool) -> None:
    """The public address is part of every provider's redirect address: saving another one (or none, which falls back
    to the request's) asks first while the authentik button registered the old one or a provider entered by hand is on
. 409 ``public_url_change`` with the question, nothing saved."""
    from ..services.oidc_store import SqlStore
    from ..vendor.nexoidc import authentik

    try:
        question = authentik.public_url_guard(SqlStore(db), value, str(request.base_url), confirm=confirm)
    except ValueError as exc:
        raise error("invalid_url", "Give an address like https://mix.example.com.", 422) from exc
    if question is not None:
        raise error("public_url_change", "The redirect address at the sign-in providers changes.", 409, **question)


@router.post("/mail-test", status_code=204, summary="Send a test mail through the configured server")
def mail_test(payload: TestMailIn, _operator: OperatorAccount, db: DbSession) -> None:
    to = payload.to.strip()
    if not accounts.EMAIL_PATTERN.match(to):
        raise error("invalid_email", "This is not a mail address.", 422)
    try:
        mailer.send_test(db, to)
    except mailer.MailError as exc:
        raise error(exc.code, str(exc), 502) from exc


@router.get("/readiness", summary="Ready for the internet? What nexmix checks about itself")
def ready(request: Request, operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    return readiness.check(db, request, operator, behind_unknown_proxy(request))
