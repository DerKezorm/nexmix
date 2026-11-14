"""Nothing real in the repository: no address of a home network, no mail address of anybody.

``test_no_attribution.py`` runs the project's own scanner (which knows the real names) where it exists. This file is the
part that holds everywhere, CI included, because it needs no secret list: addresses of the private ranges that a
home network uses in the 10.10 block, and mail addresses outside the reserved placeholder domains. The invented
data of the tests and the documentation uses ``example.com`` and ``127.0.0.1``.
"""

from __future__ import annotations

import re

from .test_no_attribution import FLOOR, ROOT, repository_files, text_of

HOME_NETWORK = re.compile(r"(?<![0-9.])10\.10\.\d{1,3}\.\d{1,3}(?![0-9])")
MAIL = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
#: Where a mail address may point: the domains reserved for examples, and the address GitHub gives to people who
#: keep theirs private (``<id>+<name>@users.noreply.github.com``).
FINE_DOMAINS = re.compile(r"^(?:[A-Za-z0-9-]+\.)*(?:example\.(?:com|org|net)|invalid|test|localhost|noreply\.github\.com)$", re.IGNORECASE)
#: A file name such as ``icon@2x.png`` looks like an address; so does a package at a version.
IMAGE_SUFFIXES = {"png", "jpg", "jpeg", "webp", "gif", "svg", "avif"}
#: Lock files list packages as ``name@1.2.3`` and carry hashes; they hold no addresses of people.
SKIPPED = {"frontend/package-lock.json"}


def strangers_in(text: str) -> list[str]:
    found = []
    for match in MAIL.finditer(text):
        domain = match.group(1)
        if domain.rsplit(".", 1)[-1].lower() in IMAGE_SUFFIXES or FINE_DOMAINS.match(domain):
            continue
        found.append(match.group(0))
    return found


def test_no_file_holds_an_address_of_a_home_network_or_somebody_s_mail_address() -> None:
    files = repository_files()
    assert len(files) >= FLOOR, f"only {len(files)} files listed; is the listing looking at the repository?"
    found: list[str] = []
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        text = text_of(path)
        if text is None or relative in SKIPPED:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if HOME_NETWORK.search(line):
                found.append(f"{relative}:{number}: address of a home network")
            if strangers_in(line):
                found.append(f"{relative}:{number}: a mail address outside the placeholder domains")
    assert not found, "Something real in the repository:\n" + "\n".join(found[:40])


def test_nothing_in_the_repository_carries_the_name_of_the_app_this_one_was_made_from() -> None:
    """The frame came from another nex app: its name, its environment variables and its token prefixes are gone. The
    shared sign-in module and its contract tests (copied unchanged) list the apps they serve."""
    old = "nex" + "paper"
    found: list[str] = []
    for path in repository_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(("backend/app/vendor/nexoidc/", "backend/tests/nexoidc/", "frontend/src/vendor/nexoidc/")):
            continue
        if relative == "frontend/package-lock.json" or old in relative.lower():
            found += [relative] if old in relative.lower() else []
            continue
        text = text_of(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if old in line.lower():
                found.append(f"{relative}:{number}")
    assert not found, "The old name is still there:" + chr(10) + chr(10).join(found[:40])


def test_the_check_knows_what_it_looks_for() -> None:
    # Put together from pieces: written out, these lines would be what the check (and the commit hook) object to.
    net = "10." + "10."
    assert HOME_NETWORK.search(f"ssh {net}11.128")
    assert HOME_NETWORK.search(f"http://{net}10.65:8420")
    assert not HOME_NETWORK.search("version 110.10.1.2 and 10.1.1.1 and 127.0.0.1")
    stranger = "jule@" + "gmail.com"
    assert strangers_in(f"write to {stranger}") == [stranger]
    other = "tom@" + "familie-beispiel.de"
    assert strangers_in(f"mail me: {other}.") == [other]
    hidden = "12345+x@" + "users.noreply." + "github.com"
    assert strangers_in(f"jule@example.com, tom@mail.example.org, {hidden}") == []
    assert strangers_in("icon@2x.png and react@19.2.0 and @milkdown/kit") == []
