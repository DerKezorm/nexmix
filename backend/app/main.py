"""Entry point: the FastAPI app, routers, background tasks, the built frontend."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, private
from .config import get_settings
from .db import SessionLocal, init_db
from .errors import detail
from .middleware import (
    ForwardedProtoMiddleware,
    GuardMiddleware,
    RequestContextMiddleware,
    SignInGate,
    unhandled_error,
)
from .routers import (
    about,
    apikeys,
    auth,
    devices,
    docs,
    emails,
    health,
    invites,
    oidc,
    passkeys,
    push,
    resets,
)
from .routers import avatars as avatars_router
from .routers import backups as backups_router
from .routers import catalog as catalog_router
from .routers import guest as guest_router
from .routers import guest_invites as guest_invites_router
from .routers import jellyfin as jellyfin_router
from .routers import join as join_router
from .routers import locales as locales_router
from .routers import logs as logs_router
from .routers import mirrors as mirrors_router
from .routers import playlists as playlists_router
from .routers import plex as plex_router
from .routers import preview as preview_router
from .routers import servers as servers_router
from .routers import settings as settings_router
from .routers import storage as storage_router
from .routers import suggestions as suggestions_router
from .routers import totp as totp_router
from .routers import v1 as v1_router
from .security import HashingBusy, purge_sessions
from .services import (
    accounts,
    backups,
    catalog,
    jellyfin_events,
    jobs,
    locales,
    logs,
    mounts,
    notices,
    oidc_store,
    pictures,
    plex_events,
    settings_service,
    sync,
    totp,
)
from .services import guests as guests_service
from .services import manifest as manifest_service
from .services import passkeys as passkeys_service
from .services import playlists as playlists_service
from .vendor import nexoidc

logger = logging.getLogger("nexmix")

ROUTERS = [
    health, about, docs, locales_router, logs_router, auth, totp_router, oidc, invites, settings_router,
    backups_router, avatars_router, emails, apikeys, devices, v1_router, push, passkeys, resets, storage_router,
    plex_router, jellyfin_router, servers_router, catalog_router, playlists_router, mirrors_router, preview_router,
    suggestions_router,
    guest_invites_router, join_router, guest_router,
]


def _read_log_mode() -> tuple[str, datetime | None]:
    with SessionLocal() as db:
        mode = settings_service.get(db, "log_mode")
        raw = settings_service.get(db, "log_mode_until")
    until = datetime.fromisoformat(raw).astimezone(UTC) if raw else None
    return str(mode or logs.DEFAULT_MODE), until


def _write_log_mode(mode: str, until: datetime | None) -> None:
    with SessionLocal() as db:
        settings_service.save(db, {"log_mode": mode, "log_mode_until": until.isoformat() if until else None})


async def _sweep_forever(stop: asyncio.Event) -> None:
    """Enrolments and sign-ins waiting for their second factor run out; what ran out goes from memory, ended
    sessions from the database once an hour (they stayed until someone showed them again)."""
    rounds = 0
    while not stop.is_set():
        totp.sweep()
        passkeys_service.sweep()
        if rounds % 60 == 0:
            with SessionLocal() as db:
                purge_sessions(db)
                # Playlists that lay in the trash for 30 days, entries taken out a day ago.
                playlists_service.purge(db)
                # Invitations for guests that ran out or ended, with their guests.
                guests_service.purge(db)
        rounds += 1
        try:
            await asyncio.wait_for(stop.wait(), 60)
        except TimeoutError:
            continue


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    logs.setup()
    backups.apply_pending()
    init_db()
    # A restore that could not be put in place at this start: the operators hear of it once.
    backups.report_failed_restore()
    mounts.warn_at_start(get_settings().data_dir, get_settings().database_path)
    # The TLS context of the sign-in module, read once in a thread now: the first sign-in does not wait for it.
    await nexoidc.warm()
    private.tighten_all()
    logs.attach_store(_read_log_mode, _write_log_mode)
    logs.apply_stored_mode()
    settings = get_settings()
    if settings.locales_dir is not None:
        settings.locales_dir.mkdir(parents=True, exist_ok=True)
    added = locales.available()
    if added:
        logger.info("Languages added by the operator: %s", ", ".join(locale.code for locale in added))
    stop = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    settings.resolved_secret_key()
    if not settings.disable_background:
        tasks.append(asyncio.create_task(logs.run_forever(stop)))
        tasks.append(asyncio.create_task(backups.run_forever(stop)))
        tasks.append(asyncio.create_task(_sweep_forever(stop)))
        tasks.append(asyncio.create_task(jobs.run_forever(stop)))
        for lane in jobs.LANES:
            tasks.append(asyncio.create_task(jobs.run_forever(stop, lane=lane)))
        # The clock of the catalogue: a partial run every 15 minutes, the whole one at night.
        tasks.append(asyncio.create_task(catalog.run_forever(stop)))
        # The clock of the playlists' sync with Plex: due playlists, and a look at Plex every 15 seconds.
        tasks.append(asyncio.create_task(sync.run_forever(stop)))
        tasks.append(asyncio.create_task(plex_events.run_forever(stop)))
        tasks.append(asyncio.create_task(jellyfin_events.run_forever(stop)))
    logger.info("nexmix %s started", __version__)
    with SessionLocal() as db:
        accounts.announce_setup_code(db)
    try:
        yield
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(notices.settle, 10)
        logger.info("nexmix stopped")


def create_app(*, api_docs: bool | None = None) -> FastAPI:
    """The app with its middleware, its routers and the built frontend. ``api_docs`` switches the description of the
    interface and its page on (``NEXMIX_API_DOCS``, off from the start: an instance on the internet does not hand
    out the list of its routes unasked); None takes the setting."""
    documented = get_settings().api_docs if api_docs is None else api_docs
    application = FastAPI(
        title="nexmix",
        version=__version__,
        summary="The interface the web app speaks, and the one native apps and scripts use.",
        lifespan=lifespan,
        # The description is part of the interface (``tests/schema/openapi.json`` keeps it); the page that lists it is
        # ``routers/docs.py`` (the usual page loads its script from a CDN, which the policy of the app forbids).
        docs_url=None,
        openapi_url="/api/v1/openapi.json" if documented else None,
        redoc_url=None,
    )
    # The settings page asks whether the description is served, and shows its links only then.
    application.state.api_docs = documented
    # Added last, runs first: the request id and headers wrap everything, the guard sits inside it, and innermost the
    # door before the sign-in (a request that cannot be anybody's is answered before the framework or the database).
    application.add_middleware(SignInGate, routes=lambda: application.routes)
    application.add_middleware(GuardMiddleware)
    application.add_middleware(RequestContextMiddleware)
    # Outermost: the scheme a trusted proxy reports is the request's for everything inside (``request.base_url``).
    application.add_middleware(ForwardedProtoMiddleware)

    @application.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [".".join(str(part) for part in error.get("loc", ()) if part != "body") for error in exc.errors()]
        return JSONResponse(
            status_code=422, content={"detail": detail("invalid_input", "The input is not valid.", fields=fields)}
        )

    @application.exception_handler(OverflowError)
    async def _too_large_a_number(_request: Request, _exc: OverflowError) -> JSONResponse:
        # A number past what SQLite holds (an id like 99999999999999999999 in the address): not a fault of the server.
        return JSONResponse(status_code=422, content={"detail": detail("invalid_input", "The input is not valid.")})

    @application.exception_handler(HashingBusy)
    async def _hashing_busy(_request: Request, _exc: HashingBusy) -> JSONResponse:
        # Every slot for checking a password stayed taken for 15 s: many sign-ins at once, as in a flood of guesses.
        return JSONResponse(status_code=503, headers={"Retry-After": "5"},
                            content={"detail": detail("busy", "The server is busy. Try again in a moment.")})

    @application.exception_handler(pictures.DecodersBusy)
    async def _decoders_busy(_request: Request, _exc: pictures.DecodersBusy) -> JSONResponse:
        # Every decoder stayed taken: many large photos at once. A moment later it works.
        return JSONResponse(status_code=503, headers={"Retry-After": "5"},
                            content={"detail": detail("busy", "The server is busy. Try again in a moment.")})

    @application.exception_handler(UnicodeEncodeError)
    async def _not_unicode(_request: Request, _exc: UnicodeEncodeError) -> JSONResponse:
        # JSON may carry a lone surrogate ("\ud800"); no file name, path or database text can hold it.
        return JSONResponse(status_code=422, content={"detail": detail("invalid_input", "The input is not valid.")})

    @application.exception_handler(StarletteHTTPException)
    async def _plain_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # FastAPI's own 400 for a body that is not JSON carries only a text; give it a code like every other answer.
        if exc.status_code == 400 and isinstance(exc.detail, str):
            return JSONResponse(status_code=400, content={"detail": detail("invalid_input", "The input is not valid.")})
        return await http_exception_handler(request, exc)

    application.add_exception_handler(Exception, unhandled_error)

    for module in ROUTERS:
        if module is docs and not documented:
            continue
        application.include_router(module.router)
    _mount_frontend(application, get_settings().frontend_dist)
    return application


#: Files a browser asks the server about again each time: the service worker (an old one would keep its old ways for a
#: day). The manifest is asked about afresh too, and comes in the language of the browser (``_manifest`` below).
FRESH = ("sw.js",)
MANIFEST = "manifest.webmanifest"


def _mount_frontend(target: FastAPI, dist: Path) -> None:
    index = dist / "index.html"
    if not index.exists():
        return
    if (dist / "assets").is_dir():
        target.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
    root = dist.resolve()
    start_page = index.resolve()

    @target.get(f"/{MANIFEST}", include_in_schema=False, response_model=None)
    def _manifest(request: Request) -> Response:
        # The names of the shortcuts in the browser's language (services/manifest.py). The answer depends on
        # Accept-Language: a cache in between keeps one per language, the browser asks again each time.
        try:
            base = json.loads((dist / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        if not isinstance(base, dict):
            base = {}
        language = manifest_service.choose(request.headers.get("accept-language", ""))
        body = json.dumps(manifest_service.localized(base, language), ensure_ascii=False, indent=2)
        return Response(body, media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache", "Vary": "Accept-Language", "Content-Language": language})

    @target.get("/{path:path}", include_in_schema=False, response_model=None)
    def spa(path: str) -> FileResponse | JSONResponse:
        if path == "api" or path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": detail("not_found", "Not found.")})
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and root in candidate.parents and candidate != start_page:
            if path in FRESH:
                return FileResponse(candidate, headers={"Cache-Control": "no-cache"})
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


app = create_app()
# The shared sign-in module learns who it runs in, before the first request.
oidc_store.configure()
