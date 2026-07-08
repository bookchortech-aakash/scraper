"""
Suvadi Books (suvadibooks.com) — Tamil bookstore, custom Django storefront.

Server-rendered, no gate/WAF. robots.txt allows crawling and advertises the
sitemap. Book pages are /book/<slug> (slug sometimes carries a numeric suffix,
sometimes not -> enumerate by slug, never id-iteration).

Detail page is self-contained (unlike Noolulagam, PRICE + MRP are BOTH here):
  title, author(s), publisher(s), category(s), ₹price ₹mrp (N% Off),
  and a labelled spec block: Edition / Published On (year) / ISBN / Pages /
  Format (= binding), plus description + stock state (OUT OF STOCK / COMING SOON).
ISBN/Pages are sometimes just "-". Language defaults to Tamil (English titles
live under /english-books/, still /book/<slug> pages).

Two resumable phases:
  1) enumerate — sitemap.xml (auto-handles sitemapindex + .gz); if it yields
     nothing, fall back to crawling /category/<slug>?page=N (25/page).
  2) detail    — fetch /book/<slug>, parse full record, scriptkit.save (DB-guarded)

Run:
  python scripts/suvadibooks.py sitemap            -> how many /book/ urls the sitemap yields
  python scripts/suvadibooks.py listing <cat>      -> a category page (default: novels)
  python scripts/suvadibooks.py book <slug|url>    -> full record for one book
  python scripts/suvadibooks.py raw <slug|url>     -> DIAGNOSTIC: dump product-block HTML
  python scripts/suvadibooks.py enumerate          -> phase 1 only
  python scripts/suvadibooks.py                     -> enumerate + detail crawl
Pace: SV_MIN_DELAY / SV_MAX_DELAY (default 1-2s).  Test batch: SV_LIMIT=30.
"""
import gzip
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

BASE = "https://suvadibooks.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MIN_DELAY = float(os.environ.get("SV_MIN_DELAY", "1.0"))
MAX_DELAY = float(os.environ.get("SV_MAX_DELAY", "2.0"))
URLS_FILE = os.environ.get("SV_URLS", "/app/scripts/.suvadi_urls.json")
DONE_FILE = os.environ.get("SV_DONE", "/app/scripts/.suvadi_done.txt")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


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
            if r.status_code in (404, 410, 500):
                return ""
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except Exception as e:
            print(f"   err (try {attempt+1}): {e}")
            time.sleep(min(60, 5 * (2 ** attempt)))
    return ""


def get_xml(url):
    """Fetch XML, transparently gunzipping .gz sitemaps."""
    for attempt in range(5):
        try:
            r = SESSION.get(url, timeout=45)
            if r.status_code in (429, 503):
                time.sleep(min(120, 6 * (2 ** attempt)))
                continue
            if r.status_code in (404, 410, 500):
                return ""
            r.raise_for_status()
            data = r.content
            if url.endswith(".gz") or data[:2] == b"\x1f\x8b":
                try:
                    data = gzip.decompress(data)
                except Exception:
                    pass
            return data.decode("utf-8", "replace")
        except Exception as e:
            print(f"   xml err (try {attempt+1}): {e}")
            time.sleep(min(60, 5 * (2 ** attempt)))
    return ""


# ---- text helpers -------------------------------------------------------
def _clean(v):
    v = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", str(v or ""))
    v = _html.unescape(v)
    return re.sub(r"\s+", " ", v).strip(" :\u00a0\t\n-")


def _text(html):
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    h = re.sub(r"(?i)<br\s*/?>", " ", h)
    h = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", _html.unescape(h)).strip()


# ---- phase 1: enumeration -----------------------------------------------
def _locs(xml):
    return [_clean(m) for m in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml, re.I)]


def sitemap_book_urls():
    """All /book/<slug> URLs from sitemap.xml (handles sitemapindex + .gz)."""
    root = get_xml(f"{BASE}/sitemap.xml")
    if not root:
        return []
    locs = _locs(root)
    is_index = ("<sitemapindex" in root.lower()) or (
        locs and not any("/book/" in l for l in locs)
        and all(l.lower().endswith((".xml", ".xml.gz")) or "sitemap" in l.lower() for l in locs))
    urls = []
    if is_index:
        for child in locs:
            urls += [l for l in _locs(get_xml(child)) if "/book/" in l]
            nap()
    else:
        urls += [l for l in locs if "/book/" in l]
    return list(dict.fromkeys(urls))


def _category_slugs():
    home = get(f"{BASE}/")
    return list(dict.fromkeys(re.findall(r"/category/([a-z0-9\-]+)", home)))


def crawl_categories():
    """Fallback: walk every /category/<slug>?page=N, dedup /book/ slugs."""
    slugs = {}
    cats = _category_slugs() or ["novels"]
    print(f"  fallback crawl over {len(cats)} categories")
    for cat in cats:
        page, empty = 1, 0
        while True:
            html = get(f"{BASE}/category/{cat}?page={page}")
            found = re.findall(r'href="/book/([^"?#/]+)"', html) or re.findall(r"/book/([^\"'?#/]+)", html)
            new = 0
            for s in found:
                if s not in slugs:
                    slugs[s] = 1
                    new += 1
            if not found:
                empty += 1
                if empty >= 1:
                    break
            print(f"    {cat} p{page}: +{new} (total {len(slugs)})")
            page += 1
            nap()
    return [f"{BASE}/book/{s}" for s in slugs]


# ---- phase 2: detail ----------------------------------------------------
_SPEC_LABELS = ["Edition", "Published On", "ISBN", "Pages", "Format"]


def _spec(text, label):
    others = "|".join(re.escape(x) for x in _SPEC_LABELS + ["About the book", "Buy Now", "Related Books"] if x != label)
    m = re.search(re.escape(label) + r"\s*[:\-]?\s*(.+?)\s*(?=" + others + r"|$)", text)
    val = _clean(m.group(1)) if m else ""
    return "" if val in ("-", "") else val


def _names(scope_html, path):
    """Linked names under /<path>/<slug> in the product block (dedup, in order)."""
    out = []
    for m in re.finditer(r'href="/' + path + r'/[^"]+"[^>]*>\s*([^<]+?)\s*</a>', scope_html):
        n = _clean(m.group(1))
        if n and n not in out:
            out.append(n)
    return out


def parse_detail(html, url):
    slug = url.rstrip("/").split("/book/")[-1].split("/")[0]

    # scope to the product block: <h1> .. "Related Books" (keeps footer's
    # publisher links + related-book links out of author/publisher/category)
    h1m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", html)
    start = h1m.start() if h1m else 0
    cut = html.find("Related Books")
    region = html[start:(cut if cut != -1 else len(html))]
    rtext = _text(region)

    # title
    title = _clean(h1m.group(1)) if h1m else ""
    # page <title> = "Title - Author - Publisher | ..." (backup for title/author/pub)
    tt = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    tparts = [p.strip() for p in re.split(r"\s[-|]\s", _clean(tt.group(1)))] if tt else []
    if not title and tparts:
        title = tparts[0]

    authors = _names(region, "authors")
    publishers = _names(region, "publishers")
    categories = _names(region, "category")
    if not authors and len(tparts) > 1:
        authors = [tparts[1]]
    if not publishers and len(tparts) > 2:
        publishers = [tparts[2]]

    # price / mrp / discount:  "₹247 ₹260 (5% Off)"  or single "₹300"  or "₹0"
    price = mrp = discount = ""
    pm = re.search(r"₹\s*([\d,]+)\s*₹\s*([\d,]+)\s*\(\s*(\d+)\s*%\s*Off\s*\)", rtext, re.I)
    if pm:
        price, mrp, discount = pm.group(1).replace(",", ""), pm.group(2).replace(",", ""), pm.group(3)
    else:
        sm = re.search(r"₹\s*([\d,]+)", rtext)
        price = sm.group(1).replace(",", "") if sm else ""

    edition = _spec(rtext, "Edition")
    ym = re.search(r"\d{4}", _spec(rtext, "Published On"))
    year = ym.group(0) if ym else ""
    isbn = re.sub(r"[^0-9Xx]", "", _spec(rtext, "ISBN"))
    pages = re.sub(r"[^\d]", "", _spec(rtext, "Pages"))
    binding = _spec(rtext, "Format")

    # description: text between "About the book" and the first spec ("Edition"),
    # with the "Book Highlights / Combo Books" scaffolding stripped from the front
    desc = ""
    if "About the book" in rtext:
        d = rtext.split("About the book", 1)[1]
        d = re.split(r"Edition\s*[:\-]|Buy Now|ADD TO CART", d)[0]
        d = re.sub(r"^\s*(?:Book Highlights|Combo Books|[-•·\s])+", "", d).strip()
        desc = d

    # stock (scoped to product block, so related-book badges don't leak)
    if re.search(r"coming soon", rtext, re.I) or price == "0":
        stock = "Coming Soon"
    elif re.search(r"out of stock", rtext, re.I):
        stock = "Out of Stock"
    else:
        stock = "In Stock"

    img = ""
    om = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html) or \
        re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', html)
    if om:
        img = _clean(om.group(1))

    def v(x):
        return x if x else "N/A"

    return {
        "title": v(title),
        "author": v(", ".join(authors)),
        "publisher": v(", ".join(publishers)),
        "category": v(", ".join(categories)),
        "price": v(price),
        "mrp": v(mrp),
        "discount": v(discount),
        "isbn": v(isbn),
        "pages": v(pages),
        "edition": v(edition),
        "year": v(year),
        "binding": v(binding),
        "language": "Tamil",
        "stock": v(stock),
        "description": desc or "N/A",
        "url": f"{BASE}/book/{slug}",
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
        return {"urls": []}


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
        return open("/tmp/suvadi_done.txt", "a", encoding="utf-8")


# ---- phases -------------------------------------------------------------
def enumerate_all():
    st = _load_urls()
    urls = st.get("urls", [])
    if urls:
        print(f"resuming with {len(urls)} book urls already enumerated")
        return urls
    print("enumerating via sitemap.xml ...")
    urls = sitemap_book_urls()
    print(f"  sitemap yielded {len(urls)} /book/ urls")
    if len(urls) < 50:
        print("  sitemap too small/empty -> category-crawl fallback")
        urls = crawl_categories()
    urls = list(dict.fromkeys(urls))
    _save_urls({"urls": urls})
    print(f"enumeration done: {len(urls)} book urls")
    return urls


def run():
    urls = enumerate_all()
    done = _load_done()
    fh = _open_done()
    todo = [u for u in urls if u not in done]
    limit = int(os.environ.get("SV_LIMIT", "0"))
    print(f"enriching {len(urls)} books ({len(urls)-len(todo)} done, {len(todo)} to go)"
          + (f" | LIMIT={limit}" if limit else ""))
    t0, sess, dbfail = time.time(), 0, 0
    for u in urls:
        if u in done:
            continue
        if limit and sess >= limit:
            print(f"  reached SV_LIMIT={limit}; stopping (resumable).")
            break
        html = get(u)
        if not html:
            print(f"  {u}: no HTML (404?); skipping")
            fh.write(u + "\n"); fh.flush(); done.add(u)
            continue
        rec = parse_detail(html, u)
        try:
            scriptkit.save("suvadibooks", [rec], key_fields=["url"])
        except Exception as e:
            dbfail += 1
            print(f"  !! DB save failed ({dbfail}/5): {e}")
            if dbfail >= 5:
                print("  !! aborting: database unreachable; rerun to resume.")
                break
            time.sleep(10)
            continue
        dbfail = 0
        fh.write(u + "\n"); fh.flush(); done.add(u)
        sess += 1
        if sess % 25 == 0:
            rate = sess / max(1e-9, time.time() - t0)
            eta = (len(todo) - sess) / max(1e-9, rate) / 3600
            print(f"  {sess}/{len(todo)} | {rate*3600:.0f}/h | ETA {eta:.1f}h | "
                  f"{rec['title'][:22]} | ₹{rec['price']}/{rec['mrp']} | {rec['isbn']} | {rec['pages']}p")
        nap()
    fh.close()
    print(f"\nDone. Saved/updated {sess} books this session.")


# ---- diagnostics --------------------------------------------------------
def cmd_sitemap():
    urls = sitemap_book_urls()
    print(f"sitemap -> {len(urls)} /book/ urls; first 8:")
    for u in urls[:8]:
        print("  ", u)


def cmd_listing(cat):
    html = get(f"{BASE}/category/{cat}")
    t = _text(html)
    tot = re.search(r"of\s+([\d,]+)\s+items", t)
    slugs = list(dict.fromkeys(re.findall(r'href="/book/([^"?#/]+)"', html) or re.findall(r"/book/([^\"'?#/]+)", html)))
    print(f"/category/{cat}: total ~{tot.group(1) if tot else '?'} | page 1 links: {len(slugs)}")
    for s in slugs[:6]:
        print("  ", s)


def cmd_book(arg):
    url = arg if arg.startswith("http") else f"{BASE}/book/{arg.rstrip('/').split('/book/')[-1]}"
    rec = parse_detail(get(url), url)
    for k, val in rec.items():
        s = val if isinstance(val, str) else str(val)
        print(f"  {k:>12}: {s[:100]}")


def cmd_raw(arg):
    url = arg if arg.startswith("http") else f"{BASE}/book/{arg.rstrip('/').split('/book/')[-1]}"
    html = get(url)
    print(f"=== fetched {len(html)} chars for {url} ===\n")
    h1m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", html)
    start = h1m.start() if h1m else 0
    cut = html.find("Related Books")
    region = html[start:(cut if cut != -1 else min(len(html), start + 4500))]
    print("--- RAW HTML (product block, 4.5k) ---")
    print(region[:4500])
    print("\n--- TAG-STRIPPED TEXT (product block) ---")
    print(_text(region)[:1500])
    print("\n--- PARSED ---")
    cmd_book(arg)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else "novels"
    if cmd == "sitemap":
        cmd_sitemap()
    elif cmd == "listing":
        cmd_listing(arg)
    elif cmd == "book":
        cmd_book(arg if len(sys.argv) > 2 else "honey-trap-20021")
    elif cmd == "raw":
        cmd_raw(arg if len(sys.argv) > 2 else "honey-trap-20021")
    elif cmd == "enumerate":
        enumerate_all()
    else:
        run()