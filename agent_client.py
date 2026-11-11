#!/usr/bin/env python3
"""Minimal client for the agent bridge (supports threads).

Usage:
    agent_client.py send   <agent_id> "<message>" [--name "Display"] [--thread ID]
    agent_client.py poll   [--since N] [--limit 500] [--thread ID]
    agent_client.py threads
    agent_client.py thread-create <thread_id> [--topic "Topic"] [--by agent]
    agent_client.py health
    agent_client.py agents

Env:
    BRIDGE_URL       base URL          (default http://127.0.0.1:8787)
    BRIDGE_API_KEY   optional key      (sent as X-Api-Key)

Examples:
    agent_client.py send hermes "hello" --thread deep-dive
    agent_client.py poll --thread deep-dive --since 0
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8787")
KEY = os.environ.get("BRIDGE_API_KEY", "")


def _req(method, path, payload=None):
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if KEY:
        req.add_header("X-Api-Key", KEY)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send")
    s.add_argument("agent_id")
    s.add_argument("text")
    s.add_argument("--name", default=None)
    s.add_argument("--thread", default=None)

    pl = sub.add_parser("poll")
    pl.add_argument("--since", type=int, default=0)
    pl.add_argument("--limit", type=int, default=500)
    pl.add_argument("--thread", default=None)

    sub.add_parser("threads")

    tc = sub.add_parser("thread-create")
    tc.add_argument("thread_id")
    tc.add_argument("--topic", default=None)
    tc.add_argument("--by", dest="created_by", default=None)

    sub.add_parser("health")
    sub.add_parser("agents")

    a = p.parse_args()
    try:
        if a.cmd == "send":
            body = {"agent_id": a.agent_id, "agent_name": a.name or a.agent_id, "text": a.text}
            if a.thread:
                body["thread_id"] = a.thread
            print(json.dumps(_req("POST", "/api/messages", body)))
        elif a.cmd == "poll":
            qs = f"?since_id={a.since}&limit={a.limit}"
            if a.thread:
                qs += "&thread_id=" + urllib.request.quote(a.thread)
            print(json.dumps(_req("GET", f"/api/messages{qs}"), indent=2))
        elif a.cmd == "threads":
            print(json.dumps(_req("GET", "/api/threads"), indent=2))
        elif a.cmd == "thread-create":
            body = {"thread_id": a.thread_id}
            if a.topic:
                body["topic"] = a.topic
            if a.created_by:
                body["created_by"] = a.created_by
            print(json.dumps(_req("POST", "/api/threads", body)))
        elif a.cmd == "health":
            print(json.dumps(_req("GET", "/health")))
        elif a.cmd == "agents":
            print(json.dumps(_req("GET", "/api/agents"), indent=2))
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode(), file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print("error:", e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()