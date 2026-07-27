#!/usr/bin/env python3
"""
Query helper for config/config.yaml.
Called from run.sh (Linux) and bat scripts (Windows).
Resolves config path relative to this script: ../config/config.yaml
"""
import os
import sys

try:
    import yaml
except ImportError:
    import subprocess
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", "pyyaml"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    import yaml

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SHARED_DIR = os.path.dirname(_SCRIPTS_DIR)
CONFIG_PATH = os.path.join(_SHARED_DIR, "config", "config.yaml")
# Optional per-VM terminal filter. When a container bind-mounts a file here,
# only terminals whose "broker account instance" line appears in it are run
# by this VM. File absent => no filter (single-VM behavior, unchanged).
VM_GROUP_PATH = os.path.join(_SHARED_DIR, "config", "vm-group.txt")
DEFAULT_INSTANCE = "default"


def _load():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _vm_group_filter():
    if not os.path.exists(VM_GROUP_PATH):
        return None
    allowed = set()
    with open(VM_GROUP_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) == 2:
                parts.append(DEFAULT_INSTANCE)
            allowed.add(tuple(parts[:3]))
    return allowed


def _in_group(terminal, allowed):
    if allowed is None:
        return True
    key = (
        terminal["broker"],
        terminal["account"],
        _normalize_instance(terminal.get("instance")),
    )
    return key in allowed


def _normalize_instance(value):
    if value in (None, ""):
        return DEFAULT_INSTANCE
    return str(value).strip() or DEFAULT_INSTANCE


def _route_prefixes(terminal):
    broker = terminal["broker"]
    account = terminal["account"]
    instance = _normalize_instance(terminal.get("instance"))
    prefixes = [f"/{broker}/{account}/{instance}/"]
    if instance == DEFAULT_INSTANCE:
        prefixes.append(f"/{broker}/{account}/")
    return prefixes


def main():
    if len(sys.argv) < 2:
        print("Usage: config_helper.py <cmd> [args...]", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]

    try:
        cfg = _load()
    except FileNotFoundError:
        print(f"ERROR: config.yaml not found at {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)

    if cmd == "terminals":
        allowed = _vm_group_filter()
        for t in cfg.get("terminals") or []:
            if not _in_group(t, allowed):
                continue
            utc = t.get("utc_offset")
            utc = "0" if utc is None else str(utc).replace(" ", "")
            mode = (t.get("mode") or "live").strip().lower() or "live"
            instance = _normalize_instance(t.get("instance"))
            if mode not in ("live", "backtest"):
                mode = "live"
            print(t["broker"], t["account"], instance, t["port"], utc, mode)

    elif cmd == "ports":
        ports = [t["port"] for t in (cfg.get("terminals") or [])]
        if not ports:
            print("6542")
        elif min(ports) == max(ports):
            print(str(min(ports)))
        else:
            print(f"{min(ports)}-{max(ports)}")

    elif cmd == "port_list":
        ports = [t["port"] for t in (cfg.get("terminals") or [])]
        print(" ".join(str(p) for p in ports) if ports else "6542")

    elif cmd == "api_token":
        print(cfg.get("api_token") or "")

    elif cmd == "ts_auth_key":
        print((cfg.get("tailscale") or {}).get("auth_key") or "")

    elif cmd == "ts_login_server":
        print((cfg.get("tailscale") or {}).get("login_server") or "")

    elif cmd == "reboot_interval":
        val = cfg.get("reboot_interval")
        print(30 if val is None else val)

    elif cmd == "requirements":
        for r in cfg.get("requirements") or []:
            print(r)

    elif cmd == "write_ini":
        if len(sys.argv) < 5:
            print("Usage: config_helper.py write_ini <broker> <account> <outpath>", file=sys.stderr)
            sys.exit(1)
        broker, account, outpath = sys.argv[2], sys.argv[3], sys.argv[4]
        accounts = cfg.get("accounts", {})
        b = accounts.get(broker, {})
        creds = b.get(account) if account else next(iter(b.values()), None) if b else None
        ini = "[Common]\n"
        if creds:
            ini += f"Login={creds['login']}\n"
            ini += f"Server={creds['server']}\n"
            ini += f"Password={creds['password']}\n"
        ini += "KeepPrivate=0\nAutoTrading=1\nNewsEnable=0\n"
        ini += "[Experts]\nAllowLiveTrading=1\nAllowDllImport=1\nEnabled=1\n"
        ini += "[Email]\nEnable=0\n"
        with open(outpath, "w", encoding="utf-8") as f:
            f.write(ini)

    elif cmd == "nginx_conf":
        if len(sys.argv) < 3:
            print("Usage: config_helper.py nginx_conf <outpath>", file=sys.stderr)
            sys.exit(1)
        outpath = sys.argv[2]
        terms = cfg.get("terminals", [])
        # Terminals listed in data/vm-group-b.txt are served by the mt5-b
        # container (VM-B); everything else stays on mt5 (VM-A).
        b_group = set()
        b_path = os.path.join(_SHARED_DIR, "data", "vm-group-b.txt")
        if os.path.exists(b_path):
            with open(b_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split()
                    if len(parts) == 2:
                        parts.append(DEFAULT_INSTANCE)
                    b_group.add(tuple(parts[:3]))
        locs = []
        for t in terms:
            key = (t["broker"], t["account"], _normalize_instance(t.get("instance")))
            host = "mt5-b" if key in b_group else "mt5"
            for p in _route_prefixes(t):
                # proxy_pass via a $variable (not a literal host) so nginx
                # resolves the upstream at REQUEST time using the resolver
                # below, instead of caching the IP at startup. This survives a
                # VM container being recreated (new IP) or temporarily down
                # without nginx 502-ing forever or failing to start (the two
                # traps hit on 2026-07-20). The rewrite+break leaves the
                # stripped path in $uri, which a URI-less proxy_pass forwards.
                locs.append(
                    f"        location {p} {{\n"
                    f"            set $backend \"{host}\";\n"
                    f"            rewrite ^{p}(.*)$ /$1 break;\n"
                    f"            proxy_pass http://$backend:{t['port']};\n"
                    f"            proxy_set_header Host $host;\n"
                    f"            proxy_set_header X-Forwarded-For $remote_addr;\n"
                    f"        }}"
                )
        nginx_conf = (
            "events {}\n"
            "http {\n"
            "    server {\n"
            "        listen 80;\n"
            # docker's embedded DNS; valid=10s re-resolves upstream IPs so a
            # recreated VM container is picked up without an nginx restart.
            "        resolver 127.0.0.11 valid=10s ipv6=off;\n"
            "        client_max_body_size 25m;\n"
            "        client_body_timeout 120s;\n"
            + "\n".join(locs) + "\n"
            "        location / { return 404 \"no route\\n\"; }\n"
            "    }\n"
            "}\n"
        )
        with open(outpath, "w", encoding="utf-8") as f:
            f.write(nginx_conf)

    elif cmd == "show_terminals":
        for t in cfg.get("terminals", []):
            instance = _normalize_instance(t.get("instance"))
            print(f"  - /{t['broker']}/{t['account']}/{instance}/")

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
