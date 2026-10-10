# Account setup

Install HoYo CLI using the [README](../README.md#installation), then run:

```powershell
hoyo init
```

You need a HoYoLAB account linked to a supported game and internet access.
Setup uses your HoYoLAB session cookies to access that account.

## First account

1. Enter an account name, such as `main`.
2. Import a browser session or paste cookies manually.
3. Choose a linked character for each discovered game and decide which games to enable.
4. Choose the game display order.
5. Choose credential storage. Prefer `keyring`, which uses your OS credential store.
6. Confirm saving, then run `hoyo` to view status.

Enable at least one game. Setup saves only after your final confirmation.

## Import from a browser

Sign in to [HoYoLAB](https://www.hoyolab.com/) in your browser before running
setup. Agree to browser import, then select a session from the numbered list.

HoYo CLI has loaders for Chrome, Edge, Firefox, Chromium, Opera, and Brave.
Availability depends on whether the cookie library can access that browser on
your system. If import fails or no usable session is found, use manual input.

## Copy cookies manually

Treat the complete cookie string like a password. Paste it only into the hidden
setup prompt, never into a command, issue, screenshot, or shared file.

To obtain a full Cookie header:

1. Sign in to HoYoLAB in your browser.
2. Open Developer Tools and select the Network tab.
3. Reload the page and inspect a request to a HoYoLAB domain.
4. In its request headers, find a `Cookie` value containing the authentication
   fields listed below. Another HoYoLAB request may be needed if they are absent.
5. Copy the header value as one line, without the `Cookie:` label.
6. Decline browser import or choose manual input, then paste the value at
   `Full HoYoLAB Cookie header (hidden)`.

The input uses semicolon-separated `name=value` pairs. HoYo CLI requires
`ltuid` and `ltoken`, or `ltuid_v2` and `ltoken_v2`; matching account-ID aliases
are also accepted. It keeps recognized authentication fields and rejects missing,
duplicate, or conflicting values without printing them.

The [upstream authentication guide](https://seria.is-a.dev/genshin.py/authentication/#from-the-browser)
also explains where to locate HoYoLAB cookies in browser storage.

## Credential storage

| Choice | Storage |
| --- | --- |
| `keyring` | OS credential store, under service `hoyo-cli` and your exact account name |
| `plaintext` | `~/.hoyo/credentials.toml`, separate from normal configuration |

Plaintext storage requires explicit consent. On POSIX systems the file is written
with mode `0600`. On Windows it inherits directory permissions, so keep the
directory private. Do not include credentials in Git commits or shared archives.

## Add or update accounts

Run `hoyo init` again and choose an existing account or **Create new account**.
An account name selects one character per game; use separate named accounts for
different characters or regions of the same game.

After creating an account named `alt`, select it with:

```powershell
hoyo --account alt
```

Names are case-sensitive and must match exactly. With an existing configuration,
`hoyo init --account alt` selects an existing account; create new accounts through
the setup menu.

Updating an account requires a fresh imported or pasted session and consent to
replace credentials. Games not found in that session are disabled, with their
previous UIDs retained. Setup preserves other accounts and unrelated settings.

Changing credential storage does not migrate or remove old secrets. Remove unused
entries from the old backend yourself.

## Troubleshooting

- **Session rejected or expired:** sign in to HoYoLAB again, then rerun `hoyo init`.
- **No linked characters:** check that the chosen HoYoLAB session belongs to the
  account with your supported game characters.
- **Status access rejected:** check your account's HoYoLAB notes and privacy
  settings. HoYo CLI does not enable notes access automatically.
- **OS keyring unavailable:** use a working OS credential backend or explicitly
  accept plaintext storage during setup.
- **Malformed stored data:** repair the reported config or credential file, or
  the `hoyo-cli` keyring entry, before retrying setup.

See [configuration](configuration.md) for file locations and manual editing.
