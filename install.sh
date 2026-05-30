#!/usr/bin/env bash
# gemini-for-agents installer for Linux/macOS/WSL
set -euo pipefail

REPO="https://github.com/mcfax-arch/gemini-for-agents.git"
INSTALL_DIR="${HOME}/.gemini-for-agents"
SERVICE_NAME="gemini-for-agents"

if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    echo "ERROR: Python 3.8+ required"
    exit 1
fi

echo "==> Installing gemini-for-agents to ${INSTALL_DIR}"

# Clone or update
if [ -d "${INSTALL_DIR}" ]; then
    echo "==> Updating existing installation..."
    cd "${INSTALL_DIR}" && git pull --ff-only
else
    git clone --depth=1 "${REPO}" "${INSTALL_DIR}"
fi

cd "${INSTALL_DIR}"

# Verify Python version
$PYTHON -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)" || {
    echo "ERROR: Python 3.8+ required"
    exit 1
}

echo "==> Testing server startup..."
$PYTHON -c "
import socket, threading, time
from gemini_web2api import main as start_server
# Quick import test — actual start via service
print('Import OK — Python $(python3 --version 2>&1)')
"

# ── Install system service ──────────────────────────────────
OS="$(uname -s)"

if [ "${OS}" = "Linux" ] || uname -r | grep -qi microsoft; then
    # systemd (Linux / WSL2 with systemd)
    SERVICE_FILE="${HOME}/.config/systemd/user/${SERVICE_NAME}.service"
    mkdir -p "$(dirname "${SERVICE_FILE}")"

    cat > "${SERVICE_FILE}" << EOF
[Unit]
Description=Gemini for Agents — Google Gemini OpenAI-compatible proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${PYTHON} ${INSTALL_DIR}/gemini_web2api.py
WorkingDirectory=${INSTALL_DIR}
Restart=on-failure
RestartSec=5
StandardOutput=append:${INSTALL_DIR}/logs/service.log
StandardError=inherit

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable --now "${SERVICE_NAME}" 2>/dev/null || {
        echo "==> Starting service (user mode)..."
        systemctl --user start "${SERVICE_NAME}" 2>/dev/null || true
    }
    echo "==> systemd service installed: ${SERVICE_FILE}"

elif [ "${OS}" = "Darwin" ]; then
    # launchd (macOS)
    PLIST="${HOME}/Library/LaunchAgents/com.mcfax.${SERVICE_NAME}.plist"
    mkdir -p "$(dirname "${PLIST}")"

    cat > "${PLIST}" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mcfax.${SERVICE_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${INSTALL_DIR}/gemini_web2api.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${INSTALL_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${INSTALL_DIR}/logs/service.log</string>
    <key>StandardErrorPath</key>
    <string>${INSTALL_DIR}/logs/service.log</string>
</dict>
</plist>
EOF

    launchctl load "${PLIST}" 2>/dev/null || true
    echo "==> launchd service installed: ${PLIST}"
else
    echo "==> Unsupported OS: ${OS}"
    echo "    Manual start: cd ${INSTALL_DIR} && python gemini_web2api.py"
fi

# ── Verify ──────────────────────────────────────────────────
echo "==> Waiting for server..."
for i in $(seq 1 10); do
    if curl -sf http://127.0.0.1:8081/v1/models >/dev/null 2>&1; then
        echo "==> ✅ gemini-for-agents running on http://127.0.0.1:8081"
        exit 0
    fi
    sleep 1
done

echo "==> ⚠️  Server may still be starting. Check:"
echo "    http://127.0.0.1:8081/v1/models"
echo "    Logs: ${INSTALL_DIR}/logs/service.log"
