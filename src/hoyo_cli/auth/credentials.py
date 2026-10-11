"""Validated secret snapshots; no values enter repr or public error messages."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast

from hoyo_cli.errors import CredentialError, ErrorCode

# Verified against genshin 1.7.31 CookieManager and utility.fs.
COOKIE_FIELDS = frozenset(
    {
        "ltuid",
        "ltoken",
        "account_id",
        "cookie_token",
        "ltuid_v2",
        "ltoken_v2",
        "ltmid_v2",
        "account_id_v2",
        "cookie_token_v2",
        "account_mid_v2",
    }
)
_IDENTIFIERS = ("ltuid", "account_id", "ltuid_v2", "account_id_v2")


def _validate_fields(values: Mapping[str, object]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for name, value in values.items():
        if (
            name not in COOKIE_FIELDS
            or not isinstance(value, str)
            or not value
            or any(ord(char) < 33 or ord(char) == 127 for char in value)
            or ";" in value
        ):
            raise CredentialError("Invalid authentication cookie fields.")
        fields[name] = value
    return fields


@dataclass(frozen=True, repr=False)
class Credentials:
    fields: Mapping[str, str]

    def __post_init__(self) -> None:
        fields = _validate_fields(self.fields)
        identifiers = [fields[name] for name in _IDENTIFIERS if name in fields]
        if not identifiers or any(
            not value.isascii() or not value.isdecimal() or int(value) <= 0
            for value in identifiers
        ):
            raise CredentialError(
                "A consistent positive HoYoLAB account ID is required."
            )
        if len(set(identifiers)) != 1:
            raise CredentialError(
                "A consistent positive HoYoLAB account ID is required.",
                code=ErrorCode.CREDENTIALS_CONFLICT,
            )
        legacy = "ltoken" in fields and any(
            name in fields for name in ("ltuid", "account_id")
        )
        modern = "ltoken_v2" in fields and any(
            name in fields for name in ("ltuid_v2", "account_id_v2")
        )
        if not (legacy or modern):
            raise CredentialError(
                "Missing authentication cookies: provide ltuid and ltoken, "
                "or ltuid_v2 and ltoken_v2, from a full HoYoLAB Cookie header."
            )
        object.__setattr__(self, "fields", MappingProxyType(fields))

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"


def parse_cookie_header(raw: str) -> Credentials:
    fields: dict[str, str] = {}
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise CredentialError("Invalid Cookie header; use semicolon-separated pairs.")
    for segment in raw.split(";"):
        if not segment.strip():
            continue
        name, separator, value = segment.strip().partition("=")
        name = name.strip()
        if not separator or not name:
            raise CredentialError(
                "Invalid Cookie header; use semicolon-separated pairs."
            )
        if name in COOKIE_FIELDS:
            if name in fields:
                raise CredentialError("Duplicate authentication cookies are ambiguous.")
            fields[name] = value.strip()
    return Credentials(fields)


def serialize_credentials(credentials: Credentials) -> str:
    return json.dumps({"version": 1, "fields": dict(credentials.fields)})


def deserialize_credentials(raw: str) -> Credentials:
    try:
        payload: object = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError
        entries = cast(dict[str, object], payload)
        if type(entries.get("version")) is not int or entries["version"] != 1:
            raise ValueError
        fields = entries.get("fields")
        if not isinstance(fields, dict):
            raise ValueError
        values = cast(dict[object, object], fields)
        if any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()
        ):
            raise ValueError
        return Credentials(cast(dict[str, str], values))
    except (ValueError, TypeError, CredentialError):
        raise CredentialError(
            "Stored credentials are invalid; repair or remove the keyring entry "
            "before running hoyo account edit NAME."
        ) from None


class CredentialStore(Protocol):
    def get(self, account: str) -> Credentials | None: ...
    def put(self, account: str, credentials: Credentials) -> None: ...
    def checkpoint(self, account: str) -> Callable[[], None]:
        """Capture an exact rollback point owned by this backend."""
        ...


def open_store(backend: str, path: Path) -> CredentialStore:
    if backend == "keyring":
        from hoyo_cli.auth.keyring_backend import KeyringStore

        return KeyringStore()
    if backend == "plaintext":
        from hoyo_cli.auth.plaintext_backend import PlaintextStore

        return PlaintextStore(path)
    raise CredentialError("Unknown credential backend; use keyring or plaintext.")
