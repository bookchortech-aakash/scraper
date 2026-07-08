"""Offline proof for the extraction core. No network, no DB.
Run:  python test_extract.py
"""
from parsel import Selector

import extract

HTML = """
<ol class="row">
  <li><article class="product_pod">
    <h3><a href="catalogue/a-light_1/index.html" title="A Light in the Attic">A Light...</a></h3>
    <p class="star-rating Three"></p>
    <p class="price_color">£51.77</p>
    <p class="instock availability">  In stock  </p>
    <img src="media/cache/2c/da/foo.jpg">
  </article></li>
  <li><article class="product_pod">
    <h3><a href="catalogue/tipping_2/index.html" title="Tipping the Velvet">Tipping...</a></h3>
    <p class="star-rating One"></p>
    <p class="price_color">£1,053.20</p>
    <p class="availability">Out of stock</p>
    <img src="media/cache/aa/bb/bar.jpg">
  </article></li>
</ol>
"""

FIELDS = {
    "title":    {"selector": "h3 a", "attr": "title", "type": "string"},
    "price":    {"selector": "p.price_color", "type": "number", "transform": "currency"},
    "in_stock": {"selector": "p.availability", "type": "boolean", "match": "In stock"},
    "rating":   {"selector": "p.star-rating", "attr": "class", "type": "string",
                 "regex": r"star-rating (\w+)"},
    "url":      {"selector": "h3 a", "attr": "href", "type": "url"},
    "images":   {"selector": "img", "attr": "src", "type": "list"},
}

BASE = "https://books.toscrape.com/"


def main():
    sel = Selector(text=HTML)
    containers = sel.css("article.product_pod")
    assert len(containers) == 2, f"expected 2 containers, got {len(containers)}"

    rows = []
    for c in containers:
        data, hits = extract.extract_html(c, FIELDS, base_url=BASE)
        rows.append((data, hits))
        print(data)

    a, _ = rows[0]
    assert a["title"] == "A Light in the Attic", a["title"]
    assert a["price"] == 51.77, a["price"]
    assert a["in_stock"] is True, a["in_stock"]
    assert a["rating"] == "Three", a["rating"]
    assert a["url"] == "https://books.toscrape.com/catalogue/a-light_1/index.html", a["url"]
    assert a["images"] == ["https://books.toscrape.com/media/cache/2c/da/foo.jpg"] or \
        a["images"] == ["media/cache/2c/da/foo.jpg"], a["images"]

    b, _ = rows[1]
    assert b["price"] == 1053.20, b["price"]
    assert b["in_stock"] is False, b["in_stock"]
    assert b["rating"] == "One", b["rating"]

    # JSON path extraction (the schoolsindia-style engine)
    rec = {"name": "Demo School", "affiliationNumber": 12345,
           "address": {"pin": "131001"}, "phones": ["011-2222", "98100-00000"]}
    jfields = {
        "name": {"path": "name", "type": "string"},
        "affiliation": {"path": "affiliationNumber", "type": "string"},
        "pincode": {"path": "address.pin", "type": "string"},
        "phones": {"path": "phones", "type": "list"},
        "missing": {"path": "nope.nope", "type": "string"},
    }
    jd, jh = extract.extract_json(rec, jfields)
    print(jd)
    assert jd["name"] == "Demo School"
    assert jd["affiliation"] == "12345"
    assert jd["pincode"] == "131001"
    assert jd["phones"] == ["011-2222", "98100-00000"]
    assert jh["missing"] is False and jd["missing"] is None

    print("\nALL EXTRACTION TESTS PASSED")


if __name__ == "__main__":
    main()
