"""Minimal .env loader.

Deliberately dependency-free. Accepts both the shell form that can also be
`source`d and the plain dotenv form:

    export TYPESAFE_API_KEY="ts_..."
    TYPESAFE_API_KEY=ts_...

Real environment variables always win, so `TYPESAFE_API_KEY=... python ...`
and an exported shell variable both override the file.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["load_dotenv", "find_dotenv"]


def find_dotenv(start: Path | None = None) -> Path | None:
    """Look for a .env in `start` and its parents."""
    here = (start or Path(__file__).resolve().parent).resolve()
    for directory in (here, *here.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def load_dotenv(path: Path | None = None, override: bool = False) -> dict[str, str]:
    """Load a .env into os.environ. Returns the names it set.

    Values already present in the environment are left alone unless
    `override` is set.
    """
    path = path or find_dotenv()
    if path is None or not path.is_file():
        return {}

    loaded: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue

        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            continue

        quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
        if not quoted and " #" in value:
            value = value.split(" #", 1)[0].rstrip()  # trailing comment
        value = _strip_quotes(value)

        if override or name not in os.environ:
            os.environ[name] = value
            loaded[name] = value
    return loaded
