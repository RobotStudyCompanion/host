#!/usr/bin/env bash
#
# install-systemd.sh — enable rsc-host as a systemd service on the Pi.
#
# Idempotent — safe to re-run. Configures:
#   * setcap on the venv's Python so rpi_ws281x can access /dev/mem
#     without sudo
#   * systemd unit template at /etc/systemd/system/rsc-host@.service
#   * per-user token in a drop-in override
#   * enables + starts rsc-host@$USER.service
#
# Run from the host repo root with your venv activated:
#
#     source .venv/bin/activate
#     ./scripts/install-systemd.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$HERE/systemd/rsc-host@.service"
UNIT_DST="/etc/systemd/system/rsc-host@.service"
SERVICE="rsc-host@$USER.service"
DROPIN_DIR="/etc/systemd/system/${SERVICE}.d"
TOKEN_FILE="$DROPIN_DIR/token.conf"

# ---- Sanity checks ---------------------------------------------------------

if [[ -z "${VIRTUAL_ENV:-}" ]]; then
    echo "error: no active virtualenv." >&2
    echo "hint:  source .venv/bin/activate first" >&2
    exit 1
fi

if [[ ! -f "$UNIT_SRC" ]]; then
    echo "error: unit file not found at $UNIT_SRC" >&2
    echo "hint:  run from the repo root" >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "error: systemctl not found — is this a systemd system?" >&2
    exit 1
fi

# ---- Resolve Python paths -----------------------------------------------
#
# Two paths matter:
#   PY_REAL — the resolved system Python (e.g. /usr/bin/python3.13).
#             This is where setcap actually applies (capabilities live on
#             the inode).
#   PY_VENV — the venv's symlink to it (e.g. .venv/bin/python).
#             This is what systemd's ExecStart must point at, so that
#             sys.path picks up the venv's site-packages.

PY_VENV="$VIRTUAL_ENV/bin/python"
if [[ ! -e "$PY_VENV" ]]; then
    echo "error: expected venv Python at $PY_VENV, not found" >&2
    exit 1
fi
PY_REAL="$(readlink -f "$PY_VENV")"
if [[ ! -x "$PY_REAL" ]]; then
    echo "error: venv Python symlink resolves to non-executable $PY_REAL" >&2
    exit 1
fi
echo "▸ venv python:   $PY_VENV"
echo "▸ system python: $PY_REAL"

# ---- setcap for NeoPixel ------------------------------------------------

echo "▸ granting capabilities to $PY_REAL (for rpi_ws281x /dev/mem access)"
sudo setcap cap_sys_rawio,cap_dac_override,cap_sys_nice+eip "$PY_REAL"
CAPS="$(getcap "$PY_REAL" || true)"
echo "  → $CAPS"

# ---- Install unit template ----------------------------------------------

# The shipped unit template assumes /home/%i/host/.venv/bin/python. Rewrite
# it to use the actual venv Python we detected — handles repo names other
# than `host` (rsc-host, host-experimental, etc.) without editing.

echo "▸ installing unit template at $UNIT_DST (with ExecStart=$PY_VENV)"
sudo tee "$UNIT_DST" > /dev/null <<EOF
[Unit]
Description=Robot Study Companion host service
Documentation=https://github.com/RobotStudyCompanion/host
After=network-online.target pigpiod.service
Wants=network-online.target
Requires=pigpiod.service
StartLimitBurst=5
StartLimitIntervalSec=60

[Service]
Type=simple
User=%i
WorkingDirectory=$HERE
Environment=RSC_HOST_TOKEN=CHANGE_ME
Environment=RSC_HOST_BACKEND=pi
Environment=RSC_HOST_BIND=0.0.0.0
Environment=RSC_HOST_PORT=8765
Environment=RSC_HOST_LOG_LEVEL=INFO
Environment=GPIOZERO_PIN_FACTORY=pigpio
ExecStart=$PY_VENV -m rsc_host

Restart=on-failure
RestartSec=2

SupplementaryGroups=gpio spi audio dialout i2c
LimitNOFILE=4096

[Install]
WantedBy=multi-user.target
EOF
sudo chmod 0644 "$UNIT_DST"

# ---- Prompt for token, write drop-in ------------------------------------

# Check whether a token is already configured.
EXISTING_TOKEN=""
if [[ -f "$TOKEN_FILE" ]]; then
    EXISTING_TOKEN="$(sudo grep -oP 'RSC_HOST_TOKEN=\K.*' "$TOKEN_FILE" || true)"
fi

if [[ -n "$EXISTING_TOKEN" && "$EXISTING_TOKEN" != "CHANGE_ME" ]]; then
    echo "▸ existing token found in $TOKEN_FILE — keeping"
    TOKEN="$EXISTING_TOKEN"
else
    echo
    echo "──────────────────────────────────────────────────────────"
    echo "  Pick a bearer token for LAN clients to authenticate."
    echo
    echo "  * For a shared/dev Pi on your home LAN, a short memorable"
    echo "    word is fine (e.g. 'dev', 'shiny', the robot's name)."
    echo "  * For anything remotely public, use a long random one."
    echo "  * Blank prompts a strong random token."
    echo
    echo "  Clients (console, VS Code, scripts) need this token to"
    echo "  connect. The console will remember it after first use."
    echo "──────────────────────────────────────────────────────────"
    echo -n "token: "
    read -r TOKEN
    if [[ -z "$TOKEN" ]]; then
        TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(24))')"
        echo "  → generated: $TOKEN"
    fi
    sudo mkdir -p "$DROPIN_DIR"
    sudo tee "$TOKEN_FILE" > /dev/null <<EOF
[Service]
Environment=RSC_HOST_TOKEN=$TOKEN
EOF
    sudo chmod 600 "$TOKEN_FILE"
    echo "▸ wrote token to $TOKEN_FILE (chmod 600)"
fi

# Also drop into a user-readable file so local scripts on this Pi
# (colleague's demo code, one-off Python) can grab the token without sudo.
USER_TOKEN_FILE="$HOME/.config/rsc-host/token"
mkdir -p "$(dirname "$USER_TOKEN_FILE")"
printf '%s' "$TOKEN" > "$USER_TOKEN_FILE"
chmod 600 "$USER_TOKEN_FILE"
echo "▸ also wrote token to $USER_TOKEN_FILE (readable by $USER only)"

# ---- Reload + enable + start --------------------------------------------

echo "▸ reloading systemd and enabling $SERVICE"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"

# If already running, restart to pick up any changes; else start fresh.
if sudo systemctl is-active --quiet "$SERVICE"; then
    echo "▸ restarting $SERVICE to pick up changes"
    sudo systemctl restart "$SERVICE"
else
    echo "▸ starting $SERVICE"
    sudo systemctl start "$SERVICE"
fi

# ---- Report -------------------------------------------------------------

sleep 1
echo
echo "── status ──────────────────────────────────────────────"
sudo systemctl status "$SERVICE" --no-pager --lines=8 || true
echo "────────────────────────────────────────────────────────"
echo
echo "▸ tail logs with:      journalctl -u $SERVICE -f"
echo "▸ stop with:           sudo systemctl stop $SERVICE"
echo "▸ disable at boot:     sudo systemctl disable $SERVICE"
echo "▸ edit config:         sudo systemctl edit $SERVICE"
echo
echo "connect from a client:"
echo "  ws://$(hostname).local:8765/"