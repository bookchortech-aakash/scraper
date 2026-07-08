"""
AITBS Publishers India (aitbspublishersindia.com) crawler.

Platform: OpenCart, server-rendered (no JS gate). Products at
  index.php?route=product/product&product_id=<N>   (sequential IDs).
Small catalogue -> discover by iterating product_id.

Access note: the site returns 409 to unfamiliar UAs on dynamic pages (Googlebot
indexes them fine). We prime a session on the homepage (OpenCart cookie), send a
browser UA + Referer, and re-prime/retry on a 409. If 409s persist from your IP,
the browser fallback (like DC Books) is the next step.

Fields per book come from a labeled block on the page:
  Author : / ISBN: / Edition: / Year: / Pages: / Size: / Publisher : / Price: र<amt>
The <h1> title often carries the edition and binding as a suffix
  e.g. "... (English-English-Hindi), 7/Ed. (H.B.)"
so we split those into their own `edition` / `binding` columns and keep the
title clean. Category is NOT in the product breadcrumb when reached by id, so we
build a product_id -> category map once by crawling the category pages; anything
unmapped is "N/A" (never the title).

Run order:
  1) probe(865)             -> HTTP status + parsed fields for one book.
  2) build_category_map()   -> verify category discovery works (prints counts).
  3) run(limit=20)          -> 20 books end to end.
  4) run()                  -> full crawl (resumable). To re-correct existing
                               rows, delete DONE_FILE first so all are re-saved.
"""
import json
import os
import re
import time

import requests
from parsel import Selector

import scriptkit

BASE = "https://www.aitbspublishersindia.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

MAX_ID = int(os.environ.get("AITBS_MAX_ID", "2000"))
STOP_AFTER_MISSES = 200
DELAY = 0.25
DONE_FILE = os.environ.get("AITBS_DONE", "/app/scripts/.aitbs_done.txt")
CATMAP_FILE = os.environ.get("AITBS_CATMAP", "/app/scripts/.aitbs_catmap.json")


def product_url(pid):
    return f"{BASE}/index.php?route=product/product&product_id={pid}"


# ---- session (handles 409 on dynamic pages) ------------------------------
def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": BASE + "/index.php?route=common/home",
    })
    try:
        s.get(BASE + "/", timeout=30)
    except Exception as e:
        print(f"   prime warning: {e}")
    return s


def fetch(s, url):
    for attempt in range(4):
        try:
            r = s.get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                return ""
            print(f"   {r.status_code} (retry {attempt+1})")
            time.sleep(3)
            s.get(BASE + "/", timeout=30)
        except Exception as e:
            print(f"   err {e} (retry {attempt+1})")
            time.sleep(3)
    return ""


# ---- title / edition / binding split -------------------------------------
BIND_RE = re.compile(r"\(\s*(H\.?\s*B\.?|P\.?\s*B\.?|Hard\s*back|Paper\s*back|"
                     r"Hard\s*bound|Paper\s*bound)\s*\)", re.I)
ED_RE = re.compile(r",?\s*(\d+\s*/\s*(?:Revised\s+)?Ed\.?)", re.I)


def split_title(raw):
    title, binding, edition = raw, "N/A", "N/A"
    mb = BIND_RE.search(title)
    if mb:
        b = re.sub(r"[\s.]", "", mb.group(1)).upper()
        binding = {"HB": "H.B.", "PB": "P.B."}.get(b, mb.group(1).strip())
        title = title[:mb.start()] + title[mb.end():]
    me = ED_RE.search(title)
    if me:
        edition = re.sub(r"\s+", " ", me.group(1)).strip()
        title = title[:me.start()] + title[me.end():]
    title = re.sub(r"\s{2,}", " ", title).strip().rstrip(",").strip()
    return title, edition, binding


# ---- labeled field parsing -----------------------------------------------
LABELS = "Author|ISBN|Edition|Year|Pages|Size|Publisher|Price|Availability|Product Code|Brand"
SECTIONS = r"Description|Reviews|Write a review|Qty|Add to|Related|Tags"


def _field(label, text):
    m = re.search(rf"\b{label}\s*:\s*(.*?)\s*(?=(?:{LABELS})\s*:|(?:{SECTIONS})\b|$)", text)
    v = m.group(1).strip() if m else ""
    return v if v else "N/A"


def _breadcrumb_category(sel, title):
    crumbs = [c.strip() for c in
              sel.css("ul.breadcrumb li a::text, .breadcrumb a::text").getall() if c.strip()]
    crumbs = [c for c in crumbs
              if c.lower() not in ("home", "home page") and c.strip() != title.strip()]
    return crumbs[-1] if crumbs else "N/A"


def parse_product(html, url, cat_map=None):
    sel = Selector(text=html)
    content = sel.css("#content").get() or html
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", content))

    raw_title = (sel.css("#content h1::text").get() or sel.css("h1::text").get() or "").strip()
    title, t_edition, t_binding = split_title(raw_title)

    edition = _field("Edition", text)
    if edition == "N/A":
        edition = t_edition                       # fall back to title-derived

    price = "N/A"
    m = re.search(r"(?:र|₹|Rs\.?|INR)\s*([0-9][0-9,]*\.?[0-9]*)", text)
    if m:
        price = m.group(1).replace(",", "")
    image = (sel.css('meta[property="og:image"]::attr(content)').get()
             or sel.css('.thumbnails img::attr(src), #content .image img::attr(src), '
                        'a.thumbnail img::attr(src)').get() or "")

    pid = None
    mm = re.search(r"product_id=(\d+)", url)
    if mm:
        pid = int(mm.group(1))
    category = "N/A"
    if cat_map and pid in cat_map:
        category = cat_map[pid]
    elif not cat_map:
        category = _breadcrumb_category(sel, raw_title)   # compare vs RAW h1

    return {
        "title": title or "N/A",
        "author": _field("Author", text),
        "isbn": _field("ISBN", text),
        "edition": edition,
        "binding": t_binding,                     # from the (H.B.)/(P.B.) marker
        "year": _field("Year", text),
        "pages": _field("Pages", text),
        "publisher": _field("Publisher", text),
        "price": price,
        "category": category,
        "url": url,
        "image_url": image,
    }


def _valid(rec):
    return (rec["title"] not in ("", "N/A")
            and (rec["isbn"] != "N/A" or rec["author"] != "N/A" or rec["price"] != "N/A"))


# ---- category map (product_id -> subject) --------------------------------
def _cat_links(html):
    sel = Selector(text=html)
    out = {}
    for a in sel.css("a"):
        href = a.attrib.get("href", "")
        if "route=product/category" in href or "route=product%2Fcategory" in href:
            m = re.search(r"path=(\d+(?:_\d+)*)", href)
            if m:
                name = " ".join(t.strip() for t in a.css("::text").getall()).strip()
                out.setdefault(m.group(1), name)
    return out


def build_category_map(s=None, save=True):
    s = s or make_session()
    links = _cat_links(fetch(s, BASE + "/"))
    if len(links) < 3:                            # homepage thin -> seed from a category page
        links.update(_cat_links(fetch(s, f"{BASE}/index.php?route=product/category&path=8")))
    print(f"discovered {len(links)} category paths")
    cmap = {}
    for path, name in links.items():
        page, seen = 1, set()
        cat_name = name
        while page <= 100:
            url = f"{BASE}/index.php?route=product/category&path={path}&limit=100&page={page}"
            html = fetch(s, url)
            if not html:
                break
            sel = Selector(text=html)
            if not cat_name:
                cat_name = (sel.css("#content h1::text").get() or "").strip()
            pids = set()
            for h in sel.css('a[href*="product_id="]::attr(href)').getall():
                m = re.search(r"product_id=(\d+)", h)
                if m:
                    pids.add(int(m.group(1)))
            if not pids or pids == seen:
                break
            seen = pids
            for pid in pids:
                cmap.setdefault(pid, cat_name or "N/A")
            page += 1
            time.sleep(DELAY)
        print(f"  path={path} '{cat_name}': total mapped {len(cmap)}")
    if save and cmap:
        try:
            os.makedirs(os.path.dirname(CATMAP_FILE) or ".", exist_ok=True)
            json.dump({str(k): v for k, v in cmap.items()},
                      open(CATMAP_FILE, "w", encoding="utf-8"))
        except Exception:
            pass
    return cmap


def _load_catmap():
    try:
        return {int(k): v for k, v in json.load(open(CATMAP_FILE, encoding="utf-8")).items()}
    except Exception:
        return {}


# ---- checkpoint ----------------------------------------------------------
def _load_done():
    try:
        with open(DONE_FILE, encoding="utf-8") as f:
            return set(int(x) for x in f.read().split() if x.strip().isdigit())
    except FileNotFoundError:
        return set()


def _open_done():
    try:
        os.makedirs(os.path.dirname(DONE_FILE) or ".", exist_ok=True)
        return open(DONE_FILE, "a", encoding="utf-8")
    except Exception:
        return open("/tmp/aitbs_done.txt", "a", encoding="utf-8")


# ---- orchestration -------------------------------------------------------
def probe(pid=865):
    s = make_session()
    r = s.get(product_url(pid), timeout=30)
    print(f"HTTP {r.status_code} | html len {len(r.text)}")
    if r.status_code == 200:
        for k, v in parse_product(r.text, product_url(pid)).items():
            print(f"{k:>10}: {v}")


def run(limit=None):
    s = make_session()
    cat_map = _load_catmap() or build_category_map(s)
    print(f"category map: {len(cat_map)} products tagged")
    done = _load_done()
    fh = _open_done()
    saved, misses, t0 = 0, 0, time.time()
    for pid in range(1, MAX_ID + 1):
        if pid in done:
            continue
        html = fetch(s, product_url(pid))
        if not html:
            misses += 1
        else:
            rec = parse_product(html, product_url(pid), cat_map)
            if _valid(rec):
                scriptkit.save("aitbs", [rec], key_fields=["url"])
                fh.write(f"{pid}\n")
                fh.flush()
                saved += 1
                misses = 0
                if saved == 1 or saved % 20 == 0:
                    rate = saved / max(1e-9, time.time() - t0)
                    print(f"  id={pid} | {saved} saved | {rate:.1f}/s | "
                          f"{rec['category']}: {rec['title'][:30]} [{rec['edition']}/{rec['binding']}]")
            else:
                misses += 1
        if misses >= STOP_AFTER_MISSES and pid > 956:
            print(f"  stopping at id={pid} after {misses} consecutive misses")
            break
        if limit and saved >= limit:
            break
        time.sleep(DELAY)
    fh.close()
    print(f"\nDone. Saved/updated {saved} books (scanned up to id {pid}).")


if __name__ == "__main__":
    run()