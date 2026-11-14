"""The libraries that read what people upload are versions without the known holes.

Pillow decodes every profile picture an account uploads (JPEG, PNG, GIF, BMP, WebP, AVIF, HEIC through pillow-heif).
Pillow 12 has dozens of known holes in its decoders (heap overflows, memory exhaustion), fixed in 13.0.0; the image and
the tests must use 13 or later, and the version that runs is the one pinned."""

from __future__ import annotations

import re
from pathlib import Path

import PIL

REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"


def _pinned(name: str) -> str:
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        found = re.fullmatch(rf"{re.escape(name)}==([0-9][0-9.]*)", line.strip(), flags=re.IGNORECASE)
        if found:
            return found.group(1)
    raise AssertionError(f"{name} is not pinned in requirements.txt")


def _major(version: str) -> int:
    return int(version.split(".")[0])


def test_pillow_is_a_version_without_the_known_holes() -> None:
    pinned = _pinned("pillow")
    assert _major(pinned) >= 13, f"pillow {pinned} has known holes in its decoders; 13.0.0 fixes them"
    assert PIL.__version__ == pinned, f"pillow {PIL.__version__} runs, {pinned} is pinned"
