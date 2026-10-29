"""A small local setup wizard for sql-mcp, with a browser UI (standard library only, no extra deps).

    sql-mcp-setup                 # opens http://127.0.0.1:8765/?t=... in your browser
    python -m sql_mcp.setup --no-browser --port 9000 --config /path/to/connections.json

It helps you build a connection profile, TEST it against the real database (through the same loader and
dialect the server uses), save it into a connections.json, and copy the exact `claude mcp add` command (or an
MCP config JSON) to register the server. The secret you type is used only for the test; it is never written to
connections.json — the file stores a `password_env` / `password_cmd` reference instead.

Security: it binds 127.0.0.1 only and requires a random token in the URL, so other local processes cannot
drive it. Stop it with Ctrl-C when you are done.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENGINES = ("mysql", "mariadb", "postgres", "sqlite", "starrocks")
TOKEN = secrets.token_urlsafe(16)


def sql_mcp_command() -> list[str]:
    exe = pathlib.Path(sys.executable).with_name("sql-mcp")
    return [str(exe)] if exe.exists() else [sys.executable, "-m", "sql_mcp.server"]


def _mask_list(text: str) -> list[str]:
    return [m.strip() for m in (text or "").replace("\n", ",").split(",") if m.strip()]


def _sqlite_databases(text: str) -> dict:
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, path = line.partition("=")
        if name.strip() and path.strip():
            out[name.strip()] = path.strip()
    return out


def build_raw(form: dict, *, for_save: bool) -> dict:
    """Turn the form into a profile dict. for_save omits the literal secret (uses a reference instead)."""
    engine = form.get("engine", "mysql")
    raw: dict = {"engine": engine}
    mask = _mask_list(form.get("mask_columns", ""))
    if mask:
        raw["mask_columns"] = mask
    if engine == "sqlite":
        dbs = _sqlite_databases(form.get("sqlite_databases", ""))
        if dbs:
            raw["databases"] = dbs
            if form.get("database"):
                raw["database"] = form["database"].strip()
        elif form.get("sqlite_path"):
            raw["path"] = form["sqlite_path"].strip()
        return raw
    for key in ("host", "user", "database"):
        if form.get(key):
            raw[key] = form[key].strip()
    if form.get("port"):
        raw["port"] = int(form["port"])
    if engine == "postgres" and form.get("schema"):
        raw["schema"] = form["schema"].strip()
    if engine == "starrocks" and form.get("catalog"):
        raw["catalog"] = form["catalog"].strip()
    if form.get("tls") and form["tls"] != "prefer":
        raw["tls"] = form["tls"]
    if form.get("tls_ca"):
        raw["tls_ca"] = form["tls_ca"].strip()
    # password
    if for_save:
        store = form.get("store_as", "env")
        value = (form.get("store_value") or "").strip()
        if store == "env" and value:
            raw["password_env"] = value
        elif store == "cmd" and value:
            raw["password_cmd"] = json.loads(value) if value.startswith("[") else value.split()
        elif store == "literal":
            raw["password"] = form.get("password", "")
    elif form.get("password") is not None:
        raw["password"] = form.get("password", "")      # literal, for the test only
    return raw


def test_profile(name: str, form: dict) -> dict:
    """Validate + connect using the real loader/dialect. Returns {ok, databases?, tables?, error?}."""
    from . import server as srv
    from .config import config
    from .executor import close_pools
    raw = build_raw(form, for_save=False)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sqlmcp-setup-")) / "c.json"
    tmp.write_text(json.dumps({"default": name, "profiles": {name: raw}}))
    saved_env = os.environ.get("SQL_MCP_CONFIG")
    saved_cache = os.environ.get("SQL_MCP_CACHE_TTL")
    os.environ["SQL_MCP_CONFIG"] = str(tmp)
    os.environ["SQL_MCP_CACHE_TTL"] = "0"
    try:
        config.cache_clear()
        close_pools()          # drop any dialect/pool a previous test cached under this name
        dbs = json.loads(srv.list_databases(profile=name))
        result = {"ok": True, "databases": dbs[:50]}
        try:                                   # a default database is optional; show tables when there is one
            result["tables"] = [r[0] for r in json.loads(srv.list_tables(profile=name))["rows"]][:50]
        except Exception as e:                 # noqa: BLE001
            result["tables_note"] = str(e)
        return result
    except Exception as e:                      # noqa: BLE001  (ToolError messages are user-facing)
        return {"ok": False, "error": str(e)}
    finally:
        for k, v in (("SQL_MCP_CONFIG", saved_env), ("SQL_MCP_CACHE_TTL", saved_cache)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.cache_clear()
        close_pools()
        try:
            tmp.unlink()
            tmp.parent.rmdir()
        except OSError:
            pass


def save_profile(name: str, form: dict, config_path: str) -> dict:
    """Merge the profile into connections.json (created 0600 if new). Returns how to register it."""
    raw = build_raw(form, for_save=True)
    path = pathlib.Path(config_path).expanduser().resolve()
    doc: dict = {"profiles": {}}
    if path.exists():
        try:
            doc = json.loads(path.read_text())
        except ValueError as e:
            return {"ok": False, "error": f"{path} is not valid JSON: {e}"}
    doc.setdefault("profiles", {})
    first = not doc["profiles"]
    doc["profiles"][name] = raw
    if first or not doc.get("default"):
        doc["default"] = name
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    cmd = sql_mcp_command()
    env_args = [f'--env SQL_MCP_CONFIG="{path}"']
    if raw.get("password_env"):
        env_args.append(f'--env {raw["password_env"]}="<your password>"')
    register = ("claude mcp add --scope local \\\n  " + " ".join(env_args)
                + f' \\\n  {name} -- ' + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    mcp_json = {"mcpServers": {name: {"command": cmd[0], **({"args": cmd[1:]} if len(cmd) > 1 else {}),
                                      "env": {"SQL_MCP_CONFIG": str(path),
                                              **({raw["password_env"]: "<your password>"} if raw.get("password_env") else {})}}}}
    return {"ok": True, "path": str(path), "profile": raw, "register": register,
            "mcp_json": json.dumps(mcp_json, indent=2),
            "secret_note": "connections.json stores no secret — set the env var (or password_cmd) separately."}


PAGE = """<!doctype html><html><head><meta charset=utf-8><title>sql-mcp setup</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{color-scheme:light dark}
body{font:15px/1.5 system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem}
h1{font-size:1.4rem} h2{font-size:1.05rem;margin:1.4rem 0 .4rem}
label{display:block;margin:.55rem 0 .15rem;font-weight:600;font-size:.9rem}
input,select,textarea{width:100%;padding:.45rem;border:1px solid #8886;border-radius:6px;background:transparent;color:inherit;font:inherit}
.row{display:flex;gap:.8rem}.row>div{flex:1}
button{margin-top:1rem;padding:.6rem 1.1rem;border:0;border-radius:6px;font:inherit;font-weight:600;cursor:pointer}
#test{background:#2563eb;color:#fff}#save{background:#16a34a;color:#fff}
.hint{font-size:.82rem;opacity:.75;font-weight:400;margin:.1rem 0 0}
pre{background:#8881;padding:.8rem;border-radius:8px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.ok{color:#16a34a}.err{color:#dc2626}.hidden{display:none}
</style></head><body>
<h1>sql-mcp setup</h1>
<p class=hint>Build a connection profile, test it, save it, and get the command to register the server with
Claude Code. The password you type is used only to test — it is never written to the file.</p>

<h2>Connection</h2>
<div class=row><div>
  <label>Engine</label>
  <select id=engine onchange=sync()>
    <option value=mysql>MySQL</option><option value=mariadb>MariaDB</option>
    <option value=postgres>PostgreSQL</option><option value=sqlite>SQLite</option>
    <option value=starrocks>StarRocks</option>
  </select>
</div><div>
  <label>Profile name</label><input id=name value=myproject>
</div></div>

<div class="net">
  <div class=row>
    <div><label>Host</label><input id=host value=127.0.0.1></div>
    <div style=flex:.5><label>Port</label><input id=port></div>
  </div>
  <label>User</label><input id=user value=claude_ro>
  <label>Password <span class=hint>(test only, not saved)</span></label>
  <input id=password type=password>
  <div class=row><div>
    <label>Store password as</label>
    <select id=store_as onchange=sync()>
      <option value=env>environment variable (recommended)</option>
      <option value=cmd>command (keychain lookup)</option>
      <option value=literal>literal in the file (discouraged)</option>
    </select>
  </div><div>
    <label id=store_label>Env var name</label><input id=store_value value=MYPROJECT_DB_PASSWORD>
  </div></div>
</div>

<div class=f_database><label>Database</label><input id=database placeholder="(optional default)"></div>
<div class=f_schema hidden><label>Schema <span class=hint>(PostgreSQL: the tools' "database" = schema)</span></label><input id=schema value=public></div>
<div class=f_catalog hidden><label>Catalog <span class=hint>(StarRocks: default_catalog, or an external catalog)</span></label><input id=catalog value=default_catalog></div>

<div class=sqlite hidden>
  <label>SQLite file</label><input id=sqlite_path placeholder="/data/app.db  (database 'main')">
  <label>…or several files <span class=hint>(one per line: name=/path.db)</span></label>
  <textarea id=sqlite_databases rows=3 placeholder="shop=/data/shop.db&#10;analytics=/data/analytics.db"></textarea>
</div>

<div class=net>
  <div class=row><div>
    <label>TLS</label>
    <select id=tls><option>prefer</option><option>disable</option><option>require</option>
      <option>verify-ca</option><option>verify-full</option></select>
  </div><div><label>TLS CA file <span class=hint>(verify-* only)</span></label><input id=tls_ca></div></div>
</div>

<h2>Safety</h2>
<label>Mask columns <span class=hint>(name fragments returned as *** ; comma-separated)</span></label>
<input id=mask_columns value="password, token, secret, ssn, email">

<h2>Save to</h2>
<input id=config_path value="">

<div><button id=test onclick=test()>Test connection</button>
<button id=save onclick=save()>Save profile</button></div>
<div id=out></div>

<script>
const T = new URLSearchParams(location.search).get('t');
const $ = id => document.getElementById(id);
const PORTS = {mysql:3306, mariadb:3306, postgres:5432, starrocks:9030};
function sync(){
  const e = $('engine').value, lit = e==='sqlite';
  const pe = $('port');                       // show the engine's default port instead of a blank (empty = default silently)
  if(!lit && (!pe.value || Object.values(PORTS).includes(+pe.value))) pe.value = PORTS[e];
  document.querySelectorAll('.net').forEach(x=>x.classList.toggle('hidden', lit));
  document.querySelector('.sqlite').classList.toggle('hidden', !lit);
  document.querySelector('.f_database').classList.toggle('hidden', false);
  document.querySelector('.f_schema').hidden = e!=='postgres';
  document.querySelector('.f_catalog').hidden = e!=='starrocks';
  const s=$('store_as').value;
  $('store_label').textContent = s==='env'?'Env var name':s==='cmd'?'Command (JSON array or space-separated)':'Literal password';
}
function form(){
  const f={};['engine','name','host','port','user','password','store_as','store_value','database','schema',
    'catalog','sqlite_path','sqlite_databases','tls','tls_ca','mask_columns','config_path'].forEach(k=>{const el=$(k);if(el)f[k]=el.value});
  return f;
}
async function call(path){
  const f=form();
  $('out').innerHTML='<p class=hint>working…</p>';
  try{
    const r=await fetch(path,{method:'POST',headers:{'content-type':'application/json','x-sqlmcp-token':T},body:JSON.stringify(f)});
    return await r.json();
  }catch(e){return {ok:false,error:String(e)}}
}
async function test(){
  const d=await call('/api/test');
  if(d.ok){
    let h='<p class=ok>✓ Connected.</p>';
    h+='<p class=hint>Databases: '+(d.databases||[]).join(', ')+'</p>';
    if(d.tables) h+='<p class=hint>Tables in the default database: '+(d.tables.join(', ')||'(none)')+'</p>';
    else if(d.tables_note) h+='<p class=hint>'+d.tables_note+'</p>';
    $('out').innerHTML=h;
  } else $('out').innerHTML='<p class=err>✗ '+(d.error||'failed')+'</p>';
}
async function save(){
  const d=await call('/api/save');
  if(d.ok){
    $('out').innerHTML='<p class=ok>✓ Saved to '+d.path+'</p>'+
      '<p class=hint>'+d.secret_note+'</p>'+
      '<h2>Register with Claude Code</h2><pre>'+d.register.replace(/[&<]/g,c=>c=='&'?'&amp;':'&lt;')+'</pre>'+
      '<h2>…or an MCP config file (--mcp-config / Claude Desktop)</h2><pre>'+d.mcp_json.replace(/[&<]/g,c=>c=='&'?'&amp;':'&lt;')+'</pre>';
  } else $('out').innerHTML='<p class=err>✗ '+(d.error||'failed')+'</p>';
}
$('config_path').value = CONFIG_PATH; sync();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    config_path = "connections.json"

    def log_message(self, *a):            # keep stdout/stderr quiet except for our own prints
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        if urlparse(self.path).path != "/" or q.get("t", [""])[0] != TOKEN:
            return self._send(403, "forbidden", "text/plain")
        page = PAGE.replace("CONFIG_PATH", json.dumps(str(pathlib.Path(self.config_path).resolve())))
        self._send(200, page, "text/html; charset=utf-8")

    def do_POST(self):
        if self.headers.get("x-sqlmcp-token") != TOKEN:
            return self._send(403, json.dumps({"ok": False, "error": "forbidden"}))
        length = int(self.headers.get("content-length", 0))
        try:
            form = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, json.dumps({"ok": False, "error": "bad request"}))
        name = (form.get("name") or "profile").strip()
        try:
            if self.path == "/api/test":
                result = test_profile(name, form)
            elif self.path == "/api/save":
                result = save_profile(name, form, form.get("config_path") or self.config_path)
            else:
                return self._send(404, json.dumps({"ok": False, "error": "not found"}))
        except Exception as e:            # noqa: BLE001
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self._send(200, json.dumps(result))


def main() -> None:
    ap = argparse.ArgumentParser(description="Local setup wizard for sql-mcp (browser UI).")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1; keep it loopback)")
    ap.add_argument("--port", type=int, default=8765, help="port (default 8765; 0 picks a free one)")
    ap.add_argument("--config", default="connections.json", help="connections.json to create/update")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser automatically")
    args = ap.parse_args()

    Handler.config_path = args.config
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        import errno
        if e.errno != errno.EADDRINUSE or args.port == 0:
            raise
        print(f"port {args.port} is already in use (another wizard may be running); picking a free port instead.",
              file=sys.stderr)
        httpd = ThreadingHTTPServer((args.host, 0), Handler)
    port = httpd.server_address[1]
    url = f"http://{args.host}:{port}/?t={TOKEN}"
    print(f"sql-mcp setup wizard: {url}", file=sys.stderr)
    print("Press Ctrl-C to stop.", file=sys.stderr)
    if not args.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
