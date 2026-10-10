# Configuration

Use `hoyo init` for account setup. For existing settings:

```powershell
hoyo config
hoyo config --edit
```

Inspection shows the config path, default account, game order, color policy,
credential backend names, and enabled games with their selected UIDs. It shows
all configured accounts and never reads or prints stored credentials.

## File locations

| Data | Location |
| --- | --- |
| Configuration | `~/.hoyo/config.toml` |
| Optional plaintext credentials | `~/.hoyo/credentials.toml` |
| Preferred credentials | OS credential store, service `hoyo-cli`, keyed by account name |
| Status cache | Your OS cache directory for `hoyo` |

`~` means your home directory. On Windows, configuration is typically under
`C:\Users\<username>\.hoyo\`, and cache is normally at
`%LOCALAPPDATA%\hoyo\Cache`. Status cache contains snapshots, not cookies.

## Settings

| Field | Values and purpose |
| --- | --- |
| `version` | Configuration schema version; use `1` |
| `default_account` | Exact name of an existing account |
| `game_order` | Ordered game identifiers: `genshin`, `hsr`, `zzz`; no duplicates |
| `display.color` | `auto`, `always`, or `never` |
| `accounts.<name>.credential_backend` | `keyring` or `plaintext` |
| `accounts.<name>.games.<game>.enabled` | Include the game in status and check-in |
| `accounts.<name>.games.<game>.uid` | Selected character's positive integer UID |

Enabled games omitted from `game_order` are appended in canonical order.
`--no-color` and the `NO_COLOR` environment variable override color settings;
redirected output is unstyled.

This example contains a placeholder UID. Run setup to select your actual linked
character and store credentials before using it:

```toml
version = 1
default_account = "main"
game_order = ["genshin", "hsr", "zzz"]

[display]
color = "auto"

[accounts.main]
credential_backend = "keyring"

[accounts.main.games.genshin]
enabled = true
uid = 111111111
```

Keep cookies out of `config.toml`. Changing `credential_backend` alone does not
move credentials; use [account setup](setup.md#credential-storage) to store a session.

## Choose an editor

`hoyo config --edit` uses `VISUAL`, falling back to `EDITOR`.
For example, in PowerShell:

```powershell
$env:EDITOR = "notepad.exe"
hoyo config --edit
```

Editor arguments are supported. Quote executable paths containing spaces inside
the setting. If neither variable is set or the editor cannot launch, the command
reports the config path and an error.

## Manual edits and recovery

Configuration is validated before commands run. If TOML or a setting is invalid,
open the reported path directly in your editor and repair it; `--edit` also
requires valid configuration.

Setup preserves comments, other accounts, and unrelated fields. It refuses to
write through a symbolic-link config path; edit the managed target directly.
Rerun `hoyo init` to discover newly linked games, since normal status commands
do not add them automatically.
