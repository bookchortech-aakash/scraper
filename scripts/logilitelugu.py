"""
Logili Telugu Books (logilitelugubooks.com) — Laravel storefront, ~14,726 books.

NOTE: this is a DIFFERENT site from logili.com (which is Infibeam/BuildaBazaar).
Same business, new platform. Server-rendered — no JS/API needed.

  category pages : /books/<category-slug>?page=<N>     (20/page, ~130 categories)
  product pages  : /book/<title-slug>-<author-slug>    (slug = title + author!)

IMPORTANT: /books/all is NOT the full catalog — it only exposes 73 books. The
14,726 books live across the ~130 category pages, so enumeration walks those
(which also gives each book its category for free).

Detail pages carry a full BOOK DETAILS table (verified markup):
    <div class="lgl-book-details__row">
      <div class="lgl-book-details__label">ISBN</div>
      <div class="lgl-book-details__value">  MANIMN7023  </div>
    </div>
labels: Title / Author / Publisher / ISBN / Binding / Number Of Pages /
        Published Date / Language / Availability
plus the description in <div class="lgl-product-desc">.
Rows are parsed GENERICALLY (label -> value), so new labels are picked up too.

ISBN NOTE: values are often the publisher's catalog code (e.g. MANIMN7023) rather
than a true ISBN-13 — same as logili.com. Captured as-is; `isbn_is_real` flags
values that match a real ISBN-10/13 pattern.

Two resumable phases:
  1) enumerate — walk every /books/<cat>?page=N, collect /book/<slug> + category
                 + the listing's title/author/price/stock
  2) detail    — fetch each /book/<slug>, parse BOOK DETAILS + description, save.

Run:
  python scripts/logilitelugu.py cats              -> discover the ~130 categories
  python scripts/logilitelugu.py listing <cat>     -> parse one category page
  python scripts/logilitelugu.py book <slug>       -> full record for one book
  python scripts/logilitelugu.py raw <slug>        -> DIAGNOSTIC: dump details rows
  python scripts/logilitelugu.py enumerate         -> phase 1 only
  python scripts/logilitelugu.py                   -> enumerate + detail crawl
Pace: LT_MIN_DELAY / LT_MAX_DELAY (default 0.8-1.6s).  Test batch: LT_LIMIT=30.
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

BASE = "https://logilitelugubooks.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MIN_DELAY = float(os.environ.get("LT_MIN_DELAY", "0.8"))
MAX_DELAY = float(os.environ.get("LT_MAX_DELAY", "1.6"))
STATE_FILE = os.environ.get("LT_STATE", "/app/scripts/.logilitelugu_state.json")
DONE_FILE = os.environ.get("LT_DONE", "/app/scripts/.logilitelugu_done.txt")
MAX_CAT_PAGES = int(os.environ.get("LT_MAX_CAT_PAGES", "120"))   # safety stop per category

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "te,en-US;q=0.9,en;q=0.8"})

# non-category /books/ routes to skip during discovery
SKIP_CATS = {"all", "new-arrivals-1"}


def nap():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def get(url):
    for attempt in range(6):
        try:
            r = SESSION.get(url, timeout=45)
            if r.status_code in (429, 503):
                wait = float(r.headers.get("Retry-After") or 0) or min(120, 6 * (2 ** attempt))
                print(f"   {r.status_code}; wait {wait:.0f}s ({attempt+1}/6)")
                time.sleep(wait)
                continue
            # a 500 here means a bad slug/dead page (Laravel error page) -> skip fast
            if r.status_code in (404, 410, 500, 502):
                return ""
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except Exception as e:
            print(f"   err (try {attempt+1}): {e}")
            time.sleep(min(60, 5 * (2 ** attempt)))
    return ""


def _clean(v):
    v = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", str(v or ""))
    v = re.sub(r"<[^>]+>", " ", v)
    return re.sub(r"\s+", " ", _html.unescape(v)).strip(" :\u00a0\t\n")


def _text(html):
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</(p|div|li|h\d|td|tr)>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = _html.unescape(h)
    h = re.sub(r"[ \t\u00a0]+", " ", h)
    return "\n".join(ln.strip() for ln in h.split("\n") if ln.strip())


# ---- phase 1: categories + listings -------------------------------------
def discover_categories():
    """[(slug, name)] for every /books/<slug> category link on the homepage."""
    html = get(f"{BASE}/")
    out, seen = [], set()
    for m in re.finditer(r'href="[^"]*?/books/([a-z0-9\-_]+)"[^>]*>\s*([^<]{2,60}?)\s*</a>', html, re.I):
        slug, name = m.group(1).lower(), _clean(m.group(2))
        if slug in seen or slug in SKIP_CATS or not name:
            continue
        seen.add(slug)
        out.append((slug, name))
    return out


CARD_RE = re.compile(r'/book/([A-Za-z0-9\-_%]+)', re.I)


def parse_listing(html):
    """[(slug, title, author, price, stock)] from a category page's cards.
    Segments the HTML by each /book/<slug> anchor so fields can't bleed across."""
    order, first = [], {}
    for m in CARD_RE.finditer(html):
        s = m.group(1)
        if s not in first:
            first[s] = m.start()
            order.append(s)
    cards = []
    for i, slug in enumerate(order):
        start = first[slug]
        end = first[order[i + 1]] if i + 1 < len(order) else min(len(html), start + 3000)
        seg = html[start:end]
        segt = _text(seg)
        title = ""
        tm = re.search(r'/book/' + re.escape(slug) + r'"[^>]*>\s*(?:<[^>]+>\s*)*([^<]{2,120})', html[start - 200:end])
        if tm:
            title = _clean(tm.group(1))
        am = re.search(r"\bBy\s+([^\n₹]{2,80})", segt)
        author = _clean(am.group(1)) if am else ""
        pm = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", segt)
        price = pm.group(1).replace(",", "") if pm else ""
        stock = "Out of Stock" if re.search(r"out of stock", segt, re.I) else "In Stock"
        cards.append((slug, title, author, price, stock))
    return cards


def enumerate_all():
    st = _load_state()
    cats_done = set(st.get("cats_done", []))
    books = st.get("books", {})          # slug -> {category, title, author, price, stock}
    cats = st.get("cats") or [list(c) for c in discover_categories()]
    st["cats"] = cats
    print(f"{len(cats)} categories ({len(cats_done)} done, {len(books)} books so far)")
    for slug, name in cats:
        if slug in cats_done:
            continue
        page, added, empty = 1, 0, 0
        while page <= MAX_CAT_PAGES:
            html = get(f"{BASE}/books/{slug}?page={page}")
            cards = parse_listing(html) if html else []
            new = 0
            for bslug, title, author, price, stock in cards:
                if bslug not in books:
                    books[bslug] = {"category": name, "title": title, "author": author,
                                    "price": price, "stock": stock}
                    new += 1
                    added += 1
                else:
                    # book in several categories -> keep them all
                    c = books[bslug].get("category", "")
                    if name and name not in c.split(", "):
                        books[bslug]["category"] = (c + ", " + name).strip(", ")
            if not cards:
                empty += 1
                if empty >= 1:
                    break
            page += 1
            nap()
        cats_done.add(slug)
        _save_state({"cats": cats, "cats_done": sorted(cats_done), "books": books})
        print(f"  [{len(cats_done)}/{len(cats)}] {name[:30]:<30} +{added} (total {len(books)})")
    print(f"\nenumeration done: {len(books)} unique books")
    return books


# ---- phase 2: detail ----------------------------------------------------
ROW_RE = re.compile(
    r'<div[^>]*class="[^"]*lgl-book-details__label[^"]*"[^>]*>(.*?)</div>\s*'
    r'<div[^>]*class="[^"]*lgl-book-details__value[^"]*"[^>]*>(.*?)</div>',
    re.I | re.S)

ISBN_REAL = re.compile(r"^(?:97[89]\d{10}|\d{9}[\dXx])$")


def detail_rows(html):
    """{label_lower: value} for every BOOK DETAILS row (generic: any label)."""
    out = {}
    for m in ROW_RE.finditer(html):
        lab = _clean(m.group(1)).lower()
        val = _clean(m.group(2))
        if lab and val:
            out[lab] = val
    return out


def parse_detail(html, slug, base=None):
    base = base or {}
    rows = detail_rows(html)

    def row(*names):
        for n in names:
            v = rows.get(n.lower())
            if v:
                return v
        return ""

    title = row("title") or base.get("title", "")
    if not title:
        h1 = re.search(r"(?is)<h1[^>]*>\s*(.*?)\s*</h1>", html)
        title = _clean(h1.group(1)) if h1 else ""

    author = row("author", "authors") or base.get("author", "")
    publisher = row("publisher", "publishers")
    isbn = row("isbn", "isbn-13", "isbn13")
    binding = row("binding", "format")
    pages = re.sub(r"[^\d]", "", row("number of pages", "no of pages", "pages") or "")
    pub_date = row("published date", "publication date", "year")
    year = ""
    ym = re.search(r"(19|20)\d{2}", pub_date or "")
    if ym:
        year = ym.group(0)
    language = row("language") or "Telugu"
    availability = row("availability")

    # category: the breadcrumb/eyebrow tag above the title, else from the feed
    category = base.get("category", "")
    cm = re.search(r'/books/([a-z0-9\-_]+)"[^>]*>\s*([^<]{2,50}?)\s*</a>\s*</div>', html, re.I)
    if not category and cm:
        category = _clean(cm.group(2))

    # price / stock
    price = base.get("price", "")
    if not price:
        pm = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", _text(html))
        price = pm.group(1).replace(",", "") if pm else ""
    stock = availability or base.get("stock", "")
    if not stock:
        stock = "Out of Stock" if re.search(r"out of stock", html, re.I) else "In Stock"

    # description (anchor on the exact class; lgl-product-desc-title must NOT match)
    desc = ""
    dm = re.search(r'<div[^>]*class="[^"]*\blgl-product-desc\b(?!-)[^"]*"[^>]*>(.*?)</div>\s*(?:</div>|<div|$)',
                   html, re.I | re.S)
    if dm:
        desc = _clean(re.sub(r"(?i)</p>", "\n", dm.group(1)))
    if re.match(r"^\s*(?:No description available|About this book)\s*$", desc, re.I):
        desc = ""

    img = ""
    im = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html, re.I)
    if im:
        img = _clean(im.group(1))

    def v(x):
        return x if x else "N/A"

    return {
        "title": v(title),
        "author": v(author),
        "publisher": v(publisher),
        "category": v(category),
        "isbn": v(isbn),
        "isbn_is_real": "yes" if isbn and ISBN_REAL.match(re.sub(r"[^0-9Xx]", "", isbn)) else "no",
        "pages": v(pages),
        "binding": v(binding),
        "language": v(language),
        "pub_date": v(pub_date),
        "year": v(year),
        "price": v(price),
        "stock": v(stock),
        "description": desc or "N/A",
        "url": f"{BASE}/book/{slug}",
        "image_url": img or "N/A",
    }


# ---- state --------------------------------------------------------------
def _save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        json.dump(st, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"   state save warn: {e}")


def _load_state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {"cats": [], "cats_done": [], "books": {}}


def _load_done():
    try:
        return set(x for x in open(DONE_FILE, encoding="utf-8").read().split("\n") if x)
    except FileNotFoundError:
        return set()


def _open_done():
    try:
        os.makedirs(os.path.dirname(DONE_FILE) or ".", exist_ok=True)
        return open(DONE_FILE, "a", encoding="utf-8")
    except Exception:
        return open("/tmp/logilitelugu_done.txt", "a", encoding="utf-8")


# ---- run ----------------------------------------------------------------
def run():
    books = enumerate_all()
    done = _load_done()
    fh = _open_done()
    items = list(books.items())
    todo = [s for s, _ in items if s not in done]
    limit = int(os.environ.get("LT_LIMIT", "0"))
    print(f"enriching {len(items)} books ({len(items)-len(todo)} done, {len(todo)} to go)"
          + (f" | LIMIT={limit}" if limit else ""))
    t0, sess, dbfail, isbns, skipped = time.time(), 0, 0, 0, 0
    for slug, meta in items:
        if slug in done:
            continue
        if limit and sess >= limit:
            print(f"  reached LT_LIMIT={limit}; stopping (resumable).")
            break
        html = get(f"{BASE}/book/{slug}")
        if not html:
            skipped += 1
            fh.write(slug + "\n"); fh.flush(); done.add(slug)
            continue
        rec = parse_detail(html, slug, meta)
        try:
            scriptkit.save("logilitelugu", [rec], key_fields=["url"])
        except Exception as e:
            dbfail += 1
            print(f"  !! DB save failed ({dbfail}/5): {e}")
            if dbfail >= 5:
                print("  !! aborting: database unreachable; rerun to resume.")
                break
            time.sleep(10)
            continue
        dbfail = 0
        fh.write(slug + "\n"); fh.flush(); done.add(slug)
        sess += 1
        if rec["isbn"] != "N/A":
            isbns += 1
        if sess % 25 == 0:
            rate = sess / max(1e-9, time.time() - t0)
            eta = (len(todo) - sess) / max(1e-9, rate) / 3600
            print(f"  {sess}/{len(todo)} | {rate*3600:.0f}/h | ETA {eta:.1f}h | isbn {isbns} | "
                  f"{rec['title'][:20]} | {rec['isbn']} | ₹{rec['price']} | {rec['pages']}p")
        nap()
    fh.close()
    print(f"\nDone. Saved/updated {sess} books ({isbns} with ISBN, {skipped} dead pages).")


# ---- diagnostics --------------------------------------------------------
def cmd_cats():
    cats = discover_categories()
    print(f"{len(cats)} categories:")
    for slug, name in cats[:25]:
        print(f"   {slug:<38} {name}")
    if len(cats) > 25:
        print(f"   ... +{len(cats)-25} more")


def cmd_listing(cat):
    html = get(f"{BASE}/books/{cat}?page=1")
    cards = parse_listing(html)
    tot = re.search(r"of\s+([\d,]+)\s+books", _text(html))
    print(f"/books/{cat}: total ~{tot.group(1) if tot else '?'} | {len(cards)} cards")
    for slug, title, author, price, stock in cards[:6]:
        print(f"   {title[:32]:<32} | {author[:22]:<22} | ₹{price:<7} | {stock}")
        print(f"      {slug}")


def cmd_book(slug):
    slug = slug.rstrip("/").split("/book/")[-1].split("/")[0]
    rec = parse_detail(get(f"{BASE}/book/{slug}"), slug, {})
    for k, v in rec.items():
        print(f"  {k:>13}: {str(v)[:100]}")


def cmd_raw(slug):
    slug = slug.rstrip("/").split("/book/")[-1].split("/")[0]
    html = get(f"{BASE}/book/{slug}")
    print(f"=== fetched {len(html)} chars ===\n")
    rows = detail_rows(html)
    print("--- BOOK DETAILS rows parsed ---")
    for k, v in rows.items():
        print(f"   {k:>18} : {v[:70]}")
    print("\n--- PARSED RECORD ---")
    cmd_book(slug)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else "novels"
    if cmd == "cats":
        cmd_cats()
    elif cmd == "listing":
        cmd_listing(arg)
    elif cmd == "book":
        cmd_book(arg if len(sys.argv) > 2 else "niseedhi-naadam-mamilla-koteswara-rao")
    elif cmd == "raw":
        cmd_raw(arg if len(sys.argv) > 2 else "niseedhi-naadam-mamilla-koteswara-rao")
    elif cmd == "enumerate":
        enumerate_all()
    else:
        run()