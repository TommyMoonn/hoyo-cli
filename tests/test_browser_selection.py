"""Offline capability, metadata discovery, and deliberate selection contracts."""

from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import pytest
from test_init import FakeBrowser, FakeDiscovery, FakeIO

from hoyo_cli.auth.browser_import import BrowserCookieAdapter, BrowserSession
from hoyo_cli.auth.credentials import manual_credentials
from hoyo_cli.auth.plaintext_backend import PlaintextStore
from hoyo_cli.commands import init
from hoyo_cli.errors import BrowserImportError, ErrorCode


def adapter(tmp_path: Path, platform: str = "win32") -> BrowserCookieAdapter:
    return BrowserCookieAdapter(
        platform=platform,
        environment={
            "LOCALAPPDATA": str(tmp_path / "Local"),
            "APPDATA": str(tmp_path / "Roaming"),
        },
    )


WINDOWS_LAYOUTS = (
    ("chrome", "Google/Chrome/User Data", "Stable"),
    ("chrome", "Google/Chrome Beta/User Data", "Beta"),
    ("chrome", "Google/Chrome Dev/User Data", "Dev"),
    ("edge", "Microsoft/Edge/User Data", "Stable"),
    ("edge", "Microsoft/Edge Beta/User Data", "Beta"),
    ("edge", "Microsoft/Edge Dev/User Data", "Dev"),
    ("edge", "Microsoft/Edge SxS/User Data", "Canary"),
    ("brave", "BraveSoftware/Brave-Browser/User Data", "Stable"),
    ("brave", "BraveSoftware/Brave-Browser-Beta/User Data", "Beta"),
    ("brave", "BraveSoftware/Brave-Browser-Dev/User Data", "Dev"),
    ("brave", "BraveSoftware/Brave-Browser-Nightly/User Data", "Nightly"),
    ("chromium", "Chromium/User Data", "Stable"),
)


@pytest.mark.parametrize(("browser", "location", "channel"), WINDOWS_LAYOUTS)
@pytest.mark.parametrize("cookie_location", ["Network/Cookies", "Cookies"])
def test_windows_channels_offer_only_existing_databases_without_secret_reads(
    tmp_path: Path, browser: str, location: str, channel: str, cookie_location: str
) -> None:
    root = tmp_path / "Local" / location
    database = root / "Default" / cookie_location
    database.parent.mkdir(parents=True)
    database.touch()
    (root / "Profile 1").mkdir()  # Empty/deleted cookie storage is not usable.
    candidate = adapter(tmp_path)
    with (
        patch("hoyo_cli.auth.browser_import.read_browser_profile") as reader,
        patch.object(Path, "open", side_effect=AssertionError("No cookie/key reads")),
    ):
        assert candidate.list_sessions() == (
            BrowserSession(browser, browser.title(), True),
        )
        (profile,) = candidate.list_profiles(browser)
        assert profile.label == f"{channel} / Default"
        assert profile.cookie_file == database
        assert profile.key_file == root / "Local State"
        reader.assert_not_called()


@pytest.mark.parametrize("channel", ["Stable", "Next", "Developer"])
@pytest.mark.parametrize("cookie_location", ["Network/Cookies", "Cookies"])
def test_opera_channels_use_root_cookie_storage(
    tmp_path: Path, channel: str, cookie_location: str
) -> None:
    root = tmp_path / "Roaming/Opera Software" / f"Opera {channel}"
    database = root / cookie_location
    database.parent.mkdir(parents=True)
    database.touch()
    (profile,) = adapter(tmp_path).list_profiles("opera")
    assert profile.label == channel
    assert profile.cookie_file == database
    assert profile.key_file == root / "Local State"


def test_profile_tokens_do_not_shift_when_an_earlier_profile_is_added_or_deleted(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Local/Microsoft/Edge/User Data"
    database = root / "Profile 2/Network/Cookies"
    database.parent.mkdir(parents=True)
    database.touch()
    candidate = adapter(tmp_path)
    (selected,) = candidate.list_profiles("edge")
    earlier = root / "Default/Cookies"
    earlier.parent.mkdir()
    earlier.touch()
    profiles = candidate.list_profiles("edge")
    assert profiles[1] == selected
    database.unlink()
    assert candidate.list_profiles("edge") == (profiles[0],)
    with patch("hoyo_cli.auth.browser_import.read_browser_profile") as reader:
        with pytest.raises(BrowserImportError) as caught:
            candidate.read(selected.source_id)
        assert caught.value.code == ErrorCode.BROWSER_PROFILE_MISSING
        assert "choose it again" in (caught.value.hint or "")
        reader.assert_not_called()


def test_empty_installed_root_is_a_source_but_not_a_usable_profile(
    tmp_path: Path,
) -> None:
    (tmp_path / "Local/Google/Chrome/User Data/Default").mkdir(parents=True)
    candidate = adapter(tmp_path)
    assert candidate.list_sessions() == (BrowserSession("chrome", "Chrome", True),)
    with pytest.raises(BrowserImportError) as caught:
        candidate.list_profiles("chrome")
    assert caught.value.code == ErrorCode.BROWSER_PROFILE_MISSING


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_library_sources_do_not_claim_installed_profiles(
    tmp_path: Path, platform: str
) -> None:
    candidate = adapter(tmp_path, platform)
    with patch.object(Path, "is_dir", side_effect=AssertionError("No discovery")):
        sessions = candidate.list_sessions()
        assert [s.source_id for s in sessions] == [
            "chrome",
            "edge",
            "firefox",
            "chromium",
            "opera",
            "brave",
        ]
        for session in sessions:
            assert not session.supports_profiles
            assert "library-selected" in session.browser_label
            assert "installation unverified" in session.browser_label
            (profile,) = candidate.list_profiles(session.source_id)
            assert profile.cookie_file is None and profile.key_file is None


def test_unknown_browser_platform_is_rejected_before_reader(tmp_path: Path) -> None:
    candidate = adapter(tmp_path, "freebsd")
    assert candidate.list_sessions() == ()
    with pytest.raises(BrowserImportError) as caught:
        candidate.list_profiles("chrome")
    assert caught.value.code == ErrorCode.BROWSER_UNSUPPORTED


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_library_controlled_selection_offers_manual_before_reading(
    tmp_path: Path, platform: str, capsys: pytest.CaptureFixture[str]
) -> None:
    class ManualIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Browser source":
                assert "library-selected" in choices[0]
                return "Enter ltuid_v2 / ltoken_v2 manually"
            return super().choose(prompt, choices)

    from hoyo_cli.auth.browser_import import SystemBrowserImporter

    browser = SystemBrowserImporter(adapter(tmp_path, platform))
    with patch.object(browser, "import_session") as reader:
        init.run_init(
            tmp_path / "config.toml",
            tmp_path / "credentials.toml",
            ManualIO(browser=True),
            browser,
            FakeDiscovery(),
            no_color=True,
        )
        reader.assert_not_called()
    assert PlaintextStore(tmp_path / "credentials.toml").get(
        "main"
    ) == manual_credentials("123", "test-secret-123")
    output = capsys.readouterr().out
    assert "Browser/profile selection not available on this platform" in output
    assert "Session verified" in output
    assert "test-secret-123" not in output


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.BROWSER_PROFILE_MISSING,
        ErrorCode.BROWSER_UNSUPPORTED,
        ErrorCode.BROWSER_DECRYPTION,
    ],
)
def test_unavailable_profile_goes_to_reselection_without_a_retry_loop(
    tmp_path: Path,
    code: ErrorCode,
) -> None:
    class ReselectIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Browser source":
                return choices[0]
            if prompt == "Browser recovery":
                assert "Retry browser import" not in choices
                assert choices[0].endswith("(recommended)")
                return choices[0]
            return super().choose(prompt, choices)

    from hoyo_cli.auth.browser_import import browser_error

    browser = FakeBrowser()
    with (
        patch.object(
            browser,
            "list_profiles",
            side_effect=browser_error(code),
        ) as profiles,
        patch.object(browser, "import_session") as reader,
    ):
        init.run_init(
            tmp_path / "config.toml",
            tmp_path / "credentials.toml",
            ReselectIO(browser=True),
            browser,
            FakeDiscovery(),
            no_color=True,
        )
        profiles.assert_called_once_with("chrome")
        reader.assert_not_called()
    assert PlaintextStore(tmp_path / "credentials.toml").get(
        "main"
    ) == manual_credentials("123", "test-secret-123")


def test_firefox_deleted_and_empty_entries_are_excluded_and_order_is_stable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Roaming/Mozilla/Firefox"
    for name in ("one", "two", "empty"):
        directory = root / "Profiles" / name
        directory.mkdir(parents=True)
        if name != "empty":
            (directory / "cookies.sqlite").touch()
    sections = [
        f"[Profile{index}]\nName={name}\nIsRelative=1\nPath=Profiles/{name}\n"
        for index, name in enumerate(("two", "deleted", "one", "empty"))
    ]
    ini = root / "profiles.ini"
    ini.write_text("\n".join(sections), encoding="utf-8")
    candidate = adapter(tmp_path)
    profiles = candidate.list_profiles("firefox")
    assert [p.label for p in profiles] == ["Stable / one", "Stable / two"]
    ini.write_text("\n".join(reversed(sections)), encoding="utf-8")
    assert candidate.list_profiles("firefox") == profiles
    # A removed registration cannot be imported through a stale menu token,
    # even when Firefox has left its former cookie database on disk.
    ini.write_text("[General]\nStartWithLastProfile=1\n", encoding="utf-8")
    with pytest.raises(BrowserImportError):
        candidate.list_profiles("firefox")
    with patch("hoyo_cli.auth.browser_import.read_browser_profile") as reader:
        with pytest.raises(BrowserImportError) as caught:
            candidate.read(profiles[0].source_id)
        assert caught.value.code == ErrorCode.BROWSER_PROFILE_MISSING
        reader.assert_not_called()


@pytest.mark.parametrize("width", [48, 70, 100])
@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_browser_selection_transcripts_wrap_without_secret_or_progress_output(
    tmp_path: Path,
    width: int,
    platform: str,
) -> None:
    import io

    from click.testing import CliRunner
    from rich.console import Console

    from hoyo_cli.auth.browser_import import SystemBrowserImporter
    from hoyo_cli.cli import main

    root = tmp_path / "Local/Microsoft/Edge/User Data"
    database = root / "Default/Network/Cookies"
    database.parent.mkdir(parents=True)
    database.touch()
    stream = io.StringIO()
    browser = SystemBrowserImporter(adapter(tmp_path, platform))

    def console(**kwargs: object) -> Console:
        return Console(width=width, file=stream, color_system=None)

    with (
        patch(
            "hoyo_cli.render.setup.make_console",
            side_effect=console,
        ),
        patch.object(init, "config_path", return_value=tmp_path / "config.toml"),
        patch.object(
            init, "credentials_path", return_value=tmp_path / "credentials.toml"
        ),
        patch.object(init, "SystemBrowserImporter", return_value=browser),
        patch.object(init, "HoYoLABDiscovery", return_value=FakeDiscovery()),
        patch.object(
            browser,
            "import_session",
            return_value=manual_credentials(
                "123",
                "test-secret-123",
            ),
        ) as reader,
    ):
        # First profile, browser source, first browser, explicit profile on
        # Windows, then character choices, order, storage, consent, save.
        answers = "main\n2\n1\n" + ("1\n" if platform == "win32" else "")
        answers += "1\n1\n1\ny\n2\ny\n1\n"
        result = CliRunner().invoke(
            main, ["init", "--no-color"], input=answers, terminal_width=width
        )
        assert result.exit_code == 0, result.output
        reader.assert_called_once()
        if platform == "win32":
            (selected,) = browser.list_profiles("edge")
            reader.assert_called_once_with(selected.source_id)
        else:
            reader.assert_called_once_with("chrome")
    transcript = stream.getvalue()
    assert all(len(line) <= width for line in transcript.splitlines())
    assert "Found HoYoLAB sessions" not in transcript
    assert "test-secret-123" not in transcript + result.output
    assert "\x1b" not in transcript + result.output
    assert "Reading browser cookies" not in transcript + result.output
    # The ordinary status sentence remains useful in redirected output; only
    # the transient progress display must be suppressed.
    assert (transcript + result.output).count(
        "Cookies found. Verifying HoYoLAB session."
    ) == 1
    assert "\r" not in transcript + result.output
