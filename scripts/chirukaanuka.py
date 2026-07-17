"""
Chirukaanuka.com — Telugu (+ English) bookstore, Shopify store, products.json crawler.

Shopify exposes the full catalog as structured JSON, so this is a SINGLE-PHASE
scrape — no detail-page fetches, no HTML parsing, no bot handling:
  /products.json?limit=250&page=<N>       (falls back to /collections/all/products.json)

robots.txt explicitly ALLOWS /products/ and /collections/ crawling (it only
disallows cart/checkout/account + AJAX surfaces, which we never touch).

body_html carries consistent bold-labelled specs in a <ul>:
    <li><b>Author: </b>Mythili Venkateswara Rao</li>
    <li><b>Publisher:</b> Gollapudi Veeraswami Son Publications (Latest Edition)</li>
    <li><b>Paperback: </b>80 Pages</li>        <- gives BOTH binding and pages
    <li><b>Language:</b> Telugu</li>
    <li><strong>Size:</strong> 22*28 Cm</li>
The "(2019)" after a publisher is the publication year.

Store carries BOTH Telugu and English books (product_type: "Telugu Books" /
"English Books"), so language is read per-book, never hardcoded.

ISBN NOTE: Shopify omits variant `barcode` from public products.json, and `sku`
here is an internal numeric code — so ISBN usually is NOT available. We DO scan
the description text for a real ISBN and capture it when present. Run the probe
to measure exactly how many books actually have one:
    python scripts/chirukaanuka.py isbnprobe [pages]

Run:
  python scripts/chirukaanuka.py peek [page]   -> parse+print first products of a page
  python scripts/chirukaanuka.py count         -> total products + page count
  python scripts/chirukaanuka.py isbnprobe [n] -> how many books carry an ISBN
  python scripts/chirukaanuka.py               -> full crawl (resumable by page)
Pace: CK_MIN_DELAY / CK_MAX_DELAY (default 0.5-1.2s).  Test cap: CK_MAX_PAGES.
"""
import html as _html
import json
import os
import random
import re
import sys
import time

import requests

_here = os.path.dirname(os.path.abspath(__file__))
for _d in (_here, os.path.dirname(_here), os.path.dirname(os.path.dirname(_here)), "/app"):
    if os.path.exists(os.path.join(_d, "scriptkit.py")):
        sys.path.insert(0, _d)
        break
import scriptkit

BASE = "https://www.chirukaanuka.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
LIMIT = 250
MIN_DELAY = float(os.environ.get("CK_MIN_DELAY", "0.5"))
MAX_DELAY = float(os.environ.get("CK_MAX_DELAY", "1.2"))
MAX_PAGES = int(os.environ.get("CK_MAX_PAGES", "0"))   # 0 = until empty
STATE_FILE = os.environ.get("CK_STATE", "/app/scripts/.chirukaanuka_page.json")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "application/json, */*;q=0.1",
                        "Accept-Language": "en-US,en;q=0.9"})
_PATH = {"p": "/products.json"}     # switches to /collections/all/products.json if needed


def nap():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def fetch_page(page):
    """Product dicts for a page ([] on empty/error). Auto-falls back to the
    /collections/all/ path if the root products.json isn't served."""
    for path in (_PATH["p"], "/collections/all/products.json"):
        url = f"{BASE}{path}?limit={LIMIT}&page={page}"
        for attempt in range(5):
            try:
                r = SESSION.get(url, timeout=45)
                if r.status_code in (429, 503):
                    wait = float(r.headers.get("Retry-After") or 0) or min(120, 6 * (2 ** attempt))
                    print(f"   {r.status_code}; wait {wait:.0f}s ({attempt+1}/5)")
                    time.sleep(wait)
                    continue
                if r.status_code in (404, 500):
                    break                     # try the other path
                r.raise_for_status()
                prods = (r.json() or {}).get("products", []) or []
                _PATH["p"] = path             # remember the working path
                return prods
            except json.JSONDecodeError:
                break
            except Exception as e:
                print(f"   err (try {attempt+1}): {e}")
                time.sleep(min(60, 5 * (2 ** attempt)))
    return []


# ---- field extraction ---------------------------------------------------
def _text(html):
    h = re.sub(r"(?i)<br\s*/?>", "\n", html or "")
    h = re.sub(r"(?i)</(p|li|div|h\d)>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = _html.unescape(h)
    h = re.sub(r"[ \t\u00a0]+", " ", h)
    return "\n".join(ln.strip() for ln in h.split("\n") if ln.strip())


def _label(text, *labels):
    """Value after 'Label:' on its line (labels are bolded in the source)."""
    for lab in labels:
        m = re.search(lab + r"\s*:\s*([^\n]+)", text, re.I)
        if m:
            v = m.group(1).strip(" :.-\u00a0")
            if v:
                return v
    return ""


# real ISBN-10/13 (allow separators); avoids matching random digit runs
ISBN_RE = re.compile(
    r"ISBN(?:[-\s]*1[03])?\s*[:\-]?\s*((?:97[89][-\s]?)?[\d][\d\-\s]{8,16}[\dXx])", re.I)
ISBN13_RE = re.compile(r"\b(97[89][-\s]?\d[\d\-\s]{9,14}\d)\b")


def _find_isbn(text):
    m = ISBN_RE.search(text) or ISBN13_RE.search(text)
    if not m:
        return ""
    digits = re.sub(r"[^0-9Xx]", "", m.group(1))
    return digits if len(digits) in (10, 13) else ""


BINDINGS = ("Paperback", "Hardcover", "Hardback", "Perfect Paperback",
            "Board Book", "Spiral", "Flexibound", "Library Binding")


def parse_product(p):
    title = _html.unescape(p.get("title", "") or "").strip()
    handle = p.get("handle", "")
    body = _text(p.get("body_html", ""))
    tags = p.get("tags", []) or []

    author = _label(body, "Author", "Authors", "Writer")
    pub_line = _label(body, "Publisher", "Publishers")
    publisher, year = pub_line, ""
    ym = re.search(r"\((?:.*?)?((?:19|20)\d{2})\)", pub_line)      # "... (2019)"
    if ym:
        year = ym.group(1)
    publisher = re.sub(r"\s*\([^)]*\)\s*$", "", pub_line).strip() or pub_line

    # "Paperback: 80 Pages" -> binding + pages (one line gives both)
    binding = pages = ""
    for b in BINDINGS:
        v = _label(body, re.escape(b))
        if v:
            binding = b
            pm = re.search(r"(\d+)", v)
            if pm:
                pages = pm.group(1)
            break
    if not pages:
        pages = re.sub(r"[^\d]", "", _label(body, "Pages", "No. of Pages") or "")
    if not binding:
        for b in BINDINGS:                       # else infer from the title
            if re.search(re.escape(b), title, re.I):
                binding = b
                break

    language = _label(body, "Language") or ""
    if not language:
        pt = (p.get("product_type") or "")
        language = "English" if "english" in pt.lower() else ("Telugu" if "telugu" in pt.lower() else "")

    size = _label(body, "Size", "Dimensions")
    isbn = _find_isbn(body)

    v0 = (p.get("variants") or [{}])[0]
    price = str(v0.get("price") or "").strip()
    cap = v0.get("compare_at_price")
    mrp = str(cap).strip() if cap else ""
    discount = ""
    try:
        if mrp and price and float(mrp) > float(price) > 0:
            discount = str(round((float(mrp) - float(price)) / float(mrp) * 100))
    except Exception:
        pass
    sku = (v0.get("sku") or "").strip()
    grams = v0.get("grams")
    weight = f"{grams} g" if grams else ""
    stock = "In Stock" if v0.get("available") else "Out of Stock"

    imgs = p.get("images") or []
    image = imgs[0].get("src", "") if imgs else ""

    def val(x):
        return x if x else "N/A"

    return {
        "title": val(title),
        "author": val(author),
        "publisher": val(publisher),
        "category": val((p.get("product_type") or "").strip()),
        "isbn": val(isbn),
        "pages": val(pages),
        "binding": val(binding),
        "language": val(language),
        "year": val(year),
        "dimensions": val(size),
        "item_weight": val(weight),
        "price": val(price),
        "mrp": val(mrp),
        "discount": val(discount),
        "stock": stock,
        "sku": val(sku),
        "vendor": val((p.get("vendor") or "").strip()),
        "description": body or "N/A",
        "tags": ", ".join(tags) if tags else "N/A",
        "url": f"{BASE}/products/{handle}" if handle else "N/A",
        "image_url": image or "N/A",
    }


# ---- state --------------------------------------------------------------
def _save_page(page):
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        json.dump({"next_page": page}, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"   state save warn: {e}")


def _load_page():
    try:
        return int(json.load(open(STATE_FILE, encoding="utf-8")).get("next_page", 1))
    except Exception:
        return 1


# ---- run ----------------------------------------------------------------
def run():
    page = _load_page()
    print(f"crawling products.json from page {page} (limit {LIMIT})")
    total, dbfail, with_isbn = 0, 0, 0
    while True:
        if MAX_PAGES and page > MAX_PAGES:
            print(f"  reached CK_MAX_PAGES={MAX_PAGES}; stopping (resumable).")
            break
        prods = fetch_page(page)
        if not prods:
            print(f"  page {page}: empty -> end of catalog")
            break
        rows = [parse_product(p) for p in prods]
        try:
            scriptkit.save("chirukaanuka", rows, key_fields=["url"])
        except Exception as e:
            dbfail += 1
            print(f"  !! DB save failed ({dbfail}/5) on page {page}: {e}")
            if dbfail >= 5:
                print("  !! aborting: database unreachable; rerun to resume.")
                break
            time.sleep(10)
            continue
        dbfail = 0
        total += len(rows)
        with_isbn += sum(1 for r in rows if r["isbn"] != "N/A")
        _save_page(page + 1)
        s = rows[0]
        print(f"  page {page}: +{len(rows)} (total {total}, isbn {with_isbn}) | "
              f"{s['title'][:26]} | ₹{s['price']}/{s['mrp']} | {s['author'][:16]}")
        page += 1
        nap()
    print(f"\nDone. Saved/updated {total} books this session ({with_isbn} had an ISBN).")


# ---- probes -------------------------------------------------------------
def cmd_peek(page=1):
    prods = fetch_page(int(page))
    print(f"page {page}: {len(prods)} products\n")
    for p in prods[:5]:
        rec = parse_product(p)
        for k, v in rec.items():
            if k in ("description", "tags"):
                v = (v or "")[:70].replace("\n", " ") + ("…" if len(v or "") > 70 else "")
            print(f"  {k:>12}: {str(v)[:90]}")
        print()


def cmd_count():
    page, total = 1, 0
    while True:
        n = len(fetch_page(page))
        if not n:
            break
        total += n
        print(f"  page {page}: {n} (running total {total})")
        page += 1
        nap()
    print(f"\ntotal products: {total} (~{page-1} pages @ {LIMIT})")


def cmd_isbnprobe(pages=4):
    """Measure how many books actually carry an ISBN (and show examples)."""
    pages = int(pages)
    seen = hits = 0
    examples, sku_like = [], 0
    for page in range(1, pages + 1):
        prods = fetch_page(page)
        if not prods:
            break
        for p in prods:
            rec = parse_product(p)
            seen += 1
            if rec["isbn"] != "N/A":
                hits += 1
                if len(examples) < 8:
                    examples.append((rec["isbn"], rec["title"][:40]))
            if rec["sku"] != "N/A":
                sku_like += 1
        print(f"  page {page}: scanned {seen}, isbn found {hits}")
        nap()
    pct = (hits / seen * 100) if seen else 0
    print(f"\n=== ISBN PROBE ===")
    print(f"  books scanned : {seen}")
    print(f"  with ISBN     : {hits}  ({pct:.1f}%)")
    print(f"  with SKU      : {sku_like}  (store codes, NOT ISBNs)")
    if examples:
        print("  examples:")
        for i, t in examples:
            print(f"     {i}  {t}")
    else:
        print("  -> No ISBNs in the catalog data. The only path to ISBNs for this")
        print("     store is a cross-source backfill (match title+author against")
        print("     sources that DO publish ISBNs, e.g. Logili / your merged CSV).")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "peek":
        cmd_peek(arg or 1)
    elif cmd == "count":
        cmd_count()
    elif cmd == "isbnprobe":
        cmd_isbnprobe(arg or 4)
    else:
        run()