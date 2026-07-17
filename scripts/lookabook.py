"""
LookaBook (lookabook.in) — MALAYALAM bookstore, WooCommerce/WordPress.

The WooCommerce Store API is OPEN, so this is a SINGLE-PHASE JSON scrape — no
HTML parsing, no detail fetches, no bot handling:
    /wp-json/wc/store/v1/products?per_page=100&page=<N>

VERIFIED FIELD REALITY:
  * short_description carries the FULL SPEC SHEET (<br />-separated):
        Book : ...        Author: ...       Category : Self Help
        ISBN : 9789370986756                Binding : Normal
        Publishing Date : 25-01-2026        Publisher : DC BOOKS
        Edition : 1       Number of pages : 256    Language : Malayalam
    -> real ISBN-13s, publisher, binding, edition, pages, date all AVAILABLE.
  * SPARSE books have only " Category ; Novel" there; for those we fall back to
    the category taxonomy for author/genre.
  * `sku` is always "" (unused by this store).

CATEGORY TAXONOMY is three kinds mixed together; we split them by PARENT id:
    parent 16  -> real genres   (Novel, Stories, Memoir, History, Poems, ...)
    parent 123 -> AUTHORS as categories (Akhil P Dharmajan, AGATHA CHRISTIE, ...)
    others     -> discount buckets ("21% OFF", "23% OFF", "30-40% Off") -> DROPPED
Used as the fallback when a book has no spec sheet.

PRICE: the `prices` object is UNRELIABLE on this store (it reports
on_sale:false / sale_price == regular_price even for discounted books), while
`price_html` shows the truth: "<del>₹180</del> <ins>₹148</ins>". So MRP/sale are
parsed from price_html, with `prices` as fallback.

Run:
  python scripts/lookabook.py peek [page]   -> parse+print first products
  python scripts/lookabook.py count         -> total products + page count
  python scripts/lookabook.py cats          -> the genre/author/junk taxonomy split
  python scripts/lookabook.py               -> full crawl (resumable by page)
Pace: LB_MIN_DELAY / LB_MAX_DELAY (default 0.5-1.2s).  Test cap: LB_MAX_PAGES.
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

BASE = "https://www.lookabook.in"
API = f"{BASE}/wp-json/wc/store/v1/products"
CATS_API = f"{BASE}/wp-json/wc/store/v1/products/categories"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
PER_PAGE = 100
MIN_DELAY = float(os.environ.get("LB_MIN_DELAY", "0.5"))
MAX_DELAY = float(os.environ.get("LB_MAX_DELAY", "1.2"))
MAX_PAGES = int(os.environ.get("LB_MAX_PAGES", "0"))     # 0 = until empty
STATE_FILE = os.environ.get("LB_STATE", "/app/scripts/.lookabook_page.json")

# taxonomy parents (from /products/categories)
GENRE_PARENT = 16      # Malayalam -> real genres
AUTHOR_PARENT = 123    # author    -> author names
JUNK_CAT = re.compile(r"%\s*off|^\d+\s*-\s*\d+%", re.I)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "application/json, */*;q=0.1"})


def nap():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def _get_json(url):
    for attempt in range(6):
        try:
            r = SESSION.get(url, timeout=45)
            if r.status_code in (429, 503):
                wait = float(r.headers.get("Retry-After") or 0) or min(120, 6 * (2 ** attempt))
                print(f"   {r.status_code}; wait {wait:.0f}s ({attempt+1}/6)")
                time.sleep(wait)
                continue
            if r.status_code in (400, 404, 500):
                return []
            r.raise_for_status()
            return r.json()
        except json.JSONDecodeError:
            return []
        except Exception as e:
            print(f"   err (try {attempt+1}): {e}")
            time.sleep(min(60, 5 * (2 ** attempt)))
    return []


def fetch_page(page):
    data = _get_json(f"{API}?per_page={PER_PAGE}&page={page}")
    return data if isinstance(data, list) else []


def fetch_categories():
    out, page = [], 1
    while True:
        data = _get_json(f"{CATS_API}?per_page={PER_PAGE}&page={page}")
        if not isinstance(data, list) or not data:
            break
        out += data
        page += 1
        nap()
    return out


# ---- field extraction ---------------------------------------------------
def _clean(v):
    v = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", str(v or ""))
    v = re.sub(r"(?i)<br\s*/?>", " ", v)
    v = re.sub(r"<[^>]+>", " ", v)
    return re.sub(r"\s+", " ", _html.unescape(v)).strip(" :;\u00a0\t\n")


def _prices_from_html(price_html, prices):
    """price_html is the source of truth: '<del>₹180</del> <ins>₹148</ins>'.
    Returns (price, mrp, discount)."""
    txt = _html.unescape(price_html or "")
    del_m = re.search(r"(?is)<del[^>]*>(.*?)</del>", txt)
    ins_m = re.search(r"(?is)<ins[^>]*>(.*?)</ins>", txt)

    def amt(frag):
        t = re.sub(r"<[^>]+>", "", frag or "")
        m = re.search(r"([\d,]+(?:\.\d+)?)", _html.unescape(t))
        return m.group(1).replace(",", "") if m else ""

    mrp = amt(del_m.group(1)) if del_m else ""
    price = amt(ins_m.group(1)) if ins_m else ""
    if not price:
        amts = [a.replace(",", "") for a in
                re.findall(r"([\d,]+(?:\.\d+)?)", re.sub(r"<[^>]+>", " ", txt))]
        if amts:
            price = amts[-1] if len(amts) > 1 else amts[0]
            if len(amts) > 1 and not mrp:
                mrp = max(amts, key=lambda x: float(x))
    # fallback to the (unreliable) prices object
    if not price and isinstance(prices, dict):
        price = str(prices.get("price") or "").strip()
        reg = str(prices.get("regular_price") or "").strip()
        if reg and price and float(reg or 0) > float(price or 0):
            mrp = reg
    discount = ""
    try:
        if mrp and price and float(mrp) > float(price) > 0:
            discount = str(round((float(mrp) - float(price)) / float(mrp) * 100))
    except Exception:
        pass
    if mrp == price:
        mrp = ""
    return price, mrp, discount


def split_categories(cats):
    """(authors, genres) — split by taxonomy parent; drop the '% OFF' buckets."""
    authors, genres, other = [], [], []
    for c in cats or []:
        if not isinstance(c, dict):
            continue
        name = _clean(c.get("name") or "")
        if not name or JUNK_CAT.search(name):
            continue
        parent = c.get("parent")
        if parent == AUTHOR_PARENT:
            authors.append(name)
        elif parent == GENRE_PARENT:
            genres.append(name)
        else:
            other.append(name)
    if not genres:                 # some books only carry a top-level genre
        genres = [n for n in other if n]
    return authors, genres


# ---- the spec block ------------------------------------------------------
# short_description carries the real spec sheet, <br />-separated, e.g.
#   Book : SANTHOSHATHINTE SAMAVAKYANGAL
#   Author: ASWATHY SREEKANTH          <- note: no space before the colon
#   Category : Self Help
#   ISBN : 9789370986756
#   Binding : Normal
#   Publishing Date : 25-01-2026
#   Publisher : DC BOOKS
#   Edition : 1
#   Number of pages : 256
#   Language : Malayalam
# Sparse books instead have only " Category ; Novel" (note the semicolon).
_SPEC_LABELS = ["Book", "Author", "Authors", "Category", "ISBN", "Binding", "Format",
                "Publishing Date", "Published Date", "Publisher", "Edition",
                "Number of pages", "No of pages", "Pages", "Language"]


def spec_lines(short_html):
    """{label_lower: value} parsed from the <br />-separated spec block."""
    h = short_html or ""
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</?(p|div|span|strong|b|em)[^>]*>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = _html.unescape(h)
    out = {}
    for line in h.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        # "Label : value" or "Label: value" (also tolerate "Label ; value")
        m = re.match(r"([A-Za-z][A-Za-z .]{1,24}?)\s*[:;]\s*(.+)$", line)
        if not m:
            continue
        lab, val = m.group(1).strip().lower(), m.group(2).strip(" .;:")
        if lab and val:
            out[lab] = val
    return out


ISBN_REAL = re.compile(r"^(?:97[89]\d{10}|\d{9}[\dXx])$")


def parse_product(p):
    name = _clean(p.get("name") or "")
    slug = p.get("slug") or ""
    permalink = p.get("permalink") or (f"{BASE}/product/{slug}/" if slug else "")

    # 1) the spec sheet in short_description (authoritative when present)
    spec = spec_lines(p.get("short_description"))

    def s(*labels):
        for lab in labels:
            v = spec.get(lab.lower())
            if v:
                return v
        return ""

    # 2) taxonomy (parent 123 = author, parent 16 = genre) as fallback/extra
    tax_authors, tax_genres = split_categories(p.get("categories"))

    title = s("Book") or name
    author = s("Author", "Authors") or ", ".join(dict.fromkeys(tax_authors))
    isbn = re.sub(r"[^0-9Xx]", "", s("ISBN"))
    publisher = s("Publisher")
    binding = s("Binding", "Format")
    edition = s("Edition")
    pages = re.sub(r"[^\d]", "", s("Number of pages", "No of pages", "Pages") or "")
    pub_date = s("Publishing Date", "Published Date")
    year = ""
    ym = re.search(r"(19|20)\d{2}", pub_date or "")
    if ym:
        year = ym.group(0)
    language = s("Language") or "Malayalam"

    genres = []
    cg = s("Category")
    if cg:
        genres = [g.strip() for g in re.split(r"[,/]", cg) if g.strip()]
    if not genres:
        genres = tax_genres

    desc = _clean(p.get("description") or "")
    # strip the wishlist widget text that WooCommerce appends to description
    desc = re.sub(r"(?i)\s*Add to Wishlist\s*", " ", desc).strip()
    if desc.lower().startswith(name.lower()):        # description repeats the title
        desc = desc[len(name):].strip(" -–:")

    price, mrp, discount = _prices_from_html(p.get("price_html"), p.get("prices"))

    imgs = p.get("images") or []
    image = (imgs[0].get("src") if imgs and isinstance(imgs[0], dict) else "") or ""

    in_stock = p.get("is_in_stock")
    if in_stock is None:
        in_stock = True
    stock = "In Stock" if in_stock else "Out of Stock"

    def v(x):
        return x if x else "N/A"

    return {
        "title": v(title),
        "author": v(author),
        "publisher": v(publisher),
        "category": v(", ".join(dict.fromkeys(genres))),
        "isbn": v(isbn),
        "isbn_is_real": "yes" if isbn and ISBN_REAL.match(isbn) else "no",
        "pages": v(pages),
        "binding": v(binding),
        "edition": v(edition),
        "pub_date": v(pub_date),
        "year": v(year),
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
    total, dbfail, with_author, with_isbn = 0, 0, 0, 0
    while True:
        if MAX_PAGES and page > MAX_PAGES:
            print(f"  reached LB_MAX_PAGES={MAX_PAGES}; stopping (resumable).")
            break
        prods = fetch_page(page)
        if not prods:
            print(f"  page {page}: empty -> end of catalog")
            break
        rows = [parse_product(p) for p in prods]
        try:
            scriptkit.save("lookabook", rows, key_fields=["url"])
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
        with_isbn += sum(1 for r in rows if r["isbn"] != "N/A")
        _save_page(page + 1)
        s = rows[0]
        print(f"  page {page}: +{len(rows)} (total {total}, author {with_author}, isbn {with_isbn}) | "
              f"{s['title'][:22]} | {s['isbn']} | ₹{s['price']}/{s['mrp']}")
        page += 1
        nap()
    print(f"\nDone. Saved/updated {total} books "
          f"({with_author} with author, {with_isbn} with ISBN).")


# ---- probes -------------------------------------------------------------
def cmd_peek(page=1):
    prods = fetch_page(int(page))
    print(f"page {page}: {len(prods)} products\n")
    for p in prods[:5]:
        rec = parse_product(p)
        for k, v in rec.items():
            if k == "description":
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
    print(f"\ntotal products: {total} (~{page-1} pages @ {PER_PAGE})")
    print(f"with author from taxonomy: {authors}/{total}")


def cmd_cats():
    cats = fetch_categories()
    genres = [c for c in cats if c.get("parent") == GENRE_PARENT]
    authors = [c for c in cats if c.get("parent") == AUTHOR_PARENT]
    junk = [c for c in cats if JUNK_CAT.search(_clean(c.get("name") or ""))]
    print(f"{len(cats)} categories total")
    print(f"  genres  (parent {GENRE_PARENT}): {len(genres)}")
    for c in genres[:15]:
        print(f"      {c['name']}  ({c.get('count')})")
    print(f"  authors (parent {AUTHOR_PARENT}): {len(authors)}")
    for c in authors[:10]:
        print(f"      {c['name']}  ({c.get('count')})")
    print(f"  junk ('% OFF' buckets, dropped): {len(junk)}")
    for c in junk[:5]:
        print(f"      {c['name']}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "peek":
        cmd_peek(arg or 1)
    elif cmd == "count":
        cmd_count()
    elif cmd == "cats":
        cmd_cats()
    else:
        run()