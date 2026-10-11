# Configuration

Most changes can be made through the CLI. Start with:

```bash
hoyo config
```

This shows the saved default profile, game order, editor, and each profile's game selections.

| To change | Use |
| --- | --- |
| Default profile | `hoyo profile use <name>` |
| Profile's characters or login | `hoyo profile edit <name>` |
| Game display order | `hoyo config order` |
| Preferred editor | `hoyo config editor` |
| Other settings in the TOML file | `hoyo config --edit` |

You can run `hoyo config --help` to see its options.

## File locations

| Data | Location |
| --- | --- |
| Configuration | `~/.hoyo/config.toml` |
| Plaintext credentials, if selected | `~/.hoyo/credentials.toml` |
| Keyring credentials | OS credential store, service `hoyo-cli`, using the login's credential key |
| Status cache | Platform-specific user cache folder for `hoyo` |

On Windows the cache is typically under `%LOCALAPPDATA%\hoyo\Cache`. The CLI resolves its cache path for your OS. `~` means your home directory.

Uninstalling HoYo CLI does not automatically delete these saved files or OS keyring entries. If you want to remove the app entirely, review those locations and remove any remaining credentials through your OS credential manager or file manager.

## Edit the config file

```bash
hoyo config --edit
```

The CLI uses your saved editor preference, then the `VISUAL` environment variable, then `EDITOR`. Set or clear the saved preference with:

```bash
hoyo config editor
```

The editor picker lists editors it can find and allows a custom executable with arguments.

If the config is already invalid, `hoyo config --edit` might not open it. Open `~/.hoyo/config.toml` directly in a text editor, correct the file, then run `hoyo config`.

## What the config contains

New installations use schema version 2. A shortened example:

```toml
version = 2
default_profile = "main"
game_order = ["genshin", "hsr", "zzz"]

[display]
color = "auto"

[logins.main]
credential_backend = "keyring"
credential_key = "main"

[profiles.main]
login = "main"

[profiles.main.games.genshin]
enabled = true
uid = 800000000

[profiles.america]
login = "main"

[profiles.america.games.genshin]
enabled = true
uid = 600000000
```

These are example UIDs, not real account data. Both profiles use the saved login `main` but track different characters. The login's secret is held by the selected credential store, outside `config.toml`.

| Setting | Meaning |
| --- | --- |
| `version` | Config schema version; new files use `2`. |
| `default_profile` | Profile used when you omit `--profile`. |
| `game_order` | Order of `genshin`, `hsr`, and `zzz` in the overview. |
| `display.color` | `auto`, `always`, or `never`. |
| `logins.<name>.credential_backend` | `keyring` or `plaintext`. |
| `logins.<name>.credential_key` | Key under which that login's session is stored. |
| `profiles.<name>.login` | Which saved HoYoLAB login the profile uses. |
| `profiles.<name>.games.<game>.enabled` | Whether the game appears in status and check-in. |
| `profiles.<name>.games.<game>.uid` | The selected character's UID for that game. |
| `editor.command` | Optional editor executable and arguments, stored as a list. |

The CLI also reads older schema-v1 configs for compatibility. Don't mix v1 `accounts` fields with v2 `logins` and `profiles` fields.

If you prefer to specify an editor in TOML, it looks like this:

```toml
[editor]
command = ["code", "--wait"]
```

The editor command must exist on your machine. Using `hoyo config editor` is less error-prone than editing this field manually.

**Do not put cookies in `config.toml`.** Changing `credential_backend` in that file does not move credentials between stores. For a storage change, use `hoyo profile edit <name>` and choose **Credential storage**. Changes to a login's storage can affect other profiles linked to it.
