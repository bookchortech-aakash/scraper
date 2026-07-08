"""
DC Books Store (dcbookstore.com) crawler.

The site is behind a JavaScript gate ("You are being redirected... Javascript is
required") that blocks plain requests on every page. Google has the real content,
so it's genuine server HTML behind a JS/cookie challenge. Strategy:

  1) Use Playwright once to load the site, let the gate JS run, and harvest the
     resulting cookies.  (Playwright + Chromium are already in the dashboard
     container — the bookganga browser engine uses them.)
  2) Try fast `requests` with those cookies for the bulk of pages. If a page
     comes back gated (cookie expired), transparently fall back to rendering it
     in the browser and refresh the cookies.
  3) Discover book URLs from the sitemap when reachable, else by crawling the
     /category/<slug> listing pages. Category itself is read off each book page
     (detail pages have a clean "Category :" field).

Detail pages expose labeled fields:
  Book / Author / Category / ISBN / Binding / Publishing Date / Publisher /
  Edition / Number of pages / Language   (+ a price ₹ value).

Run order:
  1) probe()        -> verifies the gate is solved + parses one known book.
  2) count_urls()   -> how many /books/ URLs discovered.
  3) run(limit=20)  -> smoke test end to end.
  4) run()          -> full crawl (resumable via DONE_FILE).
"""
import gzip
import json
import os
import re
import time
from urllib.parse import urljoin

import requests
from parsel import Selector

import scriptkit

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except Exception:
    HAVE_PW = False

BASE = "https://dcbookstore.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

DETAIL_DELAY = 0.25
MAP_FILE = os.environ.get("DC_MAP", "/app/scripts/.dcbooks_urls.json")
DONE_FILE = os.environ.get("DC_DONE", "/app/scripts/.dcbooks_done.txt")

# Seed category slugs (from the site's category menu) used only if the sitemap
# is unreachable. Extend freely — duplicates are de-duped by URL.
SEED_CATEGORIES = [
    "malayala-padavali", "novel", "story", "poem", "biography", "autobiography",
    "history", "politics", "religion-spirituality", "philosophy", "science",
    "children", "translations", "literary-criticism", "health", "business",
    "academic", "art", "cinema", "travel",
]


def _is_gate(html):
    return ("Javascript is required" in html
            or "being redirected" in html
            or len(html or "") < 1500)


# ---- gate-aware fetcher --------------------------------------------------
class Fetcher:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.s.headers["Accept-Language"] = "en-US,en;q=0.9"
        self._pw = self._browser = self._ctx = self._page = None
        self.fast = False

    def _harvest(self):
        for c in self._ctx.cookies():
            try:
                self.s.cookies.set(c["name"], c["value"], domain=c.get("domain"))
            except Exception:
                pass

    def _render(self, url):
        self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        for _ in range(8):                       # poll until the gate clears
            html = self._page.content()
            if not _is_gate(html):
                return html
            self._page.wait_for_timeout(1500)
        return self._page.content()

    def start(self):
        if not HAVE_PW:
            print("!! Playwright unavailable — cannot pass the JS gate here.")
            return self
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._ctx = self._browser.new_context(user_agent=UA, locale="en-US")
        self._ctx.route("**/*", lambda r: (
            r.abort() if r.request.resource_type in ("image", "media", "font")
            else r.continue_()))
        self._page = self._ctx.new_page()
        self._render(BASE)                       # solve the gate on the homepage
        self._harvest()
        probe = ""
        try:
            probe = self.s.get(BASE + "/books/newreleases", timeout=30).text
        except Exception:
            pass
        self.fast = not _is_gate(probe)
        print(f"cookies harvested; fast requests path: {'YES' if self.fast else 'no (browser)'}")
        return self

    def get(self, url):
        if self.fast:
            try:
                html = self.s.get(url, timeout=30).text
                if not _is_gate(html):
                    return html
                self.fast = False                # re-gated -> refresh via browser
            except Exception as e:
                print(f"   req err {e}")
        if self._page:
            html = self._render(url)
            self._harvest()                      # refresh cookies; stay on browser
            return html
        return ""

    def get_text(self, url):
        """Plain fetch for robots/sitemap (static files, usually un-gated)."""
        try:
            r = self.s.get(url, timeout=30)
            data = r.content
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data.decode("utf-8", "replace")
        except Exception as e:
            print(f"   txt err {url} {e}")
            return ""

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


# ---- URL discovery -------------------------------------------------------
LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)


def discover_via_sitemap(fz):
    robots = fz.get_text(BASE + "/robots.txt")
    sitemaps = re.findall(r"(?im)^\s*Sitemap:\s*(\S+)", robots)
    if not sitemaps:
        sitemaps = [BASE + "/sitemap.xml"]      # common default
    print(f"sitemaps in robots: {sitemaps or 'none — trying /sitemap.xml'}")
    queue, seen, books = list(sitemaps), set(), set()
    while queue and len(seen) < 500:
        sm = queue.pop()
        if sm in seen:
            continue
        seen.add(sm)
        xml = fz.get_text(sm)
        if not xml or _is_gate(xml):
            continue
        locs = LOC_RE.findall(xml)
        if "<sitemapindex" in xml.lower():
            queue.extend(locs)
        else:
            for u in locs:
                if "/books/" in u:
                    books.add(u.split("?")[0])
            print(f"  {sm.split('/')[-1]}: total books {len(books)}")
    return books


def discover_via_categories(fz, slugs=None):
    slugs = slugs or SEED_CATEGORIES
    books = set()
    for slug in slugs:
        url, page, seen = f"{BASE}/category/{slug}", 1, set()
        while url and url not in seen and page <= 500:
            seen.add(url)
            html = fz.get(url)
            if not html or _is_gate(html):
                break
            sel = Selector(text=html)
            for h in sel.css('a[href*="/books/"]::attr(href)').getall():
                books.add(urljoin(BASE, h).split("?")[0])
            nxt = sel.css('a[rel="next"]::attr(href)').get()
            if not nxt:
                tgt = str(page + 1)
                for a in sel.css("a"):
                    if "".join(a.css("::text").getall()).strip() == tgt:
                        nxt = a.attrib.get("href")
                        break
            url = urljoin(BASE, nxt) if nxt else None
            page += 1
        print(f"  category {slug}: books so far {len(books)}")
    return books


def get_book_urls(fz):
    books = discover_via_sitemap(fz)
    if len(books) < 50:
        print("sitemap thin/blocked — falling back to category crawl")
        books |= discover_via_categories(fz)
    return sorted(u for u in books
                  if "/books/" in u and not u.rstrip("/").endswith("/books"))


# ---- detail parsing ------------------------------------------------------
NEXT = ("Book|Author|Category|ISBN|Binding|Publishing Date|Publisher|Edition|"
        "Number of pages|Language|Price|MRP|Add to|Reviews|Description|About")


def _field(label, text):
    m = re.search(rf"\b{re.escape(label)}\s*:\s*(.+?)\s*(?=(?:{NEXT})\s*:|$)", text)
    return m.group(1).strip() if m else "N/A"


def parse_detail(html, url):
    sel = Selector(text=html)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

    title = (sel.css("h1::text").get() or "").strip() or _field("Book", text)
    price = "N/A"
    m = re.search(r"(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*\.?[0-9]*)", text)
    if m:
        price = m.group(1).replace(",", "")
    image = sel.css('meta[property="og:image"]::attr(content)').get() or ""

    return {
        "title": title,
        "author": _field("Author", text),
        "category": _field("Category", text),
        "isbn": _field("ISBN", text),
        "binding": _field("Binding", text),
        "publishing_date": _field("Publishing Date", text),
        "publisher": _field("Publisher", text),
        "edition": _field("Edition", text),
        "pages": _field("Number of pages", text),
        "language": _field("Language", text),
        "price": price,
        "url": url.split("?")[0],
        "image_url": image,
    }


# ---- checkpoint ----------------------------------------------------------
def _load_done():
    try:
        with open(DONE_FILE, encoding="utf-8") as f:
            return set(x.strip() for x in f if x.strip())
    except FileNotFoundError:
        return set()


def _open_done():
    try:
        os.makedirs(os.path.dirname(DONE_FILE) or ".", exist_ok=True)
        return open(DONE_FILE, "a", encoding="utf-8")
    except Exception:
        return open("/tmp/dcbooks_done.txt", "a", encoding="utf-8")


def _cache_urls(urls):
    try:
        os.makedirs(os.path.dirname(MAP_FILE) or ".", exist_ok=True)
        json.dump(sorted(urls), open(MAP_FILE, "w", encoding="utf-8"))
    except Exception:
        pass


def _load_cached_urls():
    try:
        return json.load(open(MAP_FILE, encoding="utf-8"))
    except Exception:
        return []


# ---- orchestration -------------------------------------------------------
def count_urls():
    fz = Fetcher().start()
    try:
        urls = get_book_urls(fz)
        _cache_urls(urls)
        print(f"\n==> {len(urls)} unique book URLs")
        for u in urls[:5]:
            print("   ", u)
    finally:
        fz.close()


def probe(url=BASE + "/books/malayalam-malayalam-nikhandu"):
    fz = Fetcher().start()
    try:
        html = fz.get(url)
        print("gate passed:", not _is_gate(html), "| html len:", len(html))
        for k, v in parse_detail(html, url).items():
            print(f"{k:>16}: {v}")
    finally:
        fz.close()


def run(limit=None):
    fz = Fetcher().start()
    try:
        urls = _load_cached_urls() or get_book_urls(fz)
        _cache_urls(urls)
        done = _load_done()
        todo = [u for u in urls if u not in done]
        if limit:
            todo = todo[:limit]
        print(f"urls={len(urls)} done={len(done)} to_crawl={len(todo)}")

        fh = _open_done()
        saved, t0 = 0, time.time()
        for i, url in enumerate(todo, 1):
            html = fz.get(url)
            if not html or _is_gate(html):
                continue
            try:
                rec = parse_detail(html, url)
                scriptkit.save("dcbooks", [rec], key_fields=["url"])
                fh.write(url + "\n")
                fh.flush()
                saved += 1
                if i == 1 or i % 25 == 0:
                    rate = i / max(1e-9, time.time() - t0)
                    eta = (len(todo) - i) / max(1e-9, rate) / 60
                    print(f"  [{i}/{len(todo)}] {saved} saved | {rate:.1f}/s | "
                          f"ETA {eta:.0f}m | {rec['category']}: {rec['title'][:26]}")
            except Exception as e:
                print(f"  !! parse {url}: {e}")
            time.sleep(DETAIL_DELAY)
        fh.close()
        print(f"\nDone. Saved/updated {saved} this run ({len(done) + saved} total).")
    finally:
        fz.close()


if __name__ == "__main__":
    run()