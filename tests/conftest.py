"""Shared offline CLI test fixtures."""

import socket
from collections.abc import Callable
from contextvars import ContextVar
from typing import NoReturn, cast

import pytest
from click.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def prevent_network(monkeypatch: pytest.MonkeyPatch) -> None:
    constructing_pair: ContextVar[bool] = ContextVar("constructing_pair", default=False)
    original_connect = socket.socket.connect
    original_pair = socket.socketpair

    def reject_network(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("The default test suite must remain offline.")

    def guarded_connect(sock: socket.socket, address: object) -> None:
        if constructing_pair.get():
            cast(Callable[[socket.socket, object], None], original_connect)(
                sock, address
            )
        else:
            reject_network()

    def local_socketpair(
        *args: object, **kwargs: object
    ) -> tuple[socket.socket, socket.socket]:
        token = constructing_pair.set(True)
        try:
            return cast(
                Callable[..., tuple[socket.socket, socket.socket]], original_pair
            )(*args, **kwargs)
        finally:
            constructing_pair.reset(token)

    # Windows builds socketpair through a temporary loopback connection. This
    # allowance ends before any test coroutine or application request can run.
    monkeypatch.setattr(socket, "socketpair", local_socketpair)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_network)
    monkeypatch.setattr(socket, "create_connection", reject_network)
