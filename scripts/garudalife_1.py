"""
Garuda Life (garudalife.in) — /books catalog crawler.

Laravel marketplace, server-rendered product pages at flat slugs (/<slug>).
The /books listing uses infinite scroll; the loader is almost always a plain
?page=N XHR returning either HTML card partials or JSON. Since the exact
mechanism can't be confirmed off-VPS, `discover` AUTO-DETECTS it (tries sitemap,
then ?page=N HTML, then common Laravel ajax routes) and caches the winner.

Detail pages are clean + labeled (ISBN 13, Book Language, Binding, Total Pages,
Author, GAIN, Publishers, Category, MRP/sale/discount, stock, seller, image) and
usually carry JSON-LD — parse_detail tries JSON-LD first, then labels.

Two resumable phases:
  1) enumerate all product slugs (sitemap or ?page=N) -> .garuda_slugs.json
  2) fetch each /<slug> -> full record -> save (checkpoint per slug), DB-guarded.

Run:
  python scripts/garudalife.py discover        -> find pagination + count
  python scripts/garudalife.py book <slug>     -> parse one detail page
  python scripts/garudalife.py enumerate       -> phase 1 only
  python scripts/garudalife.py                 -> enumerate + detail crawl
Pace: GL_MIN_DELAY / GL_MAX_DELAY (default 1-2s).
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

BASE = "https://garudalife.in"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MIN_DELAY = float(os.environ.get("GL_MIN_DELAY", "1.0"))
MAX_DELAY = float(os.environ.get("GL_MAX_DELAY", "2.0"))
SLUGS_FILE = os.environ.get("GL_SLUGS", "/app/scripts/.garuda_slugs.json")
DONE_FILE = os.environ.get("GL_DONE", "/app/scripts/.garuda_done.txt")
# slugs that are NOT products (nav/menu/system pages) — excluded from enumeration
NON_PRODUCT = re.compile(
    r"^(books|shop|category|cart|checkout|login|register|account|wishlist|"
    r"about|contact|blog|faq|terms|privacy|policy|seller|marketplace|search|"
    r"page|order|profile|home|sitemap|refund|shipping|track)(/|$|\?)", re.I)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                        "X-Requested-With": "XMLHttpRequest"})


def nap():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def get(url, xhr=False):
    hdrs = {} if xhr else {"X-Requested-With": ""}
    for attempt in range(6):
        try:
            r = SESSION.get(url, timeout=45, headers=hdrs if not xhr else None)
            if r.status_code in (429, 503):
                wait = float(r.headers.get("Retry-After") or 0) or min(120, 6 * (2 ** attempt))
                print(f"   {r.status_code}; wait {wait:.0f}s ({attempt+1}/6)")
                time.sleep(wait)
                continue
            if r.status_code in (400, 404):
                return ""
            if r.status_code == 500:
                return ""      # dead URL (e.g. publisher landing pages) — don't retry
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except Exception as e:
            print(f"   err (try {attempt+1}): {e}")
            time.sleep(min(60, 5 * (2 ** attempt)))
    return ""


# ---- product-slug extraction from a listing chunk -------------------------
def listing_slugs(html):
    """All internal product slugs in a listing/partial, in order, deduped."""
    out, seen = [], set()
    for m in re.finditer(r'href="(?:https?://garudalife\.in)?/([a-z0-9][a-z0-9\-]{2,})"', html):
        slug = m.group(1)
        if NON_PRODUCT.match(slug) or slug in seen:
            continue
        seen.add(slug)
        out.append(slug)
    return out


# ---- detail parsing (structured data only; no greedy label scraping) ------
def _all_ld(html):
    """Return all JSON-LD objects (flattening @graph)."""
    objs = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        for o in list(stack):
            if isinstance(o, dict) and "@graph" in o:
                stack.extend(o["@graph"])
        objs += [o for o in stack if isinstance(o, dict)]
    return objs


def _first(objs, *types):
    for o in objs:
        t = o.get("@type")
        t = t if isinstance(t, list) else [t]
        if any(x in types for x in t):
            return o
    return {}


def _lds(v):
    if isinstance(v, dict):
        return str(v.get("name") or "").strip()
    if isinstance(v, list):
        return ", ".join(x for x in (_lds(i) for i in v) if x)
    return str(v).strip() if v is not None else ""


def _meta(html, prop):
    m = (re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']*)', html, re.I)
         or re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{re.escape(prop)}["\']', html, re.I))
    return _html.unescape(m.group(1)).strip() if m else ""


# spec rows in the product panel: <td|th|span|div ...>Label</...> <...>value</...>
def _spec(html, *labels):
    for lab in labels:
        # Label in one element, value in the immediately following element's text
        m = re.search(rf'>\s*{re.escape(lab)}\s*<[^>]*>\s*(?:<[^>]+>\s*)*([^<]{{1,120}})<', html, re.I)
        if m:
            v = _html.unescape(m.group(1)).strip(" :\u200e\t")
            if v and '":"' not in v and "content=" not in v:
                return v
    return ""


def is_product(html):
    objs = _all_ld(html)
    if _first(objs, "Product"):
        return True
    # a real book page has a price and an add-to-cart / product panel
    return bool(re.search(r"add[-_ ]?to[-_ ]?cart|product-detail|book-detail", html, re.I)
                and re.search(r"₹\s*[\d,]", html))


def parse_detail(html, slug):
    objs = _all_ld(html)
    prod = _first(objs, "Product", "Book")
    crumb = _first(objs, "BreadcrumbList")

    title = _lds(prod.get("name")) or _meta(html, "og:title") or (
        (re.search(r"<h1[^>]*>\s*([^<]+)", html) or [None, slug])[1]).strip()
    title = _html.unescape(title).strip()

    isbn = _lds(prod.get("isbn")) or _spec(html, "ISBN 13", "ISBN-13", "ISBN13", "ISBN")
    isbn = re.sub(r"[^0-9Xx]", "", isbn)
    author = _lds(prod.get("author")) or _spec(html, "Author", "Authors")
    publisher = _lds(prod.get("publisher")) or _spec(html, "Publishers", "Publisher")
    pages = _lds(prod.get("numberOfPages")) or _spec(html, "Total Pages", "No. of Pages", "Pages")
    pages = re.sub(r"[^\d]", "", pages) if pages else ""
    binding = _spec(html, "Binding", "Format")
    language = _lds(prod.get("inLanguage")) or _spec(html, "Book Language", "Language")
    gain = _spec(html, "GAIN")

    # category: real breadcrumb crumbs only, excluding Home/Books/Shop AND the
    # product's own title (some pages put the product as the last crumb).
    category = ""
    items = crumb.get("itemListElement") if isinstance(crumb, dict) else None
    if isinstance(items, list):
        names = [str(it.get("name") or "").strip() or _lds(it.get("item"))
                 for it in items if isinstance(it, dict)]
        tnorm = re.sub(r"\W+", "", title).lower()
        names = [n for n in names
                 if n and n.lower() not in ("home", "books", "shop", "all books")
                 and re.sub(r"\W+", "", n).lower() != tnorm]
        category = names[-1] if names else ""
    cspec = _spec(html, "Category", "Categories")
    if not category and cspec and re.sub(r"\W+", "", cspec).lower() != re.sub(r"\W+", "", title).lower():
        category = cspec

    price = mrp = ""
    offers = prod.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if isinstance(offers, dict):
        price = str(offers.get("price") or "").strip()
        mrp = str(offers.get("highPrice") or offers.get("priceSpecification", {}).get("price")
                  if isinstance(offers.get("priceSpecification"), dict) else "" or "").strip()
    amts = sorted({float(a.replace(",", "")) for a in re.findall(r"₹\s*([\d,]+(?:\.\d+)?)", html)})
    if not price and amts:
        price = f"{amts[0]:.2f}"
    if not mrp and len(amts) > 1:
        mrp = f"{amts[-1]:.2f}"
    dm = re.search(r"(\d{1,2})\s*%\s*(?:off|discount)", html, re.I)
    disc = dm.group(1) + "%" if dm else ""

    img = _lds(prod.get("image")) or _meta(html, "og:image")

    def clean(v):
        v = (v or "").strip()
        return v if v and '":"' not in v and "content=" not in v and v not in (">", '">') else "N/A"

    return {
        "title": clean(title),
        "author": clean(author),
        "publisher": clean(publisher),
        "isbn13": clean(isbn),
        "pages": clean(pages),
        "binding": clean(binding),
        "language": clean(language),
        "category": clean(category),
        "gain": clean(gain),
        "price": clean(price),
        "mrp": clean(mrp),
        "discount": clean(disc),
        "url": f"{BASE}/{slug}",
        "image_url": img or "",
    }


# ---- pagination discovery -------------------------------------------------
def _try_sitemap():
    import gzip
    cands = []
    robots = get(BASE + "/robots.txt")
    cands += re.findall(r"(?im)^\s*Sitemap:\s*(\S+)", robots)
    cands += [BASE + p for p in ("/sitemap.xml", "/garudalife-in-sitemap.xml",
              "/sitemap_index.xml", "/product-sitemap.xml")]
    slugs, seen = [], set()
    for u in cands:
        if u in seen:
            continue
        seen.add(u)
        body = get(u)
        if not body:
            continue
        if body[:2] == "\x1f\x8b":
            try:
                body = gzip.decompress(body.encode("latin1")).decode("utf-8", "replace")
            except Exception:
                pass
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
        child = [x for x in locs if x.endswith(".xml") or x.endswith(".xml.gz")]
        prod = [x for x in locs if re.search(r"garudalife\.in/[a-z0-9\-]{3,}$", x)
                and not NON_PRODUCT.match(x.rsplit("/", 1)[-1])]
        if child and not prod:                       # sitemap index -> descend one level
            for c in child[:50]:
                b2 = get(c)
                prod += [x for x in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", b2)
                         if not NON_PRODUCT.match(x.rsplit("/", 1)[-1])]
                nap()
        if len(prod) > 20:
            print(f"  sitemap {u}: {len(prod)} product URLs")
            return [x.rstrip("/").rsplit("/", 1)[-1] for x in prod]
    return None


def _try_page_param():
    p1 = listing_slugs(get(f"{BASE}/books"))
    if not p1:
        return None
    for tmpl in ("{b}/books?page={n}", "{b}/books/page/{n}", "{b}/books?p={n}"):
        u = tmpl.format(b=BASE, n=2)
        s2 = listing_slugs(get(u, xhr=True))
        print(f"  {u.replace(BASE,'')}: {len(s2)} slugs, "
              f"{'DIFF' if s2 and s2[0] != p1[0] else 'same/empty'}")
        if s2 and s2[0] != p1[0]:
            return tmpl
    return None


def discover():
    st = _load_slugs()
    print("trying sitemap...")
    sm = _try_sitemap()
    if sm:
        st["mode"] = "sitemap"
        st["slugs"] = sm
        _save_slugs(st)
        print(f"DISCOVERED: sitemap, {len(sm)} products cached")
        return st
    print("no product sitemap; trying ?page=N ...")
    tmpl = _try_page_param()
    if tmpl:
        st["mode"] = "page"
        st["tmpl"] = tmpl
        _save_slugs(st)
        print(f"DISCOVERED: page param {tmpl!r}")
        return st
    print("!! neither worked — infinite scroll is JS/opaque. Paste the reference "
          "layout/endpoint you found (as text) and I'll wire it, else Playwright.")
    return None


# ---- state ----------------------------------------------------------------
def _load_slugs():
    try:
        return json.load(open(SLUGS_FILE, encoding="utf-8"))
    except Exception:
        return {}


def _save_slugs(st):
    try:
        os.makedirs(os.path.dirname(SLUGS_FILE) or ".", exist_ok=True)
        tmp = SLUGS_FILE + ".tmp"
        json.dump(st, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, SLUGS_FILE)
    except Exception as e:
        print(f"   slugs save warn: {e}")


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
        return open("/tmp/garuda_done.txt", "a", encoding="utf-8")


# ---- enumerate + crawl ----------------------------------------------------
def enumerate_all():
    st = _load_slugs()
    if not st.get("mode"):
        st = discover()
        if not st:
            return []
    if st["mode"] == "sitemap":
        slugs = list(dict.fromkeys(st.get("slugs", [])))
        print(f"sitemap mode: {len(slugs)} product slugs")
        return slugs
    # page mode: walk ?page=N until a page repeats or empties
    slugs = list(dict.fromkeys(st.get("slugs", [])))
    seen = set(slugs)
    page = st.get("next_page", 1)
    empty = 0
    while True:
        url = f"{BASE}/books" if page == 1 else st["tmpl"].format(b=BASE, n=page)
        chunk = listing_slugs(get(url, xhr=(page > 1)))
        new = [s for s in chunk if s not in seen]
        if not chunk or not new:
            empty += 1
            if empty >= 2:
                break
        else:
            empty = 0
            for s in new:
                seen.add(s)
                slugs.append(s)
        if page % 20 == 0 or page == 1:
            print(f"  page {page}: {len(slugs)} slugs")
            _save_slugs(dict(st, slugs=slugs, next_page=page + 1))
        page += 1
        nap()
    _save_slugs(dict(st, slugs=slugs, next_page=page))
    print(f"enumeration done: {len(slugs)} slugs")
    return slugs


BOOK_CAT = re.compile(r"book|novel|fiction|non[- ]?fiction|poetry|literature|biography|"
                      r"children|comics|religion|spiritual|history|academic|textbook|"
                      r"sahitya|kavita|upanishad|veda|purana|granth|magazine|author|publish", re.I)
NONBOOK_CAT = re.compile(r"home decor|kitchen|puja|pooja|idol|murti|toy|attar|itra|perfume|"
                         r"incense|dhoop|agarbatti|ghee|oil|grocery|jewel|earring|handicraft|"
                         r"apparel|clothing|footwear|soft toy|painting kit|combo|rudraksha|"
                         r"mala|tilak|roli|puja|utensil", re.I)


def is_book(rec, html=""):
    """True only for actual books, based on HARD book fields (the category field
    is unreliable here — it often holds the title). A real book has an ISBN13, or
    a page count, or a binding. The merchandise (mugs, frames, fresheners, toys,
    showpieces) has isbn13=pages=binding=N/A."""
    isbn = rec.get("isbn13", "N/A")
    pages = rec.get("pages", "N/A")
    binding = rec.get("binding", "N/A")
    if isbn not in ("", "N/A") and re.fullmatch(r"(?:97[89])?\d{9}[\dXx]", isbn):
        return True                       # valid ISBN -> definitely a book
    if pages not in ("", "N/A") and binding not in ("", "N/A"):
        return True                       # both pages AND binding -> a book (no ISBN listed)
    return False


def run():
    slugs = enumerate_all()
    if not slugs:
        return
    done = _load_done()
    fh = _open_done()
    todo = [s for s in slugs if s not in done]
    print(f"enriching {len(slugs)} books ({len(slugs)-len(todo)} done, {len(todo)} to go)")
    t0, sess, dbfail = time.time(), 0, 0
    for slug in slugs:
        if slug in done:
            continue
        html = get(f"{BASE}/{slug}")
        if not is_product(html):
            fh.write(slug + "\n")     # checkpoint as handled so we don't refetch
            fh.flush()
            done.add(slug)
            continue
        rec = parse_detail(html, slug)
        if not is_book(rec, html):
            fh.write(slug + "\n")     # non-book product (attar/toy/idol/ghee) — skip, don't refetch
            fh.flush()
            done.add(slug)
            continue
        try:
            scriptkit.save("garudalife", [rec], key_fields=["url"])
        except Exception as e:
            dbfail += 1
            print(f"  !! DB save failed ({dbfail}/5): {e}")
            if dbfail >= 5:
                print("  !! aborting: database unreachable; rerun to resume.")
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
                  f"{rec['title'][:24]} | {rec['isbn13']} | {rec['pages']}p")
        nap()
    fh.close()
    print(f"\nDone. Saved/updated {sess} books this session.")


def cmd_dump(slug):
    slug = slug.rstrip("/").rsplit("/", 1)[-1]
    html = get(f"{BASE}/{slug}")
    print(f"fetched {len(html)} chars | is_product={is_product(html)}")
    blocks = re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S)
    print(f"JSON-LD blocks: {len(blocks)}")
    for i, b in enumerate(blocks):
        try:
            data = json.loads(b.strip())
            print(f"\n--- block {i} (parsed) ---")
            print(json.dumps(data, ensure_ascii=False)[:1500])
        except Exception as e:
            print(f"\n--- block {i} (RAW, unparseable: {e}) ---")
            print(b.strip()[:800])
    print("\n--- parse_detail result ---")
    for k, v in parse_detail(html, slug).items():
        print(f"  {k:>10}: {v}")


def cmd_book(slug):
    slug = slug.rstrip("/").rsplit("/", 1)[-1]
    rec = parse_detail(get(f"{BASE}/{slug}"), slug)
    for k, v in rec.items():
        print(f"  {k:>10}: {v}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else "curse-of-the-gandhari"
    if cmd == "discover":
        discover()
    elif cmd == "dump":
        cmd_dump(arg)
    elif cmd == "book":
        cmd_book(arg)
    elif cmd == "enumerate":
        enumerate_all()
    else:
        run()
