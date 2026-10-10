"""Versioned secrets in the explicitly selected OS keyring."""

from collections.abc import Callable
from typing import Protocol

from hoyo_cli.auth.credentials import (
    Credentials,
    deserialize_credentials,
    serialize_credentials,
)
from hoyo_cli.errors import BackendUnavailable


class KeyringAdapter(Protocol):
    def get_password(self, service: str, username: str, /) -> str | None: ...
    def set_password(self, service: str, username: str, password: str, /) -> None: ...
    def delete_password(self, service: str, username: str, /) -> None: ...


class KeyringStore:
    service = "hoyo-cli"

    def __init__(self, adapter: KeyringAdapter | None = None) -> None:
        self._adapter = adapter

    @property
    def adapter(self) -> KeyringAdapter:
        if self._adapter is None:
            import keyring

            self._adapter = keyring
        return self._adapter

    def _read(self, account: str) -> str | None:
        try:
            return self.adapter.get_password(self.service, account)
        except Exception:
            # Backend/plugin exceptions may contain secrets. Never expose them.
            raise BackendUnavailable(
                "OS keyring is unavailable. Retry or explicitly select plaintext."
            ) from None

    def get(self, account: str) -> Credentials | None:
        raw = self._read(account)
        return deserialize_credentials(raw) if raw is not None else None

    def _write(self, account: str, raw: str) -> None:
        try:
            self.adapter.set_password(self.service, account, raw)
        except Exception:
            raise BackendUnavailable(
                "Unable to store credentials in OS keyring."
            ) from None

    def put(self, account: str, credentials: Credentials) -> None:
        self._write(account, serialize_credentials(credentials))

    def checkpoint(self, account: str) -> Callable[[], None]:
        raw = self._read(account)

        def restore() -> None:
            if raw is not None:
                self._write(account, raw)
            elif self._read(account) is not None:
                try:
                    self.adapter.delete_password(self.service, account)
                except Exception:
                    raise BackendUnavailable(
                        "Unable to remove newly stored credentials from OS keyring."
                    ) from None

        return restore
