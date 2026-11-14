"""The app carries no name but its own: nothing in the repository names the app it was started from."""

from __future__ import annotations

from .test_no_attribution import repository_files, text_of

#: Put together from pieces, so that this file does not name it either.
OLD = "nex" + "books"
FLOOR = 200


def test_no_file_names_the_app_it_was_started_from() -> None:
    files = repository_files()
    assert len(files) >= FLOOR, f"only {len(files)} files listed"
    found = []
    for path in files:
        if path.name == "package-lock.json" or OLD in path.name.lower():
            continue
        text = text_of(path)
        if text is not None and OLD in text.lower():
            found.append(path.name)
    assert found == [], f"the old name is in: {found}"


def test_no_file_is_called_by_the_old_name() -> None:
    assert [path.name for path in repository_files() if OLD in str(path).lower()] == []
