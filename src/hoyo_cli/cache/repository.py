"""Per-account/game cache I/O with bounded age and atomic replacement."""

import hashlib
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from hoyo_cli.cache.models import CacheEntry, decode_entry, encode_entry, require_uid
from hoyo_cli.models.common import GAME_IDS, GameId, require_aware

MAX_CACHE_AGE = timedelta(hours=3)


def _path(cache_root: Path, account: str, game: GameId) -> Path:
    if not account or game not in GAME_IDS:
        raise ValueError("Invalid cache identity.")
    # Exact account names can contain separators, reserved Windows names, or
    # differ only by case. A digest gives each one a portable directory identity.
    identity = hashlib.sha256(account.encode("utf-8")).hexdigest()
    directory = cache_root / identity
    path = directory / f"{game}.json"
    if directory.resolve() != cache_root.resolve() / identity or path.is_symlink():
        raise ValueError("Refusing redirected cache paths.")
    return path


def read_eligible(
    cache_root: Path, account: str, game: GameId, *, uid: int, now: datetime
) -> CacheEntry | None:
    require_aware(now)
    try:
        require_uid(uid)
        entry = decode_entry(
            _path(cache_root, account, game).read_text(encoding="utf-8")
        )
        if entry.account != account or entry.game != game or entry.uid != uid:
            return None
        age = now - entry.fetched_at
        return entry if timedelta(0) <= age <= MAX_CACHE_AGE else None
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        # Cache is disposable. Its failure must never replace the network error.
        return None


def write_cache(cache_root: Path, entry: CacheEntry) -> None:
    content = encode_entry(entry)
    path = _path(cache_root, entry.account, entry.game)
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _path(cache_root, entry.account, entry.game)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
