"""Explicit plaintext fallback with atomic writes and exact rollback."""

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

import tomlkit
from tomlkit.exceptions import ParseError
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from hoyo_cli.auth.credentials import Credentials
from hoyo_cli.errors import CredentialError


class PlaintextStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _read_bytes(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            raise CredentialError(
                "Unable to read plaintext credentials file."
            ) from None

    def _document(self) -> TOMLDocument:
        raw = self._read_bytes()
        if raw is None:
            document = tomlkit.document()
            document["version"] = 1
            document["accounts"] = tomlkit.table()
            return document
        try:
            document = tomlkit.parse(raw.decode("utf-8"))
            fields: dict[str, object] = document.unwrap()
            if type(fields.get("version")) is not int or fields["version"] != 1:
                raise ValueError
            accounts = fields.get("accounts")
            if not isinstance(accounts, dict):
                raise ValueError
            for entry in cast(dict[str, object], accounts).values():
                if not isinstance(entry, dict):
                    raise ValueError
                values = cast(dict[object, object], entry)
                if any(
                    not isinstance(k, str) or not isinstance(v, str)
                    for k, v in values.items()
                ):
                    raise ValueError
                Credentials(cast(dict[str, str], values))
            return document
        except (ParseError, UnicodeError, ValueError, CredentialError):
            raise CredentialError(
                "Invalid plaintext credentials file; repair it before setup."
            ) from None

    def get(self, account: str) -> Credentials | None:
        document = self._document()
        accounts = cast(dict[str, dict[str, str]], document.unwrap()["accounts"])
        values = accounts.get(account)
        return Credentials(values) if values is not None else None

    def _reject_link(self) -> None:
        if self.path.is_symlink():
            raise CredentialError(
                "Refusing to write credentials through a symbolic link. "
                "Edit the managed target directly."
            )

    def _write(self, content: bytes) -> None:
        self._reject_link()
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{self.path.name}-",
                suffix=".tmp",
                dir=self.path.parent,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                # POSIX: user-only. Windows chmod cannot establish a private ACL.
                os.chmod(temporary, 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            self._reject_link()
            os.replace(temporary, self.path)
            temporary = None
        except OSError:
            raise CredentialError(
                "Unable to save plaintext credentials safely."
            ) from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    raise CredentialError(
                        "Unable to remove temporary credentials file. "
                        "Remove .credentials.toml-*.tmp from the credentials directory."
                    ) from None

    def put(self, account: str, credentials: Credentials) -> None:
        self._reject_link()
        document = self._document()
        accounts = cast(Table, document["accounts"])
        fields = tomlkit.table()
        for name, value in credentials.fields.items():
            fields[name] = value
        accounts[account] = fields
        content = tomlkit.dumps(document)
        self._write(content.encode("utf-8"))

    def checkpoint(self, account: str) -> Callable[[], None]:
        self._reject_link()
        original = self._read_bytes()

        def restore() -> None:
            if self._read_bytes() == original:
                return
            if original is None:
                self._reject_link()
                try:
                    self.path.unlink(missing_ok=True)
                except OSError:
                    raise CredentialError(
                        "Unable to remove newly saved credentials."
                    ) from None
            else:
                self._write(original)

        return restore
