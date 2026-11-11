#!/usr/bin/env python3
"""Agent bridge — shared message bus + webchat UI between N agents.

Zero-dependency (Python stdlib only). Any number of agents join dynamically and
exchange messages over HTTP, organized into **threads** (topics):

    POST /api/messages          write a message (optional thread_id)
    GET  /api/messages          read messages (poll, ?since_id= & ?thread_id=)
    POST /api/threads           create/open a thread
    GET  /api/threads           list threads

Also serves a human-readable webchat UI at GET / (with an in-page API-key field).

Config via env (optional):
    BRIDGE_HOST    listen host      (default 127.0.0.1)
    BRIDGE_PORT    listen port      (default 8787)
    BRIDGE_DB      sqlite file path (default ./bridge.db)
    BRIDGE_API_KEY if set, /api/* require header X-Api-Key: <key>
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

DEFAULT_THREAD = "general"

_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db():
    with _lock:
        c = _conn()
        c.execute(
            """CREATE TABLE IF NOT EXISTS threads (
                thread_id TEXT PRIMARY KEY,
                topic     TEXT NOT NULL,
                created_by TEXT,
                ts        TEXT NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT NOT NULL,
                agent_id   TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                thread_id  TEXT NOT NULL DEFAULT 'general',
                text       TEXT NOT NULL
            )"""
        )
        # migrate legacy table that predates threads
        cols = [r[1] for r in c.execute("PRAGMA table_info(messages)").fetchall()]
        if "thread_id" not in cols:
            c.execute(
                "ALTER TABLE messages ADD COLUMN thread_id TEXT NOT NULL DEFAULT 'general'"
            )
        c.execute(
            "INSERT OR IGNORE INTO threads (thread_id, topic, created_by, ts) VALUES (?,?,?,?)",
            (DEFAULT_THREAD, "General", "system", now_iso()),
        )
        c.commit()
        c.close()


def ensure_thread(thread_id, topic=None, created_by=None):
    """Create a thread if missing; returns (thread_id, topic)."""
    with _lock:
        c = _conn()
        c.execute(
            "INSERT OR IGNORE INTO threads (thread_id, topic, created_by, ts) VALUES (?,?,?,?)",
            (thread_id, topic or thread_id, created_by, now_iso()),
        )
        c.commit()
        row = c.execute("SELECT topic FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
        c.close()
    return thread_id, (row[0] if row else (topic or thread_id))


def create_thread(thread_id, topic=None, created_by=None):
    """Explicit create; returns (dict, err)."""
    tid = (thread_id or "").strip()
    if not tid:
        return None, "thread_id is required"
    with _lock:
        c = _conn()
        existing = c.execute(
            "SELECT topic, created_by FROM threads WHERE thread_id=?", (tid,)
        ).fetchone()
        if existing:
            c.close()
            return {"thread_id": tid, "topic": existing[0], "created_by": existing[1], "exists": True}, None
        t = (topic or tid).strip()
        c.execute(
            "INSERT INTO threads (thread_id, topic, created_by, ts) VALUES (?,?,?,?)",
            (tid, t, created_by, now_iso()),
        )
        c.commit()
        c.close()
    return {"thread_id": tid, "topic": t, "created_by": created_by, "exists": False}, None


def insert_message(agent_id, agent_name, text, thread_id=DEFAULT_THREAD):
    with _lock:
        c = _conn()
        cur = c.execute(
            "INSERT INTO messages (ts, agent_id, agent_name, thread_id, text) VALUES (?,?,?,?,?)",
            (now_iso(), agent_id, agent_name, thread_id, text),
        )
        mid = cur.lastrowid
        c.commit()
        c.close()
    return {
        "id": mid,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "thread_id": thread_id,
        "text": text,
    }


def list_messages(since_id=0, limit=500, thread_id=None):
    with _lock:
        c = _conn()
        if thread_id:
            rows = c.execute(
                "SELECT id, ts, agent_id, agent_name, thread_id, text FROM messages "
                "WHERE thread_id=? AND id > ? ORDER BY id ASC LIMIT ?",
                (thread_id, int(since_id), int(limit)),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, ts, agent_id, agent_name, thread_id, text FROM messages "
                "WHERE id > ? ORDER BY id ASC LIMIT ?",
                (int(since_id), int(limit)),
            ).fetchall()
        c.close()
    return [
        {
            "id": r[0],
            "ts": r[1],
            "agent_id": r[2],
            "agent_name": r[3],
            "thread_id": r[4],
            "text": r[5],
        }
        for r in rows
    ]


def last_id():
    with _lock:
        c = _conn()
        r = c.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()
        c.close()
    return r[0]


def list_threads():
    with _lock:
        c = _conn()
        rows = c.execute(
            """SELECT t.thread_id, t.topic, t.created_by, t.ts,
                      COUNT(m.id), MAX(m.id), MAX(m.ts)
               FROM threads t LEFT JOIN messages m ON m.thread_id = t.thread_id
               GROUP BY t.thread_id
               ORDER BY (MAX(m.id) IS NULL), MAX(m.id) DESC"""
        ).fetchall()
        c.close()
    return [
        {
            "thread_id": r[0],
            "topic": r[1],
            "created_by": r[2],
            "created_ts": r[3],
            "messages": r[4],
            "last_message_id": r[5],
            "last_ts": r[6],
        }
        for r in rows
    ]


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
<title>Agent Bridge — Webchat</title>
<style>
  *{box-sizing:border-box}
  body{font-family:system-ui,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
  .top{display:flex;align-items:center;gap:12px;padding:10px 16px;background:#171a21;border-bottom:1px solid #2a2f3a}
  .top h1{font-size:1.1rem;margin:0}
  .top .key{display:flex;gap:6px;margin-left:auto}
  .top input,button{border:1px solid #2a2f3a;background:#0f1115;color:#e6e6e6;padding:7px 10px;border-radius:6px;font-size:.9rem}
  #apikey{width:260px} button{cursor:pointer;background:#2563eb;border:none}
  #meta{color:#8a8f98;font-size:.8rem}
  .layout{display:flex;height:calc(100vh - 48px)}
  aside{width:240px;border-right:1px solid #2a2f3a;background:#171a21;padding:10px;overflow-y:auto}
  .side-head{font-size:.85rem;color:#8a8f98;margin-bottom:6px}
  .newth{display:flex;gap:6px;margin-bottom:10px}
  .newth input{flex:1;min-width:0}
  #threads .th{padding:7px 9px;border-radius:6px;cursor:pointer;display:flex;justify-content:space-between;font-size:.9rem}
  #threads .th:hover{background:#1f242e} #threads .th.active{background:#2563eb}
  #threads .th .cnt{color:#8a8f98;font-size:.75rem;align-self:center}
  main{flex:1;display:flex;flex-direction:column;min-width:0}
  #log{flex:1;overflow-y:auto;padding:14px}
  .m{margin:6px 0;padding:8px 10px;border-radius:8px;background:#1f242e;border-left:3px solid #4f8cff}
  .m .who{font-weight:600;color:#9ecbff}.m .ts{color:#6b7280;font-size:.75rem;margin-left:8px}
  .m .txt{white-space:pre-wrap;word-break:break-word}
  form{display:flex;gap:8px;padding:10px 14px;border-top:1px solid #2a2f3a;background:#171a21}
  form input#text{flex:1}
  .hint{color:#fbbf24;font-size:.8rem;padding:4px 0}
</style></head><body>
<div class="top">
  <h1>Agent Bridge</h1>
  <div class="key">
    <input id="apikey" type="password" placeholder="API key (X-Api-Key)" autocomplete="off">
    <button id="connect">Connect</button>
  </div>
  <span id="meta"></span>
</div>
<div class="layout">
<aside>
  <div class="side-head">THREADS</div>
  <div class="newth">
    <input id="topic" placeholder="new topic…" autocomplete="off">
    <button id="createthread" title="create thread">+</button>
  </div>
  <div id="threads"></div>
  <div id="hint" class="hint"></div>
</aside>
<main>
  <div id="log"></div>
  <form id="f">
    <input id="name" placeholder="sender name" value="agent">
    <input id="text" placeholder="message" autocomplete="off">
    <button>send</button>
  </form>
</main>
</div>
<script>
let key = localStorage.getItem('bridge_key') || '';
let current = 'general';
let last = 0;

const hdr = () => {
  const h = {'Content-Type':'application/json'};
  if (key) h['X-Api-Key'] = key;
  return h;
};

async function api(method, path, body){
  const opt = {method, headers: hdr()};
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  let d = {};
  try { d = await r.json(); } catch(e){}
  if (r.status === 401) throw new Error('401 Unauthorized — enter the API key');
  if (!r.ok) throw new Error(d.error || ('HTTP '+r.status));
  return d;
}

function esc(s){ return String(s ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function hint(msg){ document.getElementById('hint').textContent = msg || ''; }

async function poll(){
  try{
    const d = await api('GET', '/api/messages?since_id='+last+'&thread_id='+encodeURIComponent(current));
    const log = document.getElementById('log');
    for(const m of d.messages){
      const div = document.createElement('div'); div.className='m';
      div.innerHTML = '<span class="who">'+esc(m.agent_name)+'</span>'+
        '<span class="ts">'+esc(m.ts)+'</span>'+
        '<div class="txt">'+esc(m.text)+'</div>';
      log.appendChild(div);
      last = Math.max(last, m.id);
    }
    if(d.messages.length) log.scrollTop = log.scrollHeight;
    document.getElementById('meta').textContent = 'thread: '+current+' · last_id='+d.last_id;
    hint('');
  }catch(e){
    document.getElementById('meta').textContent = e.message;
    hint(String(e.message).includes('401') ? 'Masukkan API key di pojok kanan atas, lalu klik Connect.' : '');
  }
}

async function loadThreads(){
  try{
    const list = await api('GET','/api/threads');
    const el = document.getElementById('threads');
    el.innerHTML = '';
    for(const t of list){
      const div = document.createElement('div');
      div.className = 'th' + (t.thread_id===current?' active':'');
      div.innerHTML = '<span>'+esc(t.topic)+'</span><span class="cnt">'+t.messages+'</span>';
      div.onclick = ()=>select(t.thread_id);
      el.appendChild(div);
    }
  }catch(e){}
}

function select(id){
  current = id;
  last = 0;
  document.getElementById('log').innerHTML = '';
  loadThreads(); poll();
}

function connect(){
  key = document.getElementById('apikey').value.trim();
  localStorage.setItem('bridge_key', key);
  last = 0;
  loadThreads(); poll();
}

function newThread(){
  const topic = document.getElementById('topic').value.trim();
  if(!topic) return;
  const slug = topic.toLowerCase().replace(/[^a-z0-9_-]+/g,'-').replace(/^-+|-+$/g,'') || '';
  api('POST','/api/threads',{thread_id: slug || topic.toLowerCase(), topic: topic, created_by: document.getElementById('name').value.trim()||'web'})
    .then(t=>{ document.getElementById('topic').value=''; loadThreads(); select(t.thread_id); })
    .catch(e=>hint(e.message));
}

document.getElementById('connect').onclick = connect;
document.getElementById('apikey').addEventListener('keydown', e=>{ if(e.key==='Enter') connect(); });
if(key) document.getElementById('apikey').value = key;
document.getElementById('createthread').onclick = newThread;
document.getElementById('topic').addEventListener('keydown', e=>{ if(e.key==='Enter') newThread(); });

document.getElementById('f').onsubmit = async (e)=>{
  e.preventDefault();
  const name = document.getElementById('name').value.trim() || 'agent';
  const textEl = document.getElementById('text');
  try{
    await api('POST','/api/messages',{agent_id:name, agent_name:name, thread_id:current, text:textEl.value});
    textEl.value='';
    await poll();
  }catch(err){ hint(err.message); }
};

setInterval(poll,1500); setInterval(loadThreads,5000);
loadThreads(); poll();
</script></body></html>
"""


def _auth_ok(headers):
    if not API_KEY:
        return True
    return headers.get("X-Api-Key", "") == API_KEY


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentBridge/1.1"

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
            thread_id = q.get("thread_id", [None])[0]
            msgs = list_messages(since, limit, thread_id)
            self._send_json({"messages": msgs, "last_id": last_id()})
            return
        if parsed.path == "/api/threads":
            if not _auth_ok(self.headers):
                self._send_json({"error": "unauthorized"}, 401)
                return
            self._send_json(list_threads())
            return
        if parsed.path == "/api/agents":
            if not _auth_ok(self.headers):
                self._send_json({"error": "unauthorized"}, 401)
                return
            self._send_json(list_agents())
            return
        if parsed.path == "/health":
            self._send_json(
                {"ok": True, "last_id": last_id(), "threads": len(list_threads()), "time": now_iso()}
            )
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/api/messages", "/api/threads"):
            if not _auth_ok(self.headers):
                self._send_json({"error": "unauthorized"}, 401)
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                self._send_json({"error": "invalid json"}, 400)
                return

            if parsed.path == "/api/threads":
                tid = (data.get("thread_id") or "").strip()
                if not tid:
                    self._send_json({"error": "thread_id is required"}, 400)
                    return
                obj, err = create_thread(
                    tid, data.get("topic"), data.get("created_by") or data.get("agent_id")
                )
                if err:
                    self._send_json({"error": err}, 400)
                    return
                self._send_json(obj, 201 if not obj.get("exists") else 200)
                return

            # /api/messages
            agent_id = (data.get("agent_id") or "").strip()
            text = (data.get("text") or "").strip()
            if not agent_id or not text:
                self._send_json({"error": "agent_id and text are required"}, 400)
                return
            agent_name = (data.get("agent_name") or agent_id).strip()
            thread_id = (data.get("thread_id") or DEFAULT_THREAD).strip() or DEFAULT_THREAD
            ensure_thread(thread_id, created_by=agent_id)
            self._send_json(insert_message(agent_id, agent_name, text, thread_id), 201)
            return

        self._send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    init_db()
    print(f"Agent bridge listening on http://{HOST}:{PORT}  (db: {DB_PATH})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()