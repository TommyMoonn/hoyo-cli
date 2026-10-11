"""Offline profile selection, isolated loaders, and Windows lock evidence."""

import asyncio
import base64
import ctypes
import importlib
import io
import json
import sqlite3
import subprocess
import sys
import warnings
from collections.abc import Generator
from contextlib import contextmanager
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from conftest import TEST_COOKIE, AcceptanceEnvironment

from hoyo_cli.auth import browser_worker
from hoyo_cli.auth.browser_import import (
    BrowserCookieAdapter,
    BrowserProfile,
    BrowserSession,
    SystemBrowserImporter,
    classify_browser_failure,
    read_browser_profile,
)
from hoyo_cli.auth.credentials import (
    deserialize_credentials,
    manual_credentials,
    parse_cookie_header,
)
from hoyo_cli.cli import main
from hoyo_cli.commands import init
from hoyo_cli.errors import BrowserImportError, ErrorCode


def cookie_jar(entries: list[tuple[str, str, str]]) -> CookieJar:
    jar = CookieJar()
    for domain, name, value in entries:
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
    return jar


def windows_adapter(tmp_path: Path) -> BrowserCookieAdapter:
    return BrowserCookieAdapter(
        platform="win32",
        environment={
            "LOCALAPPDATA": str(tmp_path / "Local"),
            "APPDATA": str(tmp_path / "Roaming"),
        },
    )


@pytest.mark.parametrize(
    ("browser", "location"),
    [
        ("chrome", "Google/Chrome/User Data"),
        ("edge", "Microsoft/Edge/User Data"),
        ("brave", "BraveSoftware/Brave-Browser/User Data"),
        ("chromium", "Chromium/User Data"),
    ],
)
def test_windows_choices_and_profiles_do_not_read_cookies(
    tmp_path: Path, browser: str, location: str
) -> None:
    root = tmp_path / "Local" / location
    for name in ("Default", "Profile 2"):
        (root / name / "Network").mkdir(parents=True)
        (root / name / "Network/Cookies").touch()
    (root / "Unrelated").mkdir()
    importer = SystemBrowserImporter(windows_adapter(tmp_path))
    with patch("hoyo_cli.auth.browser_import.read_browser_profile") as reader:
        sessions = importer.list_sessions()
        assert [s.source_id for s in sessions] == [browser]
        assert sessions[0].supports_profiles
        profiles = importer.list_profiles(browser)
        assert [p.label for p in profiles] == ["Stable / Default", "Stable / Profile 2"]
        reader.assert_not_called()
        reader.return_value = parse_cookie_header("ltuid=123; ltoken=test-secret-123")
        importer.import_session(profiles[1].source_id)
        reader.assert_called_once_with(profiles[1])
    assert profiles[1].cookie_file == root / "Profile 2/Network/Cookies"
    assert profiles[1].key_file == root / "Local State"
    assert str(tmp_path) not in repr(profiles)


def test_firefox_uses_registered_relative_and_absolute_profiles(tmp_path: Path) -> None:
    root = tmp_path / "Roaming/Mozilla/Firefox"
    root.mkdir(parents=True)
    # Local Firefox stores cache metadata, not the registered profiles.ini.
    (tmp_path / "Local/Mozilla/Firefox").mkdir(parents=True)
    external = tmp_path / "custom-profile"
    (root / "Profiles/one").mkdir(parents=True)
    (root / "Profiles/one/cookies.sqlite").touch()
    external.mkdir()
    (external / "cookies.sqlite").touch()
    (root / "profiles.ini").write_text(
        "[Profile0]\nName=Primary\nIsRelative=1\nPath=Profiles/one\n"
        f"[Profile1]\nName=Other\nIsRelative=0\nPath={external.as_posix()}\n",
        encoding="utf-8",
    )
    profiles = windows_adapter(tmp_path).list_profiles("firefox")
    assert [p.cookie_file for p in profiles] == [
        external / "cookies.sqlite",
        root / "Profiles/one/cookies.sqlite",
    ]
    assert all(p.key_file is None for p in profiles)


def test_opera_uses_its_own_layout_and_legacy_cookie_location(tmp_path: Path) -> None:
    root = tmp_path / "Roaming/Opera Software/Opera Stable"
    root.mkdir(parents=True)
    (root / "Cookies").touch()
    profiles = windows_adapter(tmp_path).list_profiles("opera")
    assert len(profiles) == 1
    assert profiles[0].cookie_file == root / "Cookies"
    assert profiles[0].key_file == root / "Local State"


@pytest.mark.parametrize("location", ["", "Microsoft/Edge/User Data"])
def test_missing_windows_profile_has_safe_recovery(
    tmp_path: Path, location: str
) -> None:
    if location:
        (tmp_path / "Local" / location).mkdir(parents=True)
    adapter = windows_adapter(tmp_path)
    with pytest.raises(BrowserImportError) as caught:
        adapter.list_profiles("edge")
    assert caught.value.code == ErrorCode.BROWSER_PROFILE_MISSING


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_unverified_profile_capability_is_explicit(platform: str) -> None:
    adapter = BrowserCookieAdapter(platform=platform, environment={})
    assert all(not s.supports_profiles for s in adapter.list_sessions())
    (profile,) = adapter.list_profiles("firefox")
    assert profile.label == "Library-selected profile"
    assert profile.cookie_file is None


def test_unsupported_platform_and_unselected_tokens_are_rejected(
    tmp_path: Path,
) -> None:
    assert (
        BrowserCookieAdapter(platform="unsupported", environment={}).list_sessions()
        == ()
    )
    with pytest.raises(BrowserImportError) as caught:
        windows_adapter(tmp_path).read("brave:0")
    assert caught.value.code == ErrorCode.BROWSER_UNSUPPORTED


@pytest.mark.parametrize(
    ("entries", "code"),
    [
        ([], ErrorCode.BROWSER_LOGIN_MISSING),
        ([(".hoyolab.com", "ltuid", "123")], ErrorCode.BROWSER_LOGIN_MISSING),
        (
            [
                (".hoyolab.com", "ltuid", "123"),
                (".hoyolab.com", "ltoken", "test-secret-123"),
                ("www.hoyolab.com", "ltoken", "other-secret"),
            ],
            ErrorCode.BROWSER_COOKIES_CONFLICT,
        ),
        (
            [
                (".hoyolab.com", "ltuid", "123"),
                (".hoyolab.com", "account_id_v2", "456"),
                (".hoyolab.com", "ltoken", "test-secret-123"),
            ],
            ErrorCode.BROWSER_COOKIES_CONFLICT,
        ),
    ],
)
def test_cookie_failures_have_distinct_safe_causes(
    entries: list[tuple[str, str, str]], code: ErrorCode
) -> None:
    with pytest.raises(BrowserImportError) as caught:
        browser_worker.extract_credentials(cookie_jar(entries))
    assert caught.value.code == code
    assert caught.value.exit_code == 3
    assert "secret" not in str(caught.value)


def test_expired_and_unrelated_cookies_are_not_credentials() -> None:
    jar = cookie_jar(
        [
            (".hoyolab.com", "ltuid", "123"),
            (".hoyolab.com", "ltoken", "test-secret-123"),
            ("evil-hoyolab.com", "ltoken", "other-secret"),
            (".hoyolab.com", "analytics", "analytics-secret"),
        ]
    )
    for cookie in jar:
        if cookie.name == "ltoken" and cookie.domain == ".hoyolab.com":
            cookie.expires = 1
    with pytest.raises(BrowserImportError) as caught:
        browser_worker.extract_credentials(jar)
    assert caught.value.code == ErrorCode.BROWSER_LOGIN_MISSING


@pytest.mark.parametrize(
    ("diagnostic", "code"),
    [
        ("Unable to get key for cookie decryption", ErrorCode.BROWSER_DECRYPTION),
        ("Unable to read database file", ErrorCode.BROWSER_STORAGE),
        ("unknown locked DPAPI private-path", ErrorCode.BROWSER_UNKNOWN),
    ],
)
def test_locked_dependency_diagnostics_are_classified_exactly(
    diagnostic: str, code: ErrorCode
) -> None:
    browser_cookie3 = importlib.import_module("browser_cookie3")

    assert (
        classify_browser_failure(browser_cookie3.BrowserCookieError(diagnostic)).code
        == code
    )


def test_winerror_32_is_a_lock_and_access_denied_is_not() -> None:
    error = PermissionError("private-path and secret")
    error.winerror = 32
    failure = classify_browser_failure(error)
    assert failure.code == ErrorCode.BROWSER_LOCKED
    assert failure.hint and "Close the selected browser fully" in failure.hint
    assert (
        classify_browser_failure(PermissionError("locked")).code
        == ErrorCode.BROWSER_ACCESS_DENIED
    )


def test_worker_contains_warnings_and_python_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jar = cookie_jar(
        [
            (".hoyolab.com", "ltuid", "123"),
            (".hoyolab.com", "ltoken", "test-secret-123"),
        ]
    )
    library = ModuleType("fake_browser_cookie3")
    library.__dict__["shadowcopy"] = object()
    calls: list[str] = []

    def loader(
        *, domain_name: str, cookie_file: str | None, key_file: str | None
    ) -> CookieJar:
        assert library.__dict__["shadowcopy"] is None
        calls.append(domain_name)
        assert cookie_file is None and key_file is None
        warnings.warn("diagnostic-private-path", stacklevel=1)
        print("diagnostic-private-path")
        print("diagnostic-private-path", file=sys.stderr)
        return jar

    library.__dict__["chrome"] = loader
    output, errors = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"browser":"chrome"}'))
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", errors)
    with patch.object(browser_worker.importlib, "import_module", return_value=library):
        browser_worker.main()
    payload = json.loads(output.getvalue())
    assert deserialize_credentials(payload["credentials"]).fields["ltuid"] == "123"
    assert calls == ["hoyolab.com"]
    assert errors.getvalue() == ""
    assert "diagnostic-private-path" not in output.getvalue()
    assert sys.stdout is output and sys.stderr is errors


@pytest.mark.parametrize(
    "failure", [KeyboardInterrupt, SystemExit, asyncio.CancelledError]
)
def test_worker_control_signals_are_not_classified(
    monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"browser":"chrome"}'))
    with patch.object(browser_worker, "load_selected", side_effect=failure):
        with pytest.raises(failure):
            browser_worker.main()


def test_selected_loader_receives_exact_cookie_and_key_paths(tmp_path: Path) -> None:
    database, key = tmp_path / "Cookies", tmp_path / "Local State"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB, expires_utc INTEGER)"
    )
    connection.close()
    jar = cookie_jar(
        [
            (".hoyolab.com", "ltuid", "123"),
            (".hoyolab.com", "ltoken", "test-secret-123"),
        ]
    )
    library = ModuleType("fake_browser_cookie3")
    seen: list[tuple[str, str | None, str | None]] = []

    def loader(
        *, domain_name: str, cookie_file: str | None, key_file: str | None
    ) -> CookieJar:
        seen.append((domain_name, cookie_file, key_file))
        assert library.__dict__["shadowcopy"] is None
        return jar

    library.__dict__["chrome"] = loader
    with patch.object(browser_worker.importlib, "import_module", return_value=library):
        browser_worker.load_selected("chrome", str(database), str(key))
    assert seen == [("hoyolab.com", str(database), str(key))]


def test_missing_database_is_classified_by_isolated_reader(tmp_path: Path) -> None:
    with pytest.raises(BrowserImportError) as caught:
        read_browser_profile(
            BrowserProfile(
                "firefox:0",
                "firefox",
                "Synthetic",
                tmp_path / "missing.sqlite",
            )
        )
    assert caught.value.code == ErrorCode.BROWSER_PROFILE_MISSING
    assert str(tmp_path) not in str(caught.value)


def test_unavailable_dependency_loader_is_recoverable() -> None:
    with patch.object(
        browser_worker.importlib,
        "import_module",
        return_value=ModuleType("missing-loader"),
    ):
        with pytest.raises(BrowserImportError) as caught:
            browser_worker.load_selected("chrome", None, None)
    assert caught.value.code == ErrorCode.BROWSER_UNSUPPORTED


def test_native_diagnostics_are_contained_by_process_pipes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = """
import os, sys, warnings
from hoyo_cli.auth import browser_worker
from hoyo_cli.auth.credentials import parse_cookie_header
def noisy(*args):
    warnings.warn("native-private-path", stacklevel=1)
    print("python-private-path")
    print("python-private-path", file=sys.stderr)
    os.write(2, b"native-private-path\\n")
    return parse_cookie_header("ltuid=123; ltoken=test-secret-123")
browser_worker.load_selected = noisy
browser_worker.main()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        input='{"browser":"chrome"}',
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert result.returncode == 0
    assert "native-private-path" in result.stderr
    with patch("hoyo_cli.auth.browser_import.subprocess.run", return_value=result):
        credentials = read_browser_profile(BrowserProfile("chrome:0", "chrome", "Test"))
    assert credentials.fields["ltuid"] == "123"
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("mode", ["noise", "exit", "timeout", "invalid-code"])
def test_worker_protocol_failures_never_expose_output(mode: str) -> None:
    result = subprocess.CompletedProcess(
        ["fake"],
        1 if mode == "exit" else 0,
        '{"error":"credentials_invalid"}'
        if mode == "invalid-code"
        else "private-path secret",
        "native private-path secret",
    )
    with patch(
        "hoyo_cli.auth.browser_import.subprocess.run", return_value=result
    ) as run:
        if mode == "timeout":
            run.side_effect = subprocess.TimeoutExpired("fake", 30, output="secret")
        with pytest.raises(BrowserImportError) as caught:
            read_browser_profile(BrowserProfile("chrome:0", "chrome", "Test"))
    assert caught.value.code == (
        ErrorCode.BROWSER_TIMEOUT if mode == "timeout" else ErrorCode.BROWSER_UNKNOWN
    )
    assert "secret" not in str(caught.value) and "private-path" not in str(caught.value)


def firefox_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE moz_cookies (host TEXT, path TEXT, isSecure INTEGER, "
            "expiry INTEGER, name TEXT, value TEXT, isHttpOnly INTEGER)"
        )
        connection.executemany(
            "INSERT INTO moz_cookies VALUES (?, '/', 1, 4102444800, ?, ?, 1)",
            [
                (".hoyolab.com", "ltuid", "123"),
                (".hoyolab.com", "ltoken", "test-secret-123"),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def test_real_isolated_reader_uses_synthetic_firefox_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "cookies.sqlite"
    firefox_database(database)
    # The locked reader prints a diagnostic for malformed session metadata.
    (tmp_path / "sessionstore.js").write_text("{broken", encoding="utf-8")
    credentials = read_browser_profile(
        BrowserProfile("firefox:0", "firefox", "Test", database)
    )
    assert dict(credentials.fields) == {"ltuid": "123", "ltoken": "test-secret-123"}
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@contextmanager
def exclusive_windows_file(database: Path) -> Generator[None]:
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(database), 0x80000000, 0, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value, ctypes.get_last_error()
    try:
        yield
    finally:
        assert close(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows file sharing")
def test_real_windows_sharing_violation_is_classified_in_child(tmp_path: Path) -> None:
    database = tmp_path / "cookies.sqlite"
    firefox_database(database)
    with exclusive_windows_file(database):
        with pytest.raises(BrowserImportError) as caught:
            read_browser_profile(
                BrowserProfile("brave:0", "brave", "Synthetic", database)
            )
        assert caught.value.code == ErrorCode.BROWSER_LOCKED
    # Closing the synthetic lock permits the real isolated reader to run again.
    credentials = read_browser_profile(
        BrowserProfile("firefox:0", "firefox", "Synthetic", database)
    )
    assert dict(credentials.fields) == {"ltuid": "123", "ltoken": "test-secret-123"}


def synthetic_windows_key() -> str:
    """Protect a disposable Chromium key, never touching browser/OS secret stores."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    data = (ctypes.c_byte * 32)(*range(32))
    source = Blob(32, data)
    target = Blob()
    protect = ctypes.WinDLL("crypt32", use_last_error=True).CryptProtectData
    protect.argtypes = [
        ctypes.POINTER(Blob),
        wintypes.LPCWSTR,
        ctypes.POINTER(Blob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    protect.restype = wintypes.BOOL
    assert protect(
        ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)
    ), ctypes.get_last_error()
    free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
    free.argtypes = [wintypes.HLOCAL]
    free.restype = wintypes.HLOCAL
    try:
        protected = ctypes.string_at(target.data, target.size)
        return base64.b64encode(b"DPAPI" + protected).decode("ascii")
    finally:
        assert not free(ctypes.cast(target.data, wintypes.HLOCAL))


def synthetic_brave_profiles(tmp_path: Path) -> tuple[SystemBrowserImporter, Path]:
    root = tmp_path / "Local/BraveSoftware/Brave-Browser/User Data"
    for name in ("Default", "Profile 2"):
        database = root / name / "Network/Cookies"
        database.parent.mkdir(parents=True)
        connection = sqlite3.connect(database)
        try:
            connection.execute("CREATE TABLE meta (key TEXT, value TEXT)")
            connection.execute("INSERT INTO meta VALUES ('version', '23')")
            connection.execute(
                "CREATE TABLE cookies (host_key TEXT, path TEXT, is_secure INTEGER, "
                "expires_utc INTEGER, name TEXT, value TEXT, encrypted_value BLOB, "
                "is_httponly INTEGER)"
            )
            # Only the Default profile has a synthetic login. Profile 2 is empty.
            if name == "Default":
                connection.executemany(
                    "INSERT INTO cookies VALUES "
                    "('.hoyolab.com', '/', 1, 0, ?, ?, X'', 1)",
                    [("ltuid", "123"), ("ltoken", "acceptance-secret-not-real")],
                )
            connection.commit()
        finally:
            connection.close()
    (root / "Local State").write_text(
        json.dumps({"os_crypt": {"encrypted_key": synthetic_windows_key()}}),
        encoding="utf-8",
    )
    return SystemBrowserImporter(
        windows_adapter(tmp_path)
    ), root / "Default/Network/Cookies"


@pytest.mark.skipif(
    sys.platform != "win32", reason="Requires Windows DPAPI/file sharing"
)
def test_windows_brave_profiles_open_lock_closed_and_missing_login(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    importer, database = synthetic_brave_profiles(tmp_path)
    assert importer.list_sessions() == (BrowserSession("brave", "Brave", True),)
    profiles = importer.list_profiles("brave")
    assert [profile.label for profile in profiles] == [
        "Stable / Default",
        "Stable / Profile 2",
    ]
    with exclusive_windows_file(database):
        with pytest.raises(BrowserImportError) as caught:
            importer.import_session(profiles[0].source_id)
        assert caught.value.code == ErrorCode.BROWSER_LOCKED
        assert "Close the selected browser fully" in (caught.value.hint or "")
    credentials = importer.import_session(profiles[0].source_id)
    assert credentials == parse_cookie_header(TEST_COOKIE)
    with pytest.raises(BrowserImportError) as caught:
        importer.import_session(profiles[1].source_id)
    assert caught.value.code == ErrorCode.BROWSER_LOGIN_MISSING
    assert str(tmp_path) not in caught.value.message
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows DPAPI")
def test_real_windows_worker_reports_selected_app_bound_cookies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    importer, database = synthetic_brave_profiles(tmp_path)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE cookies SET value = '', encrypted_value = ? WHERE name = 'ltoken'",
            (b"v20" + b"opaque-protected-data" * 4,),
        )
        connection.commit()
    finally:
        connection.close()
    profiles = importer.list_profiles("brave")
    with pytest.raises(BrowserImportError) as caught:
        importer.import_session(profiles[0].source_id)
    assert caught.value.code == ErrorCode.BROWSER_ENCRYPTION_UNSUPPORTED
    assert "app-bound encryption" in caught.value.message
    assert "ltuid_v2 / ltoken_v2 manually (recommended)" in (caught.value.hint or "")
    assert "opaque" not in str(caught.value) and str(tmp_path) not in str(caught.value)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.skipif(
    sys.platform != "win32", reason="Requires Windows DPAPI/file sharing"
)
def test_windows_brave_lock_recovers_in_account_edit_without_saving_on_cancel(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = acceptance_env
    importer, database = synthetic_brave_profiles(tmp_path)
    monkeypatch.setattr(init, "SystemBrowserImporter", lambda: importer)
    before = env.config.read_bytes()
    secrets = env.credentials.read_bytes() if env.credentials.exists() else None
    with exclusive_windows_file(database):
        result = runner.invoke(
            main,
            ["profile", "edit", "main", "--no-color"],
            input="3\ny\n2\n1\n1\n3\n123\nacceptance-secret-not-real\n1\n1\n1\n2\n",
        )
    assert result.exit_code == 1, result.output
    assert "cookie database is locked" in result.stderr
    assert "Close the selected browser fully" in result.stderr
    assert "Browser recovery" in result.stdout and "Session verified" in result.stdout
    assert "Setup cancelled; no changes saved" in result.stdout
    assert "acceptance-secret-not-real" not in result.output
    assert "Traceback" not in result.output and "\x1b" not in result.output
    assert "WinError" not in result.output and str(database) not in result.output
    assert env.config.read_bytes() == before
    assert (
        env.credentials.read_bytes() if env.credentials.exists() else None
    ) == secrets
    env.discovery.discover.assert_called_once_with(
        manual_credentials("123", "acceptance-secret-not-real")
    )
    env.keyring.set_password.assert_not_called()
    env.keyring.delete_password.assert_not_called()
