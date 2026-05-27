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

Required environment variables (no defaults for security):
    MC_USERNAME   — login username
    MC_PASSWORD   — login password
    MC_SESSION_SECRET (optional) — auto-generated if not set
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape as html_escape
from http.cookies import SimpleCookie
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen, Request
from urllib.error import URLError

# ═══════════════════════════════════════════════════════════════
#  Logging
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mission-control")

# ═══════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════

PORT = int(os.environ.get("PORT", 8880))
HOST = os.environ.get("HOST", "127.0.0.1")
PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG", str(PROJECT_DIR / "config.json")))

# CORS: set MC_ALLOWED_ORIGIN to your domain (e.g. "https://example.com").
# Leave empty to deny cross-origin requests (recommended for production).
# Set to "*" only for local development.
ALLOWED_ORIGIN = os.environ.get("MC_ALLOWED_ORIGIN", "").strip()

# Processes to skip when listing orphaned port listeners (low-level system daemons).
# Comma-separated, matches process comm name exactly. Override with MC_SKIP_PROCS.
_default_skip = "sshd,systemd,systemd-resolved,systemd-networkd,agetty,cron,crond"
SKIP_PROCS = set(
    p.strip() for p in
    os.environ.get("MC_SKIP_PROCS", _default_skip).split(",")
    if p.strip()
)

# ═══════════════════════════════════════════════════════════════
#  Auth — no hardcoded defaults for USERNAME / PASSWORD
# ═══════════════════════════════════════════════════════════════

SESSION_SECRET = os.environ.get("MC_SESSION_SECRET", secrets.token_hex(32)).encode()
SESSION_TTL = 86400  # 24 hours

_raw_username = os.environ.get("MC_USERNAME", "").strip()
_raw_password = os.environ.get("MC_PASSWORD", "").strip()

if not _raw_username or not _raw_password:
    log.error(
        "MC_USERNAME and MC_PASSWORD must be set via environment variables.\n"
        "Example:\n"
        "  export MC_USERNAME=admin\n"
        "  export MC_PASSWORD=$(openssl rand -base64 16)\n"
        "Or set them in the systemd service file:\n"
        "  Environment=MC_USERNAME=admin\n"
        "  Environment=MC_PASSWORD=yourpassword"
    )
    sys.exit(1)

# Hash credentials at startup — plaintext never lives in memory after this point.
# Using SHA-256 (fast enough for server-side compare; bcrypt not in stdlib).
_SALT = SESSION_SECRET  # reuse session secret as HMAC key for credential hashing
USERNAME_HASH = hmac.new(_SALT, _raw_username.encode(), hashlib.sha256).hexdigest()
PASSWORD_HASH = hmac.new(_SALT, _raw_password.encode(), hashlib.sha256).hexdigest()
USERNAME = _raw_username   # kept only for log display (not for auth comparison)
del _raw_username, _raw_password  # scrub plaintext from memory

# In-memory session store: {token: expiry_timestamp}
_sessions: dict[str, float] = {}

# ── Rate limiting: {ip: [timestamp, ...]} ──
_login_attempts: dict[str, list] = {}
MAX_ATTEMPTS = 10
ATTEMPT_WINDOW = 300  # 5 minutes


def _rate_limit_check(ip: str) -> bool:
    """Return True if IP is within rate limit, False if exceeded."""
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    # Prune old attempts outside the window
    attempts = [t for t in attempts if now - t < ATTEMPT_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) < MAX_ATTEMPTS


def _rate_limit_record(ip: str):
    """Record a failed login attempt for the given IP."""
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < ATTEMPT_WINDOW]
    attempts.append(now)
    _login_attempts[ip] = attempts


def _make_token() -> str:
    """Create an HMAC-signed session token."""
    payload = f"{secrets.token_hex(16)}:{int(time.time())}"
    sig = hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}:{sig}"


def _verify_token(token: str) -> bool:
    """Verify a session token is valid and not expired."""
    try:
        parts = token.rsplit(":", 1)
        if len(parts) != 2:
            return False
        payload, sig = parts
        expected = hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected):
            return False
        ts = int(payload.rsplit(":", 1)[-1])
        return (time.time() - ts) < SESSION_TTL
    except Exception:
        return False


def _check_auth(handler) -> bool:
    """Check if the request has a valid session cookie."""
    cookie_header = handler.headers.get("Cookie", "")
    cookies = SimpleCookie(cookie_header)
    token = cookies.get("mc_session")
    if token and _verify_token(token.value):
        return True
    return False


def _create_session() -> str:
    """Create and store a new session, return the token."""
    token = _make_token()
    _sessions[token] = time.time() + SESSION_TTL
    # Cleanup expired sessions
    now = time.time()
    expired = [k for k, v in _sessions.items() if v < now]
    for k in expired:
        _sessions.pop(k, None)
    return token


# ═══════════════════════════════════════════════════════════════
#  Config loader
# ═══════════════════════════════════════════════════════════════

def load_config():
    """Load optional config.json, return defaults if missing."""
    defaults = {
        "infra_patterns": [],
        "service_urls": {},
        "static_sites": [],
    }
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                loaded = json.load(f)
            defaults.update(loaded)
        except Exception as e:
            log.warning(f"Failed to load config.json: {e}")
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
                svc = name[:-8]
                if svc not in services:
                    services[svc] = {"type": "systemd", "name": svc, "scope": scope}
    return services


def get_systemd_mainpid(svc_name, scope="--user"):
    """Get MainPID for a systemd service."""
    out = run("systemctl", scope, "show", f"{svc_name}.service",
              "--property=MainPID")
    if out and "=" in out:
        pid = out.split("=")[-1]
        if pid and pid != "0":
            try:
                return int(pid)
            except ValueError:
                pass
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


def match_service_to_port(svc_name, svc_pid, pid_ports, scope="--user"):
    """Match a systemd service to its listening port(s)."""
    if svc_pid and svc_pid in pid_ports:
        ports = pid_ports[svc_pid]
        public = [p for p in ports if p > 1024]
        if public:
            return public[0]
        return ports[0] if ports else None

    # Fallback: grep the service file for port hints
    out = run("systemctl", scope, "cat", f"{svc_name}.service")
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


def check_health_port(port) -> str:
    """Check TCP connectivity to a local port."""
    if not port:
        return "unknown"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        result = s.connect_ex(("127.0.0.1", port))
        s.close()
        return "healthy" if result == 0 else "error"
    except Exception:
        return "unknown"


def check_health(svc, systemd_services: dict) -> str:
    """Check if a service is healthy via systemd state + port probe."""
    name = svc["name"]
    scope = systemd_services.get(name, {}).get("scope", "--user")

    if svc["type"] == "systemd":
        out = run("systemctl", scope, "is-active", f"{name}.service")
        if out != "active":
            return "error"

    port = svc.get("port")
    if port:
        return check_health_port(port)

    return "healthy" if svc["type"] == "systemd" else "unknown"


def get_all_services():
    """Auto-discover all services: systemd + orphaned port listeners."""
    cfg = load_config()
    infra_patterns = cfg.get("infra_patterns", [])
    url_map = cfg.get("service_urls", {})
    port_map = cfg.get("service_ports", {})   # manual port hints from config
    base_url = cfg.get("base_url", "").rstrip("/")

    def full_url(path):
        """Combine base_url with a relative path."""
        if not path:
            return None
        return base_url + path if base_url else path

    systemd_svcs = get_systemd_services()
    pid_ports = get_pid_ports()
    all_ports = get_listening_ports()

    svc_list = []
    matched_pids = set()

    # Process systemd services
    for name, svc in systemd_svcs.items():
        scope = svc.get("scope", "--user")
        pid = get_systemd_mainpid(name, scope)
        port = match_service_to_port(name, pid, pid_ports, scope)

        # Fallback: use port hint from config if auto-discovery failed
        if not port and name in port_map:
            port = port_map[name]

        if pid:
            matched_pids.add(pid)

        is_infra = any(re.search(pat, name, re.IGNORECASE) for pat in infra_patterns)

        rel = url_map.get(name)
        svc_list.append({
            "name": name,
            "type": "systemd",
            "scope": scope,
            "port": port,
            "pid": pid,
            "memory": get_process_memory(pid),
            "status": "unknown",
            "is_infra": is_infra,
            "url": rel,
            "full_url": full_url(rel),
        })

    # Discover orphaned port listeners
    for port, pid in all_ports.items():
        if pid in matched_pids:
            continue
        proc_name = ""
        out = run("ps", "-p", str(pid), "-o", "comm=")
        if out:
            proc_name = out.strip()

        if proc_name in SKIP_PROCS and port < 1024:
            continue

        name = proc_name or f"pid-{pid}"
        is_infra = any(re.search(pat, name, re.IGNORECASE) for pat in infra_patterns)

        rel = url_map.get(name)
        svc_list.append({
            "name": name,
            "type": "direct",
            "scope": None,
            "port": port,
            "pid": pid,
            "memory": get_process_memory(pid),
            "status": "healthy",
            "is_infra": is_infra,
            "url": rel,
            "full_url": full_url(rel),
        })

    # Append static sites from config
    static_sites = cfg.get("static_sites", [])
    for site in static_sites:
        site_dir = site.get("dir", "")
        dir_exists = bool(site_dir and Path(site_dir).is_dir())
        rel = site.get("url")
        svc_list.append({
            "name": site["name"],
            "type": "static",
            "scope": None,
            "port": None,
            "pid": None,
            "memory": None,
            "status": "healthy" if dir_exists else "warn",
            "is_infra": True,
            "url": rel,
            "full_url": full_url(rel),
            "desc": site.get("desc", ""),
            "dir": site_dir,
        })

    # Sort: infra first, then by name
    svc_list.sort(key=lambda s: (not s["is_infra"], s["name"]))

    # Parallel health check for systemd services
    def _check(svc):
        if svc["type"] == "systemd":
            svc["status"] = check_health(svc, systemd_svcs)
        return svc

    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(_check, s): i for i, s in enumerate(svc_list)}
        results = [None] * len(svc_list)
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()

    return [s for s in results if s is not None]


# ═══════════════════════════════════════════════════════════════
#  Resource Monitoring
# ═══════════════════════════════════════════════════════════════

def _read_cpu_stat():
    """Read aggregate CPU times from /proc/stat. Returns (total, idle)."""
    with open("/proc/stat") as f:
        line = f.readline()
    parts = line.split()
    vals = [int(x) for x in parts[1:]]
    # fields: user nice system idle iowait irq softirq steal guest guest_nice
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    total = sum(vals)
    return total, idle


def get_resources():
    """CPU / RAM / Disk / Uptime from /proc."""
    cpu = 0
    try:
        t1, i1 = _read_cpu_stat()
        time.sleep(0.2)
        t2, i2 = _read_cpu_stat()
        dt = t2 - t1
        di = i2 - i1
        cpu = round((1 - di / dt) * 100, 1) if dt > 0 else 0
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

def get_logs(svc_name, lines=100):
    """Get recent journalctl logs for any systemd service (user or system)."""
    # Try user scope first, then system
    for scope in ("--user", "--system"):
        out = run("journalctl", scope, "-u", f"{svc_name}.service",
                  "-n", str(lines), "--no-pager", timeout=8)
        if out and "No entries" not in out and "Failed to" not in out:
            return out[-8000:]
    return "No logs available"


def control_service(svc_name, action):
    """Start/stop/restart a systemd service (tries user then system scope)."""
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "error": f"Unknown action: {action}"}

    # Validate service name to prevent injection
    if not re.match(r"^[\w@:.\\-]+$", svc_name):
        return {"ok": False, "error": "Invalid service name"}

    # Try user scope first, then system scope
    for scope in ("--user", "--system"):
        result = subprocess.run(
            ["systemctl", scope, action, f"{svc_name}.service"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            log.info(f"Service control: {action} {svc_name} (scope={scope}) OK")
            return {"ok": True, "action": action, "service": svc_name, "scope": scope}

    log.warning(f"Service control: {action} {svc_name} failed on both user and system scope")
    return {"ok": False, "error": f"Failed to {action} {svc_name}", "service": svc_name}


# ═══════════════════════════════════════════════════════════════
#  HTTP Server
# ═══════════════════════════════════════════════════════════════

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Custom access log with client IP
        log.info(f"{self.client_address[0]} - {fmt % args}")

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def _send(self, data, status=200, ct="application/json"):
        body = json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, (dict, list)) else str(data)
        body_bytes = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body_bytes)

    def _set_cookie(self, token: str):
        self.send_header(
            "Set-Cookie",
            f"mc_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}"
        )

    def _redirect_login(self):
        self.send_response(302)
        self.send_header("Location", "/manage/login")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _serve_file(self, path: Path, ct: str):
        if path.exists():
            self._send(path.read_text(), ct=ct)
        else:
            self._send({"error": f"{path.name} not found"}, 404)

    def _normalize_path(self, raw: str) -> str:
        """Strip optional /manage prefix so the server works with or without nginx sub-path."""
        if raw.startswith("/manage"):
            raw = raw[len("/manage"):]
        return raw or "/"

    def do_GET(self):
        path = self._normalize_path(urlparse(self.path).path)

        # ── login page ──
        if path in ("/login", "/login.html"):
            self._serve_file(PROJECT_DIR / "login.html", ct="text/html; charset=utf-8")
            return

        # ── auth check endpoint (for nginx auth_request) ──
        if path == "/api/auth-check":
            if _check_auth(self):
                self._send({"ok": True}, 200)
            else:
                self._send({"ok": False}, 401)
            return

        # ── API endpoints (auth required) ──
        if path.startswith("/api/"):
            if not _check_auth(self):
                self._send({"error": "unauthorized"}, 401)
                return
            if path == "/api/services":
                self._send(get_all_services())
            elif path == "/api/resources":
                self._send(get_resources())
            elif path == "/api/config":
                cfg = load_config()
                cfg.pop("_secret", None)
                # Append runtime webhook status (URL masked)
                cfg["_runtime"] = {
                    "webhook_enabled": bool(WEBHOOK_URL),
                    "webhook_type": WEBHOOK_TYPE if WEBHOOK_URL else None,
                    "watch_interval": WATCH_INTERVAL,
                }
                self._send(cfg)
            elif path.startswith("/api/logs"):
                qs = parse_qs(urlparse(self.path).query)
                svc = qs.get("svc", [""])[0]
                if not svc:
                    self._send("Missing ?svc=", ct="text/plain; charset=utf-8")
                elif not re.match(r"^[\w@:.\\-]+$", svc):
                    self._send("Invalid service name", status=400, ct="text/plain")
                else:
                    self._send(get_logs(svc), ct="text/plain; charset=utf-8")
            else:
                self._send({"error": "not found"}, 404)
            return

        # ── dashboard (auth required) ──
        if not _check_auth(self):
            self._redirect_login()
            return

        if path in ("/", "/index.html"):
            html_path = PROJECT_DIR / "index.html"
            if html_path.exists():
                self._send(html_path.read_text(), ct="text/html; charset=utf-8")
            else:
                self._send({"error": "index.html not found"}, 404)
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        path = self._normalize_path(urlparse(self.path).path)

        # ── login endpoint ──
        if path == "/api/login":
            ip = self._client_ip()
            if not _rate_limit_check(ip):
                self._send({"ok": False, "error": "请求过于频繁，请稍后再试"}, 429)
                log.warning(f"Rate limit exceeded for IP: {ip}")
                return

            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except json.JSONDecodeError:
                self._send({"ok": False, "error": "无效的请求格式"}, 400)
                return

            username = (body.get("username") or "").strip()
            password = (body.get("password") or "").strip()

            if not username or not password:
                self._send({"ok": False, "error": "请输入用户名和密码"}, 400)
                return

            user_hash = hmac.new(_SALT, username.encode(), hashlib.sha256).hexdigest()
            pass_hash = hmac.new(_SALT, password.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(user_hash, USERNAME_HASH) or not hmac.compare_digest(pass_hash, PASSWORD_HASH):
                _rate_limit_record(ip)
                log.warning(f"Failed login attempt from {ip} for user '{username}'")
                self._send({"ok": False, "error": "用户名或密码错误"}, 401)
                return

            token = _create_session()
            log.info(f"Successful login from {ip} for user '{username}'")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self._set_cookie(token)
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "message": "登录成功"}).encode())
            return

        # ── all other POSTs require auth ──
        if not _check_auth(self):
            self._send({"error": "unauthorized"}, 401)
            return

        if path == "/api/ctl":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except json.JSONDecodeError:
                self._send({"ok": False, "error": "Invalid JSON"}, 400)
                return
            svc = body.get("svc") or body.get("service")
            action = body.get("action") or body.get("ctl")
            if not svc or not action:
                self._send({"ok": False, "error": "Missing svc or action"}, 400)
                return
            self._send(control_service(svc, action))
        else:
            self._send({"error": "not found"}, 404)

    def do_OPTIONS(self):
        origin = self.headers.get("Origin", "")
        allowed = ALLOWED_ORIGIN
        self.send_response(200)
        if allowed == "*" or origin == allowed:
            self.send_header("Access-Control-Allow-Origin", allowed if allowed else origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Credentials", "true")
        self.end_headers()


# ═══════════════════════════════════════════════════════════════
#  Webhook Alert
# ═══════════════════════════════════════════════════════════════

WEBHOOK_URL = os.environ.get("MC_WEBHOOK_URL", "").strip()
WEBHOOK_TYPE = os.environ.get("MC_WEBHOOK_TYPE", "feishu").strip().lower()  # feishu | dingtalk | wecom | custom

def send_webhook_alert(title: str, content: str):
    """Send an alert via webhook (Feishu / DingTalk / WeCom / custom URL)."""
    if not WEBHOOK_URL:
        return
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        if WEBHOOK_TYPE == "feishu":
            # 飞书自定义机器人 — 富文本卡片
            payload = {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"tag": "plain_text", "content": title},
                        "template": "red" if "告警" in title else "green",
                    },
                    "elements": [
                        {
                            "tag": "div",
                            "text": {
                                "tag": "lark_md",
                                "content": content,
                            },
                        },
                        {
                            "tag": "note",
                            "elements": [
                                {"tag": "plain_text", "content": f"Mission Control · {ts}"}
                            ],
                        },
                    ],
                },
            }
        elif WEBHOOK_TYPE == "dingtalk":
            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "title": title,
                    "text": f"## {title}\n\n{content}\n\n> Mission Control · {ts}",
                },
            }
        elif WEBHOOK_TYPE == "wecom":
            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "content": f"## {title}\n\n{content}\n\n> Mission Control · {ts}",
                },
            }
        else:
            # Generic JSON POST
            payload = {"title": title, "content": content, "timestamp": ts}
        data = json.dumps(payload, ensure_ascii=False).encode()
        req = Request(WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=8) as resp:
            log.info(f"Webhook sent: {title} (status={resp.status})")
    except URLError as e:
        log.warning(f"Webhook delivery failed: {e}")
    except Exception as e:
        log.warning(f"Webhook error: {e}")


# ═══════════════════════════════════════════════════════════════
#  Background Health Watcher
# ═══════════════════════════════════════════════════════════════

# Track last known status: {svc_name: status_str}
_last_status: dict[str, str] = {}
_watcher_lock = threading.Lock()

WATCH_INTERVAL = int(os.environ.get("MC_WATCH_INTERVAL", "60"))  # seconds


def _health_watcher():
    """Background thread: poll service health and fire alerts on transitions."""
    global _last_status
    # Wait for server to fully start
    time.sleep(10)
    log.info(f"Health watcher started (interval={WATCH_INTERVAL}s)")
    while True:
        try:
            svcs = get_all_services()
            with _watcher_lock:
                for svc in svcs:
                    name = svc["name"]
                    status = svc.get("status", "unknown")
                    prev = _last_status.get(name)

                    # Transition: was healthy, now error
                    if prev == "healthy" and status == "error":
                        port_info = f":{svc['port']}" if svc.get("port") else ""
                        full = svc.get("full_url") or ""
                        msg = (
                            f"**服务**: `{name}{port_info}`\n"
                            f"**状态**: 🔴 error（上次正常）\n"
                            f"**类型**: {svc.get('type','?')}\n"
                        )
                        if full:
                            msg += f"**地址**: [{full}]({full})\n"
                        log.warning(f"ALERT: {name} transitioned healthy→error")
                        threading.Thread(
                            target=send_webhook_alert,
                            args=(f"🔴 服务告警: {name}", msg),
                            daemon=True,
                        ).start()

                    # Transition: was error, now recovered
                    elif prev == "error" and status == "healthy":
                        log.info(f"RECOVERY: {name} transitioned error→healthy")
                        threading.Thread(
                            target=send_webhook_alert,
                            args=(
                                f"✅ 服务恢复: {name}",
                                f"**服务**: `{name}`\n**状态**: ✅ 已恢复正常\n",
                            ),
                            daemon=True,
                        ).start()

                    _last_status[name] = status
        except Exception as e:
            log.warning(f"Health watcher error: {e}")
        time.sleep(WATCH_INTERVAL)


def main():
    log.info(f"Mission Control → http://{HOST}:{PORT}")
    log.info(f"Config: {CONFIG_PATH} {'(found)' if CONFIG_PATH.exists() else '(not found, using defaults)'}")
    log.info(f"Auth user: {USERNAME}")
    if WEBHOOK_URL:
        log.info(f"Webhook alerts enabled: type={WEBHOOK_TYPE} interval={WATCH_INTERVAL}s")
    else:
        log.info("Webhook alerts disabled (set MC_WEBHOOK_URL to enable)")
    # Start background health watcher
    watcher = threading.Thread(target=_health_watcher, daemon=True, name="health-watcher")
    watcher.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
