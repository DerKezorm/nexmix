"""After a run of the catalogue the free memory goes back to the system (glibc keeps it otherwise), and that never
stops the view from being built."""

from __future__ import annotations

import sys

import pytest

from app.services import catalog_view


def test_warming_the_view_gives_the_memory_back_also_when_building_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    given: list[bool] = []
    monkeypatch.setattr(catalog_view, "give_back", lambda: given.append(True) or True)
    monkeypatch.setattr(catalog_view, "current", lambda db=None: None)
    catalog_view.warm()
    assert given == [True]

    def broken(db: object = None) -> None:
        raise RuntimeError("forced for the test")

    monkeypatch.setattr(catalog_view, "current", broken)
    catalog_view.warm()
    assert given == [True, True]


def test_giving_back_works_on_glibc_and_does_nothing_elsewhere() -> None:
    assert catalog_view.give_back() is sys.platform.startswith("linux")
