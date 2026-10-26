# Flamingo & Cactus — website repo

This repo is the entire Flamingo & Cactus website. The live site is
https://flamingoandcactus.com. Everything on it is generated from the files
here — there is no database, no server code running the site, and no online
editor. If you can read this repo, you can understand the whole site.

## How a change goes live

1. **Edit** — change words with the local builder (`edit.py`), or edit the
   files in `data/` by hand.
2. **Build** — `scripts/build.py` turns `data/` into the finished pages in
   `site/`. The builder runs this for you on every save.
3. **Commit** — commit with GitKraken (or git). A pre-commit hook rebuilds
   `site/` and stages it, so the built pages always travel with the source.
4. **Push** — pushing `main` triggers the `gh-pages.yml` workflow, which
   rebuilds `site/` once more and publishes it to the `gh-pages` branch.
   GitHub Pages serves that branch at our custom domain within a minute or
   two. Nothing goes live until you push.

## How the site is built

`scripts/build.py` (standard library Python only) reads `data/` and writes
`site/` from scratch on every run, so the output is fully reproducible:

- `data/site.json` — the site settings (title, tagline, search description,
  footer, theme) plus the page list: each page's file name, menu name,
  and flags (`hidden`, `badge`, `form`).
- `data/pages/<name>.html` — one file per page, containing plain HTML.
- `assets/logo.png` — the masthead picture, embedded into every page.
- `assets/uploads/` — pictures used by pages, copied to `site/uploads/`.
- `site/` — the finished website: one `.html` file per page, `404.html`,
  `favicon.png`, `CNAME`, `robots.txt`, and `sitemap.xml`.

Every build runs the page bodies through an HTML sanitizer. Only a fixed
allowlist of tags survives (paragraphs, headings, lists, links, images,
tables, quotes, dividers, and a few inline styles like text size and
color). Anything else — scripts, forms, off-site images, event handlers —
is stripped before it can reach a published page. `python3 scripts/build.py
--check` rebuilds into a temporary directory and verifies the whole site,
and the `verify.yml` workflow runs the same check on every push.

Three light themes exist: Classic (white with a whisper of tan),
Parchment (a stronger cream), and Ink (dark). Midnight (a lighter
dark-purple) is the fourth. The theme is a single
setting in `data/site.json` and restyles every page at once. Every page also
carries a small Dark mode / Light mode
button in its footer, so visitors can switch to or from Midnight themselves;
the choice is remembered in that browser.

Two floating badges appear on every page, in a black-and-white comic
style: the star links to a hidden Latest page (clicking it fires a comic
book ink burst - "Latest and greatest" with a small "Click me" - and both
badges sit level, evenly spaced either side of the centred logo); the white
FEEDBACK plate links to a hidden Feedback page. Hidden pages are
not in the menu but are still built, real pages. Which pages carry the
badges (and which page, if any, shows the contact form) is controlled by
flags in `data/site.json`, editable in the builder's Page settings. The
clicking-the-logo-returns-home behaviour is built in. Products carry a
plain-English blurb on the page and link a hidden "deep explanations"
page (slug `products-deep`) that keeps the full write-up. The Events page
weaves an `[event calendar]` marker into a month/year calendar derived from
a hidden source block on the same page (`.evsrc`, one line per event, kept
out of the visible page by CSS). The calendar is the face; it can be
searched by event name or date, and it defaults to the month of the next
upcoming event. To add an event: open the Events page in the builder's
source view and add a paragraph inside `.evsrc` containing a month and day
("November 14th" works; "Sat, Mar 7 2027" too) plus the time and place —
the calendar picks it up on the next build.

Dark mode is Midnight ("almost-black dark purple" that is kept readable) or
Ink, and the choice is carried over to the Flank ledger: both read the
same `fc-theme` browser key, so switching on any page switches both.

The Contact page's form is the one thing that is not static. It posts
directly to FormSubmit (`https://formsubmit.co`), which emails each
submission to our inbox - captcha-free, and it returns to the contact page
with a success flag. The form carries a honeypot, stamps the sender's
local Eastern time, and if a submission seems stuck it brings the button
back and explains the direct-email way out. The form markup itself lives
in `build.py`, not in the page files, so editing the Contact page text
can never break it.

The hidden Flank ledger lives here too, as the unlisted `site/flank/`
directory. It is built from `data/The Flank.md` by `scripts/build_flank.py`
on every build. Only the Financials page links to it, and each ledger page
links back. The ledger is edited on the Desktop (`~/Desktop/The Flank.md`
is the source of truth); the `flamingo-ledger-watcher` service notices
within seconds, copies it into `data/`, rebuilds, and stages both. `sh
scripts/sync-ledger.sh` does the same by hand. Install the watcher with
`sh scripts/install-watcher.sh`, remove it with `sh
scripts/uninstall-watcher.sh`.

## How editing works

You are the only one who enters text. There is no login and no online
editor. Open the `Flamingo & Cactus site` entry (Desktop or app menu). If
the builder is already running it just opens your browser to it; if not, it
starts a local-only server on `http://127.0.0.1:8767/` and nothing else can
reach it.

- **Pages** (left sidebar) — click to open, ↑↓ to reorder. The gear opens
  Page settings: menu name, browser tab title, search description, hidden
  status, star/feedback badge, and contact form. Add a blank page or copy
  the current page. The home page cannot be deleted.
- **Site** — title, tagline, search description, footer, theme, logo, star
  transition, and Advanced CSS.
- **Edit** — press Edit, then click page text and type. The toolbar is
  grouped into Edit, Text, Layout, Color, Links, Insert, Image, and Style:
  headings, text size/font/color, links and buttons, pictures, galleries,
  sections, tables, the contact-form position marker, and an HTML source
  mode for advanced markup (unsupported tags are cleaned before preview).
- **Blocks** — hover a page block for move, duplicate, and delete controls
  (Alt+↑/↓ also moves a block). Select an image to set its size, alignment,
  alt text, or replace it. Reuse uploaded images from the media library.
- **Save** (or Ctrl+S) — saves the settings and the open page, rebuilds
  everything, and stages the files with `git add`. Unsaved edits are kept
  as a draft in your browser, and the editor asks before switching pages or
  closing the tab. Nothing is published until you commit and push.
- **Preview** — shows the exact built page. **View live site** opens the
  published copy. **Help** in the Pages heading explains the shortcuts.
  **Quit** stops the builder from the browser, or Ctrl+C in the terminal.

You can also skip the builder: edit `data/site.json` or a file in
`data/pages/`, run `python3 scripts/build.py`, and commit. The pre-commit
hook (`sh scripts/install-hooks.sh` installs it; GitKraken honours it)
rebuilds and stages `site/` automatically.

## Publishing (GitHub Pages + custom domain)

Pushing `main` runs `.github/workflows/gh-pages.yml`, which rebuilds
`site/` and publishes it to the `gh-pages` branch with plain git (no
third-party actions):

1. Settings → Pages → Source: **Deploy from a branch** → `gh-pages` / (root).
2. Settings → Pages → Custom domain: `flamingoandcactus.com`, then Enforce
   HTTPS once the certificate is issued. (`site/CNAME` is written by every
   build, so the domain survives rebuilds.)
3. At the registrar (GoDaddy → Manage DNS): `A @` → `185.199.108.153`,
   `185.199.109.153`, `185.199.110.153`, `185.199.111.153`; `CNAME www` →
   `bearjhartjen.github.io`.
4. The site goes live at `https://flamingoandcactus.com` within a minute or
   two of each push to `main`.

## Layout

- `edit.py` — the local builder (loopback only, port 8767)
- `launch.sh` — the Desktop/app-menu launcher: reuses the running builder
  or starts it
- `data/site.json` — settings + the page/menu list
- `data/pages/<slug>.html` — one page each; plain allowlisted HTML,
  cleaned on every save
- `data/The Flank.md` — the ledger, synced from `~/Desktop/The Flank.md`
- `assets/logo.png` — masthead picture (embedded into each page)
- `assets/uploads/` — pictures used by pages (copied to `site/uploads/`)
- `scripts/build.py` — builds `site/` from `data/` (stdlib only, owns the
  sanitizer and the `--check` verifier)
- `scripts/build_flank.py` — builds `site/flank/` from the ledger (stdlib)
- `scripts/sync-ledger.sh`, `scripts/watch-ledger.py` — Desktop-ledger flow
- `.githooks/pre-commit` — rebuilds + stages `site/` on every commit
  (install with `scripts/install-hooks.sh`)
- `.github/workflows/gh-pages.yml` — publishes `site/` to `gh-pages` on
  push to `main`
- `.github/workflows/verify.yml` — recompiles and re-verifies the build
  on every push and pull request
- `site/` — the built pages: `<slug>.html`, `404.html`, `favicon.png`,
  `CNAME`, `robots.txt`, `sitemap.xml`, plus the unlisted `flank/`
  directory

Nothing here phones home. The only network use is the loopback builder
itself, plus the contact form posting to FormSubmit when a visitor (not
you) submits it.
