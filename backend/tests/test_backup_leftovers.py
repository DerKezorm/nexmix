"""A backup the process could not finish (stopped hard) leaves its reserved name behind, empty. The hourly look clears
it once it is old enough that no backup can still be writing it; a backup that is there stays."""

from __future__ import annotations

import os
import time

from app.services import backups


def test_an_empty_name_left_by_a_broken_backup_goes_after_some_hours() -> None:
    made = backups.create(kind=backups.MANUAL)
    folder = backups.folder()
    old = folder / "nexmix-2026-01-02-030405.zip"
    fresh = folder / "nexmix-2026-01-02-030406.zip"
    for path in (old, fresh):
        path.write_bytes(b"")
    long_ago = time.time() - 7 * 3600
    os.utime(old, (long_ago, long_ago))
    os.utime(folder / made.name, (long_ago, long_ago))

    backups.run_job()

    assert not old.exists(), "the empty name of a broken backup stayed"
    assert fresh.exists(), "a name a backup may still be writing went"
    assert (folder / made.name).exists() and (folder / made.name).stat().st_size > 0
