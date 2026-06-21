# AutoSE GitHub Pages

This branch (`gh-pages`) is the published site for AutoSE, served at
**https://autose-labs.github.io/autose/**.

It is generated/maintained separately from the application code on `main`.

## Contents

| File | Purpose |
|------|---------|
| `index.html` / `styles.css` / `main.js` | The marketing + install page (Technical Blueprint design). |
| `install.sh` | One-line installer for macOS / Linux. |
| `install.ps1` | One-line installer for Windows (PowerShell). |
| `.nojekyll` | Tells GitHub Pages to serve files as-is (no Jekyll build). |

## Install command

```sh
# macOS / Linux
curl -fsSL https://autose-labs.github.io/autose/install.sh | sh
```

```powershell
# Windows (PowerShell)
irm https://autose-labs.github.io/autose/install.ps1 | iex
```

The installer clones AutoSE into `~/.autose-cli`, runs `npm install` + `npm run build`,
and links an `autose` launcher onto your PATH. It does not touch your global npm prefix.

## Enabling Pages

Repo **Settings → Pages → Build and deployment → Source: Deploy from a branch**,
then select `gh-pages` / `(root)`.
