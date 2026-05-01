"""
web_ui.py — Flask dashboard for the Supreme drop scraper.

Routes:
  GET  /                     Dashboard (products + audit log + demo panel)
  POST /scrape               Trigger a live scrape cycle; returns JSON
  POST /demo/seed            Clear demo data, seed 5 synthetic products; returns JSON
  POST /demo/run             Run a simulated scrape with a custom scenario; returns JSON

Run:
  python web_ui.py
  open http://localhost:5000
"""

from __future__ import annotations

import asyncio
import json
import re
import traceback
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string, request
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from supreme_scraper.config import settings
from supreme_scraper.logging_config import configure_logging
from supreme_scraper.models import Drop, ScrapeLog
from supreme_scraper.scheduler import scrape_job

_sync_url = settings.DATABASE_URL.replace("sqlite+aiosqlite://", "sqlite://")
_engine = create_engine(_sync_url, echo=False)

_DEMO_SOURCE = "demo"

# Five representative Supreme-style products used for the offline demo.
_DEMO_PRODUCTS_BASE: list[dict] = [
    {
        "_id": "demo-001-sweat",
        "slug": "demo-box-logo-hooded-sweatshirt",
        "title": "Box Logo Hooded Sweatshirt",
        "category": {"slug": "sweatshirts", "title": "Sweatshirts"},
        "season": {"slug": "springsummer2026", "title": "Spring/Summer 2026 Preview"},
        "variants": [{"title": "White/Black", "images": [{"asset": {"assetId": "abc001"}}]}],
    },
    {
        "_id": "demo-002-tee",
        "slug": "demo-metallic-box-logo-tee",
        "title": "Metallic Box Logo Tee",
        "category": {"slug": "t-shirts", "title": "T-Shirts"},
        "season": {"slug": "springsummer2026", "title": "Spring/Summer 2026 Preview"},
        "variants": [{"title": "Black", "images": [{"asset": {"assetId": "def002"}}]}],
    },
    {
        "_id": "demo-003-jacket",
        "slug": "demo-denim-work-jacket",
        "title": "Denim Work Jacket",
        "category": {"slug": "jackets", "title": "Jackets"},
        "season": {"slug": "springsummer2026", "title": "Spring/Summer 2026 Preview"},
        "variants": [{"title": "Indigo", "images": [{"asset": {"assetId": "ghi003"}}]}],
    },
    {
        "_id": "demo-004-pant",
        "slug": "demo-ripstop-cargo-pant",
        "title": "Ripstop Cargo Pant",
        "category": {"slug": "pants", "title": "Pants"},
        "season": {"slug": "springsummer2026", "title": "Spring/Summer 2026 Preview"},
        "variants": [{"title": "Olive", "images": [{"asset": {"assetId": "jkl004"}}]}],
    },
    {
        "_id": "demo-005-cap",
        "slug": "demo-cotton-twill-camp-cap",
        "title": "Cotton Twill Camp Cap",
        "category": {"slug": "hats", "title": "Hats"},
        "season": {"slug": "springsummer2026", "title": "Spring/Summer 2026 Preview"},
        "variants": [{"title": "Red", "images": [{"asset": {"assetId": "mno005"}}]}],
    },
]


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _db() -> Session:
    return Session(_engine)


def _build_rsc_html(products: list[dict]) -> str:
    """
    Generate minimal Next.js RSC-style HTML that our parser can consume.

    The parser expects self.__next_f.push([1,"CHUNK"]) where CHUNK is a
    JSON-encoded string (double-escaped). json.dumps()[1:-1] produces the
    correctly escaped chunk content without the surrounding quotes.
    """
    payload_str = f'"products":{json.dumps(products)}'
    chunk = json.dumps(payload_str)[1:-1]
    return (
        "<html><body>"
        '<script>self.__next_f.push([0])</script>'
        f'<script>self.__next_f.push([1,"{chunk}"])</script>'
        "</body></html>"
    )


def _clear_demo_data_sync() -> None:
    """Delete all demo-source rows from the drops table."""
    with _db() as session:
        session.execute(delete(Drop).where(Drop.source_website == _DEMO_SOURCE))
        session.commit()


async def _run_demo_pipeline(products_data: list[dict]) -> dict:
    """
    Push a synthetic product list through the full parse → upsert →
    change-detection → audit-log pipeline, scoped to source_website='demo'.
    """
    from supreme_scraper.database import AsyncSessionFactory, init_db
    from supreme_scraper.parser import parse_preview_page
    from supreme_scraper.store import append_scrape_log, mark_removed_products, upsert_drops

    await init_db()

    html = _build_rsc_html(products_data)
    records = parse_preview_page(html, "demo://synthetic")

    # Isolate demo data from live-scrape data.
    for r in records:
        r["source_website"] = _DEMO_SOURCE

    seen_urls = {r["product_url"] for r in records}

    async with AsyncSessionFactory() as session:
        upserted, status_changes = await upsert_drops(session, records)
        removed_changes = await mark_removed_products(session, seen_urls, _DEMO_SOURCE)
        all_changes = status_changes + removed_changes
        await append_scrape_log(
            session,
            url="demo://synthetic",
            status_code=200,
            scraped_at=datetime.now(timezone.utc),
            duration_ms=0,
            records_upserted=upserted,
            error=None,
        )

    return {
        "upserted": upserted,
        "removed": sum(1 for c in all_changes if c.new_status == "removed"),
        "total_changes": len(all_changes),
    }


# ------------------------------------------------------------------ #
# App                                                                  #
# ------------------------------------------------------------------ #

app = Flask(__name__)

# ------------------------------------------------------------------ #
# Template                                                             #
# ------------------------------------------------------------------ #

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Supreme Drop Scraper</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet"/>
  <style>
    /* ── Tokens ───────────────────────────────────────────────── */
    :root {
      --red:       #E8272A;
      --red-hover: #c0211e;
      --red-bg:    #fef2f2;
      --green:     #16a34a;
      --green-bg:  #f0fdf4;
      --blue:      #1d4ed8;
      --blue-bg:   #eff6ff;
      --muted:     #6b7280;
      --border:    #e5e7eb;
      --surface:   #ffffff;
      --page-bg:   #f3f4f6;
      --text:      #111827;
      --mono:      'SF Mono','Fira Code','Menlo',monospace;
    }

    /* ── Base ─────────────────────────────────────────────────── */
    body {
      background: var(--page-bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font-size: 14px;
    }

    /* ── Nav ──────────────────────────────────────────────────── */
    .top-bar {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 0 24px;
      height: 56px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .brand { display: flex; align-items: center; gap: 12px; }
    .sup-logo {
      background: var(--red);
      color: #fff;
      font-family: Arial Black, Arial, sans-serif;
      font-weight: 900;
      font-size: 13px;
      letter-spacing: 1.5px;
      text-transform: uppercase;
      padding: 5px 10px 4px;
      line-height: 1;
      user-select: none;
    }
    .brand-sub { font-size: 13px; color: var(--muted); font-weight: 400; }
    .nav-right { display: flex; align-items: center; gap: 16px; }
    #scrape-msg { font-size: 13px; color: var(--muted); }
    .btn-scrape {
      background: var(--red);
      color: #fff;
      border: none;
      padding: 7px 18px;
      font-size: 13px;
      font-weight: 600;
      border-radius: 6px;
      cursor: pointer;
      transition: background .15s;
    }
    .btn-scrape:hover:not(:disabled) { background: var(--red-hover); }
    .btn-scrape:disabled { opacity: .55; cursor: default; }

    /* ── Layout ───────────────────────────────────────────────── */
    .page { max-width: 1400px; margin: 0 auto; padding: 28px 24px 64px; }

    /* ── Stat cards ───────────────────────────────────────────── */
    .stats { display: grid; grid-template-columns: repeat(4,1fr); gap: 16px; margin-bottom: 32px; }
    .stat {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 20px 22px;
    }
    .stat-value { font-size: 28px; font-weight: 700; line-height: 1; margin-bottom: 4px; }
    .stat-label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }
    .stat-value.green { color: var(--green); }
    .stat-value.red   { color: var(--red);   }

    /* ── Section header ───────────────────────────────────────── */
    .section-head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
    .section-label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); }
    .section-head input,
    .section-head select {
      font-size: 13px; border: 1px solid var(--border); border-radius: 6px;
      padding: 5px 10px; outline: none; background: var(--surface);
    }
    .section-head input:focus,
    .section-head select:focus { border-color: #9ca3af; }

    /* ── Card / Table ─────────────────────────────────────────── */
    .card-table {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
      margin-bottom: 32px;
    }
    table { width: 100%; border-collapse: collapse; }
    thead th {
      font-size: 11px; font-weight: 600; text-transform: uppercase;
      letter-spacing: .07em; color: var(--muted); padding: 10px 14px;
      border-bottom: 1px solid var(--border); background: #fafafa; white-space: nowrap;
    }
    tbody tr { border-bottom: 1px solid #f3f4f6; transition: background .1s; }
    tbody tr:last-child { border-bottom: none; }
    tbody tr:hover { background: #fafafa; }
    tbody tr.is-removed { background: var(--red-bg); }
    tbody tr.is-removed td { opacity: .6; }
    tbody tr.is-removed td.cell-name a { text-decoration: line-through; }
    td { padding: 10px 14px; vertical-align: middle; }

    .cell-name { max-width: 260px; }
    .cell-name a {
      color: var(--text); text-decoration: none; font-weight: 500;
      display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .cell-name a:hover { color: var(--red); }
    .cell-mono {
      font-family: var(--mono); font-size: 11px; color: var(--muted);
      max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .cell-muted { color: var(--muted); }

    /* Pills */
    .pill {
      display: inline-block; font-size: 11px; font-weight: 600;
      padding: 2px 9px; border-radius: 99px; text-transform: uppercase; letter-spacing: .04em;
    }
    .pill-in_preview { background: var(--green-bg); color: var(--green); }
    .pill-removed    { background: var(--red-bg);   color: var(--red);   }
    .pill-unknown    { background: #f3f4f6;          color: var(--muted); }
    .badge-demo {
      font-size: 10px; background: var(--blue-bg); color: var(--blue);
      padding: 1px 6px; border-radius: 99px; font-weight: 600; margin-left: 4px;
    }

    /* Empty state */
    .empty-row td { padding: 40px 14px; text-align: center; color: var(--muted); }

    /* Audit log */
    .log-table td    { padding: 8px 14px; font-size: 13px; }
    .log-table thead th { padding: 8px 14px; }
    .ok  { color: var(--green); font-weight: 600; }
    .err { color: var(--red);   font-weight: 600; }

    /* ── Demo Panel ───────────────────────────────────────────── */
    .demo-panel {
      background: #f8f9ff;
      border: 1px solid #dde1f0;
      border-radius: 10px;
      padding: 20px 24px 24px;
      margin-bottom: 32px;
    }
    .demo-panel-head {
      display: flex;
      align-items: baseline;
      gap: 10px;
      margin-bottom: 16px;
    }
    .demo-panel-note { font-size: 12px; color: var(--muted); }
    .demo-steps {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 12px;
    }
    .demo-step {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
      display: flex;
      gap: 12px;
      align-items: flex-start;
    }
    .step-num {
      width: 22px; height: 22px; border-radius: 50%;
      background: var(--blue); color: #fff;
      font-size: 11px; font-weight: 700;
      display: flex; align-items: center; justify-content: center;
      flex-shrink: 0; margin-top: 1px;
    }
    .step-body { flex: 1; min-width: 0; }
    .step-title { font-size: 13px; font-weight: 600; margin-bottom: 4px; }
    .step-desc  { font-size: 12px; color: var(--muted); margin-bottom: 10px; line-height: 1.5; }
    .btn-demo {
      background: var(--blue); color: #fff; border: none;
      padding: 6px 14px; border-radius: 6px;
      font-size: 12px; font-weight: 600;
      cursor: pointer; transition: background .15s;
    }
    .btn-demo:hover:not(:disabled) { background: #1e40af; }
    .btn-demo:disabled { opacity: .55; cursor: default; }
    .demo-msg { font-size: 12px; margin-left: 8px; }

    /* Product checkboxes inside scenario editor */
    .demo-product-list { margin-bottom: 10px; }
    .demo-product-empty { font-size: 12px; color: var(--muted); font-style: italic; padding: 6px 0; }
    .dp-row {
      display: flex; align-items: center; gap: 8px;
      padding: 4px 0; font-size: 12px; cursor: pointer;
      user-select: none;
    }
    .dp-row input[type=checkbox] { flex-shrink: 0; cursor: pointer; }
    .dp-name { font-weight: 500; transition: color .1s, text-decoration .1s; }
    .dp-cat  { color: var(--muted); font-size: 11px; }
    .dp-row.will-remove .dp-name {
      text-decoration: line-through; color: var(--muted);
    }
    .tag-remove {
      font-size: 10px; background: var(--red-bg); color: var(--red);
      padding: 1px 6px; border-radius: 99px; font-weight: 600; white-space: nowrap;
    }
    .tag-new {
      font-size: 10px; background: var(--green-bg); color: var(--green);
      padding: 1px 6px; border-radius: 99px; font-weight: 600;
    }

    /* Add-product form */
    .add-form {
      display: flex; align-items: center; gap: 6px;
      margin-top: 8px; padding-top: 10px;
      border-top: 1px solid var(--border); flex-wrap: wrap;
    }
    .add-form-label { font-size: 11px; color: var(--muted); font-weight: 600; white-space: nowrap; }
    .add-form input {
      font-size: 12px; border: 1px solid var(--border); border-radius: 5px;
      padding: 4px 8px; width: 150px; outline: none;
    }
    .add-form input:focus { border-color: var(--blue); }
    .add-form select {
      font-size: 12px; border: 1px solid var(--border); border-radius: 5px;
      padding: 4px 8px; outline: none;
    }
    .btn-add {
      background: var(--surface); border: 1px solid var(--border);
      padding: 4px 10px; border-radius: 5px; font-size: 12px;
      font-weight: 600; cursor: pointer; color: var(--blue);
    }
    .btn-add:hover { border-color: var(--blue); }
    .pending-add {
      display: flex; align-items: center; gap: 6px;
      font-size: 12px; padding: 2px 0;
    }
    .btn-xremove {
      background: none; border: none; color: var(--muted);
      cursor: pointer; font-size: 14px; line-height: 1; padding: 0 2px;
    }
    .btn-xremove:hover { color: var(--red); }

    @media (max-width: 900px) {
      .stats        { grid-template-columns: repeat(2,1fr); }
      .demo-steps   { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>

<!-- Top bar -->
<header class="top-bar">
  <div class="brand">
    <span class="sup-logo">Supreme</span>
    <span class="brand-sub">Drop Scraper &mdash; Dashboard</span>
  </div>
  <div class="nav-right">
    <span id="scrape-msg"></span>
    <button class="btn-scrape" id="scrape-btn" onclick="runScrape()">Run Live Scrape</button>
  </div>
</header>

<main class="page">

  <!-- Stats -->
  <div class="stats">
    <div class="stat">
      <div class="stat-value" id="count-total">{{ stats.total }}</div>
      <div class="stat-label">Total Products</div>
    </div>
    <div class="stat">
      <div class="stat-value green" id="count-in-preview">{{ stats.in_preview }}</div>
      <div class="stat-label">In Preview</div>
    </div>
    <div class="stat">
      <div class="stat-value red" id="count-removed">{{ stats.removed }}</div>
      <div class="stat-label">Removed</div>
    </div>
    <div class="stat">
      <div class="stat-value" style="font-size:16px;padding-top:4px">
        {{ stats.last_scrape or "&mdash;" }}
      </div>
      <div class="stat-label">Last Scrape (UTC)</div>
    </div>
  </div>

  <!-- Demo Panel -->
  <div class="demo-panel">
    <div class="demo-panel-head">
      <span class="section-label">Demo Mode &mdash; Change Detection Walkthrough</span>
      <span class="demo-panel-note">Runs the full pipeline offline &mdash; no network required</span>
    </div>
    <div class="demo-steps">

      <!-- Step 1: Seed -->
      <div class="demo-step">
        <div class="step-num">1</div>
        <div class="step-body">
          <div class="step-title">Seed Preview Data</div>
          <div class="step-desc">
            Loads 5 synthetic products as
            <span class="pill pill-in_preview" style="font-size:10px;padding:1px 6px">in_preview</span>,
            simulating an initial scrape. Clears any existing demo data.
          </div>
          <button class="btn-demo" onclick="seedDemo(event)">Reset &amp; Seed Demo</button>
          <span id="seed-msg" class="demo-msg"></span>
        </div>
      </div>

      <!-- Step 2: Configure -->
      <div class="demo-step">
        <div class="step-num">2</div>
        <div class="step-body">
          <div class="step-title">Configure Next Scrape Cycle</div>
          <div class="step-desc">
            Check products to <strong>remove</strong> (they vanish from Supreme&rsquo;s page)
            and optionally <strong>add</strong> a new product that just appeared.
          </div>

          <div class="demo-product-list" id="dp-list">
            <div class="demo-product-empty" id="dp-empty">Seed demo data first.</div>
          </div>

          <div class="add-form">
            <span class="add-form-label">Add new:</span>
            <input type="text" id="new-name" placeholder="Product name" maxlength="100"/>
            <select id="new-cat">
              <option value="">Category</option>
              <option>T-Shirts</option>
              <option>Sweatshirts</option>
              <option>Jackets</option>
              <option>Pants</option>
              <option>Hats</option>
              <option>Accessories</option>
              <option>Shoes</option>
              <option>Bags</option>
            </select>
            <button class="btn-add" onclick="addPending()">+ Add</button>
          </div>
          <div id="pending-adds" style="margin-top:6px"></div>
        </div>
      </div>

      <!-- Step 3: Run -->
      <div class="demo-step">
        <div class="step-num">3</div>
        <div class="step-body">
          <div class="step-title">Run Simulated Scrape</div>
          <div class="step-desc">
            Generates synthetic HTML from your scenario and runs it through the
            full pipeline: parse &rarr; upsert &rarr; <code>mark_removed_products()</code>
            &rarr; audit log. Status transitions are real &mdash; not mocked.
          </div>
          <button class="btn-demo" id="demo-run-btn" onclick="runDemoScrape(event)">
            Run Simulated Scrape
          </button>
          <span id="demo-result-msg" class="demo-msg"></span>
        </div>
      </div>

    </div>
  </div>

  <!-- Products table -->
  <div class="section-head">
    <span class="section-label">Products</span>
    <input id="q" type="search" placeholder="Search name or category" oninput="filterTable()"/>
    <select id="s" onchange="filterTable()">
      <option value="">All statuses</option>
      <option value="in_preview">In Preview</option>
      <option value="removed">Removed</option>
    </select>
    <select id="src" onchange="filterTable()">
      <option value="">All sources</option>
      <option value="demo">Demo only</option>
      <option value="supreme.com">Live only</option>
    </select>
  </div>

  <div class="card-table">
    <table id="products-table">
      <thead>
        <tr>
          <th>Product</th>
          <th>Category</th>
          <th>Variant</th>
          <th>Sanity ID</th>
          <th>Status</th>
          <th>Season</th>
          <th>Last Seen (UTC)</th>
        </tr>
      </thead>
      <tbody>
        {% for p in products %}
        <tr id="row-{{ p.id }}"
            class="{{ 'is-removed' if p.stock_status == 'removed' else '' }}"
            data-name="{{ p.product_name|lower }}"
            data-category="{{ (p.colorway or '')|lower }}"
            data-status="{{ p.stock_status }}"
            data-source="{{ p.source_website }}">
          <td class="cell-name">
            <a href="{{ p.product_url }}" target="_blank" title="{{ p.product_name }}">
              {{ p.product_name }}
            </a>
            {% if p.source_website == 'demo' %}
            <span class="badge-demo">demo</span>
            {% endif %}
          </td>
          <td class="cell-muted">{{ p.colorway or "&mdash;" }}</td>
          <td class="cell-muted">{{ p.notes or "&mdash;" }}</td>
          <td class="cell-mono">{{ p.sku or "&mdash;" }}</td>
          <td>
            <span class="pill pill-{{ p.stock_status }}" id="pill-{{ p.id }}">
              {{ p.stock_status }}
            </span>
          </td>
          <td class="cell-muted">{{ p.drop_date or "&mdash;" }}</td>
          <td class="cell-muted">
            {{ p.scrape_timestamp.strftime("%Y-%m-%d %H:%M") if p.scrape_timestamp else "&mdash;" }}
          </td>
        </tr>
        {% else %}
        <tr class="empty-row">
          <td colspan="7">No products yet &mdash; click <strong>Run Live Scrape</strong> or <strong>Reset &amp; Seed Demo</strong>.</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <!-- Audit log -->
  <div class="section-head">
    <span class="section-label">Audit Log &mdash; scrape_log</span>
  </div>
  <div class="card-table">
    <table class="log-table">
      <thead>
        <tr>
          <th>#</th>
          <th>Timestamp (UTC)</th>
          <th>Target URL</th>
          <th>HTTP</th>
          <th>Duration</th>
          <th>Upserted</th>
          <th>Error</th>
        </tr>
      </thead>
      <tbody>
        {% for log in scrape_logs %}
        <tr>
          <td class="cell-muted">{{ log.id }}</td>
          <td class="cell-muted">
            {{ log.scraped_at.strftime("%Y-%m-%d %H:%M:%S") if log.scraped_at else "&mdash;" }}
          </td>
          <td class="cell-mono" style="max-width:200px">{{ log.url }}</td>
          <td>
            {% if log.status_code == 200 %}
              <span class="ok">{{ log.status_code }}</span>
            {% elif log.status_code %}
              <span class="err">{{ log.status_code }}</span>
            {% else %}
              <span class="cell-muted">&mdash;</span>
            {% endif %}
          </td>
          <td class="cell-muted">{{ (log.duration_ms|string + " ms") if log.duration_ms else "&mdash;" }}</td>
          <td>{{ log.records_upserted }}</td>
          <td class="err" style="font-size:12px;max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
            {{ log.error or "" }}
          </td>
        </tr>
        {% else %}
        <tr class="empty-row">
          <td colspan="7">No scrape runs yet.</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

</main>

<script>
/* ── State ───────────────────────────────────────────────────────── */
// Initialised from server-rendered data so scenario editor survives reloads.
let demoProducts = {{ demo_products | tojson }};
let removeIds    = new Set();
let pendingAdds  = [];          // [{name, category}]

/* ── Table filter ────────────────────────────────────────────────── */
function filterTable() {
  const q   = document.getElementById('q').value.toLowerCase();
  const s   = document.getElementById('s').value;
  const src = document.getElementById('src').value;
  document.querySelectorAll('#products-table tbody tr[id^="row-"]').forEach(row => {
    const matchQ   = !q   || row.dataset.name.includes(q) || row.dataset.category.includes(q);
    const matchS   = !s   || row.dataset.status === s;
    const matchSrc = !src || row.dataset.source === src;
    row.style.display = (matchQ && matchS && matchSrc) ? '' : 'none';
  });
}

/* ── Live scrape ─────────────────────────────────────────────────── */
async function runScrape() {
  const btn = document.getElementById('scrape-btn');
  const msg = document.getElementById('scrape-msg');
  btn.disabled = true;
  btn.textContent = 'Scraping...';
  msg.textContent = '';
  try {
    const res  = await fetch('/scrape', { method: 'POST' });
    const data = await res.json();
    if (data.error) {
      msg.textContent = 'Scrape failed &mdash; see console';
      msg.style.color = 'var(--red)';
      console.error(data.error);
    } else {
      msg.textContent = data.upserted + ' upserted, ' + data.changes + ' changes detected';
      msg.style.color = 'var(--green)';
      setTimeout(() => location.reload(), 900);
    }
  } catch {
    msg.textContent = 'Request failed';
    msg.style.color = 'var(--red)';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Run Live Scrape';
  }
}

/* ── Demo: render helpers ────────────────────────────────────────── */
function renderDemoProducts() {
  const list  = document.getElementById('dp-list');
  const empty = document.getElementById('dp-empty');

  if (!demoProducts.length) {
    list.innerHTML = '<div class="demo-product-empty" id="dp-empty">Seed demo data first.</div>';
    return;
  }

  list.innerHTML = demoProducts.map(p => {
    const will = removeIds.has(p.id);
    return `<label class="dp-row${will ? ' will-remove' : ''}">
      <input type="checkbox" ${will ? 'checked' : ''}
             onchange="toggleRemove(${p.id}, this.checked)"/>
      <span class="dp-name">${p.name}</span>
      <span class="dp-cat">${p.category}</span>
      ${will ? '<span class="tag-remove">will be removed</span>' : ''}
    </label>`;
  }).join('');
}

function renderPendingAdds() {
  const el = document.getElementById('pending-adds');
  el.innerHTML = pendingAdds.map((p, i) => `
    <div class="pending-add">
      <span class="tag-new">new</span>
      <span>${p.name}</span>
      <span class="dp-cat">${p.category}</span>
      <button class="btn-xremove" onclick="removePending(${i})" title="Cancel">&times;</button>
    </div>`).join('');
}

/* ── Demo: seed ──────────────────────────────────────────────────── */
async function seedDemo(event) {
  const btn = event.currentTarget;
  const msg = document.getElementById('seed-msg');
  btn.disabled = true;
  btn.textContent = 'Seeding...';
  msg.textContent = '';
  msg.style.color  = 'var(--muted)';
  try {
    const res  = await fetch('/demo/seed', { method: 'POST' });
    const data = await res.json();
    if (data.error) {
      msg.textContent = 'Failed &mdash; see console';
      msg.style.color  = 'var(--red)';
      console.error(data.error);
      return;
    }
    demoProducts = data.products || [];
    removeIds    = new Set();
    pendingAdds  = [];
    renderDemoProducts();
    renderPendingAdds();
    msg.textContent = `${data.upserted} products loaded`;
    msg.style.color  = 'var(--green)';
    setTimeout(() => location.reload(), 1000);
  } catch {
    msg.textContent = 'Request failed';
    msg.style.color  = 'var(--red)';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Reset & Seed Demo';
  }
}

/* ── Demo: scenario controls ─────────────────────────────────────── */
function toggleRemove(id, checked) {
  if (checked) removeIds.add(id);
  else         removeIds.delete(id);
  renderDemoProducts();
}

function addPending() {
  const name = document.getElementById('new-name').value.trim();
  const cat  = document.getElementById('new-cat').value;
  if (!name || pendingAdds.length >= 3) return;
  pendingAdds.push({ name, category: cat });
  document.getElementById('new-name').value = '';
  renderPendingAdds();
}

function removePending(i) {
  pendingAdds.splice(i, 1);
  renderPendingAdds();
}

/* ── Demo: run simulated scrape ──────────────────────────────────── */
async function runDemoScrape(event) {
  const btn = event.currentTarget;
  const msg = document.getElementById('demo-result-msg');
  btn.disabled    = true;
  btn.textContent = 'Running...';
  msg.textContent = '';
  try {
    const res  = await fetch('/demo/run', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ remove_ids: [...removeIds], add: pendingAdds }),
    });
    const data = await res.json();
    if (data.error) {
      msg.textContent = 'Run failed &mdash; see console';
      msg.style.color  = 'var(--red)';
      console.error(data.error);
      return;
    }
    const parts = [];
    if (data.removed) parts.push(`${data.removed} product${data.removed > 1 ? 's' : ''} removed (change detected)`);
    if (data.added)   parts.push(`${data.added} new product${data.added > 1 ? 's' : ''} added`);
    if (!parts.length) parts.push(`${data.upserted} records re-upserted, no status changes`);
    msg.textContent = parts.join(' &mdash; ');
    msg.style.color  = (data.removed || data.added) ? 'var(--green)' : 'var(--muted)';
    setTimeout(() => location.reload(), 1200);
  } catch {
    msg.textContent = 'Request failed';
    msg.style.color  = 'var(--red)';
  } finally {
    btn.disabled    = false;
    btn.textContent = 'Run Simulated Scrape';
  }
}

/* ── Init ────────────────────────────────────────────────────────── */
renderDemoProducts();
renderPendingAdds();
</script>
</body>
</html>"""


# ------------------------------------------------------------------ #
# Routes                                                               #
# ------------------------------------------------------------------ #


@app.get("/")
def dashboard():
    with _db() as session:
        products = list(
            session.execute(
                select(Drop).order_by(Drop.stock_status, Drop.product_name)
            ).scalars()
        )
        total = session.execute(select(func.count()).select_from(Drop)).scalar() or 0
        in_preview = session.execute(
            select(func.count()).select_from(Drop).where(Drop.stock_status == "in_preview")
        ).scalar() or 0
        removed = session.execute(
            select(func.count()).select_from(Drop).where(Drop.stock_status == "removed")
        ).scalar() or 0
        last_log = session.execute(
            select(ScrapeLog).order_by(ScrapeLog.id.desc()).limit(1)
        ).scalars().first()
        logs = list(
            session.execute(
                select(ScrapeLog).order_by(ScrapeLog.id.desc()).limit(20)
            ).scalars()
        )
        demo_prods = list(
            session.execute(
                select(Drop)
                .where(Drop.source_website == _DEMO_SOURCE, Drop.stock_status != "removed")
                .order_by(Drop.product_name)
            ).scalars()
        )

    last_scrape = (
        last_log.scraped_at.strftime("%Y-%m-%d %H:%M")
        if last_log and last_log.scraped_at else None
    )

    return render_template_string(
        _TEMPLATE,
        products=products,
        stats=dict(total=total, in_preview=in_preview, removed=removed, last_scrape=last_scrape),
        scrape_logs=logs,
        demo_products=[
            {"id": p.id, "name": p.product_name, "category": p.colorway or ""}
            for p in demo_prods
        ],
    )


@app.post("/scrape")
def trigger_scrape():
    try:
        asyncio.run(scrape_job())
        with _db() as session:
            last_log = session.execute(
                select(ScrapeLog).order_by(ScrapeLog.id.desc()).limit(1)
            ).scalars().first()
            removed = session.execute(
                select(func.count()).select_from(Drop).where(Drop.stock_status == "removed")
            ).scalar() or 0
        return jsonify({
            "status":   "ok",
            "upserted": last_log.records_upserted if last_log else 0,
            "changes":  removed,
            "error":    last_log.error if last_log else None,
        })
    except Exception:
        return jsonify({"error": traceback.format_exc(limit=3)}), 500


@app.post("/demo/seed")
def demo_seed():
    """Clear demo rows and seed 5 synthetic products through the full pipeline."""
    _clear_demo_data_sync()
    try:
        result = asyncio.run(_run_demo_pipeline(_DEMO_PRODUCTS_BASE))
    except Exception:
        return jsonify({"error": traceback.format_exc(limit=3)}), 500

    with _db() as session:
        demo_prods = list(
            session.execute(
                select(Drop)
                .where(Drop.source_website == _DEMO_SOURCE)
                .order_by(Drop.product_name)
            ).scalars()
        )

    return jsonify({
        **result,
        "products": [
            {"id": p.id, "name": p.product_name, "category": p.colorway or ""}
            for p in demo_prods
        ],
    })


@app.post("/demo/run")
def demo_run():
    """
    Run a simulated scrape with a custom scenario.

    Body (JSON):
      remove_ids: list[int]   — IDs of demo products to drop from the run
      add:        list[{name, category}]  — new products to inject
    """
    body = request.get_json(force=True) or {}
    remove_ids = {int(x) for x in body.get("remove_ids", []) if str(x).lstrip("-").isdigit()}
    new_items = body.get("add", [])[:3]  # cap at 3 to prevent abuse

    with _db() as session:
        current = list(
            session.execute(
                select(Drop).where(
                    Drop.source_website == _DEMO_SOURCE,
                    Drop.stock_status != "removed",
                )
            ).scalars()
        )
        existing_urls = {p.product_url for p in current}

    # Rebuild product dicts for products NOT being removed.
    products_for_run: list[dict] = []
    for p in current:
        if p.id not in remove_ids:
            # Extract slug from the stored product_url.
            slug = p.product_url.rstrip("/").rsplit("/", 1)[-1]
            products_for_run.append({
                "_id": p.sku or f"demo-{p.id}",
                "slug": slug,
                "title": p.product_name,
                "category": {"slug": "", "title": p.colorway or ""},
                "season": {
                    "slug": "springsummer2026",
                    "title": p.drop_date or "Spring/Summer 2026 Preview",
                },
                "variants": [{"title": p.notes or "", "images": []}],
            })

    # Inject new products.
    for item in new_items:
        name = str(item.get("name", ""))[:100].strip()
        cat = str(item.get("category", ""))[:50].strip()
        if not name:
            continue
        slug_base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        uid = uuid.uuid4().hex[:8]
        products_for_run.append({
            "_id": f"demo-new-{uid}",
            "slug": f"demo-{slug_base}-{uid}",
            "title": name,
            "category": {"slug": "", "title": cat},
            "season": {"slug": "springsummer2026", "title": "Spring/Summer 2026 Preview"},
            "variants": [{"title": "New Drop", "images": []}],
        })

    try:
        result = asyncio.run(_run_demo_pipeline(products_for_run))
    except Exception:
        return jsonify({"error": traceback.format_exc(limit=3)}), 500

    # Count genuinely new products (not previously in DB).
    with _db() as session:
        all_demo_urls = {
            row[0] for row in session.execute(
                select(Drop.product_url).where(Drop.source_website == _DEMO_SOURCE)
            )
        }
    added = len(all_demo_urls - existing_urls)

    return jsonify({**result, "added": added})


# ------------------------------------------------------------------ #
# Entry point                                                          #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    configure_logging()
    from supreme_scraper.database import init_db
    asyncio.run(init_db())
    print("Dashboard → http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
