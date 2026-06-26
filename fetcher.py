"""Fetching layer. HTTP-first and polite by default (your harvester's Client,
generalized), with an optional Playwright fallback for JS-rendered pages.

The browser is only imported/launched if a fetch actually needs it, so the
package imports fine on a box without Playwright installed.
"""
from __future__ import annotations

import random
import time
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter

import config

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None


class HttpClient:
    """Randomized-delay, auto-retrying session. One instance per run."""

    def __init__(self, min_delay: float = None, max_delay: float = None):
        lo = config.DEFAULT_MIN_DELAY if min_delay is None else min_delay
        hi = config.DEFAULT_MAX_DELAY if max_delay is None else max_delay
        lo, hi = max(0.0, lo), max(0.0, hi)
        self.min_delay, self.max_delay = min(lo, hi), max(lo, hi)
        self._next_wait = 0.0
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": config.USER_AGENT,
                               "Accept": "*/*"})
        if Retry is not None:
            retry = Retry(total=config.HTTP_RETRIES, backoff_factor=2.0,
                          status_forcelist=(429, 500, 502, 503, 504),
                          allowed_methods=frozenset(["GET", "POST"]),
                          respect_retry_after_header=True)
            ad = HTTPAdapter(max_retries=retry)
            self.s.mount("https://", ad)
            self.s.mount("http://", ad)

    def _throttle(self):
        if self._next_wait > 0:
            time.sleep(self._next_wait)

    def get(self, url: str) -> str:
        self._throttle()
        r = self.s.get(url, timeout=config.HTTP_TIMEOUT)
        self._next_wait = random.uniform(self.min_delay, self.max_delay)
        r.raise_for_status()
        return r.text

    def request_json(self, method: str, url: str,
                     body: Optional[dict] = None) -> Any:
        self._throttle()
        # GET/DELETE carry parameters in the query string; POST/PUT/PATCH in a
        # JSON body. (REST APIs like the WooCommerce Store API page via ?page=.)
        if method.upper() in ("GET", "DELETE", "HEAD"):
            r = self.s.request(method, url, params=body, timeout=config.HTTP_TIMEOUT)
        else:
            r = self.s.request(method, url, json=body, timeout=config.HTTP_TIMEOUT)
        self._next_wait = random.uniform(self.min_delay, self.max_delay)
        r.raise_for_status()
        return r.json()


# Hard ceiling on load-more clicks when a config asks for "until it's gone"
# (max_clicks <= 0). Prevents a runaway loop if the button never disappears.
_LOAD_MORE_SAFETY_CAP = 1000


def fetch_browser(url: str, wait_for: Optional[str] = None,
                  timeout_ms: int = 30000,
                  load_more: Optional[str] = None,
                  max_clicks: int = 0) -> str:
    """Render a page with Playwright and return its HTML. Lazy import so the
    rest of the system works without a browser installed.

    If `load_more` is given (a CSS or XPath selector for a "Load More" button),
    the button is clicked repeatedly to exhaust an infinite-scroll listing
    before the HTML is read. `max_clicks` caps the clicks; 0 means "keep going
    until the button disappears" (bounded by an internal safety cap). A
    randomized pause from the same polite window as HTTP requests sits between
    clicks, so the browser path is no more aggressive than the HTTP path.

    First-time setup:  pip install playwright && playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "browser engine requested but Playwright isn't installed. "
            "Run: pip install playwright && playwright install chromium") from e

    def _q(selector: str):
        # Accept CSS or XPath, mirroring extract.py's auto-detection.
        if selector.lstrip().startswith(("/", "(", "./")):
            return f"xpath={selector}"
        return selector

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=config.USER_AGENT)
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=timeout_ms)
                except Exception:
                    pass

            if load_more:
                sel = _q(load_more)
                cap = max_clicks if max_clicks > 0 else _LOAD_MORE_SAFETY_CAP
                for _ in range(cap):
                    try:
                        btn = page.query_selector(sel)
                    except Exception:
                        break
                    if not btn or not btn.is_visible():
                        break
                    try:
                        btn.scroll_into_view_if_needed(timeout=5000)
                        btn.click(timeout=5000)
                    except Exception:
                        break  # button vanished or detached mid-click; we're done
                    # Polite, randomized gap, then let the new batch settle.
                    page.wait_for_timeout(
                        int(random.uniform(config.DEFAULT_MIN_DELAY,
                                           config.DEFAULT_MAX_DELAY) * 1000))
                    try:
                        page.wait_for_load_state("networkidle", timeout=timeout_ms)
                    except Exception:
                        pass

            return page.content()
        finally:
            browser.close()
