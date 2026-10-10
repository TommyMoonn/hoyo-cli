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
Phase 3 adds interactive account setup, HoYoLAB linked-character discovery,
browser-cookie import, and explicit keyring or plaintext credential storage.
Status fetching and check-in remain unavailable. Those unfinished commands
print a concise "not configured yet" error and exit with code `2`.
Setup makes a read-only linked-account request; no command claims rewards yet.

Phase 4 adds immutable status snapshots, a bounded read-only HoYoLAB gateway,
Genshin notes normalization, and compact/focused/detailed Rich views. The views
have explicit wide (80+), medium (60-79), and narrow (below 60) layouts.
The gateway disables automatic notes authorization and uses at most two
10-second attempts for transient read failures. Authentication and unsupported
data failures are not retried. Application retry control is isolated against
the pinned library's internal retries, preserving its per-request session cleanup.
Status command wiring and HSR/ZZZ mapping remain Phase 5 work; these internal
Genshin capabilities are not yet exposed through `hoyo status` or game aliases.

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

Help, version, account setup, config inspection, and config editing are
functional. Status and check-in remain planned.

| Command | Availability and behavior |
| --- | --- |
| `hoyo` or `hoyo status` | Planned: overview of all enabled games |
| `hoyo status <game>` | Planned: focused status for `genshin`, `hsr`, or `zzz` |
| `hoyo genshin`, `hoyo hsr`, `hoyo zzz` | Registered aliases for `hoyo status <game>` |
| `hoyo check-in [game]` | Planned: explicit daily reward claim |
| `hoyo init` | Available: interactive account setup or reconfiguration |
| `hoyo config` | Available: inspect non-secret configuration |
| `hoyo config --edit` | Available: open valid configuration in the selected editor |

`--account NAME` and `--no-color` can appear before or after the command name.
`--detailed` is accepted at the root and on status commands and game aliases.
An account supplied on the command overrides one supplied at the root; boolean
flags remain enabled if supplied in either position. Account identifiers are
passed through exactly, including case. Config commands validate the requested
account. On an existing config, setup also requires an exact account when
`--account` is supplied; use the setup menu to create a new account.

For working command-specific help:

```powershell
uv run hoyo status --help
uv run hoyo check-in --help
uv run hoyo config --help
```

## Configuration

Configuration lives at `~/.hoyo/config.toml`. Configure it interactively or use
the [version 1 example](specs/config.md):

```powershell
uv run hoyo init
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

## Account setup

`hoyo init` asks for an account name or existing account, browser import or a
hidden full Cookie header, a linked UID and enabled flag for each discovered
game, default-account selection, game order, and a credential backend.
It shows an explicit numbered browser choice, even for a single session.
Browser scanning is optional and restricted to HoYoLAB authentication cookies
through supported Chrome, Edge, Firefox, Chromium, Opera, and Brave loaders.
Browser access failures offer manual input without choosing another browser.

For manual input, paste the semicolon-separated `Cookie` header from an
authenticated HoYoLAB request, not the `Cookie:` label. The parser keeps only
recognized authentication fields. It accepts `ltuid` with `ltoken`, or
`ltuid_v2` with `ltoken_v2`; matching account-ID aliases and additional v2
credential fields are preserved. Missing, duplicate, or conflicting fields
fail without echoing the header. The upstream
[authentication reference](https://seria.is-a.dev/genshin.py/authentication/)
describes obtaining cookies. Treat the complete header as a password.

The keyring is offered first. Plaintext requires explicit selection or consent
after an unavailable-keyring error, plus a setup warning. Secrets go in the
`hoyo-cli` keyring service under the exact account name, or separately in
`~/.hoyo/credentials.toml`. POSIX file mode is `0600`. On Windows, chmod does
not create a private ACL; the file inherits directory permissions. Prefer the
OS keyring and keep the fallback directory private.

Existing account credentials are replaced only after consent. Setup requires
a new imported or pasted session; declining replacement cancels setup without
changing files. Switching backends does not migrate or delete the old backend's
secrets. Remove old
entries yourself if no longer needed. Malformed config or plaintext credential
files must be repaired manually; malformed keyring entries must be repaired or
removed before setup.

Setup preserves other accounts, comments, and unknown config fields. For the
account being reconfigured, games not rediscovered are disabled while their
prior UIDs are retained for manual recovery. Setup warns about previously
enabled games that will be disabled if saved. Only a UID selected from the
current session's linked characters can be enabled, and at least one such game
must be enabled. Nothing is written until final consent and config validation.
If the config changes after preparation, saving fails.
If config persistence fails after storing credentials, setup restores the
prior backend state, including exact plaintext bytes or file absence. If
rollback itself fails, the error requests backend repair. This is recovery
within the running process, not a cross-backend transaction against process
termination or concurrent credential writers.

## Project layout

- `src/hoyo_cli/`: application package and command routing.
- `tests/`: offline tests.
- `specs/`: pre-existing authoritative [specifications](specs/README.md).
- `docs/plans/`: [repository implementation plan](docs/plans/2026-10-09-hoyo-cli.md),
  linked to the [original supplied plan](specs/implementation-plan.md).

## Credential hygiene

Never put real HoYoLAB cookies or credentials in source code, test fixtures, or
shared archives. Configuration lives at `~/.hoyo/config.toml`; preferred
credential storage uses the OS keyring. An explicit plaintext fallback uses
the separate `~/.hoyo/credentials.toml` file. The project ignores `.hoyo/`,
`credentials.toml`, cookie files, and local environment files.
