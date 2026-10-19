# AutoSE GitHub Pages

This branch (`gh-pages`) is the published site for AutoSE, served at
**https://autose.dev/**.

It is generated/maintained separately from the application code on `main`.

## Contents

| File | Purpose |
|------|---------|
| `index.html` / `styles.css` / `main.js` | The home page: what AutoSE does, how it works, editions and install. |
| `research.html` | The research behind AutoSE. |
| `theme.css` | Colour and type tokens, the same as the dashboard's: light drafting paper and Catppuccin Mocha dark. |
| `demo.js` / `demo.css` | The "Try it" demo: a scripted session that runs in the page. |
| `favicon.svg` / `favicon.ico` / `apple-touch-icon.png` / `og-image.png` | The AutoSE pen-nib mark and the social preview image. |
| `install.sh` | One-line installer for macOS / Linux. |
| `install.ps1` | One-line installer for Windows (PowerShell). |
| `.nojekyll` | Tells GitHub Pages to serve files as-is (no Jekyll build). |

## Install command

```sh
# macOS / Linux
curl -fsSL https://autose.dev/install.sh | sh
```

```powershell
# Windows (PowerShell)
irm https://autose.dev/install.ps1 | iex
```

The installer needs `git`. It clones AutoSE into `~/.autose-cli` (or updates it if it is
already there), installs [uv](https://docs.astral.sh/uv/) if it is missing, and runs `uv sync`,
which fetches Python and the dependencies into the checkout. It then writes an `autose`
launcher (and an `autose-dashboard` launcher that asks for a project folder and opens the
dashboard) into `~/.local/bin` (`%LOCALAPPDATA%\AutoSE\bin` on Windows), adds an AutoSE shortcut to the desktop (and the applications
menu on Linux, the Start menu on Windows), and opens the dashboard. Nothing is installed
system-wide. Run it again at any time to update.

Environment variables: `AUTOSE_HOME` (where to clone, default `~/.autose-cli`),
`AUTOSE_BIN_DIR` (where the launchers go), `AUTOSE_REF` (a git ref to
check out), `AUTOSE_REPO` (the repository to clone), `AUTOSE_NO_SHORTCUT=1` and
`AUTOSE_NO_LAUNCH=1`.

The desktop shortcut takes its icon from this site: `favicon.svg` on Linux, `favicon.ico` on
Windows.

## Enabling Pages

Repo **Settings → Pages → Build and deployment → Source: Deploy from a branch**,
then select `gh-pages` / `(root)`.
