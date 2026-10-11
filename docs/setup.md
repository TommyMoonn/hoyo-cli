# First-time setup

After you finish [installing HoYo CLI](../README.md#install) run this to start the setup process:

```bash
hoyo init
```

## First account

1. **Name your profile:** Like `main`, or anything you want. This is a local profile name, not related to your HoYoLAB username.
2. **Connect to HoYoLAB:** Enter your `ltuid_v2` and `ltoken_v2` cookie values, or try importing them from a browser. Manual cookie input is recommended.
3. **Choose characters:** Choose the character region you want to track for each game.
4. **Set game order:** Keep the default order or choose which game appears first in the overview.
5. **Choose credential storage.** Use the OS keyring if possible. Plaintext storage is not recommended, but is still an option.

When setup finishes, you can run and check if it works:

```bash
hoyo
```

## Enter cookie values manually

For Chrome or Edge:

1. Open [HoYoLAB](https://www.hoyolab.com/) and sign in.
2. Press `F12` to open Developer Tools, then select **Application**.
3. Under **Storage**, expand **Cookies** and select `https://www.hoyolab.com`.
4. Find the row named `ltuid_v2`. Copy the contents of its **Value** column into the `ltuid_v2` setup prompt.
5. Do the same for `ltoken_v2` in the second prompt.
6. After that is finished, HoYo CLI checks the session and looks for your linked characters.

> [!IMPORTANT]
> Treat these values like passwords. Do not share them or put them in screenshots or issue reports.
>
> Both setup prompts hide what you type or paste, so its still working even if no text apprears.

## Copy cookies manually

1. Open [HoYoLAB](https://www.hoyolab.com/).
2. Press F12 and select the Application tab.
3. Under Storage, expand Cookies and select `https://www.hoyolab.com`.
4. Find ltuid_v2 and copy its Value into the first setup prompt.
5. Find ltoken_v2 and copy its Value into the second setup prompt.
6. Press enter to continue with the setup.

> [!IMPORTANT]
> Treat the cookie string like a password, losing it can be dangerous so never share it with anyone.
> The setup prompt hides the input, so your paste is still working even if no text apprears.

## Import from a browser

Browser import may not work on every machine.

1. Sign in to HoYoLAB in your browser.
2. In `hoyo init`, choose **Import from browser**.
3. Choose a browser source and, where available, a browser profile.
4. Let HoYo CLI try to read and verify the session.

Browser import supports common Chromium browsers and Firefox. Browser encryption or an open browser can prevent access to the cookies. If import fails, return to manual entry.

## Where does your crendentials go?

| Choice | Where it is stored |
| --- | --- |
| `keyring` | Your operating system's credential store (recommended) |
| `plaintext` | `~/.hoyo/credentials.toml` |

Plaintext storage leaves authentication values in a local file. That is why I highly recommend using keyring, since its way more secure. Plaintext is still available as an option if for some reason keyring does not work, but be very careful not to share or leak it elsewhere.

Once connected, you don't have to enter cookies for every `hoyo` command. If the session later stops working, update it through `hoyo profile edit <name>`.

All of these settings can be changed directly in `configuration.toml`. See [configuration guide](configuration.md) for more details.

## Next steps

- [Check your games or claim daily rewards](commands.md).
- [Add another profile or change characters](profiles.md).
- [Fix an authentication or browser import problem](troubleshooting.md).
