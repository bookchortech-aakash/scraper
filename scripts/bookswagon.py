"""
BooksWagon (bookswagon.com) crawler — listing-driven (no detail fetches).

Custom ASP.NET store, ~11.5M titles (full distribution catalogue; ~2.8M in
stock). Subject listing pages (/<slug>-books) server-render rich cards with
EVERYTHING we need: title, author(s), publisher, MRP, sale price, discount,
binding, release date, language, availability, shipping time, image, and the
ISBN-13 in the detail URL (/book/<slug>/<isbn13>). One page = 20 full records,
so we never open detail pages.

Pagination: page 1 is server-rendered; further pages load via AJAX. The exact
mechanism is unknown from research, so `probe` AUTO-DETECTS it by trying the
common ASP.NET patterns and comparing first-ISBNs; `dump` prints the page's own
loader JS so the endpooint can be locked manually if none match. run() uses the
detected pattern (cached in the state file).

Resumable: per-(subject, page) checkpoint; saves happen per page (batch of 20)
and the checkpoint advances ONLY after a confirmed DB save (Atlantic lesson).

Run order:
  python scripts/bookswagon.py dump                 -> loader JS + card sanity
  python scripts/bookswagon.py probe                -> auto-detect pagination
  python scripts/bookswagon.py subjects             -> count subject slugs
  python scripts/bookswagon.py                      -> full crawl (tmux!)
Pace: BW_MIN_DELAY / BW_MAX_DELAY (default 1-2s).
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

BASE = "https://www.bookswagon.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MIN_DELAY = float(os.environ.get("BW_MIN_DELAY", "1.0"))
MAX_DELAY = float(os.environ.get("BW_MAX_DELAY", "2.0"))
STATE_FILE = os.environ.get("BW_STATE", "/app/scripts/.bookswagon_state.json")
DEFAULT_SUBJECT = "history-books"

# candidate paginators: name -> lambda(url, page) -> page url
PAGINATORS = {
    "page":    lambda u, p: f"{u}?page={p}",
    "pageno":  lambda u, p: f"{u}?pageno={p}",
    "PageNo":  lambda u, p: f"{u}?PageNo={p}",
    "pg":      lambda u, p: f"{u}?pg={p}",
    "pagenumber": lambda u, p: f"{u}?pagenumber={p}",
    "startrow": lambda u, p: f"{u}?startrow={(p-1)*20}",
}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


def nap():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def get(url):
    for attempt in range(6):
        try:
            r = SESSION.get(url, timeout=45)
            if r.status_code in (429, 503):
                wait = float(r.headers.get("Retry-After") or 0) or min(180, 8 * (2 ** attempt))
                print(f"   {r.status_code}; wait {wait:.0f}s ({attempt+1}/6)")
                time.sleep(wait)
                continue
            if r.status_code in (400, 404):
                return ""
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"   err (try {attempt+1}): {e}")
            time.sleep(min(90, 5 * (2 ** attempt)))
    return ""


# ---- subjects (from the HTML sitemap) --------------------------------------
def discover_subjects():
    html = get(BASE + "/sitemap")
    slugs, seen = [], set()
    for m in re.finditer(r'href="https?://www\.bookswagon\.com/([a-z0-9][a-z0-9\-]*-books)"', html):
        s = m.group(1)
        if s not in seen:
            seen.add(s)
            slugs.append(s)
    return slugs


# ---- listing-card parsing ---------------------------------------------------
BOOK_RE = re.compile(r'href="((?:https?://www\.bookswagon\.com)?/book/([^"/]+)/(\d{10,13}[Xx]?))"')
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


def _label(lines, name):
    for i, l in enumerate(lines):
        m = re.match(rf"^{name}\s*:\s*(.*)$", l, re.I)
        if m:
            v = m.group(1).strip()
            if v:
                return v
            if i + 1 < len(lines):
                return lines[i + 1].strip()
    return "N/A"


def parse_listing(html):
    """Split the page into per-book segments anchored on /book/ links; parse each."""
    hits = []
    seen = set()
    for m in BOOK_RE.finditer(html):
        url, slug, isbn = m.group(1), m.group(2), m.group(3)
        if isbn not in seen:
            seen.add(isbn)
            hits.append((m.start(), url, isbn))
    out = []
    for n, (pos, url, isbn) in enumerate(hits):
        end = hits[n + 1][0] if n + 1 < len(hits) else min(len(html), pos + 12000)
        seg = html[pos:end]
        title = "N/A"
        # title = text of the title link (the non-image occurrence of this URL)
        for lm in re.finditer(r'href="%s"[^>]*>(.*?)</a>' % re.escape(url), seg, re.S):
            t = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", lm.group(1)))).strip()
            if t:
                title = t
                break
        authors = [re.sub(r"\s+", " ", _html.unescape(a)).strip() for a in
                   re.findall(r'''href=["'](?:https?://www\.bookswagon\.com)?/author/[^"']+["'][^>]*>([^<]+)<''', seg)]
        pub = re.search(r'''href=["'](?:https?://www\.bookswagon\.com)?/publisher/[^"']+["'][^>]*>([^<]+)<''', seg)
        img = re.search(r'src="((?:https?://www\.bookswagon\.com)?/BW/productimages/[^"]+)"', seg)

        def _abs(u):
            return u if not u or u.startswith("http") else BASE + u
        lines = html_to_lines(seg)
        prices = []
        for l in lines:
            for pm in re.finditer(r"₹\s*([\d,]+(?:\.\d+)?)", l):
                prices.append(pm.group(1).replace(",", ""))
        disc = re.search(r"(\d{1,2})\s*%", " ".join(lines[:6]))
        ship = next((l for l in lines if re.match(r"Ships within", l, re.I)), "N/A")
        avail = next((l for l in lines if re.fullmatch(r"(Available|Out of Stock|Pre Order)", l, re.I)), "N/A")
        out.append({
            "title": title,
            "author": ", ".join(dict.fromkeys(authors)) or "N/A",
            "publisher": _html.unescape(pub.group(1)).strip() if pub else "N/A",
            "isbn13": isbn,
            "mrp": prices[0] if prices else "N/A",
            "price": prices[1] if len(prices) > 1 else (prices[0] if prices else "N/A"),
            "discount": disc.group(1) + "%" if disc else "N/A",
            "binding": _label(lines, "Binding"),
            "release": _label(lines, "Release"),
            "language": _label(lines, "Language"),
            "availability": avail,
            "shipping": re.sub(r"\s*Explain.*$", "", ship).strip(),
            "url": _abs(url),
            "image_url": _abs(img.group(1)) if img else "",
        })
    return out


# ---- pagination detection ---------------------------------------------------
def detect_paginator(subject=DEFAULT_SUBJECT):
    base_url = f"{BASE}/{subject}"
    p1 = parse_listing(get(base_url))
    if not p1:
        print("!! page 1 parsed 0 cards — run `dump` and paste the output")
        return None
    first1 = p1[0]["isbn13"]
    print(f"page 1 OK: {len(p1)} cards, first isbn {first1}")
    for name, fn in PAGINATORS.items():
        nap()
        p2 = parse_listing(get(fn(base_url, 2)))
        first2 = p2[0]["isbn13"] if p2 else None
        print(f"  ?{name:<11} -> {len(p2):>2} cards, first isbn {first2}")
        if p2 and first2 != first1:
            print(f"DETECTED paginator: {name}")
            return name
    print("!! no candidate worked — pagination is AJAX-only. Run `dump` and paste it.")
    return None


# ---- ASMX ajax pagination -----------------------------------------------
def page_vars(html):
    """Inline JS vars + hidden fields the listing page defines for its loader."""
    out = {}
    for name in ("searchId", "search_term", "filter", "pageType", "categoryUrl",
                 "pageSize", "totalRecords", "pageNo"):
        m = re.search(rf"""\b{name}\s*=\s*['"]?([^'";\n]+)""", html)
        if m:
            out[name] = m.group(1).strip()
    for hid in re.finditer(r'''id=["'](hdnSearchId|hdnSearchWord|hdnFilter)["'][^>]*value=["']([^"']*)''', html):
        out.setdefault(hid.group(1), hid.group(2))
    return out


def svc_post(method, payload, debug=False):
    """POST a SearchResultService method. Returns the 'd' fragment (str) or ''.
    With debug=True returns (status, ctype, raw_body) for diagnosis."""
    url = f"{BASE}/SearchResultService.asmx/{method}"
    hdrs = {
        "Content-Type": "application/json; charset=utf-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE}/history-books",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": BASE,
    }
    for attempt in range(4):
        try:
            r = SESSION.post(url, json=payload, timeout=45, headers=hdrs)
            if r.status_code in (429, 503):
                time.sleep(float(r.headers.get("Retry-After") or 0) or 8 * (2 ** attempt))
                continue
            if debug:
                return r.status_code, r.headers.get("Content-Type", ""), r.text
            if r.status_code != 200:
                return ""
            try:
                return r.json().get("d") or ""
            except ValueError:
                return r.text or ""
        except Exception as e:
            if debug:
                return -1, "exception", str(e)
            print(f"   svc err (try {attempt+1}): {e}")
            time.sleep(5 * (2 ** attempt))
    return ("", "", "") if debug else ""


def cmd_svcdebug(subject=DEFAULT_SUBJECT):
    """Prime a session on the listing page, then POST the service 3 ways and show
    exactly what comes back — status, content-type, body head."""
    html = get(f"{BASE}/{subject}")
    v = page_vars(html)
    print(f"primed on /{subject}; cookies now: {list(SESSION.cookies.keys())}")
    print(f"vars: searchId={v.get('searchId')} filter={v.get('filter')} "
          f"search_term={v.get('search_term')}\n")
    word = v.get("search_term") or subject
    col = v.get("filter") or "category"
    pay = {"SearchWord": word, "SearchColoumn": col, "ID_Search": v.get("searchId", "0"),
           "PageNumber": 2, "PageSize": 20}

    # A) JSON body (what we tried)
    sc, ct, body = svc_post("GetSearchResultTable", pay, debug=True)
    print(f"[A json ] status={sc} ctype={ct} len={len(body)}")
    print("   " + body[:300].replace("\n", " ") + "\n")

    # B) form-encoded (ASMX often requires this for the raw endpoint)
    try:
        r = SESSION.post(f"{BASE}/SearchResultService.asmx/GetSearchResultTable",
                         data=pay, timeout=45,
                         headers={"X-Requested-With": "XMLHttpRequest",
                                  "Referer": f"{BASE}/{subject}"})
        print(f"[B form ] status={r.status_code} ctype={r.headers.get('Content-Type','')} len={len(r.text)}")
        print("   " + r.text[:300].replace("\n", " ") + "\n")
    except Exception as e:
        print(f"[B form ] error {e}\n")

    # C) GET the .asmx/method?params (some allow HttpGet)
    try:
        from urllib.parse import urlencode
        r = SESSION.get(f"{BASE}/SearchResultService.asmx/GetSearchResultTable?" + urlencode(pay),
                        timeout=45, headers={"X-Requested-With": "XMLHttpRequest"})
        print(f"[C get  ] status={r.status_code} ctype={r.headers.get('Content-Type','')} len={len(r.text)}")
        print("   " + r.text[:300].replace("\n", " "))
    except Exception as e:
        print(f"[C get  ] error {e}")


def _uniq(seq):
    return list(dict.fromkeys(x for x in seq if x not in (None, "")))


def _word_candidates(subject, v):
    return _uniq([v.get("search_term"), v.get("hdnSearchWord"), subject,
                  subject.rsplit("-books", 1)[0], v.get("categoryUrl")])


def _sid_candidates(v):
    return _uniq([v.get("searchId"), v.get("hdnSearchId"), "0"])


def cmd_ajax(subject=DEFAULT_SUBJECT):
    html = get(f"{BASE}/{subject}")            # primes session cookies for the ASMX call
    p1 = parse_listing(html)
    v = page_vars(html)
    print(f"page 1: {len(p1)} cards | cookies: {list(SESSION.cookies.keys())} | vars: " +
          ", ".join(f"{k}={str(x)[:28]}" for k, x in v.items()))
    if not p1:
        print("!! page 1 parsed 0 cards")
        return
    first1 = p1[0]["isbn13"]
    cols = _uniq([v.get("filter"), v.get("hdnFilter"), "category", "subject", "*", ""])
    raw_shown = False
    for method in ("GetSearchResultTable", "GetSearchResultTableSecond"):
        for sid in _sid_candidates(v):
            for w in _word_candidates(subject, v):
                for c in cols:
                    pay = {"SearchWord": w, "SearchColoumn": c, "ID_Search": sid,
                           "PageNumber": 2, "PageSize": 20}
                    d = svc_post(method, pay)
                    recs = parse_listing(d or "")
                    f2 = recs[0]["isbn13"] if recs else None
                    print(f"  [{method[-6:]}] word={w[:20]!r} col={c!r} sid={sid[:10]!r} "
                          f"-> resp {len(d or '')}b, {len(recs)} cards, first {f2}")
                    # if the service returned content but we parsed nothing, show it once
                    if d and not recs and not raw_shown:
                        raw_shown = True
                        print("\n   --- non-empty response but 0 cards parsed; first 900 chars ---")
                        print("   " + (d[:900].replace("\n", " ")))
                        print("   --- (paste this if it looks like books) ---\n")
                    if recs and f2 != first1:
                        print(f"\nWINNER: method={method} SearchWord={w!r} "
                              f"SearchColoumn={c!r} ID_Search={sid!r}")
                        big = parse_listing(svc_post(method, dict(pay, PageSize=100)) or "")
                        ps = 100 if len(big) > 25 else 20
                        print(f"PageSize=100 -> {len(big)} cards; using PageSize={ps}")
                        st = _load_state()
                        st["ajax"] = {
                            "method": method,
                            "col": c,
                            "word_mode": ("search_term" if w == v.get("search_term") else
                                          "slug_nobooks" if w == subject.rsplit("-books", 1)[0] else
                                          "slug"),
                            "sid_mode": "page" if sid not in ("0",) else "zero",
                            "page_size": ps,
                        }
                        _save_state(st)
                        print("saved ajax config to state — run the crawl now")
                        return
                    nap()
    print("!! no combo returned a different page — paste this whole output back")


PATH_FNS = {
    "slash_p-0": lambda s, p: f"{BASE}/{s}/{p}-0",
    "slash_p-1": lambda s, p: f"{BASE}/{s}/{p}-1",
    "slash_p":   lambda s, p: f"{BASE}/{s}/{p}",
    "q_page":    lambda s, p: f"{BASE}/{s}?page={p}",
}


def cmd_pathprobe(subject=DEFAULT_SUBJECT):
    """The ASMX loader is server-broken (missing stored proc). The site's own
    SEO/affiliate URLs paginate by PATH: /<slug>/<page>-<layout> (e.g. /2-0).
    Try the variants and keep the one whose page 2 differs from page 1."""
    p1 = parse_listing(get(f"{BASE}/{subject}"))
    if not p1:
        print("!! page 1 parsed 0 cards")
        return None
    first1 = p1[0]["isbn13"]
    print(f"page 1: {len(p1)} cards, first {first1}")
    for name, fn in PATH_FNS.items():
        nap()
        recs = parse_listing(get(fn(subject, 2)))
        f2 = recs[0]["isbn13"] if recs else None
        print(f"  {name:<10} {fn(subject, 2).replace(BASE, '')} -> {len(recs)} cards, first {f2}")
        if recs and f2 != first1:
            print(f"\nDETECTED path paginator: {name}")
            st = _load_state()
            st["path"] = name
            _save_state(st)
            print("saved to state — run the crawl now")
            return name
    print("!! no path variant worked — paste this output; next step is Playwright")
    return None


def _load_state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {"paginator": None, "done_subjects": [], "current": None}


def _save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        json.dump(st, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"   state save warn: {e}")


# ---- crawl ------------------------------------------------------------------
def _save_batch(recs, st):
    for attempt in range(5):
        try:
            scriptkit.save("bookswagon", recs, key_fields=["isbn13"])
            return True
        except Exception as e:
            print(f"  !! DB save failed ({attempt+1}/5): {e}")
            time.sleep(10)
    print("  !! aborting: database unreachable; rerun to resume here.")
    _save_state(st)
    return False


def run():
    st = _load_state()
    pathname = st.get("path")
    if not pathname:
        print("no path paginator yet — detecting")
        pathname = cmd_pathprobe()
        if not pathname:
            return
        st = _load_state()
    fn = PATH_FNS[pathname]
    subjects = discover_subjects()
    done_subj = set(st.get("done_subjects", []))
    print(f"{len(subjects)} subjects ({len(done_subj)} done); path={pathname}; "
          f"pace {MIN_DELAY}-{MAX_DELAY}s")
    seen = set()                     # in-memory global dedup for stale-stop
    t0, saved = time.time(), 0
    for slug in subjects:
        if slug in done_subj:
            continue
        page = 1
        if st.get("current") and st["current"].get("slug") == slug:
            page = max(1, st["current"].get("page", 1))
        empty, stale = 0, 0
        while True:
            url = f"{BASE}/{slug}" if page == 1 else fn(slug, page)
            recs = parse_listing(get(url))
            if not recs:
                empty += 1
                if empty >= 2:
                    break
                page += 1
                nap()
                continue
            empty = 0
            new = [r for r in recs if r["isbn13"] not in seen]
            seen.update(r["isbn13"] for r in recs)
            for r in recs:
                r["category"] = slug.rsplit("-books", 1)[0].replace("-", " ")
            if not _save_batch(recs, st):
                return
            saved += len(recs)
            stale = stale + 1 if not new else 0
            if stale >= 50:          # only re-serving known books
                print(f"  {slug}: 50 stale pages — moving on")
                break
            st["current"] = {"slug": slug, "page": page + 1}
            _save_state(st)
            if page % 25 == 0 or page == 1:
                rate = saved / max(1e-9, time.time() - t0)
                print(f"  {slug} p{page}: +{len(recs)} ({len(new)} new) | "
                      f"{saved} this run | {rate*3600:.0f}/h | seen {len(seen)}")
            page += 1
            nap()
        done_subj.add(slug)
        st["done_subjects"] = sorted(done_subj)
        st["current"] = None
        _save_state(st)
        print(f"== {slug} done ({saved} rows this run)")
    print(f"\nDone. {saved} rows saved/updated this run.")


# ---- probes -----------------------------------------------------------------
def cmd_dump(subject=DEFAULT_SUBJECT):
    html = get(f"{BASE}/{subject}")
    print(f"fetched {len(html)} chars; /book/ links: {len(BOOK_RE.findall(html))}")
    recs = parse_listing(html)
    print(f"parsed {len(recs)} cards; first 3:")
    for r in recs[:3]:
        print(f"  • {r['title'][:36]:<36} | {r['author'][:20]:<20} | {r['isbn13']} | "
              f"{r['mrp']}->{r['price']} {r['discount']} | {r['binding']} | {r['release']} "
              f"| {r['language']} | {r['availability']}")
    # (a) raw HTML of the first card -> shows the real author/publisher markup
    m0 = BOOK_RE.search(html)
    if m0:
        print("\n=== RAW first-card HTML (for author/publisher markup) ===")
        print(html[m0.start():m0.start() + 2400])
    # (b) every ajax / scroll-loader reference in the page's JS -> the endpoint
    print("\n=== AJAX / loader references ===")
    shown = 0
    for am in re.finditer(r"\$\.ajax\s*\(|\$\.post\s*\(|\$\.get\s*\(|fetch\s*\(|XMLHttpRequest|"
                          r"lastPostLoader|loadmore|LoadMore|PageIndex|pageindex", html):
        frag = html[max(0, am.start() - 300):am.start() + 800]
        if 'id="lastPostLoader"' in frag[280:340]:
            continue                       # skip the spinner div itself
        shown += 1
        print(f"\n--- match {shown}: '{am.group(0)}' at {am.start()} ---")
        print(frag)
        if shown >= 4:
            break
    if not shown:
        print("(none found in inline JS — loader is in an external .js file; "
              "list them:)")
        for sm in re.finditer(r'<script[^>]+src="([^"]+)"', html):
            print("  ", sm.group(1))


def cmd_service():
    """Discover the AJAX pagination call: list SearchResultService methods from
    its JS proxy, then show where the listing JS calls the service."""
    proxy = get(f"{BASE}/SearchResultService.asmx/js")
    print(f"=== SearchResultService.asmx/js: {len(proxy)} chars ===")
    meths = re.findall(r"prototype\.(\w+)\s*=\s*function\s*\(([^)]*)\)", proxy)
    if meths:
        print("methods:")
        for name, params in meths:
            print(f"  {name}({params})")
    else:
        print(proxy[:1500])
    for js in ("/js/minjs/allmin_listing.js?v=5.7", "/js/categorysearch.js", "/js/search.js"):
        body = get(BASE + js)
        print(f"\n=== {js}: {len(body)} chars ===")
        shown = 0
        for am in re.finditer(r"SearchResultService|\.asmx/", body):
            print(f"--- context @ {am.start()} ---")
            print(body[max(0, am.start() - 500):am.start() + 700])
            shown += 1
            if shown >= 3:
                break
        if not shown:
            print("(no service reference)")


def cmd_probe(subject=DEFAULT_SUBJECT):
    detect_paginator(subject)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_SUBJECT
    if cmd == "dump":
        cmd_dump(arg)
    elif cmd == "service":
        cmd_service()
    elif cmd == "ajax":
        cmd_ajax(arg)
    elif cmd == "svcdebug":
        cmd_svcdebug(arg)
    elif cmd == "pathprobe":
        cmd_pathprobe(arg)
    elif cmd == "probe":
        cmd_probe(arg)
    elif cmd == "subjects":
        subs = discover_subjects()
        print(f"{len(subs)} subject slugs; first 10: {subs[:10]}")
    else:
        run()