"""Turn a parsed page (or a JSON record) into a clean dict keyed by your field
names, applying type coercion and transforms. Pure functions, no I/O — this is
the part that's unit-tested offline.

Each field returns (value, hit) where hit=False means the selector/path matched
nothing. The runner aggregates hits into per-field fill rates, which is how the
dashboard detects a selector that has silently broken (drift).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from parsel import Selector, SelectorList


# ---- value cleaners ------------------------------------------------------
def _to_number(s: Any) -> Optional[float]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return s
    t = str(s).replace(",", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    if not m:
        return None
    f = float(m.group(0))
    return int(f) if f.is_integer() else f


TRANSFORMS = {
    "currency": _to_number,
    "int": lambda v: (int(_to_number(v)) if _to_number(v) is not None else None),
    "paise": lambda v: (_to_number(v) / 100 if _to_number(v) is not None else None),
    "lower": lambda v: v.lower() if isinstance(v, str) else v,
    "upper": lambda v: v.upper() if isinstance(v, str) else v,
    "strip": lambda v: v.strip() if isinstance(v, str) else v,
}


def _coerce(value: Any, ftype: str, base_url: str = "") -> Any:
    if value is None:
        return None
    if ftype == "number":
        return _to_number(value)
    if ftype == "int":
        n = _to_number(value)
        return int(n) if n is not None else None
    if ftype == "boolean":
        return bool(value)
    if ftype == "url":
        v = str(value).strip()
        return urljoin(base_url, v) if v else None
    if ftype == "string":
        return str(value).strip()
    return value


def _apply(spec: dict, raw: Any, base_url: str) -> Any:
    """regex capture -> transform -> type coercion, in that order."""
    if raw is None:
        return None
    rx = spec.get("regex")
    if rx and isinstance(raw, str):
        m = re.search(rx, raw)
        raw = (m.group(1) if m.groups() else m.group(0)) if m else None
        if raw is None:
            return None
    tf = spec.get("transform")
    if tf and tf in TRANSFORMS:
        raw = TRANSFORMS[tf](raw)
    return _coerce(raw, spec.get("type", "string"), base_url)


# ---- HTML (selector) extraction ------------------------------------------
def _is_xpath(sel: str) -> bool:
    return sel.lstrip().startswith(("/", "(", "./"))


def _select(node: Selector, selector: str) -> SelectorList:
    return node.xpath(selector) if _is_xpath(selector) else node.css(selector)


def _node_text(sl: SelectorList) -> Optional[str]:
    if not sl:
        return None
    txt = sl[0].xpath("normalize-space(string())").get()
    return txt or None


def _node_value(sl_or_node, attr: Optional[str]) -> Optional[str]:
    """One element -> its attribute, or its normalized text."""
    if attr:
        v = sl_or_node.attrib.get(attr)
        return v.strip() if isinstance(v, str) else v
    if isinstance(sl_or_node, SelectorList):
        return _node_text(sl_or_node)
    txt = sl_or_node.xpath("normalize-space(string())").get()
    return txt or None


def extract_html(root: Selector, fields: Dict[str, dict],
                 base_url: str = "") -> Tuple[Dict[str, Any], Dict[str, bool]]:
    out: Dict[str, Any] = {}
    hits: Dict[str, bool] = {}
    for fname, spec in fields.items():
        sel = spec.get("selector")
        ftype = spec.get("type", "string")
        attr = spec.get("attr")
        matched = _select(root, sel) if sel else root

        if ftype == "list":
            items: List[Any] = []
            nodes = matched if isinstance(matched, SelectorList) else [matched]
            for el in nodes:
                v = _apply(spec, _node_value(el, attr), base_url)
                if v not in (None, ""):
                    items.append(v)
            out[fname] = items
            hits[fname] = bool(items)
            continue

        present = bool(matched) if isinstance(matched, SelectorList) else matched is not None
        if ftype == "boolean":
            text = _node_text(matched) if isinstance(matched, SelectorList) else None
            match_str = spec.get("match")
            if match_str is not None:
                out[fname] = bool(text and match_str.lower() in text.lower())
            else:
                out[fname] = present
            hits[fname] = present
            continue

        raw = _node_value(matched, attr) if present else None
        out[fname] = _apply(spec, raw, base_url)
        hits[fname] = raw is not None
    return out, hits


# ---- JSON (dotted path) extraction ---------------------------------------
def _dig(obj: Any, path: str) -> Any:
    if path == "":
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def extract_json(record: dict, fields: Dict[str, dict],
                 base_url: str = "") -> Tuple[Dict[str, Any], Dict[str, bool]]:
    out: Dict[str, Any] = {}
    hits: Dict[str, bool] = {}
    for fname, spec in fields.items():
        raw = _dig(record, spec.get("path", ""))
        ftype = spec.get("type", "string")
        if ftype == "list":
            items = raw if isinstance(raw, list) else ([raw] if raw else [])
            out[fname] = [_apply({"type": "string", **spec, "type": "string"}, x, base_url)
                          if not isinstance(x, (int, float)) else x for x in items]
            hits[fname] = bool(items)
            continue
        out[fname] = _apply(spec, raw, base_url)
        hits[fname] = raw is not None and raw != ""
    return out, hits
