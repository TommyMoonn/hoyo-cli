# Command reference

Run `hoyo --help` or `hoyo <command> --help` for built-in help.
Game identifiers are `genshin`, `hsr`, and `zzz`.

## Commands

| Command | Purpose |
| --- | --- |
| `hoyo` or `hoyo status` | Show an overview of all enabled games |
| `hoyo status <game>` | Show one game's focused view |
| `hoyo genshin`, `hoyo hsr`, `hoyo zzz` | Shortcuts for the focused views |
| `hoyo check-in [game]` | Claim daily HoYoLAB rewards for enabled games or one game |
| `hoyo init` | Set up or reconfigure an account |
| `hoyo config` | Inspect configuration without reading stored credentials |
| `hoyo config --edit` | Open valid configuration in your selected editor |
| `hoyo --version` | Show the installed version |

## Options

| Option | Purpose |
| --- | --- |
| `--account NAME` | Select an exact, case-sensitive account name |
| `--detailed` | Show more supported fields in a status view |
| `--no-color` | Disable terminal styling |

`--account` and `--no-color` work before or after a command. `--detailed` works
at the root, on `status`, and on game shortcuts. A command-level account overrides
a root-level account; boolean options stay enabled if set in either position.

```powershell
hoyo genshin --detailed
hoyo --account alt
hoyo status zzz --account alt --no-color
```

Create `alt` through [account setup](setup.md) before selecting it. Explicitly
requesting a disabled or unconfigured game produces an error.

## Status and cached results

Every status command requests fresh data. A failed game may show a cached snapshot
for the same account and selected UID if it is no more than three hours old.
The section includes the request error and `STALE` with the snapshot's age.
Resource amounts remain as captured, without estimates of regeneration.

Other games can still show fresh results. Expired, corrupt, or incompatible cache
is ignored. A cache-save warning does not prevent fresh status from being shown.
`Locked` and `Unavailable` describe individual systems, not necessarily a failed
game request. Optional details appear only when supported data is available.

## Daily check-in

```powershell
hoyo check-in
```

This claims daily HoYoLAB rewards for every enabled game on the selected account.
To request only Zenless Zone Zero:

```powershell
hoyo check-in zzz
```

The command takes action immediately, with no additional confirmation. It checks
today's claim state first and reports `Claimed`, `Already checked in`, or an error
for each game. A failure does not hide the other results.

If a claim times out or loses its connection, it may have completed. Check today's
state on HoYoLAB before trying again. Claims are not automatically retried.
If HoYoLAB requires verification, complete check-in on its website.

Daily rewards apply to the authenticated HoYoLAB account and game rather than
the selected character UID.
Check-in does not fetch the status dashboard or use its cache.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Status has at least one usable fresh or cached game result; check-in succeeded for every requested game |
| `1` | No usable status results, a check-in failure, or another operational error |
| `2` | Invalid usage or configuration |
| `3` | Missing, invalid, or inaccessible credentials before game requests start |

Failures during individual game requests are included in the aggregate status or
check-in result, including authentication failures.

## Uninstall

```powershell
uv tool uninstall hoyo-cli
```

User configuration, stored credentials, and status cache are separate from the
installed tool. Remove them separately if you no longer need them; see
[configuration](configuration.md#file-locations).
