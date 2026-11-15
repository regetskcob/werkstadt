#!/usr/bin/env python3
"""Werkstadt-Einrichtung: fragt das Nötige ab, schreibt werkstadt.local.json und richtet auf Wunsch
Claude-Code-Hooks, Autostart und die Home-Assistant-Anbindung ein. Nur Standardbibliothek.

  python3 setup.py              interaktiv
  python3 setup.py --defaults   ohne Fragen: nur Konfiguration mit erkannten Werten, nichts installieren
  python3 setup.py --check      zeigt, was eingerichtet ist (JSON)
  python3 setup.py --uninstall  entfernt Hooks und Autostart wieder
"""
import json, os, platform, secrets, shutil, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "werkstadt.local.json"
HOOK = HERE / "tool" / "hook.sh"
SETTINGS = Path.home() / ".claude" / "settings.json"
EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "Notification", "SubagentStart", "SubagentStop",
          "Stop", "SessionEnd"]
LABEL = "werkstadt.collector"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
UNIT = Path.home() / ".config" / "systemd" / "user" / "werkstadt.service"
DE = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").lower().startswith("de")


def t(de, en):
    return de if DE else en


def run(*cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except Exception:
        return 1, ""


def ask(q, default=""):
    try:
        a = input(f"{q} [{default}] " if default else f"{q} ").strip()
    except EOFError:
        a = ""
    return a or default


def yes(q, default=True):
    a = ask(f"{q} ({t('j/n', 'y/n')})", t("j", "y") if default else "n").lower()
    return a[:1] in ("j", "y")


def head(txt):
    print(f"\n\033[1m{txt}\033[0m")


# ---------- Erkennen ----------
def find_repos(roots):
    out = {}
    for root in roots:
        root = Path(os.path.expanduser(root))
        if not root.is_dir():
            continue
        for d in list(root.glob("*/")) + list(root.glob("*/*/")):
            if (d / ".git").is_dir() and not d.name.startswith("wt-"):
                out[d.name] = d
    return out


def guess_roots():
    cands = ["~/dev", "~/code", "~/Code", "~/src", "~/Projects", "~/projects", "~/repos", "~/git", "~/workspace",
             "~/Developer"]
    found = [(len(find_repos([c])), c) for c in cands]
    found = [c for n, c in sorted(found, reverse=True) if n > 0]
    return found[:1] or ["~/dev"]


def slug(path):
    _, url = run("git", "-C", str(path), "remote", "get-url", "origin")
    if "github.com" not in url:
        return None
    s = url.split("github.com")[-1].lstrip(":/")
    return s[:-4] if s.endswith(".git") else s


def public_repos(repos):
    """Öffentliche GitHub-Repos als Vorschlag für Open-Source-Kerne (braucht gh, sonst leer)."""
    if not shutil.which("gh"):
        return []
    pub = []
    for name, path in repos.items():
        s = slug(path)
        if not s:
            continue
        code, out = run("gh", "repo", "view", s, "--json", "visibility", "-q", ".visibility")
        if code == 0 and out == "PUBLIC":
            pub.append(name)
    return pub


def names(txt, known):
    got = [x.strip() for x in txt.replace(";", ",").split(",") if x.strip()]
    bad = [x for x in got if x not in known]
    if bad:
        print("  " + t("Unbekannt, ignoriert: ", "Unknown, ignored: ") + ", ".join(bad))
    return [x for x in got if x in known]


# ---------- Hooks ----------
def hook_cmd():
    return str(HOOK)


def hooks_installed():
    try:
        h = json.loads(SETTINGS.read_text()).get("hooks", {})
    except Exception:
        return False
    return all(any(x.get("command") == hook_cmd() for g in h.get(e, []) for x in g.get("hooks", [])) for e in EVENTS)


def _ours(cmd):
    return cmd == hook_cmd() or ("werkstadt" in cmd and cmd.endswith("hook.sh"))


def install_hooks(remove=False):
    data = {}
    if SETTINGS.exists():
        data = json.loads(SETTINGS.read_text())
        shutil.copy(SETTINGS, SETTINGS.with_name(f"settings.json.bak-werkstadt-{int(time.time())}"))
    elif remove:
        return
    hooks = data.setdefault("hooks", {})
    for e in EVENTS:
        groups = hooks.get(e, [])
        for g in groups:  # eigene Einträge entfernen, fremde behalten
            g["hooks"] = [x for x in g.get("hooks", []) if not _ours(x.get("command", ""))]
        groups = [g for g in groups if g.get("hooks")]
        if not remove:
            entry = {"hooks": [{"type": "command", "command": hook_cmd(), "async": True}]}
            if e == "PreToolUse":
                entry["matcher"] = "*"
            groups.append(entry)
        if groups:
            hooks[e] = groups
        else:
            hooks.pop(e, None)
    if not hooks:
        data.pop("hooks", None)
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# ---------- Autostart ----------
def autostart_installed():
    return PLIST.exists() or UNIT.exists()


def install_autostart():
    py = sys.executable
    path = os.pathsep.join(dict.fromkeys(
        [str(Path(p).parent) for p in (shutil.which("git"), shutil.which("gh"), shutil.which("docker"),
                                       shutil.which("curl")) if p] + ["/usr/bin", "/bin"]))
    if platform.system() == "Darwin":
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array><string>{py}</string><string>{HERE / 'collector.py'}</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path}</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/werkstadt.log</string><key>StandardErrorPath</key><string>/tmp/werkstadt.log</string>
</dict></plist>
""")
        run("launchctl", "unload", str(PLIST))
        return run("launchctl", "load", str(PLIST))[0] == 0
    if platform.system() == "Linux" and shutil.which("systemctl"):
        UNIT.parent.mkdir(parents=True, exist_ok=True)
        UNIT.write_text(f"""[Unit]
Description=Werkstadt collector

[Service]
ExecStart={py} {HERE / 'collector.py'}
Environment=PATH={path}
Restart=always

[Install]
WantedBy=default.target
""")
        run("systemctl", "--user", "daemon-reload")
        return run("systemctl", "--user", "enable", "--now", "werkstadt.service")[0] == 0
    return False


def remove_autostart():
    if PLIST.exists():
        run("launchctl", "unload", str(PLIST))
        PLIST.unlink()
    if UNIT.exists():
        run("systemctl", "--user", "disable", "--now", "werkstadt.service")
        UNIT.unlink()


# ---------- Prüfen ----------
def reachable(port):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def check():
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    port = cfg.get("port", 7777)
    return {"config": CONFIG.exists(), "config_file": str(CONFIG), "roots": cfg.get("roots"),
            "repos_found": len(find_repos(cfg.get("roots", []))) if cfg else 0,
            "collector_running": reachable(port), "url": f"http://localhost:{port}",
            "claude_hooks": hooks_installed(), "autostart": autostart_installed(),
            "home_assistant": bool(cfg.get("home_assistant")),
            "tools": {x: bool(shutil.which(x)) for x in ("git", "gh", "docker", "curl")}}


# ---------- Ablauf ----------
def main():
    if "--check" in sys.argv:
        print(json.dumps(check(), indent=2))
        return
    if "--uninstall" in sys.argv:
        install_hooks(remove=True)
        remove_autostart()
        print(t("Hooks und Autostart entfernt. werkstadt.local.json bleibt liegen.",
                "Hooks and autostart removed. werkstadt.local.json was kept."))
        return
    auto = "--defaults" in sys.argv
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}

    head("Werkstadt")
    print(t("Die Karte zeigt deine Git-Repos als kleine Stadt. Diese Einrichtung dauert zwei Minuten;\n"
            "Enter übernimmt jeweils den Vorschlag in eckigen Klammern.",
            "The map shows your git repos as a small town. Setup takes two minutes;\n"
            "press Enter to accept the suggestion in brackets."))
    for tool, why in (("git", t("nötig", "required")), ("curl", t("nötig für Hooks und Home Assistant", "needed for hooks and Home Assistant")),
                      ("gh", t("optional: CI-Status und öffentliche Repos", "optional: CI status and public repos")),
                      ("docker", t("optional: Container am Hafen", "optional: containers in the harbour"))):
        print(f"  {'✓' if shutil.which(tool) else '–'} {tool}  ({why})")
    if not shutil.which("git"):
        sys.exit(t("git fehlt, Abbruch.", "git is missing, aborting."))

    head(t("1. Wo liegen deine Repos?", "1. Where are your repos?"))
    roots = cfg.get("roots") or guess_roots()
    if not auto:
        roots = [r.strip() for r in ask(t("Ordner, mehrere mit Komma", "Folders, comma separated"), ", ".join(roots)).split(",") if r.strip()]
    folders = find_repos(roots)
    aliases = cfg.get("aliases", {})
    repos = {aliases.get(n, n): p for n, p in folders.items()}  # Namen wie auf der Karte
    print("  " + t(f"{len(repos)} Repos gefunden", f"found {len(repos)} repos") + (": " + ", ".join(sorted(repos)[:12]) + (" …" if len(repos) > 12 else "") if repos else ""))
    if not repos and not auto and not yes(t("Trotzdem weiter?", "Continue anyway?"), False):
        return

    skip, oss, werk, links = cfg.get("skip", []), cfg.get("oss"), cfg.get("werk", []), cfg.get("links", [])
    if oss is None:
        print("  " + t("Suche öffentliche Repos (gh) …", "Looking for public repos (gh) …"))
        oss = public_repos(repos)
    if not auto:
        head(t("2. Was soll wie erscheinen?", "2. What should show up, and how?"))
        skip = names(ask(t("Repos ausblenden (Ordnernamen, Komma)", "Hide repos (folder names, comma)"), ", ".join(skip)), folders)
        oss = names(ask(t("Open-Source-Kerne (Leuchtturm am Rand)", "Open-source cores (lighthouse at the edge)"), ", ".join(oss)), repos)
        werk = names(ask(t("Gemeinsame Werkzeug-Repos (Fabrik)", "Shared tooling repos (factory)"), ", ".join(werk)), repos)
        print(t("  Straßen: Welches Repo liefert Änderungen in welches? Form: quelle>ziel, quelle>ziel",
                "  Roads: which repo feeds changes into which? Form: source>target, source>target"))
        raw = ask(t("Verbindungen", "Links"), ", ".join(f"{a}>{b}" for a, b in links))
        links = []
        for pair in raw.split(","):
            if ">" in pair:
                a, b = (x.strip() for x in pair.split(">", 1))
                if a in repos and b in repos:
                    links.append([a, b])
                else:
                    print("  " + t("Unbekannt, ignoriert: ", "Unknown, ignored: ") + pair.strip())

    port, host = cfg.get("port", 7777), cfg.get("host", "127.0.0.1")
    ha = cfg.get("home_assistant")
    if not auto:
        head(t("3. Erreichbarkeit", "3. Access"))
        while True:
            answer = ask("Port", str(port))
            if answer.isdigit() and 0 < int(answer) < 65536:
                port = int(answer)
                break
            print("  " + t("Bitte eine Zahl zwischen 1 und 65535.", "Please enter a number between 1 and 65535."))
        lan = yes(t("Im Heimnetz erreichbar machen (Tablet, Home Assistant)? Sonst nur auf diesem Rechner.",
                    "Make it reachable in your home network (tablet, Home Assistant)? Otherwise this machine only."), host != "127.0.0.1")
        host = "0.0.0.0" if lan else "127.0.0.1"
        if lan and yes(t("Zahl wartender Claude-Sitzungen an Home Assistant melden?",
                         "Report the number of waiting Claude sessions to Home Assistant?"), bool(ha)):
            url = ask(t("Adresse von Home Assistant", "Home Assistant address"), (ha or {}).get("url", "http://homeassistant.local:8123")).rstrip("/")
            ha = {"url": url, "webhook_id": (ha or {}).get("webhook_id") or "werkstadt-" + secrets.token_hex(12)}
        else:
            ha = None

    cfg.update(roots=roots, skip=skip, oss=oss, werk=werk, links=links, port=port, host=host)
    if ha:
        cfg["home_assistant"] = ha
    else:
        cfg.pop("home_assistant", None)
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    print("\n  ✓ " + t("Konfiguration geschrieben: ", "Config written: ") + str(CONFIG))
    if auto:
        print(t("  Start: python3 collector.py", "  Start: python3 collector.py"))
        return

    head(t("4. Claude-Code-Sitzungen als Figuren", "4. Claude Code sessions as figures"))
    print(t(f"  Dafür kommen Hooks in {SETTINGS}. Jede Sitzung schickt dann bei jedem Werkzeugaufruf ein\n"
            "  kurzes Signal an diesen Rechner (nur Sitzung, Repo, Werkzeugname; keine Inhalte). Vorher wird\n"
            "  eine Sicherung der Datei angelegt.",
            f"  This adds hooks to {SETTINGS}. Every session then sends a short local signal on each tool\n"
            "  call (session, repo and tool name only; no content). A backup of the file is made first."))
    if yes(t("Hooks eintragen?", "Install hooks?"), True):
        install_hooks()
        print("  ✓ " + t("Hooks eingetragen. Sie gelten für neu gestartete Sitzungen.", "Hooks installed. They apply to newly started sessions."))

    head(t("5. Autostart", "5. Autostart"))
    started = False
    if yes(t("Beim Anmelden automatisch starten?", "Start automatically at login?")):
        started = install_autostart()
        print("  ✓ " + t("Autostart eingerichtet.", "Autostart installed.") if started else
              "  – " + t("Autostart wird auf diesem System nicht unterstützt. Start von Hand: python3 collector.py",
                         "Autostart is not supported on this system. Start manually: python3 collector.py"))
    if started:
        for _ in range(20):
            if reachable(port):
                break
            time.sleep(.5)

    head(t("Fertig", "Done"))
    print(f"  {t('Karte', 'Map')}: http://localhost:{port}" + ("" if started and reachable(port) else t("   (erst starten: python3 collector.py)", "   (start first: python3 collector.py)")))
    print(f"  {t('Vollbild fürs Tablet', 'Full screen for a tablet')}: http://<{t('rechner', 'host')}>:{port}/?tv")
    if ha:
        print(t("\n  Home Assistant, einmal von Hand:\n"
                "   a) Helfer anlegen: Zahl „Werkstadt wartende Sitzungen“ (0–50)\n"
                "   b) Automation anlegen, im YAML-Modus einfügen:",
                "\n  Home Assistant, once by hand:\n"
                "   a) Create a helper: number \"Werkstadt waiting sessions\" (0–50)\n"
                "   b) Create an automation, paste in YAML mode:"))
        print(f"""      triggers:
        - trigger: webhook
          webhook_id: {ha['webhook_id']}
          allowed_methods: [POST]
          local_only: true
      actions:
        - action: input_number.set_value
          target: {{entity_id: input_number.werkstadt_wartende_sitzungen}}   # {t("Entität deines Helfers", "your helper's entity")}
          data: {{value: "{{{{ trigger.json.waiting | int(0) }}}}"}}""")
        print(t(f"   c) Dashboard mit Webseiten-Karte auf http://<rechner>:{port}/?tv",
                f"   c) Dashboard with a webpage card on http://<host>:{port}/?tv"))
    print(t(f"\n  Für Claude als MCP-Server (Status abfragen):\n   claude mcp add werkstadt -- {sys.executable} {HERE / 'mcp_server.py'}",
            f"\n  For Claude as an MCP server (query status):\n   claude mcp add werkstadt -- {sys.executable} {HERE / 'mcp_server.py'}"))
    print(t("  Wieder entfernen: python3 setup.py --uninstall", "  Remove again: python3 setup.py --uninstall"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
