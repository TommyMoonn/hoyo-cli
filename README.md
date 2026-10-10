# HoYo CLI

A local command-line utility for HoYoLAB status in Genshin Impact, Honkai: Star
Rail, and Zenless Zone Zero.

The planned workflow fetches fresh daily and weekly status for enabled games,
supports exact named-account selection, and falls back to clearly labeled
cached status no older than three hours. Daily reward claims require an explicit
`hoyo check-in` command. Config inspection is read-only.

## Current state

Phase 1 provides an installable `hoyo` command, help/version output, command
parsing, and offline developer tooling. Phase 2 adds typed config validation,
safe configuration persistence, and working config inspection and editing.
Status fetching, account setup, and check-in are planned and are not available
yet. Invoking those unfinished commands prints a concise "not configured yet"
error and exits with code `2`. No HoYoLAB requests or reward claims occur.

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

These forms parse now. Help, version, config inspection, and config editing are
functional. The status, setup, and check-in handlers remain planned.

| Command | Availability and behavior |
| --- | --- |
| `hoyo` or `hoyo status` | Planned: overview of all enabled games |
| `hoyo status <game>` | Planned: focused status for `genshin`, `hsr`, or `zzz` |
| `hoyo genshin`, `hoyo hsr`, `hoyo zzz` | Registered aliases for `hoyo status <game>` |
| `hoyo check-in [game]` | Planned: explicit daily reward claim |
| `hoyo init` | Planned: interactive account setup |
| `hoyo config` | Available: inspect non-secret configuration |
| `hoyo config --edit` | Available: open valid configuration in the selected editor |

`--account NAME` and `--no-color` can appear before or after the command name.
`--detailed` is accepted at the root and on status commands and game aliases.
An account supplied on the command overrides one supplied at the root; boolean
flags remain enabled if supplied in either position. Account identifiers are
passed through exactly, including case. Config commands validate the requested
account; status and setup will use the same validator when implemented.

For working command-specific help:

```powershell
uv run hoyo status --help
uv run hoyo check-in --help
uv run hoyo config --help
```

## Configuration

Configuration lives at `~/.hoyo/config.toml`. Until interactive setup arrives,
create it manually using the [version 1 example](specs/config.md). Then run:

```powershell
uv run hoyo config
uv run hoyo config --account alt
uv run hoyo config --edit
```

Inspection prints the config path, default account, configured game order,
display policy, account backend names, and enabled games with their UIDs.
It prints all configured accounts and validates any explicit account selection.
Credential files and keyring values are never read by inspection, and unknown
config values are not printed. Invalid configuration produces exit code `2`
with a field or file path and remains untouched.

Editing uses `VISUAL`, falling back to `EDITOR`. Set one to an editor executable
and its arguments, quoting paths that contain spaces. The editor is launched
without shell expansion. If neither is set or launching fails, the command
prints the config path and an actionable error. The config must exist and pass
validation before launch; repair malformed TOML in your editor directly.

The configuration repository preserves comments and unknown fields during
targeted account updates. It validates a replacement before writing it and uses
an atomic replacement so failed writes leave the original bytes intact.
Writes refuse symbolic-link config paths, including dangling links, to avoid
disconnecting managed dotfiles. Edit the managed target directly in that case.

## Project layout

- `src/hoyo_cli/`: application package and command routing.
- `tests/`: offline tests.
- `specs/`: pre-existing authoritative [specifications](specs/README.md).
- `docs/plans/`: [repository implementation plan](docs/plans/2026-10-09-hoyo-cli.md),
  linked to the [original supplied plan](specs/implementation-plan.md).

## Credential hygiene

Never put real HoYoLAB cookies or credentials in source code, test fixtures, or
shared archives. Configuration lives at `~/.hoyo/config.toml`; planned preferred
credential storage uses the OS keyring. An explicit plaintext fallback will use
the separate `~/.hoyo/credentials.toml` file. The project ignores `.hoyo/`,
`credentials.toml`, cookie files, and local environment files.
