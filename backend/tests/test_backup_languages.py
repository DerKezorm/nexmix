"""The languages the operator added go into a backup and come back with it; the archive says how many it holds, and an
archive that holds another number, or more entries than a backup can, is not usable."""

from __future__ import annotations

import json
import shutil
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services import backups, locales


@pytest.fixture(autouse=True)
def no_languages_left() -> Iterator[None]:
    """What a test added is gone before the next one: a backup takes every language of the folder along."""
    yield
    shutil.rmtree(locales.locales_dir(), ignore_errors=True)
    locales.forget()


def add_language(code: str = "es") -> Path:
    folder = locales.locales_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{code}.json"
    target.write_text(json.dumps({"_meta": {"name": "Español"}, "save": "Guardar"}), encoding="utf-8")
    locales.forget()
    return target


def test_a_backup_holds_the_added_languages_and_the_trial_run_counts_them(client: TestClient, operator: object) -> None:
    add_language("es")
    add_language("fr")
    (locales.locales_dir() / "notes.txt").write_text("not a language", encoding="utf-8")
    name = backups.create().name
    with zipfile.ZipFile(backups.path_of(name)) as archive:
        assert sorted(item for item in archive.namelist() if item.startswith("locales/")) == [
            "locales/es.json", "locales/fr.json"]
        assert json.loads(archive.read(backups.MANIFEST))["locales"] == 2
    assert backups.check(name).usable is True


def test_a_restore_puts_the_languages_back(client: TestClient, operator: object) -> None:
    target = add_language("es")
    name = backups.create().name
    target.unlink()
    backups.stage_restore(name)
    assert backups.apply_pending() is True
    assert json.loads(target.read_text(encoding="utf-8"))["save"] == "Guardar"


def test_an_archive_that_holds_another_number_of_languages_than_it_says_is_damaged(
        client: TestClient, operator: object, tmp_path: Path) -> None:
    add_language("es")
    name = backups.create().name
    source = backups.path_of(name)
    changed = source.with_name("nexmix-2026-01-01-000000.zip")
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(changed, "w") as after:
        for item in before.infolist():
            data = before.read(item.filename)
            if item.filename == backups.MANIFEST:
                data = json.dumps({**json.loads(data), "locales": 5}).encode()
            after.writestr(item.filename, data)
    with pytest.raises(backups.BackupError) as raised:
        backups.check(changed.name)
    assert raised.value.code == "backup_damaged"


def test_an_archive_with_too_many_entries_is_refused_before_one_is_read(
        client: TestClient, operator: object, monkeypatch: pytest.MonkeyPatch) -> None:
    name = backups.create().name
    monkeypatch.setattr(backups, "MAX_ENTRIES", 1)
    with pytest.raises(backups.BackupError) as raised:
        backups.check(name)
    assert raised.value.code == "backup_too_large"
