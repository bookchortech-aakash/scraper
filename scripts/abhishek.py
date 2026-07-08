"""
Abhishek Publications (abhishekpublications.com/shop) — WooCommerce crawler.

Publisher's own catalog (~1,032 products, all books — no merch filtering needed).
WordPress + WooCommerce, server-rendered, no gate. Standard pagination
  /shop/page/<N>/            (15/page, ~69 pages)
plus a "Show All" option, so enumeration grabs every /product/<slug>/ URL in a
couple of requests. Detail pages carry the full field set: title, author,
category(s), price, MRP, SKU, description — and the description commonly embeds
  Page : NNN   ISBN : ...   Dimensions : W x H x D cm
Binding is often in the title ([hardcover] / (Paperback)).

Captures EVERYTHING the page exposes: JSON-LD (name/author/sku/price/desc), the
WooCommerce additional-information attribute table, and the Page/ISBN/Dimensions/
Weight/Language/Pages/Publisher/Edition/Year fields from the description + specs.

PRICE NOTE: WooCommerce wraps the currency symbol in its own
  <span class="woocommerce-Price-currencySymbol">₹</span>325.00
so the number never sits directly after the ₹ in the raw HTML — a </span> is in
between. We therefore scope to <p class="price">, split <ins> (sale/current) and
<del> (original/MRP), and read amounts from TAG-STRIPPED text (via _clean), which
is how every other field already succeeds.

Two resumable phases (tiny site, ~30 min total):
  1) enumerate all product slugs from the shop listing
  2) fetch each /product/<slug>/ -> full record -> save (DB-guarded checkpoint)

Run:
  python scripts/abhishek.py listing            -> total + first slugs
  python scripts/abhishek.py book <slug>        -> ALL fields for one book
  python scripts/abhishek.py price <slug>       -> dump the <p class=price> block
  python scripts/abhishek.py enumerate          -> phase 1 only
  python scripts/abhishek.py                    -> enumerate + detail crawl
Pace: AB_MIN_DELAY / AB_MAX_DELAY (default 1-2s).
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

BASE = "https://abhishekpublications.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MIN_DELAY = float(os.environ.get("AB_MIN_DELAY", "1.0"))
MAX_DELAY = float(os.environ.get("AB_MAX_DELAY", "2.0"))
SLUGS_FILE = os.environ.get("AB_SLUGS", "/app/scripts/.abhishek_slugs.json")
DONE_FILE = os.environ.get("AB_DONE", "/app/scripts/.abhishek_done.txt")

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
            if r.status_code in (404, 500):
                return ""
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except Exception as e:
            print(f"   err (try {attempt+1}): {e}")
            time.sleep(min(60, 5 * (2 ** attempt)))
    return ""


# ---- listing enumeration ------------------------------------------------
PRODUCT_LINK = re.compile(r'href="https?://abhishekpublications\.com/product/([^"/?#]+)/?"')
TOTAL_RE = re.compile(r'(?:of\s+)?([\d,]+)\s+results', re.I)


def listing_total(html):
    m = TOTAL_RE.search(html)
    return int(m.group(1).replace(",", "")) if m else 0


def listing_slugs(html):
    out, seen = [], set()
    for m in PRODUCT_LINK.finditer(html):
        s = _html.unescape(m.group(1))
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ---- JSON-LD helpers ----------------------------------------------------
def _all_ld(html):
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


# ---- spec / description field extraction --------------------------------
def _clean(v):
    v = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", str(v or ""))  # bidi/zero-width marks
    v = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", v))).strip(" :\u00a0\t")
    return v


# Rupee amounts, read from TAG-STRIPPED + entity-decoded text so a </span>
# sitting between the ₹ symbol and the number can never break the match.
_AMOUNT_RE = re.compile(r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)", re.I)


def _amounts(fragment):
    txt = _clean(fragment)  # _clean already unescapes &#8377;/&#x20b9; -> ₹
    return [a.replace(",", "") for a in _AMOUNT_RE.findall(txt)]


def _attr_table(html):
    """WooCommerce additional-information table: <tr><th>Label</th><td>value</td></tr>."""
    out = {}
    for lab, val in re.findall(
            r'<t[hr][^>]*class="[^"]*woocommerce-product-attributes-item__label[^"]*"[^>]*>(.*?)</t[hd]>\s*'
            r'<td[^>]*woocommerce-product-attributes-item__value[^>]*>(.*?)</td>', html, re.S | re.I):
        k, v = _clean(lab).lower(), _clean(val)
        if k and v:
            out[k] = v
    # generic th/td fallback
    for lab, val in re.findall(r'<tr[^>]*>\s*<t[hd][^>]*>\s*([^<:]{2,40}?)\s*:?\s*</t[hd]>\s*<td[^>]*>(.*?)</td>',
                               html, re.S | re.I):
        k, v = _clean(lab).lower(), _clean(val)
        if k and v:
            out.setdefault(k, v)
    return out


def _from_text(text, *labels):
    """Fields embedded in the description like 'Page : 282' / 'ISBN : 978-...'."""
    for lab in labels:
        m = re.search(rf'{lab}\s*[:\-]\s*([^\n|•·]+?)(?=\s{{2,}}|$|[|•·]|Page\b|ISBN\b|Dimension|Weight|Language|Binding|Author|Publisher|Edition)',
                      text, re.I)
        if m:
            v = m.group(1).strip(" :.-")
            if v:
                return v
    return ""


# ---- price extraction (scoped, tag-stripped) ----------------------------
def _price_block(scope):
    """The <p class="price"> fragment from the product summary (or ''),."""
    m = re.search(r'<p[^>]*class="[^"]*\bprice\b[^"]*"[^>]*>(.*?)</p>', scope, re.S | re.I)
    return m.group(0) if m else ""


def _prices_from_html(scope, prod):
    """(price, mrp) as strings. <ins> = current/sale, <del> = original/MRP.
    Falls back to JSON-LD offers, then to any amount in the price block."""
    price = mrp = ""

    # 1) JSON-LD offers, when the theme emits them
    offers = prod.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if isinstance(offers, dict):
        p = str(offers.get("price") or offers.get("lowPrice") or "").strip()
        if p:
            price = p
        hi = offers.get("highPrice")
        ps = offers.get("priceSpecification")
        if not hi and isinstance(ps, dict):
            hi = ps.get("price")
        if hi:
            mrp = str(hi).strip()

    # 2) The <p class="price"> block: ins = sale, del = original
    block = _price_block(scope)
    if block:
        ins_m = re.search(r'<ins\b[^>]*>(.*?)</ins>', block, re.S | re.I)
        del_m = re.search(r'<del\b[^>]*>(.*?)</del>', block, re.S | re.I)
        ins_amts = _amounts(ins_m.group(1)) if ins_m else []
        del_amts = _amounts(del_m.group(1)) if del_m else []
        if ins_amts:
            price = ins_amts[0]                 # sale price wins as current
        if del_amts and not mrp:
            mrp = del_amts[0]
        if not price:                           # single-price product (no ins/del)
            amts = _amounts(block)
            if amts:
                price = amts[0]
                if len(amts) > 1 and not mrp:
                    mrp = max(amts, key=lambda x: float(x))

    return price, mrp


def parse_detail(html, slug):
    objs = _all_ld(html)
    prod = _first(objs, "Product", "Book")
    attrs = _attr_table(html)
    # full description text (strip tags) for embedded Page/ISBN/Dimensions
    dm = re.search(r'(?is)<div[^>]+(?:woocommerce-product-details__short-description|'
                   r'product_description|tab-description|woocommerce-Tabs-panel--description)[^>]*>(.*?)</div>', html)
    desc_html = dm.group(1) if dm else html
    desc = _clean(desc_html)

    def pick(*labels, ld=None):
        if ld:
            v = _lds(prod.get(ld))
            if v:
                return v
        for lab in labels:
            for ak, av in attrs.items():
                if ak == lab.lower() or ak.startswith(lab.lower()):
                    return av
        return _from_text(desc, *labels)

    title = _lds(prod.get("name")) or _clean(re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S).group(1)) \
        if re.search(r"<h1", html) else slug
    title = _html.unescape(title).strip()

    isbn = (_lds(prod.get("isbn")) or pick("ISBN 13", "ISBN-13", "ISBN13", "ISBN") or "")
    isbn = re.sub(r"[^0-9Xx]", "", isbn)
    author = _lds(prod.get("author")) or pick("Author", "Authors", "Writer")
    if not author:
        # title/slug pattern: "1984 by George Orwell (Hardback)" / "...-by-george-orwell-hardcover"
        bm = re.search(r'\bby\s+([A-Z][^()\[\]]+?)(?:\s*[\(\[]|$)', title)
        if bm:
            author = bm.group(1).strip()
        else:
            sm = re.search(r'-by-([a-z0-9\-]+?)(?:-(?:hardcover|hardback|paperback|hb|pb|h))?$', slug, re.I)
            if sm:
                author = sm.group(1).replace("-", " ").title()
    publisher = _lds(prod.get("publisher")) or pick("Publisher", "Publishers") or "Abhishek Publications"
    pages = pick("Page", "Pages", "No. of Pages", "Number of Pages", ld="numberOfPages")
    pages = re.sub(r"[^\d]", "", pages) if pages else ""
    dimensions = pick("Dimensions", "Dimension", "Size")
    dmm = re.search(r'([\d.]+\s*[x×]\s*[\d.]+(?:\s*[x×]\s*[\d.]+)?\s*(?:cm|mm|inches|in\b)?)',
                    dimensions or "", re.I)
    if dmm:
        dimensions = dmm.group(1).strip()
    weight = pick("Weight")
    language = _lds(prod.get("inLanguage")) or pick("Language") or "English"
    edition = pick("Edition")
    year = pick("Year", "Publication Year", "Published", "Year of Publication")
    sku = _lds(prod.get("sku")) or pick("SKU") or (
        (re.search(r'class="sku"[^>]*>([^<]+)', html) or [None, ""])[1]).strip()
    # binding: attr, else from the title's [hardcover]/(Paperback) marker
    binding = pick("Binding", "Format", "Cover", "Book Type")
    if not binding:
        bm = re.search(r'[\[\(]\s*(hard\s*cover|hardback|paper\s*back|hardbound|paperbound)\s*[\]\)]', title, re.I)
        binding = bm.group(1).title().replace(" ", "") if bm else ""

    # category(s)
    category = ""
    crumb = _first(objs, "BreadcrumbList")
    items = crumb.get("itemListElement") if isinstance(crumb, dict) else None
    if isinstance(items, list):
        names = [_clean(it.get("name") or "") or _lds(it.get("item")) for it in items if isinstance(it, dict)]
        tnorm = re.sub(r"\W+", "", title).lower()
        names = [n for n in names if n and n.lower() not in ("home", "shop", "products")
                 and re.sub(r"\W+", "", n).lower() != tnorm]
        category = " > ".join(names) if names else ""
    if not category:
        cats = re.findall(r'rel="tag"[^>]*>([^<]+)</a>', html) or \
            re.findall(r'/product-category/[^"]+"[^>]*>([^<]+)<', html)
        category = ", ".join(dict.fromkeys(_clean(c) for c in cats if _clean(c)))

    # prices — SCOPE to the main product area: from the <h1> product title up to
    # the "Latest Products"/related sidebar, so OTHER books' prices never bleed in.
    h1 = re.search(r'<h1', html)
    cut = re.search(r'Latest Products|class="[^"]*related|<aside|id="secondary"|<footer', html)
    scope = html[(h1.start() if h1 else 0):(cut.start() if cut else len(html))]
    price, mrp = _prices_from_html(scope, prod)

    img = _lds(prod.get("image")) or (
        (re.search(r'<meta[^>]+og:image[^>]+content="([^"]+)"', html) or [None, ""])[1])

    def v(x):
        return x if x else "N/A"

    return {
        "title": v(title),
        "author": v(author),
        "publisher": v(publisher),
        "isbn": v(isbn),
        "sku": v(sku),
        "pages": v(pages),
        "binding": v(binding),
        "language": v(language),
        "edition": v(edition),
        "year": v(year),
        "dimensions": v(dimensions),
        "weight": v(weight),
        "category": v(category),
        "price": v(price),
        "mrp": v(mrp),
        "url": f"{BASE}/product/{slug}/",
        "image_url": img or "",
    }


# ---- state --------------------------------------------------------------
def _save_slugs(state):
    try:
        os.makedirs(os.path.dirname(SLUGS_FILE) or ".", exist_ok=True)
        tmp = SLUGS_FILE + ".tmp"
        json.dump(state, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, SLUGS_FILE)
    except Exception as e:
        print(f"   slugs save warn: {e}")


def _load_slugs():
    try:
        return json.load(open(SLUGS_FILE, encoding="utf-8"))
    except Exception:
        return {"next_page": 1, "slugs": []}


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
        return open("/tmp/abhishek_done.txt", "a", encoding="utf-8")


# ---- phases -------------------------------------------------------------
def enumerate_all():
    st = _load_slugs()
    slugs = list(dict.fromkeys(st.get("slugs", [])))
    page = st.get("next_page", 1)
    first = get(f"{BASE}/shop/") if page == 1 else get(f"{BASE}/shop/page/{page}/")
    total = listing_total(first)
    last = (total + 14) // 15 if total else 200
    print(f"total ~{total} products (~{last} pages); resuming page {page}, {len(slugs)} slugs so far")
    html, empty = first, 0
    while page <= last + 1:
        if html is None:
            html = get(f"{BASE}/shop/" if page == 1 else f"{BASE}/shop/page/{page}/")
        found = listing_slugs(html)
        new = [s for s in found if s not in slugs]
        if not found:
            empty += 1
            if empty >= 2:
                break
        else:
            empty = 0
            slugs.extend(new)
        print(f"  page {page}/{last}: +{len(new)} (total {len(slugs)})")
        _save_slugs({"next_page": page + 1, "slugs": slugs})
        page += 1
        html = None
        nap()
    _save_slugs({"next_page": page, "slugs": slugs})
    print(f"\nenumeration done: {len(slugs)} product slugs")
    return slugs


def run():
    slugs = enumerate_all()
    done = _load_done()
    fh = _open_done()
    todo = [s for s in slugs if s not in done]
    print(f"enriching {len(slugs)} books ({len(slugs)-len(todo)} done, {len(todo)} to go)")
    t0, sess, dbfail = time.time(), 0, 0
    for slug in slugs:
        if slug in done:
            continue
        rec = parse_detail(get(f"{BASE}/product/{slug}/"), slug)
        try:
            scriptkit.save("abhishek", [rec], key_fields=["url"])
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
                  f"{rec['title'][:24]} | {rec['isbn']} | {rec['pages']}p | ₹{rec['price']}")
        nap()
    fh.close()
    print(f"\nDone. Saved/updated {sess} books this session.")


# ---- probes -------------------------------------------------------------
def cmd_listing():
    html = get(f"{BASE}/shop/")
    slugs = listing_slugs(html)
    print(f"total ~{listing_total(html)} | page 1: {len(slugs)} slugs; first 8:")
    for s in slugs[:8]:
        print("  ", s)


def cmd_book(slug):
    slug = slug.rstrip("/").split("/product/")[-1].split("/")[0]
    rec = parse_detail(get(f"{BASE}/product/{slug}/"), slug)
    for k, v in rec.items():
        print(f"  {k:>12}: {v[:90] if isinstance(v, str) else v}")


def cmd_price(slug):
    """Dump the <p class=price> block + what we parse from it, for diagnosis."""
    slug = slug.rstrip("/").split("/product/")[-1].split("/")[0]
    html = get(f"{BASE}/product/{slug}/")
    h1 = re.search(r'<h1', html)
    cut = re.search(r'Latest Products|class="[^"]*related|<aside|id="secondary"|<footer', html)
    scope = html[(h1.start() if h1 else 0):(cut.start() if cut else len(html))]
    block = _price_block(scope)
    print("=== <p class=price> block (raw) ===")
    print(block[:1000] if block else "(no <p class=price> found in scope)")
    print("\n=== cleaned ===")
    print(_clean(block) if block else "")
    rec = parse_detail(html, slug)
    print(f"\nparsed -> price={rec['price']}  mrp={rec['mrp']}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else "1984"
    if cmd == "listing":
        cmd_listing()
    elif cmd == "book":
        cmd_book(arg)
    elif cmd == "price":
        cmd_price(arg)
    elif cmd == "enumerate":
        enumerate_all()
    else:
        run()