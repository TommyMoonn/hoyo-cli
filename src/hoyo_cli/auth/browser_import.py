"""Browser capabilities, explicit profile selection, and safe import failures.

Listing inspects bounded profile metadata, never cookies or login state. The
selected reader runs in a child process to contain dependency diagnostics.
"""

import configparser
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from hoyo_cli.auth.credentials import Credentials, deserialize_credentials
from hoyo_cli.errors import BrowserImportError, ErrorCode

SUPPORTED_BROWSERS = ("chrome", "edge", "firefox", "chromium", "opera", "brave")
_LABELS = dict(
    zip(
        SUPPORTED_BROWSERS,
        ("Chrome", "Edge", "Firefox", "Chromium", "Opera", "Brave"),
        strict=True,
    )
)


@dataclass(frozen=True)
class BrowserSession:
    """A supported source, not proof of a HoYoLAB login."""

    source_id: str
    browser_label: str
    supports_profiles: bool = False


@dataclass(frozen=True)
class BrowserProfile:
    source_id: str
    browser: str
    label: str
    cookie_file: Path | None = field(default=None, repr=False)
    key_file: Path | None = field(default=None, repr=False)


class BrowserImporter(Protocol):
    def list_sessions(self) -> tuple[BrowserSession, ...]: ...
    def list_profiles(self, source_id: str) -> tuple[BrowserProfile, ...]: ...
    def import_session(self, source_id: str) -> Credentials: ...


_RECOVERY: dict[ErrorCode, tuple[str, str]] = {
    ErrorCode.BROWSER_LOCKED: (
        "The selected browser's cookie database is locked.",
        "Close the selected browser fully, including background processes, and retry. "
        "You can also choose another browser or paste a Cookie header manually.",
    ),
    ErrorCode.BROWSER_ACCESS_DENIED: (
        "Access to the selected browser profile was denied.",
        "Choose another supported browser/profile or paste a Cookie header manually.",
    ),
    ErrorCode.BROWSER_PROFILE_MISSING: (
        "The selected browser profile or cookie database is unavailable.",
        "Open the intended browser profile, then choose it again or use manual input.",
    ),
    ErrorCode.BROWSER_LOGIN_MISSING: (
        "No complete HoYoLAB login cookies were found in this profile.",
        "Sign in to HoYoLAB in the selected profile, then retry browser import.",
    ),
    ErrorCode.BROWSER_COOKIES_CONFLICT: (
        "The selected profile contains conflicting authentication cookies.",
        "Sign in again in that profile, choose another profile, or use manual input.",
    ),
    ErrorCode.BROWSER_DECRYPTION: (
        "This browser's cookie encryption cannot be read by the supported reader.",
        "Choose another supported source or paste a Cookie header manually.",
    ),
    ErrorCode.BROWSER_UNSUPPORTED: (
        "This browser or profile selection is not supported on this platform.",
        "Choose a listed supported source or paste a Cookie header manually.",
    ),
    ErrorCode.BROWSER_STORAGE: (
        "The browser reader could not access the selected cookie storage.",
        "Retry, choose another supported source, or paste a Cookie header manually.",
    ),
    ErrorCode.BROWSER_TIMEOUT: (
        "Reading the selected browser profile timed out.",
        "Retry, choose another supported source, or paste a Cookie header manually.",
    ),
    ErrorCode.BROWSER_UNKNOWN: (
        "The selected browser could not be imported.",
        "Retry, choose another supported source, or paste a Cookie header manually.",
    ),
}


def browser_error(code: ErrorCode) -> BrowserImportError:
    if code not in _RECOVERY:
        code = ErrorCode.BROWSER_UNKNOWN
    message, hint = _RECOVERY[code]
    return BrowserImportError(message, code=code, hint=hint)


def classify_browser_failure(error: Exception) -> BrowserImportError:
    if isinstance(error, BrowserImportError):
        return error
    if isinstance(error, OSError) and getattr(error, "winerror", None) == 32:
        return browser_error(ErrorCode.BROWSER_LOCKED)
    if isinstance(error, PermissionError):
        return browser_error(ErrorCode.BROWSER_ACCESS_DENIED)
    if isinstance(error, FileNotFoundError):
        return browser_error(ErrorCode.BROWSER_PROFILE_MISSING)
    # Exact diagnostics verified in browser-cookie3 0.20.1. Unknown exceptions
    # must not be diagnosed as locks or decryption failures by substring guessing.
    if type(error).__module__ == "browser_cookie3":
        if str(error) == "Unable to get key for cookie decryption":
            return browser_error(ErrorCode.BROWSER_DECRYPTION)
        if str(error) == "Unable to read database file":
            return browser_error(ErrorCode.BROWSER_STORAGE)
    if isinstance(error, RuntimeError) and str(error) in (
        "Failed to decrypt the cipher text with DPAPI",
        "Failed to decrypt the cipher text with DPAPI and no AES key.",
    ):
        return browser_error(ErrorCode.BROWSER_DECRYPTION)
    return browser_error(ErrorCode.BROWSER_UNKNOWN)


def read_browser_profile(profile: BrowserProfile) -> Credentials:
    request = json.dumps(
        {
            "browser": profile.browser,
            "cookie_file": str(profile.cookie_file) if profile.cookie_file else None,
            "key_file": str(profile.key_file) if profile.key_file else None,
        }
    )
    try:
        result = subprocess.run(
            [sys.executable, "-m", "hoyo_cli.auth.browser_worker"],
            input=request,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if result.returncode != 0:
            raise browser_error(ErrorCode.BROWSER_UNKNOWN)
        payload: object = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise browser_error(ErrorCode.BROWSER_UNKNOWN)
        data = cast(dict[str, object], payload)
        if "error" in data:
            if not isinstance(data["error"], str):
                raise browser_error(ErrorCode.BROWSER_UNKNOWN)
            raise browser_error(ErrorCode(data["error"]))
        raw = data.get("credentials")
        if not isinstance(raw, str):
            raise browser_error(ErrorCode.BROWSER_UNKNOWN)
        return deserialize_credentials(raw)
    except subprocess.TimeoutExpired:
        raise browser_error(ErrorCode.BROWSER_TIMEOUT) from None
    except BrowserImportError:
        raise
    except Exception:
        raise browser_error(ErrorCode.BROWSER_UNKNOWN) from None


class BrowserCookieAdapter:
    """Verified Windows profile layouts; honest library selection elsewhere."""

    def __init__(
        self,
        *,
        platform: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._platform = platform if platform is not None else sys.platform
        self._environment = environment if environment is not None else os.environ
        self._profiles: dict[str, BrowserProfile] = {}

    def _roots(self, browser: str) -> tuple[tuple[str, Path], ...]:
        # Only paths accepted by the locked reader are enumerated, without
        # recursive search, cookie reads, or reading encryption keys in Local State.
        layouts = {
            "chrome": ("Google/Chrome{channel}/User Data", ("", " Beta", " Dev")),
            "edge": (
                "Microsoft/Edge{channel}/User Data",
                ("", " Beta", " Dev", " SxS"),
            ),
            "brave": (
                "BraveSoftware/Brave-Browser{channel}/User Data",
                ("", "-Beta", "-Dev", "-Nightly"),
            ),
            "chromium": ("Chromium/User Data", ("",)),
            "opera": (
                "Opera Software/Opera {channel}",
                ("Stable", "Next", "Developer"),
            ),
            "firefox": ("Mozilla/Firefox", ("",)),
        }
        layout, channels = layouts[browser]
        bases: list[Path] = []
        for variable in ("LOCALAPPDATA", "APPDATA"):
            value = self._environment.get(variable)
            if value and Path(value) not in bases:
                bases.append(Path(value))
        roots: list[tuple[str, Path]] = []
        for channel in channels:
            for base in bases:
                root = base / layout.format(channel=channel)
                if root.is_dir():
                    label = channel.strip(" -") or "Stable"
                    roots.append((label, root))
        return tuple(roots)

    def list_sessions(self) -> tuple[BrowserSession, ...]:
        if self._platform not in ("win32", "darwin", "linux"):
            return ()
        try:
            return tuple(
                BrowserSession(browser, _LABELS[browser], self._platform == "win32")
                for browser in SUPPORTED_BROWSERS
                if self._platform != "win32" or self._roots(browser)
            )
        except OSError as error:
            raise classify_browser_failure(error) from None

    def list_profiles(self, source_id: str) -> tuple[BrowserProfile, ...]:
        if source_id not in SUPPORTED_BROWSERS or self._platform not in (
            "win32",
            "darwin",
            "linux",
        ):
            raise browser_error(ErrorCode.BROWSER_UNSUPPORTED)
        if self._platform != "win32":
            profile = BrowserProfile(source_id, source_id, "Library-selected profile")
            self._profiles[source_id] = profile
            return (profile,)
        profiles: list[BrowserProfile] = []
        try:
            for channel, root in self._roots(source_id):
                if source_id == "firefox":
                    if not (root / "profiles.ini").is_file():
                        continue
                    entries = self._firefox_profiles(root)
                elif source_id == "opera":
                    entries = [(channel, root)]
                else:
                    entries = [
                        (path.name, path)
                        for path in sorted(root.iterdir())
                        if path.is_dir()
                        and (path.name == "Default" or path.name.startswith("Profile "))
                    ]
                for label, directory in entries:
                    if source_id == "firefox":
                        cookie_file, key_file = directory / "cookies.sqlite", None
                    else:
                        cookie_file = directory / "Network/Cookies"
                        if not cookie_file.is_file():
                            cookie_file = directory / "Cookies"
                        key_file = root / "Local State"
                    source = f"{source_id}:{len(profiles)}"
                    safe_label = "".join(c for c in label if c.isprintable())[:80]
                    profile = BrowserProfile(
                        source,
                        source_id,
                        f"{channel} / {safe_label}",
                        cookie_file,
                        key_file,
                    )
                    profiles.append(profile)
        except (OSError, configparser.Error, UnicodeError) as error:
            raise classify_browser_failure(error) from None
        if not profiles:
            raise browser_error(ErrorCode.BROWSER_PROFILE_MISSING)
        self._profiles = {
            key: profile
            for key, profile in self._profiles.items()
            if profile.browser != source_id
        }
        self._profiles.update({profile.source_id: profile for profile in profiles})
        return tuple(profiles)

    @staticmethod
    def _firefox_profiles(root: Path) -> list[tuple[str, Path]]:
        parser = configparser.ConfigParser(interpolation=None)
        with (root / "profiles.ini").open(encoding="utf-8") as stream:
            parser.read_file(stream)
        entries: list[tuple[str, Path]] = []
        for section in parser.sections():
            if not section.startswith("Profile") or not parser.has_option(
                section, "Path"
            ):
                continue
            path = Path(parser.get(section, "Path"))
            relative = parser.get(section, "IsRelative", fallback="1") == "1"
            if relative:
                path = root / path
            entries.append((parser.get(section, "Name", fallback=section), path))
        return entries

    def read(self, source_id: str) -> Credentials:
        profile = self._profiles.get(source_id)
        if profile is None:
            raise browser_error(ErrorCode.BROWSER_UNSUPPORTED)
        return read_browser_profile(profile)


class BrowserAdapter(Protocol):
    def list_sessions(self) -> tuple[BrowserSession, ...]: ...
    def list_profiles(self, source_id: str) -> tuple[BrowserProfile, ...]: ...
    def read(self, source_id: str) -> Credentials: ...


class SystemBrowserImporter:
    def __init__(self, adapter: BrowserAdapter | None = None) -> None:
        self.adapter = adapter if adapter is not None else BrowserCookieAdapter()

    def list_sessions(self) -> tuple[BrowserSession, ...]:
        return self.adapter.list_sessions()

    def list_profiles(self, source_id: str) -> tuple[BrowserProfile, ...]:
        return self.adapter.list_profiles(source_id)

    def import_session(self, source_id: str) -> Credentials:
        return self.adapter.read(source_id)
