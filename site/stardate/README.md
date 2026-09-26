# The Stardate log

The lab book of the collective. Every experiment gets one entry, always in the same four
parts: **What we did**, **What we learned**, **Open questions** and **Lab book log**.

- Entries (the source of truth): `site/stardate/entries/*.md`, one Markdown file each.
- Published pages: `site/assets/stardate/` (generated; do not edit by hand). They live under
  `assets/` because the Pages workflow publishes `site/assets/` as a whole folder, so the log
  goes live without any workflow change. Public address: `https://borg.utlyze.com/assets/stardate/`.
- The home page shows the three newest entries between the `stardate:latest` markers in
  `site/index.html`. The publisher refreshes that block too.

## Stardates

A stardate is the four-digit year, a dot, and the three-digit day of the year, counted in
America/Denver: `2026.268` is 25 September 2026, and `2027.001` is 1 January 2027. Several
entries can share a stardate. The index lists entries newest first: by stardate, then by
`time` (latest first; entries without a time come after timed ones), then by file name.

## Add an entry

1. Create `site/stardate/entries/<YYYY>-<DDD>-<slug>.md`, where `<YYYY>-<DDD>` is the stardate
   with a hyphen and `<slug>` is lowercase words joined by hyphens:
   `site/stardate/entries/2026-269-first-protected-nightly.md`.
2. Write it in exactly this shape:

   ```markdown
   ---
   stardate: 2026.269
   title: The first protected nightly
   date: 2026-09-26
   time: "03:30"
   status: running
   summary: One or two plain sentences, at most 280 characters.
   ---

   ## What we did

   The question, the setup, and the size (runs, items, days).

   ## What we learned

   Results with numbers. Say plainly what surprised us.

   ## Open questions

   - What we still don't know, and what we would test next.

   ## Lab book log

   - `2026-09-26 03:30` One short line per event, oldest first.
   - `04:10` A bare time means the entry's own date.
   - `review` Use a lowercase phase word when the source has no time.
   ```

3. Publish and check, from the repository root:

   ```sh
   node site/stardate/publish.mjs    # writes the entry page, the index and the home block
   node site/verify-stardate.mjs     # format, freshness, order, links and privacy checks
   ```

4. Commit the new Markdown file together with the files the publisher changed.

## The rules the checker enforces

- Front matter keys: `stardate`, `title`, `date`, `status`, `summary`, and optional `time`
  (`HH:MM`, America/Denver). Nothing else.
- `stardate` must match `date`. `status` is `running`, `finished` or `superseded`. When a later
  entry replaces an older one, set the older one to `superseded` and say so in its log.
- The four `##` sections appear exactly once, in order, and no other headings.
- Allowed Markdown: paragraphs, `-` and `1.` lists (one nested level), `**bold**`, `*italic*`,
  `` `code` ``, and simple pipe tables. No raw HTML, links, images or code blocks.
- Lab lines are `` - `STAMP` text ``, where STAMP is `YYYY-MM-DD HH:MM`, `MM-DD HH:MM`, `HH:MM`,
  `YYYY-MM-DD`, or a lowercase phase word.

## Public-site privacy, every time

The log is public. Never write people's names, client or company names, email addresses,
account or seat names, machine hostnames (say "a studio" or "the fleet"), file paths or file
names, IP addresses, URLs, keys, tokens, hashes, run, session or tracker ids. Use only numbers
that appear in the experiment's own report, and leave a number out when the report is unclear.
`verify-stardate.mjs` scans every entry and published page for these patterns, but it is a
floor, not a substitute for reading the entry before it ships.

## For a nightly job

A job that files entries should: write one Markdown file per experiment in the shape above,
run the publisher, run `node site/verify-stardate.mjs`, and commit only when it passes. The
publisher refuses to write anything while any entry is invalid, and it removes pages whose
entry file was deleted.
