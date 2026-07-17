"""
Marina Books (marinabooks.com) — Tamil bookstore, AngularJS SPA front-end.

The homepage/catalog is Angular-rendered (no book list in the landing HTML), and
there is NO sitemap. BUT the grid listing route is fully SERVER-RENDERED and,
by default sort, spans the ENTIRE catalog newest-first:

    /newrelease?showby=grid&sortby=&page=<N>      (~16 books/page, ~6000 pages)

So enumeration walks that route; detail pages are server-rendered too:

    /detailed/<url-encoded-title>?id=<id>         (id = 4x4 digit code, e.g. 1045-8255-9248-0362)

Detail page carries a full record:
    title, ஆசிரியர்:<author>, and a spec table:
      Category | Publication | Format(=binding) | Pages | ISBN | Weight
    plus price:  ₹<MRP> ₹<PRICE>  You Save ₹X (N% OFF)   (MRP first, price second).

Two resumable phases:
  1) enumerate — walk the grid pages, collect every /detailed/?id= url (dedup by id)
  2) detail    — fetch each detail page, parse full record, scriptkit.save (DB-guarded)

Run:
  python scripts/marinabooks.py listing [page]     -> ids found on a grid page (default 1)
  python scripts/marinabooks.py book <id|url>      -> full record for one book
  python scripts/marinabooks.py raw <id|url>       -> DIAGNOSTIC: dump detail HTML/text
  python scripts/marinabooks.py enumerate          -> phase 1 only
  python scripts/marinabooks.py                     -> enumerate + detail crawl
Pace: MB_MIN_DELAY / MB_MAX_DELAY (default 1-2s).  Test batch: MB_LIMIT=30.
Enumeration cap: MB_MAX_PAGE (default 7000, safety stop).
"""
import html as _html
import json
import os
import random
import re
import sys
import time
from urllib.parse import quote, unquote

import requests

_here = os.path.dirname(os.path.abspath(__file__))
for _d in (_here, os.path.dirname(_here), os.path.dirname(os.path.dirname(_here)), "/app"):
    if os.path.exists(os.path.join(_d, "scriptkit.py")):
        sys.path.insert(0, _d)
        break
import scriptkit

BASE = "https://marinabooks.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MIN_DELAY = float(os.environ.get("MB_MIN_DELAY", "1.0"))
MAX_DELAY = float(os.environ.get("MB_MAX_DELAY", "2.0"))
MAX_PAGE = int(os.environ.get("MB_MAX_PAGE", "7000"))
URLS_FILE = os.environ.get("MB_URLS", "/app/scripts/.marina_urls.json")
DONE_FILE = os.environ.get("MB_DONE", "/app/scripts/.marina_done.txt")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

# marinabooks.com serves an incomplete TLS chain that strict clients reject.
# Prefer proper CA verification (certifi if present); if the chain still fails,
# fall back to UNVERIFIED for this public, read-only catalog. VERIFY can be
# forced with MB_VERIFY=1 (strict) or MB_VERIFY=0 (always skip).
try:
    import certifi
    _CA = certifi.where()
except Exception:
    _CA = True
_FORCE = os.environ.get("MB_VERIFY")
_VERIFY = _CA if _FORCE in (None, "", "1") else False
_WARNED = False


def _warn_insecure():
    global _WARNED
    if not _WARNED:
        _WARNED = True
        print("   note: TLS chain not verifiable; continuing UNVERIFIED for this "
              "public read-only site (set MB_VERIFY=1 to force strict).")
    try:
        requests.packages.urllib3.disable_warnings()  # quiet the per-request noise
    except Exception:
        pass

ID_RE = r"\d{4}-\d{4}-\d{4}-\d{4}"


def nap():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def get(url):
    global _VERIFY
    for attempt in range(6):
        try:
            r = SESSION.get(url, timeout=45, verify=_VERIFY)
            if r.status_code in (429, 503):
                wait = float(r.headers.get("Retry-After") or 0) or min(120, 6 * (2 ** attempt))
                print(f"   {r.status_code}; wait {wait:.0f}s ({attempt+1}/6)")
                time.sleep(wait)
                continue
            if r.status_code in (404, 410, 500):
                return ""
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except requests.exceptions.SSLError:
            if _VERIFY is not False and _FORCE != "1":
                _VERIFY = False            # drop to unverified for the rest of the run
                _warn_insecure()
                continue                   # retry this same URL immediately
            print(f"   SSL err (try {attempt+1})")
            time.sleep(min(60, 5 * (2 ** attempt)))
        except Exception as e:
            print(f"   err (try {attempt+1}): {e}")
            time.sleep(min(60, 5 * (2 ** attempt)))
    return ""


# ---- text helpers -------------------------------------------------------
def _clean(v):
    v = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", str(v or ""))
    v = _html.unescape(v)
    return re.sub(r"\s+", " ", v).strip(" :\u00a0\t\n")


def _text(html):
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    h = re.sub(r"(?i)<br\s*/?>", " ", h)
    h = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", _html.unescape(h)).strip()


# ---- phase 1: enumeration ----------------------------------------------
def grid_url(page):
    return f"{BASE}/newrelease?showby=grid&sortby=&page={page}"


def page_books(html):
    """[(url, id), ...] for the /detailed/?id= links on a grid page (dedup by id)."""
    out, seen = [], set()
    for m in re.finditer(r'href="(/detailed/[^"]*?\?id=(' + ID_RE + r'))"', html):
        href, bid = _html.unescape(m.group(1)), m.group(2)
        if bid not in seen:
            seen.add(bid)
            out.append((BASE + href, bid))
    # fallback: bare ?id= occurrences if the href capture missed
    if not out:
        for bid in dict.fromkeys(re.findall(r"[?&]id=(" + ID_RE + r")", html)):
            out.append((f"{BASE}/detailed/book?id={bid}", bid))
    return out


def enumerate_all():
    st = _load_urls()
    books = st.get("books", {})       # id -> url
    page = st.get("next_page", 1)
    empty = st.get("empty", 0)
    print(f"enumerating grid; resuming page {page}, {len(books)} books so far")
    while page <= MAX_PAGE:
        html = get(grid_url(page))
        found = page_books(html) if html else []
        new = 0
        for url, bid in found:
            if bid not in books:
                books[bid] = url
                new += 1
        if not found:
            empty += 1
            print(f"  page {page}: empty ({empty}/3)")
            if empty >= 3:
                print("  3 consecutive empty pages -> end of catalog")
                break
        else:
            empty = 0
            if page % 50 == 0 or new:
                print(f"  page {page}: +{new} (total {len(books)})")
        _save_urls({"next_page": page + 1, "empty": empty, "books": books})
        page += 1
        nap()
    _save_urls({"next_page": page, "empty": empty, "books": books})
    print(f"\nenumeration done: {len(books)} unique books")
    return books


# ---- phase 2: detail ----------------------------------------------------
_LABELS = ["ஆசிரியர்", "Category", "Publication", "Format", "Pages", "ISBN", "Weight"]
_STOP = _LABELS + ["You Save", "Add to Cart", "₹", "Qty", "Delivery"]


def _spec(text, label):
    others = "|".join(re.escape(s) for s in _STOP if s != label)
    m = re.search(re.escape(label) + r"\s*[:\-]?\s*(.+?)\s*(?=" + others + r"|$)", text)
    return _clean(m.group(1)) if m else ""


def parse_detail(html, url):
    bid = ""
    bm = re.search(r"[?&]id=(" + ID_RE + r")", url)
    if bm:
        bid = bm.group(1)
    text = _text(html)

    h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", html)
    title = _clean(h1.group(1)) if h1 else ""
    if not title:
        tt = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        title = _clean(re.split(r"\s*\|\s*", _clean(tt.group(1)))[0]) if tt else ""

    author = _spec(text, "ஆசிரியர்")
    category = _spec(text, "Category")
    publisher = _spec(text, "Publication")
    binding = _spec(text, "Format")
    pages = re.sub(r"[^\d]", "", _spec(text, "Pages"))
    isbn = re.sub(r"[^0-9Xx\-]", "", _spec(text, "ISBN")).strip("-")
    weight = _spec(text, "Weight")

    # price: "₹2000.00 ₹1800.00 ... You Save ₹200 (10% OFF)"  (MRP first, price second).
    # Store the site's EXACT strings (2 decimals as shown); assign mrp=higher, price=lower.
    price = mrp = discount = ""
    pm = re.search(r"₹\s*([\d,]+(?:\.\d+)?)\s*₹\s*([\d,]+(?:\.\d+)?)", text)
    if pm:
        s1, s2 = pm.group(1).replace(",", ""), pm.group(2).replace(",", "")
        mrp, price = (s1, s2) if float(s1) >= float(s2) else (s2, s1)
    else:
        sm = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", text)
        if sm:
            price = sm.group(1).replace(",", "")
    dm = re.search(r"\(\s*(\d+)\s*%\s*OFF\s*\)", text, re.I)
    if dm:
        discount = dm.group(1)

    # description: after "புத்தகம் பற்றி" up to reviews/related
    desc = ""
    dmt = re.search(r"புத்தகம்\s*பற்றி\s*(.+?)(?=உங்கள்\s*கருத்|Related|Prev\s*Next|$)", text)
    if dmt:
        desc = _clean(dmt.group(1))

    img = ""
    om = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html) or \
        re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', html)
    if om:
        img = _clean(om.group(1))
    if not img:
        im = re.search(r'(https?://[^"\']*thumbnail/[^"\']*?' + re.escape(bid) + r'\.jpg)', html)
        if im:
            img = im.group(1)

    def v(x):
        return x if x else "N/A"

    return {
        "title": v(title),
        "author": v(author),
        "publisher": v(publisher),
        "category": v(category),
        "isbn": v(isbn),
        "pages": v(pages),
        "binding": v(binding),
        "item_weight": v(weight),
        "language": "Tamil",
        "price": v(price),
        "mrp": v(mrp),
        "discount": v(discount),
        "description": desc or "N/A",
        "url": url,
        "image_url": img or "N/A",
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
        return open("/tmp/marina_done.txt", "a", encoding="utf-8")


# ---- run ----------------------------------------------------------------
def run():
    books = enumerate_all()
    done = _load_done()
    fh = _open_done()
    items = list(books.items())            # (id, url)
    todo = [(i, u) for i, u in items if i not in done]
    limit = int(os.environ.get("MB_LIMIT", "0"))
    print(f"enriching {len(items)} books ({len(items)-len(todo)} done, {len(todo)} to go)"
          + (f" | LIMIT={limit}" if limit else ""))
    t0, sess, dbfail = time.time(), 0, 0
    for bid, url in items:
        if bid in done:
            continue
        if limit and sess >= limit:
            print(f"  reached MB_LIMIT={limit}; stopping (resumable).")
            break
        html = get(url)
        if not html:
            print(f"  {bid}: no HTML; skipping")
            fh.write(bid + "\n"); fh.flush(); done.add(bid)
            continue
        rec = parse_detail(html, url)
        try:
            scriptkit.save("marinabooks", [rec], key_fields=["url"])
        except Exception as e:
            dbfail += 1
            print(f"  !! DB save failed ({dbfail}/5): {e}")
            if dbfail >= 5:
                print("  !! aborting: database unreachable; rerun to resume.")
                break
            time.sleep(10)
            continue
        dbfail = 0
        fh.write(bid + "\n"); fh.flush(); done.add(bid)
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
def cmd_listing(page):
    html = get(grid_url(page))
    books = page_books(html)
    print(f"grid page {page}: {len(books)} book links")
    for url, bid in books[:8]:
        print(f"   {bid} | {url}")


def _url_from_arg(arg):
    if arg.startswith("http"):
        return arg
    if re.fullmatch(ID_RE, arg):
        return f"{BASE}/detailed/book?id={arg}"
    return f"{BASE}/detailed/{quote(arg)}"


def cmd_book(arg):
    url = _url_from_arg(arg)
    rec = parse_detail(get(url), url)
    for k, val in rec.items():
        s = val if isinstance(val, str) else str(val)
        print(f"  {k:>12}: {s[:100]}")


def cmd_raw(arg):
    url = _url_from_arg(arg)
    html = get(url)
    print(f"=== fetched {len(html)} chars for {url} ===\n")
    a = re.search(r"ஆசிரியர", html)
    start = max(0, (a.start() - 200)) if a else 0
    print("--- RAW HTML (spec region, 3.5k) ---")
    print(html[start:start + 3500])
    print("\n--- TAG-STRIPPED TEXT (first 1500) ---")
    print(_text(html)[:1500])
    print("\n--- PARSED ---")
    cmd_book(arg)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else "1"
    if cmd == "listing":
        cmd_listing(int(arg) if arg.isdigit() else 1)
    elif cmd == "book":
        cmd_book(arg if len(sys.argv) > 2 else "1045-8255-9248-0362")
    elif cmd == "raw":
        cmd_raw(arg if len(sys.argv) > 2 else "1045-8255-9248-0362")
    elif cmd == "enumerate":
        enumerate_all()
    else:
        run()