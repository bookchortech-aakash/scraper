"""
Noolulagam (noolulagam.com/books) — Tamil bookstore, Next.js rebuild crawler.

NOTE: This supersedes the old NoolUlagam.py. The site is NO LONGER WooCommerce —
it was rebuilt as a custom Next.js app (build v0.1.35), server-rendered, images on
Cloudflare R2. No gate / WAF / TLS challenge observed. Plain requests + tag-stripped
text parsing works; no browser engine needed.

Catalog: ~52,237 books, listing paginated /books?page=1..2177 (~24/page).
Product URLs are numeric: /books/<id>. Filters are id-based: ?author= / ?publisherId=
/ ?category= / ?q=. Cover image is deterministic: R2/books/<id>.jpg.

WHERE FIELDS LIVE
  Listing card (phase 1):  id, title(alt), author_id, PRICE, MRP, stock
                           (MRP only appears on the card, not the detail page!)
  Detail page  (phase 2):  author/publisher/category (name + id via ?param= href),
                           Pages, ISBN, Edition, Published Year, Weight, Binding,
                           Language, description.

Two resumable phases:
  1) enumerate — walk /books?page=1..N, store full CARD data keyed by id
  2) detail    — fetch /books/<id>, merge rich fields, scriptkit.save (DB-guarded)

Run:
  python scripts/noolulagam.py listing            -> page-1 cards (price/mrp/stock)
  python scripts/noolulagam.py book <id>          -> full merged record for one book
  python scripts/noolulagam.py raw <id>           -> DIAGNOSTIC: dump the spec region
                                                     (raw + tag-stripped) so selectors
                                                     can be tightened to the real HTML
  python scripts/noolulagam.py enumerate          -> phase 1 only
  python scripts/noolulagam.py                     -> enumerate + detail crawl
Pace: NU_MIN_DELAY / NU_MAX_DELAY (default 1-2s).
"""
import html as _html
import json
import math
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

BASE = "https://www.noolulagam.com"
R2 = "https://pub-c0a25a58b54c4f25b1f6499820508f6b.r2.dev"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MIN_DELAY = float(os.environ.get("NU_MIN_DELAY", "1.0"))
MAX_DELAY = float(os.environ.get("NU_MAX_DELAY", "2.0"))
CARDS_FILE = os.environ.get("NU_CARDS", "/app/scripts/.noolulagam_cards.json")
DONE_FILE = os.environ.get("NU_DONE", "/app/scripts/.noolulagam_done.txt")
PER_PAGE = 24

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


# ---- text helpers -------------------------------------------------------
def _clean(v):
    v = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", str(v or ""))
    v = _html.unescape(v)
    return re.sub(r"\s+", " ", v).strip(" :\u00a0\t\n")


def _text(html):
    """Tag-stripped, entity-decoded, whitespace-collapsed text of an HTML blob."""
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    h = re.sub(r"(?i)<br\s*/?>", " ", h)
    h = re.sub(r"<[^>]+>", " ", h)
    return _clean(h)


# ---- listing (phase 1) --------------------------------------------------
def catalog_total(html):
    t = _text(html)
    m = re.search(r"([\d,]+)\s*(?:நூல்கள்|/\s*books|books)", t)
    return int(m.group(1).replace(",", "")) if m else 0


def last_page(html):
    total = catalog_total(html)
    if total:
        return math.ceil(total / PER_PAGE)
    m = re.search(r"\b1\s*/\s*([\d,]+)\b", _text(html))
    return int(m.group(1).replace(",", "")) if m else 0


def parse_cards(html):
    """Segment listing HTML by each /books/<id> anchor; extract card fields.
    Discount is COMPUTED from price/mrp (badge sits before the anchor and is
    easy to misattribute across card boundaries)."""
    order, first = [], {}
    for m in re.finditer(r"/books/(\d+)", html):
        bid = m.group(1)
        if bid not in first:
            first[bid] = m.start()
            order.append(bid)
    cards = []
    for i, bid in enumerate(order):
        start = first[bid]
        end = first[order[i + 1]] if i + 1 < len(order) else len(html)
        seg = html[start:end]
        segt = _text(seg)
        amts = [a.replace(",", "") for a in re.findall(r"₹\s*([\d,]+(?:\.\d+)?)", segt)]
        price = amts[0] if amts else ""
        mrp = amts[1] if len(amts) > 1 else ""
        alt = re.search(r'alt="([^"]*)"', seg)
        title = _clean(alt.group(1)) if alt else ""
        aid = re.search(r"\?author=(\d+)", seg)
        stock = "Out of Stock" if re.search(r"out of stock", segt, re.I) else "In Stock"
        disc = ""
        try:
            if price and mrp and float(mrp) > float(price) > 0:
                disc = str(round((float(mrp) - float(price)) / float(mrp) * 100))
        except Exception:
            pass
        cards.append({
            "bookcode": bid,
            "url": f"{BASE}/books/{bid}",
            "title": title,
            "author_id": aid.group(1) if aid else "",
            "price": price,
            "mrp": mrp,
            "discount": disc,
            "stock": stock,
            "image_url": f"{R2}/books/{bid}.jpg",
        })
    return cards


# ---- detail (phase 2) ---------------------------------------------------
_LABELS = ["Author", "Publisher", "Category", "Pages", "ISBN", "Edition",
           "Published Year", "Weight", "Binding", "Language"]
_TERMS = _LABELS + ["About Book", "Topics", "Reviews", "விளக்கம்", "குறியீடுகள்"]


def _spec(text, label):
    """Value that follows an English label, up to the next known label/section."""
    others = "|".join(re.escape(t) for t in _TERMS if t != label)
    m = re.search(re.escape(label) + r"\s*[:\-]?\s*(.+?)\s*(?=" + others + r"|$)", text)
    return _clean(m.group(1)) if m else ""


def _spec_link(html, label, param):
    """(tamil_name, english_name) for an Author/Publisher/Category spec row.
    Anchored on the row LABEL (`>Author</span><a ?author=…>ta</a><span>en</span>`)
    so the breadcrumb's bare category link can't shadow it. English name optional."""
    base = r">\s*" + re.escape(label) + r"\s*</span>\s*<a[^>]*\?" + param + r"=\d+\"[^>]*>\s*([^<]+?)\s*</a>"
    m = re.search(base + r"\s*<span[^>]*>\s*([^<]*?)\s*</span>", html)
    if m:
        return _clean(m.group(1)), _clean(m.group(2))
    m = re.search(base, html)
    return (_clean(m.group(1)), "") if m else ("", "")


def parse_detail(html, book_id, card=None):
    card = card or {}
    before = html.split("About Book", 1)[0] if "About Book" in html else html
    btext = _text(before)

    # title: <h1> Tamil, subtitle (transliteration) is the next line/element
    h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", html)
    title = _clean(h1.group(1)) if h1 else card.get("title", "")
    title_en = ""
    if h1:
        tail = _text(html[h1.end():h1.end() + 300])
        tm = re.match(r"\s*([A-Za-z0-9][^₹]{2,120}?)\s*(?=₹|Free shipping|Add to Cart|$)", tail)
        if tm:
            title_en = _clean(tm.group(1))

    a_ta, a_en = _spec_link(before, "Author", "author")
    p_ta, p_en = _spec_link(before, "Publisher", "publisherId")
    c_ta, c_en = _spec_link(before, "Category", "category")

    isbn = re.sub(r"[^0-9Xx]", "", _spec(btext, "ISBN"))
    pages = re.sub(r"[^\d]", "", _spec(btext, "Pages"))
    edition = _spec(btext, "Edition")
    ym = re.search(r"\d{4}", _spec(btext, "Published Year"))
    year = ym.group(0) if ym else ""
    weight = _spec(btext, "Weight")
    binding = _spec(btext, "Binding")
    language = _spec(btext, "Language") or "Tamil"

    # price: prefer the card (has MRP); detail page shows selling price only
    price = card.get("price", "")
    if not price:
        pm = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", _text(html))
        price = pm.group(1).replace(",", "") if pm else ""

    # description: between "About Book" and "Topics"
    desc = ""
    if "About Book" in html:
        chunk = html.split("About Book", 1)[1]
        chunk = chunk.split("Topics", 1)[0]
        dt = _text(chunk)
        dt = re.sub(r"^.*?விமர்சனம்\s*\d*\s*", "", dt)   # drop the Reviews / விமர்சனம் 0 prefix
        desc = dt.strip()

    stock = card.get("stock") or ("Out of Stock" if re.search(r"out of stock", _text(html), re.I) else "In Stock")

    def v(x):
        return x if x else "N/A"

    rec = {
        "title": v(title),
        "title_en": title_en or "N/A",
        "author": v(a_ta),
        "author_en": a_en or "N/A",
        "publisher": v(p_ta),
        "publisher_en": p_en or "N/A",
        "category": v(c_ta),
        "category_en": c_en or "N/A",
        "isbn": v(isbn),
        "pages": v(pages),
        "edition": v(edition),
        "year": v(year),
        "item_weight": v(weight),
        "binding": v(binding),
        "language": v(language),
        "price": v(price),
        "mrp": v(card.get("mrp", "")),
        "stock": v(stock),
        "description": desc or "N/A",
        "url": f"{BASE}/books/{book_id}",
        "image_url": f"{R2}/books/{book_id}.jpg",
    }
    return rec


# ---- state --------------------------------------------------------------
def _save_cards(state):
    try:
        os.makedirs(os.path.dirname(CARDS_FILE) or ".", exist_ok=True)
        tmp = CARDS_FILE + ".tmp"
        json.dump(state, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, CARDS_FILE)
    except Exception as e:
        print(f"   cards save warn: {e}")


def _load_cards():
    try:
        return json.load(open(CARDS_FILE, encoding="utf-8"))
    except Exception:
        return {"next_page": 1, "last_page": 0, "cards": {}}


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
        return open("/tmp/noolulagam_done.txt", "a", encoding="utf-8")


# ---- phases -------------------------------------------------------------
def enumerate_all():
    st = _load_cards()
    cards = st.get("cards", {})
    page = st.get("next_page", 1)
    first = get(f"{BASE}/books" if page == 1 else f"{BASE}/books?page={page}")
    lp = st.get("last_page") or last_page(first)
    print(f"catalog ~{catalog_total(first) or '?'} books (~{lp or '?'} pages); "
          f"resuming page {page}, {len(cards)} cards so far")
    html, empty = first, 0
    while True:
        if lp and page > lp + 1:
            break
        if html is None:
            html = get(f"{BASE}/books" if page == 1 else f"{BASE}/books?page={page}")
        page_cards = parse_cards(html) if html else []
        new = 0
        for c in page_cards:
            if c["bookcode"] not in cards:
                new += 1
            cards[c["bookcode"]] = c        # refresh price/stock on re-scrape
        if not page_cards:
            empty += 1
            if empty >= 2:
                print(f"  page {page}: empty x2, stopping")
                break
        else:
            empty = 0
        print(f"  page {page}/{lp or '?'}: +{new} (total {len(cards)})")
        _save_cards({"next_page": page + 1, "last_page": lp, "cards": cards})
        page += 1
        html = None
        nap()
    _save_cards({"next_page": page, "last_page": lp, "cards": cards})
    print(f"\nenumeration done: {len(cards)} cards")
    return cards


def run():
    cards = enumerate_all()
    done = _load_done()
    fh = _open_done()
    ids = list(cards.keys())
    todo = [i for i in ids if i not in done]
    limit = int(os.environ.get("NU_LIMIT", "0"))   # 0 = all; e.g. NU_LIMIT=30 for a test batch
    print(f"enriching {len(ids)} books ({len(ids)-len(todo)} done, {len(todo)} to go)"
          + (f" | LIMIT={limit}" if limit else ""))
    t0, sess, dbfail = time.time(), 0, 0
    for bid in ids:
        if bid in done:
            continue
        if limit and sess >= limit:
            print(f"  reached NU_LIMIT={limit}; stopping (resumable).")
            break
        html = get(f"{BASE}/books/{bid}")
        if not html:
            print(f"  {bid}: no detail HTML (404?); skipping")
            fh.write(bid + "\n"); fh.flush(); done.add(bid)
            continue
        rec = parse_detail(html, bid, cards.get(bid))
        try:
            scriptkit.save("noolulagam", [rec], key_fields=["url"])
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
def cmd_listing():
    html = get(f"{BASE}/books")
    cards = parse_cards(html)
    print(f"total ~{catalog_total(html)} | last_page ~{last_page(html)} | page 1: {len(cards)} cards")
    for c in cards[:6]:
        print(f"   {c['bookcode']:>6} | ₹{c['price']}/{c['mrp'] or '-'} ({c['discount'] or '0'}%) "
              f"| {c['stock']:>12} | {c['title'][:40]}")


def cmd_book(bid):
    bid = str(bid).rstrip("/").split("/books/")[-1].split("/")[0]
    # try to pull the card from a fresh page-1 (cheap) so price/mrp are present
    card = None
    lst = _load_cards().get("cards", {})
    if bid in lst:
        card = lst[bid]
    rec = parse_detail(get(f"{BASE}/books/{bid}"), bid, card)
    for k, val in rec.items():
        s = val if isinstance(val, str) else str(val)
        print(f"  {k:>13}: {s[:100]}")


def cmd_raw(bid):
    """Dump the spec region so selectors can be matched to the REAL fetched HTML."""
    bid = str(bid).rstrip("/").split("/books/")[-1].split("/")[0]
    html = get(f"{BASE}/books/{bid}")
    print(f"=== fetched {len(html)} chars for /books/{bid} ===\n")
    # locate the spec block: from the first ?author= link to 'About Book'
    a = re.search(r'href="[^"]*\?author=\d+"', html)
    start = max(0, (a.start() - 200) if a else 0)
    end = html.find("About Book")
    end = end if end != -1 else min(len(html), start + 4000)
    block = html[start:end][:4000]
    print("--- RAW HTML (spec region, 4k) ---")
    print(block)
    print("\n--- TAG-STRIPPED TEXT (spec region) ---")
    print(_text(html.split('About Book', 1)[0])[-1200:])
    print("\n--- PARSED ---")
    cmd_book(bid)


def cmd_rawlist():
    """Dump the first listing card's RAW HTML so price/mrp/title selectors can be
    matched to the real markup (my listing fixture was a guess)."""
    html = get(f"{BASE}/books")
    print(f"=== fetched {len(html)} chars for /books ===\n")
    m = re.search(r'href="/books/\d+"', html) or re.search(r"/books/\d+", html)
    start = max(0, m.start() - 400) if m else 0
    print("--- RAW HTML (first card region, ~3.5k) ---")
    print(html[start:start + 3500])
    print("\n--- parse_cards result (first 4) ---")
    for c in parse_cards(html)[:4]:
        print("  ", {k: c[k] for k in ("bookcode", "title", "price", "mrp", "discount", "stock")})


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else "31146"
    if cmd == "listing":
        cmd_listing()
    elif cmd == "book":
        cmd_book(arg)
    elif cmd == "raw":
        cmd_raw(arg)
    elif cmd == "rawlist":
        cmd_rawlist()
    elif cmd == "enumerate":
        enumerate_all()
    else:
        run()