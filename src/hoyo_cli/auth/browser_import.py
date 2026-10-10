"""Supported browser-cookie3 loaders, restricted to HoYoLAB auth cookies."""

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Protocol, cast

from hoyo_cli.auth.credentials import COOKIE_FIELDS, Credentials
from hoyo_cli.errors import BrowserImportError, CredentialError

SUPPORTED_BROWSERS = ("chrome", "edge", "firefox", "chromium", "opera", "brave")


@dataclass(frozen=True)
class BrowserSession:
    source_id: str
    browser_label: str


class BrowserImporter(Protocol):
    def list_sessions(self) -> tuple[BrowserSession, ...]: ...
    def import_session(self, source_id: str) -> Credentials: ...


class BrowserAdapter(Protocol):
    def read(self, source_id: str) -> Credentials: ...


class CookieLoader(Protocol):
    def __call__(self, *, domain_name: str) -> CookieJar: ...


class BrowserCookieAdapter:
    def __init__(self, loaders: Mapping[str, CookieLoader] | None = None) -> None:
        self._loaders = loaders

    def read(self, source_id: str) -> Credentials:
        if source_id not in SUPPORTED_BROWSERS:
            raise BrowserImportError(
                "Unsupported browser; paste a Cookie header instead."
            )
        try:
            if self._loaders is None:
                library = importlib.import_module("browser_cookie3")
                loader = cast(CookieLoader, getattr(library, source_id))
            else:
                loader = self._loaders[source_id]
            jar = loader(domain_name="hoyolab.com")
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
                        raise CredentialError(
                            "Ambiguous browser authentication cookies."
                        )
                    fields[cookie.name] = cookie.value
            return Credentials(fields)
        except Exception:
            # Cookie library/OS errors are recoverable and may contain cookie values.
            raise BrowserImportError(
                "Unable to import this HoYoLAB browser session. "
                "Paste a full Cookie header instead."
            ) from None


class SystemBrowserImporter:
    def __init__(
        self,
        adapter: BrowserAdapter | None = None,
        sources: tuple[str, ...] = SUPPORTED_BROWSERS,
    ) -> None:
        if any(source not in SUPPORTED_BROWSERS for source in sources):
            raise BrowserImportError("Unsupported browser source.")
        self.adapter = adapter if adapter is not None else BrowserCookieAdapter()
        self.sources = sources

    def list_sessions(self) -> tuple[BrowserSession, ...]:
        sessions: list[BrowserSession] = []
        for source in self.sources:
            try:
                self.adapter.read(source)
            except BrowserImportError:
                continue
            sessions.append(BrowserSession(source, source.capitalize()))
        return tuple(sessions)

    def import_session(self, source_id: str) -> Credentials:
        if source_id not in self.sources:
            raise BrowserImportError("Unsupported browser session.")
        return self.adapter.read(source_id)


def list_browser_sessions() -> tuple[BrowserSession, ...]:
    return SystemBrowserImporter().list_sessions()


def import_browser_session(source_id: str) -> Credentials:
    return SystemBrowserImporter().import_session(source_id)
