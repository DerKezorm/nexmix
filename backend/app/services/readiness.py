""""Ready for the internet?": what nexmix checks about itself before it is reachable from outside.

Each point is ``ok``, ``warn`` or ``bad`` with the values its sentence needs; the interface says what it means and
what helps. Checked at every call, from the request that asks and the server as it stands: nothing is remembered.
"""

from __future__ import annotations

import ipaddress
import os
import stat
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Account
from ..security import Brake
from . import backups, settings_service, totp

OK, WARN, BAD = "ok", "warn", "bad"
#: Whether file modes say anything here: Windows has no owner-only mode to read, the folder's rights decide there.
CHECK_MODE = os.name != "nt"


#: Networks a reverse proxy may sit in: the machine itself, a home or office network, a container network, a VPN.
#: ``X-Forwarded-For`` from anywhere else is not to be believed, whatever the operator wrote.
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10",
    "::1/128", "fc00::/7", "fe80::/10",
))


@dataclass
class Point:
    key: str
    state: str
    values: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "state": self.state, "values": self.values}


def came_over_https(request: Request) -> bool:
    """The request reached nexmix over https, or a trusted proxy in front says it did (``ForwardedProtoMiddleware``):
    the same as for the ``Secure`` flag of the cookies (``routers.auth.secure_cookie``), so the two never disagree."""
    return request.url.scheme == "https"


def _https(db: Session, request: Request) -> Point:
    public = settings_service.public_url(db)
    if urlsplit(public).scheme != "https":
        return Point("https", BAD, {"address": public})
    if not came_over_https(request):
        return Point("https", WARN, {"address": public})
    return Point("https", OK, {"address": public})


def _two_factor(db: Session) -> Point:
    """Off is the default and no fault: a recommendation for whoever puts nexmix on the internet."""
    return Point("two_factor", OK if settings_service.get(db, "two_factor_required") else WARN)


def _own_account(db: Session, operator: Account) -> Point:
    """Like the switch for everybody, a recommendation: an operator who signs in through a provider may well do
    without one here."""
    return Point("own_account", OK if totp.has_second_factor(db, operator) else WARN)


def public_proxy_networks(spec: str) -> list[str]:
    """The entries of the list of trusted proxies that are not inside a private network: ``0.0.0.0/0``, ``::/0``, any
    public address or network. Whoever sends from there could write any sender into ``X-Forwarded-For``, and the
    brake against guessing would count a made-up one. Entries that are no address are ignored, as at sign-in."""
    found = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if not any(network.version == private.version and network.subnet_of(private) for private in PRIVATE_NETWORKS):
            found.append(str(network))
    return found


def brake_works() -> bool:
    """Whether the brake against guessing does what it is for, tried on a copy of its own: after the failures it lets
    pass, the sender waits; another sender does not; and the wait ends the sender's count when it is told of a
    success. The brake sign-ins use is not touched."""
    probe = Brake(quiet=True)
    guesser, other = "probe:guesser", "probe:other"
    for _ in range(Brake.FREE):
        if probe.wait_seconds(guesser):
            return False
        probe.failed(guesser)
    held = probe.wait_seconds(guesser) > 0 and probe.wait_seconds(other) == 0
    probe.succeeded(guesser)
    return held and probe.wait_seconds(guesser) == 0


def _brake(unknown_proxy: bool) -> Point:
    # Checked, not taken for granted: the mechanism is tried on a copy, and the list of proxies must not let a
    # stranger choose the sender the brake counts. Behind a proxy nexmix was not told about, every sender looks
    # alike and only the lock per account is left.
    public = public_proxy_networks(get_settings().trusted_proxies)
    if public:
        return Point("brake", BAD, {"networks": ", ".join(public)})
    if not brake_works():
        return Point("brake", BAD, {"broken": True})
    return Point("brake", WARN if unknown_proxy else OK)


def _secrets() -> Point:
    """``secret.key`` seals what the server must read on its own (OIDC client secret, mail password, AI key): only the
    owner may read it. Given in the environment (``NEXMIX_SECRET_KEY``), there is no file to look at."""
    if get_settings().secret_key:
        return Point("secrets", OK, {"source": "environment"})
    path = get_settings().data_dir / "secret.key"
    try:
        info = path.stat()
    except OSError:
        return Point("secrets", BAD, {"path": str(path), "missing": True})
    if not CHECK_MODE:
        return Point("secrets", OK, {"path": str(path), "mode": None})
    mode = stat.S_IMODE(info.st_mode)
    open_to_others = bool(mode & 0o077)
    return Point("secrets", BAD if open_to_others else OK, {"path": str(path), "mode": f"{mode:04o}"})


def _cookies(request: Request) -> Point:
    """The sentence names how long a sign-in lasts without use (NEXMIX_SESSION_DAYS), never a fixed number."""
    mode = get_settings().cookie_secure.lower()
    days = get_settings().session_days
    if mode == "off":
        return Point("cookies", BAD, {"mode": mode, "days": days})
    if not came_over_https(request):
        return Point("cookies", WARN, {"mode": mode, "days": days})
    return Point("cookies", OK, {"mode": mode, "days": days})


def _proxy(request: Request, unknown_proxy: bool) -> Point:
    if unknown_proxy:
        peer = request.client.host if request.client else ""
        return Point("proxy", WARN, {"proxy": peer})
    trusted = get_settings().trusted_proxies.strip()
    public = public_proxy_networks(trusted)
    if public:
        return Point("proxy", BAD, {"proxies": trusted, "networks": ", ".join(public)})
    return Point("proxy", OK, {"proxies": trusted, "forwarded": bool(request.headers.get("x-forwarded-for"))})


def _backups(db: Session) -> Point:
    """The database, the settings and the secrets are copied regularly (the music is not: it lies on the media
    servers)."""
    every = backups.schedule(db)
    return Point("backups", OK if every != "off" else WARN, {"schedule": every})


def _internal(url: str) -> bool | None:
    """Whether the address reaches the server inside the own network only (None: its name does not resolve)."""
    from . import outbound, plex

    parts = urlsplit(url)
    if not parts.hostname:
        return None
    try:
        addresses = plex.resolver(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
        ips = [outbound.ip_of(address) for address in addresses]
    except (outbound.Unreachable, ValueError):
        return None
    return bool(ips) and all(any(ip.version == network.version and ip in network for network in PRIVATE_NETWORKS)
                             for ip in ips)


def _jellyfin(db: Session) -> Point | None:
    """Jellyfin serves sound and pictures to whoever knows an id, without signing in: nexmix never passes one on, and
    the server is best reached at an address inside the own network. Its token must be there and readable."""
    from ..models import SERVER_JELLYFIN
    from . import jellyfin_link, media

    rows = [row for row in media.active(db) if row.kind == SERVER_JELLYFIN]
    if not rows:
        return None
    no_token, outside = [], []
    for row in rows:
        try:
            readable = bool(row.token_enc) and bool(jellyfin_link.token_of(row))
        except jellyfin_link.Unreadable:
            readable = False
        if not readable:
            no_token.append(row.name)
        if _internal(row.url) is not True:
            outside.append(row.name)
    if no_token:
        return Point("jellyfin", BAD, {"names": ", ".join(no_token), "token": False})
    if outside:
        return Point("jellyfin", WARN, {"names": ", ".join(outside), "token": True})
    return Point("jellyfin", OK, {"names": ", ".join(row.name for row in rows), "token": True})


def _music_assistant(db: Session) -> Point | None:
    """Music Assistant serves previews and pictures to whoever knows their id, without signing in, and its token (a
    year at most) opens everything there: the server is best reached at an address inside the own network, its token
    must be there, readable and not about to run out (``ma_link.WARN_DAYS``)."""
    from ..models import SERVER_MA
    from . import ma_link, media

    rows = [row for row in media.active(db) if row.kind == SERVER_MA]
    if not rows:
        return None
    no_token, outside, running_out = [], [], []
    soonest: int | None = None
    for row in rows:
        try:
            readable = bool(row.token_enc) and bool(ma_link.token_of(row))
        except ma_link.Unreadable:
            readable = False
        token = ma_link.token_view(row)
        if not readable or token["expired"]:
            no_token.append(row.name)
        elif token["warn"]:
            running_out.append(row.name)
            soonest = token["days_left"] if soonest is None else min(soonest, token["days_left"])
        if _internal(row.url) is not True:
            outside.append(row.name)
    if no_token:
        return Point("music_assistant", BAD, {"names": ", ".join(no_token), "token": False, "days": None})
    if running_out:
        return Point("music_assistant", WARN, {"names": ", ".join(running_out), "token": True, "days": soonest})
    if outside:
        return Point("music_assistant", WARN, {"names": ", ".join(outside), "token": True, "days": None})
    return Point("music_assistant", OK, {"names": ", ".join(row.name for row in rows), "token": True, "days": None})


def check(db: Session, request: Request, operator: Account, unknown_proxy: bool) -> dict[str, Any]:
    jellyfin = _jellyfin(db)
    music_assistant = _music_assistant(db)
    points = [
        _https(db, request),
        _two_factor(db),
        _own_account(db, operator),
        _brake(unknown_proxy),
        _secrets(),
        _cookies(request),
        _proxy(request, unknown_proxy),
        _backups(db),
        *([jellyfin] if jellyfin is not None else []),
        *([music_assistant] if music_assistant is not None else []),
    ]
    return {"points": [point.as_dict() for point in points], "open": sum(point.state != OK for point in points)}
