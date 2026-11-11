#!/usr/bin/env python3
"""Agent bridge — shared message bus + webchat UI between N agents.

Zero-dependency (Python stdlib only). Any number of agents can join dynamically
and exchange messages through two HTTP endpoints:

    POST /api/messages          write a message   (send/emit)
    GET  /api/messages          read messages     (poll, supports ?since_id=)

Also serves a human-readable chat log / webchat at GET /.

Config via env (optional):
    BRIDGE_HOST    listen host      (default 127.0.0.1)
    BRIDGE_PORT    listen port      (default 8787)
    BRIDGE_DB      sqlite file path (default ./bridge.db)
    BRIDGE_API_KEY if set, POST/GET /api/* require header X-Api-Key: <key>
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("BRIDGE_PORT", "8787"))
DB_PATH = os.environ.get("BRIDGE_DB", os.path.join(BASE, "bridge.db"))
API_KEY = os.environ.get("BRIDGE_API_KEY", "")

_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    with _lock:
        c = _conn()
        c.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                text TEXT NOT NULL
            )"""
        )
        c.commit()
        c.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert_message(agent_id, agent_name, text):
    with _lock:
        c = _conn()
        cur = c.execute(
            "INSERT INTO messages (ts, agent_id, agent_name, text) VALUES (?,?,?,?)",
            (now_iso(), agent_id, agent_name, text),
        )
        mid = cur.lastrowid
        c.commit()
        c.close()
    return {"id": mid, "agent_id": agent_id, "agent_name": agent_name, "text": text}


def list_messages(since_id=0, limit=500):
    with _lock:
        c = _conn()
        rows = c.execute(
            "SELECT id, ts, agent_id, agent_name, text FROM messages "
            "WHERE id > ? ORDER BY id ASC LIMIT ?",
            (since_id, int(limit)),
        ).fetchall()
        c.close()
    return [
        {"id": r[0], "ts": r[1], "agent_id": r[2], "agent_name": r[3], "text": r[4]}
        for r in rows
    ]


def last_id():
    with _lock:
        c = _conn()
        r = c.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()
        c.close()
    return r[0]


def list_agents():
    with _lock:
        c = _conn()
        rows = c.execute(
            "SELECT agent_id, MAX(agent_name), COUNT(*) FROM messages GROUP BY agent_id ORDER BY 3 DESC"
        ).fetchall()
        c.close()
    return [{"agent_id": r[0], "agent_name": r[1], "messages": r[2]} for r in rows]


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Bridge — Webchat Log</title>
<style>
  body{font-family:system-ui,sans-serif;max-width:860px;margin:0 auto;padding:16px;background:#0f1115;color:#e6e6e6}
  h1{font-size:1.2rem} .meta{color:#8a8f98;font-size:.85rem}
  #log{background:#171a21;border:1px solid #2a2f3a;border-radius:8px;padding:12px;height:60vh;overflow-y:auto}
  .m{margin:6px 0;padding:8px 10px;border-radius:8px;background:#1f242e;border-left:3px solid #4f8cff}
  .m .who{font-weight:600;color:#9ecbff}.m .ts{color:#6b7280;font-size:.75rem;margin-left:8px}
  .m .txt{white-space:pre-wrap;word-break:break-word}
  form{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
  input,button{padding:8px;border-radius:6px;border:1px solid #2a2f3a;background:#171a21;color:#e6e6e6}
  input#text{flex:1;min-width:220px} button{cursor:pointer;background:#2563eb;border:none}
  #agents{color:#8a8f98;font-size:.85rem;margin-top:8px}
</style></head><body>
<h1>Agent Bridge</h1>
<div class="meta" id="meta">connecting&hellip;</div>
<div id="log"></div>
<form id="f">
  <input id="name" placeholder="sender name" value="agent">
  <input id="text" placeholder="message" autocomplete="off">
  <button>send</button>
</form>
<div id="agents"></div>
<script>
let last = 0;
async function poll(){
  try{
    const r = await fetch('/api/messages?since_id='+last);
    const d = await r.json();
    const log = document.getElementById('log');
    for(const m of d.messages){
      const div = document.createElement('div'); div.className='m';
      const who = document.createElement('span'); who.className='who'; who.textContent = m.agent_name;
      const ts = document.createElement('span'); ts.className='ts'; ts.textContent = m.ts;
      const tx = document.createElement('div'); tx.className='txt'; tx.textContent = m.text;
      div.append(who, ts, tx); log.appendChild(div); last = Math.max(last, m.id);
    }
    if(d.messages.length) log.scrollTop = log.scrollHeight;
    document.getElementById('meta').textContent = 'last_id='+d.last_id+' · '+(new Date()).toLocaleTimeString();
  }catch(e){ document.getElementById('meta').textContent = 'poll error: '+e; }
}
async function agents(){
  try{ const d = await (await fetch('/api/agents')).json();
    document.getElementById('agents').textContent = 'participants: ' +
      d.map(a=>a.agent_name+' ('+a.messages+')').join(', ');
  }catch(e){}
}
document.getElementById('f').onsubmit = async (e)=>{
  e.preventDefault();
  await fetch('/api/messages',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({agent_id:document.getElementById('name').value,
      agent_name:document.getElementById('name').value,
      text:document.getElementById('text').value})});
  document.getElementById('text').value='';
  await poll();
};
setInterval(poll,1500); setInterval(agents,5000); poll(); agents();
</script></body></html>
"""


def _auth_ok(headers):
    if not API_KEY:
        return True
    return headers.get("X-Api-Key", "") == API_KEY


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentBridge/1.0"

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Api-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send_html(HTML)
            return
        if parsed.path == "/api/messages":
            if not _auth_ok(self.headers):
                self._send_json({"error": "unauthorized"}, 401)
                return
            q = parse_qs(parsed.query)
            since = int(q.get("since_id", ["0"])[0] or 0)
            limit = int(q.get("limit", ["500"])[0] or 500)
            msgs = list_messages(since, limit)
            self._send_json({"messages": msgs, "last_id": last_id()})
            return
        if parsed.path == "/api/agents":
            if not _auth_ok(self.headers):
                self._send_json({"error": "unauthorized"}, 401)
                return
            self._send_json(list_agents())
            return
        if parsed.path == "/health":
            self._send_json({"ok": True, "last_id": last_id(), "time": now_iso()})
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/messages":
            self._send_json({"error": "not found"}, 404)
            return
        if not _auth_ok(self.headers):
            self._send_json({"error": "unauthorized"}, 401)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._send_json({"error": "invalid json"}, 400)
            return
        agent_id = (data.get("agent_id") or "").strip()
        text = (data.get("text") or "").strip()
        if not agent_id or not text:
            self._send_json({"error": "agent_id and text are required"}, 400)
            return
        agent_name = (data.get("agent_name") or agent_id).strip()
        self._send_json(insert_message(agent_id, agent_name, text), 201)


if __name__ == "__main__":
    init_db()
    print(f"Agent bridge listening on http://{HOST}:{PORT}  (db: {DB_PATH})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()