# Config-driven scraper + live dashboard

Manage everything from the **dashboard** — add a site, edit its selectors,
test them, and launch runs, all in the browser. No files, no CLI required. The
engine fetches, extracts to your field schema, dedupes into Postgres, and the
dashboard streams records in live as they're scraped.

The **database is the source of truth** for site configs. The dashboard reads
and writes it; the `sites/*.json` files are just optional seeds you can import
with `register`. (You can still drive everything from the CLI if you prefer.)

This is the generalized version of the schoolsindia harvester: that scraper is
now just one config (`sites/schoolsindia_cbse_haryana.json`) among many.

## Manage everything from the dashboard

Open `http://localhost:8050` and use the **Configuration** panel:

1. Click **+ new** (or click a site to edit it).
2. Fill in name, url, engine, and the **fields table** — one row per field with
   its selector/path, type, attr, transform, and match/regex. For `http_json`
   sites a request-block editor appears.
3. **probe** — one fetch, shows HIT/MISS per field so you fix selectors before
   committing. **save** stores the config. **save & run** launches a background
   run you watch fill in live. **delete** removes the site and its records.

The rest of the dashboard updates every few seconds: per-site record counts and
last-run status, a live run feed, per-field fill rates with **drift alerts**
(a field that used to fill and suddenly doesn't → red), a records table, and
per-site csv/xlsx export.

## Layout

```
config.py     settings (DB env, polite delays, drift thresholds)
schema.py     loads/validates a per-site JSON config
extract.py    selectors/paths -> typed values  (unit-tested, pure)
fetcher.py    polite HTTP client + optional Playwright fallback
engine.py     fetch + extract + pagination, with auto browser-fallback
db.py         Postgres: sites / runs / records (dedup) / field_stats
runner.py     CLI: register | probe | run
dashboard.py  FastAPI live dashboard (read-only)
sites/        one .json per target
```

## Add a site by file (alternative to the dashboard)

Prefer files or version control? Drop a file in `sites/` and `register` it.
Two styles depending on `engine`:

**HTML (CSS/XPath selectors)** — `engine: auto | http_html | browser`

```json
{
  "name": "books_toscrape",
  "url": "https://books.toscrape.com/",
  "engine": "auto",
  "key_fields": ["url"],
  "list": { "container": "article.product_pod" },
  "next_page": "li.next a::attr(href)",
  "fields": {
    "title":    { "selector": "h3 a", "attr": "title", "type": "string" },
    "price":    { "selector": "p.price_color", "type": "number", "transform": "currency" },
    "in_stock": { "selector": "p.availability", "type": "boolean", "match": "In stock" },
    "url":      { "selector": "h3 a", "attr": "href", "type": "url" }
  }
}
```

**JSON API (dotted paths)** — `engine: http_json` (the schoolsindia case)

```json
{
  "name": "my_api",
  "engine": "http_json",
  "request": {
    "method": "POST", "url": "https://api.example.com/search",
    "body": { "state": "Haryana" },
    "page_param": "page", "page_size_param": "pageSize",
    "page_start": 1, "page_size": 25
  },
  "total_path": "totalCount",
  "fields": { "name": { "path": "name", "type": "string" } }
}
```

### Field spec keys
- `selector` (HTML) or `path` (JSON) — where the value lives
- `type` — `string | number | boolean | url | list`
- `attr` — pull an attribute instead of text (e.g. `href`, `src`, `title`)
- `transform` — `currency | int | lower | upper | strip`
- `regex` — keep capture group 1 (e.g. `"star-rating (\\w+)"`)
- `match` (boolean) — true if the element's text contains this string
- `list.container` — selector for a repeated record block on the page
- `next_page` — CSS `::attr(href)` selector for pagination
- `key_fields` — which fields make a record unique (drives dedup); `[]` = all

### Engines
- `auto` — fetch static HTML; if few fields match, retry that page in a browser
- `http_html` — static HTML only (fastest)
- `browser` — always render with Playwright (for SPAs)
- `http_json` — call a JSON API and map dotted paths

## Run (CLI — optional)

The dashboard does all of this, but the CLI is here when you want it (e.g. cron
for scheduled runs). It reads configs from the DB, falling back to a file for a
name that isn't registered yet.

```bash
pip install -r requirements.txt
# only if you use browser/auto-fallback:
playwright install chromium

export POSTGRES_HOST=localhost POSTGRES_PASSWORD=...   # or use docker compose
export SCRAPER_UA="YourName/1.0 (contact: you@example.com)"

python runner.py register                 # seed DB from sites/*.json (optional)
python runner.py probe books_toscrape      # one fetch; HIT/MISS per field
python runner.py run   books_toscrape      # full run -> Postgres
python runner.py run   --all               # every enabled site (cron this)

uvicorn dashboard:app --port 8050          # then open http://localhost:8050
```

Or all-in with Docker:

```bash
docker compose up -d postgres dashboard
docker compose run --rm runner register
docker compose run --rm runner run --all
```

## Workflow (same spirit as the old probe-then-harvest)
1. Write the config.
2. `probe` it — a single fetch prints what each selector pulled and flags
   misses. Fix selectors until everything HITs.
3. `run` it. Reruns are safe: records dedupe on `(site, key_fields)`, so you
   never double-write; existing rows just get their `last_seen` bumped.

## Drift detection
Every run records a fill rate per field. When a field that normally fills
(≥ 60%) drops to near-zero, the dashboard flags it red — that's the signal a
site changed its HTML and a selector silently broke. Re-probe and fix the one
selector; nothing else changes.

## Politeness (kept from the original)
Randomized delay between every request, retries with exponential backoff on
429/5xx, a `User-Agent` that identifies you and a contact. Respect each site's
`robots.txt` and terms, and keep clear of personal data without a lawful basis
(DPDP). Defaults are deliberately gentle — raise them only when you should.

## Tests
```bash
python test_extract.py     # offline: extraction + coercion + JSON paths
```
