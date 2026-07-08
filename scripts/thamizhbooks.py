"""thamizhbooks — Tamil bookstore (WooCommerce/XStore).
The WAF challenges this host's plain-requests TLS fingerprint, so we use
curl_cffi impersonating real Chrome. Same two-stage crawl: enumerate product
URLs (sitemap, else /shop/) -> visit each product page -> extract full record."""
import re
import time
from curl_cffi import requests as cffi
import scriptkit

SITE = "thamizhbooks"
SHOP = "https://thamizhbooks.com/shop/"
SHOP_PAGE = "https://thamizhbooks.com/shop/page/{}/"
SITEMAPS = [
    "https://thamizhbooks.com/product-sitemap.xml",
    "https://thamizhbooks.com/sitemap_index.xml",
    "https://thamizhbooks.com/wp-sitemap.xml",
    "https://thamizhbooks.com/wp-sitemap-posts-product-1.xml",
]
IMPERSONATE = "chrome"     # curl_cffi browser profile
LIMIT = 30                 # 0 = full catalog. Start at 30 to validate.
MAX_SHOP_PAGES = 300
DELAY = 0.4
FLUSH_EVERY = 50

from parsel import Selector


def get(url, tries=3):
    for i in range(tries):
        try:
            r = cffi.get(url, impersonate=IMPERSONATE, timeout=30)
            if r.status_code == 404:
                return None
            if r.ok and not looks_blocked(r.text):
                return r.text
            if r.status_code != 404:
                print(f"   HTTP {r.status_code} (blocked={looks_blocked(r.text)}): {url}")
        except Exception as e:
            print(f"   error ({e}) retry {i + 1}/{tries}")
            time.sleep(6)
    return None


def looks_blocked(html):
    head = html[:3000].lower()
    return "woocommerce" not in head and "elementor" not in head


def num(s):
    m = re.search(r"[\d][\d.,]*", s or "")
    return float(m.group(0).replace(",", "")) if m else None


def probe():
    """One diagnostic fetch so we know whether Chrome-TLS impersonation gets in,
    and — if not — exactly what the WAF is, for the next step."""
    try:
        r = cffi.get(SHOP, impersonate=IMPERSONATE, timeout=30)
    except Exception as e:
        print(f"   probe error: {e}")
        return False
    ok = r.ok and not looks_blocked(r.text)
    print(f"   probe /shop/  status={r.status_code}  server={r.headers.get('server', '?')}  "
          f"cf-ray={r.headers.get('cf-ray', '-')}  passed={ok}")
    if not ok:
        print("   ── still blocked with Chrome impersonation; WAF fingerprint: ──")
        for hk in ("server", "cf-ray", "cf-mitigated", "x-sucuri-id", "x-sucuri-cache",
                   "x-powered-by", "x-turbo-charged-by", "retry-after"):
            if r.headers.get(hk):
                print(f"     {hk}: {r.headers.get(hk)}")
        print(f"     body[:400]: {r.text[:400].strip()}")
    return ok


# ---- Stage 1: collect product URLs --------------------------------------
def urls_from_sitemap():
    for sm in SITEMAPS:
        xml = get(sm)
        if not xml or "<loc>" not in xml:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
        if "/product/" not in xml and any("product" in u.lower() for u in locs):
            collected = []
            for child in [u for u in locs if "product" in u.lower()]:
                cx = get(child)
                if cx:
                    collected += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", cx)
            locs = collected
        prod = [u.split("?")[0] for u in locs if "/product/" in u]
        if prod:
            return list(dict.fromkeys(prod))
    return []


def urls_from_shop():
    seen = []
    for page in range(1, MAX_SHOP_PAGES + 1):
        html = get(SHOP if page == 1 else SHOP_PAGE.format(page))
        if not html:
            break
        found = [u.split("?")[0] for u in
                 Selector(text=html).css("li.product a[href*='/product/']::attr(href)").getall()]
        new = [u for u in dict.fromkeys(found) if u not in seen]
        if not new:
            break
        seen += new
        print(f"   shop page {page}: +{len(new)} (total {len(seen)})")
        time.sleep(DELAY)
    return seen


# ---- Stage 2: extract one product page ----------------------------------
def parse_detail(html, url):
    sel = Selector(text=html)
    title = (sel.css("h1.product_title::text").get()
             or sel.css("h1.entry-title::text").get()
             or sel.css("h1::text").get() or "").strip()

    authors = [a.strip() for a in sel.css("a[href*='/book-author/']::text").getall() if a.strip()]
    pubs = [p.strip() for p in sel.css("a[href*='/book-pub/']::text").getall() if p.strip()]
    cats = [c.strip() for c in sel.css("a[href*='/product-category/']::text").getall() if c.strip()]

    pp = Selector(text=(sel.css("p.price").get() or ""))    # single-product price is <p>, not <span>
    mrp = num("".join(pp.css("del ::text").getall()))
    sp = num("".join(pp.css("ins ::text").getall()))
    if sp is None:
        sp = num("".join(pp.css("::text").getall()))

    img = (sel.css(".woocommerce-product-gallery__image a::attr(href)").get()
           or sel.css("a[href*='/wp-content/uploads/2']::attr(href)").get() or "")

    sku = (sel.css(".sku::text").get() or "").strip()
    m_id = re.search(r"postid-(\d+)", html)
    pid = m_id.group(1) if m_id else ""

    stock = " ".join(sel.css("p.stock::text, .stock::text").getall()).lower()
    in_stock = "out of stock" not in stock

    desc = " ".join(sel.css("#tab-description ::text, "
                            ".woocommerce-Tabs-panel--description ::text, "
                            "#tab_description ::text").getall())
    desc = re.sub(r"\s+", " ", desc).strip()[:1500]

    m_isbn = re.search(r"ISBN[\s:]*([\d\-Xx]{10,17})", html, re.IGNORECASE)
    isbn = m_isbn.group(1) if m_isbn else ""

    return {
        "id": pid, "title": title,
        "author": ", ".join(dict.fromkeys(authors)),
        "publisher": ", ".join(dict.fromkeys(pubs)),
        "category": cats[0] if cats else "",
        "sp": sp, "mrp": mrp, "isbn": isbn, "sku": sku,
        "in_stock": in_stock, "image_url": img,
        "description": desc, "url": url.split("?")[0],
    }


def run():
    print(f"🚀 {SITE}: probing WAF with Chrome-TLS impersonation...")
    if not probe():
        print("\n→ Impersonation didn't get through, so this is IP-level (not TLS). "
              "Next step is the Playwright browser engine or a residential proxy. "
              "Paste the fingerprint lines above and I'll wire up the right one.")
        return

    print(f"\n📚 {SITE}: enumerating product URLs...")
    urls = urls_from_sitemap()
    if urls:
        print(f"   sitemap gave {len(urls)} product URLs")
    else:
        print("   sitemap unavailable — paginating /shop/")
        urls = urls_from_shop()
    if not urls:
        print("❌ Could not enumerate any products.")
        return
    if LIMIT:
        urls = urls[:LIMIT]
    print(f"   fetching {len(urls)} product pages...\n")

    buffer, total_found, total_new = [], 0, 0

    def flush():
        nonlocal buffer, total_found, total_new
        if not buffer:
            return
        f, n = scriptkit.save(SITE, buffer, url=SHOP, key_fields=["url"])
        total_found += f
        total_new += n
        print(f"   💾 {total_found}/{len(urls)} saved (+{n} new)")
        buffer = []

    for i, url in enumerate(urls, 1):
        html = get(url)
        if not html:
            print(f"  [{i}] fetch failed: {url}")
            continue
        rec = parse_detail(html, url)
        buffer.append(rec)
        if i <= 5 or i % 50 == 0:
            print(f"  [{i:>4}/{len(urls)}] {rec['title'][:28]:<28} | ₹{rec['sp']} | {rec['author'][:22]}")
        if len(buffer) >= FLUSH_EVERY:
            flush()
        time.sleep(DELAY)

    flush()
    print(f"\n🎉 Done. {total_found} found, {total_new} new for '{SITE}'.")


if __name__ == "__main__":
    run()