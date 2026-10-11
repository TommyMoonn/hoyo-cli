"""Read-only profile listing and explicit persistent default selection."""

from hoyo_cli.config.paths import config_path
from hoyo_cli.config.repository import (
    commit_config,
    load_config,
    prepare_default_profile,
)
from hoyo_cli.render.management import ManagementPresenter


def list_profiles(*, no_color: bool = False) -> None:
    config = load_config(config_path())
    ManagementPresenter(
        no_color=no_color, configured_color=config.display_color
    ).profiles(config)


def use_profile(name: str, *, no_color: bool = False) -> None:
    config = load_config(config_path())
    prepared = prepare_default_profile(config_path(), name)
    presenter = ManagementPresenter(
        no_color=no_color, configured_color=config.display_color
    )
    if prepared.content.encode("utf-8") == prepared.original:
        presenter.message(f"{name} is already the default profile.")
        return
    commit_config(prepared)
    presenter.success(f"Default profile: {name}")
