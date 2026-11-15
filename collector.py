#!/usr/bin/env python3
"""Werkstadt-Collector: sammelt Repos, Releases, CI und Claude-Sitzungen und liefert sie an web/.

Nur Standardbibliothek. Einrichtung: python3 setup.py. Start: python3 collector.py  ->  http://localhost:7777
Endpunkte: GET /api/state, POST /event (Claude-Code-Hooks), sonst statische Dateien aus web/.
"""
import json, os, subprocess, threading, time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULTS = {"roots": ["~/dev"], "host": "127.0.0.1", "port": 7777, "aliases": {}, "skip": [], "oss": [],
            "werk": [], "links": [], "docker_hosts": [], "ci_poll_seconds": 90, "git_poll_seconds": 60}
CONFIG_FILE = HERE / "werkstadt.local.json"  # nicht im Repo; legt setup.py an
CFG = dict(DEFAULTS)
if CONFIG_FILE.exists():
    CFG.update(json.loads(CONFIG_FILE.read_text()))
_last_waiting = None
LOCK = threading.Lock()
STATE = {"repos": [], "links": [], "releases": [], "sessions": [], "docker": None, "updated": None}
SESSIONS = {}  # session_id -> dict
EVENTS = []    # neue Commits: {"id","repo","from","msg","at"}
SEEN = {}      # repo -> Zeitpunkt des letzten Scans (unix)
CI = {}        # repo name -> {"status","conclusion","name","at"}
REPO_CACHE = {}  # cwd -> repo name


def git(path, *args, timeout=20):
    try:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""


def discover():
    """Hauptcheckouts unter den Wurzeln (Tiefe 1 und 2), ohne Worktrees (.git ist dort eine Datei)."""
    found = {}
    for root in CFG["roots"]:
        root = Path(os.path.expanduser(root))
        for d in list(root.glob("*/")) + list(root.glob("*/*/")):
            if not (d / ".git").is_dir() or d.name.startswith("wt-"):
                continue
            if d.name in CFG["skip"]:
                continue
            name = CFG["aliases"].get(d.name, d.name)
            found[name] = d
    return found


def remote_slug(path):
    url = git(path, "remote", "get-url", "origin")
    if "github.com" not in url:
        return None
    slug = url.split("github.com")[-1].lstrip(":/")
    return slug[:-4] if slug.endswith(".git") else slug


def scan_repo(name, path):
    files = len(git(path, "ls-files").splitlines())
    c30 = int(git(path, "rev-list", "--count", "--all", "--since=30.days") or 0)
    today = int(git(path, "rev-list", "--count", "--all", "--since=midnight") or 0)
    wts = [l for l in git(path, "worktree", "list", "--porcelain").splitlines() if l.startswith("worktree ")]
    # Offene Änderungen im Hauptcheckout und in allen Worktrees
    dirty = 0
    for w in wts:
        dirty += len(git(w.split(" ", 1)[1], "status", "--porcelain").splitlines())
    last = git(path, "log", "--all", "-1", "--format=%ct")
    tag = git(path, "for-each-ref", "--sort=-creatordate", "--count=1",
              "--format=%(refname:short)|%(creatordate:unix)", "refs/tags")
    release = None
    if "|" in tag:
        t, ts = tag.split("|")
        release = {"repo": name, "version": t, "age_days": round((time.time() - int(ts)) / 86400, 1)}
    if name in CFG["oss"]:
        tier = "oss"
    elif name in CFG["werk"]:
        tier = "werk"
    elif files > 1000 or c30 > 500:
        tier = "gross"
    elif files >= 400:
        tier = "stadt"
    else:
        tier = "dorf"
    return {"id": name, "name": name, "path": str(path), "slug": remote_slug(path), "tier": tier,
            "files": files, "commits": c30, "today": today, "dirty": dirty,
            "last_commit": int(last) if last else 0, "worktrees": max(0, len(wts) - 1),
            "release": release}


def scan_git():
    repos = discover()
    data = [scan_repo(n, p) for n, p in sorted(repos.items())]
    data = [r for r in data if r["files"] > 0]
    by = {r["id"]: r for r in data}
    links = []
    pairs = [(a, b) for a, b in CFG["links"] if a in by and b in by]
    for a, b in pairs:
        # Verkehr = Commits im Ziel der letzten 30 Tage, verteilt auf alle Zubringer
        share = sum(1 for _, t in pairs if t == b)
        links.append([a, b, round(by[b]["commits"] / share), 0.5 if by[b]["tier"] == "oss" else 0])
    scan_commits(data, by)
    rel = sorted((r["release"] for r in data if r["release"] and r["release"]["age_days"] <= 14),
                 key=lambda x: x["age_days"])[:8]
    with LOCK:
        STATE.update(repos=data, links=links, releases=rel)


def scan_commits(data, by):
    """Neue Commits seit dem letzten Scan als Ereignisse. Beim Start: die letzten 30 Minuten.

    Herkunft bei Kernen: der Zubringer, dessen Name im Commit-Text steht; sonst bei genau einem
    Zubringer dieser; sonst der Zubringer mit dem jüngsten eigenen Commit in den 3 h davor."""
    now = time.time()
    feeders = {}
    for a, b in CFG["links"]:
        feeders.setdefault(b, []).append(a)
    last_commit = {}
    for r in data:
        ts = git(r["path"], "log", "--all", "-1", "--format=%ct")
        last_commit[r["id"]] = int(ts) if ts else 0
    for r in data:
        since = SEEN.get(r["id"], now - 1800)
        SEEN[r["id"]] = now
        out = git(r["path"], "log", "--all", f"--since={int(since)}", "--format=%H|%ct|%s")
        for line in out.splitlines()[:30]:
            sha, ts, msg = line.split("|", 2)
            if any(e["id"] == sha for e in EVENTS):
                continue
            src = None
            cands = [f for f in feeders.get(r["id"], []) if f in by]
            low = msg.lower()
            for f in cands:
                if f.lower() in low:
                    src = f
            if not src and len(cands) == 1:
                src = cands[0]
            if not src and cands:
                recent = [(last_commit.get(f, 0), f) for f in cands if int(ts) - last_commit.get(f, 0) < 3 * 3600]
                src = max(recent)[1] if recent else None
            EVENTS.append({"id": sha, "repo": r["id"], "from": src, "msg": msg[:80], "at": int(ts)})
    EVENTS[:] = [e for e in EVENTS if now - e["at"] < 6 * 3600][-200:]


def scan_ci():
    with LOCK:
        repos = [r for r in STATE["repos"] if r["slug"] and r["commits"] > 0]
    for r in repos:
        try:
            out = subprocess.run(["gh", "run", "list", "-R", r["slug"], "-L", "1", "--json",
                                  "status,conclusion,workflowName,updatedAt"],
                                 capture_output=True, text=True, timeout=20).stdout
            runs = json.loads(out or "[]")
        except Exception:
            runs = []
        if runs:
            CI[r["id"]] = {"status": runs[0]["status"], "conclusion": runs[0]["conclusion"],
                           "name": runs[0]["workflowName"], "at": runs[0]["updatedAt"]}


def scan_docker():
    """Container und Images aller konfigurierten Docker-Hosts (lokal oder z. B. ssh://nas)."""
    hosts = CFG.get("docker_hosts") or [None]
    containers, images, found = [], [], False
    for h in hosts:
        base = ["docker"] + (["-H", h] if h else [])
        try:
            ps = subprocess.run(base + ["ps", "-a", "--format", "{{json .}}"], capture_output=True,
                                text=True, timeout=20)
            im = subprocess.run(base + ["images", "--format", "{{json .}}"], capture_output=True,
                                text=True, timeout=20)
        except Exception:
            continue
        if ps.returncode != 0:
            continue
        found = True
        for l in ps.stdout.splitlines():
            c = json.loads(l)
            containers.append({"name": c.get("Names"), "image": c.get("Image"), "state": c.get("State"),
                               "status": c.get("Status"), "host": h or "local"})
        for l in im.stdout.splitlines():
            i = json.loads(l)
            images.append({"repo": i.get("Repository"), "tag": i.get("Tag"), "size": i.get("Size"),
                           "created": i.get("CreatedSince"), "host": h or "local"})
    with LOCK:
        STATE["docker"] = {"containers": containers, "images": images} if found else None


GHCR_RE = r"ghcr\.io/[A-Za-z0-9_.-]+/[A-Za-z0-9_.{}-]+"


def _http_json(url, headers=None):
    import urllib.request
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _semver_key(t):
    import re
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)$", t)
    return tuple(int(x) for x in m.groups()) if m else None


def ghcr_info(ref):
    """Tags und Größe eines öffentlichen ghcr.io-Images, anonym über die Registry-API."""
    name = ref.split("ghcr.io/", 1)[1].lower()
    tok = _http_json(f"https://ghcr.io/token?scope=repository:{name}:pull")["token"]
    auth = {"Authorization": f"Bearer {tok}"}
    tags = _http_json(f"https://ghcr.io/v2/{name}/tags/list", auth).get("tags") or []
    vers = sorted((t for t in tags if _semver_key(t)), key=_semver_key)
    newest = vers[-1] if vers else ("latest" if "latest" in tags else (tags[-1] if tags else None))
    size = None
    if newest:
        acc = ", ".join(["application/vnd.oci.image.index.v1+json",
                         "application/vnd.docker.distribution.manifest.list.v2+json",
                         "application/vnd.oci.image.manifest.v1+json",
                         "application/vnd.docker.distribution.manifest.v2+json"])
        m = _http_json(f"https://ghcr.io/v2/{name}/manifests/{newest}", {**auth, "Accept": acc})
        if "manifests" in m:
            sub = next((x for x in m["manifests"] if x.get("platform", {}).get("architecture") == "amd64"),
                       m["manifests"][0])
            m = _http_json(f"https://ghcr.io/v2/{name}/manifests/{sub['digest']}", {**auth, "Accept": acc})
        size = sum(l.get("size", 0) for l in m.get("layers", []))
    return {"tags": len(vers) or len(tags), "tag": newest, "bytes": size}


def scan_images():
    """Images aus den Repos: ghcr.io-Verweise in getrackten Dateien plus Repos mit Dockerfile."""
    import re
    with LOCK:
        repos = [(r["id"], r["path"]) for r in STATE["repos"]]
    refs, dockerfile_only = {}, []
    for rid, path in repos:
        found = set(m.lower().rstrip(".") for m in
                    re.findall(GHCR_RE, git(path, "grep", "-hoE", GHCR_RE.replace("\\", "\\"))))
        found = {f for f in found if not f.endswith("/") and f.count("/") == 2 and "$" not in f}
        for f in found:
            # HA-Apps: ghcr.io/<owner>/{arch}-name -> je Architektur ein Image
            for g in ([f.replace("{arch}", a) for a in ("amd64", "aarch64")] if "{arch}" in f else [f]):
                if "{" not in g:
                    refs.setdefault(g, set()).add(rid)
        dfs = [l for l in git(path, "ls-files").splitlines() if l.rsplit("/", 1)[-1].startswith("Dockerfile")]
        if dfs and not found:
            dockerfile_only.append({"repo": rid, "tag": "Dockerfile", "dockerfiles": len(dfs),
                                    "source": [rid], "host": "local", "size": "", "created": ""})
    images = []
    for ref, src in sorted(refs.items()):
        info = {"tags": 0, "tag": None, "bytes": None}
        try:
            info = ghcr_info(ref)
        except Exception as e:
            print("ghcr", ref, e, flush=True)
        if not info["tag"]:
            continue  # Verweis ohne veröffentlichtes Image (z. B. Beispiel in einer Doku)
        mb = (info["bytes"] or 0) / 1e6
        images.append({"repo": ref.split("/", 2)[2], "ref": ref, "tag": info["tag"], "tags": info["tags"],
                       "size": f"{mb:.0f}MB" if info["bytes"] else "", "created": "",
                       "source": sorted(src), "host": "ghcr.io"})
    with LOCK:
        STATE["images"] = images + dockerfile_only


def repo_for_cwd(cwd):
    if cwd in REPO_CACHE:
        return REPO_CACHE[cwd]
    common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    name = None
    if common:
        d = Path(common).parent.name
        name = CFG["aliases"].get(d, d)
    REPO_CACHE[cwd] = name
    return name


def on_event(ev):
    sid = ev.get("session_id")
    if not sid:
        return
    kind = ev.get("hook_event_name", "")
    now = time.time()
    with LOCK:
        s = SESSIONS.setdefault(sid, {"id": sid[:8], "state": "idle", "tool": None, "subagents": 0})
        s["repo"] = repo_for_cwd(ev.get("cwd") or "") or s.get("repo")
        s["seen"] = now
        if kind == "SessionEnd":
            SESSIONS.pop(sid, None)
        elif kind in ("PreToolUse", "PostToolUse", "UserPromptSubmit"):
            s["state"], s["tool"] = "working", ev.get("tool_name") or s.get("tool")
        elif kind == "Notification":
            s["state"] = "waiting"
        elif kind == "Stop":
            s["state"], s["subagents"] = "idle", 0
        elif kind == "SubagentStart":
            s["subagents"] += 1
        elif kind == "SubagentStop":
            s["subagents"] = max(0, s["subagents"] - 1)
        waiting = sum(1 for v in SESSIONS.values() if v["state"] == "waiting")
    push_waiting(waiting)


def push_waiting(n):
    """Meldet die Zahl wartender Sitzungen an Home Assistant (nur bei Änderung)."""
    global _last_waiting
    ha = CFG.get("home_assistant")
    if not ha or n == _last_waiting:
        return
    _last_waiting = n
    # curl statt urllib: macOS verweigert Python unter launchd den Zugriff aufs lokale Netz
    cmd = ["curl", "-s", "-m", "3", "-X", "POST", "-H", "Content-Type: application/json",
           "-d", json.dumps({"waiting": n}), f"{ha['url']}/api/webhook/{ha['webhook_id']}"]
    threading.Thread(target=lambda: _quiet(subprocess.run, cmd, capture_output=True, timeout=5),
                     daemon=True).start()


def _quiet(fn, *a, **k):
    try:
        fn(*a, **k)
    except Exception as e:
        print("HA nicht erreichbar:", e, flush=True)


def snapshot():
    now = time.time()
    with LOCK:
        for k in [k for k, v in SESSIONS.items() if now - v["seen"] > 4 * 3600]:
            SESSIONS.pop(k)
        sess = [dict(v) for v in SESSIONS.values() if v.get("repo")]
        repos = []
        for r in STATE["repos"]:
            r = dict(r)
            r["ci"] = CI.get(r["id"])
            repos.append(r)
        return {"repos": repos, "links": STATE["links"], "releases": STATE["releases"],
                "docker": {"containers": (STATE["docker"] or {}).get("containers", []),
                           "images": STATE.get("images", []) + (STATE["docker"] or {}).get("images", []),
                           "host": STATE["docker"] is not None},
                "sessions": sess, "events": list(EVENTS), "updated": datetime.now(timezone.utc).isoformat()}


def loop(fn, every):
    while True:
        try:
            fn()
        except Exception as e:  # weiterlaufen, aber sichtbar
            print("Fehler in", fn.__name__, e, flush=True)
        time.sleep(every)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(HERE / "web"), **k)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/state"):
            body = json.dumps(snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def do_POST(self):
        if self.path != "/event":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            on_event(json.loads(self.rfile.read(n) or b"{}"))
        except Exception:
            pass
        self.send_response(204)
        self.end_headers()


if __name__ == "__main__":
    scan_git()
    threading.Thread(target=loop, args=(scan_git, CFG["git_poll_seconds"]), daemon=True).start()
    threading.Thread(target=loop, args=(scan_ci, CFG["ci_poll_seconds"]), daemon=True).start()
    threading.Thread(target=loop, args=(scan_docker, 60), daemon=True).start()
    threading.Thread(target=loop, args=(scan_images, 900), daemon=True).start()
    host = os.environ.get("WERKSTADT_HOST", CFG["host"])
    if not CONFIG_FILE.exists():
        print("Keine werkstadt.local.json gefunden, nutze Standardwerte. Einrichtung: python3 setup.py", flush=True)
    print(f"Werkstadt läuft auf http://localhost:{CFG['port']}", flush=True)
    ThreadingHTTPServer((host, CFG["port"]), Handler).serve_forever()
