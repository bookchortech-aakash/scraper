"""
Udumalai.com — Tamil bookstore crawler (BOOKS ONLY; skips clothing/footwear/etc).

OpenCart store selling books + non-book products. Books are cleanly isolated:
every author and category is its own '<slug>-books.htm' page, listed on
  /all-authors.php   (authors)   and   /all-category.php   (genres)
Non-book products live under clothing-shop.htm / footwear-shop.htm / etc, which
we never touch.

Each '<slug>-books.htm' listing is a shell; its books load via AJAX:
  /inc/ajax_prd_list.php?auth_id=<N>&mc_id=&sc_id=&brand_id=[&page=<P>]
The page carries the numeric id (auth_id for authors; mc_id/sc_id for categories),
read from the "Show more results..." link. Book detail pages are '<slug>.htm'.

Two resumable phases:
  1) enumerate book URLs by walking every author + category AJAX feed  -> cache
  2) fetch each detail page -> full record -> save (DB-guarded checkpoint).
Dedup on the product slug (url). category/author captured from the feed it came from.

Run:
  python scripts/udumalai.py feeds                 -> list author+category feeds (+ids)
  python scripts/udumalai.py ajax <slug>-books.htm -> probe one feed's AJAX + pagination
  python scripts/udumalai.py book <slug>.htm       -> parse one detail page
  python scripts/udumalai.py enumerate             -> phase 1 only
  python scripts/udumalai.py                       -> enumerate + detail crawl
Pace: UD_MIN_DELAY / UD_MAX_DELAY (default 1-2s).
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

BASE = "https://www.udumalai.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MIN_DELAY = float(os.environ.get("UD_MIN_DELAY", "1.0"))
MAX_DELAY = float(os.environ.get("UD_MAX_DELAY", "2.0"))
STATE_FILE = os.environ.get("UD_STATE", "/app/scripts/.udumalai_state.json")
DONE_FILE = os.environ.get("UD_DONE", "/app/scripts/.udumalai_done.txt")

# non-book product slugs to exclude if they ever leak into a feed
NONBOOK = re.compile(r'-(shop|footwear)\.htm$|/(clothing|footwear|home-furnishing|'
                     r'baby-care|grocery|bed-sheets|bath-towels)', re.I)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ta,en-US;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
})
_PRIMED = {"ok": False}


def _prime():
    """Some udumalai endpoints bot-block a cold client; a homepage GET sets the
    cookies that make subsequent requests (incl. the /inc/ AJAX) pass."""
    if _PRIMED["ok"]:
        return
    try:
        SESSION.get(BASE + "/", timeout=45)
        _PRIMED["ok"] = True
    except Exception as e:
        print(f"   prime warn: {e}")


def nap():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def get(url, xhr=False, referer=None):
    _prime()
    hdrs = {}
    if xhr:
        hdrs["X-Requested-With"] = "XMLHttpRequest"
        hdrs["Referer"] = referer or (BASE + "/")
        hdrs["Accept"] = "text/html, */*; q=0.01"
    for attempt in range(6):
        try:
            r = SESSION.get(url, timeout=45, headers=hdrs)
            if r.status_code in (429, 503):
                wait = float(r.headers.get("Retry-After") or 0) or min(120, 6 * (2 ** attempt))
                print(f"   {r.status_code}; wait {wait:.0f}s ({attempt+1}/6)")
                time.sleep(wait)
                continue
            if r.status_code == 403 and not _PRIMED.get("reprimed"):
                _PRIMED["ok"] = False           # cookies expired -> re-prime once
                _PRIMED["reprimed"] = True
                _prime()
                continue
            if r.status_code in (403, 404):
                return ""
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except Exception as e:
            print(f"   err (try {attempt+1}): {e}")
            time.sleep(min(60, 5 * (2 ** attempt)))
    return ""


# ---- feed discovery (books only) ----------------------------------------
BOOKS_HREF = re.compile(r'''href\s*=\s*["']([^"']*?/?([a-z0-9][a-z0-9\-]*)-books\.htm)/?["']''', re.I)


def discover_feeds():
    """Return [(slug, kind, listing_url)] for every author + category -books.htm,
    tolerant of relative/single-quoted/trailing-slash hrefs."""
    out, seen = [], set()
    for page, kind in ((f"{BASE}/all-authors.php", "author"),
                       (f"{BASE}/all-category.php", "category")):
        html = get(page)
        for m in BOOKS_HREF.finditer(html):
            raw, slug = _html.unescape(m.group(1)), m.group(2)
            if slug in seen or NONBOOK.search(raw):
                continue
            url = raw if raw.startswith("http") else BASE + "/" + raw.lstrip("/")
            seen.add(slug)
            out.append((slug, kind, url))
        nap()
    return out


def cmd_dump(which="authors"):
    page = f"{BASE}/all-authors.php" if which.startswith("auth") else f"{BASE}/all-category.php"
    _prime()
    print(f"primed cookies: {list(SESSION.cookies.keys())}")
    try:
        r = SESSION.get(page, timeout=45)
        html = r.text if r.status_code == 200 else ""
        print(f"GET {page} -> status {r.status_code}, {len(r.text)} chars")
    except Exception as e:
        print(f"GET {page} -> error {e}")
        return
    low = html.lower()
    print(f"contains '-books.htm': {'-books.htm' in low} | hrefs: {len(re.findall(r'href', low))} | "
          f"BOOKS_HREF matches: {len(BOOKS_HREF.findall(html))}")
    if len(html) < 1500 or "-books.htm" not in low:
        print("\n--- page looks empty/blocked; first 1200 chars ---")
        print(html[:1200])
        return
    i = low.find("-books.htm")
    print("\n--- raw HTML around first '-books.htm' ---")
    print(html[max(0, i - 400):i + 200])
    feeds = discover_feeds()
    print(f"\ndiscover_feeds -> {len(feeds)} ({sum(1 for f in feeds if f[1]=='author')} auth, "
          f"{sum(1 for f in feeds if f[1]=='category')} cat); first 5: {[f[0] for f in feeds[:5]]}")


IDS_RE = re.compile(r'ajax_prd_list\.php\?(auth_id|mc_id|sc_id|brand_id)=(\d+)')


def feed_ids(listing_html):
    """Extract the id params from the 'Show more results...' ajax link."""
    ids = {"auth_id": "", "mc_id": "", "sc_id": "", "brand_id": ""}
    m = re.search(r'ajax_prd_list\.php\?([^"\'\s]+)', listing_html)
    if m:
        for k, v in re.findall(r'(auth_id|mc_id|sc_id|brand_id)=(\d*)', m.group(1)):
            ids[k] = v
    return ids


def ajax_url(ids, page=None):
    q = f"auth_id={ids.get('auth_id','')}&mc_id={ids.get('mc_id','')}&sc_id={ids.get('sc_id','')}&brand_id={ids.get('brand_id','')}"
    if page is not None:
        q += f"&page={page}"
    return f"{BASE}/inc/ajax_prd_list.php?{q}"


# ---- product-link parsing (from AJAX html) ------------------------------
def feed_products(html):
    """Slugs of product detail pages in an AJAX chunk. Product links are
    '<slug>.htm' inside anchors that ALSO wrap a /p_images/ thumbnail."""
    out, seen = [], set()
    # anchor with an <img .../p_images/...> and href to a *.htm product page
    for m in re.finditer(
            r'<a[^>]+href="https?://www\.udumalai\.com/([a-z0-9][a-z0-9\-]*)\.htm"[^>]*>'
            r'(?:(?!</a>).)*?/p_images/', html, re.S):
        slug = m.group(1)
        if slug in seen or slug.endswith("-books") or NONBOOK.search(slug):
            continue
        seen.add(slug)
        out.append(slug)
    return out


# ---- detail parsing -----------------------------------------------------
def _between(html, label_variants):
    for lab in label_variants:
        for pat in (
            # <li><b>Label:</b> value</li>  or  <li><strong>Label</strong>: value</li>
            rf'<li[^>]*>\s*<(?:b|strong|span)[^>]*>\s*{lab}\s*:?\s*</(?:b|strong|span)>\s*:?\s*([^<]+)',
            # <td>Label</td><td>value</td>  or  <th>Label:</th><td>value</td>
            rf'<t[hd][^>]*>\s*{lab}\s*:?\s*</t[hd]>\s*<td[^>]*>\s*(?:<[^>]+>\s*)*([^<]+)',
            # Label: value  (plain, maybe with a tag before the value)
            rf'{lab}\s*:\s*(?:<[^>]+>\s*)*([^<\n]+)',
        ):
            m = re.search(pat, html, re.I)
            if m:
                v = _html.unescape(m.group(1)).strip(" :\u00a0")
                if v:
                    return v
    return ""


def parse_detail(html, slug, base=None):
    base = base or {}
    tm = re.search(r'<h1[^>]*>\s*([^<]+)', html)
    title = _html.unescape(tm.group(1)).strip() if tm else base.get("title", "N/A")
    # OpenCart product attributes: Author/Publisher/Pages/ISBN/Year in a table or li
    author = _between(html, ["Author", "எழுத்தாளர்", "Writer"]) or base.get("author", "")
    publisher = _between(html, ["Publisher", "பதிப்பகம்"])
    pages = _between(html, ["Pages", "No. of Pages", "பக்கங்கள்"])
    isbn = _between(html, ["ISBN"])
    year = _between(html, ["Year", "Published", "வெளியீடு"])
    edition = _between(html, ["Edition", "பதிப்பு"])
    # price: OpenCart .price / product-price
    pm = re.search(r'(?:price[^>]*>|Price[^0-9]{0,20})[\s₹Rs\.]*([\d,]+\.\d{2})', html) \
        or re.search(r'₹?\s*([\d,]+\.\d{2})', html)
    price = pm.group(1).replace(",", "") if pm else "N/A"
    im = re.search(r'(https?://www\.udumalai\.com/p_images/[a-z_]*thumb/[^"\']+\.jpg)', html)
    isbn = re.sub(r"[^0-9Xx]", "", isbn) if isbn else ""
    return {
        "title": title or "N/A",
        "author": author or "N/A",
        "publisher": publisher or "N/A",
        "category": base.get("category", "N/A"),
        "pages": pages or "N/A",
        "edition": edition or "N/A",
        "year": year or "N/A",
        "isbn": isbn or "N/A",
        "language": "Tamil",
        "price": price,
        "url": f"{BASE}/{slug}.htm",
        "image_url": im.group(1) if im else "",
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
        return {"feeds_done": [], "products": {}}


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
        return open("/tmp/udumalai_done.txt", "a", encoding="utf-8")


# ---- phases -------------------------------------------------------------
def enumerate_all():
    feeds = discover_feeds()
    st = _load_state()
    done = set(st.get("feeds_done", []))
    products = st.get("products", {})       # slug -> {category, author}
    print(f"{len(feeds)} book feeds ({len(done)} done, {len(products)} products so far)")
    for slug, kind, url in feeds:
        if slug in done:
            continue
        ids = feed_ids(get(url))
        label = slug.replace("-", " ")
        page, empty, added = 1, 0, 0
        while True:
            chunk = get(ajax_url(ids, page), xhr=True, referer=url)
            slugs = feed_products(chunk)
            if not slugs:
                # maybe no ?page param; try without it once on page 1
                if page == 1:
                    chunk = get(ajax_url(ids), xhr=True, referer=url)
                    slugs = feed_products(chunk)
                if not slugs:
                    empty += 1
                    if empty >= 1:
                        break
            for s in slugs:
                rec = products.get(s, {"category": "", "author": ""})
                if kind == "author" and not rec["author"]:
                    rec["author"] = label
                if kind == "category" and not rec["category"]:
                    rec["category"] = label
                if s not in products:
                    added += 1
                products[s] = rec
            page += 1
            nap()
        done.add(slug)
        st = {"feeds_done": sorted(done), "products": products}
        _save_state(st)
        print(f"  [{len(done)}/{len(feeds)}] {label[:30]:<30} +{added} (total {len(products)})")
    print(f"\nenumeration done: {len(products)} unique books")
    return products


def run():
    products = enumerate_all()
    done = _load_done()
    fh = _open_done()
    todo = [s for s in products if s not in done]
    print(f"enriching {len(products)} books ({len(products)-len(todo)} done, {len(todo)} to go)")
    t0, sess, dbfail = time.time(), 0, 0
    for slug, meta in products.items():
        if slug in done:
            continue
        rec = parse_detail(get(f"{BASE}/{slug}.htm"), slug, meta)
        try:
            scriptkit.save("udumalai", [rec], key_fields=["url"])
        except Exception as e:
            dbfail += 1
            print(f"  !! DB save failed ({dbfail}/5): {e}")
            if dbfail >= 5:
                print("  !! aborting: database unreachable. Fix Postgres and rerun (resumes).")
                break
            time.sleep(10)
            continue
        dbfail = 0
        fh.write(slug + "\n")
        fh.flush()
        done.add(slug)
        sess += 1
        if sess % 25 == 0:
            rate = sess / max(1e-9, time.time() - t0)
            eta = (len(todo) - sess) / max(1e-9, rate) / 3600
            print(f"  {sess}/{len(todo)} | {rate*3600:.0f}/h | ETA {eta:.1f}h | "
                  f"{rec['title'][:22]} | {rec['author'][:16]} | {rec['isbn']}")
        nap()
    fh.close()
    print(f"\nDone. Saved/updated {sess} books this session.")


# ---- probes -------------------------------------------------------------
def cmd_feeds():
    feeds = discover_feeds()
    au = [f for f in feeds if f[1] == "author"]
    ca = [f for f in feeds if f[1] == "category"]
    print(f"{len(feeds)} book feeds: {len(au)} authors, {len(ca)} categories")
    for slug, kind, url in feeds[:8]:
        print(f"  {kind:<8} {slug}")


def cmd_ajax(listing="jeyamohan-books.htm"):
    url = f"{BASE}/{listing}"
    listing_html = get(url)
    ids = feed_ids(listing_html)
    print(f"feed ids for {listing}: {ids}")
    # 1) base call with NO page param — many infinite-scroll feeds return all/first chunk
    base = get(ajax_url(ids), xhr=True, referer=url)
    n = len(feed_products(base))
    print(f"\n[no page] {ajax_url(ids).replace(BASE,'')} -> {len(base)}b, {n} products (my parser)")
    # raw structure so I can fix feed_products — ESCAPED ascii, safe to paste
    anchor = base.find("p_images")
    if anchor < 0:
        anchor = base.find(".htm")
    print("\n=== RAW chunk around first product (escaped; paste this line) ===")
    print(repr(base[max(0, anchor - 300):anchor + 400]))
    print("\n=== count of key tokens in chunk ===")
    for tok in ("/p_images/", ".htm", "product-thumb", "href=", "<img", "add_cart", "pid="):
        print(f"   {tok:<14}: {base.count(tok)}")
    # 2) pagination param hunt (category feeds 500 on &page=; try alternatives)
    print("\n=== pagination probe (which param advances?) ===")
    first_ids = re.findall(r'/([a-z0-9\-]+)\.htm', base)
    fp = first_ids[0] if first_ids else None
    for name, u in [("page", ajax_url(ids, 2)),
                    ("start", f"{ajax_url(ids)}&start=20"),
                    ("offset", f"{ajax_url(ids)}&offset=20"),
                    ("limitstart", f"{ajax_url(ids)}&limitstart=20"),
                    ("p", f"{ajax_url(ids)}&p=2")]:
        try:
            chunk = get(u, xhr=True, referer=url)
        except Exception as e:
            print(f"   {name:<11} ERROR {e}"); continue
        slugs = feed_products(chunk)
        ids2 = re.findall(r'/([a-z0-9\-]+)\.htm', chunk)
        changed = "DIFFERENT" if (ids2 and ids2[0] != fp) else "same/empty"
        print(f"   {name:<11} -> {len(chunk)}b, {len(slugs)} parsed, first-htm {changed}")
        nap()


def cmd_book(slug):
    slug = slug.rstrip("/").split("/")[-1].replace(".htm", "")
    rec = parse_detail(get(f"{BASE}/{slug}.htm"), slug, {})
    for k, v in rec.items():
        print(f"  {k:>10}: {v}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else "jeyamohan-books.htm"
    if cmd == "feeds":
        cmd_feeds()
    elif cmd == "dump":
        cmd_dump(arg if arg != "jeyamohan-books.htm" else "authors")
    elif cmd == "ajax":
        cmd_ajax(arg)
    elif cmd == "book":
        cmd_book(arg)
    elif cmd == "enumerate":
        enumerate_all()
    else:
        run()