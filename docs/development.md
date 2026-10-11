# Development

As I have mentioned, this project is built entirely using AI. I have not read a single line of code, so don't treat the code or the tests as a security audit. The commands below show how to run the existing checks.

> [!IMPORTANT]
> Everything below this is written by an LLM.

Use Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). The project's `.python-version` selects Python 3.12 for development.

## Local environment

From the project root:

```powershell
uv sync --locked
uv run hoyo --help
uv run hoyo --version
```

Prefix CLI commands with `uv run` to use the project environment. The checked-in `uv.lock` records resolved dependencies; `genshin` is pinned to `1.7.31`.

## Checks

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
```

Default tests use fake external services, block network connections, and require no HoYoLAB login. They exclude `tests/live/`. Lint and formatting exclude `specs/` and `docs/plans/`.

## Optional live status test

Real HoYoLAB requests are opt-in. First set up a profile, then run:

```powershell
uv run pytest --live-read-only -m live_read_only tests/live -q
```

This reads fresh status for every enabled game on the default profile. It doesn't claim rewards, enable notes access, or read or write the status cache. The flag is required; selecting the test marker alone doesn't enable live requests.

To test another profile in PowerShell:

```powershell
$env:HOYO_LIVE_PROFILE = "america"
try {
    uv run pytest --live-read-only -m live_read_only tests/live -q
} finally {
    Remove-Item Env:HOYO_LIVE_PROFILE
}
```

Missing configuration or credentials cause the live test to skip. API failures fail the test with a classified message. Credentials come from the configured backend, never from command-line arguments. Don't put live sessions in test fixtures.

## Project references

- `src/hoyo_cli/`: CLI commands, authentication, services, game adapters, and terminal rendering.
- `tests/`: offline checks and the opt-in live status test.
- [`specs/`](../specs/): project specifications.
- [Implementation plan](plans/2026-10-09-hoyo-cli.md): initial architecture and implementation milestones.
