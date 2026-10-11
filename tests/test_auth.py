"""Offline authentication tests use fabricated credentials only."""

import os
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import pytest

from hoyo_cli.auth.browser_import import (
    BrowserProfile,
    BrowserSession,
    SystemBrowserImporter,
    classify_browser_failure,
)
from hoyo_cli.auth.browser_worker import load_selected
from hoyo_cli.auth.credentials import Credentials, open_store, parse_cookie_header
from hoyo_cli.auth.keyring_backend import KeyringStore
from hoyo_cli.auth.plaintext_backend import PlaintextStore
from hoyo_cli.errors import BackendUnavailable, BrowserImportError, CredentialError


class FakeBrowserAdapter:
    def __init__(self, denied: bool = False) -> None:
        self.denied = denied
        self.calls: list[str] = []

    def list_sessions(self) -> tuple[BrowserSession, ...]:
        return (BrowserSession("chrome", "Chrome"), BrowserSession("edge", "Edge"))

    def list_profiles(self, source_id: str) -> tuple[BrowserProfile, ...]:
        return (BrowserProfile(source_id, source_id, "Test profile"),)

    def read(self, source_id: str) -> Credentials:
        self.calls.append(source_id)
        if source_id not in ("chrome", "edge"):
            raise BrowserImportError("Unsupported browser.")
        if self.denied:
            raise BrowserImportError("Access denied.")
        return parse_cookie_header("ltuid=123; ltoken=test-secret-123")


def test_two_browser_sessions_are_both_listed() -> None:
    adapter = FakeBrowserAdapter()
    importer = SystemBrowserImporter(adapter)
    sessions = importer.list_sessions()
    assert [s.source_id for s in sessions] == ["chrome", "edge"]
    assert adapter.calls == []
    assert "test-secret-123" not in repr(sessions)
    assert importer.import_session("edge").fields["ltuid"] == "123"
    with pytest.raises(BrowserImportError):
        importer.import_session("unsupported")


def test_permission_denied_is_recoverable() -> None:
    adapter = FakeBrowserAdapter(True)
    importer = SystemBrowserImporter(adapter)
    assert [s.source_id for s in importer.list_sessions()] == ["chrome", "edge"]
    assert adapter.calls == []
    with pytest.raises(BrowserImportError):
        importer.import_session("edge")


def test_browser_loader_is_scoped_and_cookie_values_never_logged() -> None:
    from http.cookiejar import Cookie, CookieJar

    jar = CookieJar()
    for domain, name, value in [
        (".hoyolab.com", "ltuid", "123"),
        (".hoyolab.com", "ltoken", "test-secret-123"),
        ("evil-hoyolab.com", "ltoken", "test-evil"),
        (".hoyolab.com", "analytics", "test-analytics"),
    ]:
        jar.set_cookie(
            Cookie(
                0,
                name,
                value,
                None,
                False,
                domain,
                True,
                True,
                "/",
                True,
                True,
                None,
                True,
                None,
                None,
                {},
            )
        )
    calls: list[str] = []

    def loader(
        *, domain_name: str, cookie_file: str | None, key_file: str | None
    ) -> CookieJar:
        calls.append(domain_name)
        assert cookie_file is None and key_file is None
        return jar

    from types import ModuleType

    library = ModuleType("fake_browser_cookie3")
    library.__dict__["chrome"] = loader
    with patch(
        "hoyo_cli.auth.browser_worker.importlib.import_module", return_value=library
    ):
        credentials = load_selected("chrome", None, None)
    assert calls == ["hoyolab.com"]
    assert dict(credentials.fields) == {"ltuid": "123", "ltoken": "test-secret-123"}


@pytest.mark.parametrize(
    ("failure", "code", "hint"),
    [
        (FileNotFoundError("private-path"), "browser_profile_missing", "profile"),
        (PermissionError("private-path"), "browser_access_denied", "manual"),
        (RuntimeError("unexpected-secret"), "browser_unknown", "manual"),
    ],
)
def test_browser_failure_causes_are_safe(
    failure: Exception, code: str, hint: str
) -> None:
    error = classify_browser_failure(failure)
    assert error.code.value == code
    assert error.hint and hint in error.hint.lower()
    assert "private-path" not in str(error)
    assert "unexpected-secret" not in str(error)


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[service, username] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[service, username]


def test_keyring_roundtrip_mocked_and_rollback() -> None:
    backend = FakeKeyring()
    store = KeyringStore(backend)
    credentials = parse_cookie_header("ltuid=123; ltoken=test-secret-123")
    assert store.get("Exact.Account") is None
    undo = store.checkpoint("Exact.Account")
    store.put("Exact.Account", credentials)
    assert store.get("Exact.Account") == credentials
    assert ("hoyo-cli", "Exact.Account") in backend.values
    undo()
    assert store.get("Exact.Account") is None


def test_missing_keyring_is_classified() -> None:
    backend = FakeKeyring()
    with patch.object(
        backend, "get_password", side_effect=RuntimeError("test-secret-123")
    ):
        with pytest.raises(BackendUnavailable) as caught:
            KeyringStore(backend).get("main")
    assert "test-secret-123" not in str(caught.value)


def test_plaintext_roundtrip_separate_file_and_exact_rollback(tmp_path: Path) -> None:
    path = tmp_path / "credentials.toml"
    store = PlaintextStore(path)
    credentials = parse_cookie_header("ltuid=123; ltoken=test-secret-123")
    undo = store.checkpoint("main")
    store.put("main", credentials)
    assert store.get("main") == credentials
    assert "version = 1" in path.read_text()
    assert "[accounts.main]" in path.read_text()
    assert not (tmp_path / "config.toml").exists()
    undo()
    assert not path.exists()
    original = (
        b"# keep\r\nversion = 1\r\n[accounts.alt]\r\n"
        b'ltuid="123"\r\nltoken="test-other"\r\n'
    )
    path.write_bytes(original)
    undo = store.checkpoint("main")
    store.put("main", credentials)
    undo()
    assert path.read_bytes() == original


def test_plaintext_permissions_and_write_failure(tmp_path: Path) -> None:
    path = tmp_path / "credentials.toml"
    store = PlaintextStore(path)
    credentials = parse_cookie_header("ltuid=123; ltoken=test-secret-123")
    with patch("hoyo_cli.auth.plaintext_backend.os.chmod", wraps=os.chmod) as chmod:
        store.put("main", credentials)
    assert any(call.args[1] == 0o600 for call in chmod.call_args_list)
    original = path.read_bytes()
    with patch(
        "hoyo_cli.auth.plaintext_backend.os.replace",
        side_effect=OSError("test-secret-123"),
    ):
        with pytest.raises(CredentialError) as caught:
            store.put("main", parse_cookie_header("ltuid=123; ltoken=test-new"))
    assert "test-secret-123" not in str(caught.value)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "content", ["broken [", "version=2", 'version=1\n[accounts.main]\nltuid="123"']
)
def test_malformed_plaintext_is_never_replaced(tmp_path: Path, content: str) -> None:
    path = tmp_path / "credentials.toml"
    path.write_text(content)
    with pytest.raises(CredentialError):
        PlaintextStore(path).put(
            "main", parse_cookie_header("ltuid=123; ltoken=test-secret-123")
        )
    assert path.read_text() == content


def test_no_implicit_backend_migration(tmp_path: Path) -> None:
    path = tmp_path / "credentials.toml"
    path.write_text("private-marker")
    assert isinstance(open_store("keyring", path), KeyringStore)
    assert isinstance(open_store("plaintext", path), PlaintextStore)
    assert path.read_text() == "private-marker"
    with pytest.raises(CredentialError):
        open_store("automatic", path)


@pytest.mark.parametrize(
    "raw",
    [
        "test-secret-123",
        '{"version":2}',
        '{"version":true}',
        '{"version":1,"fields":{"ltuid":123,"ltoken":"test-secret-123"}}',
    ],
)
def test_invalid_stored_keyring_credentials_are_redacted(raw: str) -> None:
    adapter = FakeKeyring()
    adapter.values[("hoyo-cli", "main")] = raw
    with pytest.raises(CredentialError) as caught:
        KeyringStore(adapter).get("main")
    assert "test-secret-123" not in str(caught.value)


@pytest.mark.parametrize("operation", ["chmod", "fsync"])
def test_plaintext_permission_or_flush_failure_preserves_original(
    tmp_path: Path, operation: str
) -> None:
    path = tmp_path / "credentials.toml"
    store = PlaintextStore(path)
    credentials = parse_cookie_header("ltuid=123; ltoken=test-original")
    store.put("main", credentials)
    original = path.read_bytes()
    with patch(
        f"hoyo_cli.auth.plaintext_backend.os.{operation}",
        side_effect=OSError("test-secret-123"),
    ):
        with pytest.raises(CredentialError):
            store.put("main", parse_cookie_header("ltuid=123; ltoken=test-secret-123"))
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_plaintext_refuses_detected_symlink(tmp_path: Path) -> None:
    path = tmp_path / "credentials.toml"
    with patch.object(Path, "is_symlink", return_value=True):
        with pytest.raises(CredentialError, match="symbolic link"):
            PlaintextStore(path).put(
                "main", parse_cookie_header("ltuid=123; ltoken=test-secret-123")
            )
    assert not path.exists()


def test_internal_cookie_header_fixture_accepts_semicolon_pairs() -> None:
    credentials = parse_cookie_header(
        "analytics=discard; ltuid=123; ltoken=test-secret-123==; ignored=value"
    )
    assert dict(credentials.fields) == {"ltuid": "123", "ltoken": "test-secret-123=="}


@pytest.mark.parametrize(
    "raw",
    [
        "ltuid=123",
        "ltoken=test-secret-123",
        "ltuid=0; ltoken=x",
        "ltuid=123; ltoken=x; ltoken=y",
        "ltuid=123; account_id=456; ltoken=x",
        "ltuid=123; ltoken=",
        "ltuid=123; ltoken=x\nAuthorization: test-secret-123",
    ],
)
def test_missing_or_ambiguous_cookie_is_rejected_without_echo(raw: str) -> None:
    with pytest.raises(CredentialError) as caught:
        parse_cookie_header(raw)
    assert raw not in str(caught.value)
    assert "test-secret-123" not in str(caught.value)
    assert caught.value.exit_code == 3


def test_v2_fields_are_preserved() -> None:
    credentials = parse_cookie_header(
        "ltuid_v2=123; ltoken_v2=test-secret-123; ltmid_v2=test-mid; "
        "account_id_v2=123; cookie_token_v2=test-cookie; account_mid_v2=test-mid"
    )
    assert len(credentials.fields) == 6


def test_credential_repr_masks_secrets_and_snapshot_is_immutable() -> None:
    fields = {"ltuid": "123", "ltoken": "test-secret-123"}
    credentials = Credentials(fields)
    fields["ltoken"] = "changed"
    assert credentials.fields["ltoken"] == "test-secret-123"
    assert "test-secret-123" not in repr(credentials)
    assert "123" not in str(credentials)
    with pytest.raises(FrozenInstanceError):
        field_name = "fields"
        setattr(credentials, field_name, {})
