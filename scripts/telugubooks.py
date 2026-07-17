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

Detail pages carry a LEAN field set (Udumalai has no ISBN/publisher/pages/binding):
  title (h1), Author (label), category (in <title> tail), Price (+ struck MRP if
  discounted), stock status, description, image.

Two resumable phases:
  1) enumerate book URLs by walking every author + category AJAX feed  -> cache
  2) fetch each detail page -> record -> save (DB-guarded checkpoint).
Dedup on the product slug (url). category/author captured from the feed it came from.

Run:
  python scripts/udumalai.py feeds                 -> list author+category feeds (+ids)
  python scripts/udumalai.py dump authors|category -> diagnose /all-*.php discovery
  python scripts/udumalai.py ajax <slug>-books.htm -> probe one feed's AJAX + pagination
  python scripts/udumalai.py book <slug>.htm       -> parse one detail page
  python scripts/udumalai.py enumerate             -> phase 1 only
  python scripts/udumalai.py                       -> enumerate + detail crawl
Pace: UD_MIN_DELAY / UD_MAX_DELAY (default 1-2s).  Test batch: UD_LIMIT=30.
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

# non-book product slugs / listing pages to exclude if they leak into a feed
NONBOOK = re.compile(r'-(shop|footwear)$|^(clothing|footwear|home-furnishing|'
                     r'baby-care|grocery|bed-sheets|bath-towels|poomex)', re.I)
# navigation / non-product .htm pages that can appear in chunks
NAV_SLUGS = {
    "index", "book-shop", "all-authors", "all-category", "cart", "checkout",
    "login", "register", "contact-us", "about-us", "wishlist", "home",
    "clothing-shop", "footwear-shop", "terms-conditions", "privacy-policy",
    "return-policy", "shipping", "faq", "sitemap",
}

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
            # 500/502 = a broken product page (or a dead ajax page) on THEIR end;
            # retrying 6x can't fix it, so skip fast for both detail + ajax calls.
            if r.status_code in (500, 502):
                return ""
            if r.status_code in (403, 404):
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


# ---- feed discovery (books only) ----------------------------------------
BOOKS_HREF = re.compile(r'''href\s*=\s*["']([^"']*?/?([a-z0-9][a-z0-9\-]*)-books\.htm)/?["']''', re.I)


def discover_feeds():
    """Return [(slug, kind, listing_url)] for every author + category -books.htm."""
    out, seen = [], set()
    for page, kind in ((f"{BASE}/all-authors.php", "author"),
                       (f"{BASE}/all-category.php", "category")):
        html = get(page)
        for m in BOOKS_HREF.finditer(html):
            raw, slug = _html.unescape(m.group(1)), m.group(2).lower()
            if slug in seen or NONBOOK.search(slug) or slug in NAV_SLUGS:
                continue
            url = raw if raw.startswith("http") else BASE + "/" + raw.lstrip("/")
            seen.add(slug)
            out.append((slug, kind, url))
        nap()
    return out


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
    q = (f"auth_id={ids.get('auth_id','')}&mc_id={ids.get('mc_id','')}"
         f"&sc_id={ids.get('sc_id','')}&brand_id={ids.get('brand_id','')}")
    if page is not None:
        q += f"&page={page}"
    return f"{BASE}/inc/ajax_prd_list.php?{q}"


# ---- product-link parsing (from AJAX html) ------------------------------
# TOLERANT: any <slug>.htm product link in the chunk (relative OR absolute, any
# quoting), minus -books.htm listing pages, nav pages, and non-book slugs.
PROD_HREF = re.compile(
    r'href\s*=\s*["\'](?:https?://(?:www\.)?udumalai\.com)?/?([a-z0-9][a-z0-9\-]*)\.htm\b', re.I)


def feed_products(html):
    out, seen = [], set()
    for m in PROD_HREF.finditer(html):
        slug = m.group(1).lower()
        if (slug in seen or slug.endswith("-books")
                or slug in NAV_SLUGS or NONBOOK.search(slug)):
            continue
        seen.add(slug)
        out.append(slug)
    return out


# ---- detail parsing -----------------------------------------------------
def _between(html, label_variants):
    for lab in label_variants:
        for pat in (
            rf'<li[^>]*>\s*<(?:b|strong|span)[^>]*>\s*{lab}\s*:?\s*</(?:b|strong|span)>\s*:?\s*(?:<[^>]+>\s*)*([^<]+)',
            rf'<t[hd][^>]*>\s*{lab}\s*:?\s*</t[hd]>\s*<td[^>]*>\s*(?:<[^>]+>\s*)*([^<]+)',
            rf'{lab}\s*:\s*(?:<[^>]+>\s*)*([^<\n]+)',
        ):
            m = re.search(pat, html, re.I)
            if m:
                v = _clean(m.group(1))
                if v:
                    return v
    return ""


def parse_detail(html, slug, base=None):
    base = base or {}
    tm = re.search(r'(?is)<h1[^>]*>\s*(.*?)\s*</h1>', html)
    title = _clean(tm.group(1)) if tm else base.get("title", "")

    author = _between(html, ["Author", "எழுத்தாளர்", "Writer"]) or base.get("author", "")

    # category: from <title> tail ("...<Author> Books, <category>"), else feed meta
    category = ""
    tt = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if tt:
        parts = [p.strip() for p in _clean(tt.group(1)).split(",") if p.strip()]
        if len(parts) >= 2:
            category = parts[-1]
    category = category or base.get("category", "")

    # scope for price/stock: from <h1> up to Description/Related (avoids header
    # cart total and related-product prices)
    h1i = html.find("<h1")
    ends = [i for i in (html.find("Related Products"), html.find("Description")) if i > (h1i if h1i >= 0 else 0)]
    scope = html[(h1i if h1i >= 0 else 0):(min(ends) if ends else (h1i + 4000 if h1i >= 0 else 4000))]

    amts = sorted({float(a.replace(",", "")) for a in re.findall(r"([\d,]+\.\d{2})", scope)})
    price = mrp = ""
    if amts:
        price = f"{amts[0]:.2f}"
        if len(amts) > 1:
            mrp = f"{amts[-1]:.2f}"

    stock = ""
    sm = re.search(r"(Out of Stock|Stock Available[^<\n]*|In Stock|Pre[- ]?Order[^<\n]*)", scope, re.I)
    if sm:
        stock = _clean(sm.group(1))

    # description: the Description tab body, up to Reviews/Related
    desc = ""
    di = html.find("Description")
    if di >= 0:
        rel = html.find("Related Products", di)
        dchunk = html[di:(rel if rel > 0 else di + 6000)]
        dtext = _clean(re.sub(r"<[^>]+>", " ", dchunk))
        dtext = re.sub(r"^\s*Description\s*(?:Reviews?)?\s*", "", dtext, flags=re.I)
        dtext = re.split(r"Product Reviews|No reviews|Related Products|Add to (?:Cart|Wish)", dtext)[0]
        desc = dtext.strip()
        if title and desc.startswith(title):      # description tab repeats the title as a heading
            desc = desc[len(title):].strip(" :-\u00a0")

    im = re.search(r'(https?://(?:www\.)?udumalai\.com/p_images/[^"\'\s]+\.(?:jpe?g|png|webp))', html, re.I) \
        or re.search(r'<meta[^>]+og:image[^>]+content="([^"]+)"', html, re.I)
    image = im.group(1) if im else ""

    def v(x):
        return x if x else "N/A"

    return {
        "title": v(title),
        "author": v(author),
        "category": v(category),
        "price": v(price),
        "mrp": v(mrp),
        "stock": v(stock),
        "language": "Tamil",
        "description": desc or "N/A",
        "url": f"{BASE}/{slug}.htm",
        "image_url": image or "N/A",
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
def _walk_feed(url, ids):
    """Yield product slugs for one feed. First call has no &page (chunk 1),
    then &page=2,3...; stops when a chunk adds no new slugs (covers both
    single-chunk category feeds and paginated author feeds)."""
    feed_seen = set()
    n = 1
    while True:
        chunk = get(ajax_url(ids, None if n == 1 else n), xhr=True, referer=url)
        slugs = feed_products(chunk)
        new = [s for s in slugs if s not in feed_seen]
        if not new:
            break
        for s in new:
            feed_seen.add(s)
            yield s
        n += 1
        nap()


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
        added = 0
        for s in _walk_feed(url, ids):
            rec = products.get(s, {"category": "", "author": ""})
            if kind == "author" and not rec["author"]:
                rec["author"] = label
            if kind == "category" and not rec["category"]:
                rec["category"] = label
            if s not in products:
                added += 1
            products[s] = rec
        done.add(slug)
        _save_state({"feeds_done": sorted(done), "products": products})
        print(f"  [{len(done)}/{len(feeds)}] {label[:30]:<30} +{added} (total {len(products)})")
    print(f"\nenumeration done: {len(products)} unique books")
    return products


def run():
    products = enumerate_all()
    done = _load_done()
    fh = _open_done()
    todo = [s for s in products if s not in done]
    limit = int(os.environ.get("UD_LIMIT", "0"))
    print(f"enriching {len(products)} books ({len(products)-len(todo)} done, {len(todo)} to go)"
          + (f" | LIMIT={limit}" if limit else ""))
    t0, sess, dbfail, skipped = time.time(), 0, 0, 0
    for slug, meta in products.items():
        if slug in done:
            continue
        if limit and sess >= limit:
            print(f"  reached UD_LIMIT={limit}; stopping (resumable).")
            break
        html = get(f"{BASE}/{slug}.htm")
        if not html:
            # broken page on their end (500/404) -> mark done so reruns skip it
            skipped += 1
            fh.write(slug + "\n")
            fh.flush()
            done.add(slug)
            if skipped % 25 == 0:
                print(f"  ...skipped {skipped} dead pages (500/404)")
            continue
        rec = parse_detail(html, slug, meta)
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
                  f"{rec['title'][:22]} | {rec['author'][:16]} | ₹{rec['price']}")
        nap()
    fh.close()
    print(f"\nDone. Saved/updated {sess} books this session ({skipped} dead pages skipped).")


# ---- probes -------------------------------------------------------------
def cmd_feeds():
    feeds = discover_feeds()
    au = [f for f in feeds if f[1] == "author"]
    ca = [f for f in feeds if f[1] == "category"]
    print(f"{len(feeds)} book feeds: {len(au)} authors, {len(ca)} categories")
    for slug, kind, url in feeds[:8]:
        print(f"  {kind:<8} {slug}")


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


def cmd_ajax(listing="jeyamohan-books.htm"):
    url = f"{BASE}/{listing}"
    listing_html = get(url)
    ids = feed_ids(listing_html)
    print(f"feed ids for {listing}: {ids}")
    base = get(ajax_url(ids), xhr=True, referer=url)
    n = len(feed_products(base))
    print(f"\n[no page] {ajax_url(ids).replace(BASE,'')} -> {len(base)}b, {n} products (my parser)")
    anchor = base.find("p_images")
    if anchor < 0:
        anchor = base.find(".htm")
    print("\n=== RAW chunk around first product (escaped; paste this) ===")
    print(repr(base[max(0, anchor - 300):anchor + 400]))
    print("\n=== token counts in chunk ===")
    for tok in ("/p_images/", ".htm", "product-thumb", "href=", "<img", "add_cart", "pid="):
        print(f"   {tok:<14}: {base.count(tok)}")
    print("\n=== pagination probe (which advances?) ===")
    first = re.findall(r'/([a-z0-9\-]+)\.htm', base)
    fp = first[0] if first else None
    for name, u in [("page=2", ajax_url(ids, 2)),
                    ("start=20", f"{ajax_url(ids)}&start=20"),
                    ("offset=20", f"{ajax_url(ids)}&offset=20"),
                    ("limitstart", f"{ajax_url(ids)}&limitstart=20"),
                    ("p=2", f"{ajax_url(ids)}&p=2")]:
        chunk = get(u, xhr=True, referer=url)
        slugs = feed_products(chunk)
        got = re.findall(r'/([a-z0-9\-]+)\.htm', chunk)
        changed = "DIFFERENT" if (got and got[0] != fp) else "same/empty"
        print(f"   {name:<11} -> {len(chunk)}b, {len(slugs)} parsed, first-htm {changed}")
        nap()


def cmd_book(slug):
    slug = slug.rstrip("/").split("/")[-1].replace(".htm", "")
    rec = parse_detail(get(f"{BASE}/{slug}.htm"), slug, {})
    for k, v in rec.items():
        s = v if isinstance(v, str) else str(v)
        print(f"  {k:>11}: {s[:100]}")


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