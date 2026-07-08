"""
Hindi Book Centre (hindibook.com) crawler.

Custom PHP catalogue; everything routes through index.php?p=sr.
  - Browse subjects page lists ~34 subjects, each ->
      index.php?p=sr&format=listpage&subject=<S>&keywords=<S>&Display=<S>&showsubsubject=0
  - A subject listing reports "... N record(s)" and paginates via &startrow=N (24/page).
    Each card's detail link carries the bookcode.
  - Detail page: index.php?p=sr&format=fullpage&Field=bookcode&String=<code>
    -> clean labeled record: Title, Author, ISBN 13, ISBN 10, Year, Language,
       Pages etc., Binding, Subject(s), Sale Price/Discount/selling price;
       publisher + city in the meta-keywords.

Two phases (resumable):
  1) enumerate bookcodes across all subjects  -> .hindibook_codes.json (+ counts)
  2) fetch each book's detail page -> full record -> save (checkpoint per code)

Run:
  python scripts/hindibook.py subjects           -> list subjects + record counts
  python scripts/hindibook.py book <bookcode>    -> parse one detail page
  python scripts/hindibook.py enumerate          -> phase 1 only (reports total)
  python scripts/hindibook.py                    -> enumerate (if needed) + detail crawl
Pace: HB_MIN_DELAY / HB_MAX_DELAY (seconds).
"""
import html as _html
import json
import os
import random
import re
import sys
import time
from urllib.parse import quote, parse_qs, urlparse

import requests

_here = os.path.dirname(os.path.abspath(__file__))
for _d in (_here, os.path.dirname(_here), os.path.dirname(os.path.dirname(_here)), "/app"):
    if os.path.exists(os.path.join(_d, "scriptkit.py")):
        sys.path.insert(0, _d)
        break
import scriptkit

BASE = "https://www.hindibook.com"
SUBJECT_PAGE = BASE + "/index.php?p=pages/subject"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
PAGE = 24                                        # listing page size (fixed by site)
MIN_DELAY = float(os.environ.get("HB_MIN_DELAY", "1.0"))
MAX_DELAY = float(os.environ.get("HB_MAX_DELAY", "2.0"))
CODES_FILE = os.environ.get("HB_CODES", "/app/scripts/.hindibook_codes.json")
DONE_FILE = os.environ.get("HB_DONE", "/app/scripts/.hindibook_done.txt")

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
            if r.status_code == 404:
                return ""
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"   err {url.split('String=')[-1][:24]} (try {attempt+1}): {e}")
            time.sleep(min(60, 5 * (2 ** attempt)))
    return ""


# ---- subjects ------------------------------------------------------------
HREF_RE = re.compile(r'''href\s*=\s*["']([^"']+)["']''', re.I)


def discover_subjects(html=None):
    html = html if html is not None else get(SUBJECT_PAGE)
    out, seen = [], set()
    for raw in HREF_RE.findall(html):
        url = _html.unescape(raw).strip()
        q = parse_qs(urlparse(url).query)
        if (q.get("p", [""])[0] or "").strip("/") != "sr":
            continue
        name = (q.get("subject") or q.get("Display") or q.get("keywords") or [""])[0].strip()
        if not name or name.lower() in seen:
            continue
        # normalize to absolute
        if not url.startswith("http"):
            url = BASE + "/" + url.lstrip("/")
        seen.add(name.lower())
        out.append((name, url))
    return out


def cmd_dump():
    """Diagnostic: show what the subjects page actually returns to requests."""
    html = get(SUBJECT_PAGE)
    print(f"fetched chars: {len(html)}")
    low = html.lower()
    print(f"contains 'browse subjects': {'browse subjects' in low} | "
          f"'listpage': {'listpage' in low} | 'p=sr': {'p=sr' in low} | "
          f"hrefs found: {len(HREF_RE.findall(html))}")
    i = low.find("listpage")
    if i >= 0:
        print("\n--- raw HTML around first 'listpage' (paste back to me if 0 subjects) ---")
        print(html[max(0, i - 500):i + 300])
    else:
        print("\n--- first 1200 chars of the page ---")
        print(html[:1200])
    subs = discover_subjects(html)
    print(f"\ndiscover_subjects -> {len(subs)}")
    for n, u in subs[:8]:
        print(f"  {n[:30]:<30} {u[:80]}")


# ---- listing enumeration -------------------------------------------------
def listing_total(html):
    m = re.search(r"results?\s*([\d,]+)\s*record", html, re.I)
    return int(m.group(1).replace(",", "")) if m else 0


def listing_codes(html):
    codes = re.findall(r'format=fullpage&(?:amp;)?Field=bookcode&(?:amp;)?String=([^"&\']+)', html)
    seen, out = set(), []
    for c in codes:
        c = _html.unescape(c).strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def enumerate_subject(name, url):
    """Yield all bookcodes for one subject, paging via &startrow."""
    first = get(url)
    total = listing_total(first)
    codes = listing_codes(first)
    for c in codes:
        yield c
    got = len(codes)
    start = PAGE
    while start < total:
        page_url = f"{url}&startrow={start}"
        nap()
        html = get(page_url)
        cs = listing_codes(html)
        if not cs:
            break
        for c in cs:
            yield c
        got += len(cs)
        start += PAGE
    print(f"    {name}: {total} records, {got} codes read")


# ---- detail parsing ------------------------------------------------------
BLOCK = (r"(p|div|li|ul|ol|tr|td|table|h[1-6]|section|article|header|footer|"
         r"blockquote|dl|dt|dd|nav|main|aside)")


def html_to_lines(h):
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    sep = "\x00"
    h = re.sub(r"(?i)<\s*br\s*/?>", sep, h)
    h = re.sub(rf"(?i)</\s*{BLOCK}\s*>", sep, h)
    h = re.sub(rf"(?i)<\s*{BLOCK}(\s[^>]*)?>", sep, h)
    h = _html.unescape(re.sub(r"<[^>]+>", " ", h))
    return [re.sub(r"\s+", " ", p).strip() for p in h.split(sep) if p.strip()]


_DLABEL = re.compile(
    r"^(Title|Author|ISBN 13|ISBN 10|Year|Language|Pages etc\.?|Binding|Subject\(s\))\s*:\s*(.*)$", re.I)
_DKEY = {"title": "title", "author": "author", "isbn 13": "isbn13", "isbn 10": "isbn10",
         "year": "year", "language": "language", "pages etc.": "pages", "pages etc": "pages",
         "binding": "binding", "subject(s)": "subjects"}


def _prices(text):
    save = re.search(r"₹\s*([\d,]+(?:\.\d+)?)\s*You Save", text)
    sale = re.search(r"Sale Price\s*:?\s*₹\s*([\d,]+(?:\.\d+)?)", text)
    disc = re.search(r"Discount\s*:?\s*(\d+)\s*%", text)
    amts = re.findall(r"₹\s*([\d,]+(?:\.\d+)?)", text)
    selling = (save.group(1) if save else (amts[0] if amts else "N/A")).replace(",", "")
    mrp = (sale.group(1) if sale else selling).replace(",", "")
    return selling, mrp, (disc.group(1) + "%" if disc else "N/A")


def _meta_pub_city(html):
    m = re.search(r'<meta\s+name=["\']keywords["\']\s+content=["\']([^"\']*)["\']', html, re.I)
    if not m:
        return "N/A", "N/A"
    parts = [p.strip() for p in _html.unescape(m.group(1)).split(",")]
    for i, p in enumerate(parts):
        if re.fullmatch(r"(19|20)\d\d", p):
            return (parts[i + 1] if i + 1 < len(parts) else "N/A") or "N/A", \
                   (parts[i + 2] if i + 2 < len(parts) else "N/A") or "N/A"
    return "N/A", "N/A"


def parse_detail(html, bookcode):
    lines = html_to_lines(html)
    f = {}
    for i, ln in enumerate(lines):
        m = _DLABEL.match(ln)
        if not m:
            continue
        key = _DKEY.get(m.group(1).lower().rstrip("."))
        if not key:
            continue
        val = m.group(2).strip()
        if not val and i + 1 < len(lines):
            val = lines[i + 1].strip()
        f.setdefault(key, val)
    text = " ".join(lines)
    selling, mrp, disc = _prices(text)
    pub, city = _meta_pub_city(html)
    img = re.search(r'(https?://[^"\']*/books/pics/[^"\']+\.jpg)', html)
    return {
        "title": f.get("title", "N/A"),
        "author": f.get("author", "N/A"),
        "isbn13": f.get("isbn13", "N/A"),
        "isbn10": f.get("isbn10", "N/A"),
        "year": f.get("year", "N/A"),
        "language": f.get("language", "N/A"),
        "pages": f.get("pages", "N/A"),
        "binding": f.get("binding", "N/A"),
        "subjects": f.get("subjects", "N/A"),
        "publisher": pub,
        "city": city,
        "price": selling,
        "mrp": mrp,
        "discount": disc,
        "bookcode": bookcode,
        "url": f"{BASE}/index.php?p=sr&format=fullpage&Field=bookcode&String={quote(bookcode)}",
        "image_url": img.group(1) if img else f"{BASE}/books/pics/{bookcode}.jpg",
    }


# ---- checkpoint / codes cache --------------------------------------------
def _save_codes(state):
    try:
        os.makedirs(os.path.dirname(CODES_FILE) or ".", exist_ok=True)
        tmp = CODES_FILE + ".tmp"
        json.dump(state, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, CODES_FILE)
    except Exception as e:
        print(f"   codes save warn: {e}")


def _load_codes():
    try:
        return json.load(open(CODES_FILE, encoding="utf-8"))
    except Exception:
        return {"subjects_done": [], "codes": []}


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
        return open("/tmp/hindibook_done.txt", "a", encoding="utf-8")


# ---- phases --------------------------------------------------------------
def enumerate_all():
    subjects = discover_subjects()
    state = _load_codes()
    done_subj = set(state.get("subjects_done", []))
    codes = set(state.get("codes", []))
    if done_subj and len(done_subj) >= len(subjects):
        print(f"enumeration complete: {len(codes)} unique bookcodes")
        return sorted(codes)
    print(f"enumerating {len(subjects)} subjects ({len(done_subj)} done, {len(codes)} codes so far)")
    for name, url in subjects:
        if name in done_subj:
            continue
        before = len(codes)
        for c in enumerate_subject(name, url):
            codes.add(c)
        done_subj.add(name)
        print(f"  [{len(done_subj)}/{len(subjects)}] {name[:30]:<30} +{len(codes)-before} (total {len(codes)})")
        _save_codes({"subjects_done": sorted(done_subj), "codes": sorted(codes)})
        nap()
    print(f"\nenumeration done: {len(codes)} unique bookcodes")
    return sorted(codes)


def run():
    codes = enumerate_all()
    done = _load_done()
    fh = _open_done()
    total = sum(1 for c in codes if c in done)
    print(f"enriching {len(codes)} books ({total} done); pace {MIN_DELAY}-{MAX_DELAY}s")
    t0 = time.time()
    for code in codes:
        if code in done:
            continue
        rec = parse_detail(get(f"{BASE}/index.php?p=sr&format=fullpage&Field=bookcode&String={quote(code)}"), code)
        scriptkit.save("hindibook", [rec], key_fields=["url"])
        fh.write(code + "\n")
        fh.flush()
        done.add(code)
        total += 1
        if total % 25 == 0:
            rate = total / max(1e-9, time.time() - t0)
            eta = (len(codes) - total) / max(1e-9, rate) / 3600
            print(f"  {total}/{len(codes)} | {rate*3600:.0f}/h | ETA {eta:.1f}h | "
                  f"{rec['title'][:26]} | {rec['year']} {rec['language']} {rec['pages']}")
        nap()
    fh.close()
    print(f"\nDone. Saved/updated {total} books.")


# ---- probes --------------------------------------------------------------
def cmd_subjects():
    subjects = discover_subjects()
    print(f"discovered {len(subjects)} subjects:")
    for name, url in subjects:
        nap()
        total = listing_total(get(url))
        print(f"  {name[:34]:<34} {total:>6} records")


def cmd_book(code):
    rec = parse_detail(get(f"{BASE}/index.php?p=sr&format=fullpage&Field=bookcode&String={quote(code)}"), code)
    for k, v in rec.items():
        print(f"  {k:>10}: {v}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "subjects":
        cmd_subjects()
    elif cmd == "dump":
        cmd_dump()
    elif cmd == "book":
        cmd_book(sys.argv[2] if len(sys.argv) > 2 else "9789383894918")
    elif cmd == "enumerate":
        enumerate_all()
    else:
        run()