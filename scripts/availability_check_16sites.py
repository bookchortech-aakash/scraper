#!/usr/bin/env python3
"""
availability_check_16sites.py

Standalone, one-off availability/price checker for a fixed list of ~8137 known
book URLs across 16 second-hand/regional bookstore sites, for a real purchase
order (Bookchor). This is NOT a catalogue crawler and does not touch the
`records` DB table used by the other per-site crawlers in this project.

Reads:  AVAIL_INPUT_CSV  (default /app/scripts/availability_check_input.csv)
        columns: isbn, title, author, qty, site, existing_availability, url
Writes: AVAIL_OUTPUT_CSV (default /app/scripts/availability_results.csv)
        columns: isbn, title, site, url_checked, current_availability,
                 stock_qty_if_known, current_price, existing_availability_before,
                 status_note, checked_at
Logs:   /app/scripts/availability_check.log  (also printed to stdout)

Design notes (see status_note on individual rows for specifics):
  - Every network call goes through fetch() which retries a BOUNDED number of
    times (default 3) with short backoff, then gives up and the row is
    recorded as "Unable to verify" with a reason. No infinite retry loops.
  - Results are written to the output CSV incrementally (flushed after every
    row), so partial progress survives a crash. Re-running the script skips
    (isbn, site) pairs already present in the output file.
  - akshardhara / ritikart / darussalam are all Shopify stores. Rather than
    trusting the possibly-stale per-row "url" (a bare slug for akshardhara/
    ritikart, sometimes absent/wrong for darussalam — confirmed during
    investigation that some captured darussalam/prajaktprakashan URLs point to
    the WRONG product), we bulk-fetch each site's public products.json
    catalogue once (paginated) and match by ISBN (akshardhara/ritikart expose
    ISBN in variants[0].barcode) or, for darussalam (no barcode field
    populated), by fuzzy title match with the match confidence recorded in
    status_note.
  - prajaktprakashan's captured URLs are WooCommerce "add-to-cart=<id>" links
    that were confirmed (by hand, during investigation) to sometimes point at
    an unrelated product left over from crawl-time state, not the target
    book. We resolve the WordPress post ID from the URL, fetch that product,
    and only accept the result if its SKU/ISBN matches the input ISBN;
    otherwise the row is marked Unable to verify rather than reporting data
    for the wrong book.
"""

import csv
import datetime as dt
import json
import os
import re
import sys
import time
import difflib
from collections import defaultdict

import requests

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

UA = os.environ.get(
    "SCRAPER_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)

INPUT_CSV = os.environ.get("AVAIL_INPUT_CSV", "/app/scripts/availability_check_input.csv")
OUTPUT_CSV = os.environ.get("AVAIL_OUTPUT_CSV", "/app/scripts/availability_results.csv")
LOG_FILE = os.environ.get("AVAIL_LOG_FILE", "/app/scripts/availability_check.log")

REQUEST_TIMEOUT = 20
MAX_RETRIES = 3

DELAYS = {
    "commonfolks": 0.3,
    "exoticindia": 0.3,
    "panuval": 0.3,
    "bookganga": 0.3,
    "dcbooks": 0.3,
    "champaca": 0.2,
    "akshardhara": 0.2,
    "ritikart": 0.2,
    "boighar": 0.3,
    "kairalibooks": 0.3,
    "darussalam": 0.2,
    "rajhansprakashan": 0.3,
    "padmagandha": 0.3,
    "prajaktprakashan": 0.3,
    "idara": 0.3,
    "aitbspublishers": 0.3,
}

FIELDNAMES = [
    "isbn", "title", "site", "url_checked", "current_availability",
    "stock_qty_if_known", "current_price", "existing_availability_before",
    "status_note", "checked_at",
]

# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------


def log(msg):
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fetch(url, session=None, timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES, headers=None):
    """Bounded-retry GET. Returns (response, None) or (None, error_string)."""
    hdrs = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        hdrs.update(headers)
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            getter = session.get if session is not None else requests.get
            resp = getter(url, headers=hdrs, timeout=timeout)
            return resp, None
        except requests.exceptions.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < max_retries:
                time.sleep(1.5 * attempt)
    return None, last_err


def normalize_isbn(s):
    return re.sub(r"[^0-9Xx]", "", s or "").upper()


def normalize_title(t):
    t = (t or "").lower()
    t = re.sub(r"\[.*?\]|\(.*?\)", " ", t)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def format_price(price, currency=None):
    if price is None or price == "":
        return ""
    try:
        val = float(str(price).replace(",", "").strip())
        pricestr = f"{val:.2f}"
    except Exception:
        pricestr = str(price).strip()
    cur = currency or "INR"
    return f"{pricestr} {cur}"


def fail(reason, url=""):
    return {
        "url_checked": url,
        "current_availability": "Unable to verify",
        "stock_qty_if_known": "",
        "current_price": "",
        "status_note": reason,
    }


def ok_result(url_checked, availability, price="", currency=None, note="", qty=""):
    return {
        "url_checked": url_checked,
        "current_availability": availability,
        "stock_qty_if_known": qty,
        "current_price": format_price(price, currency) if price not in (None, "") else "",
        "status_note": note,
    }


# --------------------------------------------------------------------------
# Generic schema.org (microdata + JSON-LD) extractor
# used by: commonfolks, exoticindia, panuval, rajhansprakashan, and as a
# fallback for akshardhara / ritikart product pages.
# --------------------------------------------------------------------------

MICRODATA_AVAIL_RE = re.compile(
    r'itemprop="availability"[^>]*href="https?://schema\.org/(\w+)"', re.I
)
MICRODATA_PRICE_RE = re.compile(
    r'itemprop="price"[^>]*?(?:content="([\d][\d.,]*)"|>\s*([\d][\d.,]*)\s*<)', re.I
)
MICRODATA_CURRENCY_RE = re.compile(
    r'itemprop="priceCurrency"[^>]*content="(\w+)"', re.I
)
LDJSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S
)


def normalize_avail(word):
    if not word:
        return None
    w = word.lower()
    if "outofstock" in w or "soldout" in w or "discontinued" in w:
        return "Out of Stock"
    if "instock" in w or "limitedavailability" in w:
        return "In Stock" if "limited" not in w else "Limited Availability"
    if "preorder" in w:
        return "Pre-Order"
    if "backorder" in w:
        return "Back Order"
    return word


def extract_schema_offer(html):
    """Returns (availability_normalized_or_None, price_or_None, currency_or_None)."""
    avail = None
    price = None
    currency = None

    m = MICRODATA_AVAIL_RE.search(html)
    if m:
        avail = normalize_avail(m.group(1))
    mp = MICRODATA_PRICE_RE.search(html)
    if mp:
        price = (mp.group(1) or mp.group(2) or "").replace(",", "")
    mc = MICRODATA_CURRENCY_RE.search(html)
    if mc:
        currency = mc.group(1)

    if avail and price:
        return avail, price, currency

    # Fall back to JSON-LD Product/Offer blocks.
    for m2 in LDJSON_RE.finditer(html):
        blob = m2.group(1).strip()
        if '"Product"' not in blob and '"offers"' not in blob:
            continue
        data = None
        try:
            data = json.loads(blob)
        except Exception:
            data = None
        candidates = data if isinstance(data, list) else ([data] if data else [])
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            offers = obj.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict):
                a = offers.get("availability") or ""
                if a and not avail:
                    avail = normalize_avail(a.rstrip("/").rsplit("/", 1)[-1])
                p = offers.get("price")
                if p is not None and not price:
                    price = str(p)
                c = offers.get("priceCurrency")
                if c and not currency:
                    currency = c
        if avail or price:
            break

    if not avail or not price:
        # last-resort regex over raw text (handles malformed/escaped JSON-LD)
        if not avail:
            am = re.search(r'"availability"\s*:\s*"https?://schema\.org/(\w+)"', html)
            if am:
                avail = normalize_avail(am.group(1))
        if not price:
            pm = re.search(r'"offers"[^{}]{0,400}?"price"\s*:\s*"?([\d][\d.,]*)"?', html, re.S)
            if pm:
                price = pm.group(1)
        if not currency:
            cm = re.search(r'"priceCurrency"\s*:\s*"(\w+)"', html)
            if cm:
                currency = cm.group(1)

    return avail, price, currency


def handle_generic_schema(row):
    url = (row.get("url") or "").strip()
    if not url:
        return fail("no URL provided")
    resp, err = fetch(url)
    if err:
        return fail(f"fetch error: {err}", url)
    if resp.status_code == 404:
        return fail("HTTP 404 - page not found (book may have been delisted)", url)
    if resp.status_code != 200:
        return fail(f"HTTP {resp.status_code}", url)
    avail, price, currency = extract_schema_offer(resp.text)
    note = "" if avail else "no schema.org availability marker found on page"
    if not price:
        note = (note + "; " if note else "") + "price not found on page"
    return ok_result(resp.url, avail or "Unknown", price, currency, note)


# --------------------------------------------------------------------------
# Generic WooCommerce extractor
# used by: boighar, idara, kairalibooks, and (after ID resolution) prajaktprakashan
# --------------------------------------------------------------------------


def extract_woocommerce(html):
    avail = None
    price = None
    currency = "INR"

    m = re.search(r'<meta property="product:availability" content="([^"]+)"', html, re.I)
    if m:
        v = m.group(1).lower()
        if "instock" in v or v == "in stock":
            avail = "In Stock"
        elif "out" in v:
            avail = "Out of Stock"
        else:
            avail = m.group(1)
    pm = re.search(r'<meta property="product:price:amount" content="([^"]+)"', html, re.I)
    if pm:
        price = pm.group(1)
    cm = re.search(r'<meta property="product:price:currency" content="([^"]+)"', html, re.I)
    if cm:
        currency = cm.group(1)

    if avail and price:
        return avail, price, currency

    if BeautifulSoup is None:
        return avail, price, currency

    soup = BeautifulSoup(html, "html.parser")
    summary = soup.select_one("div.entry-summary, div.summary.entry-summary, div.product-summary")
    scope = summary or soup

    if not avail:
        stock_el = scope.select_one("p.stock, span.stock, .stock")
        if stock_el:
            t = stock_el.get_text(" ", strip=True).lower()
            if "out of stock" in t:
                avail = "Out of Stock"
            elif "in stock" in t:
                avail = "In Stock"
            elif "backorder" in t:
                avail = "Back Order"
        if not avail:
            if scope.select_one(".out-of-stock-label"):
                avail = "Out of Stock"
            elif scope.select_one("button.single_add_to_cart_button, .single_add_to_cart_button"):
                avail = "In Stock"

    if not price:
        amt_el = (
            scope.select_one("p.price ins .amount, .price ins .amount")
            or scope.select_one("p.price .amount, .price .amount")
        )
        if amt_el:
            txt = amt_el.get_text(" ", strip=True)
            pmatch = re.search(r"[\d][\d,]*\.?\d*", txt)
            if pmatch:
                price = pmatch.group(0).replace(",", "")

    return avail, price, currency


def handle_woocommerce(row):
    url = (row.get("url") or "").strip()
    if not url:
        return fail("no URL provided")
    resp, err = fetch(url)
    if err:
        return fail(f"fetch error: {err}", url)
    if resp.status_code == 404:
        return fail("HTTP 404 - page not found (book may have been delisted)", url)
    if resp.status_code != 200:
        return fail(f"HTTP {resp.status_code}", url)
    avail, price, currency = extract_woocommerce(resp.text)
    note = "" if avail else "no stock indicator found on page"
    if not price:
        note = (note + "; " if note else "") + "price not found on page"
    return ok_result(resp.url, avail or "Unknown", price, currency, note)


# --------------------------------------------------------------------------
# OpenCart extractor (padmagandha, aitbspublishers)
# --------------------------------------------------------------------------


def extract_opencart_availability(html):
    m = re.search(
        r"Availability:\s*(?:</[a-zA-Z]+>\s*)?(?:<[^>]+>\s*)?([A-Za-z][A-Za-z ]{2,20})",
        html,
    )
    if m:
        return m.group(1).strip()
    return None


def extract_opencart_price(html):
    m = re.search(r'class="price-new"[^>]*>(.{0,200}?)<', html, re.S)
    window = m.group(1) if m else None
    if not window:
        m2 = re.search(r"<h2>\s*(.{0,100}?)</h2>", html)
        if m2 and ("₹" in m2.group(1) or "Rs" in m2.group(1)):
            window = m2.group(1)
    if window:
        num = re.search(r"[\d][\d,]*\.?\d*", window)
        if num:
            return num.group(0).replace(",", "")
    num = re.search(r"(?:₹|र|Rs\.?)\s*([\d][\d,]*\.?\d*)", html)
    return num.group(1).replace(",", "") if num else None


def handle_padmagandha(row):
    url = (row.get("url") or "").strip()
    if not url:
        return fail("no URL provided")
    m = re.search(r"product_id=(\d+)", url)
    canon = (
        f"http://www.padmagandha.com/index.php?route=product/product&product_id={m.group(1)}"
        if m else url
    )
    resp, err = fetch(canon)
    if err:
        return fail(f"fetch error: {err}", canon)
    if resp.status_code != 200:
        return fail(f"HTTP {resp.status_code}", canon)
    html = resp.text
    avail_raw = extract_opencart_availability(html)
    avail = None
    if avail_raw:
        low = avail_raw.lower()
        if "out of stock" in low:
            avail = "Out of Stock"
        elif "in stock" in low:
            avail = "In Stock"
        else:
            avail = avail_raw
    price = extract_opencart_price(html)
    note = "" if (avail and price) else "some fields not found on page (legacy OpenCart catalog site)"
    return ok_result(canon, avail or "Unknown", price, "INR", note)


def handle_aitbspublishers(row):
    url = (row.get("url") or "").strip()
    if not url:
        return fail("no URL provided")
    resp, err = fetch(url, headers={"Referer": "https://www.aitbspublishersindia.com/"})
    if err:
        return fail(f"fetch error: {err}", url)
    if resp.status_code != 200:
        return fail(f"HTTP {resp.status_code}", url)
    html = resp.text
    avail_raw = extract_opencart_availability(html)
    if avail_raw:
        low = avail_raw.lower()
        avail = "Out of Stock" if "out of stock" in low else ("In Stock" if "in stock" in low else avail_raw)
        note = ""
    else:
        has_cart = 'id="button-cart"' in html
        avail = "In Stock (assumed)" if has_cart else "Unknown"
        note = (
            "site shows no explicit stock text; inferred from add-to-cart button presence"
            if has_cart else "no stock indicator found on page"
        )
    price = extract_opencart_price(html)
    if not price:
        note = (note + "; " if note else "") + "price not found on page"
    return ok_result(resp.url, avail, price, "INR", note)


# --------------------------------------------------------------------------
# bookganga
# --------------------------------------------------------------------------


# bookganga.com was found (during investigation) to return HTTP 403 for its
# ENTIRE domain from this server's IP (confirmed via both `requests` and a raw
# curl from the VPS host itself, on every path including the bare homepage) -
# this is a datacenter-IP-level WAF block, not a per-request/header issue.
# Retrying per-book would waste hours (1463 rows * a multi-attempt backoff
# each) without ever succeeding while the block is in effect, so instead we
# run a single precheck before starting the whole site batch (see
# check_bookganga_access() / main()) and short-circuit every row instantly if
# it's blocked, while still trying a real fetch per-row (with its normal
# bounded retries) if the precheck says the site is reachable.
BOOKGANGA_BLOCK_NOTE = (
    "bookganga.com returned HTTP 403 (Forbidden) for its entire domain from this "
    "server's IP, confirmed via a precheck before this batch started - this is an "
    "infrastructure-level WAF/IP block, not a per-book data issue; needs a different "
    "network path (e.g. a different egress IP) to verify these books"
)


def check_bookganga_access():
    """One-time precheck: is bookganga.com reachable from here at all right now?"""
    resp, err = fetch("https://www.bookganga.com/", max_retries=1)
    if err:
        return False, f"fetch error during precheck: {err}"
    if resp.status_code == 403:
        return False, "HTTP 403 on homepage during precheck"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code} on homepage during precheck"
    return True, ""


def handle_bookganga(row, extra=None):
    url = (row.get("url") or "").strip()
    if not url:
        return fail("no URL provided")
    if extra is not None and extra.get("bookganga_ok") is False:
        return fail(BOOKGANGA_BLOCK_NOTE, url)

    resp, err = fetch(url)
    if err:
        return fail(f"fetch error: {err}", url)
    if resp.status_code == 403:
        return fail(
            "HTTP 403 Forbidden fetching this specific book (site was reachable "
            "at batch precheck time, so this looks like a per-request block rather "
            "than the full-domain block seen earlier)",
            url,
        )
    if resp.status_code != 200:
        return fail(f"HTTP {resp.status_code}", url)
    text = resp.text
    avail = "In Stock" if ("Add to Cart" in text or "Buy Now" in text) else "Out of Stock"
    price = None
    m = re.search(r'BookDetails_BookPrice"[^>]*>(.*?)</div>', text, re.S)
    if m:
        pm = re.search(r"R\s*([\d,]+\.?\d*)", m.group(1))
        if pm:
            price = pm.group(1).replace(",", "")
    note = "" if price else "price not found on detail page"
    return ok_result(resp.url, avail, price, "INR", note)


# --------------------------------------------------------------------------
# champaca is handled via the same bulk Shopify-catalog approach as
# akshardhara/ritikart below (see build_shopify_catalog / handle_shopify_catalog_site).
# NOTE: an earlier version of this script tried reading {url}.json per book,
# but Shopify's per-product JSON endpoint does NOT include the "available"
# boolean (confirmed empirically) — only the bulk /products.json listing
# endpoint does. That earlier approach always returned "Unknown" for stock
# and has been removed in favour of the bulk-catalog approach.
# --------------------------------------------------------------------------
# dcbooks (React SPA behind a JS gate — needs a real browser render)
# --------------------------------------------------------------------------


class DCFetcher:
    def __init__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._ctx = self._browser.new_context(user_agent=UA, locale="en-US")
        self._ctx.route(
            "**/*",
            lambda r: (
                r.abort()
                if r.request.resource_type in ("image", "media", "font")
                else r.continue_()
            ),
        )
        self._page = self._ctx.new_page()

    def get(self, url, timeout_ms=30000, selector_timeout_ms=15000):
        # NOTE: wait_until="networkidle" was tried first and consistently timed
        # out (45s+) on this site - it looks like the page keeps some
        # background connection open (analytics/chat widget) that never lets
        # the network go idle. domcontentloaded + waiting for the specific
        # elements we actually need is both faster (~1-2s typical) and more
        # reliable, confirmed during investigation.
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                self._page.wait_for_selector(
                    ".stock-status, .price-now, h1", timeout=selector_timeout_ms
                )
            except Exception:
                pass  # proceed with whatever loaded; caller handles missing fields
            return self._page.content(), None
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    def close(self):
        try:
            self._browser.close()
            self._pw.stop()
        except Exception:
            pass


def handle_dcbooks(row, fetcher):
    url = (row.get("url") or "").strip()
    if not url:
        return fail("no URL provided")
    html, err = fetcher.get(url)
    if err:
        html, err = fetcher.get(url)  # one bounded retry
        if err:
            return fail(f"render error: {err}", url)
    if not html or len(html) < 2000:
        return fail("page did not render fully (too short / possible gate or 404)", url)

    avail = "Unknown"
    m = re.search(r'class="stock-status"[^>]*>(.*?)</div>', html, re.S)
    if m:
        t = re.sub(r"<[^>]+>", " ", m.group(1)).strip()
        low = t.lower()
        if "out of stock" in low:
            avail = "Out of Stock"
        elif "in stock" in low:
            avail = "In Stock"
        elif t:
            avail = t

    price = None
    pm = re.search(r'class="price-now"[^>]*>\s*(?:<[^>]*>)*\s*₹?\s*([\d][\d,]*\.?\d*)', html)
    if pm:
        price = pm.group(1).replace(",", "")

    note = ""
    if avail == "Unknown":
        note = "stock indicator not found on rendered page"
    if not price:
        note = (note + "; " if note else "") + "price not found on rendered page"
    return ok_result(url, avail, price, "INR", note)


# --------------------------------------------------------------------------
# Shopify bulk-catalog sites: akshardhara, ritikart (ISBN indexed via barcode)
# --------------------------------------------------------------------------


def fetch_json_page_with_backoff(url, max_429_retries=6):
    """GET a paginated-catalog JSON URL, retrying on HTTP 429 with growing
    backoff (bounded) instead of giving up on the whole catalog fetch."""
    attempt = 0
    while True:
        resp, err = fetch(url)
        if err:
            return None, err
        if resp.status_code == 429:
            attempt += 1
            if attempt > max_429_retries:
                return None, f"HTTP 429 (rate limited) after {max_429_retries} retries"
            wait = min(30, 5 * attempt)
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = max(wait, float(retry_after))
                except ValueError:
                    pass
            log(f"    HTTP 429 on {url} - backing off {wait:.0f}s (retry {attempt}/{max_429_retries})")
            time.sleep(wait)
            continue
        return resp, None


def build_shopify_catalog(domain, max_pages=200):
    by_isbn = {}
    by_handle = {}
    page = 1
    total = 0
    consecutive_page_failures = 0
    while page <= max_pages:
        resp, err = fetch_json_page_with_backoff(f"https://{domain}/products.json?limit=250&page={page}")
        if err:
            log(f"  [{domain}] catalog page {page} fetch error: {err}")
            consecutive_page_failures += 1
            if consecutive_page_failures >= 5:
                log(f"  [{domain}] giving up after {consecutive_page_failures} consecutive page failures "
                    f"(catalog may be incomplete beyond page {page-1})")
                break
            page += 1
            time.sleep(2)
            continue
        if resp.status_code != 200:
            log(f"  [{domain}] catalog page {page} HTTP {resp.status_code} (stopping catalog fetch)")
            break
        try:
            data = resp.json()
        except Exception:
            log(f"  [{domain}] catalog page {page} bad JSON (stopping catalog fetch)")
            break
        consecutive_page_failures = 0
        products = data.get("products", [])
        if not products:
            break
        for p in products:
            handle = p.get("handle")
            if handle:
                by_handle[handle] = p
            variants = p.get("variants") or []
            if variants:
                barcode = (variants[0].get("barcode") or "").strip()
                if barcode:
                    by_isbn[normalize_isbn(barcode)] = p
        total += len(products)
        page += 1
        time.sleep(0.4)
    log(f"  [{domain}] catalog fetched: {total} products, {len(by_isbn)} with ISBN barcode, {page-1} pages")
    return by_isbn, by_handle


def extract_shopify_handle(url_or_slug):
    """Accepts either a bare Shopify handle/slug or a full product URL and
    returns just the handle."""
    s = (url_or_slug or "").strip()
    if not s:
        return ""
    s = s.split("?")[0].rstrip("/")
    if "://" in s:
        s = s.rsplit("/", 1)[-1]
    return s


def handle_shopify_catalog_site(row, domain, catalog):
    by_isbn, by_handle = catalog
    isbn_norm = normalize_isbn(row.get("isbn"))
    p = by_isbn.get(isbn_norm) if isbn_norm else None
    matched_by = "isbn"
    if not p:
        slug = extract_shopify_handle(row.get("url"))
        if slug:
            p = by_handle.get(slug)
            matched_by = "slug"
    if not p:
        return fail(
            f"not found in {domain} catalog by ISBN ({row.get('isbn')}) or by slug "
            f"('{extract_shopify_handle(row.get('url'))}')"
        )
    variants = p.get("variants") or []
    v = variants[0] if variants else {}
    available = v.get("available")
    avail = "In Stock" if available is True else ("Out of Stock" if available is False else "Unknown")
    price = v.get("price")
    handle = p.get("handle")
    url_checked = f"https://{domain}/products/{handle}" if handle else (row.get("url") or "")
    # NOTE: on all three Shopify sites we bulk-index (akshardhara, ritikart,
    # champaca), the variant "barcode" field is populated with the ISBN for
    # only a small minority of products - matching by slug/handle (an exact
    # match against the site's own product permalink) is the normal, reliable
    # path here, not a fallback of last resort.
    note = f"matched catalog product via {matched_by}"
    return ok_result(url_checked, avail, price, "INR", note)


# --------------------------------------------------------------------------
# darussalam (Shopify, no ISBN field exposed -> bulk catalog + fuzzy title match)
# --------------------------------------------------------------------------


def build_darussalam_catalog(max_pages=60):
    catalog = []
    page = 1
    total = 0
    consecutive_page_failures = 0
    while page <= max_pages:
        resp, err = fetch_json_page_with_backoff(f"https://darussalam.in/products.json?limit=250&page={page}")
        if err:
            log(f"  [darussalam] catalog page {page} fetch error: {err}")
            consecutive_page_failures += 1
            if consecutive_page_failures >= 5:
                log(f"  [darussalam] giving up after {consecutive_page_failures} consecutive page "
                    f"failures (catalog may be incomplete beyond page {page-1})")
                break
            page += 1
            time.sleep(2)
            continue
        if resp.status_code != 200:
            log(f"  [darussalam] catalog page {page} HTTP {resp.status_code} (stopping)")
            break
        try:
            data = resp.json()
        except Exception:
            break
        consecutive_page_failures = 0
        products = data.get("products", [])
        if not products:
            break
        for p in products:
            catalog.append((normalize_title(p.get("title", "")), p))
        total += len(products)
        page += 1
        time.sleep(0.4)
    log(f"  [darussalam] catalog fetched: {total} products, {page-1} pages")
    return catalog


def handle_darussalam(row, catalog):
    title = row.get("title") or ""
    norm = normalize_title(title)
    if not norm:
        return fail("no usable title to match against darussalam catalog")
    best = None
    best_score = 0.0
    for cand_norm, p in catalog:
        if not cand_norm:
            continue
        score = difflib.SequenceMatcher(None, norm, cand_norm).ratio()
        if score > best_score:
            best_score = score
            best = p
    THRESHOLD = 0.6
    if not best or best_score < THRESHOLD:
        return fail(
            f"no confident match in darussalam catalog (best fuzzy title score "
            f"{best_score:.2f} < {THRESHOLD}; site exposes no ISBN field so exact "
            f"matching isn't possible - source URL/slug for this row was also "
            f"confirmed unreliable during investigation)"
        )
    variants = best.get("variants") or []
    v = variants[0] if variants else {}
    available = v.get("available")
    avail = "In Stock" if available is True else ("Out of Stock" if available is False else "Unknown")
    price = v.get("price")
    handle = best.get("handle")
    url_checked = f"https://darussalam.in/products/{handle}" if handle else ""
    note = (
        f"matched via fuzzy title match only (score={best_score:.2f}); darussalam's "
        f"public catalog has no ISBN field, so please spot-check this match manually "
        f"before relying on it -- matched title: '{best.get('title','')}'"
    )
    return ok_result(url_checked, avail, price, "INR", note)


# --------------------------------------------------------------------------
# prajaktprakashan: captured URLs are "add-to-cart=<id>" links on a shop
# listing page; the <id> is a WooCommerce post ID that we resolve, but we
# ONLY trust the result if the resolved product's SKU matches the input ISBN
# (investigation confirmed at least one row's captured id pointed at a
# completely different, unrelated book).
# --------------------------------------------------------------------------


def handle_prajaktprakashan(row):
    url = (row.get("url") or "").strip()
    if not url:
        return fail("no URL provided")
    m = re.search(r"add-to-cart=(\d+)", url)
    if not m:
        return fail(f"could not extract a WooCommerce product id from captured URL '{url}'")
    pid = m.group(1)
    resp, err = fetch(f"https://prajaktprakashan.com/?p={pid}")
    if err:
        return fail(f"fetch error resolving product id {pid}: {err}")
    if resp.status_code != 200:
        return fail(f"HTTP {resp.status_code} resolving product id {pid}")
    resolved_url = resp.url
    html = resp.text
    skum = re.search(r'class="sku">([^<]+)</span>', html)
    sku = skum.group(1).strip() if skum else ""
    target_isbn = normalize_isbn(row.get("isbn"))
    sku_isbn = normalize_isbn(sku)
    if not sku_isbn or not target_isbn or sku_isbn != target_isbn:
        return fail(
            f"captured URL's add-to-cart id ({pid}) resolves to a DIFFERENT product "
            f"(SKU '{sku}') than the target ISBN {row.get('isbn')} - the source URL "
            f"for this row is unreliable; not reporting data for the wrong book",
            resolved_url,
        )
    avail, price, currency = extract_woocommerce(html)
    note = "resolved via WordPress post-ID redirect; SKU/ISBN match confirmed"
    if not avail:
        avail = "Unknown"
        note += "; no stock indicator found on page"
    if not price:
        note += "; price not found on page"
    return ok_result(resolved_url, avail, price, currency, note)


# --------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------


def process_row(row, extra):
    site = row.get("site", "")
    try:
        if site in ("commonfolks", "exoticindia", "panuval", "rajhansprakashan"):
            res = handle_generic_schema(row)
        elif site == "bookganga":
            res = handle_bookganga(row, extra)
        elif site == "champaca":
            res = handle_shopify_catalog_site(row, "champaca.in", extra["champaca_catalog"])
        elif site == "dcbooks":
            res = handle_dcbooks(row, extra["dc_fetcher"])
        elif site == "akshardhara":
            res = handle_shopify_catalog_site(row, "akshardhara.com", extra["akshardhara_catalog"])
        elif site == "ritikart":
            res = handle_shopify_catalog_site(row, "ritikart.com", extra["ritikart_catalog"])
        elif site == "darussalam":
            res = handle_darussalam(row, extra["darussalam_catalog"])
        elif site in ("boighar", "idara", "kairalibooks"):
            res = handle_woocommerce(row)
        elif site == "prajaktprakashan":
            res = handle_prajaktprakashan(row)
        elif site == "padmagandha":
            res = handle_padmagandha(row)
        elif site == "aitbspublishers":
            res = handle_aitbspublishers(row)
        else:
            res = fail(f"no handler implemented for site '{site}'", row.get("url", ""))
    except Exception as e:
        res = fail(f"unexpected exception: {type(e).__name__}: {e}", row.get("url", ""))

    return {
        "isbn": row.get("isbn", ""),
        "title": row.get("title", ""),
        "site": site,
        "url_checked": res["url_checked"] or row.get("url", ""),
        "current_availability": res["current_availability"],
        "stock_qty_if_known": res["stock_qty_if_known"],
        "current_price": res["current_price"],
        "existing_availability_before": row.get("existing_availability", ""),
        "status_note": res["status_note"],
        "checked_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def read_input_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_done_keys(path):
    keys = set()
    if not os.path.exists(path):
        return keys
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                keys.add((row.get("isbn", ""), row.get("site", "")))
    except Exception:
        pass
    return keys


# Process cheap/fast sites first, dcbooks (browser-render, slowest per item) last.
SITE_ORDER = [
    "champaca", "akshardhara", "ritikart", "darussalam", "boighar", "idara",
    "kairalibooks", "prajaktprakashan", "padmagandha", "aitbspublishers",
    "rajhansprakashan", "bookganga", "panuval", "exoticindia", "commonfolks",
    "dcbooks",
]


def main():
    log("=" * 70)
    log("Starting availability_check_16sites.py")
    log(f"Input: {INPUT_CSV}")
    log(f"Output: {OUTPUT_CSV}")

    rows = read_input_csv(INPUT_CSV)
    by_site = defaultdict(list)
    for r in rows:
        by_site[r["site"]].append(r)

    total = len(rows)
    log(f"Loaded {total} rows across {len(by_site)} sites: "
        + ", ".join(f"{s}={len(by_site[s])}" for s in SITE_ORDER if s in by_site))

    unknown_sites = set(by_site) - set(SITE_ORDER)
    if unknown_sites:
        log(f"WARNING: sites in input with no handler: {unknown_sites} "
            f"({sum(len(by_site[s]) for s in unknown_sites)} rows) - "
            f"will be recorded as Unable to verify")

    done_keys = load_done_keys(OUTPUT_CSV)
    if done_keys:
        log(f"Resuming previous run: {len(done_keys)} (isbn,site) pairs already done, will be skipped")

    write_header = not (os.path.exists(OUTPUT_CSV) and os.path.getsize(OUTPUT_CSV) > 0)
    out_f = open(OUTPUT_CSV, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(out_f, fieldnames=FIELDNAMES)
    if write_header:
        writer.writeheader()
        out_f.flush()

    total_done = len(done_keys)
    t_start = time.time()
    extra = {}

    order = SITE_ORDER + sorted(unknown_sites)

    for site in order:
        site_rows = by_site.get(site, [])
        if not site_rows:
            continue
        log(f"=== Starting site: {site} ({len(site_rows)} rows) ===")

        if site == "champaca":
            log("Building champaca catalog (products.json, paginated)...")
            extra["champaca_catalog"] = build_shopify_catalog("champaca.in")
        elif site == "akshardhara":
            log("Building akshardhara catalog (products.json, paginated)...")
            extra["akshardhara_catalog"] = build_shopify_catalog("akshardhara.com")
        elif site == "ritikart":
            log("Building ritikart catalog (products.json, paginated)...")
            extra["ritikart_catalog"] = build_shopify_catalog("ritikart.com")
        elif site == "darussalam":
            log("Building darussalam catalog (products.json, paginated)...")
            extra["darussalam_catalog"] = build_darussalam_catalog()
        elif site == "bookganga":
            log("Precheck: is bookganga.com reachable from this server right now?")
            ok, reason = check_bookganga_access()
            extra["bookganga_ok"] = ok
            if ok:
                log("  bookganga.com reachable - proceeding with per-book fetches")
            else:
                log(f"  bookganga.com NOT reachable ({reason}) - this matches a domain-wide "
                    f"HTTP 403 WAF block confirmed during investigation (this server's IP is "
                    f"blocked). All {len(site_rows)} bookganga rows will be recorded as "
                    f"'Unable to verify' immediately (no per-row retries) to avoid wasting "
                    f"hours retrying a block that a retry loop cannot clear.")
        elif site == "dcbooks":
            log("Launching Playwright browser for dcbooks (JS-gated SPA)...")
            try:
                extra["dc_fetcher"] = DCFetcher()
            except Exception as e:
                log(f"  Could not launch Playwright: {e} - all dcbooks rows will be marked Unable to verify")
                extra["dc_fetcher"] = None

        delay = DELAYS.get(site, 0.3)
        ok_ct = 0
        fail_ct = 0
        skipped = 0

        for i, row in enumerate(site_rows, 1):
            key = (row.get("isbn", ""), row.get("site", ""))
            if key in done_keys:
                skipped += 1
                continue

            if site == "dcbooks" and extra.get("dc_fetcher") is None:
                out = process_row(row, extra)
                out["current_availability"] = "Unable to verify"
                out["status_note"] = "Playwright unavailable in this environment"
            else:
                out = process_row(row, extra)

            writer.writerow(out)
            out_f.flush()
            done_keys.add(key)
            total_done += 1
            if out["current_availability"] == "Unable to verify":
                fail_ct += 1
            else:
                ok_ct += 1

            if i % 100 == 0 or i == len(site_rows):
                elapsed = time.time() - t_start
                log(f"  [{site}] {i}/{len(site_rows)} (ok={ok_ct} fail={fail_ct} "
                    f"skipped_resumed={skipped}) | overall {total_done}/{total} | "
                    f"elapsed {elapsed/60:.1f}m")

            time.sleep(delay)

        if site == "dcbooks" and extra.get("dc_fetcher") is not None:
            extra["dc_fetcher"].close()

        log(f"=== Finished site: {site}: ok={ok_ct} fail={fail_ct} skipped_resumed={skipped} ===")

    out_f.close()
    elapsed = (time.time() - t_start) / 60
    log(f"ALL DONE. total_processed={total_done}/{total} elapsed={elapsed:.1f}m")
    log("=" * 70)


if __name__ == "__main__":
    main()
