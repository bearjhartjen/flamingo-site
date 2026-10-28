# How this site came to be

This file is the real log for this site. Our commit messages just say
"improvements" and that tells you nothing. This file tells you what we
changed and why. If you read this, you can understand how things go.

Our big rule is simple. All products and projects by Flamingo and Cactus
are open source. This helps us be honest. We publish everything we can
about everything we do. That keeps everyone on the same playing field.

## How a change goes live

1. Edit with the local builder (`edit.py`), or edit the files in `data/` by hand.
2. Build with `python3 scripts/build.py`. It turns `data/` into `site/`.
3. Commit with GitKraken. A pre-commit hook rebuilds `site/` and stages it.
4. Push `main`. That runs `gh-pages.yml`, which publishes to the
   `gh-pages` branch. The live site is https://flamingoandcactus.com.
   Nothing goes live until we push.

The full version of this is in `README.md`. This file is about the why.

## The builder is local only

There is no login and no online editor. You are the only one who enters
text. You open the `Flamingo & Cactus site` launcher. It starts a server
on `http://127.0.0.1:8767/` that only your own computer can reach.

We did it this way on purpose. No accounts to hack. No database to break.
If you can edit files, you can run the whole site.

## The site is static

`scripts/build.py` reads `data/` and writes `site/` from scratch every
time. There is no database and no server code running the live site.

- `data/site.json` holds the settings and the page list.
- `data/pages/<name>.html` holds one page each.
- `site/` is the finished site. Never edit it by hand. A build overwrites it.

Every build runs the page bodies through a cleaner. Only plain tags
survive. Scripts, forms, off-site images, and click handlers are stripped
before they can reach a published page. This keeps visitors safe.

`python3 scripts/build.py --check` rebuilds into a temp folder and checks
the whole site. The `verify.yml` workflow runs the same check on every push.

## Themes

We have four themes: Classic, Parchment, Ink, and Midnight. The theme is
one setting in `data/site.json`. It restyles every page at once.

Every page also has a small Dark mode / Light mode button in the footer.
Visitors can switch for themselves. The choice is saved in that browser
under the key `fc-theme`.

Decision: the Flank ledger does NOT follow the theme. It keeps its own
look. We tried carrying the theme over, then pulled it back out on
2026-10-28 because it was confusing. The settings page now says the choice
follows you across every page, and stops there.

## Star, badges, and hidden pages

Two badges float on every page in a black-and-white comic style. The star
links to a hidden Latest page. The white FEEDBACK plate links to a hidden
Feedback page. Hidden means not in the menu, but still a real built page.

Clicking the star plays a burst. The setting is `star_animation` in
`data/site.json`. Right now it is `portal`. We also have `comic`, `fade`,
and `none`. The default in the build script is `comic`, so a missing or
bad value falls back to comic.

What we learned: a builder tab holds its settings from when it was opened.
If you save from an old tab, it can silently flip `star_animation` back.
We fixed this in `edit.py`. The save now carries what the tab saw at load
(`siteBase`). If the tab never touched the dropdown, the disk value wins.
If the tab did touch it, the tab wins.

## The contact form

The Contact page form is the one thing that is not static. It posts to
FormSubmit, which emails each message to our inbox. The email address on
the page is plain text on purpose. We want people to reach us.

What we decided:

- No captcha. A honeypot field catches spam instead.
- The form stamps the sender's local Eastern time.
- The page shows a clear verdict. "It went through" with a time stamp,
  or an error panel with the service's own message and a plain-email way out.
- Notes without files go by background send. Notes with files use a normal
  submit with a 20-second watchdog and a `?sent=1` return.
- File sizes read in KB up to 1024 KB, then MB. The cap is 10,000,000 bytes.
  That is the FormSubmit limit, not ours.
- The button says "Send it our way."
- The wording lives in `data/form.json`, not in the page file, so editing
  the Contact page text can never break the form. There is a Form button in
  the builder to edit that wording.

What we learned: FormSubmit needs a Referer header. Local test pages get a
"needs activation" reply. Only the live domain is activated. That is normal.

## Events calendar

The Events page holds one marker: `[event calendar]`. The build swaps it
for a month calendar. The calendar is drawn from a hidden source block on
the same page (`.evsrc`, one line per event). The CSS hides that block, so
visitors only see the calendar face.

How it works now:

- The legend list under the grid is gone. Only day marks show.
- The mark is a symbol, default green dot. Right now it is bright green,
  size 48. You change shape, colour, and size from the Calendar button in
  the builder. It rides in `data/calendar.json`.
- Click a day with events and a panel opens under the grid with the date,
  time, and full text. Click again to close.
- To add an event, the builder has an Events button. Each event is one
  paragraph with a month and day, plus time and place. Links go inline like
  `[words](address)`. Only http, https, mailto, tel, or site paths are kept.

What we learned: type the marker as `<p>[event calendar]</p>`. A bare text
line without the `<p>` wrap survives as bare text and the build misses it.
Also, the page files must never hold `<p>` inside `<p>`. The browser closes
that for you and the line breaks.

## Stress City (removed) and Settings

Stress City was a hidden page holding one marker, `[stress express]`. The
build swapped it for a station scene, and the level lived in
`data/stress.json` (`level` 0 to 100). Six tiers moved the train from
parked to overloaded. It was removed on October 28, 2026: the page, its
data file, its generated CSS and its build code are all gone.

Settings is a visible page. It holds one marker: `[site settings]`. The
build swaps it for theme cards: Classic, Parchment, Ink, Midnight, plus
Site default. Site default clears the saved key and falls back to the
baked-in theme.

Developer sign-in for live edits is planned next, through a worker.

## Financials and the Flank ledger

The Financials page has one gold button: The Flank. It links to
`site/flank/`. That is the Flank ledger, built from `data/The Flank.md`
by `scripts/build_flank.py` on every build. Only Financials links to it.
Each ledger page links back.

The ledger lists every transaction with Flamingo and Cactus. We publish it
to be fully transparent. That is the defining principle we are built on.

Some note fields are blurred. That protects Bear's personal time with
family and friends. Those notes are encrypted, not just hidden:

- `~/Desktop/The Flank.md` is the source of truth, and the only plain copy.
- Each transaction carries a `- **Private:** yes/no` line.
- On sync, `scripts/encrypt_flank.py` replaces each yes-note with an
  encrypted token. Neither `data/` nor the published page ever holds the
  private text.
- The published page decrypts in your browser when you type the passphrase
  into the lock box. The passphrase lives only in a keyfile outside this
  repo and is never committed.
- `scripts/sync-ledger.sh` does the sync by hand. The
  `flamingo-ledger-watcher` service does it within seconds and stages both
  files. Install with `scripts/install-watcher.sh`.

The ledger also shows a small silver spot line at the top. It reads the
live price in your browser and never touches our build.

## Products page buttons

Small buttons (`a.btn`) keep only colours through the cleaner. Geometry
like dragged position is dropped on purpose, so a button placed on a wide
screen can not hang off a phone screen. On phones, dragged buttons drop to
plain centred blocks.

The PCBWay order button on Products is styled by the build CSS, keyed on
its share-project link. It is a sunset block: pink to orange to gold,
rounded, centred. No inline position needed.

What we learned: one dragged button sat at `left:286px;top:1379px` on
Products. On a 393px phone it hung off the screen and broke the layout.
The fix is CSS only, in the `@media (max-width:700px)` block. Dragged
buttons go static on phones. Colours stay.

## Publishing

Pushing `main` runs `.github/workflows/gh-pages.yml`. It rebuilds `site/`
and pushes it to the `gh-pages` branch with plain git. No third-party
actions.

- Pages source: Deploy from a branch, `gh-pages` / root.
- Custom domain: `flamingoandcactus.com`, Enforce HTTPS once issued.
  Every build writes `site/CNAME`, so the domain survives rebuilds.
- DNS at GoDaddy: `A @` to the four GitHub Pages addresses,
  `CNAME www` to `bearjhartjen.github.io`.
- The site goes live a minute or two after each push to `main`. A commit
  without a push changes nothing live.

Two gotchas we hit and fixed in the workflow: the `gh-pages` branch has
its own history, so the job fetches it first instead of pushing from main.
And the old `pages.yml` deploy sat inside the `github-pages` environment,
which only allows `gh-pages`, so it could never run from `main`. The check
for whether to deploy lives outside that gate now.

## What we will keep doing

Every change to this site gets a line here. What changed, and why. Short
and plain, so a stranger can follow it. The commit message can stay short.
This file carries the meaning.
