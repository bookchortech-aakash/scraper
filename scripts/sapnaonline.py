"""
SapnaOnline (sapnaonline.com) — India's largest book mall. Custom PHP front-end,
but the catalog is served by a public, no-auth GraphQL API backed by
ElasticSearch:

    POST https://api.sapnaonline.com/graphql        (Authorization: null)

The `catalog.search` operation returns `CatalogReturnType { total, hits[] }`,
and every hit (`CatalogEsEntity`) already carries the FULL record — title,
author, ISBN, publisher, binding, pages, language, edition, series, dates,
MSRP, unit price, discount, description, images, ratings, category. So there is
NO detail-follow: one API call yields a whole page of complete book records.

--- Why publisher-sharding -------------------------------------------------
`store=""` reports total=1,472,226 (the whole catalog), BUT ElasticSearch caps
`skip` at 10,000 (max_result_window) — skip>=5000 errors — and `sort` is
ignored, so deep/keyset pagination is impossible. The only complete path is to
shard the catalog into slices each < 10k results and page each slice 0->total.

The clean partition is PUBLISHER (facet key `product_publisher_keyword`,
~3,056 values, largest ~1,922). We:
  1) pull the full publisher list from getCategoryFilters (the AUTHORS/PUBLISHERS
     facet), for the whole catalog,
  2) for each publisher, page search(term=[{product_publisher_keyword: <name>}])
     from skip 0 -> total (all < 10k),
  3) dedup on ISBN-13 (product_sku) across shards,
  4) any shard that still reports >10k (shouldn't happen for publisher) is
     sub-sharded by product_binding.

Data -> `records` table under site "sapnaonline", deduped on isbn13. Resumable:
a checkpoint records completed publisher shards, so a killed run resumes.

Run:
  python scripts/sapnaonline.py shards               -> list publisher shards + counts (no save)
  python scripts/sapnaonline.py page <publisher>     -> DIAGNOSTIC: first rows for one publisher
  python scripts/sapnaonline.py book <slug|sku>      -> one record via getItemByUrlSlug
  python scripts/sapnaonline.py                       -> full sharded crawl
Pace: SO_MIN_DELAY / SO_MAX_DELAY (default 0.6-1.2s). Test: SO_LIMIT_SHARDS=5.
Page size: SO_PAGE (default 100). Language slice instead of all: SO_LANGUAGE=Kannada.
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

API = "https://api.sapnaonline.com/graphql"
SITE = "sapnaonline"
BOOK_URL = "https://www.sapnaonline.com/books/"

MIN_DELAY = float(os.environ.get("SO_MIN_DELAY", "0.6"))
MAX_DELAY = float(os.environ.get("SO_MAX_DELAY", "1.2"))
PAGE = int(os.environ.get("SO_PAGE", "100"))
SKIP_CAP = 10000                      # ES max_result_window (skip must stay under)
LIMIT_SHARDS = int(os.environ.get("SO_LIMIT_SHARDS", "0"))   # 0 = all shards
LANGUAGE = os.environ.get("SO_LANGUAGE", "").strip()          # optional slice
PUB_KEY = "product_publisher_keyword"
BIND_KEY = "product_binding"

CKPT = os.environ.get("SO_CKPT", "/app/scripts/.sapnaonline_done.json")

H = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": "https://www.sapnaonline.com",
    "Referer": "https://www.sapnaonline.com/",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                   "Version/17 Safari/605.1.15"),
}
SESSION = requests.Session()
SESSION.headers.update(H)

# full record selection — everything CatalogEsEntity exposes that we want
FIELDS = """product_id product_sku product_name product_author product_isbn10
product_binding product_pages product_seo_url product_language product_edition
product_series product_publisher product_publisher_url_slug product_publish_date
product_release_date product_msrp product_unit_price product_discount
product_discount_percentage in_stock product_stock product_avg_rating
product_total_rating product_total_review product_weight product_description
product_type item_store_url_slugs item_category_names item_subcategory_names
product_image_opts { type value } product_images { type value }"""

SEARCH_Q = (
    "query search($term:[ElasticSearchTermQueryInputType],$store:String,"
    "$skip:Int,$limit:Int,$sort:String){catalog{search(term:$term,store:$store,"
    "skip:$skip,limit:$limit,sort:$sort){total hits{%s}}}}" % FIELDS
)
FILTERS_Q = (
    "query($term:[ElasticSearchTermQueryInputType],$filters:"
    "[ElasticSearchTermQueryInputType]){catalog{getCategoryFilters(term:$term,"
    "filters:$filters){characteristics_values{name code key value}}}}"
)
ITEM_Q = ("query($s:String){catalog{getItemByUrlSlug(url_slug:$s){%s}}}" % FIELDS)


# ---- http ---------------------------------------------------------------
def nap():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def gql(query, variables, tries=5):
    for attempt in range(1, tries + 1):
        try:
            r = SESSION.post(API, data=json.dumps(
                {"query": query, "variables": variables}), timeout=90)
            if r.status_code in (429, 502, 503, 504):
                wait = min(90, 5 * (2 ** attempt))
                print(f"   {r.status_code}; wait {wait}s ({attempt}/{tries})")
                time.sleep(wait)
                continue
            d = r.json()
            if d.get("errors"):
                # ES skip-cap or transient; surface briefly, let caller decide
                msg = json.dumps(d["errors"])[:120]
                return {"_error": msg}
            return d.get("data") or {}
        except Exception as e:
            print(f"   err ({attempt}/{tries}): {e}")
            time.sleep(min(60, 4 * (2 ** attempt)))
    return {"_error": "exhausted"}


# ---- helpers ------------------------------------------------------------
def _clean(v):
    if v is None:
        return ""
    v = re.sub(r"<[^>]+>", " ", str(v))
    v = _html.unescape(v)
    return re.sub(r"\s+", " ", v).strip()


def _isbn13(row):
    sku = (row.get("product_sku") or "").strip()
    if re.fullmatch(r"\d{13}", sku):
        return sku
    m = re.search(r"(\d{13})", row.get("product_id") or "")
    return m.group(1) if m else sku


def _img(row):
    for key in ("product_image_opts", "product_images"):
        arr = row.get(key) or []
        prim = [x.get("value") for x in arr if (x.get("type") == "PRIMARY")]
        if prim and prim[0]:
            return prim[0]
        if arr and arr[0].get("value"):
            return arr[0]["value"]
    return ""


def _join(v):
    if isinstance(v, list):
        return ", ".join(str(x) for x in v if x)
    return _clean(v)


def to_record(row):
    def val(x):
        x = _clean(x) if not isinstance(x, list) else _join(x)
        return x if x not in ("", "None") else "N/A"
    isbn13 = _isbn13(row)
    slug = row.get("product_seo_url") or ""
    url = (BOOK_URL + slug) if slug else "N/A"
    return {
        "isbn13": isbn13 or "N/A",
        "isbn10": val(row.get("product_isbn10")),
        "sku": val(row.get("product_sku")),
        "title": val(row.get("product_name")),
        "author": val(row.get("product_author")),
        "publisher": val(row.get("product_publisher")),
        "language": val(row.get("product_language")),
        "binding": val(row.get("product_binding")),
        "pages": val(row.get("product_pages")),
        "edition": val(row.get("product_edition")),
        "series": val(row.get("product_series")),
        "publish_date": val(row.get("product_publish_date")),
        "release_date": val(row.get("product_release_date")),
        "mrp": val(row.get("product_msrp")),
        "price": val(row.get("product_unit_price")),
        "discount_pct": val(row.get("product_discount_percentage")),
        "in_stock": val(row.get("in_stock")),
        "stock_qty": val(row.get("product_stock")),
        "avg_rating": val(row.get("product_avg_rating")),
        "total_ratings": val(row.get("product_total_rating")),
        "category": _join(row.get("item_category_names")) or "N/A",
        "subcategory": _join(row.get("item_subcategory_names")) or "N/A",
        "store": _join(row.get("item_store_url_slugs")) or "N/A",
        "description": val(row.get("product_description")),
        "image_url": _img(row) or "N/A",
        "url": url,
        "product_id": val(row.get("product_id")),
    }


# ---- shard discovery ----------------------------------------------------
def base_term():
    """Term list applied to every shard (optional language slice)."""
    if LANGUAGE:
        return [{"field": "product_language", "value": LANGUAGE}]
    return []


def publisher_shards():
    """[(publisher_name, count), ...] from the PUBLISHERS facet, whole catalog
    (or language slice). Descending by count."""
    data = gql(FILTERS_Q, {"term": base_term(), "filters": []})
    if data.get("_error"):
        print("  facet error:", data["_error"])
        return []
    cvs = (((data.get("catalog") or {}).get("getCategoryFilters") or {})
           .get("characteristics_values") or [])
    pubs = [(x["name"], int(x["value"])) for x in cvs
            if x.get("key") == PUB_KEY and x.get("name")]
    pubs.sort(key=lambda t: -t[1])
    return pubs


# ---- paging one shard ---------------------------------------------------
def shard_term(publisher, binding=None):
    t = base_term() + [{"field": PUB_KEY, "value": publisher}]
    if binding:
        t.append({"field": BIND_KEY, "value": binding})
    return t


def page_shard(term, cap=SKIP_CAP):
    """Yield rows for a shard, paging skip 0->total while skip<cap."""
    skip, total = 0, None
    while skip < cap:
        data = gql(SEARCH_Q, {"term": term, "skip": skip,
                              "limit": PAGE, "sort": ""})
        if data.get("_error"):
            print(f"     page error @skip={skip}: {data['_error']}")
            break
        s = (data.get("catalog") or {}).get("search") or {}
        if total is None:
            total = int(s.get("total") or 0)
        hits = s.get("hits") or []
        if not hits:
            break
        for row in hits:
            yield row, total
        skip += PAGE
        if skip >= total:
            break
        nap()


def bindings_for(publisher):
    data = gql(FILTERS_Q, {"term": shard_term(publisher), "filters": []})
    cvs = (((data.get("catalog") or {}).get("getCategoryFilters") or {})
           .get("characteristics_values") or [])
    return [x["name"] for x in cvs if x.get("key") == BIND_KEY and x.get("name")]


# ---- checkpoint ---------------------------------------------------------
def _load_done():
    try:
        return set(json.load(open(CKPT, encoding="utf-8")).get("done", []))
    except Exception:
        return set()


def _save_done(done):
    try:
        os.makedirs(os.path.dirname(CKPT) or ".", exist_ok=True)
        tmp = CKPT + ".tmp"
        json.dump({"done": sorted(done)}, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, CKPT)
    except Exception as e:
        print(f"   ckpt warn: {e}")


# ---- run ----------------------------------------------------------------
def run():
    pubs = publisher_shards()
    if not pubs:
        print("no publisher shards found; aborting.")
        return
    grand = sum(c for _, c in pubs)
    print(f"{len(pubs)} publisher shards | facet-sum={grand:,}"
          + (f" | language={LANGUAGE}" if LANGUAGE else "")
          + (f" | LIMIT_SHARDS={LIMIT_SHARDS}" if LIMIT_SHARDS else ""))

    done = _load_done()
    seen_isbn = set()
    saved = dbfail = 0
    t0 = time.time()
    shard_list = pubs[:LIMIT_SHARDS] if LIMIT_SHARDS else pubs

    for idx, (pub, cnt) in enumerate(shard_list, 1):
        tag = f"{LANGUAGE}|{pub}" if LANGUAGE else pub
        if tag in done:
            continue
        # publisher > cap -> sub-shard by binding (rare)
        subshards = [None]
        if cnt > SKIP_CAP:
            subshards = bindings_for(pub) or [None]
            print(f"[{idx}/{len(shard_list)}] {pub} ({cnt}) > {SKIP_CAP}: "
                  f"sub-sharding into {len(subshards)} bindings")

        batch, got = [], 0
        for b in subshards:
            for row, total in page_shard(shard_term(pub, b)):
                isbn = _isbn13(row)
                if isbn and isbn in seen_isbn:
                    continue
                if isbn:
                    seen_isbn.add(isbn)
                batch.append(to_record(row))
                got += 1
                if len(batch) >= 200:
                    ok = _flush(batch)
                    if ok is False:
                        dbfail += 1
                        if dbfail >= 5:
                            print("  !! DB unreachable 5x; abort (resumable).")
                            return
                    else:
                        saved += len(batch); dbfail = 0
                    batch = []
        if batch:
            if _flush(batch) is not False:
                saved += len(batch)
            batch = []

        done.add(tag)
        _save_done(done)
        rate = saved / max(1e-9, time.time() - t0)
        print(f"[{idx}/{len(shard_list)}] {pub[:32]:<32} +{got:<5} "
              f"| saved {saved:,} | uniq {len(seen_isbn):,} | {rate*60:.0f}/min")
        nap()

    print(f"\nDone. {saved:,} rows saved, {len(seen_isbn):,} unique ISBNs.")


def _flush(batch):
    try:
        scriptkit.save(SITE, batch, url="https://www.sapnaonline.com",
                       key_fields=["isbn13"])
        return True
    except Exception as e:
        print(f"  !! DB save failed: {e}")
        time.sleep(10)
        return False


# ---- diagnostics --------------------------------------------------------
def cmd_shards():
    pubs = publisher_shards()
    over = [p for p in pubs if p[1] > SKIP_CAP]
    print(f"{len(pubs)} publisher shards, facet-sum={sum(c for _,c in pubs):,}")
    print(f"shards over {SKIP_CAP} (need binding sub-shard): {len(over)}")
    for name, c in pubs[:20]:
        print(f"   {c:>7}  {name}")
    if over:
        print("OVER-CAP:", ", ".join(f"{n}({c})" for n, c in over))


def cmd_page(publisher):
    data = gql(SEARCH_Q, {"term": shard_term(publisher), "skip": 0,
                          "limit": 3, "sort": ""})
    s = (data.get("catalog") or {}).get("search") or {}
    print(f"total for publisher {publisher!r}: {s.get('total')}")
    for row in (s.get("hits") or [])[:3]:
        rec = to_record(row)
        for k, v in rec.items():
            print(f"   {k:>14}: {str(v)[:80]}")
        print("   " + "-" * 40)


def cmd_book(arg):
    slug = arg
    if re.fullmatch(r"\d{10,13}", arg):   # sku -> resolve via search
        data = gql(SEARCH_Q, {"term": [{"field": "product_sku", "value": arg}],
                              "skip": 0, "limit": 1, "sort": ""})
        hits = ((data.get("catalog") or {}).get("search") or {}).get("hits") or []
        if hits:
            for k, v in to_record(hits[0]).items():
                print(f"   {k:>14}: {str(v)[:90]}")
            return
        print("  not found by sku"); return
    data = gql(ITEM_Q, {"s": slug})
    it = (data.get("catalog") or {}).get("getItemByUrlSlug")
    if not it:
        print("  not found"); return
    for k, v in to_record(it).items():
        print(f"   {k:>14}: {str(v)[:90]}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else ""
    if cmd == "shards":
        cmd_shards()
    elif cmd == "page":
        cmd_page(arg or "Penguin India")
    elif cmd == "book":
        cmd_book(arg)
    else:
        run()