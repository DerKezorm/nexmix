"""Every test run gets its own empty data directory; nothing touches ``data/`` of the project."""

from __future__ import annotations

import os
import secrets
import tempfile
from collections.abc import Iterator

_DATA = tempfile.mkdtemp(prefix="nexmix-tests-")
os.environ["NEXMIX_DATA_DIR"] = _DATA
os.environ["NEXMIX_DISABLE_BACKGROUND"] = "1"
# The description of the interface and its page are off from the start; the tests of them turn them on by themselves.
os.environ["NEXMIX_API_DOCS"] = "false"
os.environ["NEXMIX_FRONTEND_DIST"] = os.path.join(_DATA, "no-frontend")
# Set, not removed: a value in the environment wins over a .env file in the project, a removed one does not.
os.environ["NEXMIX_LOCALES_DIR"] = os.path.join(_DATA, "locales")
os.environ["NEXMIX_LOG_LEVEL"] = ""
# Argon2 as cheap as it goes: the tests make many accounts. The strength itself is Argon2's business.
os.environ["NEXMIX_ARGON2_TIME"] = "1"
os.environ["NEXMIX_ARGON2_MEMORY_KIB"] = "1024"
os.environ["NEXMIX_ARGON2_PARALLELISM"] = "1"
# Never GitHub from a test: port 9 refuses at once.
os.environ["NEXMIX_UPDATE_URL"] = "http://127.0.0.1:9/releases/latest"
# A fresh secret for each run, made here and never written down: no key stands in the code.
os.environ["NEXMIX_SECRET_KEY"] = secrets.token_urlsafe(32)
SETUP_CODE = "test-setup-code"
os.environ["NEXMIX_SETUP_TOKEN"] = SETUP_CODE

import shutil  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete, text  # noqa: E402

from app import middleware  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import MEMBER, OPERATOR, Account, Base, Setting  # noqa: E402
from app.routers import playlists as playlist_routes  # noqa: E402
from app.security import SESSION_COOKIE, brake, hash_password, start_session  # noqa: E402
from app.services import (  # noqa: E402
    apikeys,
    backups,
    brakes,
    catalog,
    catalog_view,
    covers,
    guests,
    jellyfin,
    jellyfin_link,
    jobs,
    mirrors,
    notices,
    oidc_store,
    passkeys,
    plex,
    plex_link,
    plextv,
    preview,
    push,
    settings_service,
    suggestions,
    totp,
    updates,
)
from app.services import sync as server_sync  # noqa: E402
from app.vendor.nexoidc import attempt as nexoidc_attempt  # noqa: E402
from app.vendor.nexoidc import protocol as nexoidc_protocol  # noqa: E402

# The stand-in sign-in provider of the shared contract tests, for nexmix's own tests around it (``oidc_helpers``).
from .oidc_helpers import provider  # noqa: E402, F401

DATA_DIR = _DATA
TAB = {"X-Nexmix-Client": "tab-tests000"}


@pytest.fixture(scope="session", autouse=True)
def schema() -> None:
    init_db()


@pytest.fixture(autouse=True)
def clean_db(schema: None) -> Iterator[None]:
    with SessionLocal() as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(delete(table))
        db.execute(delete(Setting))
        db.execute(text("DELETE FROM track_search"))
        # The settings as they come from the start: a second factor is not required. The tests of the requirement
        # turn it on (``require_second_factor``).
        db.commit()
    # Backups of a test before (an automatic one of the same second) must not decide what another test prunes.
    shutil.rmtree(backups.folder(), ignore_errors=True)
    # The shared sign-in module: configured as at start-up, nothing remembered from a test before.
    oidc_store.configure()
    nexoidc_protocol.clear_caches()
    nexoidc_attempt.forget_used_states()
    brake.forget()
    middleware.forget_gate()
    brakes.forget()
    totp.forget()
    updates.forget()
    apikeys.forget()
    notices.forget()
    push.forget()
    passkeys.forget()
    plex_link.forget()
    jellyfin_link.forget()
    server_sync.forget()
    mirrors.forget_asked()
    mirrors.forget_looks()
    catalog.forget()
    covers.forget_all()
    catalog_view.forget()
    preview.forget()
    suggestions.forget()
    guests.forget()
    playlist_routes.forget()
    # A test that ran the background loop leaves the flag "the server is stopping" set when the loop ends; a worker
    # started in a later test would take it as its own stop.
    jobs.stopping.clear()
    yield
    notices.settle()
    app.dependency_overrides.clear()


PASSWORD = "correct horse battery"


def require_second_factor(on: bool = True) -> None:
    """The operator's switch "require a second factor" (off from the start)."""
    with SessionLocal() as db:
        settings_service.save(db, {"two_factor_required": on})


def make_account(name: str, role: str = MEMBER, password: str = PASSWORD) -> Account:
    """An account with a password, straight into the database."""
    with SessionLocal() as db:
        row = Account(name=name, role=role, password_hash=hash_password(password))
        db.add(row)
        db.commit()
        db.expunge(row)
    return row


def sign_in(client: TestClient, account: Account) -> None:
    """The client carries a session of ``account`` from now on (a real one, as after signing in)."""
    with SessionLocal() as db:
        row = db.get(Account, account.id)
        assert row is not None
        token = start_session(db, row, "127.0.0.1", "tests")
    client.cookies.set(SESSION_COOKIE, token)


def new_client(account: Account | None = None) -> TestClient:
    """Another browser, signed in as ``account`` when one is given. Close it (``with``) when done."""
    client = TestClient(app, base_url="http://testserver", headers=TAB)
    if account is not None:
        sign_in(client, account)
    return client


def person(name: str) -> TestClient:
    """A browser signed in as a new member ``name``."""
    return new_client(make_account(name))


def _operator(client: TestClient) -> Account:
    with SessionLocal() as db:
        row = db.query(Account).filter_by(name="tester").one_or_none()
        if row is not None:
            db.expunge(row)
    if row is None:
        row = make_account("tester", OPERATOR)
    sign_in(client, row)
    return row


@pytest.fixture
def client() -> Iterator[TestClient]:
    # As the interface does: every request names its tab (changes are refused without it).
    with TestClient(app, base_url="http://testserver", headers=TAB) as test_client:
        yield test_client


@pytest.fixture
def push_service(monkeypatch: pytest.MonkeyPatch) -> object:
    """The stand-in of a push service (``fake_push.py``), added to the operator's list of push services, its name
    resolving to a public address. The check of where a message may go stays as it is."""
    from .fake_push import HOST, FakePushService, public_resolver

    fake = FakePushService()
    monkeypatch.setattr(push, "transport", fake.transport())
    monkeypatch.setattr(push, "resolver", public_resolver)
    with SessionLocal() as db:
        settings_service.save(db, {"push_hosts": [HOST]})
    return fake


def fake_resolver(host: str, port: int) -> list[str]:
    """Names of the stand-in Plex resolve to a documentation address; one name stands for a metadata service."""
    if host.replace(".", "").isdigit() or ":" in host:
        return [host]
    if host == "metadata.example.com":
        return ["169.254.169.254"]
    return ["192.0.2.10"]


@pytest.fixture
def fake_plex(monkeypatch: pytest.MonkeyPatch) -> object:
    """The stand-in plex.tv and Plex Media Server (``fakeplex``) under the real client: every request of nexmix to
    Plex goes to it, nothing to the network."""
    import httpx

    from .fakeplex import FakePlex

    fake = FakePlex()
    transport = httpx.MockTransport(fake.handle)
    monkeypatch.setattr(plex, "TRANSPORT", transport)
    monkeypatch.setattr(plex, "ASYNC_TRANSPORT", transport)
    monkeypatch.setattr(plextv, "TRANSPORT", transport)
    monkeypatch.setattr(plex, "resolver", fake_resolver)
    return fake


@pytest.fixture
def fake_jellyfin(monkeypatch: pytest.MonkeyPatch) -> object:
    """The stand-in Jellyfin (``fakejellyfin``) under the real client: every request of nexmix to Jellyfin goes to it,
    nothing to the network. Waiting for a server that starts is counted, not waited (``fake.waits``)."""
    import httpx

    from .fakejellyfin import FakeJellyfin

    fake = FakeJellyfin()
    transport = httpx.MockTransport(fake.handle)
    monkeypatch.setattr(jellyfin, "TRANSPORT", transport)
    monkeypatch.setattr(jellyfin, "ASYNC_TRANSPORT", transport)
    monkeypatch.setattr(plex, "resolver", fake_resolver)
    waits: list[float] = []
    monkeypatch.setattr(jellyfin, "pause", waits.append)
    fake.waits = waits  # type: ignore[attr-defined]
    return fake


@pytest.fixture
def operator(client: TestClient) -> Account:
    """The signed-in operator ``tester``."""
    return _operator(client)


@pytest.fixture
def account(client: TestClient) -> Account:
    """The same as ``operator``, under another name."""
    return _operator(client)


def pytest_unconfigure(config: pytest.Config) -> None:
    """The data folder of this run goes when the run ends: the log file is closed first (Windows keeps open files),
    then the folder with everything in it."""
    import logging

    for logger in (logging.getLogger(), logging.getLogger("uvicorn.error")):
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
    from app.db import engine

    engine.dispose()
    shutil.rmtree(_DATA, ignore_errors=True)
