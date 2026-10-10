"""Minimal read-only setup client; full gateway policy belongs to Phase 4."""

import asyncio
from collections.abc import Sequence
from typing import Protocol

import aiohttp
import genshin

from hoyo_cli.auth.credentials import Credentials
from hoyo_cli.errors import AppError, AuthenticationError


class DiscoveryClient(Protocol):
    async def get_game_accounts(self) -> Sequence[genshin.models.GenshinAccount]: ...


def create_client(credentials: Credentials) -> DiscoveryClient:
    # The pinned client's CookieManager._request owns an aiohttp session via
    # async-with for every request. Client has no persistent session/close method.
    return genshin.Client(dict(credentials.fields))


async def _fetch(credentials: Credentials) -> Sequence[genshin.models.GenshinAccount]:
    try:
        client = create_client(credentials)
        return await asyncio.wait_for(client.get_game_accounts(), timeout=20)
    except genshin.errors.InvalidCookies:
        raise AuthenticationError(
            "HoYoLAB rejected the session. Sign in again and rerun hoyo init."
        ) from None
    except TimeoutError:
        raise AppError("HoYoLAB character discovery timed out; retry setup.") from None
    except aiohttp.ClientError:
        raise AppError(
            "Unable to connect to HoYoLAB for character discovery."
        ) from None
    except genshin.errors.GenshinException:
        raise AppError("HoYoLAB could not return linked game accounts.") from None
    except Exception:
        raise AppError("Unable to read the HoYoLAB linked-account response.") from None


def fetch_linked_accounts(
    credentials: Credentials,
) -> Sequence[genshin.models.GenshinAccount]:
    return asyncio.run(_fetch(credentials))
