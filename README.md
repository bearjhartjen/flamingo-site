# Flamingo & Cactus — website builder (desktop only)

A GoDaddy-style builder that runs on this computer only: pages down the
left, a live canvas in the middle, blocks and themes. Plain old-style
look, desktop only (min-width 1120px).

The repo lives at `~/Documents/flamingo-site/` and publishes to
`https://flamingoandcactus.com`.
The launcher is the `Flamingo & Cactus site` entry — on the Desktop and in the app menu.

## How editing works

You are the only one who enters text. There is no login and no online editor.

1. Open `Flamingo & Cactus site` (Desktop or app menu). If the builder is
   already running it just opens your browser to it; if not, it starts it.
2. Left sidebar:
   - **Pages** — click to open, ↑↓ to reorder the menu, ✎ to rename,
     × to delete, **+ Page** to add one. The home page cannot be deleted.
   - **Site** — title, tagline, footer line, theme
     (Classic / Parchment / Ink), logo.
3. Middle: press **Edit**, then type on the page itself. Toolbar blocks:
   bold, italic, underline, Heading, Text, bullets, numbers, quote,
   alignment, Link, **Button** (asks whether it opens in a new tab),
   Picture, **Gallery**, Divider.
4. Pictures: Picture button, Gallery button (many at once), drag files
   onto the page, or paste from the clipboard. Click a picture, then
   Small / Medium / Full. Galleries lay out side by side on their own.
5. **Save** (or Ctrl+S) saves the settings and the open page, rebuilds
   every page (plus `site/flank/` below), and stages it all with `git add`.
   Commit in GitKraken when it looks right.
6. **Quit** stops the builder from the browser, or Ctrl+C in the terminal.

`view built page` shows the exact built file for the open page (no edit
chrome), so you can check before committing. `view live site` opens the
published GitHub Pages copy in a new tab. Each page is also served
locally at its live address (e.g. `/financials.html`), rebuilt on the spot.

## Publishing (GitHub Pages + custom domain)

Pushing `main` runs `.github/workflows/gh-pages.yml`, which rebuilds
`site/` and publishes it to the `gh-pages` branch:

1. Settings → Pages → Source: **Deploy from a branch** → `gh-pages` / (root).
2. Settings → Pages → Custom domain: `flamingoandcactus.com`, then Enforce
   HTTPS once the certificate is issued. (`site/CNAME` is written by every
   build, so the domain survives rebuilds.)
3. At the registrar (GoDaddy → Manage DNS): `A @` → `185.199.108.153`,
   `185.199.109.153`, `185.199.110.153`, `185.199.111.153`; `CNAME www` →
   `bearjhartjen.github.io`.
4. The site goes live at `https://flamingoandcactus.com` within a minute or
   two of each push to `main`.

`.githooks/pre-commit` rebuilds `site/` and stages it with every commit
(install with `scripts/install-hooks.sh`); `verify.yml` re-checks the build
in CI.

## The hidden Flank ledger

The old standalone Flank-ledger site now lives here as the unlisted
`site/flank/` directory (`flank/index.html` + `flank/about.html`), built
from `data/The Flank.md` by `scripts/build_flank.py` on every build. It is
in no menu; the **The Flank** button on the Financials page is the only
link to it, and each ledger page links back to the main site.

The editing flow is unchanged from the old repo: the ledger is edited on
the Desktop (`~/Desktop/The Flank.md`, the source of truth), and the
**flamingo-ledger-watcher** service notices within seconds, copies it into
`data/`, rebuilds `site/` (main pages + ledger), and stages both — commit
in GitKraken like any other edit. It only stages, never commits or pushes.
`sh scripts/sync-ledger.sh` does the same by hand; `--check` reports what
would change without writing. Install with `sh scripts/install-watcher.sh`,
remove with `sh scripts/uninstall-watcher.sh`.

## Layout

- `data/site.json` — settings + the page/menu list
- `data/pages/<slug>.html` — one page each; plain allowlisted HTML,
  cleaned on every save
- `data/The Flank.md` — the ledger, synced from `~/Desktop/The Flank.md`
- `assets/logo.png` — masthead picture (embedded into each page, like Flank)
- `assets/uploads/` — pictures used by pages (copied to `site/uploads/`)
- `scripts/build.py` — builds `site/` from `data/` (stdlib only, owns the cleaner)
- `scripts/build_flank.py` — builds `site/flank/` from the ledger (stdlib only)
- `scripts/sync-ledger.sh`, `scripts/watch-ledger.py` — Desktop-ledger flow
- `site/` — the built pages: `<slug>.html`, `404.html`, `favicon.png`,
  `CNAME`, plus the unlisted `flank/` directory
- `edit.py` — the local builder (loopback only, port 8767)

`python3 scripts/build.py --check` rebuilds to a temp dir and verifies,
including a check that scripts and off-site images never reach the pages.
Nothing here phones home. The only network use is the loopback server itself.
