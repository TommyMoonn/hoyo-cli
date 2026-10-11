# About HoYo CLI

I got too lazy to open each game (or use HoYoLAB) just to check my daily, weekly stats. So I made a small terminal app that puts all the information I need in one place.

HoYo CLI currently supports **Genshin Impact**, **Honkai: Star Rail**, and **Zenless Zone Zero**. It uses your HoYoLAB login to read linked data from those games you play.

## Vibes

This app is written entirely by AI. I have not read a single line of code in this.

I'm not planning to add any more features than just for checking daily and weekly information. No current plans for maintaning this app, but any feedback you have are still greatly appreciated.

## Features

- See stamina, daily tasks, expeditions or assignments, and weekly activities.
- Has support for multiple accounts through profiles.
- Claim HoYoLAB daily check-in rewards.

Some fields depend on what HoYoLAB makes available for your account.

## Requirements

- Python 3.11 or newer.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) for installation.
- A HoYoLAB account linked to at least one supported game.

## Installation

Open terminal and clone this repository.

```bash
git clone https://github.com/TommyMoonn/hoyo-cli.git
cd hoyo-cli
uv tool install .
```

After installing, you can run `hoyo --help` to test it.

## Getting started

Set up your first profile, then from there you can check your games:

```bash
hoyo init
hoyo
```

Setup can import an existing browser session or you can provide the cookies yourself. See the [setup guide](docs/setup.md) for a detailed walk through of the process.

## Everyday commands

```bash
hoyo                        # Check all enabled games
hoyo genshin                # One game
hoyo hsr --detailed         # More supported details
hoyo check-in               # Daily rewards for enabled games
```

Treat your HoYoLAB cookies like a password. Never share credential files.

## Profiles

For another character or server, you can add a profile then choose when to use it:

```bash
hoyo profile add america
hoyo --profile america      # Effective for that command, does not change the default profile
hoyo profile use america    # Changes the default profile, all commands after this uses this profile.
```

For more detail see [profiles guide](docs/profiles.md).

## Documentation

- [First-time setup](docs/setup.md): authentication, character selection, and storage.
- [Profiles](docs/profiles.md): add, edit, switch, and share a saved HoYoLAB login.
- [Commands](docs/commands.md): command syntax and check-in results.
- [Configuration](docs/configuration.md): settings, file locations, and editor preferences.
- [Troubleshooting](docs/troubleshooting.md): common setup and status problems.
- [Development](docs/development.md): local checks and opt-in live tests.

## License

Hoyo CLI is licensed under the [MIT License](LICENSE).
