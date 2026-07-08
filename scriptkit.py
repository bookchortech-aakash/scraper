"""scriptkit — the contract a custom script uses to land data in the dashboard.

A custom script does whatever it needs (requests, playwright, parsing, ...) to
build a list of dict rows, then calls scriptkit.save(...). That registers the
site and upserts the rows into the SAME `records` table the grid and analyze
pages read, so the data shows up there automatically — with counts, run
history, export, and analyze breakdowns, for free.

    import scriptkit
    rows = [{"title": "...", "price": 199.0, "isbn": "..."}]
    found, new = scriptkit.save("my_site", rows, key_fields=["isbn"])
    print(f"done: {found} found, {new} new")

Run from the dashboard's Scripts page, or directly:
    docker compose exec dashboard python scripts/<name>.py
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import db


def save(site: str,
         rows: Iterable[Dict[str, Any]],
         *,
         url: str = "",
         key_fields: Optional[List[str]] = None,
         batch_log: int = 0) -> tuple[int, int]:
    """Register `site` and upsert `rows` into the records table.

    site        the name this data appears under in the grid/analyze dropdown.
    rows        an iterable of flat dicts (one per record). Keys become columns.
    url         optional source url, stored on the site row.
    key_fields  fields used for dedup. Reruns won't double-write; matching rows
                just refresh. Default [] hashes the whole row (safe, no dedup
                collapse). Pass e.g. ["isbn"] to dedup on a stable id.
    batch_log   if > 0, print a progress line every N records.

    Returns (found, new).
    """
    db.init()
    # Satisfy the FK on records.site and make the site visible in the dashboard.
    db.upsert_site(site, url, "custom", {"fields": {}})
    run_id = db.start_run(site)
    found = new = 0
    used = "custom"
    try:
        for data in rows:
            if not isinstance(data, dict):
                raise TypeError(
                    f"each row must be a dict, got {type(data).__name__}")
            h = db.record_hash(site, data, key_fields or [])
            if db.upsert_record(site, run_id, h, data, {"_engine": used}):
                new += 1
            found += 1
            if batch_log and found % batch_log == 0:
                print(f"  ...{found} written ({new} new)", flush=True)
            # keep live progress visible on the dashboard's runs feed
            if found % 200 == 0:
                db.update_run_progress(run_id, 0, found, new)
        db.update_run_progress(run_id, 0, found, new)
        db.finish_run(run_id, "ok", used, 0, found, new)
        return found, new
    except Exception as e:
        db.finish_run(run_id, "error", used, 0, found, new, error=str(e)[:500])
        raise
