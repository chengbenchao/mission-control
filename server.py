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
import secrets
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", 8880))
HOST = os.environ.get("HOST", "127.0.0.1")
PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ["CONFIG"]) if os.environ.get("CONFIG") else None
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.json"
LOCAL_CONFIG_PATH = PROJECT_DIR / "config.local.json"
BASE_PATH = os.environ.get("MISSION_CONTROL_BASE_PATH", os.environ.get("BASE_PATH", "")).strip()
TOKEN_FILE = Path(os.environ.get(
    "MISSION_CONTROL_TOKEN_FILE",
    str(Path.home() / ".config" / "mission-control" / "token"),
))
_AUTH_TOKEN_CACHE = None


# ═══════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════

def load_config():
    """Load config files, letting local machine config override defaults."""
    defaults = {
        "infra_patterns": [],
        "service_urls": {},
    }
    for path in config_paths():
        if not path.exists():
            continue
        try:
            with open(path) as f:
                loaded = json.load(f)
            defaults.update(loaded)
        except Exception:
            pass
    return defaults


def config_paths():
    """Return config files in load order."""
    if CONFIG_PATH:
        return [CONFIG_PATH]
    return [DEFAULT_CONFIG_PATH, LOCAL_CONFIG_PATH]


def get_auth_token(create=False):
    """Return the configured API token, creating a per-user token if needed."""
    global _AUTH_TOKEN_CACHE
    if _AUTH_TOKEN_CACHE:
        return _AUTH_TOKEN_CACHE

    token = os.environ.get("MISSION_CONTROL_TOKEN", "").strip()
    if not token:
        token = str(load_config().get("auth_token", "")).strip()
    if not token and TOKEN_FILE.exists():
        try:
            token = TOKEN_FILE.read_text().strip()
        except Exception:
            token = ""
    if not token and create:
        token = secrets.token_urlsafe(32)
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(token + "\n")
        os.chmod(TOKEN_FILE, 0o600)

    _AUTH_TOKEN_CACHE = token
    return token


def public_config():
    """Return config safe to expose to the browser."""
    cfg = load_config()
    cfg.pop("auth_token", None)
    cfg["auth_enabled"] = bool(get_auth_token(create=False))
    return cfg


# ═══════════════════════════════════════════════════════════════
#  Service Discovery (fully dynamic)
# ═══════════════════════════════════════════════════════════════

def run_result(*args, timeout=5):
    """Run a command, returning (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or "").strip(), "Command timed out"
    except Exception as e:
        return 1, "", str(e)


def run(*args, timeout=5):
    """Run a command, return stdout or '' on failure."""
    code, stdout, _stderr = run_result(*args, timeout=timeout)
    return stdout if code == 0 else ""


def get_systemd_services():
    """Discover all systemd user services, including stopped/failed units."""
    services = {}

    out = run("systemctl", "--user", "list-units", "--type=service",
              "--all", "--no-legend", "--no-pager")
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        name = line.split()[0]
        if name.endswith(".service"):
            svc = name[:-8]  # strip .service
            services[svc] = {"type": "systemd", "name": svc}

    out = run("systemctl", "--user", "list-unit-files", "--type=service",
              "--no-legend", "--no-pager")
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        name = line.split()[0]
        if name.endswith(".service"):
            svc = name[:-8]
            services.setdefault(svc, {"type": "systemd", "name": svc})

    return services


def get_systemd_unit_info(svc_name):
    """Get selected systemd properties for a user service."""
    out = run("systemctl", "--user", "show", f"{svc_name}.service",
              "--property=MainPID,ActiveState,SubState,LoadState")
    info = {"MainPID": 0, "ActiveState": "unknown", "SubState": "unknown", "LoadState": "unknown"}
    for line in out.split("\n"):
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        info[key] = value
    try:
        info["MainPID"] = int(info.get("MainPID") or 0)
    except ValueError:
        info["MainPID"] = 0
    return info


def get_listening_ports():
    """Return {port: pid} for all listening ports."""
    out = run("ss", "-tlnp")
    ports = {}
    for line in out.split("\n")[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        addr = parts[3]  # Local Address:Port
        proc = parts[5]
        port_str = addr.rsplit(":", 1)[-1]
        if not port_str.isdigit():
            continue
        port = int(port_str)
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
        if len(parts) < 6:
            continue
        addr = parts[3]  # Local Address:Port
        proc = parts[5]
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
    # 1) Direct PID match
    if svc_pid and svc_pid in pid_ports:
        ports = pid_ports[svc_pid]
        public = [p for p in ports if p > 1024]
        if public:
            return public[0]
        return ports[0] if ports else None

    # 2) PID=0 fallback: try systemctl status for PID
    if svc_pid == 0:
        out = run("systemctl", "--user", "status", f"{svc_name}.service")
        m = re.search(r"Main PID: (\d+)", out)
        if m:
            fallback_pid = int(m.group(1))
            if fallback_pid and fallback_pid in pid_ports:
                ports = pid_ports[fallback_pid]
                public = [p for p in ports if p > 1024]
                if public:
                    return public[0]
                return ports[0] if ports else None

    # 3) Name-based fallback: match svc_name against ss process names (handles truncation)
    name_ports = _get_name_ports()
    if name_ports:
        for proc_name, ports in name_ports.items():
            # substring match handles truncated names like 'openclaw-gatewa'
            if svc_name[:12] in proc_name or proc_name in svc_name:
                public = [p for p in ports if p > 1024]
                if public:
                    return public[0]
                return ports[0]

    # 4) Grep service file for port hints + script path hints
    out = run("systemctl", "--user", "cat", f"{svc_name}.service")
    for line in out.split("\n"):
        m = re.search(r"(?:port|PORT|--port|:)\s*(\d{4,5})", line)
        if m:
            return int(m.group(1))

    # 5) For services with no PID match, match by /proc/<pid>/cwd
    for port, cwd in _get_port_cwds().items():
        if svc_name in cwd:
            return port
    return None


def _get_port_cwds():
    """Return {port: cwd} for port listeners."""
    result = {}
    out = run("ss", "-tlnp")
    for line in out.split("\n")[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        addr = parts[3]
        proc = parts[5]
        port_str = addr.rsplit(":", 1)[-1]
        if not port_str.isdigit():
            continue
        port = int(port_str)
        m = re.search(r"pid=(\d+)", proc)
        if m:
            pid = m.group(1)
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
                result[port] = cwd
            except Exception:
                pass
    return result


def _get_port_cmdlines():
    """Return {port: cmdline} for port listeners, reading /proc/<pid>/cmdline."""
    result = {}
    # get {port: pid} first
    out = run("ss", "-tlnp")
    for line in out.split("\n")[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        addr = parts[3]
        proc = parts[5]
        port_str = addr.rsplit(":", 1)[-1]
        if not port_str.isdigit():
            continue
        port = int(port_str)
        m = re.search(r"pid=(\d+)", proc)
        if m:
            pid = m.group(1)
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
                result[port] = cmdline
            except Exception:
                pass
    return result


def _get_name_ports():
    """Return {process_name: [ports]} from ss -tlnp."""
    out = run("ss", "-tlnp")
    result = {}
    for line in out.split("\n")[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        addr = parts[3]  # Local Address:Port
        proc = parts[5]
        port_str = addr.rsplit(":", 1)[-1]
        if not port_str.isdigit():
            continue
        port = int(port_str)
        # Extract process name: users:((\"procname\",pid=123,fd=4))
        m = re.search(r'"([^"]+)"', proc)
        if m:
            name = m.group(1)
            result.setdefault(name, []).append(port)
    return result


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

    # Build cwd→svc_name map for orphan matching
    port_cwds = _get_port_cwds()
    port_to_svc = {}
    for port, cwd in port_cwds.items():
        for svc_name in systemd_svcs:
            if svc_name in cwd:
                port_to_svc[port] = svc_name

    # Process systemd services
    for name, svc in systemd_svcs.items():
        unit = get_systemd_unit_info(name)
        pid = unit["MainPID"]
        port = match_service_to_port(name, pid, pid_ports) if unit["ActiveState"] == "active" else None

        if pid:
            matched_pids.add(pid)

        # Also mark PID from port match
        if port:
            for p, pid_list in pid_ports.items():
                if port in pid_list:
                    matched_pids.add(p)

        # Determine if this is "infra" based on config patterns
        is_infra = any(re.search(pat, name, re.IGNORECASE) for pat in infra_patterns)

        svc_list.append({
            "name": name,
            "type": "systemd",
            "port": port,
            "pid": pid or None,
            "memory": get_process_memory(pid),
            "status": check_health({"name": name, "type": "systemd", "port": port, "unit": unit}),
            "active_state": unit["ActiveState"],
            "sub_state": unit["SubState"],
            "is_infra": is_infra,
            "url": url_map.get(name),
        })

    # Discover orphaned port listeners (not matched to any systemd service)
    for port, pid in all_ports.items():
        if pid in matched_pids or port in port_to_svc:
            continue
        # Try to get process name
        proc_name = ""
        out = run("ps", "-p", str(pid), "-o", "comm=")
        if out:
            proc_name = out.strip()

        # Skip system-daemon processes (sshd, systemd). Other processes
        # discovered via systemd have already been matched above — only
        # genuinely orphaned listeners reach here. Classify them by the
        # same infra_patterns config that drives the main service list.
        if proc_name in ("sshd", "systemd", "systemd-"):
            continue
        if port < 1024:  # system-reserved ports
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

    return svc_list


def check_health(svc):
    """Check if a service is healthy via systemd + port."""
    name = svc["name"]
    if svc["type"] == "systemd":
        unit = svc.get("unit") or get_systemd_unit_info(name)
        active_state = unit.get("ActiveState")
        if active_state == "failed":
            return "error"
        if active_state != "active":
            return "stopped"

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

def service_exists(svc_name):
    """Return True when svc_name is a known systemd user service."""
    return bool(svc_name and svc_name in get_systemd_services())


def get_logs(svc_name, lines=50):
    """Get recent journalctl logs for any systemd service."""
    if not service_exists(svc_name):
        return "Unknown service"
    out = run("journalctl", "--user", "-u", f"{svc_name}.service",
              "-n", str(lines), "--no-pager", timeout=5)
    return out[-5000:] if out else "No logs available"


def control_service(svc_name, action):
    """Start/stop/restart a systemd service."""
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "error": f"Unknown action: {action}"}
    if not service_exists(svc_name):
        return {"ok": False, "error": f"Unknown service: {svc_name}"}

    code, stdout, stderr = run_result("systemctl", "--user", action, f"{svc_name}.service", timeout=10)
    if code != 0:
        return {
            "ok": False,
            "action": action,
            "service": svc_name,
            "error": stderr or stdout or f"systemctl exited with {code}",
        }
    return {
        "ok": True,
        "action": action,
        "service": svc_name,
        "output": stdout,
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

    def _external_prefix(self):
        prefix = self.headers.get("X-Forwarded-Prefix", "").strip() or BASE_PATH
        if not prefix:
            return ""
        if not prefix.startswith("/"):
            prefix = "/" + prefix
        return prefix.rstrip("/")

    def _route_path(self):
        path = urlparse(self.path).path
        prefix = self._external_prefix()
        if prefix and path == prefix:
            return "/"
        if prefix and path.startswith(prefix + "/"):
            return path[len(prefix):] or "/"
        return path

    def _send(self, data, status=200, ct="application/json"):
        body = json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, (dict, list)) else str(data)
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body.encode())

    def _authorized(self):
        token = get_auth_token(create=True)
        auth = self.headers.get("Authorization", "")
        provided = ""
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
        if not provided:
            provided = self.headers.get("X-Mission-Control-Token", "").strip()
        return bool(provided) and secrets.compare_digest(provided, token)

    def _require_auth(self, path):
        if not path.startswith("/api/") or path == "/api/auth/status":
            return True
        if self._authorized():
            return True
        self._send({"ok": False, "error": "Unauthorized"}, 401)
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        path = self._route_path()
        if not self._require_auth(path):
            return

        if path == "/api/auth/status":
            self._send({"auth_required": True})
        elif path == "/api/services":
            self._send(get_all_services())
        elif path == "/api/resources":
            self._send(get_resources())
        elif path == "/api/config":
            self._send(public_config())
        elif path.startswith("/api/logs"):
            params = parse_qs(parsed.query)
            svc = params.get("svc", [""])[0]
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
        parsed = urlparse(self.path)
        path = self._route_path()
        if not self._require_auth(path):
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
        except json.JSONDecodeError:
            self._send({"ok": False, "error": "Invalid JSON body"}, 400)
            return

        if path == "/api/ctl":
            svc = body.get("svc") or body.get("service")
            action = body.get("action") or body.get("ctl")
            if not svc or not action:
                self._send({"ok": False, "error": "Missing svc or action"}, 400)
                return
            result = control_service(svc, action)
            self._send(result, 200 if result.get("ok") else 400)
        else:
            self._send({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()


def main():
    get_auth_token(create=True)
    print(f"Mission Control → http://{HOST}:{PORT}")
    print("Config files:")
    for path in config_paths():
        print(f"  {path} {'(found)' if path.exists() else '(not found)'}")
    if BASE_PATH:
        print(f"Base path: {BASE_PATH}")
    print(f"Auth token file: {TOKEN_FILE}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
