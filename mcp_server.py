#!/usr/bin/env python3
"""Werkstadt als MCP-Server (stdio, nur Standardbibliothek). Liest nur, ändert nichts.

  claude mcp add werkstadt -- python3 /pfad/zu/werkstadt/mcp_server.py

Werkzeuge: werkstadt_status (was ist gerade los), werkstadt_setup_check (was ist eingerichtet, was fehlt).
"""
import json, subprocess, sys, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = [
    {"name": "werkstadt_status",
     "description": "Live status of the user's projects from the Werkstadt collector: Claude sessions waiting for "
                    "input or working, repos with recent activity or uncommitted changes, failed or running CI, "
                    "releases of the last 14 days, recent commits and published container images.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "werkstadt_setup_check",
     "description": "Shows what of Werkstadt is set up on this machine (config, collector running, Claude Code hooks, "
                    "autostart, Home Assistant, available tools) and the next step to finish setup. Setup itself is "
                    "interactive: the user runs `python3 setup.py` in a terminal.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
]


def port():
    f = HERE / "werkstadt.local.json"
    return (json.loads(f.read_text()) if f.exists() else {}).get("port", 7777)


def status():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port()}/api/state", timeout=3) as r:
            st = json.load(r)
    except Exception:
        return {"error": "Collector not reachable. Start it with `python3 collector.py` or run `python3 setup.py`."}
    now = time.time()
    repos = st["repos"]
    return {
        "sessions_waiting": [s["repo"] for s in st["sessions"] if s["state"] == "waiting"],
        "sessions_working": [{"repo": s["repo"], "tool": s.get("tool"), "subagents": s.get("subagents", 0)}
                             for s in st["sessions"] if s["state"] == "working"],
        "active_repos": [r["id"] for r in repos if now - r.get("last_commit", 0) < 1800],
        "uncommitted": {r["id"]: r["dirty"] for r in repos if r.get("dirty")},
        "open_worktrees": {r["id"]: r["worktrees"] for r in repos if r.get("worktrees")},
        "ci_failed": [r["id"] for r in repos if (r.get("ci") or {}).get("conclusion") == "failure"],
        "ci_running": [r["id"] for r in repos if (r.get("ci") or {}).get("status") in ("in_progress", "queued")],
        "releases": st["releases"],
        "recent_commits": [{"repo": e["repo"], "from": e["from"], "msg": e["msg"],
                            "minutes_ago": round((now - e["at"]) / 60)} for e in st.get("events", [])][-10:],
        "images": [f"{i.get('ref') or i['repo']}:{i['tag']}" for i in (st.get("docker") or {}).get("images", [])],
        "repo_count": len(repos), "url": f"http://localhost:{port()}",
    }


def setup_check():
    out = subprocess.run([sys.executable, str(HERE / "setup.py"), "--check"], capture_output=True, text=True, timeout=120)
    c = json.loads(out.stdout)
    todo = []
    if not c["config"]:
        todo.append(f"Run the wizard in a terminal: {sys.executable} {HERE / 'setup.py'}")
    elif not c["collector_running"]:
        todo.append(f"Start the collector: {sys.executable} {HERE / 'collector.py'} (or enable autostart in the wizard)")
    if c["config"] and not c["claude_hooks"]:
        todo.append("Claude Code hooks are not installed, so sessions do not show up. Re-run the wizard, step 4.")
    if not c["tools"]["gh"]:
        todo.append("Optional: install and log in to the GitHub CLI (gh) for CI status.")
    c["next_steps"] = todo or ["Everything is set up."]
    return c


def handle(msg):
    m, i = msg.get("method"), msg.get("id")
    if m == "initialize":
        return {"jsonrpc": "2.0", "id": i, "result": {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}}, "serverInfo": {"name": "werkstadt", "version": "0.1.0"}}}
    if m == "tools/list":
        return {"jsonrpc": "2.0", "id": i, "result": {"tools": TOOLS}}
    if m == "tools/call":
        name = msg.get("params", {}).get("name")
        fn = {"werkstadt_status": status, "werkstadt_setup_check": setup_check}.get(name)
        if not fn:
            return {"jsonrpc": "2.0", "id": i, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        try:
            res, err = fn(), False
        except Exception as e:
            res, err = {"error": str(e)}, True
        return {"jsonrpc": "2.0", "id": i, "result": {
            "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=1)}], "isError": err}}
    if m == "ping":
        return {"jsonrpc": "2.0", "id": i, "result": {}}
    if i is not None:
        return {"jsonrpc": "2.0", "id": i, "error": {"code": -32601, "message": f"method not found: {m}"}}
    return None  # Benachrichtigung


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        resp = handle(json.loads(line))
    except Exception as e:
        resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}
    if resp:
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
