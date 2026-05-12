#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  Mission Control — Universal Install Script
#  Zero assumptions. Works on any Linux with systemd.
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "▸ Project dir: $PROJECT_DIR"

# ── 1. Check prerequisites ──────────────────────────────────
PYTHON=""
for candidate in python3 python3.12 python3.11 python3.10; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "✗ python3 not found. Install Python 3.10+ first."
    exit 1
fi
echo "▸ Python: $($PYTHON --version)"

# ── 2. Check systemd user session ───────────────────────────
if ! systemctl --user --quiet is-active default.target 2>/dev/null; then
    echo "▸ Enabling linger for systemd user session..."
    loginctl enable-linger "$(whoami)" 2>/dev/null || true
fi

# ── 3. Create default config if missing ─────────────────────
if [ ! -f "$PROJECT_DIR/config.json" ]; then
    echo "▸ Creating default config.json..."
    cat > "$PROJECT_DIR/config.json" <<'EOF'
{
  "infra_patterns": [],
  "service_urls": {}
}
EOF
    echo "   Edit $PROJECT_DIR/config.json to add infra patterns and URLs."
fi

# ── 4. Install systemd user service ─────────────────────────
SERVICE_FILE="$HOME/.config/systemd/user/mission-control.service"
mkdir -p "$(dirname "$SERVICE_FILE")"

cat > "$SERVICE_FILE" <<SERVICEOF
[Unit]
Description=Mission Control Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$PYTHON server.py
Environment=PORT=${PORT:-8880}
Environment=HOST=127.0.0.1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
SERVICEOF

echo "▸ Service file written: $SERVICE_FILE"

# ── 5. Enable and start ─────────────────────────────────────
systemctl --user daemon-reload
systemctl --user enable mission-control.service
systemctl --user restart mission-control.service

sleep 1
if systemctl --user is-active mission-control.service &>/dev/null; then
    echo "✓ Mission Control running on http://127.0.0.1:${PORT:-8880}"
else
    echo "✗ Failed to start. Check: journalctl --user -u mission-control.service"
    exit 1
fi

# ── 6. Nginx hint ───────────────────────────────────────────
echo ""
echo "To expose via nginx, add to your server block:"
echo ""
echo "    location /manage/ {"
echo "        proxy_pass http://127.0.0.1:${PORT:-8880}/;"
echo "        proxy_set_header Host \$host;"
echo "        proxy_set_header X-Forwarded-Prefix /manage;"
echo "    }"
echo ""
echo "Then configure config.json service_urls to link services."
