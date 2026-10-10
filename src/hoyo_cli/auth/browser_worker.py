"""Isolated browser-cookie3 boundary. Invoke only as a child-process module.

The protocol carries secrets through private pipes, never command arguments.
Python and native diagnostics are contained by the parent's pipes. Only safe
failure codes and validated auth fields cross the boundary.
"""

import ctypes
import importlib
import io
import json
import sys
import warnings
from contextlib import redirect_stderr, redirect_stdout
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
    if cookie_file is not None:
        _preflight_database(cookie_file)
    library = importlib.import_module("browser_cookie3")
    # Only this private child process changes the dependency's fallback setting.
    library.__dict__["shadowcopy"] = None
    loader = getattr(library, browser, None)
    if not callable(loader):
        raise browser_error(ErrorCode.BROWSER_UNSUPPORTED)
    reader = cast(CookieLoader, loader)
    return extract_credentials(
        reader(domain_name="hoyolab.com", cookie_file=cookie_file, key_file=key_file)
    )


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
