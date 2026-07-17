"""
BookCarry (bookcarry.com) — Malayalam bookstore, WooCommerce/WordPress.

SINGLE-PHASE JSON scrape via the open WooCommerce Store API — no HTML parsing,
no detail fetches, no bot handling:
    /wp-json/wc/store/v1/products?per_page=100&page=<N>
robots.txt is fully permissive (Disallow: empty) and advertises a Yoast sitemap.

Catalog: ~1,890 books. Product URLs are /book/<slug>/  (note: /book/, not /product/).

*** PRICES ARE IN MINOR UNITS (paise) ***
    "prices": {"price":"32900", "currency_minor_unit":2}   ->  ₹329.00
Reading `price` naively would inflate every price 100x. We divide by
10**currency_minor_unit (defaulting to 2 if the field is missing).

ATTRIBUTES are proper WooCommerce taxonomies. Measured coverage over 400 books:
    Author     396/400  (99%)   pa_book_author
    Publisher  324/400  (81%)   pa_publisher
    Pages      259/400  (65%)   custom (no taxonomy)
    ISBN        24/400  ( 6%)   sparse, but REAL — captured wherever present
Attributes are matched BY NAME (never by position/id), so a new attribute or a
reordering can't silently write the wrong value into a column.

Categories come from the proper taxonomy (Horror, Novels, Thriller, Memoirs,
Non Fiction, Psychology, Study, ...).

Run:
  python scripts/bookcarry.py peek [page]   -> parse+print first products
  python scripts/bookcarry.py attrs         -> audit attribute names + coverage
  python scripts/bookcarry.py               -> full crawl (resumable by page)
Pace: BC_MIN_DELAY / BC_MAX_DELAY (default 1-2s).  Test cap: BC_MAX_PAGES.
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

BASE = "https://bookcarry.com"
API = f"{BASE}/wp-json/wc/store/v1/products"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
PER_PAGE = int(os.environ.get("BC_PER_PAGE", "100"))
MIN_DELAY = float(os.environ.get("BC_MIN_DELAY", "1.0"))
MAX_DELAY = float(os.environ.get("BC_MAX_DELAY", "2.0"))
MAX_PAGES = int(os.environ.get("BC_MAX_PAGES", "0"))     # 0 = until empty
STATE_FILE = os.environ.get("BC_STATE", "/app/scripts/.bookcarry_page.json")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "application/json, */*;q=0.1"})

ISBN_REAL = re.compile(r"^(?:97[89]\d{10}|\d{9}[\dXx])$")


def nap():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def fetch_page(page):
    url = f"{API}?per_page={PER_PAGE}&page={page}"
    for attempt in range(5):
        try:
            r = SESSION.get(url, timeout=45)
            if r.status_code in (429, 503):
                wait = float(r.headers.get("Retry-After") or 0) or min(120, 10 * (2 ** attempt))
                print(f"   {r.status_code}; wait {wait:.0f}s ({attempt+1}/5)")
                time.sleep(wait)
                continue
            if r.status_code in (400, 404, 500):
                return []
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
        except Exception as e:
            print(f"   err (try {attempt+1}): {e}")
            time.sleep(min(60, 5 * (2 ** attempt)))
    return []


# ---- helpers ------------------------------------------------------------
def _clean(v):
    v = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", str(v or ""))
    v = re.sub(r"(?i)<br\s*/?>", " ", v)
    v = re.sub(r"<[^>]+>", " ", v)
    return re.sub(r"\s+", " ", _html.unescape(v)).strip(" :\u00a0\t\n")


def _body(html):
    h = re.sub(r"(?i)<br\s*/?>", "\n", html or "")
    h = re.sub(r"(?i)</(p|div|li|h\d)>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = _html.unescape(h)
    h = re.sub(r"[ \t\u00a0]+", " ", h)
    return "\n".join(ln.strip() for ln in h.split("\n") if ln.strip())


def attr_map(p):
    """{attribute_name_lower: 'term1, term2'} — matched BY NAME, not position."""
    out = {}
    for a in p.get("attributes") or []:
        if not isinstance(a, dict):
            continue
        name = _clean(a.get("name") or "").lower()
        terms = [_clean(t.get("name")) for t in (a.get("terms") or [])
                 if isinstance(t, dict) and t.get("name")]
        if name and terms:
            out[name] = ", ".join(dict.fromkeys(terms))
    return out


def _attr(amap, *names):
    for want in names:
        w = want.lower()
        for name, val in amap.items():
            if name == w or w in name or name in w:
                return val
    return ""


def _money(raw, minor):
    """Store API returns MINOR units: '32900' with minor_unit 2 -> '329.00'."""
    if raw in (None, "", "0") and raw != 0:
        return ""
    try:
        n = int(str(raw).strip())
    except Exception:
        try:
            return f"{float(raw):.2f}"
        except Exception:
            return ""
    if n <= 0:
        return ""
    return f"{n / (10 ** minor):.2f}"


def parse_product(p):
    name = _clean(p.get("name") or "")
    permalink = p.get("permalink") or ""
    amap = attr_map(p)

    author = _attr(amap, "author", "authors", "book author")
    publisher = _attr(amap, "publisher", "publishers")
    pages = re.sub(r"[^\d]", "", _attr(amap, "pages", "page", "no of pages") or "")
    isbn_raw = _attr(amap, "isbn", "isbn 13", "isbn-13")
    isbn = re.sub(r"[^0-9Xx]", "", isbn_raw)
    binding = _attr(amap, "binding", "format", "cover")
    language = _attr(amap, "language") or "Malayalam"

    cats = [_clean(c.get("name")) for c in (p.get("categories") or [])
            if isinstance(c, dict) and c.get("name")]

    # PRICES ARE IN MINOR UNITS (paise) -> scale by 10**minor_unit
    prices = p.get("prices") or {}
    try:
        minor = int(prices.get("currency_minor_unit", 2))
    except Exception:
        minor = 2
    price = _money(prices.get("price"), minor)
    regular = _money(prices.get("regular_price"), minor)
    sale = _money(prices.get("sale_price"), minor)
    mrp = ""
    if regular and price:
        try:
            if float(regular) > float(price):
                mrp = regular
        except Exception:
            pass
    if sale and regular and not mrp:
        try:
            if float(regular) > float(sale):
                mrp, price = regular, sale
        except Exception:
            pass
    discount = ""
    try:
        if mrp and price and float(mrp) > float(price) > 0:
            discount = str(round((float(mrp) - float(price)) / float(mrp) * 100))
    except Exception:
        pass

    in_stock = p.get("is_in_stock")
    if in_stock is None:
        in_stock = True
    stock = "In Stock" if in_stock else "Out of Stock"

    imgs = p.get("images") or []
    image = (imgs[0].get("src") if imgs and isinstance(imgs[0], dict) else "") or ""

    # short_description holds the blurb on this store; description is an SEO line
    desc = _body(p.get("short_description")) or _body(p.get("description"))
    desc = re.sub(r"(?i)For details regarding International shipping.*$", "", desc).strip()

    def v(x):
        return x if x else "N/A"

    return {
        "title": v(name),
        "author": v(author),
        "publisher": v(publisher),
        "category": v(", ".join(dict.fromkeys(cats))),
        "isbn": v(isbn),
        "isbn_is_real": "yes" if isbn and ISBN_REAL.match(isbn) else "no",
        "pages": v(pages),
        "binding": v(binding),
        "language": v(language),
        "price": v(price),
        "mrp": v(mrp),
        "discount": v(discount),
        "stock": stock,
        "sku": v(_clean(p.get("sku") or "")),
        "description": desc or "N/A",
        "url": v(permalink),
        "image_url": v(image),
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
    print(f"crawling Store API from page {page} (per_page {PER_PAGE})")
    total, dbfail, isbns, pubs = 0, 0, 0, 0
    while True:
        if MAX_PAGES and page > MAX_PAGES:
            print(f"  reached BC_MAX_PAGES={MAX_PAGES}; stopping (resumable).")
            break
        prods = fetch_page(page)
        if not prods:
            print(f"  page {page}: empty -> end of catalog")
            break
        rows = [parse_product(p) for p in prods]
        try:
            scriptkit.save("bookcarry", rows, key_fields=["url"])
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
        isbns += sum(1 for r in rows if r["isbn"] != "N/A")
        pubs += sum(1 for r in rows if r["publisher"] != "N/A")
        _save_page(page + 1)
        s = rows[0]
        print(f"  page {page}: +{len(rows)} (total {total}, isbn {isbns}, pub {pubs}) | "
              f"{s['title'][:22]} | {s['author'][:16]} | ₹{s['price']}")
        page += 1
        nap()
    print(f"\nDone. Saved/updated {total} books ({isbns} with ISBN, {pubs} with publisher).")


# ---- probes -------------------------------------------------------------
def cmd_peek(page=1):
    prods = fetch_page(int(page))
    print(f"page {page}: {len(prods)} products\n")
    for p in prods[:5]:
        rec = parse_product(p)
        for k, v in rec.items():
            if k == "description":
                v = (v or "")[:70].replace("\n", " ") + ("…" if len(v or "") > 70 else "")
            print(f"  {k:>13}: {str(v)[:90]}")
        print()


def cmd_attrs(pages=4):
    """Audit attribute names + coverage (validates the by-name mapping)."""
    from collections import Counter
    names, seen = Counter(), 0
    for page in range(1, int(pages) + 1):
        prods = fetch_page(page)
        if not prods:
            break
        for p in prods:
            seen += 1
            for a in (p.get("attributes") or []):
                if isinstance(a, dict) and a.get("name"):
                    names[_clean(a["name"])] += 1
        nap()
    print(f"scanned {seen} books; attribute names in use:")
    for n, c in names.most_common():
        print(f"   {n:<20} {c:>4}  ({c/max(1,seen)*100:.0f}%)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "peek":
        cmd_peek(arg or 1)
    elif cmd == "attrs":
        cmd_attrs(arg or 4)
    else:
        run()