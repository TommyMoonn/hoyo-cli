"""Selected-profile encryption evidence and safe recovery, without decryption."""

import asyncio
import importlib
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

import click
import pytest
from test_browser_import import cookie_jar
from test_init import FakeBrowser, FakeDiscovery, FakeIO
from test_profiles import seed_shared

from hoyo_cli.auth import browser_worker
from hoyo_cli.auth.browser_import import browser_error, classify_browser_failure
from hoyo_cli.commands.init import run_add_profile, run_edit_profile, run_init
from hoyo_cli.errors import BrowserImportError, ErrorCode

Row = tuple[str, str, bytes, str, int]


def chromium_database(path: Path, rows: list[Row]) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB, "
        "value TEXT, expires_utc INTEGER)"
    )
    connection.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?, ?)", rows)
    connection.commit()
    return connection


def selected_import(path: Path, loader: Mock, *, platform: str = "win32") -> object:
    library = ModuleType("fake_browser_cookie3")
    library.__dict__["chrome"] = loader

    def check_file(cookie_file: str) -> None:
        with Path(cookie_file).open("rb"):
            pass

    with (
        patch.object(browser_worker.sys, "platform", platform),
        # Windows sharing evidence has separate native child-process coverage.
        # Format fixtures also run on hosts without the Win32 ctypes API.
        patch.object(browser_worker, "_preflight_database", side_effect=check_file),
        patch.object(browser_worker.importlib, "import_module", return_value=library),
    ):
        return browser_worker.load_selected("chrome", str(path), None)


def decryption_failure() -> Exception:
    library = importlib.import_module("browser_cookie3")
    return library.BrowserCookieError("Unable to get key for cookie decryption")


@pytest.mark.parametrize("failure", ["decryption", "missing"])
@pytest.mark.parametrize("mixed", [False, True])
def test_relevant_v20_is_unsupported_encryption(
    tmp_path: Path, failure: str, mixed: bool
) -> None:
    path = tmp_path / "Cookies"
    rows: list[Row] = [(".hoyolab.com", "ltoken_v2", b"v20opaque", "", 0)]
    if mixed:
        rows.append(("www.hoyolab.com", "ltuid_v2", b"v10opaque", "", 0))
    chromium_database(path, rows).close()
    loader = Mock(
        side_effect=decryption_failure() if failure == "decryption" else None,
        return_value=cookie_jar([]),
    )
    with pytest.raises(BrowserImportError) as caught:
        selected_import(path, loader)
    assert caught.value.code.value == "browser_encryption_unsupported"
    assert "app-bound" in caught.value.message
    assert "opaque" not in str(caught.value) and str(path) not in str(caught.value)
    loader.assert_called_once_with(
        domain_name="hoyolab.com", cookie_file=str(path), key_file=None
    )


@pytest.mark.parametrize(
    "row",
    [
        ("example.com", "ltoken_v2", b"v20opaque", "", 0),
        ("evil-hoyolab.com", "ltoken_v2", b"v20opaque", "", 0),
        ("hoyolab.com.evil", "ltoken_v2", b"v20opaque", "", 0),
        (".hoyolab.com", "analytics", b"v20opaque", "", 0),
        (".hoyolab.com", "ltoken_v2", b"v10opaque", "", 0),
        (".hoyolab.com", "ltoken_v2", b"v11opaque", "", 0),
        (".hoyolab.com", "ltoken_v2", b"", "legacy", 0),
        (".hoyolab.com", "ltoken_v2", b"v20opaque", "legacy", 0),
        (".hoyolab.com", "ltoken_v2", b"v20opaque", "", 1),
    ],
)
def test_unrelated_legacy_or_expired_rows_are_not_app_bound(
    tmp_path: Path, row: Row
) -> None:
    path = tmp_path / "Cookies"
    chromium_database(path, [row]).close()
    with pytest.raises(BrowserImportError) as caught:
        selected_import(path, Mock(side_effect=decryption_failure()))
    assert caught.value.code == ErrorCode.BROWSER_DECRYPTION


def test_successful_legacy_import_wins_over_mixed_encryption(tmp_path: Path) -> None:
    path = tmp_path / "Cookies"
    chromium_database(
        path, [(".hoyolab.com", "ltoken_v2", b"v20opaque", "", 0)]
    ).close()
    jar = cookie_jar(
        [(".hoyolab.com", "ltuid", "123"), (".hoyolab.com", "ltoken", "legacy")]
    )
    credentials = selected_import(path, Mock(return_value=jar))
    assert credentials == browser_worker.extract_credentials(jar)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_windows_app_bound_format_is_not_inferred_on_other_platforms(
    tmp_path: Path, platform: str
) -> None:
    path = tmp_path / "Cookies"
    chromium_database(
        path, [(".hoyolab.com", "ltoken_v2", b"v20opaque", "", 0)]
    ).close()
    with pytest.raises(BrowserImportError) as caught:
        selected_import(path, Mock(side_effect=decryption_failure()), platform=platform)
    assert caught.value.code == ErrorCode.BROWSER_DECRYPTION


@pytest.mark.parametrize(
    "failure,code",
    [
        (PermissionError("private-secret"), ErrorCode.BROWSER_ACCESS_DENIED),
        (FileNotFoundError("private-secret"), ErrorCode.BROWSER_PROFILE_MISSING),
        (OSError("private-secret"), ErrorCode.BROWSER_STORAGE),
        (RuntimeError("private-secret v20 locked"), ErrorCode.BROWSER_UNKNOWN),
        (
            browser_error(ErrorCode.BROWSER_COOKIES_CONFLICT),
            ErrorCode.BROWSER_COOKIES_CONFLICT,
        ),
    ],
)
def test_encryption_evidence_does_not_replace_other_failures(
    tmp_path: Path, failure: Exception, code: ErrorCode
) -> None:
    path = tmp_path / "Cookies"
    chromium_database(
        path, [(".hoyolab.com", "ltoken_v2", b"v20opaque", "", 0)]
    ).close()
    with pytest.raises(BrowserImportError) as caught:
        selected_import(path, Mock(side_effect=failure))
    assert caught.value.code == code
    assert "private-secret" not in str(caught.value)


@pytest.mark.parametrize("mode", ["malformed", "missing-schema", "locked", "missing"])
def test_unreadable_sqlite_keeps_storage_cause(tmp_path: Path, mode: str) -> None:
    path = tmp_path / "Cookies"
    connection: sqlite3.Connection | None = None
    if mode == "malformed":
        path.write_bytes(b"v20private-secret not sqlite")
    elif mode != "missing":
        connection = chromium_database(path, [])
        if mode == "missing-schema":
            connection.execute("DROP TABLE cookies")
            connection.commit()
        else:
            connection.execute("BEGIN EXCLUSIVE")
    loader = Mock(return_value=cookie_jar([]))
    try:
        with pytest.raises(BrowserImportError) as caught:
            selected_import(path, loader)
        assert caught.value.code == {
            "locked": ErrorCode.BROWSER_LOCKED,
            "missing": ErrorCode.BROWSER_PROFILE_MISSING,
        }.get(mode, ErrorCode.BROWSER_STORAGE)
        assert "private-secret" not in str(caught.value)
        loader.assert_not_called()
    finally:
        if connection is not None:
            connection.close()
    if mode == "missing":
        assert not path.exists()


def test_wal_evidence_is_read_from_selected_database_without_copy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "Cookies # selected"
    connection = chromium_database(path, [])
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "INSERT INTO cookies VALUES ('.hoyolab.com', 'ltoken_v2', ?, '', 0)",
            (b"v20opaque",),
        )
        connection.commit()
        paths = set(tmp_path.iterdir())
        # SQLite updates shared-memory reader bookkeeping during read transactions.
        before = {p.name: p.read_bytes() for p in paths if not p.name.endswith("-shm")}
        with pytest.raises(BrowserImportError) as caught:
            selected_import(path, Mock(side_effect=decryption_failure()))
        assert caught.value.code.value == "browser_encryption_unsupported"
        assert set(tmp_path.iterdir()) == paths
        assert {
            p.name: p.read_bytes() for p in paths if not p.name.endswith("-shm")
        } == before
    finally:
        connection.close()


@pytest.mark.parametrize(
    "signal", [KeyboardInterrupt, SystemExit, asyncio.CancelledError]
)
def test_encryption_diagnosis_does_not_swallow_control_signals(
    tmp_path: Path, signal: type[BaseException]
) -> None:
    path = tmp_path / "Cookies"
    chromium_database(path, []).close()
    with pytest.raises(signal):
        selected_import(path, Mock(side_effect=signal))


@pytest.mark.parametrize(
    "code", [code for code in ErrorCode if code.value.startswith("browser_")]
)
def test_every_browser_category_names_two_field_manual_recovery(
    code: ErrorCode,
) -> None:
    error = browser_error(code)
    assert error.hint and "ltuid_v2 / ltoken_v2" in error.hint


@pytest.mark.parametrize("mode", ["first", "add", "edit"])
@pytest.mark.parametrize("cancel", [False, True])
def test_app_bound_recovery_in_shared_wizard(
    tmp_path: Path, mode: str, cancel: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    class RecoveryIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Browser recovery":
                assert tuple(choices) == (
                    "Enter ltuid_v2 / ltoken_v2 manually (recommended)",
                    "Choose another browser/profile",
                    "Cancel (no changes)",
                )
                return choices[-1] if cancel else choices[0]
            return super().choose(prompt, choices)

    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    if mode != "first":
        config, secrets = seed_shared(tmp_path)
    before = tuple(p.read_bytes() if p.exists() else None for p in (config, secrets))
    io = RecoveryIO(name="new-profile", browser=True)
    browser, discovery = FakeBrowser(), FakeDiscovery()
    with patch.object(
        browser,
        "import_session",
        side_effect=browser_error(ErrorCode.BROWSER_ENCRYPTION_UNSUPPORTED),
    ) as read:

        def run() -> None:
            if mode == "first":
                run_init(config, secrets, io, browser, discovery)
            elif mode == "add":
                run_add_profile(
                    config,
                    secrets,
                    io,
                    browser,
                    discovery,
                    requested_profile="new-profile",
                )
            else:
                run_edit_profile(
                    config, secrets, io, browser, discovery, requested_profile="main"
                )

        if cancel:
            with pytest.raises(click.Abort):
                run()
            assert (
                tuple(p.read_bytes() if p.exists() else None for p in (config, secrets))
                == before
            )
        else:
            run()
            assert [
                prompt
                for prompt in io.prompts
                if prompt.startswith(("ltuid_v2", "ltoken_v2"))
            ] == [
                "ltuid_v2 (HoYoLAB account ID, hidden)",
                "ltoken_v2 (session token, hidden)",
            ]
        read.assert_called_once()
    captured = capsys.readouterr()
    assert "app-bound encryption" in captured.err
    assert "test-secret-123" not in captured.out + captured.err


def test_unknown_substring_diagnostics_remain_unknown() -> None:
    assert (
        classify_browser_failure(RuntimeError("v20 app-bound encryption")).code
        == ErrorCode.BROWSER_UNKNOWN
    )
