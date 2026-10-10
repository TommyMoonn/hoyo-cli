# Development

Use Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). The project's
`.python-version` selects Python 3.12 for development.

## Local environment

From the project root:

```powershell
uv sync --locked
uv run hoyo --help
uv run hoyo --version
```

Prefix CLI commands with `uv run` to use the project environment. The checked-in
`uv.lock` records resolved dependencies; `genshin` is pinned to `1.7.31`.

## Checks

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
```

Default tests use fake external services, block network connections, and require
no HoYoLAB account. They exclude `tests/live/`. Lint and formatting exclude
`specs/` and `docs/plans/` to preserve those documents.

## Optional live status test

After configuring an account with `hoyo init`, explicitly opt into real requests:

```powershell
uv run pytest --live-read-only -m live_read_only tests/live -q
```

This reads fresh status for every enabled game on the default account. It never
claims rewards, enables notes access, or reads or writes status cache. The flag is
required; selecting the marker alone does not enable network access.

To select another existing account in PowerShell:

```powershell
$env:HOYO_LIVE_ACCOUNT = "alt"
try {
    uv run pytest --live-read-only -m live_read_only tests/live -q
} finally {
    Remove-Item Env:HOYO_LIVE_ACCOUNT
}
```

Missing configuration or credentials skip the live test. API failures fail it
with a classified message. Credentials come from the configured backend and are
never passed on the command line. Keep real sessions out of test fixtures.

## Project references

- `src/hoyo_cli/`: commands, services, game adapters, and terminal rendering.
- `tests/`: offline checks and the opt-in live test.
- [Implementation plan](plans/2026-10-09-hoyo-cli.md): architecture and implementation requirements.

The plan references the original `specs/` directory, which must be supplied
separately if it is absent from your checkout. User guides are self-contained.
