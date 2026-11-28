# Poll form settings

Every Flamingo & Cactus poll uses the same Google Form settings. Set these when the
form is created, then check them before publishing.

- Title: the product name, spelled the same as on the Polls page.
- First question: the poll question, Multiple choice, options `Yes` and `No`,
  Required on.
- Second question: `Comment`, Paragraph, not required.
- Settings → Responses:
  - Collect email addresses: Verified
  - Send responders a copy of their response: When requested
  - Allow response editing: on
  - Limit to 1 response: on
- Settings → Presentation: Show progress bar off, Shuffle question order off,
  View results summary off.
- Publish the form, link a response sheet, then add a `Public` tab with
  `=QUERY('Form Responses 1'!A:D,"select A,B,C where A is not null",0)` and publish
  that tab as CSV. Never publish the response tab: it carries respondent emails.
- Add the form link and the public CSV link to `data/polls.json`.

On the site the form is embedded at 1000px tall, 1180px at 700px wide or less, and
1260px at 380px wide or less. A longer form sets `form_height` (in pixels) on that
poll instead of changing the CSS.
