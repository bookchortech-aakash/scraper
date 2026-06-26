"""The engine ties fetching to extraction and pagination, and decides — for
`auto` — whether the static HTML was enough or a browser render is needed.

iter_pages(cfg) yields one PageBatch per fetched page, so a caller can store
records and update progress *as the run proceeds* (live dashboard). scrape(cfg)
wraps it into a single ScrapeResult for callers that just want everything.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urljoin

from parsel import Selector

import config
import extract
from fetcher import HttpClient, fetch_browser
from schema import SiteConfig


@dataclass
class PageBatch:
    records: List[Dict[str, Any]]
    hit_counts: Dict[str, int]          # field -> # records where it was present
    n: int
    engine_used: str
    total: Optional[int] = None         # site-reported total, if any


@dataclass
class ScrapeResult:
    records: List[Dict[str, Any]] = field(default_factory=list)
    hit_counts: Dict[str, int] = field(default_factory=dict)
    total_records: int = 0
    pages: int = 0
    engine_used: str = ""


# ---- HTML helpers --------------------------------------------------------
def _parse_records_html(html: str, cfg: SiteConfig,
                        base_url: str) -> List[Tuple[dict, dict]]:
    sel = Selector(text=html)
    out = []
    if cfg.list and cfg.list.get("container"):
        for r in sel.css(cfg.list["container"]):
            out.append(extract.extract_html(r, cfg.fields, base_url))
    else:
        out.append(extract.extract_html(sel, cfg.fields, base_url))
    return out


def _html_hit_ratio(parsed: List[Tuple[dict, dict]]) -> float:
    if not parsed:
        return 0.0
    _, hits = parsed[0]
    return (sum(1 for v in hits.values() if v) / len(hits)) if hits else 0.0


def _next_url(html: str, cfg: SiteConfig, base_url: str) -> Optional[str]:
    if not cfg.next_page:
        return None
    href = Selector(text=html).css(cfg.next_page).get()
    return urljoin(base_url, href) if href else None


def _batch_from_parsed(parsed: List[Tuple[dict, dict]], used: str) -> PageBatch:
    recs, counts = [], {}
    for data, hits in parsed:
        recs.append(data)
        for k, present in hits.items():
            counts[k] = counts.get(k, 0) + (1 if present else 0)
    return PageBatch(recs, counts, len(recs), used)


def _iter_html(cfg: SiteConfig) -> Iterator[PageBatch]:
    client = HttpClient()
    url = cfg.url
    seen = set()
    pages = 0
    while url and pages < config.MAX_PAGES_GUARD and url not in seen:
        seen.add(url)
        if cfg.engine == "browser":
            html = fetch_browser(url, cfg.wait_for,
                                 load_more=cfg.load_more,
                                 max_clicks=cfg.max_clicks)
            used = "browser"
        else:
            html = client.get(url)
            used = "http"
        parsed = _parse_records_html(html, cfg, url)
        if cfg.engine == "auto" and _html_hit_ratio(parsed) < 0.34:
            try:
                html = fetch_browser(url, cfg.wait_for,
                                     load_more=cfg.load_more,
                                     max_clicks=cfg.max_clicks)
                parsed = _parse_records_html(html, cfg, url)
                used = "browser"
            except RuntimeError:
                pass  # no browser installed; keep the http result
        pages += 1
        yield _batch_from_parsed(parsed, used)
        url = _next_url(html, cfg, url)


# ---- JSON helpers --------------------------------------------------------
def _json_records(payload: Any, cfg: SiteConfig) -> Tuple[List[dict], Optional[int]]:
    rows = extract._dig(payload, cfg.records_path) if cfg.records_path else payload
    if isinstance(payload, dict) and not cfg.records_path:
        for key in ("data", "result", "items", "records"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    if not isinstance(rows, list):
        return [], None
    total = None
    if cfg.total_path:
        if cfg.total_path.startswith("$"):
            t = extract._dig(payload, cfg.total_path[1:])
        else:
            t = extract._dig(rows[0], cfg.total_path) if rows else None
        try:
            total = int(t)
        except (TypeError, ValueError):
            total = None
    return rows, total


def _set_path(obj: dict, path: str, value: Any) -> None:
    """Set body[path]=value, where path may be dotted into nested dicts
    (e.g. 'variables.skip' for a GraphQL body). Flat keys still work."""
    parts = path.split(".")
    for p in parts[:-1]:
        nxt = obj.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            obj[p] = nxt
        obj = nxt
    obj[parts[-1]] = value


def _iter_json(cfg: SiteConfig) -> Iterator[PageBatch]:
    client = HttpClient()
    req = cfg.request or {}
    method = req.get("method", "POST")
    url = req.get("url") or cfg.url
    body = copy.deepcopy(req.get("body", {}))   # deep: nested params get mutated
    page_param = req.get("page_param")
    size_param = req.get("page_size_param")
    page = req.get("page_start", 1)
    size = req.get("page_size", 25)
    if size_param:
        _set_path(body, size_param, size)

    pages, total = 0, None
    while pages < config.MAX_PAGES_GUARD:
        if page_param:
            _set_path(body, page_param, page)
        payload = client.request_json(method, url, body)
        rows, t = _json_records(payload, cfg)
        if t is not None:
            total = t
        if not rows:
            break
        recs, counts = [], {}
        for o in rows:
            if not isinstance(o, dict):
                continue
            data, hits = extract.extract_json(o, cfg.fields, cfg.url)
            recs.append(data)
            for k, present in hits.items():
                counts[k] = counts.get(k, 0) + (1 if present else 0)
        pages += 1
        yield PageBatch(recs, counts, len(recs), "http_json", total)
        if not page_param or len(rows) < size:
            break
        if total is not None and page * size >= total:
            break
        page += 1


def iter_pages(cfg: SiteConfig) -> Iterator[PageBatch]:
    yield from (_iter_json(cfg) if cfg.is_json else _iter_html(cfg))


def scrape(cfg: SiteConfig) -> ScrapeResult:
    res = ScrapeResult()
    for b in iter_pages(cfg):
        res.engine_used = b.engine_used or res.engine_used
        res.records.extend(b.records)
        res.total_records += b.n
        res.pages += 1
        for k, c in b.hit_counts.items():
            res.hit_counts[k] = res.hit_counts.get(k, 0) + c
    return res
