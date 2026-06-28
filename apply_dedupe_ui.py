#!/usr/bin/env python3
"""Apply the 'remove duplicate titles' UI to dashboard.py — safely.

Makes three edits to the data-grid page:
  1. include dedupe_api's router
  2. add a 'remove duplicate titles' button next to 'delete selected'
  3. add its click handler

It refuses to change anything unless every anchor is found EXACTLY once, so it
can't silently corrupt the file. A timestamped backup is written first.

Run from ~/scraper:   python3 apply_dedupe_ui.py
"""
import shutil
import sys
import time

PATH = "dashboard.py"

EDITS = [
    # ---- 1) wire in the router (anchored on the scripts_api include) -------
    (
        "include router",
        "app.include_router(scripts_api.router)",
        "app.include_router(scripts_api.router)\n"
        "import dedupe_api\n"
        "app.include_router(dedupe_api.router)",
    ),
    # ---- 2) the button (anchored on the data-grid delete-selected button) --
    (
        "button",
        '<button class="btn danger" id="delsel">delete selected</button>',
        '<button class="btn danger" id="delsel">delete selected</button>\n'
        '    <button class="btn danger" id="dedupe">remove duplicate titles</button>',
    ),
    # ---- 3) the click handler (anchored on the delsel handler tail) --------
    (
        "handler",
        "  const d=await (await fetch(`/api/records?site=${encodeURIComponent(st.site)}"
        "&ids=${ids.join(',')}`,{method:'DELETE'})).json();\n"
        "  toast(`deleted ${d.deleted}`); loadGrid();\n"
        "};",
        "  const d=await (await fetch(`/api/records?site=${encodeURIComponent(st.site)}"
        "&ids=${ids.join(',')}`,{method:'DELETE'})).json();\n"
        "  toast(`deleted ${d.deleted}`); loadGrid();\n"
        "};\n"
        "$('#dedupe').onclick=async()=>{\n"
        "  if(!st.site){toast('no site');return;}\n"
        "  if(!confirm(`Remove duplicate \"title\" rows from ${st.site}? "
        "One copy of each title is kept; the rest are permanently deleted.`))return;\n"
        "  const d=await (await fetch(`/api/dedupe?site=${encodeURIComponent(st.site)}"
        "&field=title`,{method:'DELETE'})).json();\n"
        "  if(d.error){toast(d.error);return;}\n"
        "  toast(`removed ${d.deleted} duplicate(s)`); loadGrid();\n"
        "};",
    ),
]


def main():
    with open(PATH, encoding="utf-8") as f:
        src = f.read()

    # already applied? refuse, so re-running can't create duplicates.
    if "dedupe_api" in src:
        print("!! dedupe_api is already wired into dashboard.py — nothing to do. "
              "(Remove the existing lines if you want to re-apply.)")
        sys.exit(1)

    # verify every anchor is present exactly once before touching anything
    for label, old, _new in EDITS:
        n = src.count(old)
        if n != 1:
            print(f"!! {label}: anchor found {n} time(s), expected 1 — aborting, "
                  f"no changes made.")
            print("   (paste me this message and I'll adjust the anchor.)")
            sys.exit(1)

    bak = f"{PATH}.bak.{int(time.time())}"
    shutil.copy2(PATH, bak)

    for _label, old, new in EDITS:
        src = src.replace(old, new, 1)

    with open(PATH, "w", encoding="utf-8") as f:
        f.write(src)

    print(f"all 3 edits applied. backup: {bak}")
    print("now: python3 -m py_compile dashboard.py && echo OK")


if __name__ == "__main__":
    main()
