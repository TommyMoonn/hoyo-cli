"""Isolated browser-cookie3 boundary. Invoke only as a child-process module.

The protocol carries secrets through private pipes, never command arguments.
Python and native diagnostics are contained by the parent's pipes. Only safe
failure codes and validated auth fields cross the boundary.
"""

import ctypes
import importlib
import io
import json
import sqlite3
import sys
import time
import warnings
from contextlib import closing, redirect_stderr, redirect_stdout
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Protocol, cast

from hoyo_cli.auth.browser_import import (
    SUPPORTED_BROWSERS,
    browser_error,
    classify_browser_failure,
)
from hoyo_cli.auth.credentials import COOKIE_FIELDS, Credentials, serialize_credentials
from hoyo_cli.errors import CredentialError, ErrorCode


class CookieLoader(Protocol):
    def __call__(
        self, *, domain_name: str, cookie_file: str | None, key_file: str | None
    ) -> CookieJar: ...


def extract_credentials(jar: CookieJar) -> Credentials:
    fields: dict[str, str] = {}
    for cookie in jar:
        domain = cookie.domain.lstrip(".").lower()
        if (
            (domain == "hoyolab.com" or domain.endswith(".hoyolab.com"))
            and cookie.name in COOKIE_FIELDS
            and not cookie.is_expired()
            and cookie.value is not None
        ):
            if cookie.name in fields and fields[cookie.name] != cookie.value:
                raise browser_error(ErrorCode.BROWSER_COOKIES_CONFLICT)
            fields[cookie.name] = cookie.value
    try:
        return Credentials(fields)
    except CredentialError as error:
        code = (
            ErrorCode.BROWSER_COOKIES_CONFLICT
            if error.code == ErrorCode.CREDENTIALS_CONFLICT
            else ErrorCode.BROWSER_LOGIN_MISSING
        )
        raise browser_error(code) from None


def _preflight_database(cookie_file: str) -> None:
    if sys.platform != "win32":
        with Path(cookie_file).open("rb"):
            pass
        return
    # CRT open() can discard winerror=32. Preserve Win32's actual result while
    # allowing other readers/writers/deleters, without taking a database lock.
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
    handle = create(cookie_file, 0x80000000, 7, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    if not close(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def load_selected(
    browser: str, cookie_file: str | None, key_file: str | None
) -> Credentials:
    if browser not in SUPPORTED_BROWSERS:
        raise browser_error(ErrorCode.BROWSER_UNSUPPORTED)
    # Opening preserves OS evidence that the library's fallback can swallow.
    # It does not read contents or probe another profile.
    app_bound = False
    if cookie_file is not None:
        try:
            _preflight_database(cookie_file)
            if sys.platform == "win32" and browser != "firefox":
                app_bound = _has_app_bound_cookies(cookie_file)
        except Exception as error:
            raise classify_browser_failure(error) from None
    library = importlib.import_module("browser_cookie3")
    # Only this private child process changes the dependency's fallback setting.
    library.__dict__["shadowcopy"] = None
    loader = getattr(library, browser, None)
    if not callable(loader):
        raise browser_error(ErrorCode.BROWSER_UNSUPPORTED)
    reader = cast(CookieLoader, loader)
    try:
        return extract_credentials(
            reader(
                domain_name="hoyolab.com", cookie_file=cookie_file, key_file=key_file
            )
        )
    except Exception as error:
        failure = classify_browser_failure(error)
        # Always attempt the supported reader: usable legacy cookies win even
        # when the selected database also contains protected rows. Storage,
        # access, conflicts, and unknown faults keep their independent causes.
        if app_bound and failure.code in (
            ErrorCode.BROWSER_DECRYPTION,
            ErrorCode.BROWSER_LOGIN_MISSING,
        ):
            failure = browser_error(ErrorCode.BROWSER_ENCRYPTION_UNSUPPORTED)
        raise failure from None


def _has_app_bound_cookies(cookie_file: str) -> bool:
    """Inspect only format metadata in the selected Windows Chromium database.

    Normal read-only SQLite locking sees committed WAL data. Do not fall back to
    immutable/nolock reads or copies, which can hide locks or miss WAL evidence.
    The query returns a boolean, never cookie values or ciphertext.
    """
    uri = Path(cookie_file).absolute().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as connection:
        placeholders = ",".join("?" for _ in COOKIE_FIELDS)
        row = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM cookies WHERE "
            "(lower(ltrim(host_key, '.')) = 'hoyolab.com' OR "
            "lower(ltrim(host_key, '.')) LIKE '%.hoyolab.com') "
            f"AND name IN ({placeholders}) "
            "AND (value IS NULL OR value = '') "
            "AND (expires_utc = 0 OR expires_utc > ?) "
            "AND substr(encrypted_value, 1, 3) = ?)",
            (*COOKIE_FIELDS, int((time.time() + 11644473600) * 1000000), b"v20"),
        ).fetchone()
        return bool(row and row[0])


def main() -> None:
    try:
        request: object = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise browser_error(ErrorCode.BROWSER_UNSUPPORTED)
        data = cast(dict[str, object], request)
        browser = data.get("browser")
        cookie_file = data.get("cookie_file")
        key_file = data.get("key_file")
        if (
            not isinstance(browser, str)
            or (cookie_file is not None and not isinstance(cookie_file, str))
            or (key_file is not None and not isinstance(key_file, str))
        ):
            raise browser_error(ErrorCode.BROWSER_UNSUPPORTED)
        # Redirects/warning filters are confined to this loader process.
        with (
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
            warnings.catch_warnings(record=True),
        ):
            warnings.simplefilter("always")
            credentials = load_selected(browser, cookie_file, key_file)
        payload = {"credentials": serialize_credentials(credentials)}
    except Exception as error:
        payload = {"error": classify_browser_failure(error).code.value}
    sys.stdout.write(json.dumps(payload))


if __name__ == "__main__":
    main()
