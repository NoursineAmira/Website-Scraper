# Supreme Drop Scraper

**Course:** Information Assurance & Security  
**Target:** `https://supreme.com/previews/springsummer2026/all`  
**Stack:** Python 3.11+, httpx, BeautifulSoup4, SQLAlchemy + SQLite, APScheduler, structlog

---

## How it works

Supreme's preview page uses **Next.js App Router with React Server Components (RSC)**. The full product catalogue (323 products) is embedded as JSON inside `<script>` tags in the raw HTML response — identical to what `curl` retrieves. No JavaScript execution or browser automation is needed.

**Parsing pipeline (3 stages):**
1. `BeautifulSoup` locates `<script>` tags (CSS selector)
2. Regex extracts the `self.__next_f.push([1, "..."])` RSC chunks and decodes them
3. `json.loads()` parses the embedded `"products"` array

**Change detection:** On each scrape cycle, products present in the database but absent from the current response are marked `"removed"` and trigger an alert. Newly added products are marked `"in_preview"` and also trigger an alert. This detects when Supreme quietly adds or removes items from the preview.

---

## Local Setup

```bash
# 1. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate

# 2. Install pinned dependencies
pip install -r requirements.txt

# 3. Configure credentials (optional — alerting works in mock mode without them)
cp .env.example .env
# Edit .env to add your DISCORD_WEBHOOK_URL if desired
```

---

## Discord Webhook Setup 

1. Open Discord → create a free server (or use an existing one)
2. Click a channel's ⚙️ **Settings** → **Integrations** → **Webhooks** → **New Webhook**
3. Copy the webhook URL
4. Paste it in your `.env` file:

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your_id/your_token
```

---

## Running the Demo

### Offline demo

Uses the synthetic fixture HTML. No network required. Shows the full pipeline including change detection.

```bash
# Single run
python demo.py --fixture

# Two runs — second run simulates products disappearing from the preview
python demo.py --fixture --twice
```

### Live demo

Fetches live data from supreme.com.

```bash
python demo.py
```

### Web dashboard

```bash
python web_ui.py
```

Open `http://localhost:5000` in your browser. The dashboard lets you:
- **Reset & Seed Demo** — loads 5 synthetic products silently (no alerts sent)
- **Configure Next Scrape Cycle** — check products to remove or add new ones
- **Run Simulated Scrape** — runs the full pipeline; sends Discord alerts for any additions or removals

### Continuous scheduler

```bash
python -m supreme_scraper.scheduler
```

Runs every `SCRAPE_INTERVAL_MINUTES` (default: 15). Stop with Ctrl-C.

---

## Alert Types

| Event | Discord Message |
|---|---|
| New product added to preview | 🆕 BRAND NEW PRODUCT ADDED |
| Product back in preview | 🟢 BACK IN PREVIEW |
| Product removed from preview | 🔴 REMOVED FROM PREVIEW |

---

## Tests

```bash
pytest tests/ -v
```

All tests run offline. No network calls, no credentials needed.

| Test file | What it covers |
|---|---|
| `test_parser.py` | Text sanitization, URL normalization, RSC extraction, full parse |
| `test_store.py` | DB upsert, no-duplicate, change detection, removal detection, audit log |
| `test_tls.py` | TLS config fields absent, `certifi.where()` in source, CA bundle on disk |

---

## Project Structure

```
supreme_scraper/
├── config.py          Settings (python-dotenv); TLS unconditionally in crawler.py
├── logging_config.py  structlog JSON + SensitiveFilter credential redaction
├── models.py          SQLAlchemy ORM: Drop, ScrapeLog (append-only)
├── database.py        Async engine, create_all bootstrap, WAL mode
├── crawler.py         httpx + certifi TLS, robots.txt gate, 2s rate limit
├── parser.py          BeautifulSoup → RSC extraction → JSON → validated dicts
├── store.py           Upsert, change detection, mark_removed, audit log
├── alerting.py        Discord Webhook or mock-log fallback
└── scheduler.py       APScheduler wiring; audit log always written in finally
```

---

## InfoSec Checklist

| Concept | Location | Demonstration |
|---|---|---|
| **TLS Enforcement** | `crawler.py:95` | `verify=certifi.where()` hardcoded; `test_tls.py` asserts no config toggle exists |
| **Input Validation** | `parser.py:_sanitize_text`, `_normalize_url` | Type checks, length caps (`String(N)` on all columns), URL scheme whitelist, stock_status whitelist |
| **Output Sanitization** | `parser.py:_sanitize_text` | Control char removal (`\x00-\x1f\x7f`), whitespace normalization before any DB write |
| **Credential Management** | `config.py`, `.env.example`, `.gitignore` | `python-dotenv` loads webhook URL from `.env`; URL never in source; `.env` gitignored |
| **Log Redaction** | `logging_config.py:sensitive_filter` | Regex processor runs **before** `JSONRenderer`; strips credential-shaped strings from every log event |
| **Audit Trail** | `models.py:ScrapeLog`, `store.py:append_scrape_log` | Append-only table; no UPDATE path; written in `finally` block even on failure |
| **Data Minimization** | `models.py` `String(N)` lengths, `parser.py` | Schema defines exactly what is stored; parser fills only named fields |
| **Polite Crawling** | `crawler.py:_enforce_rate_limit` | 2-second `time.monotonic()` gap between requests |
| **robots.txt Compliance** | `crawler.py:RobotsTxtGate` | Parsed before first fetch; `CrawlDisallowedError` aborts crawl if disallowed |
| **Structured Logging** | `logging_config.py:configure_logging` | `structlog` with `JSONRenderer`; every event is machine-parseable JSON |
| **Change Detection** | `store.py:upsert_drops`, `mark_removed_products` | `in_preview` → `removed` transition alerts; new product inserts trigger alert |
| **SQL Injection Prevention** | `store.py` all queries | SQLAlchemy ORM with bound parameters; no string-formatted SQL anywhere |
| **Dependency Pinning** | `requirements.txt` | All runtime deps pinned to exact versions (supply chain auditability) |
| **Graceful Degradation** | `alerting.py`, `scheduler.py:finally` | Alert failure never propagates; audit log always written |
| **Secret-Free Source** | All `.py` files | Zero credentials or tokens hardcoded anywhere in source |

---
## Limitations

- **RSC parsing is fragile:** The regex-based extraction of Next.js RSC chunks will break silently if Supreme updates their frontend framework version. A schema validation step on the extracted JSON would make failures explicit.
- **Web UI has no authentication:** The Flask dashboard (`web_ui.py`) is intended for local use only and has no login or access control. It must never be exposed on a public or shared network.
- **CA bundle freshness:** `certifi` should be updated regularly, as it bundles Mozilla's CA certificate list which changes when authorities are added or revoked.

## Future Work

- Add Alembic for proper schema migrations (noted in code but not implemented)
- Add schema validation on parsed RSC JSON to catch upstream HTML structure changes early
- Add authentication to the web dashboard for safer demo environments
- Parameterize `TARGET_URL` via `.env` so season updates require no code edits

## Notes

**Image URLs:** Product images are served from Sanity CDN (`cdn.sanity.io`). The scraper stores the Sanity asset hash in `image_url`. To construct the full CDN URL, you need the project ID from the Supreme JS bundle — this is documented here rather than hardcoded, as it may change between seasons.

**Schema migrations:** This prototype uses `SQLAlchemy create_all()`. For production, add Alembic and generate migration scripts. The existing `drops` and `scrape_log` table definitions are the starting point.

**Season updates:** When Supreme releases the next season's preview, update `TARGET_URL` and `PRODUCT_URL_TEMPLATE` in `config.py` and `parser.py`. No other code changes are needed.
