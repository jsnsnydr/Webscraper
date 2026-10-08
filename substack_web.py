#!/usr/bin/env python3
"""
substack_web.py - local web UI for substack_tool.py (keep both files in one folder).

  python substack_web.py            # then open http://127.0.0.1:8000
  python substack_web.py --port 9000

Fetched articles are saved to articles.json next to where you run it and
reloaded on the next start. Filtering, sorting and export happen in the browser.
"""
import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import substack_tool as st

DATA_FILE = "articles.json"
state = {"articles": [], "running": False, "msg": "Idle"}
lock = threading.Lock()


def save():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state["articles"], f, ensure_ascii=False)


def merge(recs):
    """Add/update articles by URL, never discarding a body we already downloaded."""
    with lock:
        by_url = {a["url"]: a for a in state["articles"]}
        for r in recs:
            old = by_url.get(r["url"])
            if old and old.get("body_text") and not r.get("body_text"):
                r["body_text"] = old["body_text"]
            by_url[r["url"]] = r
        state["articles"] = list(by_url.values())
        save()


def download_bodies(todo):
    for i in range(0, len(todo), 20):
        state["msg"] = f"Downloading full text {i}/{len(todo)}..."
        try:
            st.fetch_bodies(todo[i:i + 20], 0.5)
        finally:
            with lock:
                save()


def run_fetch(cfg):
    try:
        t0 = time.perf_counter()
        pubs = [p.strip() for p in cfg.get("publications", []) if p.strip()]
        since, until = st.parse_date(cfg.get("since")), st.parse_date(cfg.get("until"))
        if until and len(cfg["until"]) == 10:
            until = until.replace(hour=23, minute=59, second=59)
        limit = int(cfg["limit"]) if cfg.get("limit") else None
        errors = []
        for i, pub in enumerate(pubs, 1):
            got = []

            def on_batch(recs, pub=pub, i=i, got=got):
                got.extend(recs)
                merge(recs)
                state["msg"] = f"Fetching {pub} ({i}/{len(pubs)}): {len(got)} posts so far..."

            try:
                state["msg"] = f"Fetching {pub} ({i}/{len(pubs)})..."
                st.fetch_publication(pub, limit, since, until, False, 0.5,
                                     cfg.get("search", ""), on_batch)
                if cfg.get("full_text"):
                    download_bodies([r for r in got if not r.get("body_text")])
            except Exception as e:
                errors.append(f"{pub}: {e} (kept {len(got)} posts; run again to resume)")
        state["msg"] = f"Done in {time.perf_counter() - t0:.0f}s" + (" - errors: " + "; ".join(errors) if errors else "")
    finally:
        state["running"] = False


def run_bodies(urls):
    t0 = time.perf_counter()
    want = set(urls)
    with lock:
        todo = [a for a in state["articles"] if a["url"] in want and not a.get("body_text")]
    try:
        download_bodies(todo)
        state["msg"] = f"Done in {time.perf_counter() - t0:.0f}s - downloaded text for {len(todo)} articles"
    except Exception as e:
        done = sum(1 for a in todo if a.get("body_text"))
        state["msg"] = f"Stopped: {e} ({done}/{len(todo)} done; click again to resume)"
    finally:
        state["running"] = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json", code=200):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self.send(PAGE, "text/html")
        elif self.path == "/api/articles":
            self.send(json.dumps(state["articles"]))
        elif self.path == "/api/status":
            self.send(json.dumps({"running": state["running"], "msg": state["msg"],
                                  "count": len(state["articles"])}))
        else:
            self.send("{}", code=404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/fetch":
            if state["running"]:
                return self.send('{"error":"A fetch is already running"}', code=409)
            state["running"] = True
            threading.Thread(target=run_fetch, args=(body,), daemon=True).start()
            self.send("{}")
        elif self.path == "/api/bodies":
            if state["running"]:
                return self.send('{"error":"A fetch is already running"}', code=409)
            state["running"] = True
            threading.Thread(target=run_bodies, args=(body.get("urls", []),), daemon=True).start()
            self.send("{}")
        elif self.path == "/api/clear":
            with lock:
                state["articles"] = []
                save()
            self.send("{}")
        else:
            self.send("{}", code=404)


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Substack Collector</title>
<style>
:root{--bg:#fafaf7;--card:#fff;--fg:#1d1d1b;--mut:#6b6b66;--bd:#e2e1da;--ac:#ff6719}
@media(prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--fg:#ececE8;--mut:#9a9a93;--bd:#33332f}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:14px 20px;border-bottom:1px solid var(--bd);font-weight:600;font-size:16px}
header b{color:var(--ac)}main{max-width:1200px;margin:0 auto;padding:16px 20px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:14px;margin-bottom:14px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:0 0 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
label{display:block;font-size:12px;color:var(--mut);margin-bottom:3px}
input,select,textarea{width:100%;padding:6px 8px;border:1px solid var(--bd);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
textarea{resize:vertical;min-height:54px}
button{padding:7px 14px;border:1px solid var(--bd);border-radius:6px;background:var(--card);color:var(--fg);cursor:pointer;font:inherit}
button.p{background:var(--ac);border-color:var(--ac);color:#fff}button:disabled{opacity:.5}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px}.sp{flex:1}
.chk{display:flex;align-items:center;gap:6px;color:var(--fg);font-size:13px;margin:0}.chk input{width:auto}
.mut{color:var(--mut)}table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px;border-bottom:1px solid var(--bd);vertical-align:top}
th{font-size:12px;color:var(--mut);cursor:pointer;white-space:nowrap}td.n{white-space:nowrap}
a{color:var(--ac);text-decoration:none}a:hover{text-decoration:underline}
.tag{font-size:11px;padding:1px 6px;border-radius:9px;border:1px solid var(--bd);color:var(--mut)}
.tag.paid{color:var(--ac);border-color:var(--ac)}.sub{color:var(--mut);font-size:12px}
.wrap{overflow-x:auto}
</style></head><body>
<header><b>Substack</b> Collector</header>
<main>
<div class="card"><h2>1 · Fetch articles</h2>
<label>Publications (one per line or comma-separated: subdomain, domain or URL)</label>
<textarea id="pubs" placeholder="noahpinion&#10;astralcodexten"></textarea>
<div class="grid" style="margin-top:10px">
<div><label>Max posts per publication</label><input id="limit" type="number" min="1" placeholder="all"></div>
<div><label>Since</label><input id="since" type="date"></div>
<div><label>Until</label><input id="until" type="date"></div>
<div><label>Search term (optional, applied by Substack)</label><input id="ssearch"></div></div>
<div class="row"><label class="chk"><input id="full" type="checkbox"> Download full text (slower; enables body search)</label>
<span class="sp"></span><span id="status" class="mut"></span>
<button class="p" id="go">Fetch</button><button id="clear">Clear all</button></div></div>

<div class="card"><h2>2 · Filter</h2><div class="grid">
<div><label>Anywhere (title, subtitle, body)</label><input id="q"></div>
<div><label>Title contains</label><input id="title"></div>
<div><label>Body contains</label><input id="text"></div>
<div><label>Author</label><input id="author"></div>
<div><label>Publication</label><input id="pub"></div>
<div><label>After</label><input id="after" type="date"></div>
<div><label>Before</label><input id="before" type="date"></div>
<div><label>Access</label><select id="access"><option value="">All</option><option value="everyone">Free</option><option value="only_paid">Paid</option></select></div>
<div><label>Min words</label><input id="minw" type="number" min="0"></div>
<div><label>Min likes</label><input id="minl" type="number" min="0"></div>
<div><label>Sort by</label><select id="sort"><option value="date">Date</option><option value="likes">Likes</option><option value="word_count">Words</option><option value="title">Title</option></select></div>
<div><label>Order</label><select id="dir"><option value="-1">Descending</option><option value="1">Ascending</option></select></div></div>
<div class="row"><label class="chk"><input id="rx" type="checkbox"> Regex</label>
<label class="chk"><input id="all" type="checkbox"> Require all words</label>
<span class="sp"></span><button id="reset">Reset filters</button></div></div>

<div class="card"><div class="row" style="margin:0 0 10px"><h2 style="margin:0" id="count"></h2><span class="sp"></span>
<button id="gb">Download full text</button><button id="ecsv">Export CSV</button><button id="ejson">Export JSON</button></div>
<div class="wrap"><table><thead><tr><th data-s="date">Date</th><th data-s="title">Title</th><th data-s="authors">Author</th>
<th data-s="publication">Publication</th><th data-s="word_count">Words</th><th data-s="likes">Likes</th></tr></thead>
<tbody id="rows"></tbody></table></div></div></main>
<script>
const $=id=>document.getElementById(id);let ALL=[],VIEW=[];
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const FIL=["q","title","text","author","pub","after","before","access","minw","minl","sort","dir","rx","all"];
function matcher(v){v=v.trim();if(!v)return null;
 let terms=$("rx").checked?[v]:($("all").checked?v.split(/\s+/):[v]);
 let res;try{res=terms.map(t=>new RegExp($("rx").checked?t:t.replace(/[.*+?^${}()|[\]\\]/g,"\\$&"),"i"))}catch(e){return()=>false}
 return s=>$("all").checked&&!$("rx").checked?res.every(r=>r.test(s||"")):res.some(r=>r.test(s||""))}
function apply(){
 const mq=matcher($("q").value),mt=matcher($("title").value),mb=matcher($("text").value);
 const au=$("author").value.toLowerCase(),pb=$("pub").value.toLowerCase(),acc=$("access").value;
 const af=$("after").value,bf=$("before").value,mw=+$("minw").value||0,ml=+$("minl").value||0;
 VIEW=ALL.filter(a=>{const d=(a.date||"").slice(0,10);
  if(af&&(!d||d<af))return false;if(bf&&(!d||d>bf))return false;
  if(mt&&!mt(a.title))return false;if(mb&&!mb(a.body_text))return false;
  if(mq&&!mq([a.title,a.subtitle,a.description,a.body_text].join(" ")))return false;
  if(au&&!(a.authors||[]).join(" ").toLowerCase().includes(au))return false;
  if(pb&&!(a.publication||"").toLowerCase().includes(pb))return false;
  if(acc&&a.audience!==acc)return false;if((a.word_count||0)<mw)return false;if((a.likes||0)<ml)return false;return true});
 const k=$("sort").value,dir=+$("dir").value;
 VIEW.sort((x,y)=>{let a=x[k]??"",b=y[k]??"";if(k==="title"){a=a.toLowerCase();b=b.toLowerCase()}
  if(k==="authors"){a=(a||[]).join();b=(b||[]).join()}return (a>b?1:a<b?-1:0)*dir});
 $("count").textContent=`${VIEW.length} of ${ALL.length} articles`;
 const miss=VIEW.filter(a=>!a.body_text).length;$("gb").textContent=`Download full text for ${miss} shown`;$("gb").style.display=miss?"":"none";
 $("rows").innerHTML=VIEW.slice(0,500).map(a=>`<tr><td class="n">${esc((a.date||"").slice(0,10))}</td>
 <td><a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a>
 ${a.audience==="only_paid"?' <span class="tag paid">paid</span>':""}<div class="sub">${esc(a.subtitle)}</div></td>
 <td>${esc((a.authors||[]).join(", "))}</td><td class="mut">${esc(a.publication)}</td>
 <td class="n">${a.word_count??""}</td><td class="n">${a.likes??""}</td></tr>`).join("")
 +(VIEW.length>500?`<tr><td colspan="6" class="mut">Showing first 500 - narrow filters or export to see all.</td></tr>`:"")}
async function load(){ALL=await (await fetch("/api/articles")).json();apply()}
function download(name,type,data){const a=document.createElement("a");
 a.href=URL.createObjectURL(new Blob([data],{type}));a.download=name;a.click()}
$("ejson").onclick=()=>download("articles.json","application/json",JSON.stringify(VIEW,null,2));
$("ecsv").onclick=()=>{const cols=["publication","title","subtitle","authors","date","url","audience","word_count","likes","comments","description","body_text"];
 const cell=v=>`"${String(Array.isArray(v)?v.join("; "):v??"").replace(/"/g,'""')}"`;
 download("articles.csv","text/csv",[cols.join(","),...VIEW.map(a=>cols.map(c=>cell(a[c])).join(","))].join("\n"))};
$("gb").onclick=async()=>{const todo=VIEW.filter(a=>!a.body_text);
 if(!confirm(`Download full text for ${todo.length} articles? ~${Math.ceil(todo.length*0.7/60)} min.`))return;
 const r=await fetch("/api/bodies",{method:"POST",body:JSON.stringify({urls:todo.map(a=>a.url)})});
 if(!r.ok){$("status").textContent=(await r.json()).error;return}poll()};
FIL.forEach(id=>$(id).addEventListener("input",apply));
$("reset").onclick=()=>{FIL.forEach(id=>{const e=$(id);if(e.type==="checkbox")e.checked=false;else if(id==="sort")e.value="date";else if(id==="dir")e.value="-1";else e.value=""});apply()};
document.querySelectorAll("th[data-s]").forEach(th=>th.onclick=()=>{const s=th.dataset.s;
 if(["date","title","likes","word_count"].includes(s)){if($("sort").value===s)$("dir").value=-$("dir").value;else $("sort").value=s;apply()}});
async function poll(){const s=await (await fetch("/api/status")).json();$("status").textContent=s.msg==="Idle"?"":s.msg+` (${s.count} stored)`;
 $("go").disabled=$("gb").disabled=s.running;if(s.running){await load();setTimeout(poll,1500)}else await load()}
$("go").onclick=async()=>{const r=await fetch("/api/fetch",{method:"POST",body:JSON.stringify({
 publications:$("pubs").value.split(/[\n,]+/),limit:$("limit").value,since:$("since").value,until:$("until").value,full_text:$("full").checked,search:$("ssearch").value})});
 if(!r.ok){$("status").textContent=(await r.json()).error;return}poll()};
$("clear").onclick=async()=>{if(confirm("Delete all stored articles?")){await fetch("/api/clear",{method:"POST",body:"{}"});load()}};
poll();
</script></body></html>"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                state["articles"] = json.load(f)
        except Exception:
            pass
    print(f"Open http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()