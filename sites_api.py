"""sites_api — a standalone site-config JSON editor for the dashboard.

Adds a /sites-editor page where you write/save/delete site config JSON files.
On save the JSON is validated through schema.from_dict, written to
./sites/<name>.json (bind-mounted, survives rebuilds), AND upserted into the DB
— so the site shows up in the main dashboard list and the data grid right away.

Self-contained: own APIRouter, own page. Wire it in with two lines in
dashboard.py, right after the scripts_api include:

    import sites_api
    app.include_router(sites_api.router)

SECURITY: gated by config.SCRIPTS_TOKEN (the same token as the scripts page). If
that is empty the feature is DISABLED and the routes refuse — fails safe so the
publicly-exposed dashboard can't become an open config-write endpoint.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

from fastapi import APIRouter, Body, Header
from fastapi.responses import HTMLResponse, JSONResponse

import config

router = APIRouter()

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SITES_DIR = getattr(config, "SITES_DIR", os.path.join(APP_DIR, "sites"))
TOKEN = getattr(config, "SCRIPTS_TOKEN", "")
SITE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

os.makedirs(SITES_DIR, exist_ok=True)


# ---- helpers -------------------------------------------------------------
def _denied(token: str) -> Optional[JSONResponse]:
    if not TOKEN:
        return JSONResponse(
            {"ok": False, "error": "Feature is disabled. Set SCRIPTS_TOKEN in "
             "the environment to enable it."}, status_code=403)
    if token != TOKEN:
        return JSONResponse({"ok": False, "error": "invalid token"},
                            status_code=401)
    return None


def _site_path(name: str) -> Optional[str]:
    name = (name or "").strip()
    if name.endswith(".json"):
        name = name[:-5]
    if not SITE_NAME_RE.match(name) or ".." in name:
        return None
    p = os.path.realpath(os.path.join(SITES_DIR, name + ".json"))
    if os.path.dirname(p) != os.path.realpath(SITES_DIR):
        return None
    return p


# ---- routes --------------------------------------------------------------
@router.get("/api/sitefiles")
def list_sitefiles(x_scripts_token: str = Header(default="")):
    d = _denied(x_scripts_token)
    if d:
        return d
    items = []
    for fn in sorted(os.listdir(SITES_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            stt = os.stat(os.path.join(SITES_DIR, fn))
        except OSError:
            continue
        items.append({"name": fn[:-5], "size": stt.st_size,
                      "mtime": int(stt.st_mtime)})
    return {"ok": True, "sites": items}


@router.get("/api/sitefile")
def read_sitefile(name: str, x_scripts_token: str = Header(default="")):
    d = _denied(x_scripts_token)
    if d:
        return d
    p = _site_path(name)
    if not p:
        return {"ok": False, "error": "bad name"}
    if not os.path.exists(p):
        return {"ok": False, "error": "not found"}
    with open(p, "r", encoding="utf-8") as f:
        return {"ok": True, "name": name, "json": f.read()}


@router.post("/api/sitefile")
def save_sitefile(payload: dict = Body(...),
                  x_scripts_token: str = Header(default="")):
    d = _denied(x_scripts_token)
    if d:
        return d
    text = (payload or {}).get("json", "")
    # 1) must be valid JSON
    try:
        cfg_dict = json.loads(text)
    except Exception as e:
        return {"ok": False, "error": f"invalid JSON: {e}"}
    if not isinstance(cfg_dict, dict):
        return {"ok": False, "error": "config must be a JSON object"}
    # 2) must be a valid site config (uses your real validator)
    try:
        import schema
        cfg = schema.from_dict(cfg_dict)
    except Exception as e:
        return {"ok": False, "error": f"config invalid: {e}"}
    # 3) filename = the config's own name, so file identity == DB identity
    p = _site_path(cfg.name)
    if not p:
        return {"ok": False, "error":
                "config 'name' must be letters/digits/._- (it becomes the file name)"}
    # 4) write the file (pretty) then register in the DB -> shows in the list
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg_dict, f, indent=2, ensure_ascii=False)
            f.write("\n")
        import db
        db.init()
        db.upsert_site(cfg.name, cfg.url, cfg.engine, cfg.raw)
    except Exception as e:
        return {"ok": False, "error": f"save/register failed: {e}"}
    return {"ok": True, "name": cfg.name}


@router.delete("/api/sitefile")
def delete_sitefile(name: str, x_scripts_token: str = Header(default="")):
    """Deletes only the JSON file. The site stays registered in the DB (with its
    records) — remove it from the main dashboard if you want it gone entirely."""
    d = _denied(x_scripts_token)
    if d:
        return d
    p = _site_path(name)
    if not p:
        return {"ok": False, "error": "bad name"}
    if os.path.exists(p):
        os.remove(p)
    return {"ok": True}


@router.get("/sites-editor", response_class=HTMLResponse)
def sites_editor_page():
    return SITES_PAGE


# ---- the page ------------------------------------------------------------
SITE_STARTER = """{
  "name": "example_site",
  "url": "https://books.toscrape.com/",
  "engine": "auto",
  "key_fields": ["url"],
  "list": { "container": "article.product_pod" },
  "next_page": "li.next a::attr(href)",
  "fields": {
    "title": { "selector": "h3 a", "attr": "title", "type": "string" },
    "price": { "selector": "p.price_color", "type": "number", "transform": "currency" },
    "url":   { "selector": "h3 a", "attr": "href", "type": "url" }
  }
}
"""

SITES_PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Site configs</title>
<style>
  :root{
    --bg:#0e1116; --panel:#161b22; --panel-2:#1c232d; --line:#2a3340;
    --ink:#e6edf3; --muted:#8b97a6; --dim:#5c6776;
    --live:#3fb950; --warn:#d29922; --bad:#f85149; --bar:#2f81f7;
    --mono:"SF Mono",ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:var(--mono);font-size:13px;line-height:1.5}
  a{color:var(--bar);text-decoration:none}
  .wrap{max-width:1180px;margin:0 auto;padding:24px 22px 80px}
  header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
    border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:18px}
  h1{font-size:15px;font-weight:600;letter-spacing:.04em;margin:0;text-transform:uppercase}
  .sub{color:var(--dim);font-size:12px}
  .btn{background:var(--panel-2);border:1px solid var(--line);color:var(--ink);
    border-radius:6px;cursor:pointer;padding:6px 11px;font-family:var(--mono);font-size:12px}
  .btn:hover{border-color:var(--bar)}
  .btn.danger:hover{border-color:var(--bad);color:var(--bad)}
  .btn.go{border-color:#1f3d27;color:var(--live)}
  .btn.go:hover{background:#13251a}
  .spacer{flex:1}
  .cols{display:grid;grid-template-columns:230px 1fr;gap:16px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
  .card h2{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
    margin:0 0 10px;display:flex;align-items:center;gap:8px}
  .slist{display:flex;flex-direction:column;gap:4px}
  .sitem{padding:7px 9px;border:1px solid var(--line);border-radius:6px;cursor:pointer;
    display:flex;align-items:center;gap:7px;background:var(--panel-2)}
  .sitem:hover{border-color:var(--bar)}
  .sitem.active{border-color:var(--bar);background:#15233b}
  .sitem .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--dim)}
  .empty{color:var(--dim);padding:6px 2px}
  .toolbar{display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
  textarea.editor{width:100%;height:440px;background:#0b0e13;border:1px solid var(--line);
    color:var(--ink);border-radius:8px;padding:12px;font-family:var(--mono);font-size:12.5px;
    line-height:1.55;resize:vertical;tab-size:4}
  .msg{font-size:12px} .msg.ok{color:var(--live)} .msg.err{color:var(--bad)}
  .tok{display:flex;align-items:center;gap:8px;margin-left:auto}
  .tok input{background:var(--panel-2);border:1px solid var(--line);color:var(--ink);
    border-radius:6px;padding:5px 8px;font-family:var(--mono);font-size:12px;width:150px}
  .note{color:var(--dim);font-size:11.5px;margin-top:8px}
  .name-tag{color:var(--muted);font-size:12px;margin-left:4px}
</style></head>
<body>
<div class="wrap">
  <header>
    <h1>Site configs</h1>
    <span class="sub">write · save JSON — registers in the dashboard list on save</span>
    <a class="btn" href="/">‹ dashboard</a>
    <a class="btn" href="/scripts">scripts ↗</a>
    <a class="btn" href="/data">data grid ↗</a>
    <span class="tok">
      <label class="sub">token</label>
      <input id="token" type="password" placeholder="SCRIPTS_TOKEN">
      <button class="btn" id="savetok">set</button>
    </span>
  </header>
  <div class="cols">
    <div class="card">
      <h2>sites/*.json <button class="btn" id="newbtn" style="margin-left:auto;padding:3px 9px">+ new</button></h2>
      <div class="slist" id="slist"><div class="empty">enter token…</div></div>
    </div>
    <div class="card">
      <div class="toolbar">
        <span class="sub">editing</span><span class="name-tag" id="curname">— new —</span>
        <button class="btn go" id="save">save &amp; register</button>
        <button class="btn danger" id="del">delete file</button>
        <span class="spacer"></span>
        <span class="msg" id="msg"></span>
      </div>
      <textarea id="json" class="editor" spellcheck="false"></textarea>
      <div class="note">On save the JSON is validated, written to <b>sites/&lt;name&gt;.json</b>, and registered in the DB —
        so it shows up in the main dashboard list and the data grid right away. The file name comes from the config's <b>"name"</b>.
        <br>Delete removes only the JSON file; the registered site and its records stay (remove those from the main dashboard).</div>
    </div>
  </div>
</div>
<script>
const $=s=>document.querySelector(s);
const STARTER=__SITE_STARTER__;
let token=sessionStorage.getItem('scrtok')||'';
let cur=null;
$('#token').value=token;
function hdr(){return token?{'X-Scripts-Token':token}:{};}
function msg(t,ok){const m=$('#msg');m.textContent=t;m.className='msg '+(ok?'ok':'err');
  if(t)setTimeout(()=>{if(m.textContent===t)m.textContent='';},5000);}
async function api(url,opt){opt=opt||{};opt.headers=Object.assign(hdr(),opt.headers||{});
  const r=await fetch(url,opt);let d={};try{d=await r.json();}catch(e){}
  if(d&&d.error){msg(d.error,false);}return d;}
async function loadList(){const d=await api('/api/sitefiles');const el=$('#slist');
  if(!d.ok){el.innerHTML='<div class="empty">'+(d.error||'enter token…')+'</div>';return;}
  if(!d.sites.length){el.innerHTML='<div class="empty">no site files yet — + new</div>';return;}
  el.innerHTML=d.sites.map(s=>'<div class="sitem'+(s.name===cur?' active':'')+'" data-n="'+s.name+'">'+
    '<span class="dot"></span><span class="nm" title="'+s.name+'">'+s.name+'</span></div>').join('');
  el.querySelectorAll('.sitem').forEach(it=>it.onclick=()=>openS(it.dataset.n));}
async function openS(name){const d=await api('/api/sitefile?name='+encodeURIComponent(name));if(!d.ok)return;
  cur=name;$('#curname').textContent=name+'.json';$('#json').value=d.json;loadList();}
function newSite(){cur=null;$('#curname').textContent='— new —';$('#json').value=STARTER;loadList();$('#json').focus();}
async function save(){const d=await api('/api/sitefile',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({json:$('#json').value})});
  if(d.ok){cur=d.name;$('#curname').textContent=d.name+'.json';msg('saved & registered as "'+d.name+'"',true);loadList();}}
async function del(){if(!cur){msg('nothing selected',false);return;}
  if(!confirm('Delete the file sites/'+cur+'.json? (the registered site/records stay)'))return;
  const d=await api('/api/sitefile?name='+encodeURIComponent(cur),{method:'DELETE'});
  if(d.ok){msg('file deleted',true);newSite();}}
$('#savetok').onclick=()=>{token=$('#token').value.trim();sessionStorage.setItem('scrtok',token);msg('token set',true);loadList();};
$('#newbtn').onclick=newSite;$('#save').onclick=save;$('#del').onclick=del;
$('#json').addEventListener('keydown',e=>{if(e.key==='Tab'){e.preventDefault();
  const t=e.target,s=t.selectionStart,en=t.selectionEnd;t.value=t.value.slice(0,s)+'  '+t.value.slice(en);
  t.selectionStart=t.selectionEnd=s+2;}});
loadList();
</script></body></html>
"""
SITES_PAGE = SITES_PAGE.replace("__SITE_STARTER__", json.dumps(SITE_STARTER))