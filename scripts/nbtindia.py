"""
National Book Trust India (nbtindia.gov.in) catalogue crawler.

ASP.NET site; the catalogue browses by SERIES via clean GET URLs
  catalogues__booksseries__<id>__<slug>.nbt
Each series page is server-rendered and lists EVERY book with full data, so no
per-book detail fetch is needed (though each book DOES have a detail URL + cover
image, which we capture).

Real per-book markup (one <p>, fields separated by <br> and by source-indent
newlines):
    <a href="/books_detail__22__autobiography__123__...nbt" title="TITLE">TITLE</a>
    <img src="/writereaddata/booksimages/123.jpg">
    <p> AUTHOR,<BR> Format, Year, Lang, Nth Edition, N Pages <br>
        ISBN10, ISBN13 <br> View Description ... </p>
    175.00  In Stock
So we (1) collapse source whitespace and break lines ONLY at <br>/block tags,
(2) anchor each book on its "Format, Year, Lang, Edition, Pages" line, taking the
author from the line above and title/url/image from the book's detail link.

Run (takes a CLI arg):
  python scripts/nbtindia.py probe 22   -> parse Autobiography, print first books
  python scripts/nbtindia.py dump 22    -> raw HTML of one book (debug)
  python scripts/nbtindia.py            -> full crawl (resumable)
"""
import html as _html
import json
import os
import re
import sys
import time

import requests
from parsel import Selector

# make scriptkit importable no matter where this is launched from
_here = os.path.dirname(os.path.abspath(__file__))
for _d in (_here, os.path.dirname(_here), os.path.dirname(os.path.dirname(_here)), "/app"):
    if os.path.exists(os.path.join(_d, "scriptkit.py")):
        sys.path.insert(0, _d)
        break
import scriptkit

BASE = "https://www.nbtindia.gov.in"
INDEX = BASE + "/catalogues__online-index.aspx"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

DELAY = 0.5
DONE_FILE = os.environ.get("NBT_DONE", "/app/scripts/.nbt_done.json")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


def get(url):
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=60)
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"   retry {attempt+1}/3 {url} -> {e}")
            time.sleep(4)
    return ""


# ---- discovery -----------------------------------------------------------
def discover_series():
    sel = Selector(text=get(INDEX))
    out = {}
    for a in sel.css('a[href*="booksseries"]'):
        href = a.attrib.get("href", "")
        mm = re.search(r"(catalogues__booksseries__\d+__[^\"'#?]+?\.nbt)", href)
        if mm:
            url = href if href.startswith("http") else BASE + "/" + mm.group(1)
            name = " ".join(t.strip() for t in a.css("::text").getall()).strip()
            out[url] = name or mm.group(1)
    return out


# ---- parsing -------------------------------------------------------------
# The format line no longer carries the author (author sits on the line above,
# separated by <BR>): "Paperback, 2023, English, 1st Edition, 176 Pages".
FORMAT_RE = re.compile(
    r"^(?P<format>Paperback|Hardcover|Hardback|Hard\s*Bound|Paper\s*Back)\s*,\s*"
    r"(?P<year>\d{4})\s*,\s*(?P<lang>[A-Za-z]+)\s*,\s*"
    r"(?P<edition>\d+\s*(?:st|nd|rd|th)\s*Edition)\s*,\s*(?P<pages>\d+)\s*Pages", re.I)
ISBN_RE = re.compile(r"(\d[\dX\-]{5,}[\dX])\s*,\s*(97[89][\d\-]{6,}\d)", re.I)
FIELDISH = re.compile(r"View Description|In Stock|Out of Stock|^\d+\.\d{2}$|^\d[\dX\-]{5,}[\dX],", re.I)
BLOCK = (r"(p|div|li|ul|ol|tr|td|table|h[1-6]|section|article|header|footer|"
         r"blockquote|dl|dt|dd|nav|main|aside)")


def html_to_lines(h):
    """Break lines ONLY at <br>/block tags; collapse the raw source whitespace
    (indentation newlines) so a field split across source lines stays together."""
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    sep = "\x00"
    h = re.sub(r"(?i)<\s*br\s*/?>", sep, h)
    h = re.sub(rf"(?i)</\s*{BLOCK}\s*>", sep, h)
    h = re.sub(rf"(?i)<\s*{BLOCK}(\s[^>]*)?>", sep, h)
    h = _html.unescape(re.sub(r"<[^>]+>", " ", h))
    return [re.sub(r"\s+", " ", p).strip() for p in h.split(sep) if p.strip()]


def _price(block):
    m = (re.search(r"(\d+\.\d{2})\s*(?:In Stock|Out of Stock|$)", block, re.I)
         or re.search(r"(\d+\.\d{2})", block))
    return m.group(1) if m else "N/A"


def _detail_links(sel):
    """Book detail links in document order, deduped by book id (the second-to-
    last '__' field of /books_detail__<series>__<slug>__<bookid>__<bookslug>.nbt)."""
    out, seen = [], set()
    for a in sel.css('a[href*="books_detail__"]'):
        href = a.attrib.get("href", "")
        parts = href.split("__")
        if len(parts) < 2:
            continue
        bid = parts[-2] if parts[-2].isdigit() else (re.search(r"\d+", parts[-2]) or [None])[0]
        if not bid or bid in seen:
            continue
        seen.add(bid)
        u = href if href.startswith("http") else BASE + "/" + href.lstrip("/")
        out.append((bid, (a.attrib.get("title") or "").strip(), u))
    return out


def parse_series(raw, series, url_page):
    sel = Selector(text=raw)
    detail = _detail_links(sel)
    lines = html_to_lines(raw)
    fmts = [(i, FORMAT_RE.match(ln)) for i, ln in enumerate(lines) if FORMAT_RE.match(ln)]
    out = []
    for n, (i, fm) in enumerate(fmts):
        author = lines[i - 1].rstrip(" ,").strip() if i >= 1 else "N/A"
        j = i - 2
        while j >= 0 and (not lines[j] or FIELDISH.search(lines[j])):
            j -= 1
        title_txt = lines[j].rstrip(" ,").strip() if j >= 0 else "N/A"
        end = fmts[n + 1][0] if n + 1 < len(fmts) else len(lines)
        block = " \n ".join(lines[i + 1:end])
        mi = ISBN_RE.search(block)
        ms = re.search(r"(In Stock|Out of Stock)", block, re.I)
        durl, image, dtitle = url_page, "", ""
        if n < len(detail):
            bid, dtitle, durl = detail[n]
            image = f"{BASE}/writereaddata/booksimages/{bid}.jpg"
        out.append({
            "title": dtitle or title_txt,
            "author": author,
            "format": fm.group("format").strip(),
            "year": fm.group("year"),
            "language": fm.group("lang").strip(),
            "edition": re.sub(r"\s+", " ", fm.group("edition")).strip(),
            "pages": fm.group("pages"),
            "isbn10": mi.group(1) if mi else "N/A",
            "isbn13": mi.group(2) if mi else "N/A",
            "price": _price(block),
            "stock": ms.group(1) if ms else "N/A",
            "series": series,
            "url": durl,
            "image_url": image,
        })
    return out


# ---- checkpoint ----------------------------------------------------------
def _load_done():
    try:
        return set(json.load(open(DONE_FILE, encoding="utf-8")))
    except Exception:
        return set()


def _save_done(done):
    try:
        os.makedirs(os.path.dirname(DONE_FILE) or ".", exist_ok=True)
        json.dump(sorted(done), open(DONE_FILE, "w", encoding="utf-8"))
    except Exception:
        pass


# ---- diagnostics + orchestration -----------------------------------------
def _series_url(series_id):
    return next((u for u in discover_series() if f"booksseries__{series_id}__" in u),
                BASE + "/catalogues__booksseries__22__autobiography.nbt")


def dump_html(series_id=22):
    url = _series_url(series_id)
    raw = get(url)
    print(f"URL: {url}\nfetched chars: {len(raw)}")
    m = (re.search(r"97[89][\d\-]{6,}\d", raw)
         or re.search(r"In Stock|Out of Stock", raw, re.I)
         or re.search(r"\d+\s*Pages", raw, re.I))
    if not m:
        print("\n--- no book anchor found; first 1500 chars ---\n" + raw[:1500])
        return
    s, e = max(0, m.start() - 1500), m.end() + 350
    print("\n=== RAW HTML around the first book record ===\n" + raw[s:e])
    print("\n=== same region after html_to_lines ===")
    for l in html_to_lines(raw[s:e]):
        print("  |", l)
    print(f"\nformat-line matches on full page: "
          f"{sum(1 for l in html_to_lines(raw) if FORMAT_RE.match(l))}")


def probe(series_id=22):
    url = _series_url(series_id)
    recs = parse_series(get(url), "probe", url)
    print(f"series url: {url}\nparsed {len(recs)} books; first 6:")
    for r in recs[:6]:
        print(f"  • {r['title'][:32]:<32} | {r['author'][:16]:<16} | {r['language']:<7} "
              f"| {r['edition']:<11} | {r['pages']}p | {r['isbn13']} | {r['price']} | {r['stock']}")


def run():
    series = discover_series()
    print(f"discovered {len(series)} series")
    done = _load_done()
    total = 0
    for url, name in series.items():
        if url in done:
            print(f"  [cached] {name}")
            continue
        recs = parse_series(get(url), name, url)
        for r in recs:
            scriptkit.save("nbtindia", [r], key_fields=["isbn13"])
        total += len(recs)
        done.add(url)
        _save_done(done)
        print(f"  {name}: {len(recs)} books (running total {total})")
        time.sleep(DELAY)
    print(f"\nDone. Saved/updated {total} book rows across {len(series)} series.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = int(sys.argv[2]) if len(sys.argv) > 2 else 22
    if cmd == "dump":
        dump_html(arg)
    elif cmd == "probe":
        probe(arg)
    else:
        run()