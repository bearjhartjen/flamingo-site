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

Clicking the star plays a comic ink-and-paper burst: a black impact
frame, jagged white rays, black and white shards fly out, then the page
reroutes. Returning to a visited page plays a dark flash + soft rise.

The comic burst is the only star effect this site ships. There is no
dropdown in the builder for it, on purpose. Older states tried
`portal`/`comic`/`fade`/`none` and the toggle kept flipping back. The
October 2026 build dropped the others entirely:

- `data/site.json` carries `star_animation` set to `"comic"`.
- `scripts/build.py STAR_ANIMATIONS = ("comic",)` — anything else fails.
- The builder sidebar shows a one-line explanation, not a dropdown.
- Both save handlers force the value to comic on every write, so a
  stale tab or an external edit cannot drift it back.

## The contact form

The Contact page form is the one thing that is not static. It posts to
a third-party service that emails each message to our inbox. The email
address on the page is plain text on purpose. We want people to reach us.

A disclaimer card sits just above the form. It states what services run
the form (FormSubmit by default, with Web3Forms pre-wired as a silent
failover) so visitors know who is handling their note before they hit
Send.

What we decided:

- No captcha. A honeypot field catches spam instead.
- The form stamps the sender's local Eastern time.
- The page shows a clear verdict. "It went through" with a time stamp,
  or an error panel with the service's own message and a plain-email way out.
- Plain-text notes go by AJAX POST to the primary service.
  Notes with files go by the classic multipart POST into a hidden
  iframe, with a 20-second watchdog and a handoff ack from
  `contact-ack.html` (see below).
- File sizes read in KB up to 1024 KB, then MB. The cap is 10,000,000 bytes.
  That is the FormSubmit limit, not ours.
- The button says "Send it our way."
- The wording lives in `data/form.json`, not in the page file, so editing
  the Contact page text can never break the form. There is a Form button in
  the builder to edit that wording.

### Failover and resilient sending (October 2026)

FormSubmit went down on October 29, 2026, while a visitor was typing a
note. The visitor lost their draft and had nowhere to send. We fixed
both halves of that story.

- **What survives a failed send.** Every keystroke in any text field
  saves to `localStorage` under `flamingo-cf-draft`. On reload, the page
  restores that draft and shows a chip: *"Draft restored from your browser
  — it will clear when the note goes through."* The draft clears itself on
  successful send or when the visitor clicks *Send another note*.
- **What happens if FormSubmit is down.** Plain-text notes post to
  FormSubmit's AJAX endpoint first. On `success !== "true"`, network
  error, or timeout, the same payload is retried through Web3Forms
  (`https://api.web3forms.com/submit`) using the access key in
  `data/form.json` (`web3forms_access_key`). The visitor sees the same
  *"It went through"* panel in both cases — the failover is silent. A
  third backup and the file story are below.

### A third backup, and an invisible frame (October 29, 2026)

Today FormSubmit's origin went down again — a Cloudflare 521, "Web
server is down" — and its answer page would have landed the visitor on
an error page the moment they attached a file and pressed Send. We
re-built the sending around three services and a page that never
navigates away.

- **Three services, in order.** FormSubmit (primary), then Web3Forms,
  then FormInit (forminit.com, formerly Getform) as the last resort.
  FormInit's form ID is public by design, lives in
  `data/form.json` (`forminit_form_id`), and is baked into the page the
  way a form `action` is. API keys never go on a public page; the
  builder dialog says so. Cross-checked against FormInit's docs:
  browser posting needs no key at all (public form), rate limit one
  submission per 5 seconds, and it takes up to 20 file blocks / 25 MB
  combined — more than our own 10 MB cap, so the last backup can always
  carry what the form allowed. FormInit validates `sender.email`, so
  the site only sends a sender block when the contact field actually
  parses as an email.
- **Files retry through the backups.** Web3Forms takes exactly one file
  of 5 MB or less, as multipart with the file attached. Anything over
  that limit is announced in the status line — *"Attachments exceed the
  Web3Forms limits (one file, 5 MB or less) — sending everything
  through FormInit (up to 25 MB) instead…"* — and rerouted with the
  files still attached. Nothing is silently dropped.
- **The page no longer leaves on submit.** With files attached the
  classic multipart POST goes into a hidden iframe
  (`form.target="cf-frame"`), not into the page. FormSubmit's
  `_url` field (its redirect parameter; the `_next` name we carried was
  Formspree's and FormSubmit ignores it) turns the frame into
  `site/contact-ack.html` after a genuine success, and that page — ours,
  on our domain — tells the parent through `postMessage`. The frame
  landing on a Cloudflare 521 speeds nothing to the visitor and kills
  nothing: the visible page stays put, with every field and file row
  intact. The same treatment covers the AJAX path's classic fallback.
- **Named hops, no silent drops.** Every retry is announced: *"Web3Forms
  did not answer — retrying through FormInit…"*, *"Attachments exceed…"*,
  and a watchdog (20 s for files, 5 s after a dead AJAX fetch) that
  walks the chain while the page still stands. Known tradeoff: if the
  first send was merely slow and not dead, the note can arrive twice.
- **Verified against the real outage.** While FormSubmit was returning
  Cloudflare 521s we walked every path in a browser: two oversized
  files, one small file with no Web3Forms key, and a text-only note.
  All kept their drafts, named their hops, and ended in the honest
  error panel with both backup names — no exceptions, no navigation.
- **How to arm the chain.** Web3Forms access key (free at
  web3forms.com) + FormInit form ID (free form at forminit.com) both go
  into the Form dialog's *"Third-party services + failover"* section.
  Missing pieces are skipped, named as much: *"no last backup is
  configured"* or *"a backup is configured but has no access key"*.
  The disclaimer card names all three services with the order.
- **Armed October 29, 2026, evening.** The owner signed up for both
  services and the keys went into `data/form.json` (`web3forms_access_key`,
  `forminit_form_id` `ju1kw1ai9w4`). Live end-to-end test while
  FormSubmit was still returning Cloudflare 521s: a text-only note posted
  from the local build walked FormSubmit AJAX (dead) -> Web3Forms and
  showed *"Your note went through with a backup service."* The note
  arrived in the inbox a minute later with the name/message/footer
  intact. The chain is no longer theoretical.

**Correction (October 29, 2026, late).** FormSubmit's redirect field is
`_next`; the `_url` this site sent is not a FormSubmit field, and the entry
above claiming the reverse was wrong. With `_url` the ack page never loaded,
so every note with files waited out the 15-second watchdog and was sent on to
backups that cannot carry files (Web3Forms attachments are a paid feature).
The form now sends `_next`. If no service can carry a file, the note's text is
still delivered with the file names listed, and the visitor is asked to email
the file. Backup answers that are not JSON now report their HTTP status.

How to turn on the failover fully:

1. Sign up at [web3forms.com](https://web3forms.com) — the access key is
   free and shows up on the dashboard.
2. Open the Contact page in the builder, press **Form**, scroll to
   *"Third-party services + failover"*, paste the access key.
3. Apply + Save. Next build embeds the key in `site/contact.html`. The
   page never echoes the key back to the dialog (no accidental disclosure
   in screenshots).

What we learned: FormSubmit needs a Referer header. Local test pages get a
"needs activation" reply. Only the live domain is activated. That is
normal. With the failover wired, an unactivated local test still shows
the *"a backup is configured but has no access key"* panel — paste a key
to make local testing also genuinely succeed.

### Files take their own road; the visitor stays put (October 30, 2026)

A visitor attached a file while FormSubmit was down. Web3Forms emailed
"no file came through" (its free plan cannot attach files), and only then
did FormInit deliver the note with the file, after the old 45-second
wait. Two emails, a long silence, and a visitor likely to give up and
close the tab. Fixed:

- **Files go to FormInit first.** It answers over `fetch` with a real
  yes/no, takes up to 20 files / 25 MB, and its timeout now grows with the
  upload size. If it refuses or is unreachable, the classic multipart POST
  goes to FormSubmit in the hidden iframe (30 s watchdog, scaled by size).
  If that is never confirmed, the note's words are delivered on their own
  with the file names listed. Web3Forms is no longer asked to carry files.
  Text-only notes keep the old order: FormSubmit, Web3Forms, FormInit.
- **The visitor cannot leave by accident while a send is in flight.** The
  browser asks before the tab is closed or navigated away; the status line
  shows a moving bar and elapsed seconds; the file list is locked so it
  cannot change mid-upload; and the hidden iframe is sandboxed
  (`allow-forms allow-scripts`) so no answer page, hostile or broken, can
  navigate the real page. All of it switches off the moment the send
  ends, success or failure.
- **The disclaimer card says so**: notes with attachments go to FormInit
  first, then FormSubmit.
- **Tested** in a real browser against mocked FormSubmit / Web3Forms /
  FormInit: text note, file note, FormInit down, FormSubmit down, every
  file road dead (note still delivered as text), a frame-busting answer
  page, leaving mid-send, and leaving while idle.

Anti-duplicate (October 30, 2026): FormInit was the service showing up twice
in the inbox. Files already POST to FormInit first; when the browser timed out
or lost the response, the chain treated that as failure and sent again
(FormSubmit iframe and/or FormInit as last resort) while FormInit had often
already accepted the first upload. Fix: one FormInit attempt per send
(`formInitUsed`), single-flight `settled` gate, abort in-flight backups on
success, and on FormInit timeout/network do not re-send the same note through
another path. A pure FormSubmit AJAX timeout also no longer auto-fires
Web3Forms. Each send carries a short `ref` token. Explicit refusals still walk
the remaining chain once.

One send, one service (October 31, 2026): a submit still produced a
FormInit email AND a FormSubmit email. The October 30 fix only covered
timeouts. The chain (FormSubmit -> Web3Forms -> FormInit, and for files
FormInit -> FormSubmit iframe -> text rescue) still re-sent through the next
service after any ambiguous answer: a reply that was not JSON, a slow ack
from the hidden frame, the 45 s watchdog. A browser cannot tell "never
arrived" from "arrived, answer lost", so any chain that re-sends can deliver
twice. Fix: the page makes exactly one request per send, to FormInit, which
answers over fetch with a JSON verdict for text and files alike. Delivered
shows the success panel. A JSON refusal shows the error panel (nothing was
stored, Send again is safe). A timeout, network error or non-JSON reply is
announced as unconfirmed and is never re-sent automatically; the visitor is
pointed at the email address. The Web3Forms hop, the FormSubmit AJAX and
iframe roads, the watchdog and the text rescue were removed. With
JavaScript off the plain form still posts once to FormSubmit. The cost, on
purpose: if FormInit itself is down, the page says so and shows the email
address instead of quietly trying another service. To bring failover back
safely it needs a server that can deduplicate by `ref`, not browser retries.

### Disclaimer position (October 2026)

The disclaimer card used to sit between the intro paragraph and the
form, splitting the page. It now rides directly under the form wherever
the `[contact form]` marker sits — and the marker sits above the plain
email/phone lines in `data/pages/contact.html`. Page order is now intro,
form, disclaimer card, then plain contact info. Nothing about editing
changed: the email/phone lines edit inline on the canvas, the disclaimer
title/body/service name edit through the Form button.

### Unconfirmed file sends (October 31, 2026)

A file note sent from the live page arrived at the inbox with its
attachment, but the page still said "FormInit did not confirm". The file
road posted multipart FormData without an `Accept: application/json`
header. FormInit answers that as a normal browser form post: a 302 to its
thank-you page, not a JSON verdict. `fetch` followed the redirect, got
HTML, and the script called a delivered note unconfirmed. Reproduced with
curl: the same multipart post returns 302 by default and
`{"success":true,...}` with the JSON header. Fix: both text and file
requests now ask for JSON, and a redirect to FormInit's own thank-you page
is also accepted as delivery. The success panel now shows FormInit's
submission reference when FormInit returns one. The
disclaimer was rewritten to describe this exactly: one request, no
automatic retry, 10 MB of files, and the direct email address if a send
is not confirmed. The builder's dead Web3Forms/failover fields and their
wrong hints were removed; the stored values are left untouched.

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
