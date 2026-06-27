"""scripts_api — a self-contained 'custom scripts' feature for the dashboard.

Adds a /scripts page where you can write, save, run, and delete custom Python
scripts that live on disk (bind-mounted, so they survive image rebuilds) and
run as subprocesses inside the dashboard container. Scripts use `scriptkit.save`
to write into the same records table the grid/analyze pages read.

Wire it into the dashboard with two lines (see the README at the bottom):
    import scripts_api
    app.include_router(scripts_api.router)

SECURITY: every /api/script* route is gated by config.SCRIPTS_TOKEN. If that is
empty the whole feature is DISABLED and the routes refuse — this fails safe so
the publicly-exposed dashboard can never become an open remote-code-execution
endpoint. Set SCRIPTS_TOKEN in the environment to enable it, then paste the same
value into the token box on the page.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from typing import Optional

from fastapi import APIRouter, Body, Header
from fastapi.responses import HTMLResponse, JSONResponse

import config

router = APIRouter()

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = getattr(config, "SCRIPTS_DIR", os.path.join(APP_DIR, "scripts"))
TOKEN = getattr(config, "SCRIPTS_TOKEN", "")
NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
MAX_OUTPUT_LINES = 2000

os.makedirs(SCRIPTS_DIR, exist_ok=True)

# name -> {"proc": Popen, "out": [str], "started": float}
_procs: dict = {}
_lock = threading.Lock()


# ---- helpers -------------------------------------------------------------
def _denied(token: str) -> Optional[JSONResponse]:
    """Return an error response if the token is missing/disabled/wrong, else None."""
    if not TOKEN:
        return JSONResponse(
            {"ok": False, "error": "Scripts feature is disabled. Set "
             "SCRIPTS_TOKEN in the environment to enable it."},
            status_code=403)
    if token != TOKEN:
        return JSONResponse({"ok": False, "error": "invalid token"},
                            status_code=401)
    return None


def _path(name: str) -> Optional[str]:
    """Resolve a script name to a safe path inside SCRIPTS_DIR, or None."""
    name = (name or "").strip()
    if not NAME_RE.match(name):
        return None
    p = os.path.realpath(os.path.join(SCRIPTS_DIR, name + ".py"))
    if os.path.dirname(p) != os.path.realpath(SCRIPTS_DIR):
        return None
    return p


def _reader(name: str, proc: subprocess.Popen):
    for line in proc.stdout:                       # type: ignore[union-attr]
        with _lock:
            rec = _procs.get(name)
            if rec is None:
                break
            rec["out"].append(line.rstrip("\n"))
            extra = len(rec["out"]) - MAX_OUTPUT_LINES
            if extra > 0:
                del rec["out"][:extra]
    rc = proc.wait()
    with _lock:
        rec = _procs.get(name)
        if rec is not None:
            rec["out"].append(f"[exited with code {rc}]")


# ---- routes --------------------------------------------------------------
@router.get("/api/scripts")
def list_scripts(x_scripts_token: str = Header(default="")):
    d = _denied(x_scripts_token)
    if d:
        return d
    items = []
    for fn in sorted(os.listdir(SCRIPTS_DIR)):
        if not fn.endswith(".py"):
            continue
        full = os.path.join(SCRIPTS_DIR, fn)
        try:
            stt = os.stat(full)
        except OSError:
            continue
        name = fn[:-3]
        with _lock:
            rec = _procs.get(name)
            running = bool(rec and rec["proc"].poll() is None)
        items.append({"name": name, "size": stt.st_size,
                      "mtime": int(stt.st_mtime), "running": running})
    return {"ok": True, "scripts": items}


@router.get("/api/script")
def read_script(name: str, x_scripts_token: str = Header(default="")):
    d = _denied(x_scripts_token)
    if d:
        return d
    p = _path(name)
    if not p:
        return {"ok": False, "error": "bad name"}
    if not os.path.exists(p):
        return {"ok": False, "error": "not found"}
    with open(p, "r", encoding="utf-8") as f:
        return {"ok": True, "name": name, "code": f.read()}


@router.post("/api/script")
def save_script(payload: dict = Body(...),
                x_scripts_token: str = Header(default="")):
    d = _denied(x_scripts_token)
    if d:
        return d
    name = (payload or {}).get("name", "")
    code = (payload or {}).get("code", "")
    p = _path(name)
    if not p:
        return {"ok": False, "error": "name must be letters, digits, underscore"}
    with open(p, "w", encoding="utf-8") as f:
        f.write(code)
    return {"ok": True, "name": name}


@router.delete("/api/script")
def delete_script(name: str, x_scripts_token: str = Header(default="")):
    d = _denied(x_scripts_token)
    if d:
        return d
    p = _path(name)
    if not p:
        return {"ok": False, "error": "bad name"}
    with _lock:
        rec = _procs.get(name)
        if rec and rec["proc"].poll() is None:
            return {"ok": False, "error": "stop it before deleting"}
        _procs.pop(name, None)
    if os.path.exists(p):
        os.remove(p)
    return {"ok": True}


@router.post("/api/script/run")
def run_script(payload: dict = Body(...),
               x_scripts_token: str = Header(default="")):
    d = _denied(x_scripts_token)
    if d:
        return d
    name = (payload or {}).get("name", "")
    p = _path(name)
    if not p or not os.path.exists(p):
        return {"ok": False, "error": "not found"}
    with _lock:
        rec = _procs.get(name)
        if rec and rec["proc"].poll() is None:
            return {"ok": False, "error": "already running"}
        env = dict(os.environ)
        # so the subprocess can `import scriptkit`, `import db`, etc.
        env["PYTHONPATH"] = APP_DIR + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [sys.executable, p],
            cwd=APP_DIR, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        _procs[name] = {"proc": proc, "out": [f"$ python scripts/{name}.py"],
                        "started": time.time()}
    threading.Thread(target=_reader, args=(name, proc), daemon=True).start()
    return {"ok": True}


@router.get("/api/script/output")
def script_output(name: str, x_scripts_token: str = Header(default="")):
    d = _denied(x_scripts_token)
    if d:
        return d
    with _lock:
        rec = _procs.get(name)
        if rec is None:
            return {"ok": True, "running": False, "output": "", "started": 0}
        running = rec["proc"].poll() is None
        return {"ok": True, "running": running,
                "output": "\n".join(rec["out"]), "started": rec["started"]}


@router.post("/api/script/stop")
def stop_script(payload: dict = Body(...),
                x_scripts_token: str = Header(default="")):
    d = _denied(x_scripts_token)
    if d:
        return d
    name = (payload or {}).get("name", "")
    with _lock:
        rec = _procs.get(name)
        if not rec or rec["proc"].poll() is not None:
            return {"ok": False, "error": "not running"}
        proc = rec["proc"]
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    return {"ok": True}


@router.get("/scripts", response_class=HTMLResponse)
def scripts_page():
    return SCRIPTS_PAGE


# ---- the page ------------------------------------------------------------
STARTER = """import scriptkit

# Do whatever you need to build a list of dict rows: requests, playwright,
# parsing, multiple pages... each dict is one record, keys become columns.
rows = [
    {"title": "Example Book", "price": 199.0, "isbn": "0000000000"},
]

# Registers the site and writes the rows into the dashboard's records table.
# After it runs, open the Data grid and pick this site name.
found, new = scriptkit.save("my_custom_site", rows, key_fields=["isbn"])
print(f"done: {found} found, {new} new")
"""

SCRIPTS_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Custom scripts</title>
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
  .dot.on{background:var(--live);box-shadow:0 0 6px var(--live)}
  .empty{color:var(--dim);padding:6px 2px}
  .toolbar{display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
  input.nm{background:var(--panel-2);border:1px solid var(--line);color:var(--ink);
    border-radius:6px;padding:6px 9px;font-family:var(--mono);font-size:13px;min-width:200px}
  textarea#code{width:100%;height:430px;background:#0b0e13;border:1px solid var(--line);
    color:var(--ink);border-radius:8px;padding:12px;font-family:var(--mono);font-size:12.5px;
    line-height:1.55;resize:vertical;tab-size:4}
  .out{margin-top:12px;background:#0b0e13;border:1px solid var(--line);border-radius:8px;
    padding:10px 12px;white-space:pre-wrap;font-size:12px;color:#cdd6e0;min-height:60px;
    max-height:260px;overflow:auto}
  .msg{font-size:12px} .msg.ok{color:var(--live)} .msg.err{color:var(--bad)}
  .tok{display:flex;align-items:center;gap:8px;margin-left:auto}
  .tok input{background:var(--panel-2);border:1px solid var(--line);color:var(--ink);
    border-radius:6px;padding:5px 8px;font-family:var(--mono);font-size:12px;width:150px}
  .note{color:var(--dim);font-size:11.5px;margin-top:8px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Custom scripts</h1>
    <span class="sub">write · save · run — output lands in the data grid</span>
    <a class="btn" href="/">‹ dashboard</a>
    <a class="btn" href="/data">data grid ↗</a>
    <span class="tok">
      <label class="sub">token</label>
      <input id="token" type="password" placeholder="SCRIPTS_TOKEN">
      <button class="btn" id="savetok">set</button>
    </span>
  </header>

  <div class="cols">
    <div class="card">
      <h2>Scripts <button class="btn" id="newbtn" style="margin-left:auto;padding:3px 9px">+ new</button></h2>
      <div class="slist" id="slist"><div class="empty">enter token…</div></div>
    </div>

    <div class="card">
      <div class="toolbar">
        <input class="nm" id="name" placeholder="script_name (letters, digits, _)">
        <button class="btn" id="save">save</button>
        <button class="btn go" id="run">▶ run</button>
        <button class="btn danger" id="stop">■ stop</button>
        <button class="btn danger" id="del">delete</button>
        <span class="spacer"></span>
        <span class="msg" id="msg"></span>
      </div>
      <textarea id="code" spellcheck="false"></textarea>
      <div class="note">Scripts call <b>scriptkit.save(site, rows, key_fields=[...])</b> to write into the dashboard.
        They run inside the dashboard container with full DB access.</div>
      <div class="out" id="out"></div>
    </div>
  </div>
</div>

<script>
const $=s=>document.querySelector(s);
const STARTER=__STARTER__;
let token=sessionStorage.getItem('scrtok')||'';
let cur=null, poll=null;
$('#token').value=token;

function hdr(){return token?{'X-Scripts-Token':token}:{};}
function msg(t,ok){const m=$('#msg');m.textContent=t;m.className='msg '+(ok?'ok':'err');
  if(t)setTimeout(()=>{if(m.textContent===t)m.textContent='';},4000);}

async function api(url,opt){
  opt=opt||{}; opt.headers=Object.assign(hdr(),opt.headers||{});
  const r=await fetch(url,opt);
  let d={}; try{d=await r.json();}catch(e){}
  if(d&&d.error){msg(d.error,false);}
  return d;
}

async function loadList(){
  const d=await api('/api/scripts');
  const el=$('#slist');
  if(!d.ok){el.innerHTML='<div class="empty">'+(d.error||'enter token…')+'</div>';return;}
  if(!d.scripts.length){el.innerHTML='<div class="empty">no scripts yet — + new</div>';return;}
  el.innerHTML=d.scripts.map(s=>
    '<div class="sitem'+(s.name===cur?' active':'')+'" data-n="'+s.name+'">'+
    '<span class="dot'+(s.running?' on':'')+'"></span>'+
    '<span class="nm" title="'+s.name+'">'+s.name+'</span></div>').join('');
  el.querySelectorAll('.sitem').forEach(it=>it.onclick=()=>open(it.dataset.n));
}

async function open(name){
  const d=await api('/api/script?name='+encodeURIComponent(name));
  if(!d.ok)return;
  cur=name; $('#name').value=name; $('#code').value=d.code;
  $('#out').textContent=''; loadList(); startPoll();
}

function newScript(){
  cur=null; $('#name').value=''; $('#code').value=STARTER; $('#out').textContent='';
  stopPoll(); loadList(); $('#name').focus();
}

async function save(){
  const name=$('#name').value.trim();
  if(!name){msg('name required',false);return;}
  const d=await api('/api/script',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name,code:$('#code').value})});
  if(d.ok){cur=name;msg('saved',true);loadList();}
}

async function run(){
  await save();
  if(!cur)return;
  const d=await api('/api/script/run',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:cur})});
  if(d.ok){msg('running…',true);startPoll();}
}

async function stop(){
  if(!cur)return;
  await api('/api/script/stop',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:cur})});
}

async function del(){
  if(!cur){msg('nothing selected',false);return;}
  if(!confirm('Delete script "'+cur+'"?'))return;
  const d=await api('/api/script?name='+encodeURIComponent(cur),{method:'DELETE'});
  if(d.ok){msg('deleted',true);newScript();}
}

function startPoll(){stopPoll();if(!cur)return;poll=setInterval(refreshOut,1200);refreshOut();}
function stopPoll(){if(poll){clearInterval(poll);poll=null;}}
async function refreshOut(){
  if(!cur){stopPoll();return;}
  const d=await api('/api/script/output?name='+encodeURIComponent(cur));
  if(!d.ok)return;
  const o=$('#out'); const atBottom=o.scrollHeight-o.scrollTop-o.clientHeight<40;
  o.textContent=d.output||''; if(atBottom)o.scrollTop=o.scrollHeight;
  if(!d.running){stopPoll();loadList();}
}

$('#savetok').onclick=()=>{token=$('#token').value.trim();
  sessionStorage.setItem('scrtok',token);msg('token set',true);loadList();};
$('#newbtn').onclick=newScript;
$('#save').onclick=save;
$('#run').onclick=run;
$('#stop').onclick=stop;
$('#del').onclick=del;
$('#code').addEventListener('keydown',e=>{
  if(e.key==='Tab'){e.preventDefault();
    const t=e.target,s=t.selectionStart,en=t.selectionEnd;
    t.value=t.value.slice(0,s)+'    '+t.value.slice(en);
    t.selectionStart=t.selectionEnd=s+4;}
});
loadList();
</script>
</body>
</html>
"""
SCRIPTS_PAGE = SCRIPTS_PAGE.replace("__STARTER__", __import__("json").dumps(STARTER))
