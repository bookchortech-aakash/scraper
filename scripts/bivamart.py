"""
Bivamart (bivamart.in) — Bengali bookstore, custom PHP storefront (Martfury
template). Static server-rendered HTML: no JSON API, no JS rendering, no WAF.
Plain requests + regex/parse.

The whole book catalog is enumerable from ONE server-rendered page (no
pagination): the "BOOKS" parent category lists every title:

    /shop?catagory=95        (note the site's own misspelling "catagory")

Each detail page carries a full, self-contained record:
    /product/<slug>--<hexid>
      title (<h1>), price block  Rs.<PRICE> Rs.<MRP> <N> % Off  (selling first,
      MRP struck second), a "Label : Value" spec list (Author, Translator,
      Series Name, Language, Publisher, Published on, No. of Pages, Binding,
      Edition, Illustrations, Cover Picture, ISBN, + any extras like Colours),
      Categories (p.categories a), a Bengali synopsis (div.ps-document in
      #tab-1), and a cover + thumbnail gallery (imgs sharing the product code).

Two resumable phases:
  1) enumerate — fetch catagory=95, collect every /product/ url (dedup)
  2) detail    — fetch each detail page, parse the full record, scriptkit.save
                 (DB-guarded, 5-strike abort)

Run:
  python scripts/bivamart.py listing [catid]      -> product urls on a category page (default 95)
  python scripts/bivamart.py book <url>           -> full parsed record for one book
  python scripts/bivamart.py raw <url>            -> DIAGNOSTIC: dump spec region + parsed
  python scripts/bivamart.py enumerate            -> phase 1 only
  python scripts/bivamart.py                       -> enumerate + detail crawl
Pace: BM_MIN_DELAY / BM_MAX_DELAY (default 1-2s).  Test batch: BM_LIMIT=30.
"""
import html as _html
import json
import os
import random
import re
import sys
import time
from collections import Counter
from urllib.parse import urljoin

import requests

_here = os.path.dirname(os.path.abspath(__file__))
for _d in (_here, os.path.dirname(_here), os.path.dirname(os.path.dirname(_here)), "/app"):
    if os.path.exists(os.path.join(_d, "scriptkit.py")):
        sys.path.insert(0, _d)
        break
import scriptkit

BASE = "https://bivamart.in"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MIN_DELAY = float(os.environ.get("BM_MIN_DELAY", "1.0"))
MAX_DELAY = float(os.environ.get("BM_MAX_DELAY", "2.0"))
URLS_FILE = os.environ.get("BM_URLS", "/app/scripts/.bivamart_urls.json")
DONE_FILE = os.environ.get("BM_DONE", "/app/scripts/.bivamart_done.txt")

# Enumeration category. 95 = "BOOKS" parent = the full ~255-book set on one page.
ENUM_CAT = int(os.environ.get("BM_ENUM_CAT", "95"))

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})

# Every spec label the site prints, in the order we try them. Each value is
# bounded by the *next* known label (so ISBN stops before Colours — no bleed).
# "1st Published on" is listed before "Published on" so the specific one wins.
_LABELS = [
    "Translator", "Author", "Series Name", "Language", "Publisher",
    "1st Published on", "Published on", "No. of Pages", "Binding", "Edition",
    "Illustrations", "Pictures", "Cover Picture", "ISBN", "Colours",
    "No. of Colours", "Weight", "Dimensions", "Country of Origin", "Genre",
    "Format", "Sub Title", "Subtitle",
]
# known label -> canonical column; anything else keeps a snake_cased key
_LABEL_KEY = {
    "author": "author", "translator": "translator", "series name": "series",
    "language": "language", "publisher": "publisher",
    "published on": "published_on", "1st published on": "published_on",
    "no. of pages": "pages", "binding": "binding", "edition": "edition",
    "illustrations": "illustrations", "pictures": "illustrations",
    "cover picture": "cover_picture", "isbn": "isbn",
}
_LABEL_ALT = "|".join(re.escape(l) for l in _LABELS)
# landmarks that also end a spec value / the spec block
_STOP = (r"Rs\.|Quantity|Add to cart|Buy Now|Need Assist|Categories\s*:|"
         r"Description|Specification|Reviews|Report Abuse|Related|You may also")


def nap():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def get(url):
    for attempt in range(5):
        try:
            r = SESSION.get(url, timeout=40)
            if r.status_code in (429, 503):
                wait = float(r.headers.get("Retry-After") or 0) or min(90, 5 * (2 ** attempt))
                print(f"   {r.status_code}; wait {wait:.0f}s ({attempt+1}/5)")
                time.sleep(wait)
                continue
            if r.status_code in (404, 410, 500):
                return ""
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except Exception as e:
            print(f"   err (try {attempt+1}): {e}")
            time.sleep(min(60, 4 * (2 ** attempt)))
    return ""


# ---- text helpers -------------------------------------------------------
def _clean(v):
    v = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", str(v or ""))
    v = _html.unescape(v)
    return re.sub(r"\s+", " ", v).strip(" :\u2013\u00a0\t\n-")


def _text(html):
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    h = re.sub(r"(?i)<br\s*/?>", " ", h)
    h = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", _html.unescape(h)).strip()


def _product_id(url):
    m = re.search(r"([0-9a-fA-F]{10,})$", url)
    return m.group(1) if m else ""


def _num(s):
    m = re.search(r"([\d,]+(?:\.\d+)?)", s or "")
    return m.group(1).replace(",", "") if m else ""


def _snake(label):
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def _clean_isbn(s):
    """Keep the ISBN token only. The live page prints a bare 'Colours' after the
    ISBN (no colon), so the generic label terminator can't catch it — trim to the
    ISBN shape (13/10 digits with hyphens/spaces and X placeholders) or the
    site's sentinels (NA / N/A / Multiple)."""
    if not s:
        return ""
    s = s.strip()
    m = re.match(r"(97[89][\dXx\- ]+[\dXx]|[\dXx][\dXx\- ]{6,}[\dXx])", s)
    if m:
        return m.group(1).strip()
    m = re.match(r"(N/?A|Multiple)\b", s, re.I)
    return m.group(1) if m else s


# ---- phase 1: enumeration ----------------------------------------------
def cat_url(catid):
    return f"{BASE}/shop?catagory={catid}"


def page_products(html):
    """Ordered unique product urls on a shop/category page."""
    urls, seen = [], set()
    for m in re.finditer(r'href="((?:https?://[^"]*)?/product/[^"#?]+)"', html):
        u = urljoin(BASE, _html.unescape(m.group(1))).split("?")[0].split("#")[0]
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def enumerate_all():
    print(f"enumerating category {ENUM_CAT} ({cat_url(ENUM_CAT)})")
    html = get(cat_url(ENUM_CAT))
    urls = page_products(html) if html else []
    if not urls:
        print("  no product urls found — check the page/selector before crawling")
    else:
        cnt = re.search(r"([\d,]+)\s*Products?\s*found", _text(html), re.I)
        claim = cnt.group(1) if cnt else "?"
        print(f"  {len(urls)} product urls (site claims '{claim} Products found')")
    _save_urls({"urls": urls})
    return urls


# ---- phase 2: detail ----------------------------------------------------
def _specs(text):
    """Extract each known spec label's value, bounded by the next known label or
    a page landmark. Anchored on the label vocabulary (not run-together text) so
    values never bleed into each other. Returns {canonical_or_snake_key: value}."""
    out = {}
    for lab in _LABELS:
        m = re.search(
            re.escape(lab) + r"\s*:\s*(.+?)\s*(?=(?:" + _LABEL_ALT + r")\s*:|"
            + _STOP + r"|$)", text)
        if not m:
            continue
        val = _clean(m.group(1))
        if not val:
            continue
        key = _LABEL_KEY.get(lab.lower(), _snake(lab))
        if key not in out:
            out[key] = val
    return out


def _categories(html):
    m = re.search(r'(?is)<p[^>]*class="[^"]*categories[^"]*"[^>]*>(.*?)</p>', html)
    if not m:
        return []
    out, seen = [], set()
    for c in re.findall(r"(?is)<a[^>]*>(.*?)</a>", m.group(1)):
        c = _clean(c).strip(" |")
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _description(html):
    m = re.search(r'(?is)<div[^>]*class="[^"]*ps-document[^"]*"[^>]*>(.*?)</div>', html)
    return _clean(_text(m.group(1))) if m else ""


def _gallery(html):
    """Cover + thumbnails. The product SKU code (e.g. BPHBENREDRHB010278) starts
    with uppercase letters and repeats across the gallery's _001/_002/_003 files
    (in two naming forms). Sidebar covers use different SKUs; nav icons like
    BOOKS.jpg have none. Match the most-frequent SKU; fall back to the first
    plausible product image for books with odd manual filenames."""
    all_src = [urljoin(BASE, _html.unescape(m)) for m in
               re.findall(r'<img[^>]+src="([^"]*/admin-login/img/[^"]+)"', html)]
    codes = re.findall(
        r'/admin-login/img/[^"\']*?([A-Z][A-Za-z0-9&]{4,})_\d{1,3}\.', html)
    out, seen = [], set()
    if codes:
        code = Counter(codes).most_common(1)[0][0]
        for src in all_src:
            if code in src and src not in seen:
                seen.add(src)
                out.append(src)
        if out:
            return out
    for src in all_src:                       # fallback: first real product image
        low = src.lower()
        if any(x in low for x in ("logo", "banner", "/flag/", "payment")):
            continue
        if re.search(r"/[A-Z]{3,}\.jpe?g", src):   # nav icons (BOOKS.jpg, etc.)
            continue
        return [src]
    return []


def parse_detail(html, url):
    text = _text(html)

    h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", html)
    title = _clean(h1.group(1)) if h1 else ""

    rc = re.search(r"\(\s*([\d,]+)\s*Ratings?\s*\)", text, re.I)
    rating_count = rc.group(1).replace(",", "") if rc else ""

    # price block sits between the rating and the first spec label
    seg = text
    ms = re.search(r"Ratings?\s*\)(.*?)(?:Author|Series Name|Language)\s*:", text, re.I | re.S)
    if ms:
        seg = ms.group(1)
    prices = re.findall(r"Rs\.?\s*([\d,]+(?:\.\d+)?)", seg)
    price = _num(prices[0]) if prices else ""            # selling price (first)
    mrp = _num(prices[1]) if len(prices) > 1 else ""      # struck-through MRP
    dm = re.search(r"([\d.]+)\s*%\s*Off", seg, re.I)
    discount = dm.group(1) if dm else ""

    specs = _specs(text)
    cats = _categories(html)
    desc = _description(html)
    imgs = _gallery(html)

    def v(x):
        return x if x else "N/A"

    rec = {
        "url": url,
        "product_id": _product_id(url),
        "title": v(title),
        "author": v(specs.get("author")),
        "translator": v(specs.get("translator")),
        "series": v(specs.get("series")),
        "publisher": v(specs.get("publisher")),
        "language": v(specs.get("language") or "Bengali"),
        "isbn": v(_clean_isbn(specs.get("isbn"))),
        "pages": v(specs.get("pages")),
        "binding": v(specs.get("binding")),
        "edition": v(specs.get("edition")),
        "illustrations": v(specs.get("illustrations")),
        "cover_picture": v(specs.get("cover_picture")),
        "published_on": v(specs.get("published_on")),
        "categories": ", ".join(cats) if cats else "N/A",
        "price": v(price),
        "mrp": v(mrp),
        "discount_pct": v(discount),
        "currency": "INR",
        "rating_count": v(rating_count),
        "description": v(desc),
        "image_url": imgs[0] if imgs else "N/A",
        "image_urls": ", ".join(imgs) if imgs else "N/A",
        "source_url": url,
    }
    # carry any extra spec fields (e.g. Colours) not in the fixed set above
    known = set(rec)
    for k, val in specs.items():
        if k not in known:
            rec[k] = val
    return rec


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
        return json.load(open(URLS_FILE, encoding="utf-8")).get("urls", [])
    except Exception:
        return []


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
        return open("/tmp/bivamart_done.txt", "a", encoding="utf-8")


# ---- run ----------------------------------------------------------------
def run():
    urls = _load_urls() or enumerate_all()
    done = _load_done()
    fh = _open_done()
    todo = [u for u in urls if u not in done]
    limit = int(os.environ.get("BM_LIMIT", "0"))
    print(f"enriching {len(urls)} books ({len(urls)-len(todo)} done, {len(todo)} to go)"
          + (f" | LIMIT={limit}" if limit else ""))
    t0, sess, dbfail = time.time(), 0, 0
    for url in urls:
        if url in done:
            continue
        if limit and sess >= limit:
            print(f"  reached BM_LIMIT={limit}; stopping (resumable).")
            break
        html = get(url)
        if not html:
            print(f"  no HTML; skipping {url}")
            fh.write(url + "\n"); fh.flush(); done.add(url)
            continue
        rec = parse_detail(html, url)
        try:
            scriptkit.save("bivamart", [rec], url=BASE, key_fields=["url"])
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
        if sess % 25 == 0:
            rate = sess / max(1e-9, time.time() - t0)
            eta = (len(todo) - sess) / max(1e-9, rate) / 3600
            print(f"  {sess}/{len(todo)} | {rate*3600:.0f}/h | ETA {eta:.2f}h | "
                  f"{rec['title'][:24]} | {rec['isbn']} | Rs.{rec['price']}/{rec['mrp']} | {rec['pages']}p")
        nap()
    fh.close()
    print(f"\nDone. Saved/updated {sess} books this session.")


# ---- diagnostics --------------------------------------------------------
def cmd_listing(catid):
    html = get(cat_url(catid))
    urls = page_products(html)
    print(f"category {catid}: {len(urls)} product urls")
    for u in urls[:8]:
        print(f"   {u}")


def cmd_book(url):
    rec = parse_detail(get(url), url)
    for k, val in rec.items():
        s = val if isinstance(val, str) else str(val)
        print(f"  {k:>14}: {s[:110]}")


def cmd_raw(url):
    html = get(url)
    print(f"=== fetched {len(html)} chars for {url} ===\n")
    a = re.search(r"(?i)Author\s*:", html)
    start = max(0, (a.start() - 200)) if a else 0
    print("--- RAW HTML (spec region, 3.5k) ---")
    print(html[start:start + 3500])
    print("\n--- TAG-STRIPPED TEXT (first 1500) ---")
    print(_text(html)[:1500])
    print("\n--- PARSED ---")
    cmd_book(url)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else ""
    if cmd == "listing":
        cmd_listing(int(arg) if arg.isdigit() else ENUM_CAT)
    elif cmd == "book":
        cmd_book(arg)
    elif cmd == "raw":
        cmd_raw(arg)
    elif cmd == "enumerate":
        enumerate_all()
    else:
        run()