"""Postgres storage. Generic by design: a record's fields live in a JSONB
column, so the same three tables serve every site regardless of its schema.

  sites        one row per registered config
  runs         one row per scrape run (status, counts, timing, errors)
  records      every scraped row; (site, hash) is unique -> cross-run dedup
  field_stats  per-run per-field fill counts -> fuels the drift detector
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import Json, RealDictCursor

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  name        text PRIMARY KEY,
  url         text,
  engine      text,
  cfg         jsonb NOT NULL,
  enabled     boolean DEFAULT true,
  created_at  timestamptz DEFAULT now(),
  updated_at  timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS runs (
  id            bigserial PRIMARY KEY,
  site          text REFERENCES sites(name) ON DELETE CASCADE,
  status        text DEFAULT 'running',           -- running|ok|error|partial
  engine_used   text,
  pages         int DEFAULT 0,
  records_found int DEFAULT 0,
  records_new   int DEFAULT 0,
  error         text,
  started_at    timestamptz DEFAULT now(),
  finished_at   timestamptz
);
CREATE TABLE IF NOT EXISTS records (
  id          bigserial PRIMARY KEY,
  site        text REFERENCES sites(name) ON DELETE CASCADE,
  run_id      bigint REFERENCES runs(id) ON DELETE SET NULL,
  hash        text NOT NULL,
  data        jsonb NOT NULL,
  provenance  jsonb,
  first_seen  timestamptz DEFAULT now(),
  last_seen   timestamptz DEFAULT now(),
  UNIQUE (site, hash)
);
CREATE INDEX IF NOT EXISTS records_site_idx ON records(site);
CREATE INDEX IF NOT EXISTS records_seen_idx ON records(last_seen DESC);
CREATE TABLE IF NOT EXISTS field_stats (
  run_id     bigint REFERENCES runs(id) ON DELETE CASCADE,
  field      text,
  total      int,
  filled     int,
  fill_rate  real,
  PRIMARY KEY (run_id, field)
);
"""


@contextmanager
def conn():
    c = psycopg2.connect(**config.PG)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.cursor().execute(SCHEMA)


def record_hash(site: str, data: Dict[str, Any], key_fields: List[str]) -> str:
    keys = key_fields or sorted(data.keys())
    # Safety net: if the chosen key fields are all empty for this record,
    # fall back to hashing the whole row so records can't all collapse to one.
    if all(data.get(k) in (None, "", []) for k in keys):
        keys = sorted(data.keys())
    basis = "|".join(f"{k}={data.get(k)}" for k in keys)
    return hashlib.sha1(f"{site}|{basis}".encode("utf-8")).hexdigest()


# ---- sites ---------------------------------------------------------------
def upsert_site(name: str, url: str, engine: str, cfg: dict):
    with conn() as c:
        c.cursor().execute(
            """INSERT INTO sites(name,url,engine,cfg) VALUES (%s,%s,%s,%s)
               ON CONFLICT (name) DO UPDATE SET
                 url=EXCLUDED.url, engine=EXCLUDED.engine,
                 cfg=EXCLUDED.cfg, updated_at=now();""",
            (name, url, engine, Json(cfg)))


# ---- runs ----------------------------------------------------------------
def start_run(site: str) -> int:
    with conn() as c:
        cur = c.cursor()
        cur.execute("INSERT INTO runs(site) VALUES (%s) RETURNING id;", (site,))
        return cur.fetchone()[0]


def finish_run(run_id: int, status: str, engine_used: str, pages: int,
               found: int, new: int, error: Optional[str] = None):
    with conn() as c:
        c.cursor().execute(
            """UPDATE runs SET status=%s, engine_used=%s, pages=%s,
               records_found=%s, records_new=%s, error=%s, finished_at=now()
               WHERE id=%s;""",
            (status, engine_used, pages, found, new, error, run_id))


# ---- records (dedup upsert) ----------------------------------------------
def upsert_record(site: str, run_id: int, h: str,
                  data: Dict[str, Any], provenance: Dict[str, Any]) -> bool:
    """Returns True if this was a brand-new record (for records_new counts)."""
    with conn() as c:
        cur = c.cursor()
        cur.execute(
            """INSERT INTO records(site,run_id,hash,data,provenance)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (site,hash) DO UPDATE SET
                 last_seen=now(), run_id=EXCLUDED.run_id, data=EXCLUDED.data
               RETURNING (xmax = 0) AS inserted;""",
            (site, run_id, h, Json(data), Json(provenance)))
        return bool(cur.fetchone()[0])


def save_field_stats(run_id: int, total: int, hit_counts: Dict[str, int]):
    with conn() as c:
        cur = c.cursor()
        for f, filled in hit_counts.items():
            rate = (filled / total) if total else 0.0
            cur.execute(
                """INSERT INTO field_stats(run_id,field,total,filled,fill_rate)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING;""",
                (run_id, f, total, filled, rate))


# ---- dashboard reads ------------------------------------------------------
def _rows(sql: str, params=()) -> List[dict]:
    with conn() as c:
        cur = c.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, params)
        return cur.fetchall()


def sites_overview() -> List[dict]:
    return _rows("""
      SELECT s.name, s.url, s.engine, s.enabled,
             COALESCE(rc.cnt,0) AS records,
             lr.status AS last_status, lr.finished_at AS last_run,
             lr.records_new AS last_new
      FROM sites s
      LEFT JOIN (SELECT site, count(*) cnt FROM records GROUP BY site) rc
             ON rc.site = s.name
      LEFT JOIN LATERAL (
             SELECT status, finished_at, records_new FROM runs
             WHERE site = s.name ORDER BY started_at DESC LIMIT 1) lr ON true
      ORDER BY s.name;""")


def recent_runs(limit: int = 20) -> List[dict]:
    return _rows("""
      SELECT id, site, status, engine_used, pages, records_found, records_new,
             error, started_at, finished_at,
             EXTRACT(EPOCH FROM (COALESCE(finished_at,now())-started_at)) AS secs
      FROM runs ORDER BY started_at DESC LIMIT %s;""", (min(int(limit), 100),))


def field_fill_latest(site: str) -> List[dict]:
    """Latest run's fill rates plus the trailing average, with a drift flag."""
    return _rows("""
      WITH last AS (
        SELECT id FROM runs WHERE site=%s AND status<>'running'
        ORDER BY started_at DESC LIMIT 1),
      prev AS (
        SELECT field, avg(fill_rate) AS avg_rate
        FROM field_stats
        WHERE run_id IN (SELECT id FROM runs WHERE site=%s
                         AND id <> (SELECT id FROM last)
                         ORDER BY started_at DESC LIMIT 5)
        GROUP BY field)
      SELECT fs.field, fs.filled, fs.total, fs.fill_rate,
             COALESCE(p.avg_rate, fs.fill_rate) AS avg_rate,
             (COALESCE(p.avg_rate,0) >= %s AND fs.fill_rate <= %s) AS drift
      FROM field_stats fs
      LEFT JOIN prev p ON p.field = fs.field
      WHERE fs.run_id = (SELECT id FROM last)
      ORDER BY fs.field;""",
      (site, site, config.DRIFT_HIGH, config.DRIFT_LOW))


def preview(site: str, limit: int = 50, q: str = "") -> List[dict]:
    where, params = ["site = %s"], [site]
    if q:
        where.append("data::text ILIKE %s")
        params.append(f"%{q}%")
    params.append(min(int(limit), 500))
    return _rows(
        f"""SELECT id, data, last_seen FROM records
            WHERE {' AND '.join(where)}
            ORDER BY last_seen DESC LIMIT %s;""", params)


def delete_runs() -> int:
    """Clear finished run history; keep any run still in progress (deleting a
    live run's row would break its in-flight inserts). field_stats for the
    cleared runs cascade away; records survive (run_id set null)."""
    with conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM runs WHERE status <> 'running';")
        return cur.rowcount


def mark_orphan_runs() -> int:
    """Called on dashboard startup: a fresh process means no run can actually
    be alive, so any row still 'running' is an orphan from a crash/restart.
    Flip them to 'interrupted' so they stop showing as live and can be cleared."""
    with conn() as c:
        cur = c.cursor()
        cur.execute("UPDATE runs SET status='interrupted', finished_at=now() "
                    "WHERE status='running';")
        return cur.rowcount


def force_stop_site_runs(site: str) -> int:
    """Force-finish a site's stale 'running' rows when there's no live thread
    to cancel (e.g. a zombie left by a restart)."""
    with conn() as c:
        cur = c.cursor()
        cur.execute("UPDATE runs SET status='partial', finished_at=now() "
                    "WHERE site=%s AND status='running';", (site,))
        return cur.rowcount


def delete_records(site: Optional[str] = None, ids: Optional[List[int]] = None,
                   all_sites: bool = False) -> int:
    """Delete scraped rows. Priority: all_sites > ids (within site) > whole site.
    Never touches the site config — only the records table."""
    with conn() as c:
        cur = c.cursor()
        if all_sites:
            cur.execute("DELETE FROM records;")
        elif ids:
            cur.execute("DELETE FROM records WHERE site=%s AND id = ANY(%s);",
                        (site, ids))
        elif site:
            cur.execute("DELETE FROM records WHERE site=%s;", (site,))
        else:
            return 0
        return cur.rowcount


def export_rows(site: str) -> List[dict]:
    return _rows("SELECT data FROM records WHERE site=%s ORDER BY id;", (site,))


def site_fields(name: str) -> List[str]:
    r = _rows("SELECT cfg FROM sites WHERE name=%s;", (name,))
    if not r:
        return []
    return list((r[0]["cfg"] or {}).get("fields", {}).keys())


def _observed_fields(site: str) -> List[str]:
    seen: List[str] = []
    for x in _rows("SELECT data FROM records WHERE site=%s LIMIT 200;", (site,)):
        for k in (x["data"] or {}):
            if k not in seen and not k.startswith("_"):
                seen.append(k)
    return seen


def columns_for(site: str) -> List[str]:
    return site_fields(site) or _observed_fields(site)


GRID_OPS = {"contains": "ILIKE", "equals": "=", "not_equals": "!=",
            "starts_with": "ILIKE", "greater": ">", "less": "<"}


def _site_names() -> List[str]:
    return [r["name"] for r in _rows("SELECT name FROM sites ORDER BY name;")]


def groups() -> dict:
    """Sites that share a prefix before the first '_' form a group (a website
    split into category configs, e.g. all 'sapna_*'). Returns {prefix: [sites]}
    for prefixes with more than one site."""
    out: dict = {}
    for n in _site_names():
        out.setdefault(n.split("_", 1)[0], []).append(n)
    return {p: ms for p, ms in out.items() if len(ms) > 1}


def _resolve(site: str):
    """(members, is_group, prefix). 'group:sapna' -> all sapna_* merged."""
    if site.startswith("group:"):
        pre = site[6:]
        return groups().get(pre, []), True, pre
    return [site], False, ""


def _category_of(site_name: str, prefix: str) -> str:
    if prefix and site_name.startswith(prefix + "_"):
        return site_name[len(prefix) + 1:]
    return site_name


def grid(site: str, offset: int = 0, limit: int = 100, sort: str = "",
         direction: str = "asc", fcol: str = "", fop: str = "",
         fval: str = "") -> dict:
    """Server-side page of one site (or a merged group): total count + one
    window of rows, with optional filter and sort. For a group, every row gets
    a `_category` column. Built for millions of rows."""
    members, grp, prefix = _resolve(site)
    if not members:
        return {"columns": [], "rows": [], "total": 0}
    base = columns_for(members[0])
    where, params = ["site = ANY(%s)"], [members]
    if fval and fcol:
        if fcol == "_category" and grp:
            where.append("site ILIKE %s")
            params.append(f"%{fval}%")
        elif fcol == "__all__":
            where.append("data::text ILIKE %s")
            params.append(f"%{fval}%")
        elif fcol in base and fop in GRID_OPS:
            if fop == "contains":
                where.append("data->>%s ILIKE %s"); params += [fcol, f"%{fval}%"]
            elif fop == "starts_with":
                where.append("data->>%s ILIKE %s"); params += [fcol, f"{fval}%"]
            else:
                where.append(f"data->>%s {GRID_OPS[fop]} %s"); params += [fcol, fval]
    wsql = " AND ".join(where)

    with conn() as c:
        cur = c.cursor(cursor_factory=RealDictCursor)
        cur.execute(f"SELECT count(*) AS n FROM records WHERE {wsql};", params)
        total = cur.fetchone()["n"]
        oparams = []
        if sort == "_category" and grp:
            order = f"ORDER BY site {'DESC' if str(direction).lower()=='desc' else 'ASC'}, id ASC"
        elif sort in base:
            d = "DESC" if str(direction).lower() == "desc" else "ASC"
            order, oparams = f"ORDER BY data->>%s {d}, id ASC", [sort]
        else:
            order = "ORDER BY id ASC"
        cur.execute(
            f"SELECT id, site, data FROM records WHERE {wsql} {order} OFFSET %s LIMIT %s;",
            params + oparams + [max(0, int(offset)), min(int(limit), 1000)])
        rows = cur.fetchall()
    out = []
    for r in rows:
        row = {"_rid": r["id"], **(r["data"] or {})}
        if grp:
            row["_category"] = _category_of(r["site"], prefix)
        out.append(row)
    return {"columns": (["_category"] + base) if grp else base,
            "rows": out, "total": total}


def analyze(site: str, col: str, top: int = 50, search: str = "") -> dict:
    """Category breakdown of one column (top-N + Others), across a single site
    or a merged group. `col='_category'` on a group counts rows per category."""
    members, grp, prefix = _resolve(site)
    if not members:
        return {"error": "no data", "rows": [], "total": 0}
    base = columns_for(members[0])
    if not (col in base or (col == "_category" and grp)):
        return {"error": "unknown column", "rows": [], "total": 0}

    with conn() as c:
        cur = c.cursor(cursor_factory=RealDictCursor)
        if col == "_category" and grp:
            cur.execute("SELECT count(*) AS n FROM records WHERE site=ANY(%s);", [members])
            total = cur.fetchone()["n"]
            cur.execute("SELECT site AS value, count(*) AS n FROM records "
                        "WHERE site=ANY(%s) GROUP BY site ORDER BY n DESC;", [members])
            rows = [{"value": _category_of(r["value"], prefix), "count": r["n"]}
                    for r in cur.fetchall()]
            distinct = len(rows)
        else:
            extra = "AND data->>%s ILIKE %s" if search else ""
            sp = [col, f"%{search}%"] if search else []
            cur.execute(f"SELECT count(*) AS n FROM records WHERE site=ANY(%s) {extra};",
                        [members] + sp)
            total = cur.fetchone()["n"]
            cur.execute(f"SELECT count(DISTINCT data->>%s) AS d FROM records "
                        f"WHERE site=ANY(%s) {extra};", [col, members] + sp)
            distinct = cur.fetchone()["d"]
            cur.execute(
                f"""SELECT coalesce(data->>%s,'') AS value, count(*) AS n
                    FROM records WHERE site=ANY(%s) {extra}
                    GROUP BY data->>%s ORDER BY n DESC, value ASC LIMIT %s;""",
                [col, members] + sp + [col, min(int(top), 5000)])
            rows = [{"value": r["value"], "count": r["n"]} for r in cur.fetchall()]
    shown = sum(r["count"] for r in rows)
    return {"col": col, "total": total, "distinct": distinct,
            "rows": rows, "shown_distinct": len(rows),
            "hidden_distinct": max(0, distinct - len(rows)),
            "others": max(0, total - shown)}


def _cell(v):
    if v is None:
        return None
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    if isinstance(v, (dict,)):
        import json as _j
        return _j.dumps(v, ensure_ascii=False)
    return str(v)


def _san_col(name: str) -> str:
    import re as _re
    out = _re.sub(r"[^0-9a-zA-Z_]", "_", name.strip().lower()).strip("_")
    return out or "col"


def recategorize(site: str, col: str, old_value: str, new_value: str,
                 is_blank: bool = False) -> int:
    """Rename one value of a column across every matching row (merges into an
    existing value if they collide). Works across a merged group too."""
    members, _, _ = _resolve(site)
    if not members or col not in columns_for(members[0]):
        return 0
    with conn() as c:
        cur = c.cursor()
        if is_blank:
            cur.execute(
                "UPDATE records SET data=jsonb_set(data, ARRAY[%s], to_jsonb(%s::text)) "
                "WHERE site=ANY(%s) AND (data->>%s IS NULL OR data->>%s='');",
                (col, new_value, members, col, col))
        else:
            cur.execute(
                "UPDATE records SET data=jsonb_set(data, ARRAY[%s], to_jsonb(%s::text)) "
                "WHERE site=ANY(%s) AND data->>%s=%s;",
                (col, new_value, members, old_value))
        return cur.rowcount


def delete_category(site: str, col: str, value: str,
                    is_blank: bool = False) -> int:
    """Delete every row whose column equals the given value (group-aware)."""
    members, _, _ = _resolve(site)
    if not members or col not in columns_for(members[0]):
        return 0
    with conn() as c:
        cur = c.cursor()
        if is_blank:
            cur.execute("DELETE FROM records WHERE site=ANY(%s) AND "
                        "(data->>%s IS NULL OR data->>%s='');", (members, col, col))
        else:
            cur.execute("DELETE FROM records WHERE site=ANY(%s) AND data->>%s=%s;",
                        (members, col, value))
        return cur.rowcount


def _write_site_table(sq, site: str, table: str):
    """Stream one site's records into an open SQLite connection as `table`,
    using keyset pagination on id so memory stays flat for millions of rows."""
    fields = columns_for(site)
    if not fields:
        return 0
    cols, seen = [], {}
    for f in fields:                       # unique, sanitized column names
        b = _san_col(f)
        if b in seen:
            seen[b] += 1; b = f"{b}_{seen[b]}"
        else:
            seen[b] = 1
        cols.append(b)
    sq.execute(f'DROP TABLE IF EXISTS "{table}"')
    sq.execute(f'CREATE TABLE "{table}" ({", ".join(chr(34)+c+chr(34)+" TEXT" for c in cols)})')
    ins = f'INSERT INTO "{table}" VALUES ({", ".join("?" for _ in cols)})'
    last_id, n = 0, 0
    while True:
        rows = _rows("SELECT id, data FROM records WHERE site=%s AND id>%s "
                     "ORDER BY id LIMIT 10000;", (site, last_id))
        if not rows:
            break
        sq.executemany(ins, [[_cell((r["data"] or {}).get(f)) for f in fields]
                             for r in rows])
        sq.commit()
        last_id = rows[-1]["id"]
        n += len(rows)
    return n


def _merged_records(members, prefix, dedup_field=None):
    """Yield (category_str, data_dict) across a group. With dedup_field set,
    collapse to one record per non-empty key value and COMBINE the categories
    each book appears under (e.g. 'fiction, non_fiction'); records whose key is
    empty can't be deduped, so each is kept."""
    if not dedup_field:
        for site in members:
            cat, last = _category_of(site, prefix), 0
            while True:
                rows = _rows("SELECT id, data FROM records WHERE site=%s AND id>%s "
                             "ORDER BY id LIMIT 10000;", (site, last))
                if not rows:
                    break
                for r in rows:
                    yield cat, (r["data"] or {})
                last = rows[-1]["id"]
        return
    seen, order, nokey = {}, [], []
    for site in members:
        cat, last = _category_of(site, prefix), 0
        while True:
            rows = _rows("SELECT id, data FROM records WHERE site=%s AND id>%s "
                         "ORDER BY id LIMIT 10000;", (site, last))
            if not rows:
                break
            for r in rows:
                data = r["data"] or {}
                kv = str(data.get(dedup_field) or "").strip()
                if not kv:
                    nokey.append((cat, data))
                elif kv in seen:
                    seen[kv][0].add(cat)
                else:
                    seen[kv] = [{cat}, data]; order.append(kv)
            last = rows[-1]["id"]
    for kv in order:
        cats, data = seen[kv]
        yield ", ".join(sorted(cats)), data
    for cat, data in nokey:
        yield cat, data


def _write_merged_table(sq, members, prefix, table, dedup_field=None):
    """Merge several sites into ONE SQLite table with a leading `category`
    column. Optionally dedup to one row per `dedup_field` value."""
    if not members:
        return 0
    fields = columns_for(members[0])
    cols, seen = ["category"], {"category": 1}
    for f in fields:
        b = _san_col(f)
        if b in seen:
            seen[b] += 1; b = f"{b}_{seen[b]}"
        else:
            seen[b] = 1
        cols.append(b)
    sq.execute(f'DROP TABLE IF EXISTS "{table}"')
    sq.execute(f'CREATE TABLE "{table}" ({", ".join(chr(34)+c+chr(34)+" TEXT" for c in cols)})')
    ins = f'INSERT INTO "{table}" VALUES ({", ".join("?" for _ in cols)})'
    n, batch = 0, []
    for cat, data in _merged_records(members, prefix, dedup_field):
        batch.append([cat] + [_cell(data.get(f)) for f in fields])
        n += 1
        if len(batch) >= 10000:
            sq.executemany(ins, batch); sq.commit(); batch.clear()
    if batch:
        sq.executemany(ins, batch); sq.commit()
    return n


def export_db_file(site: str, path: str, dedup: str = "") -> int:
    import sqlite3
    members, grp, prefix = _resolve(site)
    sq = sqlite3.connect(path)
    try:
        if grp:
            return _write_merged_table(sq, members, prefix, _san_col(prefix),
                                       dedup or None)
        return _write_site_table(sq, site, _san_col(site))
    finally:
        sq.close()


def export_all_db_file(path: str) -> dict:
    """One SQLite file, one table per site."""
    import sqlite3
    sq = sqlite3.connect(path)
    counts = {}
    try:
        used = set()
        for s in sites_overview():
            t = _san_col(s["name"]); base, i = t, 2
            while t in used:
                t = f"{base}_{i}"; i += 1
            used.add(t)
            counts[s["name"]] = _write_site_table(sq, s["name"], t)
    finally:
        sq.close()
    return counts


def get_site(name: str) -> Optional[dict]:
    r = _rows("SELECT cfg FROM sites WHERE name=%s;", (name,))
    return r[0]["cfg"] if r else None


def enabled_sites() -> List[str]:
    return [r["name"] for r in
            _rows("SELECT name FROM sites WHERE enabled ORDER BY name;")]


def set_enabled(name: str, enabled: bool):
    with conn() as c:
        c.cursor().execute("UPDATE sites SET enabled=%s WHERE name=%s;",
                           (enabled, name))


def delete_site(name: str):
    # runs/records/field_stats cascade via their FKs
    with conn() as c:
        c.cursor().execute("DELETE FROM sites WHERE name=%s;", (name,))


def update_run_progress(run_id: int, pages: int, found: int, new: int):
    with conn() as c:
        c.cursor().execute(
            "UPDATE runs SET pages=%s, records_found=%s, records_new=%s "
            "WHERE id=%s;", (pages, found, new, run_id))
