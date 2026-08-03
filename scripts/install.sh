#!/usr/bin/env bash
# install.sh — Robot Study Companion host bootstrap
#
# Brings a freshly-flashed Raspberry Pi 4 to the point where
# `python -m rsc_host` runs and every peripheral answers.
#
# Owns:      apt packages, pigpiod, UART/I2C/serial config, group membership,
#            venv creation, pip install, token bootstrap, smoke test.
# Does NOT own: the systemd unit — that is scripts/install-systemd.sh.
#
# Usage:
#   bash scripts/install.sh                 # interactive
#   bash scripts/install.sh --unattended    # no prompts, safe defaults
#   bash scripts/install.sh --dry-run       # print every mutation, change nothing
#   bash scripts/install.sh --skip-apt      # re-run quickly, skip the slow step
#   bash scripts/install.sh --help
#
# Assumes: 64-bit Raspberry Pi OS (Trixie / Debian 13), network up, a regular
# user with sudo, and this repo already cloned.

# ---- Source-safety ----------------------------------------------------------
# Only apply strict mode and the log redirect when run standalone. If a parent
# script sources this, the parent owns logging and we must not clobber its tee.
_RSC_INSTALL_STANDALONE=false
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    _RSC_INSTALL_STANDALONE=true
    set -euo pipefail
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="${RSC_VENV:-$REPO_DIR/.venv}"
TOKEN_DIR="$HOME/.config/rsc-host"
TOKEN_FILE="$TOKEN_DIR/token"
LOG_FILE="$HOME/rsc-install.log"

UNATTENDED=false
DRY_RUN=false
SKIP_APT=false
REBOOT_REQUIRED=false
SMOKE_OK=true

# ---- Arguments --------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --unattended) UNATTENDED=true ;;
        --dry-run)    DRY_RUN=true ;;
        --skip-apt)   SKIP_APT=true ;;
        --help|-h)    sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
        *)            echo "Unknown argument: $1  (try --help)"; exit 2 ;;
    esac
    shift
done

if [[ "$_RSC_INSTALL_STANDALONE" == true && "$DRY_RUN" == false ]]; then
    exec > >(tee -a "$LOG_FILE") 2>&1
fi
echo "=== install.sh started at $(date -Iseconds) on $(hostname) ==="

# ---- Helpers ----------------------------------------------------------------
if ! declare -f log >/dev/null 2>&1; then
    GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
    log()  { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $*"; }
    warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
    fail() { echo -e "${RED}[fail]${NC} $*"; exit 1; }
fi
step() { echo ""; echo -e "${BLUE:-}── $* ──${NC:-}"; }
ok()   { echo "  ✓  $*"; }
bad()  { echo "  ✗  $*"; SMOKE_OK=false; }

# Every mutating command goes through run(), so --dry-run is honest rather
# than aspirational.
run() {
    if [[ "$DRY_RUN" == true ]]; then
        echo "  [dry-run] $*"
    else
        "$@"
    fi
}

# Append a line to a file only if it is not already there.
ensure_line() {
    local line="$1" file="$2" use_sudo="${3:-false}"
    if grep -qxF "$line" "$file" 2>/dev/null; then
        return 0
    fi
    if [[ "$use_sudo" == true ]]; then
        run bash -c "echo '$line' | sudo tee -a '$file' >/dev/null"
    else
        run bash -c "echo '$line' >> '$file'"
    fi
    log "appended to ${file}: ${line}"
}

ask() {  # ask "prompt" "default"  → echoes answer; returns default when unattended
    local prompt="$1" default="$2" reply
    if [[ "$UNATTENDED" == true ]]; then echo "$default"; return; fi
    read -rp "  ${prompt} [${default}]: " reply </dev/tty || reply=""
    echo "${reply:-$default}"
}

# ════════════════════════════════════════════════════════════════════════════
# PREFLIGHT — every check runs before a single byte of system state changes.
# ════════════════════════════════════════════════════════════════════════════
step "Preflight"

[[ $EUID -ne 0 ]] || fail "Run as your regular user, not root. The script sudos where needed."
command -v sudo >/dev/null || fail "sudo not found."
[[ "$(uname -m)" == "aarch64" ]] || fail "Not aarch64. This targets 64-bit Raspberry Pi OS."

PI_MODEL=$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo "unknown")
log "Board: ${PI_MODEL}"
case "$PI_MODEL" in
    *"Raspberry Pi 4"*)
        PIN_FACTORY="pigpio"
        ok "Pi 4 — mainline pigpio supported"
        ;;
    *"Raspberry Pi 5"*)
        fail "Pi 5 detected. Mainline pigpio does not support the RP1 southbridge.
       This script targets Pi 4. For a Pi 5 you need GPIOZERO_PIN_FACTORY=lgpio
       and a pigpio-free peripheral backend — a different code path we haven't
       built yet. Stopping rather than installing something that half-works."
        ;;
    *)
        warn "Unrecognised board '${PI_MODEL}'. Continuing, but treat results with suspicion."
        PIN_FACTORY="pigpio"
        ;;
esac

# Repo sanity — are we actually inside the host repo?
[[ -f "$REPO_DIR/pyproject.toml" ]] \
    || fail "No pyproject.toml at ${REPO_DIR}. Run this from inside the rsc-host repo (scripts/install.sh)."
[[ -d "$REPO_DIR/rsc_host" ]] \
    || fail "No rsc_host/ package at ${REPO_DIR}. Wrong repo?"
ok "Repo root: ${REPO_DIR}"

# Python — the daemon needs 3.11+
PY_SYS="$(command -v python3 || true)"
[[ -n "$PY_SYS" ]] || fail "python3 not found on PATH."
PY_VER=$("$PY_SYS" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
"$PY_SYS" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' \
    || fail "Python ${PY_VER} is too old. Need 3.11 or newer."
ok "python3 ${PY_VER} at ${PY_SYS}"

# Disk — venv + wheels + apt upgrade need elbow room
AVAIL_GB=$(df -BG --output=avail / | tail -1 | tr -d 'G ')
[[ "$AVAIL_GB" -ge 4 ]] || fail "Only ${AVAIL_GB}G free on /. Need at least 4G."
[[ "$AVAIL_GB" -ge 8 ]] || warn "Only ${AVAIL_GB}G free on /. Tight but workable."
ok "${AVAIL_GB}G free on /"

# Network — apt and pip both need it
if [[ "$SKIP_APT" == false ]]; then
    curl -fsS --max-time 8 https://deb.debian.org/ >/dev/null \
        || fail "Cannot reach the Debian apt mirror. Fix networking first."
    ok "apt mirror reachable"
fi
curl -fsS --max-time 8 https://pypi.org/simple/ >/dev/null \
    || fail "Cannot reach PyPI. Fix networking or set a proxy first."
ok "PyPI reachable"

# Port 8765 — is something already squatting on it?
if command -v ss >/dev/null && ss -lnt 2>/dev/null | grep -q ':8765 '; then
    warn "Something already listens on :8765 —"
    ss -lntp 2>/dev/null | grep ':8765 ' | sed 's/^/      /'
    warn "Likely a hand-started 'python -m rsc_host'. Stop it before installing the unit."
fi

[[ "$DRY_RUN" == true ]] && warn "DRY RUN — nothing below will actually change."

# ════════════════════════════════════════════════════════════════════════════
# APT PACKAGES
# ════════════════════════════════════════════════════════════════════════════
if [[ "$SKIP_APT" == true ]]; then
    step "System packages (skipped: --skip-apt)"
else
    step "System packages"
    log "Updating apt index (this is the slow step)…"
    run sudo apt-get update -qq

    # Grouped by why they are here, so future-you can prune with confidence.
    APT_PACKAGES=(
        # build + python
        build-essential python3-dev python3-venv python3-pip
        # audio: sounddevice is a binding, PortAudio is the actual library
        libportaudio2 portaudio19-dev
        # serial link to the CYD
        python3-serial
        # i2c tooling for bench diagnostics
        i2c-tools
        # mDNS advertising (rsc-shiny.local)
        avahi-daemon avahi-utils
        # operator comfort on a headless box
        git curl jq tmux
    )
    log "Installing: ${APT_PACKAGES[*]}"
    run sudo apt-get install -y "${APT_PACKAGES[@]}"
    ok "apt packages installed"
fi

# ════════════════════════════════════════════════════════════════════════════
# PIGPIO — the daemon every GPIO peripheral depends on
# ════════════════════════════════════════════════════════════════════════════
step "pigpio"

PIGPIOD_BIN="$(command -v pigpiod || true)"
if [[ -n "$PIGPIOD_BIN" ]]; then
    ok "pigpiod present at ${PIGPIOD_BIN}"
else
    log "pigpiod not found — installing from apt…"
    run sudo apt-get install -y pigpio
    PIGPIOD_BIN="$(command -v pigpiod || echo /usr/bin/pigpiod)"
    [[ "$DRY_RUN" == true || -x "$PIGPIOD_BIN" ]] \
        || fail "pigpiod still missing after apt install. Build from source: https://abyz.me.uk/rpi/pigpio/download.html"
    ok "pigpiod installed at ${PIGPIOD_BIN}"
fi

# The apt package ships a unit; a source build usually does not. Write one if
# it is missing, binding to localhost only (-l) so the GPIO daemon is not
# exposed to the LAN.
if ! systemctl list-unit-files 2>/dev/null | grep -q '^pigpiod\.service'; then
    log "No pigpiod.service found — writing one…"
    if [[ "$DRY_RUN" == true ]]; then
        echo "  [dry-run] would write /etc/systemd/system/pigpiod.service"
    else
        sudo tee /etc/systemd/system/pigpiod.service >/dev/null <<EOF
[Unit]
Description=Pigpio daemon
After=multi-user.target

[Service]
Type=forking
# -l binds to localhost only. Do not remove: pigpiod has no authentication.
ExecStart=${PIGPIOD_BIN} -l
ExecStop=/bin/systemctl kill pigpiod
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
    fi
    run sudo systemctl daemon-reload
fi

log "Enabling pigpiod at boot and starting it now…"
run sudo systemctl enable --now pigpiod

# Bounded wait, then a real liveness probe — not just 'is-active'.
if [[ "$DRY_RUN" == false ]]; then
    for i in 1 2 3 4 5; do
        systemctl is-active --quiet pigpiod && break
        sleep 2
        if [[ "$i" -eq 5 ]]; then
            warn "pigpiod not active after 10s. Recent journal:"
            sudo journalctl -u pigpiod -n 20 --no-pager | sed 's/^/      /'
        fi
    done
fi

# ════════════════════════════════════════════════════════════════════════════
# HARDWARE INTERFACES — UART for the CYD, I2C for bench tooling
# ════════════════════════════════════════════════════════════════════════════
step "Hardware interfaces"

if command -v raspi-config >/dev/null; then
    # Newer raspi-config splits serial into hardware and console. Older builds
    # expose only do_serial. Handle both without failing on whichever is absent.
    if sudo raspi-config nonint 2>&1 | grep -q do_serial_hw; then
        run sudo raspi-config nonint do_serial_hw 0   # UART on
        run sudo raspi-config nonint do_serial_cons 1 # login console off
    else
        run sudo raspi-config nonint do_serial 2      # UART on, console off
    fi
    run sudo raspi-config nonint do_i2c 0
    ok "UART enabled, serial console disabled, I2C enabled"
else
    warn "raspi-config absent — configuring /boot/firmware/config.txt directly"
    ensure_line "enable_uart=1" /boot/firmware/config.txt true
    ensure_line "dtparam=i2c_arm=on" /boot/firmware/config.txt true
fi

ensure_line "i2c-dev" /etc/modules true
run sudo modprobe i2c-dev || warn "i2c-dev not loaded; needs a reboot."

if [[ ! -e /dev/serial0 ]]; then
    warn "/dev/serial0 not present yet — appears after reboot."
    REBOOT_REQUIRED=true
else
    ok "/dev/serial0 present"
fi

# ---- Groups: unprivileged access to GPIO, SPI, serial, audio ---------------
log "Adding ${USER} to hardware groups…"
for grp in gpio spi i2c dialout audio video; do
    if getent group "$grp" >/dev/null; then
        if id -nG "$USER" | tr ' ' '\n' | grep -qx "$grp"; then
            ok "already in ${grp}"
        else
            run sudo usermod -aG "$grp" "$USER"
            log "added ${USER} to ${grp}"
            REBOOT_REQUIRED=true
        fi
    fi
done
warn "Group changes need a re-login (or reboot) before they take effect."

# ════════════════════════════════════════════════════════════════════════════
# PYTHON ENVIRONMENT
# ════════════════════════════════════════════════════════════════════════════
step "Python environment"

# Rebuild the venv if it exists but is broken — a half-built venv causes the
# 203/EXEC class of systemd failure, which is miserable to diagnose later.
if [[ -d "$VENV_DIR" ]] && ! "$VENV_DIR/bin/python" --version &>/dev/null; then
    warn "Existing venv at ${VENV_DIR} is broken — recreating."
    run rm -rf "$VENV_DIR"
fi

if [[ ! -d "$VENV_DIR" ]]; then
    log "Creating venv at ${VENV_DIR}…"
    run "$PY_SYS" -m venv "$VENV_DIR"
else
    ok "venv exists at ${VENV_DIR}"
fi

PY_VENV="$VENV_DIR/bin/python"
if [[ "$DRY_RUN" == false ]]; then
    [[ -x "$PY_VENV" ]] || fail "venv python missing at ${PY_VENV}. Delete ${VENV_DIR} and re-run."
fi

log "Installing the host package (editable)…"
run "$PY_VENV" -m pip install --upgrade pip --quiet
if [[ "$DRY_RUN" == false ]]; then
    if grep -q '\[project.optional-dependencies\]' "$REPO_DIR/pyproject.toml" \
       && grep -qE '^\s*pi\s*=' "$REPO_DIR/pyproject.toml"; then
        "$PY_VENV" -m pip install -e "$REPO_DIR[pi]"
    else
        "$PY_VENV" -m pip install -e "$REPO_DIR"
    fi
else
    echo "  [dry-run] would pip install -e ${REPO_DIR}"
fi

# Backends the daemon needs on a Pi that a generic pyproject may not pin.
log "Ensuring Pi-specific runtime deps…"
run "$PY_VENV" -m pip install --quiet pigpio gpiozero rpi_ws281x sounddevice pyserial

# Freeze for reproducibility — cheap, and invaluable when a wheel changes
# under you three months from now.
if [[ "$DRY_RUN" == false ]]; then
    "$PY_VENV" -m pip freeze > "$REPO_DIR/requirements.lock"
    ok "Pinned → ${REPO_DIR}/requirements.lock"
fi

# ════════════════════════════════════════════════════════════════════════════
# TOKEN
# ════════════════════════════════════════════════════════════════════════════
step "Auth token"

run mkdir -p "$TOKEN_DIR"
run chmod 700 "$TOKEN_DIR"

if [[ -s "$TOKEN_FILE" ]]; then
    ok "Existing token at ${TOKEN_FILE} — keeping it."
    log "Read it with: cat ${TOKEN_FILE}"
else
    echo "  The token gates LAN access to the robot. A memorable word is fine"
    echo "  on a trusted home network; press Enter for a strong random one."
    CHOSEN=$(ask "Token (blank = generate)" "")
    if [[ -z "$CHOSEN" ]]; then
        CHOSEN=$("$PY_SYS" -c 'import secrets; print(secrets.token_urlsafe(24))')
        log "Generated a random token."
    fi
    if [[ "$DRY_RUN" == false ]]; then
        printf '%s\n' "$CHOSEN" > "$TOKEN_FILE"
        chmod 600 "$TOKEN_FILE"
    fi
    ok "Token written to ${TOKEN_FILE} (mode 600)"
fi

# ════════════════════════════════════════════════════════════════════════════
# SMOKE TEST — assert the things the daemon will assume at boot
# ════════════════════════════════════════════════════════════════════════════
step "Smoke test"

if [[ "$DRY_RUN" == true ]]; then
    warn "Skipped in dry-run mode."
else
    # 1. Package imports from the venv python, not from system python.
    if "$PY_VENV" -c 'import rsc_host' 2>/dev/null; then
        ok "import rsc_host"
    else
        bad "import rsc_host FAILED — the unit will exit 1/FAILURE. Detail:"
        "$PY_VENV" -c 'import rsc_host' 2>&1 | tail -5 | sed 's/^/      /'
    fi

    # 2. pigpiod answers on its socket. 'pigs t' returns a tick on success.
    if command -v pigs >/dev/null && pigs t >/dev/null 2>&1; then
        ok "pigpiod responds (pigs t)"
    else
        bad "pigpiod not responding. Check: sudo systemctl status pigpiod"
    fi

    # 3. gpiozero can actually reach pigpio through the configured pin factory.
    if GPIOZERO_PIN_FACTORY="$PIN_FACTORY" "$PY_VENV" -c '
from gpiozero import Device
Device.ensure_pin_factory()
print(type(Device.pin_factory).__name__)
' >/dev/null 2>&1; then
        ok "gpiozero pin factory: ${PIN_FACTORY}"
    else
        bad "gpiozero cannot initialise the ${PIN_FACTORY} pin factory."
    fi

    # 4. Arcade button reads high when unpressed (pull-up on GPIO 23).
    if command -v pigs >/dev/null && pigs t >/dev/null 2>&1; then
        pigs m 23 r >/dev/null 2>&1 || true
        pigs pud 23 u >/dev/null 2>&1 || true
        BTN=$(pigs r 23 2>/dev/null || echo "?")
        if [[ "$BTN" == "1" ]]; then
            ok "arcade button (GPIO 23) reads 1 — pulled up, unpressed"
        else
            bad "GPIO 23 reads '${BTN}' — expected 1. Check wiring, or the button is held down."
        fi
    fi

    # 5. Serial link to the CYD.
    if [[ -c /dev/serial0 ]]; then
        if [[ -w /dev/serial0 ]]; then
            ok "/dev/serial0 present and writable"
        else
            bad "/dev/serial0 present but not writable — dialout group not active yet (re-login)."
        fi
    else
        bad "/dev/serial0 missing — reboot needed for the UART change."
    fi

    # 6. PortAudio is actually loadable, not merely pip-installed.
    if "$PY_VENV" -c 'import sounddevice; sounddevice.query_devices()' >/dev/null 2>&1; then
        DEV_COUNT=$("$PY_VENV" -c 'import sounddevice; print(len(sounddevice.query_devices()))' 2>/dev/null)
        ok "sounddevice works (${DEV_COUNT} audio devices)"
    else
        bad "sounddevice cannot enumerate devices — libportaudio2 missing or no audio hardware."
    fi

    # 7. mDNS, so the console can find rsc-<name>.local.
    if systemctl is-active --quiet avahi-daemon; then
        ok "avahi-daemon running — $(hostname).local resolvable"
    else
        bad "avahi-daemon not running; the console's hostname scan will miss this Pi."
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════════════════════
echo ""
step "Summary"
echo "  Board        : ${PI_MODEL}"
echo "  Repo         : ${REPO_DIR}"
echo "  venv         : ${VENV_DIR}"
echo "  Pin factory  : ${PIN_FACTORY}"
echo "  Token file   : ${TOKEN_FILE}"
echo "  Smoke test   : $([[ "$SMOKE_OK" == true ]] && echo 'PASSED ✓' || echo 'ISSUES — see ✗ lines above')"
echo "  Log          : ${LOG_FILE}"
echo ""

if [[ "$REBOOT_REQUIRED" == true ]]; then
    warn "A reboot is required (UART and/or group membership changed)."
    echo "    sudo reboot"
    echo ""
fi

echo "Next steps:"
if [[ "$REBOOT_REQUIRED" == true ]]; then
    echo "  1. sudo reboot"
    echo "  2. cd ${REPO_DIR} && source .venv/bin/activate"
    echo "  3. bash scripts/install-systemd.sh"
else
    echo "  1. cd ${REPO_DIR} && source .venv/bin/activate"
    echo "  2. bash scripts/install-systemd.sh"
fi
echo ""
echo "  Or run in the foreground first, to watch it boot:"
echo "     RSC_HOST_TOKEN=\$(cat ${TOKEN_FILE}) \\"
echo "     RSC_HOST_BACKEND=pi RSC_HOST_BIND=0.0.0.0 \\"
echo "     ${PY_VENV} -m rsc_host"
echo ""
echo "=== install.sh finished at $(date -Iseconds) ==="

[[ "$SMOKE_OK" == true ]] || exit 1
