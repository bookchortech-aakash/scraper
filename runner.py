#!/usr/bin/env python3
"""CLI + shared run logic.

The database is the source of truth for site configs now (the dashboard edits
them there). `register` seeds the DB from sites/*.json; everything else reads
from the DB, falling back to a file only if a name isn't registered yet.

  python runner.py register                 # seed DB from sites/*.json
  python runner.py probe books_toscrape      # one fetch; HIT/MISS per field
  python runner.py run   books_toscrape      # full run -> Postgres
  python runner.py run   --all               # every enabled site (cron this)

execute_run() and probe_config() are imported by the dashboard so the browser
and the CLI run the exact same code path.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Callable, Optional

import config
import db
import engine
import schema
from schema import SiteConfig


# ---- config loading: DB first, file fallback -----------------------------
def load_site(name: str) -> SiteConfig:
    cfg_dict = db.get_site(name)
    if cfg_dict:
        return schema.from_dict(cfg_dict)
    files = schema.load_all(config.SITES_DIR)
    if name in files:
        return files[name]
    sys.exit(f"unknown site {name!r}. Registered: "
             f"{', '.join(db.enabled_sites()) or '(none)'}")


# ---- the one run path, shared by CLI and dashboard -----------------------
def execute_run(cfg: SiteConfig, run_id: Optional[int] = None,
                log: Callable[[str], None] = lambda *_: None,
                should_stop: Callable[[], bool] = lambda: False):
    """Stream a run into Postgres: insert records per page, bump live progress,
    save field stats, finish. Checks should_stop() between pages so a run can
    be cancelled from the dashboard. Returns (run_id, found, new)."""
    db.init()
    db.upsert_site(cfg.name, cfg.url, cfg.engine, cfg.raw)
    if run_id is None:
        run_id = db.start_run(cfg.name)

    found = new = pages = 0
    hits: dict = {}
    used = ""
    stopped = False
    try:
        for batch in engine.iter_pages(cfg):
            used = batch.engine_used or used
            for data in batch.records:
                h = db.record_hash(cfg.name, data, cfg.key_fields)
                prov = {"_source_url": cfg.url, "_engine": used}
                if db.upsert_record(cfg.name, run_id, h, data, prov):
                    new += 1
            for f, c in batch.hit_counts.items():
                hits[f] = hits.get(f, 0) + c
            found += batch.n
            pages += 1
            db.update_run_progress(run_id, pages, found, new)
            log(f"    page {pages}: {batch.n} rows ({found} total, {new} new)")
            if should_stop():
                stopped = True
                log("  stop requested — finishing cleanly")
                break
        db.save_field_stats(run_id, found, hits)
        db.finish_run(run_id, "partial" if stopped else "ok",
                      used, pages, found, new)
        return run_id, found, new
    except Exception as e:
        db.finish_run(run_id, "error", used, pages, found, new, error=str(e)[:500])
        log(f"  ! {cfg.name} failed: {e}")
        raise


def probe_config(cfg: SiteConfig) -> dict:
    """One-page fetch; report per-field HIT/MISS. Does not write to the DB."""
    orig = config.MAX_PAGES_GUARD
    config.MAX_PAGES_GUARD = 1
    try:
        res = engine.scrape(cfg)
    finally:
        config.MAX_PAGES_GUARD = orig
    sample = res.records[0] if res.records else {}
    fields = [{"field": k, "value": sample.get(k),
               "hit": sample.get(k) not in (None, "", [])}
              for k in cfg.field_names]
    return {"engine_used": res.engine_used or cfg.engine,
            "total": res.total_records, "fields": fields, "sample": sample}


# ---- CLI -----------------------------------------------------------------
def cmd_register(args):
    db.init()
    sites = schema.load_all(config.SITES_DIR)
    if not sites:
        sys.exit(f"no configs found in {config.SITES_DIR}")
    for name, cfg in sites.items():
        db.upsert_site(name, cfg.url, cfg.engine, cfg.raw)
        print(f"  registered {name}  ({cfg.engine}, {len(cfg.fields)} fields)")
    print(f"{len(sites)} site(s) registered.")


def cmd_probe(args):
    cfg = load_site(args.site)
    r = probe_config(cfg)
    print(f"site   : {cfg.name}\nengine : {r['engine_used']}\n"
          f"records: {r['total']} on first page\n")
    if not r["fields"]:
        print("No fields configured.")
        return
    width = max(len(f["field"]) for f in r["fields"])
    for f in r["fields"]:
        v = f["value"]
        shown = v if not isinstance(v, list) else f"[{len(v)} items] {v[:2]}"
        print(f"  [{'HIT ' if f['hit'] else 'MISS'}] {f['field']:<{width}}  {shown}")
    misses = [f["field"] for f in r["fields"] if not f["hit"]]
    print(("\n%d field(s) missed: %s — fix before a full run."
           % (len(misses), ", ".join(misses))) if misses
          else "\nAll fields hit. Safe to run.")


def cmd_run(args):
    if args.all:
        names = db.enabled_sites()
        if not names:
            sys.exit("no registered sites — run `register` or add one in the dashboard")
        print(f"Running {len(names)} site(s). "
              f"Delay {config.DEFAULT_MIN_DELAY:g}-{config.DEFAULT_MAX_DELAY:g}s.\n")
        for name in names:
            cfg = load_site(name)
            print(f"  run {cfg.name} ...", flush=True)
            t0 = time.time()
            try:
                _, found, new = execute_run(cfg, log=print)
                print(f"  done {cfg.name}: {found} found, {new} new, {time.time()-t0:.1f}s")
            except Exception:
                pass
    elif args.site:
        cfg = load_site(args.site)
        t0 = time.time()
        _, found, new = execute_run(cfg, log=print)
        print(f"  done {cfg.name}: {found} found, {new} new, {time.time()-t0:.1f}s")
    else:
        sys.exit("pass a site name, or --all")


def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("register", help="seed the DB from sites/*.json")
    rp.set_defaults(func=cmd_register)
    pp = sub.add_parser("probe", help="one fetch; validate a config's selectors")
    pp.add_argument("site")
    pp.set_defaults(func=cmd_probe)
    xp = sub.add_parser("run", help="full run into Postgres")
    xp.add_argument("site", nargs="?", default=None)
    xp.add_argument("--all", action="store_true", help="run every enabled site")
    xp.set_defaults(func=cmd_run)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
