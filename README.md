# LiveSellSI

Ultra-lightweight, zero-cost tracker of verified `.si` (Slovenia) domain sales. Static Astro site with a GitHub Actions data pipeline — no database, no servers, no client-side frameworks.

**Domain:** livesellsi.com

## Stack

| Layer     | Tech                                                              |
| --------- | ----------------------------------------------------------------- |
| Frontend  | Astro 5 (SSG), Tailwind CSS 4, ~0 KB client-side JS (vanilla only) |
| Data      | Static JSON in the repo (`src/data/sales.json`)                    |
| Pipeline  | Python 3 stdlib script (`scripts/fetch_sales.py`)                  |
| Scheduler | GitHub Actions cron (`0 8,20 * * *` UTC + manual dispatch)         |
| Hosting   | Vercel or Netlify free tier (auto-deploys on push)                 |

## Directory structure

```
livesellsi/
├── .github/workflows/update-data.yml   # cron pipeline: fetch → commit → push → deploy
├── public/
│   ├── favicon.svg
│   └── robots.txt
├── scripts/
│   ├── fetch_sales.py                  # fetch/normalize/dedupe/sort/write
│   └── seeds/seed_sales.json           # manual entries, always merged
├── src/
│   ├── components/SalesTable.astro     # shared table (homepage)
│   ├── data/sales.json                 # canonical dataset (auto-generated)
│   ├── layouts/BaseLayout.astro        # head, header (LIVE dot), footer
│   ├── lib/format.ts                   # USD/date formatting, sort helpers
│   ├── pages/
│   │   ├── index.astro                 # tabs, search, aggregate metrics
│   │   └── sales/[slug].astro          # pSEO pages (getStaticPaths)
│   └── styles/global.css               # Tailwind v4 + live-dot animation
├── astro.config.mjs
├── package.json
└── tsconfig.json
```

## Quick start (local)

```bash
# 1. Install frontend dependencies
npm install

# 2. Run the data pipeline (optional network scrape; seed merge always works)
python scripts/fetch_sales.py            # or: --dry-run / --skip-scrape / --verbose

# 3. Develop / build
npm run dev      # http://localhost:4321
npm run build    # outputs dist/
npm run preview  # serve the production build
```

## Data pipeline

`scripts/fetch_sales.py` (Python 3.9+, stdlib only):

1. Loads existing records from `src/data/sales.json`.
2. Merges manual entries from `scripts/seeds/seed_sales.json` (add curated/private sales here).
3. Scrapes the current DNJournal weekly chart + the last N archive pages (`--max-weeks`, default 6) and extracts `.si` rows (domain, USD price, venue, reporting-period end date).
4. Normalizes (lowercase domain, `id` slug, price/date validation, category heuristic), dedupes by domain, sorts by date desc, and rewrites `sales.json` **only if content changed**.

Rules and caveats:

- **Dedupe:** first-seen wins per domain, except a public record (venue != "Private") replaces a `Private` placeholder.
- **Currency:** DNJournal rows use the page's explicit `= $X` conversion; bare €/£ fall back to static rates (1.12 / 1.28) defined at the top of the script.
- **Date:** uses the chart's reporting-period end date (DNJournal publishes weekly/bi-weekly; year-to-date chart rows are dated at their reporting period since DNJ does not publish exact per-sale dates).
- **Category:** keyword heuristic when a record has no explicit category; labels ≤ 3 chars become `Short Brandable`.
- Network failures only log warnings — the run still succeeds and keeps existing data.

### Schedule & deployment flow

- GitHub Actions runs at **08:00 and 20:00 UTC daily** (`0 8,20 * * *`) and on `workflow_dispatch`.
- If `sales.json` changed, the bot commits and pushes to `main`.
- The push triggers Vercel/Netlify auto-deploy natively — no extra step.

Setup notes:

- Repo must have `Settings → Actions → General → Read and write permissions` (the workflow also sets `permissions: contents: write`).
- If `main` has branch protection, allow the `github-actions[bot]` user or change the workflow to push via a PR.

## Deploy to Vercel (free tier)

1. Push this folder to a GitHub repo.
2. In Vercel: **Add New → Project → Import** the repo.
3. Framework preset: **Astro** (build command `npm run build`, output `dist`). No env vars needed.
4. Deploy. Every bot push to `main` redeploys automatically.

Netlify works the same way (Build command `npm run build`, Publish directory `dist`).

## pSEO & SEO

- Every record in `sales.json` gets a static page at `/sales/{id}/` (`getStaticPaths`).
- Each page ships pre-rendered `og:title`/`og:description`, Twitter cards, canonical URL, and `schema.org/Product` JSON-LD (price, currency, venue, date, category, length, reg-fee multiplier).
- `@astrojs/sitemap` emits `sitemap-index.xml` for all pages.

## Sample data disclaimer

The bundled `src/data/sales.json` and `scripts/seeds/seed_sales.json` contain seed records (a mix of real reported sales such as Bot.si and Iodata.si, plus illustrative placeholder entries). The pipeline replaces/extends them with scraped public records over time. Delete entries you cannot verify — the script will not re-add them unless they reappear in a scraped source.

## Performance

- 100% static HTML, system fonts, one CSS file, ~1 KB inline JS → targets Lighthouse 100.
- No external requests, no analytics, no cookies.
