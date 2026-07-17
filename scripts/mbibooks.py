"""
Mathrubhumi Books (mbibooks.com) — major Kerala publisher, WooCommerce/WordPress.

*** PERMISSION NOTE ***
mbibooks.com's robots.txt explicitly disallows AI crawlers (anthropic-ai,
Claude-Web, GPTBot, CCBot, Google-Extended, xAI, DeepSeek) and also disallows
/shop/page/ and /page/. The operator has GRANTED THIS PROJECT PERMISSION to
collect their catalog (confirmed by the site owner). Keep that authorisation on
file. This scraper is nonetheless deliberately gentle:
  * never touches the disallowed /shop/page/ pagination — enumeration goes
    through the Store API instead
  * modest pacing (MB_MIN_DELAY / MB_MAX_DELAY, default 1-2s), single-threaded
  * backs off on 429/503
If the operator withdraws permission or starts blocking, STOP.

WHY TWO PHASES:
  The WooCommerce Store API is open but its `attributes` array is EMPTY for every
  product (verified across 300 products) — it yields only title/category/prices.
  The real bibliographic data lives on the PRODUCT PAGE in custom theme spans:

      <span class="posted_in book_lang" >Language: &nbsp;&nbsp;MALAYALAM</span>
      <span class="posted_in pdt_isbn13" >ISBN 13: 9789376880294</span>
      <span class="posted_in editions"  >Edition: 1</span>
      <span class="posted_in pdt_pubs"  >Publisher: <a ...>Mathrubhumi</a></span>
      <span class="posted_in pdt_pages" >Pages: 110</span>

  (Not a WooCommerce attributes table — a custom `posted_in` span family.)
  ISBNs here are REAL ISBN-13s.

  Phase 1: Store API  -> every product URL + category + price/MRP/discount (cheap)
  Phase 2: product page -> ISBN, author, publisher, pages, language, edition, desc

*** PRICES ARE IN MINOR UNITS (paise) ***
    "prices": {"price":"22900","regular_price":"27000","currency_minor_unit":2}
    -> ₹229.00 (sale) / ₹270.00 (MRP).  Divide by 10**minor_unit or every price
    lands 100x too high.

Run:
  python scripts/mbibooks.py api [page]     -> what the Store API gives (enumeration)
  python scripts/mbibooks.py book <url>     -> full record for one product
  python scripts/mbibooks.py raw <url>      -> DIAGNOSTIC: dump the spec spans
  python scripts/mbibooks.py enumerate      -> phase 1 only
  python scripts/mbibooks.py                -> enumerate + detail crawl
Pace: MB_MIN_DELAY / MB_MAX_DELAY (default 1-2s).  Test batch: MB_LIMIT=30.
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

BASE = "https://www.mbibooks.com"
API = f"{BASE}/wp-json/wc/store/v1/products"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
PER_PAGE = int(os.environ.get("MB_PER_PAGE", "100"))
MIN_DELAY = float(os.environ.get("MB_MIN_DELAY", "1.0"))
MAX_DELAY = float(os.environ.get("MB_MAX_DELAY", "2.0"))
STATE_FILE = os.environ.get("MB_STATE", "/app/scripts/.mbibooks_state.json")
DONE_FILE = os.environ.get("MB_DONE", "/app/scripts/.mbibooks_done.txt")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "ml,en-US;q=0.9,en;q=0.8"})

ISBN_REAL = re.compile(r"^(?:97[89]\d{10}|\d{9}[\dXx])$")


def nap():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def _req(url, as_json=False, params=None):
    for attempt in range(5):
        try:
            r = SESSION.get(url, params=params, timeout=45,
                            headers={"Accept": "application/json"} if as_json else None)
            if r.status_code in (429, 503):
                wait = float(r.headers.get("Retry-After") or 0) or min(180, 15 * (2 ** attempt))
                print(f"   {r.status_code}; backing off {wait:.0f}s ({attempt+1}/5)")
                time.sleep(wait)
                continue
            if r.status_code == 403:
                print("   403 Forbidden — the site is refusing us. If permission was")
                print("   granted, ask them to whitelist this server/UA. Stopping.")
                raise SystemExit(1)
            if r.status_code in (400, 404, 410, 500, 502):
                return [] if as_json else ""
            r.raise_for_status()
            if as_json:
                d = r.json()
                return d if isinstance(d, list) else []
            r.encoding = r.encoding or "utf-8"
            return r.text
        except SystemExit:
            raise
        except json.JSONDecodeError:
            return []
        except Exception as e:
            print(f"   err (try {attempt+1}): {e}")
            time.sleep(min(60, 5 * (2 ** attempt)))
    return [] if as_json else ""


def _clean(v):
    v = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", str(v or ""))
    v = re.sub(r"<[^>]+>", " ", v)
    v = _html.unescape(v)                     # &nbsp; -> \xa0 (labels use &nbsp;&nbsp;)
    v = v.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", v).strip(" :\u00a0\t\n")


def _money(raw, minor):
    try:
        n = int(str(raw).strip())
    except Exception:
        return ""
    if n <= 0:
        return ""
    return f"{n / (10 ** minor):.2f}"


# ---- phase 1: Store API (enumeration + pricing) -------------------------
def api_page(page):
    return _req(API, as_json=True, params={"per_page": PER_PAGE, "page": page})


def api_fields(p):
    """title / category / price / mrp / discount / stock / image from the API."""
    prices = p.get("prices") or {}
    try:
        minor = int(prices.get("currency_minor_unit", 2))
    except Exception:
        minor = 2
    price = _money(prices.get("sale_price") or prices.get("price"), minor)
    regular = _money(prices.get("regular_price"), minor)
    mrp = ""
    try:
        if regular and price and float(regular) > float(price):
            mrp = regular
    except Exception:
        pass
    discount = ""
    try:
        if mrp and price and float(mrp) > float(price) > 0:
            discount = str(round((float(mrp) - float(price)) / float(mrp) * 100))
    except Exception:
        pass
    cats = [_clean(c.get("name")) for c in (p.get("categories") or [])
            if isinstance(c, dict) and c.get("name")]
    imgs = p.get("images") or []
    in_stock = p.get("is_in_stock")
    return {
        "title": _clean(p.get("name")),
        "category": ", ".join(dict.fromkeys(cats)),
        "price": price,
        "mrp": mrp,
        "discount": discount,
        "stock": "In Stock" if (in_stock is None or in_stock) else "Out of Stock",
        "image_url": (imgs[0].get("src") if imgs and isinstance(imgs[0], dict) else "") or "",
        "url": p.get("permalink") or "",
    }


def enumerate_all():
    st = _load_state()
    books = st.get("books", {})          # url -> api fields
    page = st.get("next_page", 1)
    print(f"enumerating via Store API; resuming page {page}, {len(books)} products so far")
    while True:
        prods = api_page(page)
        if not prods:
            print(f"  page {page}: empty -> end of catalog")
            break
        new = 0
        for p in prods:
            f = api_fields(p)
            if f["url"] and f["url"] not in books:
                books[f["url"]] = f
                new += 1
            elif f["url"]:
                books[f["url"]] = f          # refresh price/stock
        print(f"  page {page}: +{new} (total {len(books)})")
        page += 1
        _save_state({"next_page": page, "books": books})
        nap()
    _save_state({"next_page": page, "books": books})
    print(f"\nenumeration done: {len(books)} products")
    return books


# ---- phase 2: product page (the real bibliographic data) ---------------
# Custom theme spans: <span class="posted_in pdt_isbn13" >ISBN 13: 978...</span>
SPAN_RE = re.compile(
    r'<span[^>]*class="[^"]*posted_in[^"]*"[^>]*>(.*?)</span>', re.I | re.S)


def spec_spans(html):
    """{label_lower: value} from every posted_in span ("Label: value")."""
    out = {}
    for m in SPAN_RE.finditer(html):
        t = _clean(m.group(1))
        if not t:
            continue
        lm = re.match(r"([A-Za-z][A-Za-z0-9 .\-]{1,24}?)\s*:\s*(.+)$", t)
        if lm:
            lab, val = lm.group(1).strip().lower(), lm.group(2).strip()
            if lab and val:
                out.setdefault(lab, val)
    return out


def _sp(spans, *labels):
    for want in labels:
        w = want.lower()
        for lab, val in spans.items():
            if lab == w or w in lab or lab in w:
                return val
    return ""


def parse_detail(html, url, base=None):
    base = base or {}
    spans = spec_spans(html)

    title = base.get("title") or ""
    if not title:
        h1 = re.search(r"(?is)<h1[^>]*>\s*(.*?)\s*</h1>", html)
        title = _clean(h1.group(1)) if h1 else ""

    isbn = re.sub(r"[^0-9Xx]", "", _sp(spans, "isbn 13", "isbn13", "isbn"))
    publisher = _sp(spans, "publisher", "publishers")
    pages = re.sub(r"[^\d]", "", _sp(spans, "pages", "page") or "")
    language = _sp(spans, "language", "lang") or "Malayalam"
    edition = _sp(spans, "edition", "editions")
    binding = _sp(spans, "binding", "format")

    # author: an author span, else the /author/ or /genre/ author link
    author = _sp(spans, "author", "authors", "written by")
    if not author:
        am = re.findall(r'href="[^"]*/(?:author|book-author|all-authors)/[^"]*"[^>]*>\s*([^<]{2,60}?)\s*</a>',
                        html, re.I)
        if am:
            author = ", ".join(dict.fromkeys(_clean(a) for a in am if _clean(a)))
    if not author:
        # theme sometimes renders authors as a comma list under the title
        am2 = re.search(r'(?is)<div[^>]*class="[^"]*(?:pdt_author|book_author|author)[^"]*"[^>]*>(.*?)</div>', html)
        if am2:
            author = _clean(am2.group(1))

    # category: from the API (authoritative). If absent, take ONLY the category
    # span inside the product meta — never every /product-category/ link on the
    # page (that would hoover up the whole sidebar menu).
    category = base.get("category") or ""
    if not category:
        cat_span = _sp(spans, "category", "categories")
        if cat_span:
            category = cat_span
    if not category:
        pm = re.search(r'(?is)<div[^>]*class="[^"]*product_meta[^"]*"[^>]*>(.*?)</div>', html)
        if pm:
            cm = re.findall(r'href="[^"]*/product-category/[^"]*"[^>]*>\s*([^<]+?)\s*</a>',
                            pm.group(1), re.I)
            category = ", ".join(dict.fromkeys(_clean(c) for c in cm if _clean(c)))

    # price / mrp / stock: from the API when enumerated; else read the page
    price = base.get("price") or ""
    mrp = base.get("mrp") or ""
    discount = base.get("discount") or ""
    stock = base.get("stock") or ""
    if not price:
        pm = re.search(r'(?is)<p[^>]*class="[^"]*\bprice\b[^"]*"[^>]*>(.*?)</p>', html)
        scope = pm.group(1) if pm else html[:0]
        ins = re.search(r"(?is)<ins[^>]*>(.*?)</ins>", scope)
        dele = re.search(r"(?is)<del[^>]*>(.*?)</del>", scope)

        def _amt(frag):
            t = _clean(frag or "")
            m = re.search(r"([\d,]+(?:\.\d+)?)", t)
            return m.group(1).replace(",", "") if m else ""
        price = _amt(ins.group(1)) if ins else ""
        mrp = _amt(dele.group(1)) if dele else ""
        if not price and scope:
            price = _amt(scope)
        try:
            if mrp and price and float(mrp) > float(price) > 0:
                discount = str(round((float(mrp) - float(price)) / float(mrp) * 100))
        except Exception:
            pass
    if not stock:
        stock = "Out of Stock" if re.search(r"out of stock", html, re.I) else "In Stock"

    img = base.get("image_url") or ""
    if not img:
        im = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html, re.I) \
            or re.search(r'<img[^>]+class="[^"]*wp-post-image[^"]*"[^>]*src="([^"]+)"', html, re.I)
        img = _clean(im.group(1)) if im else ""

    # description tab
    desc = ""
    dm = re.search(r'(?is)<div[^>]*id="tab-description"[^>]*>(.*?)</div>\s*(?:</div>|<div[^>]*class="[^"]*(?:related|upsell))',
                   html)
    if not dm:
        dm = re.search(r'(?is)<div[^>]*class="[^"]*woocommerce-Tabs-panel--description[^"]*"[^>]*>(.*?)</div>', html)
    if dm:
        d = re.sub(r"(?is)<h2[^>]*>.*?</h2>", " ", dm.group(1))
        desc = _clean(d)

    def v(x):
        return x if x else "N/A"

    return {
        "title": v(title),
        "author": v(author),
        "publisher": v(publisher),
        "category": v(category),
        "isbn": v(isbn),
        "isbn_is_real": "yes" if isbn and ISBN_REAL.match(isbn) else "no",
        "pages": v(pages),
        "binding": v(binding),
        "edition": v(edition),
        "language": v(language),
        "price": v(price),
        "mrp": v(mrp),
        "discount": v(discount),
        "stock": v(stock),
        "description": desc or "N/A",
        "url": url,
        "image_url": v(img),
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
        return {"next_page": 1, "books": {}}


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
        return open("/tmp/mbibooks_done.txt", "a", encoding="utf-8")


# ---- run ----------------------------------------------------------------
def run():
    books = enumerate_all()
    done = _load_done()
    fh = _open_done()
    items = list(books.items())
    todo = [u for u, _ in items if u not in done]
    limit = int(os.environ.get("MB_LIMIT", "0"))
    print(f"enriching {len(items)} books ({len(items)-len(todo)} done, {len(todo)} to go)"
          + (f" | LIMIT={limit}" if limit else ""))
    t0, sess, dbfail, isbns = time.time(), 0, 0, 0
    for url, meta in items:
        if url in done:
            continue
        if limit and sess >= limit:
            print(f"  reached MB_LIMIT={limit}; stopping (resumable).")
            break
        html = _req(url)
        if not html:
            fh.write(url + "\n"); fh.flush(); done.add(url)
            continue
        rec = parse_detail(html, url, meta)
        try:
            scriptkit.save("mbibooks", [rec], key_fields=["url"])
        except Exception as e:
            dbfail += 1
            print(f"  !! DB save failed ({dbfail}/5): {e}")
            if dbfail >= 5:
                print("  !! aborting: database unreachable; rerun to resume.")
                break
            time.sleep(10)
            continue
        dbfail = 0
        fh.write(url + "\n"); fh.flush(); done.add(url)
        sess += 1
        if rec["isbn"] != "N/A":
            isbns += 1
        if sess % 25 == 0:
            rate = sess / max(1e-9, time.time() - t0)
            eta = (len(todo) - sess) / max(1e-9, rate) / 3600
            print(f"  {sess}/{len(todo)} | {rate*3600:.0f}/h | ETA {eta:.1f}h | isbn {isbns} | "
                  f"{rec['title'][:20]} | {rec['isbn']} | ₹{rec['price']}/{rec['mrp']}")
        nap()
    fh.close()
    print(f"\nDone. Saved/updated {sess} books ({isbns} with ISBN).")


# ---- diagnostics --------------------------------------------------------
def cmd_api(page=1):
    prods = api_page(int(page))
    print(f"Store API page {page}: {len(prods)} products\n")
    for p in prods[:5]:
        f = api_fields(p)
        print(f"  {f['title'][:34]:<34} | ₹{f['price']}/{f['mrp'] or '-'} "
              f"({f['discount'] or '0'}%) | {f['category'][:18]}")
        print(f"      {f['url']}")


def cmd_book(url):
    if not url.startswith("http"):
        url = f"{BASE}/product/{url.strip('/')}/"
    st = _load_state().get("books", {})
    rec = parse_detail(_req(url), url, st.get(url, {}))
    for k, v in rec.items():
        print(f"  {k:>13}: {str(v)[:100]}")


def cmd_raw(url):
    if not url.startswith("http"):
        url = f"{BASE}/product/{url.strip('/')}/"
    html = _req(url)
    print(f"=== fetched {len(html)} chars ===\n")
    spans = spec_spans(html)
    print("--- posted_in spans parsed ---")
    for k, v in spans.items():
        print(f"   {k:>14} : {v[:60]}")
    print("\n--- PARSED ---")
    cmd_book(url)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else "nishkalankayaaya-sthree"
    if cmd == "api":
        cmd_api(arg if str(arg).isdigit() else 1)
    elif cmd == "book":
        cmd_book(arg)
    elif cmd == "raw":
        cmd_raw(arg)
    elif cmd == "enumerate":
        enumerate_all()
    else:
        run()