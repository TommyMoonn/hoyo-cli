# HoYo CLI

Check your daily and weekly HoYo game progress from the terminal, without opening
each game or navigating HoYoLAB.

HoYo CLI supports **Genshin Impact**, **Honkai: Star Rail**, and
**Zenless Zone Zero** using your HoYoLAB account.

## Features

- See stamina, daily tasks, expeditions or assignments, and weekly activities in
  one overview.
- Focus on one game or show more detail, including available recovery times and
  reset information.
- Switch between multiple named accounts and choose which games to display.
- Claim HoYoLAB daily check-in rewards with an explicit command.
- Get readable output that adapts to your terminal width, with optional color.
- Keep seeing successful games when another fails. Recent cached results can
  appear as a clearly marked fallback.

| Game | Examples of tracked status |
| --- | --- |
| Genshin Impact | Resin, Realm Currency, commissions, expeditions, Transformer |
| Honkai: Star Rail | Trailblaze Power, Daily Training, assignments, weekly activities |
| Zenless Zone Zero | Battery Charge, Engagement, Scratch Card, Video Store, Hollow Zero |

Some fields depend on what HoYoLAB makes available for your account.

## Requirements

- Python 3.11 or newer.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) for installation.
- A HoYoLAB account linked to at least one supported game.
- Internet access for account setup, fresh status, and check-in.

Runtime dependencies are installed automatically.

## Installation

Download or clone this repository, then open a terminal in the project directory:

```powershell
uv tool install .
hoyo --help
```

If uv reports that its tool directory is missing from `PATH`, follow its
instructions and open a new terminal.

## Getting started

Set up your account, then view your enabled games:

```powershell
hoyo init
hoyo
```

Setup can import an existing browser session or accept manually copied cookies.
It lets you select your game characters and credential storage. See the
[account setup guide](docs/setup.md) for instructions.

For a closer look at one game:

```powershell
hoyo genshin --detailed
```

Status requests fresh data each time. If a request fails, a matching cached
snapshot up to three hours old may be shown as `STALE`; resource amounts are
not estimated forward. Viewing status never claims rewards. Use `hoyo check-in`
when you want to claim them.

Treat HoYoLAB cookies like a password. Prefer the OS credential store; plaintext
storage is optional and requires your consent. Never share credential files.

## Documentation

- [Account setup](docs/setup.md): browser import, manual cookies, and multiple accounts.
- [Command reference](docs/commands.md): commands, options, check-in, and uninstalling.
- [Configuration](docs/configuration.md): settings, file locations, and editor setup.
- [Development](docs/development.md): local environment, checks, and optional live tests.
