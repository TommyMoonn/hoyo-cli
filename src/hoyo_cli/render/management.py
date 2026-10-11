"""Read-only profile/config hierarchy using the shared semantic presenter."""

from pathlib import Path

from hoyo_cli.config.editor import describe_editor, editor_available
from hoyo_cli.config.models import (
    AppConfig,
    ProfileConfig,
    ordered_games,
    resolve_login,
)
from hoyo_cli.models.common import GAME_IDS, GameId
from hoyo_cli.render.setup import SetupPresenter


class ManagementPresenter(SetupPresenter):
    def _profile(
        self,
        config: AppConfig,
        name: str,
        profile: ProfileConfig,
        games: tuple[GameId, ...],
        *,
        include_backend: bool = False,
    ) -> None:
        self.console.print()
        self.heading(f"Profile: {name}")
        if name == config.default_profile:
            self.current("  Default")
        self.detail("Login", profile.login, indent=2)
        if include_backend:
            self.detail(
                "Credential backend",
                resolve_login(config, profile).credential_backend,
                indent=2,
            )
        for game in games:
            selection = profile.games.get(game)
            value = (
                f"{'Enabled' if selection.enabled else 'Disabled'}, UID {selection.uid}"
                if selection is not None
                else "Not configured"
            )
            self.detail(game, value, indent=4)
        if not any(selection.enabled for selection in profile.games.values()):
            self.message("    No enabled games")

    def profiles(self, config: AppConfig) -> None:
        self.heading("PROFILES")
        for name, profile in config.profiles.items():
            self._profile(config, name, profile, GAME_IDS)
        self.console.print()
        self.detail("Switch default", "hoyo profile use <name>")
        self.detail("Add profile", "hoyo profile add [name]")
        self.detail("Edit profile", "hoyo profile edit <name>")

    def config(self, config: AppConfig, path: Path) -> None:
        self.heading("GLOBAL PREFERENCES")
        self.detail("Config", str(path))
        self.current(f"Default profile: {config.default_profile}")
        self.detail(
            "Game order", ", ".join(config.game_order) or "(canonical fallback)"
        )
        self.detail("Display color", config.display_color)
        self.console.print()
        self.heading("EDITOR")
        self.detail("Editor", describe_editor(config.editor_command))
        available = editor_available(config.editor_command)
        self.detail("Editor executable", "available" if available else "unavailable")
        if not available:
            self.message("Choose an editor: hoyo config editor")
        self.console.print()
        self.heading("PROFILE SELECTIONS")
        for name, profile in config.profiles.items():
            self._profile(
                config, name, profile, ordered_games(config), include_backend=True
            )
        self.console.print()
        self.detail("Add profile", "hoyo profile add")
        self.detail("Update profile or session", "hoyo profile edit <name>")
        self.detail("Switch default", "hoyo profile use <name>")
        self.detail("Change game order", "hoyo config order")
        self.detail("Change editor", "hoyo config editor")
