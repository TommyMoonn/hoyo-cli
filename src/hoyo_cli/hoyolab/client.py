"""Pinned upstream integration with one application-owned request policy."""

import asyncio
import inspect
import math
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, TypeVar, cast

import aiohttp
import genshin
from genshin.client.manager import managers

from hoyo_cli.auth.credentials import Credentials
from hoyo_cli.errors import (
    AppError,
    AuthenticationError,
    ConfigError,
    NetworkError,
    UnsupportedDataError,
)
from hoyo_cli.hoyolab.adapters.genshin import normalize_genshin
from hoyo_cli.hoyolab.adapters.hsr import normalize_hsr
from hoyo_cli.hoyolab.adapters.zzz import normalize_zzz
from hoyo_cli.models.common import (
    CheckInReceipt,
    CheckInState,
    GameId,
    GameStatus,
)

# Each attempt has a 10-second deadline, including upstream processing. At most
# two attempts means at most 20 seconds of network wait per read operation.
REQUEST_TIMEOUT_SECONDS = 10.0
T = TypeVar("T")

# genshin 1.7.31 decorates these with nested 5/7-attempt retries and proxy retry.
# Keep its transport and async session cleanup, but remove that policy locally.
# No global monkeypatch: only clients constructed here use this pinned adapter.
_Request = Callable[..., Awaitable[Any]]
_single_request = cast(_Request, inspect.unwrap(managers.BaseCookieManager._request))  # pyright: ignore[reportPrivateUsage]
_single_raw_request = cast(
    _Request,
    inspect.unwrap(managers.BaseCookieManager._raw_request),  # pyright: ignore[reportPrivateUsage]
)


class _SingleAttemptCookieManager(managers.CookieManager):
    def create_session(self, **kwargs: Any) -> aiohttp.ClientSession:
        # The pinned transport otherwise parses even HTTP error responses.
        kwargs["raise_for_status"] = True
        return super().create_session(**kwargs)

    async def _request(self, *args: Any, **kwargs: Any) -> Any:
        return await _single_request(self, *args, **kwargs)

    async def _raw_request(self, *args: Any, **kwargs: Any) -> Any:
        return await _single_raw_request(self, *args, **kwargs)


def create_client(credentials: Credentials) -> genshin.Client:
    client = genshin.Client(dict(credentials.fields))
    client.cookie_manager = _SingleAttemptCookieManager(dict(credentials.fields))
    return client


def _classify(error: Exception) -> AppError:
    if isinstance(error, AppError):
        return error
    if isinstance(error, genshin.errors.InvalidCookies):
        return AuthenticationError(
            "HoYoLAB rejected the session. Sign in again and rerun hoyo init."
        )
    if isinstance(error, TimeoutError):
        return NetworkError("HoYoLAB request timed out.")
    if isinstance(error, aiohttp.ClientResponseError):
        if error.status in (401, 403):
            return AuthenticationError(
                "HoYoLAB rejected authorization for this request."
            )
        if error.status == 429:
            return NetworkError("HoYoLAB rate limited this request.")
        if 500 <= error.status < 600:
            return NetworkError("HoYoLAB server failed to complete the request.")
        return UnsupportedDataError(
            "HoYoLAB rejected the request or returned unsupported data."
        )
    if isinstance(error, aiohttp.ClientSSLError):
        return NetworkError(
            "Unable to verify the HoYoLAB secure connection.", transient=False
        )
    if isinstance(
        error,
        (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, ConnectionError),
    ):
        return NetworkError("Unable to connect to HoYoLAB.")
    if isinstance(error, genshin.errors.GenshinException):
        if error.retcode in (-110, 1028, 10101):
            return NetworkError("HoYoLAB rate limited this request.")
        if error.retcode in (-1, 10307):
            return NetworkError("HoYoLAB server failed to complete the request.")
        if error.retcode == 0:
            return UnsupportedDataError(
                "HoYoLAB returned an unsupported or changed response."
            )
        return AppError(
            "HoYoLAB could not complete the requested read. "
            "Check account access and privacy settings."
        )
    if isinstance(
        error,
        (ValueError, TypeError, KeyError, AttributeError, aiohttp.ContentTypeError),
    ):
        return UnsupportedDataError(
            "HoYoLAB returned an unsupported or changed response."
        )
    return AppError("HoYoLAB request failed; retry or check account configuration.")


async def request_with_retry(
    operation: Callable[[], Awaitable[T]], *, timeout_seconds: float
) -> T:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Request timeout must be finite and positive.")
    for attempt in range(2):
        try:
            return await asyncio.wait_for(operation(), timeout=timeout_seconds)
        except Exception as error:
            classified = _classify(error)
            if (
                attempt == 0
                and isinstance(classified, NetworkError)
                and classified.transient
            ):
                continue
            raise classified from None
    raise AssertionError("Unreachable request state.")


async def _fetch(credentials: Credentials) -> Sequence[genshin.models.GenshinAccount]:
    client = create_client(credentials)
    return await request_with_retry(
        client.get_game_accounts, timeout_seconds=REQUEST_TIMEOUT_SECONDS
    )


def fetch_linked_accounts(
    credentials: Credentials,
) -> Sequence[genshin.models.GenshinAccount]:
    return asyncio.run(_fetch(credentials))


class HoyolabGateway(Protocol):
    async def fetch_status(self, game: GameId, uid: int) -> GameStatus: ...
    async def check_in_state(self, game: GameId, uid: int) -> CheckInState: ...
    async def claim_check_in(self, game: GameId, uid: int) -> CheckInReceipt: ...


class NotesClient(Protocol):
    async def get_genshin_notes(
        self, uid: int, *, autoauth: bool, return_raw_data: Literal[True]
    ) -> object: ...

    async def get_starrail_notes(
        self, uid: int, *, autoauth: bool, return_raw_data: Literal[True]
    ) -> object: ...

    async def get_zzz_notes(
        self, uid: int, *, autoauth: bool, return_raw_data: Literal[True]
    ) -> object: ...


class GenshinPyGateway:
    def __init__(
        self,
        credentials: Credentials,
        *,
        client: NotesClient | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        # Upstream opens and closes sessions within each individual request;
        # this gateway holds no persistent session needing a separate close.
        self._client = client if client is not None else create_client(credentials)
        self._now = now if now is not None else lambda: datetime.now(UTC)

    async def fetch_status(self, game: GameId, uid: int) -> GameStatus:
        if type(uid) is not int or uid <= 0:
            raise ConfigError("A positive selected game UID is required.")
        if game == "genshin":
            fetch = self._client.get_genshin_notes
            normalize = normalize_genshin
        elif game == "hsr":
            fetch = self._client.get_starrail_notes
            normalize = normalize_hsr
        else:
            fetch = self._client.get_zzz_notes
            normalize = normalize_zzz
        data = await request_with_retry(
            lambda: fetch(uid, autoauth=False, return_raw_data=True),
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        )
        return normalize(data, now=self._now())

    async def check_in_state(self, game: GameId, uid: int) -> CheckInState:
        raise UnsupportedDataError("Check-in is not implemented yet.")

    async def claim_check_in(self, game: GameId, uid: int) -> CheckInReceipt:
        raise UnsupportedDataError("Check-in is not implemented yet.")
