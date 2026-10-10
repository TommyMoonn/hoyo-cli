# HoYo CLI

A local command-line utility for HoYoLAB status in Genshin Impact, Honkai: Star
Rail, and Zenless Zone Zero.

The CLI fetches fresh daily and weekly status for enabled games,
supports exact named-account selection, and falls back to clearly labeled
cached status no older than three hours. Daily reward claims require an explicit
`hoyo check-in` command. Config inspection is read-only.

## Local installation and first use

Use Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). Development uses
Python 3.12, recorded in `.python-version`. From this project directory:

```powershell
uv sync --locked
uv run hoyo --help
uv tool install .
hoyo --help
hoyo init
hoyo
hoyo genshin --detailed
hoyo --account alt
hoyo check-in zzz
hoyo config --edit
```

Create `alt` using `hoyo init` before selecting it. Setup and status need an
authenticated HoYoLAB session and internet access. Check-in is the only command
that claims rewards. To remove the installed tool:

```powershell
uv tool uninstall hoyo-cli
```

If uv reports that its tool bin directory is absent from `PATH`, follow its
path setup instructions and open a new shell. During development, prefix commands
with `uv run` to use the project environment.

## Status behavior

Compact, focused, and detailed views cover all three games. The views
have explicit wide (80+), medium (60-79), and narrow (below 60) layouts.
The gateway disables automatic notes authorization and uses at most two
10-second attempts for transient read failures. Authentication and unsupported
data failures are not retried. Application retry control is isolated against
the pinned library's internal retries, preserving its per-request session cleanup.
Use `hoyo`, `hoyo status`, or the game aliases. Enabled games fetch concurrently
through one gateway for the selected account, and results retain configured order.
One failed game keeps its error
section beside successful games. Status exits `0` if any game succeeds, `1` if
all fail, `2` for configuration/usage failures, and `3` for missing or invalid
credentials before requests begin. Status reads never claim rewards or enable
HoYoLAB notes settings automatically.

Compact views summarize assignments and compound systems; focused views expand
assignments and ZZZ management states. `--detailed` adds supported timestamps,
weekly resets, Member Card expiry, and Temple management details. Missing or
unknown optional systems show `Unavailable`; explicitly locked systems are
informational. Temple level zero is interpreted as `Locked`. HSR's reserve cap
is 2,400, from [HoYoWiki](https://wiki.hoyolab.com/pc/hsr/entry/179?lang=en-us),
because the pinned notes response supplies only the amount and full-state flag.

Validated, atomic per-game cache snapshots live in the OS cache directory
resolved by `platformdirs`. Every status invocation requests fresh data first.
Each successful game updates its own snapshot immediately. If its request fails,
that game can display its cached snapshot when it is no more than three hours old,
including exactly three hours at the moment fallback is evaluated after the
failed request. Future-dated, corrupt, or incompatible snapshots
are ignored. Cached resource amounts remain exactly as reported by the API.
Each fallback section shows the request error and `STALE · fetched <age> ago`;
`Updated HH:MM` appears only for an entirely fresh result. Status exits `0` when
any requested game has usable fresh or eligible cached data, and `1` otherwise.
Cache write failures print a warning while preserving fresh status.

Cache files use `<cache-root>/<account-digest>/<game>.json`. A SHA-256 digest of
the exact account name keeps paths portable and distinguishes case-sensitive
account names on Windows. The version 1 JSON envelope includes the original
account name, game, selected character UID, fetch timestamp, and normalized status;
it contains no cookies or upstream objects. Fallback requires the cached UID to
match the currently selected UID. Older snapshots without a verifiable UID are
ineligible and are replaced by the next successful fresh fetch. Fresh snapshots
record the time their request completes. Failed requests leave their previous
cache bytes untouched.

Explicit daily check-in uses `hoyo check-in [game]`. Each game
reads today's check-in state first and skips the claim if already checked in.
Unclaimed games make one claim attempt; ambiguous claim failures are never
retried automatically. Outcomes retain configured order and failures do not
hide other games' results. Check-in exits `0` only when every requested game
is claimed or already checked in, `1` for any action failure, and `2`/`3` for
preflight configuration/credential failures. No additional confirmation is
shown, and check-in does not fetch status or access its cache.

The pinned library's HoYoLAB daily rewards are scoped to the authenticated
account and game. The selected character UID is retained in its per-game
selection, but the overseas daily endpoint has no character UID parameter.
The claim endpoint does not return reward details, so the gateway reports
`Claimed` without extra reward reads. This avoids reporting a confirmed claim
as failed because a later reward lookup failed. A known already-claimed
response also counts as success if another caller claimed after the state read.
If a claim times out or loses its connection, the result warns that it may
have completed; check today's state before trying again.

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

### Optional live status verification

The default test run excludes `tests/live/` and blocks network connections.
After `hoyo init`, explicitly opt into real status reads with:

```powershell
uv run pytest --live-read-only -m live_read_only tests/live -q
```

This reads fresh status for every enabled game on the default account using its
configured credential backend. To select another exact account, set
`$env:HOYO_LIVE_ACCOUNT = "alt"` for the test, then remove it with
`Remove-Item Env:HOYO_LIVE_ACCOUNT`. No cookies are passed on the command line.
Missing configuration or credentials skip the live test; API failures fail it
with a classified message. Live verification never claims rewards, authorizes
notes automatically, or reads/writes status cache. Selecting the marker alone
does not enable network access; the flag is required.

## Command grammar

Help, version, account setup, status, daily check-in, config inspection, and
config editing are functional.

| Command | Availability and behavior |
| --- | --- |
| `hoyo` or `hoyo status` | Available: concurrent overview of all enabled games |
| `hoyo status <game>` | Available: focused status for `genshin`, `hsr`, or `zzz` |
| `hoyo genshin`, `hoyo hsr`, `hoyo zzz` | Registered aliases for `hoyo status <game>` |
| `hoyo check-in [game]` | Available: explicit daily reward claim for enabled games |
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
