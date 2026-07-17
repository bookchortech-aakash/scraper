"""
Acchamgatelugu (books.acchamgatelugu.com) — Telugu book publisher/store.

Custom storefront (NOT Shopify — /products.json 404s; NOT WooCommerce). The
category pages (/categories/<slug>) render "No products found" to non-JS clients,
i.e. listings are CLIENT-RENDERED. So enumeration goes via sitemap.xml, which
does exist (it returns XML). Product pages themselves ARE server-rendered.

  product URLs : /products/<slug>        (older form: /product/<slug>/)
  categories   : /categories/<slug>      (client-rendered -> not used for enum)

Catalog is SMALL: category counts on the site total ~518 slots across
Latest 28 / Combos 17 / Our Publications 91 / Akshagna 12 / Story 109 /
Novels 138 / Spiritual 62 / Others 12 / nostalgia 10 / Literature 39,
with overlap -> realistically ~300-500 unique books. Whole crawl = minutes.

Detail pages carry a labelled spec block, e.g.:
    Book Name : Emantavoi Naruda? (ఏమంటావోయ్ నరుడా?)
    Writer Name : Allam Arun Kumar
    Publisher Name: Acchamga Telugu publications
    Price : Rs. 80
    Dimensions : A8
    No. of Pages: 48
    Year of Publication: 2022
    Edition: 1st Edition
    Description: ...
Labels vary a bit between books ("Writer Name"/"Author", "రచన:" etc), so the
parser tries several variants and falls back to the page's own <h1>/price.
No ISBN label was seen (same as the other Telugu stores) but we scan for one.

Run:
  python scripts/acchamgatelugu.py sitemap        -> how many /products/ urls found
  python scripts/acchamgatelugu.py book <slug>    -> full record for one book
  python scripts/acchamgatelugu.py raw <slug>     -> DIAGNOSTIC: dump spec HTML/text
  python scripts/acchamgatelugu.py enumerate      -> phase 1 only
  python scripts/acchamgatelugu.py                -> enumerate + detail crawl
Pace: AG_MIN_DELAY / AG_MAX_DELAY (default 0.8-1.6s).  Test batch: AG_LIMIT=20.
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

BASE = "https://books.acchamgatelugu.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MIN_DELAY = float(os.environ.get("AG_MIN_DELAY", "0.8"))
MAX_DELAY = float(os.environ.get("AG_MAX_DELAY", "1.6"))
URLS_FILE = os.environ.get("AG_URLS", "/app/scripts/.acchamga_urls.json")
DONE_FILE = os.environ.get("AG_DONE", "/app/scripts/.acchamga_done.txt")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "te,en-US;q=0.9,en;q=0.8"})


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


def _clean(v):
    v = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", str(v or ""))
    return re.sub(r"\s+", " ", _html.unescape(v)).strip(" :\u00a0\t\n-")


def _text(html):
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</(p|li|div|h\d|td|tr)>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = _html.unescape(h)
    h = re.sub(r"[ \t\u00a0]+", " ", h)
    return "\n".join(ln.strip() for ln in h.split("\n") if ln.strip())


# ---- phase 1: sitemap enumeration ---------------------------------------
PROD_PATH = re.compile(r"/products?/[^/\s\"'<>]+", re.I)


def _locs(xml):
    return [_clean(m) for m in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml, re.I)]


def sitemap_product_urls():
    """All /products/<slug> URLs from sitemap.xml (handles sitemapindex + .gz)."""
    root = get_xml(f"{BASE}/sitemap.xml")
    if not root:
        return []
    locs = _locs(root)
    is_index = ("<sitemapindex" in root.lower()) or (
        locs and not any(PROD_PATH.search(l) for l in locs)
        and all(l.lower().endswith((".xml", ".xml.gz")) or "sitemap" in l.lower() for l in locs))
    urls = []
    if is_index:
        for child in locs:
            urls += [l for l in _locs(get_xml(child)) if PROD_PATH.search(l)]
            nap()
    else:
        urls += [l for l in locs if PROD_PATH.search(l)]
    # normalize + dedup by slug (a book may appear as /product/x/ and /products/x)
    out, seen = [], set()
    for u in urls:
        slug = u.rstrip("/").split("/")[-1].lower()
        if slug in seen or slug in ("catalog", "products", "product"):
            continue
        seen.add(slug)
        out.append(u.rstrip("/"))
    return out


def enumerate_all():
    st = _load_urls()
    urls = st.get("urls", [])
    if urls:
        print(f"resuming with {len(urls)} product urls already enumerated")
        return urls
    print("enumerating via sitemap.xml ...")
    urls = sitemap_product_urls()
    print(f"  sitemap yielded {len(urls)} product urls")
    _save_urls({"urls": urls})
    return urls


# ---- phase 2: detail ----------------------------------------------------
_STOPS = ["Book Name", "Writer Name", "Author Name", "Author", "Publisher Name", "Publisher",
          "Published by", "Price", "Dimensions", "Size", "No. of Pages", "Number of Pages",
          "Pages", "Year of Publication", "Year", "Edition", "Description", "ISBN",
          "Similar products", "Add More", "Out of stock"]


def _spec(text, *labels):
    """Value after 'Label :' up to end-of-line (or the next known label)."""
    others = "|".join(re.escape(s) for s in _STOPS)
    for lab in labels:
        m = re.search(re.escape(lab) + r"\s*[:：]\s*(.+?)(?=\n|$|(?:" + others + r")\s*[:：])",
                      text, re.I)
        if m:
            v = _clean(m.group(1))
            if v:
                return v
    return ""


ISBN_RE = re.compile(r"ISBN(?:[-\s]*1[03])?\s*[:\-]?\s*((?:97[89][-\s]?)?[\d][\d\-\s]{8,16}[\dXx])", re.I)


def _find_isbn(text):
    m = ISBN_RE.search(text)
    if not m:
        return ""
    d = re.sub(r"[^0-9Xx]", "", m.group(1))
    return d if len(d) in (10, 13) else ""


# ---- JSON-LD (the page ships exact structured data: Product + BreadcrumbList) --
def _ld_fix(raw):
    """Dukaan emits LITERAL newlines/tabs inside JSON string values (the product
    description), which is invalid JSON and makes json.loads throw. Escape any
    control chars that occur *inside* a quoted string so the block parses."""
    out, in_str, esc = [], False, False
    for ch in raw:
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == "\\":
            out.append(ch)
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            continue
        if in_str and ch in "\n\r\t":
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
            continue
        out.append(ch)
    return "".join(out)


def _ld_objects(html):
    objs = []
    for m in re.finditer(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html):
        raw = m.group(1).strip()
        data = None
        for candidate in (raw, _ld_fix(raw)):
            try:
                data = json.loads(candidate)
                break
            except Exception:
                continue
        if data is None:
            continue
        stack = data if isinstance(data, list) else [data]
        for o in list(stack):
            if isinstance(o, dict) and "@graph" in o:
                stack.extend(o["@graph"])
        objs += [o for o in stack if isinstance(o, dict)]
    return objs


def _ld_of_type(objs, *types):
    for o in objs:
        t = o.get("@type")
        t = t if isinstance(t, list) else [t]
        if any(x in types for x in t):
            return o
    return {}


def _ld_category(objs):
    """category from Product.category, else the 2nd BreadcrumbList item."""
    prod = _ld_of_type(objs, "Product", "Book")
    cat = _clean(prod.get("category") or "")
    if cat:
        return cat
    crumb = _ld_of_type(objs, "BreadcrumbList")
    items = crumb.get("itemListElement") if isinstance(crumb, dict) else None
    if isinstance(items, list):
        names = []
        for it in items:
            if not isinstance(it, dict):
                continue
            n = _clean(it.get("name") or "")
            if n and n.lower() not in ("home", "products", "shop"):
                names.append(n)
        if len(names) >= 2:      # [<category>, <product name>]
            return names[0]
    return ""


def parse_detail(html, url):
    slug = url.rstrip("/").split("/")[-1]
    objs = _ld_objects(html)
    prod = _ld_of_type(objs, "Product", "Book")

    # JSON-LD description carries the labelled spec block; fall back to page text
    ld_desc = _clean(prod.get("description") or "")
    spec_src = ld_desc if ld_desc else ""
    text = _text(html)
    # normalize the LD description's newlines so _spec's line-anchored regex works
    lddesc_lines = (prod.get("description") or "").replace("\r", "")
    src = lddesc_lines if lddesc_lines else text

    title = _clean(prod.get("name") or "") or _spec(src, "Book Name", "Book Title")
    if not title:
        h1 = re.search(r"(?is)<h1[^>]*>\s*(.*?)\s*</h1>", html)
        title = _clean(re.sub(r"<[^>]+>", " ", h1.group(1))) if h1 else ""
    if not title:
        tt = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        if tt:
            t = re.sub(r"^\s*Buy\s+", "", _clean(tt.group(1)), flags=re.I)
            title = re.sub(r"\s*online from Acchamgatelugu.*$", "", t, flags=re.I)

    category = _ld_category(objs)

    author = _spec(src, "Writer Name", "Author Name", "Author", "Written by", "రచన")
    publisher = _spec(src, "Publisher Name", "Published by", "Publisher", "ప్రచురించిన సంస్థ")
    pages = re.sub(r"[^\d]", "",
                   _spec(src, "No.of pages", "No. of Pages", "Number of Pages", "Pages") or "")
    year = ""
    ym = re.search(r"(19|20)\d{2}", _spec(src, "Year of Publication", "Year") or "")
    if ym:
        year = ym.group(0)
    edition = _spec(src, "Edition")
    dimensions = _spec(src, "Dimensions", "Size")
    isbn = _find_isbn(src) or _find_isbn(text)

    # ---- price / mrp / stock: prefer JSON-LD offers (exact) ----
    price = mrp = discount = ""
    stock = ""
    offers = prod.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if isinstance(offers, dict) and offers:
        p = str(offers.get("price") or offers.get("lowPrice") or "").strip()
        if p:
            price = p.replace(",", "")
        hi = offers.get("highPrice")
        if hi:
            mrp = str(hi).strip().replace(",", "")
        avail = str(offers.get("availability") or "")
        if avail:
            stock = "Out of Stock" if re.search(r"OutOfStock|SoldOut", avail, re.I) else "In Stock"
    if not price:
        ps = _spec(src, "Price")
        pm = re.search(r"([\d,]+(?:\.\d+)?)", ps or "")
        if pm:
            price = pm.group(1).replace(",", "")
    rup = [a.replace(",", "") for a in re.findall(r"₹\s*([\d,]+(?:\.\d+)?)", text)]
    if rup and not price:
        price = rup[0]
    if rup and not mrp and price:
        hi = [x for x in rup[:4] if float(x) > float(price)]
        if hi:
            mrp = max(hi, key=float)
    try:
        if mrp and price and float(mrp) > float(price) > 0:
            discount = str(round((float(mrp) - float(price)) / float(mrp) * 100))
    except Exception:
        pass
    if not stock:
        # only trust an explicit product-level message, not hidden UI boilerplate
        stock = "Out of Stock" if re.search(r"This product is out of stock", text, re.I) else "In Stock"

    # description: the LD description minus the trailing spec lines + title line
    desc = ld_desc
    if prod.get("description"):
        raw = prod["description"].replace("\r", "")
        # cut at the first spec label line (Author:/Published by:/No.of pages:/Price:)
        raw = re.split(r"\n\s*(?:Author|Writer Name|Publisher|Published by|No\.?\s*of\s*pages|Price|Edition|Dimensions)\s*[:：]",
                       raw)[0]
        lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
        # drop a trailing "Title - <telugu title>" restatement line
        if lines and title and lines[-1].lower().startswith(title.lower()):
            lines = lines[:-1]
        desc = _clean(" ".join(lines))
    if not desc:
        desc = _spec(src, "Description")
    if not desc:
        md = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]+)"', html, re.I)
        if md:
            d = _clean(md.group(1))
            if not re.match(r"^\s*Order .* online from Acchamgatelugu", d, re.I):
                desc = d          # skip the boilerplate meta description

    image = ""
    img = prod.get("image")
    if isinstance(img, dict):
        image = _clean(img.get("url") or img.get("image") or "")
    elif isinstance(img, str):
        image = _clean(img)
    elif isinstance(img, list) and img:
        first = img[0]
        image = _clean(first.get("url") if isinstance(first, dict) else first)
    if not image:
        im = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html, re.I)
        image = _clean(im.group(1)) if im else ""

    sku = _clean(prod.get("sku") or "")

    def v(x):
        return x if x else "N/A"

    return {
        "title": v(title),
        "author": v(author),
        "publisher": v(publisher),
        "category": v(category),
        "isbn": v(isbn),
        "pages": v(pages),
        "edition": v(edition),
        "year": v(year),
        "dimensions": v(dimensions),
        "language": "Telugu",
        "price": v(price),
        "mrp": v(mrp),
        "discount": v(discount),
        "stock": stock,
        "sku": v(sku),
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
        return open("/tmp/acchamga_done.txt", "a", encoding="utf-8")


# ---- run ----------------------------------------------------------------
def run():
    urls = enumerate_all()
    if not urls:
        print("!! no product urls found. Run 'sitemap' to diagnose "
              "(if the sitemap is empty we'll need the client-side API instead).")
        return
    done = _load_done()
    fh = _open_done()
    todo = [u for u in urls if u not in done]
    limit = int(os.environ.get("AG_LIMIT", "0"))
    print(f"enriching {len(urls)} books ({len(urls)-len(todo)} done, {len(todo)} to go)"
          + (f" | LIMIT={limit}" if limit else ""))
    t0, sess, dbfail = time.time(), 0, 0
    for u in urls:
        if u in done:
            continue
        if limit and sess >= limit:
            print(f"  reached AG_LIMIT={limit}; stopping (resumable).")
            break
        html = get(u)
        if not html:
            fh.write(u + "\n"); fh.flush(); done.add(u)
            continue
        rec = parse_detail(html, u)
        try:
            scriptkit.save("acchamgatelugu", [rec], key_fields=["url"])
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
        if sess % 20 == 0:
            rate = sess / max(1e-9, time.time() - t0)
            eta = (len(todo) - sess) / max(1e-9, rate) / 60
            print(f"  {sess}/{len(todo)} | {rate*3600:.0f}/h | ETA {eta:.0f}m | "
                  f"{rec['title'][:24]} | {rec['author'][:16]} | ₹{rec['price']} | {rec['pages']}p")
        nap()
    fh.close()
    print(f"\nDone. Saved/updated {sess} books this session.")


# ---- diagnostics --------------------------------------------------------
def cmd_sitemap():
    root = get_xml(f"{BASE}/sitemap.xml")
    print(f"sitemap.xml -> {len(root)} chars")
    if not root:
        print("  EMPTY/blocked. The listing is client-rendered, so without a sitemap")
        print("  we'd need the site's product API (DevTools -> Network -> XHR).")
        return
    locs = _locs(root)
    print(f"  <loc> entries: {len(locs)} | looks like index: {'<sitemapindex' in root.lower()}")
    for l in locs[:5]:
        print("    ", l)
    urls = sitemap_product_urls()
    print(f"\nproduct urls: {len(urls)}; first 10:")
    for u in urls[:10]:
        print("  ", u)


def _url_from_arg(arg):
    if arg.startswith("http"):
        return arg
    return f"{BASE}/products/{arg.strip('/').split('/')[-1]}"


def cmd_cats():
    """Crawl-free-ish: fetch each book and tally categories (uses the cached url list)."""
    urls = enumerate_all()
    from collections import Counter
    tally = Counter()
    print(f"scanning {len(urls)} books for categories ...")
    for i, u in enumerate(urls, 1):
        html = get(u)
        if html:
            tally[_ld_category(_ld_objects(html)) or "N/A"] += 1
        if i % 50 == 0:
            print(f"  {i}/{len(urls)} ...")
        nap()
    print("\n=== categories ===")
    for c, n in tally.most_common():
        print(f"  {n:>4}  {c}")


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
    t = _text(html)
    i = max(0, min([t.find(x) for x in ("Book Name", "Writer Name", "Publisher") if t.find(x) > 0] or [0]) - 100)
    print("--- TAG-STRIPPED TEXT (spec region, 1500) ---")
    print(t[i:i + 1500])
    print("\n--- PARSED ---")
    cmd_book(arg)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else "kainkaryam"
    if cmd == "sitemap":
        cmd_sitemap()
    elif cmd == "book":
        cmd_book(arg)
    elif cmd == "raw":
        cmd_raw(arg)
    elif cmd == "cats":
        cmd_cats()
    elif cmd == "enumerate":
        enumerate_all()
    else:
        run()