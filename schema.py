"""The per-site config — this is the JSON 'schema' you edit per site.

One file under sites/ describes one target completely: where to fetch, how to
fetch (engine), and a field map. To add a site you write one of these and never
touch code. Two field-map styles depending on engine:

  http_html / browser   -> each field has a CSS (or XPath) `selector`
  http_json             -> each field has a dotted `path` into the JSON record

Minimal HTML example:

  {
    "name": "books_toscrape",
    "url": "https://books.toscrape.com/",
    "engine": "auto",                       // auto | http_html | browser | http_json
    "list": { "container": "article.product_pod" },
    "next_page": "li.next a::attr(href)",   // optional pagination
    "key_fields": ["url"],                  // what makes a record unique (dedup)
    "fields": {
      "title": { "selector": "h3 a", "attr": "title", "type": "string" },
      "price": { "selector": "p.price_color", "type": "number", "transform": "currency" },
      "in_stock": { "selector": "p.availability", "type": "boolean", "match": "In stock" },
      "url": { "selector": "h3 a", "attr": "href", "type": "url" }
    }
  }
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ENGINES = {"auto", "http_html", "browser", "http_json"}
TYPES = {"string", "number", "int", "boolean", "url", "list"}
# Forgiving aliases: hand-written configs often use Python-style names.
TYPE_ALIASES = {"integer": "int", "float": "number", "double": "number",
                "bool": "boolean", "str": "string", "text": "string",
                "html_text": "string", "html": "string",
                "array": "list"}


@dataclass
class SiteConfig:
    name: str
    url: str
    engine: str = "auto"
    fields: Dict[str, dict] = field(default_factory=dict)
    list: Optional[dict] = None          # {"container": "<selector>"}
    next_page: Optional[str] = None      # css ::attr(href) selector (html only)
    wait_for: Optional[str] = None       # browser: selector to await before read
    load_more: Optional[str] = None      # browser: "load more" button selector to click
    max_clicks: int = 0                  # browser: cap on load-more clicks (0 = until gone)
    key_fields: List[str] = field(default_factory=list)  # dedup key; [] = all
    request: Optional[dict] = None       # http_json: method/url/body/page params
    records_path: str = ""               # http_json: where the list lives
    total_path: Optional[str] = None     # http_json: count for pagination stop
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_json(self) -> bool:
        return self.engine == "http_json"

    @property
    def field_names(self) -> List[str]:
        return list(self.fields.keys())


def _validate(cfg: SiteConfig) -> None:
    if not cfg.name:
        raise ValueError("config needs a 'name'")
    if cfg.engine not in ENGINES:
        raise ValueError(f"{cfg.name}: engine must be one of {sorted(ENGINES)}")
    if not cfg.fields:
        raise ValueError(f"{cfg.name}: no 'fields' defined")
    if cfg.is_json and not cfg.request:
        raise ValueError(f"{cfg.name}: http_json needs a 'request' block")
    if not cfg.is_json and not cfg.url:
        raise ValueError(f"{cfg.name}: needs a 'url'")
    for fname, spec in cfg.fields.items():
        t = spec.get("type", "string")
        t = TYPE_ALIASES.get(t, t)
        spec["type"] = t                      # canonicalize in place
        if t not in TYPES:
            raise ValueError(f"{cfg.name}.{fname}: bad type {t!r}")
        if cfg.is_json:
            if "path" not in spec:
                raise ValueError(f"{cfg.name}.{fname}: http_json field needs 'path'")
        else:
            if "selector" not in spec:
                raise ValueError(f"{cfg.name}.{fname}: field needs 'selector'")


def from_dict(d: dict) -> SiteConfig:
    cfg = SiteConfig(
        name=d.get("name", ""),
        url=d.get("url", ""),
        engine=d.get("engine", "auto"),
        fields=d.get("fields", {}),
        list=d.get("list"),
        next_page=d.get("next_page"),
        wait_for=d.get("wait_for"),
        load_more=d.get("load_more"),
        max_clicks=d.get("max_clicks", 0),
        key_fields=d.get("key_fields", []),
        request=d.get("request"),
        records_path=d.get("records_path", ""),
        total_path=d.get("total_path"),
        raw=d,
    )
    _validate(cfg)
    return cfg


def load(path: str) -> SiteConfig:
    with open(path, encoding="utf-8") as f:
        return from_dict(json.load(f))


def load_all(sites_dir: str) -> Dict[str, SiteConfig]:
    out: Dict[str, SiteConfig] = {}
    if not os.path.isdir(sites_dir):
        return out
    for fn in sorted(os.listdir(sites_dir)):
        if not fn.endswith(".json"):
            continue
        try:
            cfg = load(os.path.join(sites_dir, fn))
            out[cfg.name] = cfg
        except Exception as e:
            # One malformed config shouldn't block registering the rest.
            print(f"  ! skipped {fn}: {e}")
    return out
