"""
The Book Addicts (thebookaddicts.com) — SECOND-HAND Malayalam bookstore, Shopify.

SINGLE-PHASE JSON scrape (no detail fetches, no HTML parsing, no bot handling):
    /products.json?limit=250&page=<N>   (falls back to /collections/all/products.json)
robots.txt explicitly permits this ("Public product, collection ... is crawlable",
Allow: /). Only cart/checkout/account and AJAX surfaces are disallowed — untouched.

THIS STORE USES SHOPIFY VARIANT OPTIONS AS A METADATA SCHEMA (rare + excellent):
    "options":  [{"name":"Author","position":1}, {"name":"ISBN Paper","position":2},
                 {"name":"Edition","position":3}]
    "variants": [{"option1":"LILLY BABU JOSE", "option2":"9788126429523", "option3":"1",
                  "price":"39.99", "compare_at_price":"80.00", "sku":"382336",
                  "grams":146, "available":false}]
=> author, REAL ISBN-13s, and edition are STRUCTURED fields, not prose.

IMPORTANT: options are mapped BY NAME, never by position — some books have only
Author/ISBN (option3 is null), so assuming option1==author would break on those.

Other fields:
    product_type -> binding ("Paperback")
    tags         -> category + language (NOVEL, MEMOIRS, COOKERY, STORY, MALAYALAM)
    compare_at_price -> genuine MRP (this is a discount store: Rs 80 -> Rs 39.99)
    grams -> weight;  body_html -> bilingual blurb (Malayalam + English gloss)

NOT AVAILABLE from the API: publisher, page count. (Per your call, we don't fetch
product pages; those two stay N/A.)

Run:
  python scripts/thebookaddicts.py peek [page]  -> parse+print first products
  python scripts/thebookaddicts.py count        -> total + ISBN/author coverage
  python scripts/thebookaddicts.py opts         -> audit the option NAMES used
  python scripts/thebookaddicts.py              -> full crawl (resumable by page)
Pace: BA_MIN_DELAY / BA_MAX_DELAY (default 0.5-1.2s).  Test cap: BA_MAX_PAGES.
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

BASE = "https://thebookaddicts.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
LIMIT = int(os.environ.get("BA_LIMIT", "50"))   # Shopify throttles bursts of 250
MIN_DELAY = float(os.environ.get("BA_MIN_DELAY", "3"))
MAX_DELAY = float(os.environ.get("BA_MAX_DELAY", "6"))
MAX_PAGES = int(os.environ.get("BA_MAX_PAGES", "0"))     # 0 = until empty
STATE_FILE = os.environ.get("BA_STATE", "/app/scripts/.bookaddicts_page.json")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "application/json, */*;q=0.1"})
_PATH = {"p": "/products.json"}

# tags that denote LANGUAGE rather than a genre/category
LANG_TAGS = {"MALAYALAM", "ENGLISH", "HINDI", "TAMIL", "SANSKRIT", "ARABIC", "KANNADA"}
# tags that are merchandising noise, not real categories
JUNK_TAGS = re.compile(r"%\s*off|^sale$|^offer|^new$|^featured$|^discount", re.I)

ISBN_REAL = re.compile(r"^(?:97[89]\d{10}|\d{9}[\dXx])$")


def nap():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


_RL = {"strikes": 0}


def fetch_page(page):
    for path in (_PATH["p"], "/collections/all/products.json"):
        url = f"{BASE}{path}?limit={LIMIT}&page={page}"
        for attempt in range(3):
            try:
                r = SESSION.get(url, timeout=45)
                if r.status_code in (429, 503):
                    # Shopify edge throttling. Hammering a limiter DEEPENS the block,
                    # so honour Retry-After once, then give up and tell the user to wait.
                    _RL["strikes"] += 1
                    wait = float(r.headers.get("Retry-After") or 0) or 60
                    if _RL["strikes"] >= 2:
                        print(f"\n   !! {r.status_code} rate-limited repeatedly (Retry-After: {wait:.0f}s).")
                        print("   !! Your IP is in a cooldown. STOP and wait 30-60 min without")
                        print("   !! touching the site, then rerun — progress is checkpointed.")
                        raise SystemExit(1)
                    print(f"   {r.status_code}; honouring Retry-After {wait:.0f}s (strike {_RL['strikes']}/2)")
                    time.sleep(wait)
                    continue
                if r.status_code in (404, 500):
                    break
                r.raise_for_status()
                prods = (r.json() or {}).get("products", []) or []
                _PATH["p"] = path
                _RL["strikes"] = 0          # a clean fetch clears the strike counter
                return prods
            except SystemExit:
                raise
            except json.JSONDecodeError:
                break
            except Exception as e:
                print(f"   err (try {attempt+1}): {e}")
                time.sleep(min(60, 5 * (2 ** attempt)))
    return []


# ---- field extraction ---------------------------------------------------
def _clean(v):
    v = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", str(v or ""))
    return re.sub(r"\s+", " ", _html.unescape(v)).strip(" :\u00a0\t\n")


def _body_text(body_html):
    h = re.sub(r"(?i)<br\s*/?>", "\n", body_html or "")
    h = re.sub(r"(?i)</(p|div|li|h\d)>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = _html.unescape(h)
    h = re.sub(r"[ \t\u00a0]+", " ", h)
    return "\n".join(ln.strip() for ln in h.split("\n") if ln.strip())


def option_map(p):
    """{option_name_lower: 'option1'|'option2'|'option3'} — mapped BY NAME.
    Never assume option1==Author: some books have no Edition (option3 is null)."""
    out = {}
    for o in p.get("options") or []:
        if not isinstance(o, dict):
            continue
        name = _clean(o.get("name") or "").lower()
        pos = o.get("position")
        if name and isinstance(pos, int) and 1 <= pos <= 3:
            out[name] = f"option{pos}"
    return out


def _opt(variant, omap, *names):
    """Value of the first matching option name (substring-tolerant:
    'ISBN Paper' / 'ISBN' / 'isbn paper' all resolve)."""
    for want in names:
        w = want.lower()
        for name, key in omap.items():
            if name == w or w in name or name in w:
                val = variant.get(key)
                if val:
                    return _clean(val)
    return ""


def split_tags(tags):
    """(language, categories) from the tag list."""
    langs, cats = [], []
    for t in tags or []:
        t = _clean(t)
        if not t or JUNK_TAGS.search(t):
            continue
        if t.upper() in LANG_TAGS:
            langs.append(t.title())
        else:
            cats.append(t.title())
    return langs, cats


def parse_product(p):
    title = _clean(p.get("title") or "")
    handle = p.get("handle") or ""
    variants = p.get("variants") or []
    v0 = variants[0] if variants else {}
    omap = option_map(p)

    author = _opt(v0, omap, "author", "authors", "writer")
    isbn_raw = _opt(v0, omap, "isbn paper", "isbn", "isbn 13")
    isbn = re.sub(r"[^0-9Xx]", "", isbn_raw)
    edition = _opt(v0, omap, "edition")

    # fallback: variant title is "AUTHOR / ISBN / EDITION"
    if (not author or not isbn) and v0.get("title"):
        parts = [x.strip() for x in str(v0["title"]).split("/")]
        if not author and parts:
            author = _clean(parts[0])
        if not isbn:
            for x in parts:
                d = re.sub(r"[^0-9Xx]", "", x)
                if ISBN_REAL.match(d):
                    isbn = d
                    break

    langs, cats = split_tags(p.get("tags"))
    language = ", ".join(langs) or "Malayalam"

    price = str(v0.get("price") or "").strip()
    cap = v0.get("compare_at_price")
    mrp = str(cap).strip() if cap else ""
    discount = ""
    try:
        if mrp and price and float(mrp) > float(price) > 0:
            discount = str(round((float(mrp) - float(price)) / float(mrp) * 100))
    except Exception:
        pass
    if mrp == price:
        mrp = ""

    grams = v0.get("grams") or 0
    weight = f"{grams} g" if grams else ""
    stock = "In Stock" if v0.get("available") else "Out of Stock"
    sku = _clean(v0.get("sku") or "")

    imgs = p.get("images") or []
    image = (imgs[0].get("src") if imgs and isinstance(imgs[0], dict) else "") or ""

    desc = _body_text(p.get("body_html"))

    def val(x):
        return x if x else "N/A"

    return {
        "title": val(title),
        "author": val(author),
        "publisher": "N/A",          # not exposed by this store's API
        "category": val(", ".join(dict.fromkeys(cats))),
        "isbn": val(isbn),
        "isbn_is_real": "yes" if isbn and ISBN_REAL.match(isbn) else "no",
        "pages": "N/A",              # not exposed by this store's API
        "binding": val(_clean(p.get("product_type") or "")),
        "edition": val(edition),
        "language": val(language),
        "condition": "Second Hand",  # this is a used-book store
        "price": val(price),
        "mrp": val(mrp),
        "discount": val(discount),
        "stock": stock,
        "sku": val(sku),
        "item_weight": val(weight),
        "vendor": val(_clean(p.get("vendor") or "")),
        "description": desc or "N/A",
        "url": f"{BASE}/products/{handle}" if handle else "N/A",
        "image_url": val(image),
    }


# ---- state --------------------------------------------------------------
def _save_page(page):
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        json.dump({"next_page": page}, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"   state save warn: {e}")


def _load_page():
    try:
        return int(json.load(open(STATE_FILE, encoding="utf-8")).get("next_page", 1))
    except Exception:
        return 1


# ---- run ----------------------------------------------------------------
def run():
    page = _load_page()
    print(f"crawling products.json from page {page} (limit {LIMIT})")
    total, dbfail, isbns, authors = 0, 0, 0, 0
    while True:
        if MAX_PAGES and page > MAX_PAGES:
            print(f"  reached BA_MAX_PAGES={MAX_PAGES}; stopping (resumable).")
            break
        prods = fetch_page(page)
        if not prods:
            print(f"  page {page}: empty -> end of catalog")
            break
        rows = [parse_product(p) for p in prods]
        try:
            scriptkit.save("thebookaddicts", rows, key_fields=["url"])
        except Exception as e:
            dbfail += 1
            print(f"  !! DB save failed ({dbfail}/5) on page {page}: {e}")
            if dbfail >= 5:
                print("  !! aborting: database unreachable; rerun to resume.")
                break
            time.sleep(10)
            continue
        dbfail = 0
        total += len(rows)
        isbns += sum(1 for r in rows if r["isbn"] != "N/A")
        authors += sum(1 for r in rows if r["author"] != "N/A")
        _save_page(page + 1)
        s = rows[0]
        print(f"  page {page}: +{len(rows)} (total {total}, isbn {isbns}, author {authors}) | "
              f"{s['title'][:22]} | {s['isbn']} | Rs{s['price']}/{s['mrp']}")
        page += 1
        nap()
    print(f"\nDone. Saved/updated {total} books ({isbns} with ISBN, {authors} with author).")


# ---- probes -------------------------------------------------------------
def cmd_peek(page=1):
    prods = fetch_page(int(page))
    print(f"page {page}: {len(prods)} products\n")
    for p in prods[:5]:
        rec = parse_product(p)
        for k, v in rec.items():
            if k == "description":
                v = (v or "")[:70].replace("\n", " ") + ("…" if len(v or "") > 70 else "")
            print(f"  {k:>13}: {str(v)[:90]}")
        print()


def cmd_count(max_pages=3):
    """Samples a few pages only. Walking the whole catalog just to count is what
    triggered Shopify's rate limiter — the real run reports these tallies anyway."""
    page, total, isbns, authors, real = 1, 0, 0, 0, 0
    while page <= int(max_pages):
        prods = fetch_page(page)
        if not prods:
            break
        for p in prods:
            r = parse_product(p)
            total += 1
            if r["isbn"] != "N/A":
                isbns += 1
            if r["isbn_is_real"] == "yes":
                real += 1
            if r["author"] != "N/A":
                authors += 1
        print(f"  page {page}: {len(prods)} (running total {total})")
        page += 1
        nap()
    print(f"\nsampled {total} products over {page-1} page(s) @ {LIMIT}/page")
    print(f"  with ISBN        : {isbns}/{total}")
    print(f"  REAL ISBN-10/13  : {real}/{total}")
    print(f"  with author      : {authors}/{total}")


def cmd_opts():
    """Audit which option NAMES the store actually uses (validates the by-name map)."""
    from collections import Counter
    names, page = Counter(), 1
    while page <= 2:
        prods = fetch_page(page)
        if not prods:
            break
        for p in prods:
            for o in p.get("options") or []:
                if isinstance(o, dict):
                    names[f"{o.get('position')}:{_clean(o.get('name'))}"] += 1
        page += 1
        nap()
    print("option names in use (position:name -> count):")
    for k, n in names.most_common():
        print(f"   {k:<24} {n}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "peek":
        cmd_peek(arg or 1)
    elif cmd == "count":
        cmd_count(arg or 3)
    elif cmd == "opts":
        cmd_opts()
    else:
        run()