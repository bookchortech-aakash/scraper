"""
Logili.com (Logili Book House, Guntur) — Telugu bookstore on Infibeam BuildaBazaar.

Server-rendered, no gate. ~15,200 books. This is the richest-fielded site: each
product page has a "Features" block with Title / Author / Publisher / ISBN /
Binding / Number Of Pages / Language, plus List Price (MRP) + Our Price (selling),
stock, category breadcrumb, and description.

ISBN NOTE: the ISBN field EXISTS here (unlike Udumalai/TeluguBooks), but many
values are the store's internal catalog codes (e.g. EMESCO0995) rather than true
ISBN-13s; some are real. We capture the field as-is.

Product URLs: /<category>/<slug>/p-7488847-<id>-cat.html   (7488847 = store id).
Listing paginates: /home-books?page=<N>.

Two resumable phases:
  1) enumerate product URLs by paging /home-books  -> cache (dedup by product id)
  2) fetch each product page -> full record -> save (DB-guarded checkpoint).

Run:
  python scripts/logili.py listing [page]     -> product urls found on a listing page
  python scripts/logili.py book <url|id>      -> full record for one product
  python scripts/logili.py raw <url|id>       -> DIAGNOSTIC: dump Features/price HTML
  python scripts/logili.py enumerate          -> phase 1 only
  python scripts/logili.py                    -> enumerate + detail crawl
Pace: LG_MIN_DELAY / LG_MAX_DELAY (default 0.8-1.6s).  Test batch: LG_LIMIT=30.
Enumeration cap: LG_MAX_PAGE (default 2000 safety stop).
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

BASE = "https://www.logili.com"
STORE = "7488847"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MIN_DELAY = float(os.environ.get("LG_MIN_DELAY", "0.8"))
MAX_DELAY = float(os.environ.get("LG_MAX_DELAY", "1.6"))
MAX_PAGE = int(os.environ.get("LG_MAX_PAGE", "2000"))
URLS_FILE = os.environ.get("LG_URLS", "/app/scripts/.logili_urls.json")
DONE_FILE = os.environ.get("LG_DONE", "/app/scripts/.logili_done.txt")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

PROD_URL = re.compile(
    r'(?:https?://www\.logili\.com)?(/[^"\'\s]+?/p-' + STORE + r'-(\d+)-cat\.html)', re.I)


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
    return re.sub(r"\s+", " ", _html.unescape(v)).strip(" :\u00a0\t\n")


def _text(html):
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    h = re.sub(r"(?i)<br\s*/?>", " ", h)
    h = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", _html.unescape(h)).strip()


# ---- phase 1: enumeration ----------------------------------------------
def page_products(html):
    """[(url, id), ...] for product links on a listing page (dedup by id)."""
    out, seen = [], set()
    for m in PROD_URL.finditer(html):
        path, pid = _html.unescape(m.group(1)), m.group(2)
        if pid in seen:
            continue
        seen.add(pid)
        out.append((BASE + path, pid))
    return out


def enumerate_all():
    st = _load_urls()
    books = st.get("books", {})           # id -> url
    page = st.get("next_page", 1)
    empty = st.get("empty", 0)
    print(f"enumerating /home-books; resuming page {page}, {len(books)} urls so far")
    while page <= MAX_PAGE:
        html = get(f"{BASE}/home-books?page={page}")
        found = page_products(html) if html else []
        new = 0
        for url, pid in found:
            if pid not in books:
                books[pid] = url
                new += 1
        if not found:
            empty += 1
            print(f"  page {page}: empty ({empty}/3)")
            if empty >= 3:
                print("  3 consecutive empty pages -> end of catalog")
                break
        else:
            empty = 0
            if page % 25 == 0 or new:
                print(f"  page {page}: +{new} (total {len(books)})")
        _save_urls({"next_page": page + 1, "empty": empty, "books": books})
        page += 1
        nap()
    _save_urls({"next_page": page, "empty": empty, "books": books})
    print(f"\nenumeration done: {len(books)} unique products")
    return books


# ---- phase 2: detail ----------------------------------------------------
def _feature(html, label):
    """Value from a Features <li>: <label> Label </label>: VALUE </li>."""
    m = re.search(r"<label>\s*" + re.escape(label) + r"\s*</label>\s*:?\s*([^<]+?)\s*</li>",
                  html, re.I | re.S)
    if m:
        return _clean(m.group(1))
    # fallback: bare "Label : value" up to a tag/newline
    m = re.search(re.escape(label) + r"\s*</label>\s*:?\s*([^<\n]+)", html, re.I)
    return _clean(m.group(1)) if m else ""


def parse_detail(html, url):
    pid = ""
    pm = re.search(r"p-" + STORE + r"-(\d+)-cat", url)
    if pm:
        pid = pm.group(1)

    title = _feature(html, "Title")
    if not title:
        h1 = re.search(r"(?is)<h1[^>]*>\s*(.*?)\s*</h1>", html)
        title = _clean(h1.group(1)) if h1 else ""

    author = _feature(html, "Author")
    publisher = _feature(html, "Publisher")
    isbn = _feature(html, "ISBN")
    binding = _feature(html, "Binding")
    pages = re.sub(r"[^\d]", "", _feature(html, "Number Of Pages") or _feature(html, "Pages"))
    language = _feature(html, "Language") or "Telugu"
    pubdate = _feature(html, "Published Date")

    # prices: <label>List Price:</label><span> ... Rs.</span>800  (nested spans)
    def _price(lbl):
        m = re.search(r"<label>\s*" + lbl + r"\s*:?\s*</label>.*?Rs\.?\s*</span>\s*([\d,]+(?:\.\d+)?)",
                      html, re.I | re.S) \
            or re.search(lbl + r"\s*:?\s*</label>.*?Rs\.?\s*([\d,]+(?:\.\d+)?)", html, re.I | re.S)
        return m.group(1).replace(",", "") if m else ""
    mrp = _price("List Price")
    price = _price("Our Price") or mrp
    discount = ""
    try:
        if mrp and price and float(mrp) > float(price) > 0:
            discount = str(round((float(mrp) - float(price)) / float(mrp) * 100))
    except Exception:
        pass

    stock = "Out of Stock" if re.search(r"Out\s*Of\s*Stock", html, re.I) else "In Stock"

    # category: breadcrumb "Books > Telugu > <cat>", else product-url first segment
    category = ""
    bc = re.findall(r'href="/home-books[^"]*"[^>]*>\s*([^<]+?)\s*</a>', html, re.I)
    if bc:
        cats = [_clean(x) for x in bc if _clean(x) and _clean(x).lower() not in ("books", "home")]
        category = cats[-1] if cats else ""
    if not category:
        seg = re.search(r"logili\.com/([^/]+)/[^/]+/p-" + STORE, url)
        if seg and seg.group(1).lower() != "books":
            category = seg.group(1).replace("-", " ").title()

    # description: the "Available in:" block up to "Check for shipping"/Features
    desc = ""
    ai = html.find("Available in")
    if ai >= 0:
        cut = min([i for i in (html.find("Check for", ai), html.find("Features", ai),
                               html.find("You may", ai)) if i > 0] or [ai + 4000])
        desc = _text(html[ai:cut])
        desc = re.sub(r"^\s*Available in\s*:?\s*", "", desc, flags=re.I).strip()

    im = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html, re.I) \
        or re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', html, re.I)
    image = _clean(im.group(1)) if im else ""

    def v(x):
        return x if x else "N/A"

    ym = re.search(r"(1[89]\d{2}|20\d{2})", pubdate or "")
    year = ym.group(1) if ym else ""

    return {
        "title": v(title),
        "author": v(re.sub(r"\s*\(Author\)", "", author)),
        "publisher": v(publisher),
        "category": v(category),
        "isbn": v(isbn),
        "pages": v(pages),
        "binding": v(binding),
        "language": v(language),
        "pub_date": v(pubdate),
        "year": v(year),
        "price": v(price),
        "mrp": v(mrp),
        "discount": v(discount),
        "stock": stock,
        "description": desc or "N/A",
        "url": url,
        "image_url": image or "N/A",
    }


# ---- state --------------------------------------------------------------
def _save_urls(state):
    try:
        os.makedirs(os.path.dirname(URLS_FILE) or ".", exist_ok=True)
        tmp = URLS_FILE + ".tmp"
        json.dump(state, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, URLS_FILE)
    except Exception as e:
        print(f"   urls save warn: {e}")


def _load_urls():
    try:
        return json.load(open(URLS_FILE, encoding="utf-8"))
    except Exception:
        return {"next_page": 1, "empty": 0, "books": {}}


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
        return open("/tmp/logili_done.txt", "a", encoding="utf-8")


# ---- run ----------------------------------------------------------------
def run():
    books = enumerate_all()
    done = _load_done()
    fh = _open_done()
    items = list(books.items())            # (id, url)
    todo = [(i, u) for i, u in items if i not in done]
    limit = int(os.environ.get("LG_LIMIT", "0"))
    print(f"enriching {len(items)} books ({len(items)-len(todo)} done, {len(todo)} to go)"
          + (f" | LIMIT={limit}" if limit else ""))
    t0, sess, dbfail = time.time(), 0, 0
    for pid, url in items:
        if pid in done:
            continue
        if limit and sess >= limit:
            print(f"  reached LG_LIMIT={limit}; stopping (resumable).")
            break
        html = get(url)
        if not html:
            fh.write(pid + "\n"); fh.flush(); done.add(pid)
            continue
        rec = parse_detail(html, url)
        try:
            scriptkit.save("logili", [rec], key_fields=["url"])
        except Exception as e:
            dbfail += 1
            print(f"  !! DB save failed ({dbfail}/5): {e}")
            if dbfail >= 5:
                print("  !! aborting: database unreachable; rerun to resume.")
                break
            time.sleep(10)
            continue
        dbfail = 0
        fh.write(pid + "\n"); fh.flush(); done.add(pid)
        sess += 1
        if sess % 25 == 0:
            rate = sess / max(1e-9, time.time() - t0)
            eta = (len(todo) - sess) / max(1e-9, rate) / 3600
            print(f"  {sess}/{len(todo)} | {rate*3600:.0f}/h | ETA {eta:.1f}h | "
                  f"{rec['title'][:22]} | {rec['isbn']} | ₹{rec['price']}/{rec['mrp']} | {rec['pages']}p")
        nap()
    fh.close()
    print(f"\nDone. Saved/updated {sess} books this session.")


# ---- diagnostics --------------------------------------------------------
def _url_from_arg(arg):
    if arg.startswith("http"):
        return arg
    return f"{BASE}/books/x/p-{STORE}-{arg}-cat.html"   # id -> canonical


def cmd_listing(page):
    html = get(f"{BASE}/home-books?page={page}")
    prods = page_products(html)
    tot = re.search(r"Showing\s+([\d,]+)\s+Results", _text(html))
    print(f"listing page {page}: total ~{tot.group(1) if tot else '?'} | {len(prods)} product urls")
    for url, pid in prods[:6]:
        print(f"   {pid} | {url}")


def cmd_book(arg):
    url = _url_from_arg(arg)
    rec = parse_detail(get(url), url)
    for k, v in rec.items():
        s = v if isinstance(v, str) else str(v)
        print(f"  {k:>12}: {s[:100]}")


def cmd_raw(arg):
    url = _url_from_arg(arg)
    html = get(url)
    print(f"=== fetched {len(html)} chars for {url} ===\n")
    fi = html.find("Features")
    if fi >= 0:
        print("--- RAW HTML (Features region, 2k) ---")
        print(html[fi:fi + 2000])
    pi = html.find("List Price")
    if pi >= 0:
        print("\n--- RAW HTML (price region, 600) ---")
        print(html[pi:pi + 600])
    print("\n--- PARSED ---")
    cmd_book(arg)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else "1"
    if cmd == "listing":
        cmd_listing(int(arg) if arg.isdigit() else 1)
    elif cmd == "book":
        cmd_book(arg)
    elif cmd == "raw":
        cmd_raw(arg)
    elif cmd == "enumerate":
        enumerate_all()
    else:
        run()