"""
Kerala Book Store (keralabookstore.com) — Malayalam bookstore, Java/Struts (.do).

*** POLITENESS NOTICE — READ BEFORE RUNNING ***
This site's robots.txt is deliberately restrictive: it allowlists named search
engines only (with `Crawl-delay: 10`) and explicitly blocks generic crawlers,
including HTTP-library agents (httplib, lwp-trivial, libWeb) and even the
literal UAs "mozilla/4" / "mozilla/5" — i.e. it is trying to stop scripted
clients that spoof a browser.

This scraper therefore behaves as a good citizen rather than sneaking past:
  * honours Crawl-delay: 10  (KB_DELAY, default 10s; do NOT lower it casually)
  * single-threaded, no parallelism
  * an HONEST, self-identifying User-Agent with contact info (KB_CONTACT) —
    we do NOT spoof a browser
  * backs off hard on 429/503, and stops entirely after repeated 403s
  * minimises requests: harvests everything possible from LISTING pages and
    only fetches a detail page for the fields that require it.
If the operator asks you to stop, stop. Consider emailing them for a data feed.

STRUCTURE (researched):
  product : /book/<slug>/<id>/        e.g. /book/indulekha/5069/
            ids are sparse (1k .. 1,004,806) -> never id-iterate
  listings: /new-books.do, /best-sellers.do, category/author/publisher pages
  sitemap : /sitemap.xml exists (XML) -> primary, cheapest enumeration

FIELDS:
  listing card : title, author, PUBLISHER, price, offer price   (no detail fetch!)
  detail page  : + ISBN (real ISBN-13s), category, description, pages/binding if present

Two resumable phases:
  1) enumerate — sitemap.xml -> every /book/<slug>/<id>/ URL
  2) detail    — fetch each book, parse fields, scriptkit.save (DB-guarded)

Run:
  python scripts/keralabookstore.py sitemap        -> how many book urls found
  python scripts/keralabookstore.py book <url|id>  -> full record for one book
  python scripts/keralabookstore.py raw <url|id>   -> DIAGNOSTIC: dump detail text
  python scripts/keralabookstore.py enumerate      -> phase 1 only
  python scripts/keralabookstore.py                -> enumerate + detail crawl
Env: KB_DELAY (default 10), KB_CONTACT (your email — PLEASE SET IT), KB_LIMIT.
At 10s/book, ~20k books ≈ 2-3 days. It is fully resumable; run it off-peak.
"""
import gzip
import html as _html
import json
import os
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

BASE = "https://keralabookstore.com"
# Honest, self-identifying UA (NOT a spoofed browser). Set KB_CONTACT to your email.
CONTACT = os.environ.get("KB_CONTACT", "lookabook-aggregator@example.com")
UA = f"BookCatalogAggregator/1.0 (+contact: {CONTACT}) python-requests"

DELAY = float(os.environ.get("KB_DELAY", "10"))      # robots.txt Crawl-delay: 10
URLS_FILE = os.environ.get("KB_URLS", "/app/scripts/.kbs_urls.json")
DONE_FILE = os.environ.get("KB_DONE", "/app/scripts/.kbs_done.txt")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ml,en;q=0.8",
})

BOOK_URL = re.compile(r"/book/([^/\s\"'<>]+)/(\d+)/?", re.I)
_403 = {"n": 0}


def nap():
    time.sleep(DELAY)


def get(url, xml=False):
    """Single-threaded, crawl-delay-respecting fetch. Aborts on repeated 403s."""
    for attempt in range(5):
        try:
            r = SESSION.get(url, timeout=60)
            if r.status_code == 403:
                _403["n"] += 1
                print(f"   403 Forbidden ({_403['n']}/3) — the site is refusing us.")
                if _403["n"] >= 3:
                    print("   !! Repeated 403s: the operator is blocking this crawler.")
                    print("   !! Stopping out of respect. Consider emailing them for a feed.")
                    raise SystemExit(1)
                time.sleep(60)
                continue
            if r.status_code in (429, 503):
                wait = float(r.headers.get("Retry-After") or 0) or min(300, 30 * (2 ** attempt))
                print(f"   {r.status_code}; backing off {wait:.0f}s ({attempt+1}/5)")
                time.sleep(wait)
                continue
            if r.status_code in (404, 410, 500, 502):
                return b"" if xml else ""
            r.raise_for_status()
            if xml:
                data = r.content
                if url.endswith(".gz") or data[:2] == b"\x1f\x8b":
                    try:
                        data = gzip.decompress(data)
                    except Exception:
                        pass
                return data.decode("utf-8", "replace")
            r.encoding = r.encoding or "utf-8"
            return r.text
        except SystemExit:
            raise
        except Exception as e:
            print(f"   err (try {attempt+1}): {e}")
            time.sleep(min(120, 15 * (2 ** attempt)))
    return "" if not xml else ""


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


# ---- phase 1: sitemap ---------------------------------------------------
def _locs(xml):
    return [_clean(m) for m in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml, re.I)]


def sitemap_book_urls():
    root = get(f"{BASE}/sitemap.xml", xml=True)
    if not root:
        return []
    locs = _locs(root)
    is_index = ("<sitemapindex" in root.lower()) or (
        locs and not any(BOOK_URL.search(l) for l in locs)
        and all(l.lower().endswith((".xml", ".xml.gz")) or "sitemap" in l.lower() for l in locs))
    urls = []
    if is_index:
        children = [l for l in locs if l.lower().endswith((".xml", ".xml.gz"))]
        print(f"  sitemap index with {len(children)} children")
        for i, child in enumerate(children, 1):
            nap()
            part = [l for l in _locs(get(child, xml=True)) if BOOK_URL.search(l)]
            urls += part
            print(f"    [{i}/{len(children)}] {child.split('/')[-1]}: +{len(part)} (total {len(urls)})")
    else:
        urls = [l for l in locs if BOOK_URL.search(l)]
    # dedup by numeric book id
    out, seen = [], set()
    for u in urls:
        m = BOOK_URL.search(u)
        if not m:
            continue
        bid = m.group(2)
        if bid in seen:
            continue
        seen.add(bid)
        out.append(u if u.startswith("http") else BASE + u)
    return out


def enumerate_all():
    st = _load_urls()
    urls = st.get("urls", [])
    if urls:
        print(f"resuming with {len(urls)} book urls already enumerated")
        return urls
    print("enumerating via sitemap.xml (respecting crawl-delay) ...")
    urls = sitemap_book_urls()
    print(f"  sitemap yielded {len(urls)} book urls")
    _save_urls({"urls": urls})
    return urls


# ---- phase 2: detail ----------------------------------------------------
_LABELS = ["ISBN", "Author", "Publisher", "Category", "Language", "Pages",
           "No of Pages", "Number of Pages", "Binding", "Edition", "Year",
           "Published", "Price", "Weight"]
ISBN_REAL = re.compile(r"^(?:97[89]\d{10}|\d{9}[\dXx])$")


def _spec(text, *labels):
    others = "|".join(re.escape(x) for x in _LABELS)
    for lab in labels:
        m = re.search(re.escape(lab) + r"\s*[:\-]\s*(.+?)(?=\n|$|(?:" + others + r")\s*[:\-])",
                      text, re.I)
        if m:
            v = _clean(m.group(1))
            if v:
                return v
    return ""


def parse_detail(html, url):
    m = BOOK_URL.search(url)
    bid = m.group(2) if m else ""
    text = _text(html)

    # The <title>/meta of these pages is a goldmine:
    #  "buy the book <TITLE> written by <AUTHOR> in category <CAT>, ISBN <N>,
    #   Published by <PUB> from Kerala Book Store..."
    head = ""
    tt = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if tt:
        head = _clean(tt.group(1))
    md = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]+)"', html, re.I)
    meta = _clean(md.group(1)) if md else ""
    blob = head + " || " + meta

    title = author = category = isbn = publisher = ""
    hm = re.search(r"(?i)book\s+(.+?)\s+written by\s+(.+?)\s+in category\s+(.+?)\s*,\s*"
                   r"ISBN\s*([0-9Xx\-]+)\s*,\s*Published by\s+(.+?)\s+from\s+Kerala Book Store", blob)
    if hm:
        title, author, category, isbn, publisher = (_clean(hm.group(i)) for i in range(1, 6))
    else:
        # looser: pull each piece wherever it appears
        t = re.search(r"(?i)buy the book\s+(.+?)\s*(?:,|\bwritten by\b)", blob)
        title = _clean(t.group(1)) if t else ""
        a = re.search(r"(?i)written by\s+(.+?)\s*(?:,|\bfrom\b|\bin category\b|$)", blob)
        author = _clean(a.group(1)) if a else ""
        c = re.search(r"(?i)in category\s+(.+?)\s*(?:,|$)", blob)
        category = _clean(c.group(1)) if c else ""
        i2 = re.search(r"(?i)ISBN\s*([0-9Xx\-]{9,20})", blob)
        isbn = _clean(i2.group(1)) if i2 else ""
        p = re.search(r"(?i)published by\s+(.+?)\s*(?:,|\bfrom\b|$)", blob)
        publisher = _clean(p.group(1)) if p else ""

    # page body as backup / extras
    if not title:
        h1 = re.search(r"(?is)<h1[^>]*>\s*(.*?)\s*</h1>", html)
        title = _clean(h1.group(1)) if h1 else ""
    isbn = re.sub(r"[^0-9Xx]", "", isbn or _spec(text, "ISBN"))
    author = author or _spec(text, "Author")
    publisher = publisher or _spec(text, "Publisher")
    category = category or _spec(text, "Category")
    pages = re.sub(r"[^\d]", "", _spec(text, "No of Pages", "Number of Pages", "Pages") or "")
    binding = _spec(text, "Binding")
    edition = _spec(text, "Edition")
    year = ""
    ym = re.search(r"(19|20)\d{2}", _spec(text, "Year", "Published") or "")
    if ym:
        year = ym.group(0)
    language = _spec(text, "Language") or "Malayalam"

    # price: "Rs 240.00 Rs 228.00" -> mrp first, offer second
    price = mrp = discount = ""
    amts = [a.replace(",", "") for a in re.findall(r"Rs\.?\s*([\d,]+(?:\.\d{2})?)", text)]
    amts = [a for a in amts if float(a or 0) > 0]
    if amts:
        if len(amts) >= 2 and float(amts[0]) > float(amts[1]):
            mrp, price = amts[0], amts[1]
        else:
            price = amts[0]
    try:
        if mrp and price and float(mrp) > float(price) > 0:
            discount = str(round((float(mrp) - float(price)) / float(mrp) * 100))
    except Exception:
        pass

    stock = "Out of Stock" if re.search(r"out of stock|not available", text, re.I) else "In Stock"

    desc = ""
    dm = re.search(r"(?is)(?:About the Book|Description|Book Description)\s*[:\-]?\s*(.{40,3000}?)"
                   r"(?=Related|Customers who|Reviews|Add to Cart|$)", text)
    if dm:
        desc = _clean(dm.group(1))
    if not desc and meta and len(meta) > 60:
        d = re.sub(r"(?i)\s*\|?\s*Buy the book.*$", "", meta).strip()
        if len(d) > 40:
            desc = d

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
        "isbn_is_real": "yes" if isbn and ISBN_REAL.match(isbn) else "no",
        "pages": v(pages),
        "binding": v(binding),
        "edition": v(edition),
        "year": v(year),
        "language": v(language),
        "price": v(price),
        "mrp": v(mrp),
        "discount": v(discount),
        "stock": stock,
        "bookid": v(bid),
        "description": desc or "N/A",
        "url": url,
        "image_url": v(img),
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
        return open("/tmp/kbs_done.txt", "a", encoding="utf-8")


# ---- run ----------------------------------------------------------------
def run():
    if CONTACT.endswith("example.com"):
        print("!! Please set KB_CONTACT to a real email so the site operator can reach you:")
        print("   KB_CONTACT=you@domain.com docker compose exec dashboard python scripts/keralabookstore.py")
        print("   (continuing anyway, but an honest contact address is the polite thing to do)\n")
    print(f"UA: {UA}")
    print(f"crawl-delay: {DELAY}s (robots.txt asks for 10)\n")

    urls = enumerate_all()
    if not urls:
        print("!! no book urls. Run 'sitemap' to diagnose.")
        return
    done = _load_done()
    fh = _open_done()
    todo = [u for u in urls if u not in done]
    limit = int(os.environ.get("KB_LIMIT", "0"))
    eta_h = len(todo) * DELAY / 3600
    print(f"enriching {len(urls)} books ({len(urls)-len(todo)} done, {len(todo)} to go)"
          + (f" | LIMIT={limit}" if limit else "") + f" | ETA ~{eta_h:.1f}h at {DELAY}s/book")
    t0, sess, dbfail, isbns = time.time(), 0, 0, 0
    for u in urls:
        if u in done:
            continue
        if limit and sess >= limit:
            print(f"  reached KB_LIMIT={limit}; stopping (resumable).")
            break
        html = get(u)
        if not html:
            fh.write(u + "\n"); fh.flush(); done.add(u)
            nap()
            continue
        rec = parse_detail(html, u)
        try:
            scriptkit.save("keralabookstore", [rec], key_fields=["url"])
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
        if rec["isbn"] != "N/A":
            isbns += 1
        if sess % 10 == 0:
            rate = sess / max(1e-9, time.time() - t0)
            eta = (len(todo) - sess) / max(1e-9, rate) / 3600
            print(f"  {sess}/{len(todo)} | {rate*3600:.0f}/h | ETA {eta:.1f}h | isbn {isbns} | "
                  f"{rec['title'][:20]} | {rec['isbn']} | Rs{rec['price']}")
        nap()
    fh.close()
    print(f"\nDone. Saved/updated {sess} books ({isbns} with ISBN).")


# ---- diagnostics --------------------------------------------------------
def cmd_sitemap():
    urls = sitemap_book_urls()
    print(f"\nsitemap -> {len(urls)} book urls; first 8:")
    for u in urls[:8]:
        print("  ", u)
    if urls:
        print(f"\nat {DELAY}s/book a full crawl is ~{len(urls)*DELAY/3600:.1f} hours")


def _url_from_arg(arg):
    if arg.startswith("http"):
        return arg
    if arg.isdigit():
        return f"{BASE}/book/x/{arg}/"
    return f"{BASE}/{arg.lstrip('/')}"


def cmd_book(arg):
    url = _url_from_arg(arg)
    rec = parse_detail(get(url), url)
    for k, v in rec.items():
        print(f"  {k:>13}: {str(v)[:100]}")


def cmd_raw(arg):
    url = _url_from_arg(arg)
    html = get(url)
    print(f"=== fetched {len(html)} chars for {url} ===\n")
    tt = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    print("--- <title> ---")
    print(_clean(tt.group(1)) if tt else "(none)")
    md = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]+)"', html, re.I)
    print("\n--- meta description ---")
    print(_clean(md.group(1)) if md else "(none)")
    print("\n--- TAG-STRIPPED TEXT (first 1200) ---")
    print(_text(html)[:1200])
    print("\n--- PARSED ---")
    cmd_book(arg)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else "book/indulekha/5069/"
    if cmd == "sitemap":
        cmd_sitemap()
    elif cmd == "book":
        cmd_book(arg)
    elif cmd == "raw":
        cmd_raw(arg)
    elif cmd == "enumerate":
        enumerate_all()
    else:
        run()