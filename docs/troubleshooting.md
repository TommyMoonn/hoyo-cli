# Troubleshooting

> [!IMPORTANT]
> Everything below this written by an LLM.

These are the usual places to check when setup or a daily command doesn't work. Error messages from HoYo CLI may also suggest a specific next command.

## `hoyo` isn't recognized

Check that installation succeeded:

```bash
uv tool list
```

If `hoyo-cli` is installed but `hoyo` isn't found, open a new terminal and check that uv's tool executable directory is on your `PATH`. See the [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/) for shell setup.

## You can't find the cookie values

The manual setup guide uses **Chrome or Edge**. Sign in to [HoYoLAB](https://www.hoyolab.com/), open Developer Tools (`F12`), choose **Application → Storage → Cookies → `https://www.hoyolab.com`**, and find `ltuid_v2` and `ltoken_v2` in the **Name** column. Copy each **Value** into the matching hidden prompt.

If those names don't appear, check that you're signed in and looking at the HoYoLAB cookie store. Don't paste arbitrary cookies or your password into the prompts. See the full [manual setup walkthrough](setup.md#enter-cookie-values-manually).

## Browser import fails

Browser import is best effort. It can fail because the browser is open, access to its files is restricted, or its cookies are protected by encryption. Return to setup and choose the manual `ltuid_v2` / `ltoken_v2` option. You don't need browser import to use the CLI.

## Your session is missing, expired, or rejected

For an existing profile, run:

```bash
hoyo profile edit main
```

Choose **Update HoYoLAB session** and enter new values from the *same* HoYoLAB identity. If you are connecting a different HoYoLAB account, choose **Change linked login** instead. If several profiles share a login, the CLI asks before replacing that shared session.

If you haven't set up any profile yet, run `hoyo init`.

## A game or character is missing

The CLI offers characters it can discover under the selected HoYoLAB login. Check that you've signed in to the right HoYoLAB account and that the game character is linked to it. To change an existing profile's selection:

```bash
hoyo profile edit main
```

Choose **Games and characters**, then **Choose linked character** for the game. A profile can select one character per game. For another server, add another profile if needed.

## Status shows old data or an error

Status tries to fetch fresh data every time. If a game request fails, it may fall back to a stored snapshot for that character when the snapshot is no more than three hours old. Cached data is marked as stale, and the CLI doesn't estimate how resources have changed since it was saved.

Try the command again later. If other games load normally, their results are still usable. `hoyo` never claims daily rewards.

## Check-in says `Unconfirmed`

A request may have reached HoYoLAB even if the reply was lost. Before running check-in again, inspect today's check-in state on HoYoLAB. The CLI makes one claim attempt and, when the result is uncertain, performs a read-only verification rather than automatically sending the claim again.

## The configuration file won't load

Open `~/.hoyo/config.toml` directly in a text editor, correct the invalid TOML or settings, and run:

```bash
hoyo config
```

`hoyo config --edit` first loads the configuration, so it may not work when that file is already invalid. For the supported settings and schema, see [Configuration](configuration.md).
