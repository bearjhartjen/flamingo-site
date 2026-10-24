# Flamingo & Cactus — website builder (desktop only)

A GoDaddy-style builder that runs on this computer only: pages down the
left, a live canvas in the middle, blocks and themes. Plain old-style
look, desktop only (min-width 1120px).

The repo lives next to the Flank ledger at `~/Documents/flamingo-site/`.
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
   every page, and stages it all with `git add`. Commit in GitKraken
   when it looks right, exactly like the Flank.
6. **Quit** stops the builder from the browser, or Ctrl+C in the terminal.

`view built page` shows the exact built file for the open page (no edit
chrome), so you can check before committing. `view live site` opens the
published GitHub Pages copy in a new tab. Each page is also served
locally at its live address (e.g. `/financials.html`), rebuilt on the spot.

## Publishing (GitHub Pages, same as the Flank)

Pushing `main` runs `.github/workflows/gh-pages.yml`, which rebuilds
`site/` and publishes it to the `gh-pages` branch — no repo settings
needed beyond enabling Pages once:

1. Create the repo on GitHub and push `main` to it (GitKraken).
2. Settings → Pages → Source: **Deploy from a branch** → `gh-pages` / (root).
3. The site goes live at `https://<you>.github.io/<repo>/` within a minute
   or two of each push to `main`.

`.githooks/pre-commit` rebuilds `site/` and stages it with every commit
(install with `scripts/install-hooks.sh`, same as the Flank); `verify.yml`
re-checks the build in CI.

## Layout

- `data/site.json` — settings + the page/menu list
- `data/pages/<slug>.html` — one page each; plain allowlisted HTML,
  cleaned on every save
- `assets/logo.png` — masthead picture (embedded into each page, like Flank)
- `assets/uploads/` — pictures used by pages (copied to `site/uploads/`)
- `scripts/build.py` — builds `site/` from `data/` (stdlib only, owns the cleaner)
- `site/` — the built pages: `<slug>.html`, `404.html`, `favicon.png`
- `edit.py` — the local builder (loopback only, port 8767)

`python3 scripts/build.py --check` rebuilds to a temp dir and verifies,
including a check that scripts and off-site images never reach the pages.
Nothing here phones home. The only network use is the loopback server itself.
