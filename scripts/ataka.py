"""
Ataka Books (books.ataka.in) — అటక, Telugu bookstore on Shopify.

SINGLE-PHASE JSON scrape (no detail fetches, no HTML parsing, no bot handling):
  /products.json?limit=250&page=<N>     (falls back to /collections/all/products.json)
robots.txt explicitly ALLOWS /products/ and /collections/ crawling.

Catalog is SMALL: ~575 items -> ~3 pages -> finishes in seconds.

FIELD REALITY (verified against the raw product JSON — this store is sparse):
  populated : title, price, vendor(=publisher), available(stock), body_html, images
  EMPTY     : product_type (no category), tags, sku (null), barcode (null),
              compare_at_price (no MRP), grams/weight (0)
  -> so NO isbn / category / pages / binding / mrp are available from this store.

AUTHOR is recoverable from body_html, which opens with a consistent line:
    <p>Aavarana BY <a href="...writers.php?more1=S.L.%20Bhyrappa">S.L. Bhyrappa</a></p>
so we take the text after "<TITLE> BY", and also read the emescobooks
writers.php?more1=<name> link as a cross-check.

body_html also contains THEME JUNK (group-block divs, an add-to-cart <form>) and
SEO BOILERPLATE tails ("Free Shipping on orders above ...", "Get your copy of X
online at Ataka Books...", "Related: telugu books online, ..."), all stripped so
`description` is the real blurb.

Run:
  python scripts/ataka.py peek [page]   -> parse+print first products of a page
  python scripts/ataka.py count         -> total products + page count
  python scripts/ataka.py               -> full crawl (resumable by page)
Pace: AT_MIN_DELAY / AT_MAX_DELAY (default 0.5-1.2s).  Test cap: AT_MAX_PAGES.
"""
import html as _html
import json
import os
import random
import re
import sys
import time
from urllib.parse import unquote

import requests

_here = os.path.dirname(os.path.abspath(__file__))
for _d in (_here, os.path.dirname(_here), os.path.dirname(os.path.dirname(_here)), "/app"):
    if os.path.exists(os.path.join(_d, "scriptkit.py")):
        sys.path.insert(0, _d)
        break
import scriptkit

BASE = "https://books.ataka.in"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
LIMIT = 250
MIN_DELAY = float(os.environ.get("AT_MIN_DELAY", "0.5"))
MAX_DELAY = float(os.environ.get("AT_MAX_DELAY", "1.2"))
MAX_PAGES = int(os.environ.get("AT_MAX_PAGES", "0"))     # 0 = until empty
STATE_FILE = os.environ.get("AT_STATE", "/app/scripts/.ataka_page.json")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "application/json, */*;q=0.1",
                        "Accept-Language": "en-US,en;q=0.9"})
_PATH = {"p": "/products.json"}


def nap():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def fetch_page(page):
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
                    break
                r.raise_for_status()
                prods = (r.json() or {}).get("products", []) or []
                _PATH["p"] = path
                return prods
            except json.JSONDecodeError:
                break
            except Exception as e:
                print(f"   err (try {attempt+1}): {e}")
                time.sleep(min(60, 5 * (2 ** attempt)))
    return []


# ---- body_html cleanup / field extraction -------------------------------
def _clean(v):
    v = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", str(v or ""))
    return re.sub(r"\s+", " ", _html.unescape(v)).strip(" :\u00a0\t\n")


def _body_text(body_html):
    """Strip Shopify theme junk (group-blocks, add-to-cart form) then tags."""
    h = body_html or ""
    h = re.sub(r"(?is)<form[^>]*add-to-cart-form.*?</form>", " ", h)
    h = re.sub(r"(?is)<form[^>]*>.*?</form>", " ", h)
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</(p|div|li|h\d)>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = _html.unescape(h)
    h = re.sub(r"[ \t\u00a0]+", " ", h)
    return "\n".join(ln.strip() for ln in h.split("\n") if ln.strip())


# boilerplate tails to drop from the description
_BOILER = re.compile(
    r"(?is)(?:🚚|Free Shipping on orders|Get your copy of|Related\s*:|Show More|"
    r"We are a dedicated online Telugu bookstore|PAN India Delivery).*$")


def _author_from(body_text, body_html, title):
    """'<TITLE> BY <Author>' opening line; cross-checked against the
    emescobooks writers.php?more1=<name> link."""
    # 1) the "... BY <author>" line (first line of the body)
    for ln in body_text.split("\n")[:4]:
        m = re.search(r"\bBY\b\s*[:\-]?\s*(.+)$", ln, re.I)
        if m:
            a = _clean(m.group(1))
            a = re.sub(r"^\s*[-–:]\s*", "", a)
            if a and len(a) <= 80:
                return a
    # 2) the writers.php link (url-encoded author name)
    m = re.search(r"writers\.php\?more1=([^\"'&<>]+)", body_html or "", re.I)
    if m:
        a = _clean(unquote(m.group(1)))
        if a:
            return a
    # 3) title itself sometimes reads "Book - Author"
    m = re.search(r"[-–]\s*([A-Z][A-Za-z.\s]{2,40})$", title or "")
    return _clean(m.group(1)) if m else ""


ISBN_RE = re.compile(
    r"ISBN(?:[-\s]*1[03])?\s*[:\-]?\s*((?:97[89][-\s]?)?[\d][\d\-\s]{8,16}[\dXx])", re.I)


def _find_isbn(text):
    m = ISBN_RE.search(text or "")
    if not m:
        return ""
    d = re.sub(r"[^0-9Xx]", "", m.group(1))
    return d if len(d) in (10, 13) else ""


def parse_product(p):
    title = _html.unescape(p.get("title", "") or "").strip()
    handle = p.get("handle", "")
    body_html = p.get("body_html", "") or ""
    body = _body_text(body_html)

    author = _author_from(body, body_html, title)

    # description = body minus the "TITLE BY AUTHOR" head line and the SEO tail
    desc_lines = body.split("\n")
    if desc_lines and re.search(r"\bBY\b", desc_lines[0], re.I) and len(desc_lines[0]) <= 120:
        desc_lines = desc_lines[1:]
    desc = "\n".join(desc_lines).strip()
    desc = _BOILER.sub("", desc).strip()
    desc = _clean(desc)

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
    stock = "In Stock" if v0.get("available") else "Out of Stock"
    sku = (v0.get("sku") or "").strip() if v0.get("sku") else ""
    grams = v0.get("grams") or 0
    weight = f"{grams} g" if grams else ""

    tags = p.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    tags = tags or []

    imgs = p.get("images") or []
    image = imgs[0].get("src", "") if imgs else ""

    def val(x):
        return x if x else "N/A"

    return {
        "title": val(title),
        "author": val(author),
        "publisher": val((p.get("vendor") or "").strip()),
        "category": val((p.get("product_type") or "").strip()),
        "isbn": val(isbn),
        "language": "Telugu",
        "price": val(price),
        "mrp": val(mrp),
        "discount": val(discount),
        "stock": stock,
        "sku": val(sku),
        "item_weight": val(weight),
        "description": desc or "N/A",
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
    total, dbfail, with_author = 0, 0, 0
    while True:
        if MAX_PAGES and page > MAX_PAGES:
            print(f"  reached AT_MAX_PAGES={MAX_PAGES}; stopping (resumable).")
            break
        prods = fetch_page(page)
        if not prods:
            print(f"  page {page}: empty -> end of catalog")
            break
        rows = [parse_product(p) for p in prods]
        try:
            scriptkit.save("ataka", rows, key_fields=["url"])
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
        with_author += sum(1 for r in rows if r["author"] != "N/A")
        _save_page(page + 1)
        s = rows[0]
        print(f"  page {page}: +{len(rows)} (total {total}, author {with_author}) | "
              f"{s['title'][:26]} | {s['author'][:20]} | ₹{s['price']} | {s['stock']}")
        page += 1
        nap()
    print(f"\nDone. Saved/updated {total} books this session "
          f"({with_author} with an author).")


# ---- probes -------------------------------------------------------------
def cmd_peek(page=1):
    prods = fetch_page(int(page))
    print(f"page {page}: {len(prods)} products\n")
    for p in prods[:5]:
        rec = parse_product(p)
        for k, v in rec.items():
            if k in ("description", "tags"):
                v = (v or "")[:70] + ("…" if len(v or "") > 70 else "")
            print(f"  {k:>12}: {str(v)[:90]}")
        print()


def cmd_count():
    page, total, authors = 1, 0, 0
    while True:
        prods = fetch_page(page)
        if not prods:
            break
        total += len(prods)
        authors += sum(1 for p in prods if parse_product(p)["author"] != "N/A")
        print(f"  page {page}: {len(prods)} (running total {total})")
        page += 1
        nap()
    print(f"\ntotal products: {total} (~{page-1} pages @ {LIMIT})")
    print(f"with author extracted: {authors}/{total}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "peek":
        cmd_peek(arg or 1)
    elif cmd == "count":
        cmd_count()
    else:
        run()