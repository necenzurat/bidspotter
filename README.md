# BidSpotter scraper

Python command-line scraper starting from:

https://www.bidspotter.co.uk/secondary_categories_sitemapindex

It recursively reads sitemap indexes, collects English category URLs, follows category pagination, and visits individual auction lots. It saves titles, full descriptions, lot numbers, auctioneer and auction details, dates, category associations, image URLs, and prices when present. Missing values stay `null`; an opening price is kept separately from a current bid.

## Setup

Use Python 3.10 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

The default transport uses the Firecrawl CLI and its existing credentials. If it is not installed/configured:

```sh
npm install -g firecrawl-cli
firecrawl login
```

Firecrawl requests consume credits. Each uncached sitemap/category/lot fetch makes one scrape request; retries may make additional requests. The scraper does not read or copy credentials from the CLI configuration.

## Run

Start with a small sample:

```sh
.venv/bin/python scraper.py --category printing-equipment --max-pages 2 --max-lots 3 --output data-sample
```

Only export the sitemap's category list:

```sh
.venv/bin/python scraper.py --categories-only
```

Scrape all pages/lots for one category:

```sh
.venv/bin/python scraper.py --category woodworking/saws --output data-saws
```

Scrape all categories and their lots (potentially a large run):

```sh
.venv/bin/python scraper.py
```

Request limits are optional: `--max-categories`, `--max-pages` (per category), and `--max-lots` (lot pages attempted per invocation, including failures). Fetch retries are counted within a page attempt. Zero means unlimited. Category URL matching with `--category` is case-insensitive. All sitemap categories are exported even when only some are selected for lot collection.

## Outputs and resume

The chosen output directory contains:

| File | Contents |
| --- | --- |
| `categories.json`, `categories.csv` | Discovered categories and visited category titles |
| `lots.json`, `lots.csv` | One row/object per unique lot, including all discovered category associations |
| `summary.json` | Counts of categories, saved lots, completed jobs, pending jobs, and failed jobs |
| `state.sqlite3` | Durable work queue, category associations, extracted records, and failure reasons |
| `pages/` | Cached page responses for restarting without refetching successful pages |

Records are committed to SQLite after each successful page. Exports are written at the end of a run, on a handled error, and on Ctrl-C. JSON files remain valid JSON arrays; CSV list fields contain JSON arrays. A CSV file appears once that record type has data. Spreadsheet formula-like text is escaped in CSV; JSON retains the original text.

Rerun the same command to continue. Completed lot details are skipped, pending pagination is retained, and failed jobs are retried. A per-run lot limit allows successive runs to collect more lots. Limits and filters leave skipped work pending; pending does not mean failure. Export saved records without any network access:

```sh
.venv/bin/python scraper.py --output data-sample --export-only
```

Use a **new output directory** for a fresh snapshot. Resume uses cached pages and saved details; it does not refresh prices or remove lots that disappeared from the site. Run only one scraper process per output directory.

Failure details:

```sh
sqlite3 data-sample/state.sqlite3 "SELECT url,error FROM jobs WHERE status='failed';"
```

The scraper returns exit code 1 when selected pages fail, 130 on Ctrl-C, and 0 after successfully completing the selected work. A restricted sample can finish successfully with pending jobs outside its limits.

## Chrome browser mode

Direct HTTP requests to this site can return a human-verification page. An alternative to Firecrawl is a visible, locally installed Google Chrome:

```sh
.venv/bin/python scraper.py --transport browser --category printing-equipment --max-pages 1 --max-lots 3 --output data-browser
```

This creates an isolated Chrome profile inside the output directory. If the site displays human verification, complete it manually in that window; the scraper waits up to 180 seconds (`--human-timeout`). It does not solve verification challenges. Some environments receive HTTP 403 even in Chrome; use Firecrawl in that case. `--headless` is available for environments where Chrome can already load the site, but cannot handle manual verification.

Requests are sequential, with a default two-second minimum interval. Transient fetch failures retry twice with backoff. `--delay`, `--retries`, `--timeout`, and `--wait` control those settings. Unexpected markup and unrendered category pages fail explicitly rather than being saved as empty results. Only English public category and lot pages are followed; images are exported as URLs, not downloaded.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests cover sitemap traversal, duplicate/recommended lot exclusion, pagination, full descriptions, missing prices, interrupted-run limits, category associations, exports, and retrying failed pages.

Live sample verification on 6 November 2026: the child sitemap contained 118 category URLs. Two category pages yielded 120 lot URLs, and three full lot records were saved successfully.

The repository includes the CSV/JSON exports, cached HTML, and resumable SQLite database in `data-sample/`, plus the original scrape results and retrieved Firecrawl documentation in `.firecrawl/`. The sample is a partial crawl with pending work. Rerun the sample command to continue it, or choose a new output directory for a fresh snapshot. Local Python environments, bytecode, browser profiles, credentials, and the generated code-review index are excluded.
