"""Every link with `?tab=` anywhere in the code (interface, server, mails, README, docs) names a tab of the settings or
of "My account" that is there, without leaning on the redirect of an old or unknown value."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TABS = ROOT / "frontend" / "src" / "pages" / "settings" / "tabs.ts"
LINK = re.compile(r"/(settings|account)\?tab=([a-z_-]+)")
SKIP = {"node_modules", "__pycache__", "dist", "static"}


def _tabs(name: str) -> set[str]:
    found = re.search(rf"export const {name} = \[([^\]]*)\]", TABS.read_text(encoding="utf-8"))
    assert found, f"{name} not found in tabs.ts"
    return set(re.findall(r"'([a-z_-]+)'", found.group(1)))


def _files() -> list[Path]:
    places = [ROOT / "frontend" / "src", ROOT / "backend" / "app", ROOT / "docs"]
    files = [ROOT / "README.md", ROOT / "SECURITY.md"]
    for place in places:
        for path in place.rglob("*"):
            if any(part in SKIP or part.startswith(".") for part in path.relative_to(ROOT).parts):
                continue
            if (
                path.is_file()
                and path.suffix in {".ts", ".tsx", ".py", ".md", ".json", ".html"}
                and ".test." not in path.name
            ):
                files.append(path)
    return files


def test_every_tab_link_names_a_tab_that_is_there() -> None:
    tabs = {"settings": _tabs("SERVER_TABS"), "account": _tabs("ACCOUNT_TABS")}
    assert "backup" in tabs["settings"] and "security" in tabs["account"]
    seen, wrong, server_side = 0, [], 0
    for path in _files():
        for page, tab in LINK.findall(path.read_text(encoding="utf-8", errors="replace")):
            seen += 1
            server_side += path.suffix == ".py"
            if tab not in tabs[page]:
                wrong.append(f"{path.relative_to(ROOT)}: /{page}?tab={tab}")
    assert seen > 5 and server_side >= 2, (seen, server_side)
    assert wrong == []
