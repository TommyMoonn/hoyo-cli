"""Predictable user config paths and platform-resolved disposable cache paths."""

from pathlib import Path

import platformdirs


def config_path(home: Path | None = None) -> Path:
    return (Path.home() if home is None else home) / ".hoyo" / "config.toml"


def credentials_path(home: Path | None = None) -> Path:
    return (Path.home() if home is None else home) / ".hoyo" / "credentials.toml"


def cache_root() -> Path:
    return platformdirs.user_cache_path("hoyo", appauthor=False)
