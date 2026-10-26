"""The program's version, for the title bar.

Frozen (PyInstaller): build.py writes slogicpt/_version.py before
freezing -- the release tag on CI (v0.4.8), else `git describe`.
From source: `git describe --tags --match "v*" --dirty` of the checkout,
e.g. v0.4.7-3-g0093445 (3 commits after v0.4.7) or ...-dirty.
"""
from __future__ import annotations

import subprocess
import sys
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def describe(repo: Path = REPO) -> str | None:
    """`git describe` of `repo`, or None (no git, not a checkout, no tag)."""
    from .procutil import hidden_kwargs
    try:
        out = subprocess.run(["git", "describe", "--tags", "--match", "v*", "--dirty"],
                             cwd=str(repo), capture_output=True, text=True, timeout=5,
                             **hidden_kwargs())
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


@lru_cache(maxsize=1)
def get() -> str:
    if not getattr(sys, "frozen", False):
        found = describe()
        if found:
            return found
    try:
        from ._version import VERSION
        return VERSION
    except ImportError:
        return "dev"


if __name__ == "__main__":
    print(get())
