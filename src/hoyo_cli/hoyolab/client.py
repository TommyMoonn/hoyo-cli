"""Pinned upstream integration with one application-owned request policy."""

import asyncio
import inspect
import math
import ssl
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, TypeVar, cast

import aiohttp
import genshin
from genshin.client.manager import managers

from hoyo_cli.auth.credentials import Credentials
from hoyo_cli.errors import (
    AlreadyCheckedInError,
    AppError,
    AuthenticationError,
    ConfigError,
    ErrorCode,
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


def _require_selected_uid(uid: int) -> None:
    if type(uid) is not int or uid <= 0:
        raise ConfigError("A positive selected game UID is required.")


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


def _rate_limited() -> NetworkError:
    return NetworkError(
        "HoYoLAB rate limited this request.",
        code=ErrorCode.RATE_LIMITED,
        hint="Wait before trying again.",
    )


def _server_failed() -> NetworkError:
    return NetworkError(
        "HoYoLAB server failed to complete the request.",
        code=ErrorCode.SERVER_ERROR,
        hint="Wait for HoYoLAB to recover, then retry.",
    )


def _classify(error: Exception) -> AppError:
    if isinstance(error, AppError):
        return error
    if isinstance(error, genshin.errors.AlreadyClaimed):
        return AlreadyCheckedInError()
    if isinstance(error, genshin.errors.DailyGeetestTriggered):
        return AppError(
            "HoYoLAB requires verification.",
            code=ErrorCode.VERIFICATION_REQUIRED,
            hint="Complete verification and check-in on the HoYoLAB website.",
        )
    if isinstance(error, genshin.errors.InvalidCookies):
        return AuthenticationError("HoYoLAB rejected the session.")
    if isinstance(error, TimeoutError):
        return NetworkError(
            "HoYoLAB request timed out.", code=ErrorCode.NETWORK_TIMEOUT
        )
    # TLS exceptions also inherit from connection errors. Classify them first.
    if isinstance(error, (aiohttp.ClientSSLError, ssl.SSLError)):
        return NetworkError(
            "Unable to verify the HoYoLAB secure connection.",
            transient=False,
            code=ErrorCode.TLS_FAILURE,
            hint="Check your system clock and trusted certificates, then retry.",
        )
    if isinstance(error, aiohttp.ClientResponseError):
        if error.status in (401, 403):
            return AuthenticationError(
                "HoYoLAB rejected authorization for this request."
            )
        if error.status == 429:
            return _rate_limited()
        if 500 <= error.status < 600:
            return _server_failed()
        return UnsupportedDataError(
            "HoYoLAB rejected the request or returned unsupported data."
        )
    if isinstance(
        error,
        (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, ConnectionError),
    ):
        return NetworkError("Unable to connect to HoYoLAB.")
    if isinstance(error, genshin.errors.GenshinException):
        if error.retcode in (-110, 1028, 10101):
            return _rate_limited()
        if error.retcode in (-1, 10307):
            return _server_failed()
        if error.retcode == 0:
            return UnsupportedDataError(
                "HoYoLAB returned an unsupported or changed response."
            )
        return AppError(
            "HoYoLAB could not complete the request.",
            code=ErrorCode.API_REJECTED,
            hint="Check account access and HoYoLAB privacy settings, then retry.",
        )
    if isinstance(
        error,
        (ValueError, TypeError, KeyError, AttributeError, aiohttp.ContentTypeError),
    ):
        return UnsupportedDataError(
            "HoYoLAB returned an unsupported or changed response."
        )
    return AppError("HoYoLAB request failed.")


async def request_with_retry(
    operation: Callable[[], Awaitable[T]], *, timeout_seconds: float
) -> T:
    return await _request(operation, timeout_seconds=timeout_seconds, attempts=2)


async def _request(
    operation: Callable[[], Awaitable[T]], *, timeout_seconds: float, attempts: int
) -> T:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Request timeout must be finite and positive.")
    for attempt in range(attempts):
        try:
            return await asyncio.wait_for(operation(), timeout=timeout_seconds)
        except Exception as error:
            classified = _classify(error)
            if attempt + 1 < attempts and classified.retryable:
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


class GatewayClient(Protocol):
    uids: dict[genshin.Game, int]

    async def get_reward_info(self, *, game: genshin.Game) -> object: ...

    async def claim_daily_reward(
        self, *, game: genshin.Game, reward: Literal[False]
    ) -> None: ...

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
        client: GatewayClient | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        # Upstream opens and closes sessions within each individual request;
        # this gateway holds no persistent session needing a separate close.
        self._client = client if client is not None else create_client(credentials)
        self._now = now if now is not None else lambda: datetime.now(UTC)

    async def fetch_status(self, game: GameId, uid: int) -> GameStatus:
        _require_selected_uid(uid)
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
        selected = self._select_check_in_game(game, uid)
        info = await request_with_retry(
            lambda: self._client.get_reward_info(game=selected),
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        )
        if (
            not isinstance(info, genshin.models.DailyRewardInfo)
            or type(info.signed_in) is not bool
            or type(info.claimed_rewards) is not int
            or info.claimed_rewards < 0
        ):
            raise UnsupportedDataError(
                "HoYoLAB returned an unsupported or changed check-in state."
            )
        return CheckInState(info.signed_in)

    async def claim_check_in(self, game: GameId, uid: int) -> CheckInReceipt:
        selected = self._select_check_in_game(game, uid)
        try:
            await _request(
                # The sign endpoint confirms the action but returns no reward.
                # Disable upstream's follow-up reads, which can fail after a
                # successful mutation and otherwise obscure its real outcome.
                lambda: self._client.claim_daily_reward(game=selected, reward=False),
                timeout_seconds=REQUEST_TIMEOUT_SECONDS,
                attempts=1,
            )
        except NetworkError as error:
            raise NetworkError(
                f"{error.message} Check-in may have completed; "
                "check today's state before trying again.",
                transient=False,
                code=error.code,
                hint="Check today's state on HoYoLAB before trying check-in again.",
            ) from None
        except AppError as error:
            if error.code in (ErrorCode.UNEXPECTED, ErrorCode.UNSUPPORTED_DATA):
                # Preserve the classified error; its recovery must account for
                # a write that could have completed before the response failed.
                error.hint = (
                    "Check today's state on HoYoLAB before trying check-in again."
                )
            raise
        return CheckInReceipt()

    def _select_check_in_game(self, game: GameId, uid: int) -> genshin.Game:
        _require_selected_uid(uid)
        selected = {
            "genshin": genshin.Game.GENSHIN,
            "hsr": genshin.Game.STARRAIL,
            "zzz": genshin.Game.ZZZ,
        }[game]
        # Overseas daily rewards are account/game scoped, with no UID parameter.
        # Keep the selected UID in the library's supported per-game mapping,
        # rather than allowing its character-selection fallback to choose one.
        self._client.uids[selected] = uid
        return selected
