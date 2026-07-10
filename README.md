# Transfer Fees Monitoring

Tracks InstaPay and PESONet transfer fees for BSP-supervised participating
institutions (banks, e-wallets, EMIs), sourced only from each institution's own
public website, press releases, or Facebook page -- **never from BSP reports**.
The point is that this dataset can be used to independently check against BSP
reports, not derived from them.

Fee structures are rarely a single number. An institution can have several
concurrent conditions at once -- e.g. BPI has both a permanent standing rate
and, separately, a limited-time promo waiver for small transactions -- so this
project captures the full structure (fee type, conditions, effective/promo
dates) rather than reducing everything to one figure per network. Every figure
carries an audit trail: the source URL, when it was fetched, and an archived
copy of the raw page, so anyone questioning a number can trace it back to
exactly where and when it was captured.

## Setup

```
pip install -r requirements.txt
```

Create a `.env` file in the project root (already gitignored) with your
Anthropic API key -- extraction is LLM-based and needs it:

```
ANTHROPIC_API_KEY=sk-ant-api03-...
```

Get a key at [console.anthropic.com](https://console.anthropic.com) → Settings
→ API Keys. This is a separate account/balance from a claude.ai chat
subscription -- it needs its own credits or billing set up. Cost is trivial
for this workload (see "Cost" below).

Optional, for screenshot capture when a fee changes and for the Playwright
fallback fetch (see "Bot-blocked and JS-rendered sites" below):

```
pip install -r requirements-browser.txt
playwright install chromium
```

The scheduled GitHub Actions workflow installs this unconditionally (so the
fallback runs automatically); it's optional for local dev.

## Running it

Populate the database by scraping the entities in `config/entities.yaml`:

```
python -m scraper.run_all
```

Launch the dashboard locally:

```
streamlit run app.py
```

Run tests (fully mocked -- no API key or network calls needed):

```
pytest
```

## How extraction works

Fee text is read by an LLM (Claude Haiku, via `scraper/llm_extract.py`) rather
than regex/keyword matching. An earlier version of this project used regex --
it required a hand-rolled rule for every edge case found in testing (comma-
grouped numbers, magnitude words, a bare "free" matching inside "Toll-Free",
an OTC-vs-digital-channel state machine) and still couldn't represent an
institution having more than one real condition at once. The LLM handles all
of that through prompt instructions instead of one-off patterns, and returns a
structured list of **every** distinct fee condition found on a page -- zero,
one, or several -- instead of being forced to pick "the one best number".

For each condition, the model returns: `network` (InstaPay/PESONet),
`fee_type` (`flat` / `free` / `tiered` / `promo`), `amount`, `conditions` (a
full human-readable description -- thresholds, channel, eligibility), and
`effective_date`/`promo_end_date` when stated. It's explicitly instructed to
exclude over-the-counter (OTC) and ATM fees (only digital channels count), not
confuse a transaction limit or loan/cashback amount with an actual fee, and
return an empty list rather than force an answer out of irrelevant content.

**A safety net backs the OTC/ATM exclusion, not just the prompt.** Live
testing found the model doesn't always follow that instruction on pages that
table multiple channels together (Bank of Commerce: real OTC and ATM figures
were extracted despite the prompt explicitly excluding both). Every condition
whose own description names an excluded channel is dropped in
`scraper/llm_extract.py` after extraction, regardless of what the model
decided -- don't rely on prompting alone for this kind of correctness
requirement.

### Cost

Roughly $0.002-0.003 per page (Haiku pricing: $1/1M input tokens, $5/1M
output). At the current registry size (~19 configured entities, up to 8
candidate pages checked each), a full scheduled run costs well under $0.50.

## Adding institutions

`config/entities.yaml` is seeded from BSP's published
[list of supervised EMIs](https://www.bsp.gov.ph/Lists/Directories/Attachments/7/emi.pdf)
(29 EMI-banks + 40 non-bank EMIs, as of the version checked) -- a directory of
*who BSP supervises*, not a fee report, so using it to build the roster doesn't
conflict with the "never source fees from BSP reports" rule. That PDF is
updated periodically by BSP; re-fetching and re-diffing it against
`config/entities.yaml` by hand is currently the way to keep the roster current.

Only entities whose real domain is confidently known have a `website` block --
guessing at a domain risks pointing the scraper at the wrong site, so many
entries are left as name/category only, marked `# website: TBD`. A domain
being present also isn't a guarantee of good data -- see "Known limitations"
below. **Treat every scraped fee as provisional until you've checked its
archived snapshot in the dashboard's audit trail.**

Each entity's `website.mode` picks how its fee page is found:

- **`mode: fixed`** -- you already know the exact fee page. Set `website.url`
  and `website.selectors` (CSS selectors per network). The selector locates
  which element to read; the LLM still does the reading (fee type, amount,
  conditions), with the network forced to match the selector's key rather than
  trusted from the model's own guess.
- **`mode: crawl`** -- you only know the institution's domain. The crawler
  (`scraper/website/crawler.py`) finds relevant pages in two phases: first it
  checks robots.txt/sitemap.xml (faster and more reliable when a sitemap
  exists); if that doesn't clear the relevance bar, it falls back to a
  contextual link crawl, prioritizing pages/links that look fee-related (by
  URL/anchor-text keywords, normalized so hyphenated slugs like
  "transfer-fees" still match a "transfer fee" keyword phrase) over a plain
  crawl. Unlike an earlier version, this does **not** reduce to a single "best"
  page -- it returns up to `MAX_CANDIDATE_PAGES` (8) qualifying candidates,
  ranked by relevance score with publish-date as a tiebreak, and runs LLM
  extraction on **each one**, keeping every distinct condition found rather
  than discarding all but one page's worth of data. If no page clears the
  relevance threshold at all, the entity is flagged in `scraper_health` instead
  of guessing.

  Some institutions post fee changes as **news/press releases** rather than
  updating a dedicated fee page (`media center`/`press release` are included
  as scoring keywords for this -- kept deliberately narrow, since broader words
  like "advisory"/"announcement" were tried and reverted after they pulled the
  crawler toward unrelated notices).

Sites that need more than either mode can offer (e.g. JavaScript-rendered fee
tables) should get their own scraper module under `scraper/website/` rather
than forcing the generic or crawl scraper to handle everything.

## How it works

- `scraper/run_all.py` loads `config/entities.yaml` and calls `run_multi()` on
  each configured scraper, writing results into `storage/fees.db`:
  - `fee_snapshots` -- append-only fee readings (never overwritten). An entity
    can have several rows for the same network from the same run (different
    real conditions found on different pages) -- see "Reading the dashboard"
    below for how the UI groups these.
  - `audit_log` -- one row per page fetched: source URL, timestamp, content/
    structure hashes, archived raw page path. Every `fee_snapshots` row points
    to the `audit_log` row it came from.
  - `scraper_health` -- flags an entity/source when its page structure changes
    unexpectedly, a fetch/LLM call errors, or crawl discovery finds nothing at
    all clearing the relevance bar.
- `app.py` is the Streamlit dashboard: a per-institution card view by default
  (all currently-relevant conditions per network, each with its source link
  and dates), a timepoint picker for historical views, and a "System details"
  panel with fee history charts, the full audit trail, and Scraper Health.
- `.github/workflows/scrape.yml` runs the scraper on a daily schedule and
  commits the updated `fees.db` + snapshots back to the repo. Needs
  `ANTHROPIC_API_KEY` added as a repo secret (Settings → Secrets and variables
  → Actions) for the scheduled run to work.

### Reading the dashboard

Each entity card shows every currently-relevant condition per network, not
just one. "Currently relevant" means: every `fee_snapshots` row from that
entity's most recent scrape batch (rows within an hour of each other, since
pages within one run are fetched seconds to minutes apart -- see
`db.query_latest_fees_grouped`). A green badge means free/waived; an amber
badge means a limited-time promo (check the dates); a plain badge is a
standing flat/tiered fee. Click "source" on any condition to open exactly the
page it came from.

### Structure-change detection

Each website fetch computes two hashes: a `content_hash` over the whole page
(expected to change often) and a `structure_hash` over just the HTML tag
skeleton with text/attributes stripped (expected to stay stable). If
`structure_hash` changes from the last successful scrape of that page, the
entity/source is flagged in `scraper_health` -- visibly marked unverified until
someone reviews whether the page changed in a way that affects extraction.

## Known limitations found in practice

Live-testing the registry against real institution sites surfaced concrete
failure modes -- not hypothetical risks:

- **Bot/WAF-protected sites return nothing -- and a real browser doesn't
  reliably fix it.** BDO, PNB, Security Bank, and UnionBank all fail to fetch
  even a single page via plain HTTP. A Playwright fallback (real headless
  Chromium, tried automatically when plain HTTP finds zero candidate pages --
  see `_discover_with_browser` in `crawler.py`) was added and verified
  working in general, but tested against BDO specifically it *still* failed
  to fetch even the homepage (0 pages) -- this points to blocking at the
  network/WAF level (e.g. IP/datacenter reputation), not just a missing
  browser fingerprint, which a headless browser alone doesn't get around.
  These come back flagged in `scraper_health` rather than silently empty.
- **A "JS-rendered SPA" diagnosis can be wrong -- verify before assuming.**
  GCash, ShopeePay, and TayoCash were assumed to be app-shell SPAs invisible
  to plain HTTP. Testing the Playwright fallback found otherwise for GCash:
  its homepage already returns substantial content via plain `requests`
  (157KB) with only ~9% more from a full Chromium render (172KB) -- not the
  dramatic difference an empty SPA shell would show. The real reason no fee
  page is found is more likely that GCash's public marketing site simply
  doesn't publish a fee schedule in crawlable form at all (probably
  in-app-only content), which no fetch strategy fixes. Don't assume the
  JS-rendering theory without checking content-length before vs. after a
  real browser render, the way this was checked here.
- **A crawler can find the wrong page entirely, not just misparse the right
  one.** Maya (`maya.ph`) is a confirmed case: the crawler kept landing on an
  old (2021) promo blog post about a cashback voucher instead of a current fee
  schedule. No extraction fix solves a wrong-page problem -- Maya's `website`
  block is disabled in `config/entities.yaml` (marked `# website: TBD`) until
  replaced with a `mode: fixed` entry pointing at a verified real fee page.
- **Discovery can miss a genuinely relevant page.** BPI has (at least) two
  real, currently-relevant announcements -- a permanent rate change and a
  separate small-transaction promo waiver -- but the crawler's top-8 candidate
  cutoff has, in testing, sometimes found only one of them, depending on which
  pages happen to be discovered/linked at crawl time. This is a coverage gap,
  not a correctness bug (the pages it does find are extracted accurately) --
  if a specific known page is being missed, add it explicitly via a `mode:
  fixed` entry.
- **Truncation can cut off the real content if not done carefully.** A real
  bug found in testing: RCBC's fee page has ~8,400 characters of navigation
  before its actual fee table, so a blind prefix truncation at 8,000 characters
  cut the table off entirely before the LLM ever saw it -- silently returning
  nothing, not an error. Fixed in `scraper/llm_extract.py`'s `_trim_page_text`
  by centering the truncation window on wherever InstaPay/PESONet is first
  mentioned in the text, instead of truncating blindly from the start.
- **The model doesn't always follow its own instructions -- back it with a
  filter, not just a better prompt.** See "How extraction works" above re: the
  OTC/ATM safety-net filter. The general lesson: for any hard correctness
  requirement, verify the output programmatically rather than trusting prompt
  compliance alone.
- **A successful extraction still isn't automatically correct.** The model can
  misread a page same as any parser could. There's no independent verification
  layer -- that's what the audit trail and manual spot-checking are for.

## Other known risks

- **Facebook scraping is fragile.** There's no reliable open-source way to read
  posts from a page without a logged-in session or the paid Graph API --
  confirmed in testing: `mbasic.facebook.com` now shows a login wall even for
  well-known official pages. `scraper/facebook.py` best-effort fetches it
  anyway (uses the same LLM extraction as the website scrapers, so if access
  ever improves, real content benefits from the same fee-structure capture
  with no separate logic needed) and should be expected to find nothing most
  of the time; failures/empty results are logged to `scraper_health`, not
  raised as crashes.
- **Streamlit Community Cloud custom domains.** The free tier is generally
  accessed via a `*.streamlit.app` URL. Mapping the Spaceship-purchased domain
  directly to it isn't confirmed to work out of the box -- to be solved once
  the app is live (likely a redirect/landing page or proxy in front of it).
- **Repo size growth.** SQLite rows, HTML snapshots, and occasional
  screenshots only ever accumulate via the scheduled commit job. Fine to
  start; may need a pruning/archival strategy later.

## Open question: automating registry updates from the BSP EMI list

Right now, adding/removing institutions as BSP's EMI list changes is a manual
edit to `config/entities.yaml`. Automating that (fetch the PDF, parse it, diff
against the existing registry) is a reasonable follow-up but hasn't been
built -- it would add a PDF-parsing dependency and needs a decision on how to
handle entities the PDF removes (auto-delete vs. flag for manual review).
