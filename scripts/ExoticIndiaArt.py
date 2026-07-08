"""
ExoticIndiaArt book crawler — PAGINATION-DRIVEN (full catalogue, correct category).

The site uses path-based numbered pagination (e.g. /book/hindi/ has ~795 pages);
query-string paging is blocked by robots (`Disallow: /*?`). We don't hardcode the
page-URL format — we FOLLOW the page's own "next" link (rel=next, else the link
whose text is the next page number). Category comes from the crawl path, so it's
always correct (fixes the earlier "Wishlist" bug from parsing the detail page).

Two phases, both resumable:
  Phase 1  crawl_categories(): walk every category's pages, build {url: category}
           map -> cached to MAP_FILE. (~thousands of listing fetches.)
  Phase 2  run(): fetch each book's detail page, parse full fields, save. Done
           URLs checkpointed to DONE_FILE.

Run order:
  1) count_urls()            -> how many unique book URLs across all categories.
  2) run(limit=20)           -> smoke test end to end.
  3) run()                   -> full crawl (resumable; rerun to continue).
"""
import json
import os
import re
import time
from urllib.parse import urljoin

import requests
from parsel import Selector

import scriptkit

BASE = "https://www.exoticindiaart.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TOP_CATEGORIES = [
    "hindu", "ayurveda", "tantra", "hindi", "regionallanguages", "yoga",
    "rare", "sanskrit", "astrology", "performingarts", "languageandliterature",
    "history", "buddhist", "artandarchitecture", "philosophy", "audiovideo",
    "bundles",
]

DETAIL_DELAY = 0.3
LISTING_DELAY = 0.3
MAX_PAGES = 2000                      # per-category safety cap
MAP_FILE = os.environ.get("EXOTIC_MAP", "/app/scripts/.exoticindia_urls.json")
DONE_FILE = os.environ.get("EXOTIC_DONE", "/app/scripts/.exoticindia_done.txt")

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ---- fetch ---------------------------------------------------------------
def fetch_text(url):
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"   retry {attempt + 1}/3 {url} -> {e}")
            time.sleep(4)
    return ""


# ---- pagination: follow the page's own next link -------------------------
def _next_link(html, base_url, cat, cur_page):
    sel = Selector(text=html)
    href = sel.css('a[rel="next"]::attr(href)').get()
    if href:
        return urljoin(base_url, href)
    target = str(cur_page + 1)
    cands = []
    for a in sel.css("a"):
        if "".join(a.css("::text").getall()).strip() == target:
            h = a.attrib.get("href")
            if h:
                cands.append(urljoin(base_url, h))
    # keep pagination within this category; never wander into the nav menu
    for h in cands:
        if f"/book/{cat}" in h:
            return h
    return None


# ---- phase 1: build {url: category} map ----------------------------------
def _load_map():
    try:
        with open(MAP_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("urls", {}), set(d.get("cats_done", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}, set()


def _save_map(urls, cats_done):
    tmp = MAP_FILE + ".tmp"
    os.makedirs(os.path.dirname(MAP_FILE) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"urls": urls, "cats_done": sorted(cats_done)}, f)
    os.replace(tmp, MAP_FILE)


def crawl_categories(cats=None):
    cats = cats or TOP_CATEGORIES
    urls, done = _load_map()
    for cat in cats:
        if cat in done:
            print(f"== {cat}: cached, skipping")
            continue
        url, page, seen, new_here = f"{BASE}/book/{cat}/", 1, set(), 0
        while page <= MAX_PAGES and url and url not in seen:
            seen.add(url)
            html = fetch_text(url)
            if not html:
                break
            links = Selector(text=html).css('a[href*="/book/details/"]::attr(href)').getall()
            for h in links:
                u = urljoin(BASE, h).split("?")[0]
                if u not in urls:
                    urls[u] = cat
                    new_here += 1
            print(f"  {cat} p{page}: {len(links)} links | map={len(urls)}")
            nxt = _next_link(html, url, cat, page)
            if not nxt or nxt in seen:
                break
            url, page = nxt, page + 1
            time.sleep(LISTING_DELAY)
        done.add(cat)
        _save_map(urls, done)
        print(f"== {cat}: +{new_here} new over {page} page(s)")
    return urls


def count_urls():
    urls = crawl_categories()
    print(f"\n==> {len(urls)} unique book URLs discovered across categories")


# ---- detail parsing (category supplied by caller) ------------------------
LANGS = (r"English|Hindi|Sanskrit|Marathi|Tamil|Telugu|Malayalam|Bengali|"
         r"Gujarati|Kannada|Oriya|Punjabi|Urdu|Assamese|Nepali|Pali")
STOP = (r"(?:Author|Publisher|Language|ISBN|Pages|Cover|Edition|Size|Weight|"
        r"Item Code|Binding|Other Details|$)")
SPEC_MARKERS = ("Item Code", "Cover", "Edition", "Binding", "ISBN",
                "Weight", "Author", "Publisher")


def _field(label, text):
    m = re.search(rf"\b{label}\s*:?\s*([^\n;|]{{2,90}}?)(?=\s*{STOP})", text)
    return m.group(1).strip() if m else None


def _spec_region(text):
    start = len(text)
    for marker in SPEC_MARKERS:
        p = text.find(marker)
        if 0 <= p < start:
            start = p
    return text[max(0, start - 120):] if start < len(text) else text


def parse_detail(html, url, category="book"):
    sel = Selector(text=html)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    spec = _spec_region(text)

    title = (sel.css("h1::text").get() or sel.css("h1 *::text").get() or "Unknown").strip()

    author = _field("Author", spec)
    if not author:
        m = re.search(r"\bBy\s+([A-Z][A-Za-z.\-'& ]{2,70})", spec)
        author = m.group(1).strip() if m else "N/A"

    publisher = _field("Publisher", spec) or "N/A"

    isbn = "N/A"
    m = re.search(r"ISBN[^\dXx]{0,6}([\dXx][\dXx \-]{8,18}[\dXx])", spec)
    if m:
        isbn = re.sub(r"[ \-]", "", m.group(1))

    language = _field("Language", spec)
    if not language:
        m = re.search(rf"\b({LANGS})\b", spec)
        language = m.group(1) if m else "N/A"

    pages = "N/A"
    m = re.search(r"\bPages\s*:?\s*([0-9]{1,5})", spec)
    if m:
        pages = m.group(1)

    price = "N/A"
    m = re.search(r"(?:\u20b9|Rs\.?|\$)\s*([0-9][0-9,]*\.?[0-9]*)", text)
    if m:
        price = m.group(1).replace(",", "")

    image = sel.css('meta[property="og:image"]::attr(content)').get() or ""

    return {
        "title": title, "author": author, "publisher": publisher,
        "price": price, "mrp": "N/A", "isbn": isbn, "language": language,
        "pages": pages, "category": category, "url": url, "image_url": image,
    }


# ---- phase 2 checkpoint --------------------------------------------------
def _load_done():
    try:
        with open(DONE_FILE, encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()


def _open_done():
    try:
        os.makedirs(os.path.dirname(DONE_FILE) or ".", exist_ok=True)
        return open(DONE_FILE, "a", encoding="utf-8")
    except Exception:
        return open("/tmp/exoticindia_done.txt", "a", encoding="utf-8")


# ---- phase 2: crawl details ----------------------------------------------
def run(limit=None):
    urls, _ = _load_map()
    if not urls:
        print("No URL map yet — running phase 1 (category crawl)...")
        urls = crawl_categories()
    done = _load_done()
    todo = [u for u in urls if u not in done]
    if limit:
        todo = todo[:limit]
    print(f"map={len(urls)}  done={len(done)}  to crawl={len(todo)}")

    fh = _open_done()
    saved, t0 = 0, time.time()
    for i, url in enumerate(todo, 1):
        html = fetch_text(url)
        if not html:
            continue
        try:
            rec = parse_detail(html, url, urls.get(url, "book"))
            scriptkit.save("exoticindia", [rec], key_fields=["url"])
            fh.write(url + "\n")
            fh.flush()
            saved += 1
            if i == 1 or i % 25 == 0:
                rate = i / max(1e-9, time.time() - t0)
                eta = (len(todo) - i) / max(1e-9, rate) / 60
                print(f"  [{i}/{len(todo)}] {saved} saved | {rate:.1f}/s | "
                      f"ETA {eta:.0f} min | {rec['category']}: {rec['title'][:28]}")
        except Exception as e:
            print(f"  !! parse {url}: {e}")
        time.sleep(DETAIL_DELAY)
    fh.close()
    print(f"\nDone. Saved/updated {saved} this run ({len(done) + saved} total).")


# ---- inspector -----------------------------------------------------------
def inspect_one(url=f"{BASE}/book/details/intermediate-level-hindi-textbook-nam753/"):
    html = fetch_text(url)
    print("=== PARSED ===")
    for k, v in parse_detail(html, url, "hindi").items():
        print(f"{k:>10}: {v}")


if __name__ == "__main__":
    run()