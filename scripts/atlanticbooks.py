"""
Atlantic Books (atlanticbooks.com) — full catalogue, detail-enriched.

Shopify. Flow: /collections -> /collections/<h>/products.json (discover) ->
/products/<h> detail page "More Information" (metafields: ISBN13, Binding,
Subject, Publisher, Publisher Imprint, Publication Date, Pages, Original Price,
Language, Edition, Item Weight, BISAC).

Scale note: ~200k products (Atlantic exposes a large slice of its distribution
catalogue; the "Shop by Age" collections are ~25k each). Detail enrichment for
all of them is a MULTI-DAY crawl. Runs at a random 2-5s gap per request with
429/503 backoff, and is fully resumable:
  - phase 1/2 map cached in .atlantic_map.json (per-collection resumable)
  - phase 3 enrichment checkpointed per handle in .atlantic_done.txt
Shopify caps collection products.json at page 100 (25k), so the giant
collections are truncated there; near-total coverage still holds via overlap.

Run (in tmux):
  python scripts/atlanticbooks.py collections   -> list collections
  python scripts/atlanticbooks.py product <handle-or-url>  -> parse one detail
  python scripts/atlanticbooks.py map           -> build/resume the map only
  python scripts/atlanticbooks.py               -> map (if needed) + full crawl
Tune pace with env: ATL_MIN_DELAY / ATL_MAX_DELAY (seconds).
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

BASE = "https://atlanticbooks.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
PER_PAGE = 250
PAGE_CAP = 100                                   # Shopify products.json hard cap
MIN_DELAY = float(os.environ.get("ATL_MIN_DELAY", "2"))
MAX_DELAY = float(os.environ.get("ATL_MAX_DELAY", "5"))
MAP_FILE = os.environ.get("ATL_MAP", "/app/scripts/.atlantic_map.json")
DONE_FILE = os.environ.get("ATL_DONE", "/app/scripts/.atlantic_done.txt")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "application/json, text/html,*/*"})


def nap():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def get(url):
    """GET with patient rate-limit backoff. 429/503 -> honor Retry-After, else
    escalating waits up to 5 min, 10 attempts (~30 min total) so a throttle
    self-heals rather than giving up; 400/404 -> give up quietly."""
    for attempt in range(10):
        try:
            r = SESSION.get(url, timeout=45)
            if r.status_code in (429, 503):
                wait = float(r.headers.get("Retry-After") or 0) or min(300, 10 * (2 ** min(attempt, 5)))
                print(f"   {r.status_code} {url.split('/collections/')[-1][:44]}; "
                      f"wait {wait:.0f}s ({attempt+1}/10)")
                time.sleep(wait)
                continue
            if r.status_code in (400, 404):
                return ""
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"   err {url.split('/')[-1][:36]} (try {attempt+1}): {e}")
            time.sleep(min(120, 5 * (2 ** attempt)))
    print("   !! gave up after 10 attempts — IP is likely rate-limited; stop and "
          "let it cool down (~30-60 min), then rerun (resumes automatically).")
    return ""


def get_json(path):
    try:
        return json.loads(get(BASE + path) or "{}")
    except ValueError:
        return {}


# ---- body_html + tags field parsing --------------------------------------
_KEYMAP = {"author(s)": "author", "publisher imprint": "imprint",
           "publisher": "publisher", "subject": "subject", "bisac": "bisac"}


def body_fields(body):
    text = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", body or ""))).strip()
    out = {}
    for seg in re.split(r"[•·●|]", text):
        m = re.match(r"\s*(Author\(s\)|Publisher Imprint|Publisher|Subject|BISAC)\s*:\s*(.+)",
                     seg.strip(), re.I)
        if not m:
            continue
        key = _KEYMAP.get(m.group(1).lower(), m.group(1).lower())
        val = m.group(2).strip()
        if key in ("subject", "bisac"):
            val = re.split(r"(?<=[a-z])(?=[A-Z])", val, 1)[0].strip()
        out.setdefault(key, val)
    return out


def _slim(p):
    """Small per-product JSON record kept in the map (no raw body_html)."""
    v = (p.get("variants") or [{}])[0]
    imgs = p.get("images") or []
    bf = body_fields(p.get("body_html", ""))
    return {
        "title": p.get("title", ""), "vendor": p.get("vendor", ""),
        "price": v.get("price", ""), "cmp": v.get("compare_at_price", ""),
        "sku": v.get("sku", ""), "grams": v.get("grams", ""), "opt": v.get("option1", ""),
        "img": imgs[0].get("src", "") if imgs else "",
        "b_author": bf.get("author", ""), "b_pub": bf.get("publisher", ""),
        "b_imp": bf.get("imprint", ""), "b_sub": bf.get("subject", ""),
        "b_bisac": bf.get("bisac", ""),
    }


# ---- detail-page "More Information" ---------------------------------------
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


_LMAP = [("ISBN13", "isbn13"), ("ISBN 13", "isbn13"), ("ISBN", "isbn13"),
         ("Publisher Imprint", "imprint"), ("Publisher", "publisher"),
         ("Publication Date", "pub_date"), ("Pages", "pages"),
         ("Original Price", "original_price"), ("Language", "language"),
         ("Edition", "edition"), ("Item Weight", "item_weight"),
         ("BISAC Subject(s)", "bisac"), ("BISAC", "bisac"),
         ("Binding", "binding"), ("Subject", "subject")]
_LRE = re.compile(
    r"^(ISBN13|ISBN 13|Publisher Imprint|Publisher|Publication Date|Pages|Original Price|"
    r"Language|Edition|Item Weight|BISAC Subject\(s\)|BISAC|Binding|Subject|ISBN)\s*:\s*(.+)$", re.I)


def parse_more_info(html):
    lines = html_to_lines(html)
    start = next((i for i, l in enumerate(lines) if re.match(r"More Information\b", l, re.I)), None)
    scope = lines[start:start + 60] if start is not None else lines
    out = {}
    for line in scope:
        m = _LRE.match(line)
        if not m:
            continue
        key = next((k for lbl, k in _LMAP if lbl.lower() == m.group(1).lower()), None)
        if key:
            out.setdefault(key, m.group(2).strip())
    return out


def _clean(v):
    v = (v or "").strip()
    return v if v and v.upper() != "N/A" else ""


def _valid_isbn(s):
    return bool(re.fullmatch(r"97[89]\d{10}|\d{9}[\dXx]", s or ""))


def _isbn_from(handle, sku, img):
    for c in (re.search(r"(97[89]\d{10}|\d{9}[\dXx])$", handle or ""),
              re.fullmatch(r"97[89]\d{10}|\d{9}[\dXx]", re.sub(r"[\s\-]", "", sku or "")),
              re.search(r"/(97[89]\d{10})", img or "")):
        if c:
            return c.group(1) if c.re.groups else c.group(0)
    return ""


def build_record(j, more, collection, handle):
    isbn = _clean(more.get("isbn13"))
    if not _valid_isbn(isbn):
        isbn = _isbn_from(handle, j.get("sku", ""), j.get("img", "")) or (isbn or "N/A")
    subject = (_clean(more.get("subject")) or _clean(j.get("b_sub"))
               or _clean(more.get("bisac")) or _clean(j.get("b_bisac")) or "N/A")
    return {
        "title": j.get("title") or "N/A",
        "author": _clean(j.get("vendor")) or _clean(j.get("b_author")) or "N/A",
        "publisher": _clean(more.get("publisher")) or _clean(j.get("b_pub")) or "N/A",
        "imprint": _clean(more.get("imprint")) or _clean(j.get("b_imp")) or "N/A",
        "subject": subject,
        "bisac": _clean(more.get("bisac")) or _clean(j.get("b_bisac")) or "N/A",
        "isbn13": isbn,
        "binding": _clean(more.get("binding")) or _clean(j.get("opt")) or "N/A",
        "pages": _clean(more.get("pages")) or "N/A",
        "language": _clean(more.get("language")) or "N/A",
        "edition": _clean(more.get("edition")) or "N/A",
        "pub_date": _clean(more.get("pub_date")) or "N/A",
        "price": _clean(j.get("price")) or "N/A",
        "original_price": _clean(more.get("original_price")) or _clean(j.get("cmp")) or "N/A",
        "item_weight": _clean(more.get("item_weight")) or (f"{j['grams']} grams" if j.get("grams") else "N/A"),
        "collection": collection,
        "url": f"{BASE}/products/{handle}",
        "image_url": j.get("img", ""),
    }


# ---- map cache (resumable) + done checkpoint ------------------------------
def _load_map():
    try:
        return json.load(open(MAP_FILE, encoding="utf-8"))
    except Exception:
        return {"done": [], "prod": {}}


def _save_map(state):
    try:
        os.makedirs(os.path.dirname(MAP_FILE) or ".", exist_ok=True)
        tmp = MAP_FILE + ".tmp"
        json.dump(state, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, MAP_FILE)
    except Exception as e:
        print(f"   map save warn: {e}")


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
        return open("/tmp/atlantic_done.txt", "a", encoding="utf-8")


# ---- phases --------------------------------------------------------------
def discover_collections():
    cols, page = [], 1
    while True:
        chunk = get_json(f"/collections.json?limit={PER_PAGE}&page={page}").get("collections", [])
        if not chunk:
            break
        cols += [(c["handle"], c.get("title", c["handle"])) for c in chunk]
        page += 1
        nap()
    return cols


def build_map():
    collections = discover_collections()
    state = _load_map()
    done_cols, prod = set(state.get("done", [])), state.get("prod", {})
    if done_cols and len(done_cols) >= len(collections):
        print(f"map complete: {len(prod)} products across {len(done_cols)} collections")
        return prod
    print(f"building map: {len(done_cols)}/{len(collections)} collections done, "
          f"{len(prod)} products so far")
    for i, (handle, title) in enumerate(collections, 1):
        if handle in done_cols:
            continue
        page, n = 1, 0
        while page <= PAGE_CAP:
            items = get_json(f"/collections/{handle}/products.json?limit={PER_PAGE}&page={page}").get("products", [])
            if not items:
                break
            for p in items:
                h = p.get("handle")
                if h and h not in prod:
                    prod[h] = {"c": title, "j": _slim(p)}
                    n += 1
            page += 1
            nap()
        done_cols.add(handle)
        print(f"  [{i}/{len(collections)}] {title[:42]:<42} +{n} (total {len(prod)})")
        if i % 5 == 0:
            _save_map({"done": sorted(done_cols), "prod": prod})
    _save_map({"done": sorted(done_cols), "prod": prod})
    print(f"map built: {len(prod)} unique products")
    return prod


def run():
    prod = build_map()
    done = _load_done()
    fh = _open_done()
    skip = sum(1 for h in prod if h in done)
    todo = len(prod) - skip
    print(f"enriching {len(prod)} products ({skip} already done, {todo} to go); "
          f"pace {MIN_DELAY}-{MAX_DELAY}s")
    t0, sess, db_fails = time.time(), 0, 0
    for handle, info in prod.items():
        if handle in done:
            continue
        more = parse_more_info(get(f"{BASE}/products/{handle}"))
        rec = build_record(info["j"], more, info["c"], handle)
        # checkpoint ONLY after the DB save succeeds — if Postgres is down we
        # must not mark books done (that's how 50k books got skipped once).
        try:
            scriptkit.save("atlanticbooks", [rec], key_fields=["url"])
        except Exception as e:
            db_fails += 1
            print(f"  !! DB save failed ({db_fails}/5): {e}")
            if db_fails >= 5:
                print("  !! aborting: database unreachable. Fix Postgres, then rerun — "
                      "resumes from checkpoint; nothing was falsely marked done.")
                break
            time.sleep(10)
            continue
        db_fails = 0
        fh.write(handle + "\n")
        fh.flush()
        done.add(handle)
        sess += 1
        if sess % 25 == 0:
            rate = sess / max(1e-9, time.time() - t0)          # this session only
            eta_d = (todo - sess) / max(1e-9, rate) / 86400
            print(f"  {skip+sess}/{len(prod)} | {rate*3600:.0f}/h | ETA {eta_d:.1f}d | "
                  f"{rec['title'][:26]} | {rec['pages']}p {rec['language']} | {rec['isbn13']}")
        nap()
    fh.close()
    print(f"\nDone. Saved/updated {sess} books this session ({skip+sess} total).")


# ---- probes --------------------------------------------------------------
def cmd_collections():
    cols = discover_collections()
    print(f"discovered {len(cols)} collections; first 10:")
    for h, t in cols[:10]:
        print(f"  {t[:50]:<50} ({h})")


def cmd_product(handle):
    handle = handle.rstrip("/").split("/products/")[-1].split("?")[0]
    more = parse_more_info(get(f"{BASE}/products/{handle}"))
    print(f"More Information for /products/{handle}:")
    for k, v in more.items():
        print(f"  {k:>14}: {v}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "collections":
        cmd_collections()
    elif cmd == "product":
        cmd_product(sys.argv[2] if len(sys.argv) > 2 else "the-republic")
    elif cmd == "map":
        build_map()
    else:
        run()