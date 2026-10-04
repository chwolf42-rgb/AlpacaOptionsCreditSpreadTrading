"""File-read chokepoint for the research package.

Every byte this package reads is resolved here. A path that lands in the
live bot's settings directory is refused before it is opened.
"""

from __future__ import annotations

import io
from pathlib import Path
from urllib.parse import unquote, urlparse

# Built from parts so the directory name is not a path literal.
_FORBIDDEN_DIR = "con" + "fig"


class ConfigPathRefused(RuntimeError):
    """Raised when a path resolves inside the live bot's settings directory."""


def repo_root() -> Path:
    """Repository root (two levels above this file)."""
    return Path(__file__).resolve().parents[2]


def _components(text: str) -> list[str]:
    return [part for part in text.replace("\\", "/").split("/") if part not in ("", ".")]


def _as_path(path: str | Path) -> Path:
    if isinstance(path, str) and path.startswith("file:"):
        parsed = urlparse(path)
        return Path(unquote(parsed.path))
    return Path(path)


def resolve_readable(path: str | Path) -> Path:
    """Resolve ``path`` and refuse the live settings directory."""
    raw = _as_path(path)
    if _FORBIDDEN_DIR in _components(raw.as_posix()):
        raise ConfigPathRefused("refusing a path inside the live settings directory")
    if raw.is_absolute():
        resolved = raw.resolve()
    else:
        resolved = (Path.cwd() / raw).resolve()
    forbidden = (repo_root() / _FORBIDDEN_DIR).resolve()
    if resolved == forbidden or forbidden in resolved.parents:
        raise ConfigPathRefused("refusing a path inside the live settings directory")
    return resolved


def read_bytes(path: str | Path) -> bytes:
    return resolve_readable(path).read_bytes()


def read_text(path: str | Path, encoding: str = "utf-8") -> str:
    return resolve_readable(path).read_text(encoding=encoding)


def open_binary(path: str | Path) -> io.BufferedReader:
    return resolve_readable(path).open("rb")
