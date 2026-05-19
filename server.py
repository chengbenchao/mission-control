#!/usr/bin/env python3
"""
Mission Control — Universal Server Dashboard
============================================
Zero hardcoded service names. Auto-discovers everything from systemd + ss.
Optional config.json for URL mappings and infra classification.

Usage:
    python3 server.py                  # default port 8880
    PORT=9999 python3 server.py        # custom port
    CONFIG=my-config.json python3 ...  # custom config path
"""

import json
import os
import re
import subprocess
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path

PORT = int(os.environ.get("PORT", 8880))
HOST = os.environ.get("HOST", "127.0.0.1")
PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG", str(PROJECT_DIR / "config.json")))


# ═══════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════

def load_config():
    """Load optional config.json, return defaults if missing."""
    defaults = {
        "infra_patterns": [],
        "service_urls": {},
    }
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                loaded = json.load(f)
            defaults.update(loaded)
        except Exception:
            pass
    return defaults


# ═══════════════════════════════════════════════════════════════
#  Service Discovery (fully dynamic)
# ═══════════════════════════════════════════════════════════════

def run(*args, timeout=5):
    """Run a command, return stdout or '' on failure."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def get_systemd_services():
    """Discover all running systemd services (user + system)."""
    services = {}
    for scope in ["--user", "--system"]:
        out = run("systemctl", scope, "list-units", "--type=service",
                  "--state=running", "--no-legend", "--no-pager")
        for line in out.split("\n"):
            line = line.strip()
            if not line:
                continue
            name = line.split()[0]
            if name.endswith(".service"):
                svc = name[:-8]  # strip .service
                # Prefer user service if duplicate
                if svc not in services:
                    services[svc] = {"type": "systemd", "name": svc, "scope": scope}
    return services


def get_systemd_mainpid(svc_name):
    """Get MainPID for a systemd service. Tries user first, then system."""
    services_db = get_systemd_services()
    scope = services_db.get(svc_name, {}).get("scope", "--user")
    out = run("systemctl", scope, "show", f"{svc_name}.service",
              "--property=MainPID")
    if out and "=" in out:
        pid = out.split("=")[-1]
        if pid and pid != "0":
            return int(pid)
    return None


def get_listening_ports():
    """Return {port: pid} for all listening ports."""
    out = run("ss", "-tlnp")
    ports = {}
    for line in out.split("\n")[1:]:
        parts = line.split()
        if len(parts) < 5:
            continue
        addr = parts[4]
        proc = parts[-1] if len(parts) > 5 else ""
        port_str = addr.rsplit(":", 1)[-1]
        if not port_str.isdigit():
            continue
        port = int(port_str)
        # Extract PID from ss output like: users:(("python3",pid=123,fd=4))
        m = re.search(r"pid=(\d+)", proc)
        if m:
            ports[port] = int(m.group(1))
    return ports


def get_pid_ports():
    """Return {pid: [ports]} mapping."""
    out = run("ss", "-tlnp")
    pid_ports = {}
    for line in out.split("\n")[1:]:
        parts = line.split()
        if len(parts) < 5:
            continue
        addr = parts[4]
        proc = parts[-1] if len(parts) > 5 else ""
        port_str = addr.rsplit(":", 1)[-1]
        if not port_str.isdigit():
            continue
        port = int(port_str)
        m = re.search(r"pid=(\d+)", proc)
        if m:
            pid = int(m.group(1))
            pid_ports.setdefault(pid, []).append(port)
    return pid_ports


def match_service_to_port(svc_name, svc_pid, pid_ports):
    """Match a systemd service to its listening port(s)."""
    if svc_pid and svc_pid in pid_ports:
        ports = pid_ports[svc_pid]
        # Prefer the lowest non-localhost port, then any port
        public = [p for p in ports if p > 1024]
        if public:
            return public[0]
        return ports[0] if ports else None

    # Fallback: grep the service file for port hints
    out = run("systemctl", "--user", "cat", f"{svc_name}.service")
    for line in out.split("\n"):
        m = re.search(r"(?:port|PORT|--port|:)(\d{4,5})", line)
        if m:
            return int(m.group(1))
    return None


def get_process_memory(pid):
    """Get RSS memory in MB."""
    if not pid:
        return None
    out = run("ps", "-o", "rss=", "-p", str(pid))
    if out and out.strip().isdigit():
        return round(int(out.strip()) / 1024, 1)
    return None


def get_all_services():
    """Auto-discover all services: systemd + orphaned port listeners."""
    cfg = load_config()
    infra_patterns = cfg.get("infra_patterns", [])
    url_map = cfg.get("service_urls", {})

    systemd_svcs = get_systemd_services()
    pid_ports = get_pid_ports()
    all_ports = get_listening_ports()

    svc_list = []
    matched_pids = set()

    # Process systemd services
    for name, svc in systemd_svcs.items():
        pid = get_systemd_mainpid(name)
        port = match_service_to_port(name, pid, pid_ports)

        if pid:
            matched_pids.add(pid)

        # Determine if this is "infra" based on config patterns
        is_infra = any(re.search(pat, name, re.IGNORECASE) for pat in infra_patterns)

        svc_list.append({
            "name": name,
            "type": "systemd",
            "port": port,
            "pid": pid,
            "memory": get_process_memory(pid),
            "status": "healthy" if pid else "error",
            "is_infra": is_infra,
            "url": url_map.get(name),
        })

    # Discover orphaned port listeners (not matched to any systemd service)
    for port, pid in all_ports.items():
        if pid in matched_pids:
            continue
        # Try to get process name
        proc_name = ""
        out = run("ps", "-p", str(pid), "-o", "comm=")
        if out:
            proc_name = out.strip()

        # Skip system processes and known non-services
        if proc_name in ("sshd", "systemd", "systemd-", "mihomo"):
            # For mihomo, still show it if it's on a web port
            if proc_name == "mihomo" and port in (9090,):
                pass
            elif port < 1024:  # system ports
                continue

        name = proc_name or f"pid-{pid}"
        is_infra = any(re.search(pat, name, re.IGNORECASE) for pat in infra_patterns)

        svc_list.append({
            "name": name,
            "type": "direct",
            "port": port,
            "pid": pid,
            "memory": get_process_memory(pid),
            "status": "healthy",
            "is_infra": is_infra,
            "url": url_map.get(name),
        })

    # Sort: infra first, then system
    svc_list.sort(key=lambda s: (not s["is_infra"], s["name"]))

    # Re-check health for systemd services
    for svc in svc_list:
        if svc["type"] == "systemd":
            svc["status"] = check_health(svc)

    return svc_list


def check_health(svc):
    """Check if a service is healthy via systemd + port."""
    name = svc["name"]
    if svc["type"] == "systemd":
        out = run("systemctl", "--user", "is-active", f"{name}.service")
        if out != "active":
            return "error"

    port = svc.get("port")
    if port:
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            result = s.connect_ex(("127.0.0.1", port))
            s.close()
            if result == 0:
                return "healthy"
            return "error"
        except Exception:
            return "unknown"

    return "healthy" if svc["type"] == "systemd" else "unknown"


# ═══════════════════════════════════════════════════════════════
#  Resource Monitoring
# ═══════════════════════════════════════════════════════════════

def get_resources():
    """CPU / RAM / Disk / Uptime from /proc."""
    cpu = 0
    try:
        with open("/proc/loadavg") as f:
            n_cpu = os.cpu_count() or 1
            cpu = round(float(f.read().split()[0]) * 100 / n_cpu, 1)
    except Exception:
        pass

    ram_used = ram_total = ram_pct = 0
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                if "MemTotal" in line:
                    mem["total"] = int(line.split()[1])
                elif "MemAvailable" in line:
                    mem["avail"] = int(line.split()[1])
            ram_total = mem.get("total", 0) // 1024
            ram_avail = mem.get("avail", 0) // 1024
            ram_used = ram_total - ram_avail
            ram_pct = round((ram_used / ram_total) * 100, 1) if ram_total else 0
    except Exception:
        pass

    disk_total = disk_used = disk_pct = 0
    try:
        out = run("df", "-BG", "/")
        lines = out.split("\n")
        if len(lines) > 1:
            parts = lines[1].split()
            disk_total = int(parts[1].replace("G", ""))
            disk_used = int(parts[2].replace("G", ""))
            disk_pct = int(parts[4].replace("%", ""))
    except Exception:
        pass

    uptime = 0
    try:
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
    except Exception:
        pass

    return {
        "cpu": cpu,
        "ram": {"total": round(ram_total / 1024, 1), "used": round(ram_used / 1024, 1), "percent": ram_pct},
        "disk": {"total": disk_total, "used": disk_used, "percent": disk_pct},
        "uptime": uptime,
    }


# ═══════════════════════════════════════════════════════════════
#  Logs & Service Control
# ═══════════════════════════════════════════════════════════════

def get_logs(svc_name, lines=50):
    """Get recent journalctl logs for any systemd service."""
    out = run("journalctl", "--user", "-u", f"{svc_name}.service",
              "-n", str(lines), "--no-pager", timeout=5)
    return out[-5000:] if out else "No logs available"


def control_service(svc_name, action):
    """Start/stop/restart a systemd service."""
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "error": f"Unknown action: {action}"}
    out = run("systemctl", "--user", action, f"{svc_name}.service", timeout=10)
    return {
        "ok": True,  # systemctl returns 0 even on some failures; best-effort
        "action": action,
        "service": svc_name,
    }


# ═══════════════════════════════════════════════════════════════
#  HTTP Server
# ═══════════════════════════════════════════════════════════════

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, data, status=200, ct="application/json"):
        body = json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, (dict, list)) else str(data)
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/services":
            self._send(get_all_services())
        elif path == "/api/resources":
            self._send(get_resources())
        elif path == "/api/config":
            self._send(load_config())
        elif path.startswith("/api/logs"):
            svc = ""
            if "svc=" in self.path:
                svc = self.path.split("svc=")[-1].split("&")[0]
            self._send(get_logs(svc) if svc else "Missing ?svc=", ct="text/plain; charset=utf-8")
        elif path in ("/", "/index.html"):
            html_path = PROJECT_DIR / "index.html"
            if html_path.exists():
                self._send(html_path.read_text(), ct="text/html; charset=utf-8")
            else:
                self._send({"error": "index.html not found"}, 404)
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length > 0 else {}

        if self.path == "/api/ctl":
            svc = body.get("svc") or body.get("service")
            action = body.get("action") or body.get("ctl")
            if not svc or not action:
                self._send({"ok": False, "error": "Missing svc or action"}, 400)
                return
            self._send(control_service(svc, action))
        else:
            self._send({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def main():
    print(f"Mission Control → http://{HOST}:{PORT}")
    print(f"Config: {CONFIG_PATH} {'(found)' if CONFIG_PATH.exists() else '(not found, using defaults)'}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
