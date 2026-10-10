# HoYo CLI

A local command-line utility for HoYoLAB status in Genshin Impact, Honkai: Star
Rail, and Zenless Zone Zero.

The planned workflow fetches fresh daily and weekly status for enabled games,
supports exact named-account selection, and falls back to clearly labeled
cached status no older than three hours. Daily reward claims require an explicit
`hoyo check-in` command. Config inspection is read-only.

## Current state

Phase 1 provides an installable `hoyo` command, help/version output, command
parsing, and offline developer tooling. Status fetching, account setup,
check-in, and configuration handling are planned and are not available yet.
Invoking those commands currently prints a concise "not configured yet" error
and exits with code `2`. No HoYoLAB requests or reward claims occur.

## Prerequisites

Use Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). Development uses
Python 3.12, recorded in `.python-version`.

## Development

From the project root:

```powershell
uv sync --locked
uv run hoyo --help
uv run hoyo --version
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
```

The checked-in `uv.lock` records the resolved environment. The API client is
pinned to `genshin==1.7.31`; its cookie integration accepts the installed
`browser-cookie3` version. The test suite blocks network connections and needs
no HoYoLAB account. Lint and formatting exclude the original specs and the
repository plan copy to preserve their text.

To install the command for local use:

```powershell
uv tool install .
hoyo --help
hoyo --version
uv tool uninstall hoyo-cli
```

If uv reports that its tool bin directory is absent from `PATH`, follow its
path setup instructions and open a new shell.

## Command grammar

These forms parse now. Only help and version are fully functional; each command
handler remains pending implementation in later phases.

| Command | Planned behavior |
| --- | --- |
| `hoyo` or `hoyo status` | Overview of all enabled games |
| `hoyo status <game>` | Focused status for `genshin`, `hsr`, or `zzz` |
| `hoyo genshin`, `hoyo hsr`, `hoyo zzz` | Aliases for `hoyo status <game>` |
| `hoyo check-in [game]` | Explicit daily reward claim |
| `hoyo init` | Interactive account setup |
| `hoyo config` | Inspect non-secret configuration |
| `hoyo config --edit` | Open configuration in the configured editor |

`--account NAME` and `--no-color` can appear before or after the command name.
`--detailed` is accepted at the root and on status commands and game aliases.
An account supplied on the command overrides one supplied at the root; boolean
flags remain enabled if supplied in either position. Account identifiers are
passed through exactly, including case. Account existence validation arrives
with configuration support.

For working command-specific help:

```powershell
uv run hoyo status --help
uv run hoyo check-in --help
uv run hoyo config --help
```

## Project layout

- `src/hoyo_cli/`: application package and command routing.
- `tests/`: offline tests.
- `specs/`: pre-existing authoritative [specifications](specs/README.md).
- `docs/plans/`: [repository implementation plan](docs/plans/2026-10-09-hoyo-cli.md),
  linked to the [original supplied plan](specs/implementation-plan.md).

## Credential hygiene

Never put real HoYoLAB cookies or credentials in source code, test fixtures, or
shared archives. Planned configuration lives at `~/.hoyo/config.toml`; preferred
credential storage uses the OS keyring. An explicit plaintext fallback will use
the separate `~/.hoyo/credentials.toml` file. The project ignores `.hoyo/`,
`credentials.toml`, cookie files, and local environment files.
