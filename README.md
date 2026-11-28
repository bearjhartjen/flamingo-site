# Flamingo & Cactus — website repo

This repo is the entire Flamingo & Cactus website. The live site is
https://flamingoandcactus.com. Everything on it is generated from the files
here — there is no database, no server code running the site, and no online
editor. If you can read this repo, you can understand the whole site.

New here? Read `HISTORY.md` next. It is the plain-English log of every
decision: what we changed and why.

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
  browser-tab title, search description, optional heading override,
  and flags (`hidden`, `badge`, `form`).
- `data/pages/<name>.html` — one file per page, containing plain HTML.
- `assets/logo.png` — the masthead picture, published once as `site/logo.png`.
- `assets/favicon.png` — the transparent browser-tab icon.
- `assets/apple-touch-icon.png` — the orange link-preview / home-screen icon;
  `og:image` and `rel="apple-touch-icon"` use this, never the tab favicon.
- `assets/uploads/` — pictures used by pages, copied to `site/uploads/`.
- `site/` — the finished website: one `.html` file per page, `404.html`,
  `favicon.png`, `apple-touch-icon.png`, `logo.png`, `assets/site.css`,
  `assets/site.js` (shared by every page), `CNAME`, `robots.txt`, and
  `sitemap.xml`.

Every build runs the page bodies through an HTML sanitizer. Only a fixed
allowlist of tags survives (paragraphs, headings, lists, links, images,
tables, quotes, dividers, and a few inline styles like text size and
color). Anything else — scripts, forms, off-site images, event handlers —
is stripped before it can reach a published page. `python3 scripts/build.py
--check` rebuilds into a temporary directory and verifies the whole site,
and the `verify.yml` workflow runs the same check on every push.

Three light themes exist: Classic (near-white),
Parchment (a stronger cream), and Ink (dark). Midnight (a lighter
dark-purple) is the fourth. The theme is a single
setting in `data/site.json` and restyles every page at once. Every page also
carries a small Dark mode / Light mode
button in its footer, so visitors can switch to or from Midnight themselves;
the choice is remembered in that browser.

Two floating badges appear on the home page, in a black-and-white comic
style: the star links to a hidden Latest page (clicking it fires a comic
book ink burst - "Latest and greatest" with a small "Click me" - and both
badges sit level, evenly spaced either side of the centred logo); the white
FEEDBACK plate links to a hidden Feedback page. Other pages carry no badges,
so their content starts higher, especially on a phone. Hidden pages are
not in the menu but are still built, real pages. Which pages carry the
badges (and which page, if any, shows the contact form) is controlled by
flags in `data/site.json`, editable in the builder's Page settings. The
footer on every page links News, Feedback, Promotional and Settings,
so those pages stay reachable without a long menu. The
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

The Contact page's form is the one thing that is not static. With
JavaScript on, each note makes exactly one request, to FormInit
(`https://forminit.com`), which emails it to our inbox and answers with a
JSON verdict (the file upload asks for JSON too; if FormInit answers with
its thank-you redirect instead, that still counts as delivered). There is
no backup chain: a second service firing after an unconfirmed send is how
one submit became two emails. With JavaScript off, the plain form posts
once to FormSubmit (`https://formsubmit.co`) and lands on
`contact-ack.html`, a plain thank-you page. The form carries a honeypot,
stamps the sender's local Eastern time, and if a send is not confirmed it
brings the button back and explains the direct-email way out. The form
markup itself lives in `build.py`, not in the page files, so editing the
Contact page text can never break it.

The hidden Flank ledger lives here too, as the unlisted `site/flank/`
directory. It is built from `data/The Flank.md` by `scripts/build_flank.py`
on every build. Only the Financials page links to it, and each ledger page
links back. The ledger is edited on the Desktop (`~/Desktop/The Flank.md`
is the source of truth, and the only copy in plain text); the
`flamingo-ledger-watcher` service notices within seconds, syncs it into
`data/` with every private note encrypted, rebuilds, and stages both. `sh
scripts/sync-ledger.sh` does the same by hand. Install the watcher with
`sh scripts/install-watcher.sh`, remove it with `sh
scripts/uninstall-watcher.sh`.

The ledger now tracks more than cash and silver. Each transaction can carry
optional columns for `Gold holdings` (weight, like silver), `Cash`, `Coin`,
`Checking`, `Savings` and `Deep savings` (dollar amounts). The ledger page
shows a live gold spot flag (XAU/USD) next to the silver one, a `Gold
Holdings` headline figure, and a holdings allocation bar at the very bottom
that compares the total US dollar value of Gold, Silver, Cash, Coin,
Checking, Savings and Deep savings (the last at 3.65%). Hovering a
partition shows its dollar value, share and, for the metals, the weight in
troy ounces. The bar recomputes whenever either spot price refreshes.

### Financials, live repositories, and Giscus-ting

The Financials page is a deliberately small teaser: money in, money out, a
month picker, and one Chart.js bar chart of those two numbers by month. The
full transaction list, running balance and 10-day chart live in The Flank,
and a button in the glance sends visitors there. `scripts/page_extras.py`
parses `data/The Flank.md` at build time and embeds month totals only — no
note text ever reaches the chart data. Adding a transaction (or a new field
the ledger parser already understands) and letting the watcher rebuild is
all it takes; there is no second data file to update. The chart library is
vendored at `assets/chart.umd.min.js` so the page does not depend on a CDN.

The Products page has a live GitHub strip under each product, and the home
page ends with the website's own strip. `site.js` asks GitHub's public API
for stars, open issues, the latest commit and the newest release, caches the
answer in `localStorage` for 30 minutes, and falls back to the plain repo
link if the API is unavailable. No token is used or needed.

Giscus-ting is a normal nav tab with the GitHub Discussions forum embedded
through giscus. Its public settings (repository id, category id, discussion
number) live in the `giscus` block in `data/site.json`; the forum's
welcome thread is discussion #3 in `bearjhartjen/flamingo-site`. The embed
only loads on that page. If the giscus app is not installed on the
repository yet, the page shows a plain link to the GitHub discussion
instead of a broken box.

### The private notes are encrypted, not just hidden

Every transaction in the ledger carries a `- **Private:** yes/no` line.
On sync, `scripts/encrypt_flank.py` replaces each "yes" note with an
encrypted `[[FCEnc ...]]` token (AES-256-CBC, key derived from a
passphrase with PBKDF2 — see `scripts/flank_crypto.py`), so neither
`data/` nor the published page ever holds the private note text. The
published page decrypts them in the browser when the owner types the
passphrase into the lock box; the passphrase lives only in a local
keyfile outside this repository
(`~/.config/flamingo-site/flank-notes.key`) and is never committed.
Building the site never needs the passphrase — only the sync does.
`python3 scripts/encrypt_flank.py --check` roundtrip-verifies the repo
copy against the Desktop original, and `--decrypt` prints the private
notes locally.

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
- `data/The Flank.md` — the ledger, synced (with private notes
  encrypted) from `~/Desktop/The Flank.md`
- `assets/logo.png` — masthead picture (embedded into each page)
- `assets/chart.umd.min.js` — vendored Chart.js 4.4.7 (MIT) for the
  Financials dashboard
- `assets/uploads/` — pictures used by pages (copied to `site/uploads/`)
- `scripts/build.py` — builds `site/` from `data/` (stdlib only, owns the
  sanitizer and the `--check` verifier)
- `scripts/page_extras.py` — the generated Financials dashboard, GitHub
  strips and Giscus-ting block
- `scripts/build_flank.py` — builds `site/flank/` from the ledger (stdlib)
- `scripts/flank_crypto.py`, `scripts/encrypt_flank.py` — ledger note
  encryption (sync / check / decrypt)
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

Nothing here phones home by itself. Three visitor-facing pieces do use the
network, all of them on purpose and all of them easy to see: the contact
form posting to FormInit (FormSubmit with JavaScript off) when a visitor
submits it, the live GitHub strips asking `api.github.com` for public repo
stats, and the Giscus-ting forum loading `giscus.app` / GitHub Discussions
on that page only. The loopback builder is the only local network listener.
