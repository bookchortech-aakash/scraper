"""dedupe_api — 'remove duplicate titles' for the data-grid page.

Adds DELETE /api/dedupe?site=<site>&field=title. For the given site (or a
merged 'group:<prefix>' view) it keeps ONE record per distinct, non-blank value
of `field` (default 'title') and permanently deletes the rest. The kept copy is
the earliest one (lowest id / first seen). Records whose field is missing or
blank are left untouched, so title-less rows are never collapsed together.

Standalone: own APIRouter, uses db.conn()/db._resolve(). Wire it in dashboard.py
(next to the other includes):
    import dedupe_api
    app.include_router(dedupe_api.router)

Note: this matches the existing data-grid delete buttons (delete selected /
clear site / clear all), which are also un-gated — so no token is required here.
"""
from __future__ import annotations

from fastapi import APIRouter

import db

router = APIRouter()

# Keep min(id) per trimmed, non-blank field value; delete the other duplicates.
_SQL = """
DELETE FROM records
WHERE site = ANY(%(sites)s)
  AND nullif(btrim(data->>%(f)s), '') IS NOT NULL
  AND id NOT IN (
    SELECT min(id) FROM records
    WHERE site = ANY(%(sites)s)
      AND nullif(btrim(data->>%(f)s), '') IS NOT NULL
    GROUP BY btrim(data->>%(f)s)
  );
"""


@router.delete("/api/dedupe")
def dedupe(site: str, field: str = "title"):
    field = (field or "title").strip() or "title"
    members, _is_group, _prefix = db._resolve(site)
    if not members:
        return {"ok": False, "error": "unknown site"}
    with db.conn() as c:
        cur = c.cursor()
        cur.execute(_SQL, {"sites": members, "f": field})
        deleted = cur.rowcount
    return {"ok": True, "deleted": deleted, "field": field}
