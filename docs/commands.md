# Command reference

Use `genshin`, `hsr`, or `zzz` for the three supported games. Run `hoyo --help` for the commands, or `hoyo <command> --help` for a command's options/flags.

## Check game statuses

| Command | What it shows |
| --- | --- |
| `hoyo` | Overview of all enabled games on the default profile. |
| `hoyo genshin` | Focused Genshin Impact status. |
| `hoyo hsr` | Focused Honkai: Star Rail status. |
| `hoyo zzz` | Focused Zenless Zone Zero status. |

Some options/flags you can use with:

- `--detailed` shows additional supported status fields.
- `--no-color` disables terminal styling.
- `--profile <name>` uses that profile for this command only.

Examples:

```bash
hoyo --detailed
hoyo hsr --detailed
hoyo status zzz --no-color
hoyo genshin --profile america
```

## Options

| Option | Purpose |
| --- | --- |
| `--account <name>` | Run the command for this account/profile |
| `--detailed` | Show more supported fields in a status view |
| `--no-color` | Disable terminal styling |

```bash
hoyo genshin --detailed
hoyo --account alt
hoyo status zzz --account alt --no-color
```

## Daily check-in

```bash
hoyo check-in                     # All enabled games
hoyo check-in genshin             # One game
hoyo check-in --profile america   # Check in all enabled games for another profile
```

| Result | Meaning |
| --- | --- |
| `Claimed` | HoYoLAB confirmed the claim. |
| `Already checked in` | Today's reward was already claimed; no claim was sent. |
| `Claimed (verified)` | The claim result was unclear, but a later check confirmed it. |
| `Unconfirmed` | The claim may have worked, but a later check couldn't confirm it. |
| `Failed` | The state check or claim failed. |

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Status has at least one usable fresh or cached game result; check-in succeeded for every requested game |
| `1` | No usable status results, a failed or unconfirmed check-in, or another operational error |
| `2` | Invalid usage or configuration |
| `3` | Missing, invalid, or inaccessible credentials before game requests start |

## Profiles

| Command | Purpose |
| --- | --- |
| `hoyo init` | Set up the first profile. |
| `hoyo profile` | List saved profiles and the current default. |
| `hoyo profile add [name]` | Add a profile. The name is optional, setup asks if you omit it. |
| `hoyo profile edit <name>` | Edit a profile's characters, linked login, session, or storage. |
| `hoyo profile use <name>` | Set the default profile for future commands. |

See [Profiles](profiles.md) for when to reuse a login.

## Configuration

| Command | Purpose |
| --- | --- |
| `hoyo config` | Show non-secret configuration, including disabled and unconfigured games. |
| `hoyo config order` | Choose and save the global game display order. |
| `hoyo config editor` | Set or clear the preferred editor. |
| `hoyo config --edit` | Open `config.toml` with the configured editor. |
| `hoyo --version` | Show installed version. |

`--no-color` is available on normal status and check-in commands, and on supported setup/configuration commands.

In syntax examples, `<name>` and `<game>` mean required values. `[name]` and `[game]` are optional. Enter the actual value without brackets.

## Uninstall

```bash
uv tool uninstall hoyo-cli
```

Uninstalling the executable does not clear your saved configuration, credential backend, or cache. See [file locations](configuration.md#file-locations) if you want to clean up those separately.
