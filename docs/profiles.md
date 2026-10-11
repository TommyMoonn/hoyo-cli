# Profiles and HoYoLAB logins

A **login** is a saved HoYoLAB session. A **profile** is the set of game characters you want to view with that session.

You can have two profiles using the same login. For example, `main` could track your Asia characters while `america` tracks characters on the America server.

Each profile can select one character per game.

## See your profiles

```bash
hoyo profile
```

This lists profile names, which login they use, their game selections, and which profile is the default.

## Add a profile

```bash
hoyo profile add america
```

The command asks whether you want to reuse a saved HoYoLAB login or add a new one:

- **Reuse a login** if the characters are linked to the same HoYoLAB account. The app find the characters you can choose.
- **Add a new login** for a different HoYoLAB account. You will enter its cookie values (or try browser import).

You can omit the name (`hoyo profile add`) and enter it when prompted.

## Use another profile

To use `america` for just one command:

```bash
hoyo --profile america
hoyo genshin --profile america
hoyo check-in --profile america
```

Your default remains unchanged. To make `america` the default profile for future commands:

```bash
hoyo profile use america
hoyo
```

`profile use` changes the saved default. Profile names are case sensitive, so it must match what you set for it.

## Edit a profile

```bash
hoyo profile edit america
```

You'll be asked what you want to change:

| Option | What it changes |
| --- | --- |
| **Games and characters** | Keep or disable a game, or select another linked character using the saved login. |
| **Change linked login** | Reuse another saved HoYoLAB login or add a new one, then choose that login's characters. |
| **Update HoYoLAB session** | Re-enter credentials for the *same* HoYoLAB identity, then review character selections. |
| **Credential storage** | Change between keyring and plaintext storage for the linked login. |

## When profiles share a login

Updating a shared login's session or storage affects every profile linked to it. The CLI shows the affected profiles and asks before making that kind of change.

Daily check-in is tied to the HoYoLAB login and game, not to your profile name. If two profiles share one login, running `hoyo check-in` on one doesn't give the other a second daily reward for that game.

There is no `profile delete` or `profile rename` command in the current CLI. See [Configuration](configuration.md) for the saved file structure if you need to inspect it manually.
