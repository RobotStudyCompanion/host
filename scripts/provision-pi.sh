#!/usr/bin/env bash
# provision-pi.sh — Robot Study Companion box provisioning
#
# Configures the *machine*: filesystem, hostname, editor, shell tooling,
# compressed swap, Syncthing, and the local LLM that gives the RSC its voice.
# Hands off to scripts/install.sh, which configures the *host stack*.
#
# Usage:
#   bash scripts/provision-pi.sh
#   bash scripts/provision-pi.sh --unattended --hostname shiny
#   bash scripts/provision-pi.sh --dry-run
#   bash scripts/provision-pi.sh --no-ollama --no-syncthing
#   bash scripts/provision-pi.sh --help
#
# Idempotent. Safe to re-run after a reboot or a partial failure.
# Targets 64-bit Raspberry Pi OS (Trixie / Debian 13) on a Pi 4.

# ---- Source-safety ----------------------------------------------------------
_RSC_PROVISION_STANDALONE=false
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    _RSC_PROVISION_STANDALONE=true
    set -euo pipefail
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_FILE="$HOME/rsc-provision.log"

UNATTENDED=false
DRY_RUN=false
SKIP_APT=false
WITH_OLLAMA=true
WITH_SYNCTHING=true
WANT_HOSTNAME=""
WANT_TZ=""
WANT_MODEL=""
REBOOT_REQUIRED=false
SMOKE_OK=true
RUN_INSTALL=true

while [[ $# -gt 0 ]]; do
    case "$1" in
        --unattended)    UNATTENDED=true ;;
        --dry-run)       DRY_RUN=true ;;
        --skip-apt)      SKIP_APT=true ;;
        --no-ollama)     WITH_OLLAMA=false ;;
        --no-syncthing)  WITH_SYNCTHING=false ;;
        --no-install)    RUN_INSTALL=false ;;
        --hostname)      WANT_HOSTNAME="$2"; shift ;;
        --timezone)      WANT_TZ="$2"; shift ;;
        --model)         WANT_MODEL="$2"; shift ;;
        --help|-h)       sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
        *)               echo "Unknown argument: $1  (try --help)"; exit 2 ;;
    esac
    shift
done

if [[ "$_RSC_PROVISION_STANDALONE" == true && "$DRY_RUN" == false ]]; then
    exec > >(tee -a "$LOG_FILE") 2>&1
fi
echo "=== provision-pi.sh started at $(date -Iseconds) on $(hostname) ==="

# ---- Helpers ----------------------------------------------------------------
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
fail() { echo -e "${RED}[fail]${NC} $*"; exit 1; }
step() { echo ""; echo -e "${BLUE}── $* ──${NC}"; }
ok()   { echo "  ✓  $*"; }
bad()  { echo "  ✗  $*"; SMOKE_OK=false; }

run() {
    if [[ "$DRY_RUN" == true ]]; then echo "  [dry-run] $*"; else "$@"; fi
}

ensure_line() {
    local line="$1" file="$2" use_sudo="${3:-false}"
    grep -qxF "$line" "$file" 2>/dev/null && return 0
    if [[ "$use_sudo" == true ]]; then
        run bash -c "printf '%s\n' \"$line\" | sudo tee -a '$file' >/dev/null"
    else
        run bash -c "printf '%s\n' \"$line\" >> '$file'"
    fi
    log "appended to ${file}: ${line}"
}

ask() {
    local prompt="$1" default="$2" reply
    if [[ "$UNATTENDED" == true ]]; then echo "$default"; return; fi
    if [[ ! -t 0 && ! -e /dev/tty ]]; then echo "$default"; return; fi
    read -rp "  ${prompt} [${default}]: " reply </dev/tty || reply=""
    echo "${reply:-$default}"
}

# ════════════════════════════════════════════════════════════════════════════
step "Preflight"
# ════════════════════════════════════════════════════════════════════════════

[[ $EUID -ne 0 ]] || fail "Run as your regular user, not root."
command -v sudo >/dev/null || fail "sudo not found."
[[ "$(uname -m)" == "aarch64" ]] || fail "Not aarch64 — this targets 64-bit Raspberry Pi OS."

PI_MODEL=$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo "unknown")
case "$PI_MODEL" in
    *"Raspberry Pi 5"*) fail "Pi 5 detected — the peripheral stack needs mainline pigpio, which cannot drive RP1." ;;
    *"Raspberry Pi 4"*) ok "Board: ${PI_MODEL}" ;;
    *)                  warn "Unrecognised board '${PI_MODEL}'. Continuing cautiously." ;;
esac

RAM_MB=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
log "RAM: ${RAM_MB} MB"

if [[ "$SKIP_APT" == false ]]; then
    curl -fsS --max-time 8 https://deb.debian.org/ >/dev/null \
        || fail "Cannot reach the Debian apt mirror."
    ok "apt mirror reachable"
fi

[[ "$DRY_RUN" == true ]] && warn "DRY RUN — nothing below will actually change."

# ════════════════════════════════════════════════════════════════════════════
step "Root filesystem"
# ════════════════════════════════════════════════════════════════════════════
# Raspberry Pi OS expands root automatically on first boot via
# init=/usr/lib/raspberrypi-sys-mods/firstboot. It only fails to do so when an
# extra partition sits after root. So check before acting — blindly calling
# do_expand_rootfs on an already-expanded card costs you a needless reboot.

ROOT_SRC=$(findmnt -no SOURCE / 2>/dev/null || echo "")
if [[ -n "$ROOT_SRC" && -b "$ROOT_SRC" ]]; then
    ROOT_DISK="/dev/$(lsblk -no PKNAME "$ROOT_SRC" 2>/dev/null | head -1)"
    if [[ -b "$ROOT_DISK" ]]; then
        DISK_B=$(sudo blockdev --getsize64 "$ROOT_DISK")
        PART_B=$(sudo blockdev --getsize64 "$ROOT_SRC")
        SLACK_MB=$(( (DISK_B - PART_B) / 1024 / 1024 ))
        if [[ "$SLACK_MB" -gt 1024 ]]; then
            warn "${SLACK_MB} MB unallocated after root — expanding."
            run sudo raspi-config nonint do_expand_rootfs
            REBOOT_REQUIRED=true
        else
            ok "Root filesystem already fills the card ($(df -h / | awk 'NR==2{print $2}'))"
        fi
    fi
else
    warn "Could not identify the root device — skipping expansion check."
fi

# ════════════════════════════════════════════════════════════════════════════
step "Identity: hostname and time"
# ════════════════════════════════════════════════════════════════════════════

CURRENT_HOST=$(hostname)
NEW_HOST="${WANT_HOSTNAME:-$(ask "Hostname (the robot's name)" "$CURRENT_HOST")}"
if [[ "$NEW_HOST" != "$CURRENT_HOST" ]]; then
    if [[ "$NEW_HOST" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
        run sudo raspi-config nonint do_hostname "$NEW_HOST"
        log "Hostname set to ${NEW_HOST} — reachable as ${NEW_HOST}.local"
        REBOOT_REQUIRED=true
    else
        warn "'${NEW_HOST}' is not a valid hostname (lowercase letters, digits, hyphens). Keeping ${CURRENT_HOST}."
        NEW_HOST="$CURRENT_HOST"
    fi
else
    ok "Hostname: ${CURRENT_HOST}"
fi

CURRENT_TZ=$(timedatectl show --property=Timezone --value 2>/dev/null || echo "UTC")
NEW_TZ="${WANT_TZ:-$(ask "Timezone" "$CURRENT_TZ")}"
if [[ "$NEW_TZ" != "$CURRENT_TZ" ]]; then
    run sudo timedatectl set-timezone "$NEW_TZ" \
        || warn "Failed to set '${NEW_TZ}'. List valid names: timedatectl list-timezones"
fi
run sudo timedatectl set-ntp true
ok "Timezone ${NEW_TZ}, NTP on"

# Persistent journal, so a crash at 3am is still readable at 9am.
run sudo mkdir -p /var/log/journal
run sudo systemctl restart systemd-journald
ok "Persistent journald enabled"

# ════════════════════════════════════════════════════════════════════════════
step "Shell tooling"
# ════════════════════════════════════════════════════════════════════════════

if [[ "$SKIP_APT" == true ]]; then
    warn "Skipped (--skip-apt)"
else
    log "Updating apt index…"
    run sudo apt-get update -qq
    log "Upgrading existing packages (slow — go make tea)…"
    run sudo apt-get full-upgrade -y

    APT_PACKAGES=(
        micro tmux btop htop ncdu tree
        git curl wget jq rsync unzip
        ca-certificates zram-tools
        avahi-daemon avahi-utils
        libraspberrypi-bin
    )
    log "Installing: ${APT_PACKAGES[*]}"
    run sudo apt-get install -y "${APT_PACKAGES[@]}"
    ok "Packages installed"
fi

# ---- micro as the default editor everywhere --------------------------------
if command -v micro >/dev/null || [[ "$DRY_RUN" == true ]]; then
    if ! grep -q "EDITOR=micro" "$HOME/.bashrc" 2>/dev/null; then
        if [[ "$DRY_RUN" == false ]]; then
            {
                echo ''
                echo '# Default editor (added by provision-pi.sh)'
                echo 'export EDITOR=micro'
                echo 'export VISUAL=micro'
                echo 'export SUDO_EDITOR=micro'
            } >> "$HOME/.bashrc"
        fi
        log "micro set as default editor in .bashrc"
    fi
    run sudo update-alternatives --install /usr/bin/editor editor /usr/bin/micro 100 2>/dev/null || true
    run sudo update-alternatives --set editor /usr/bin/micro 2>/dev/null || true
    ok "micro is the default editor"
fi

# ---- tmux: a config that does not fight you --------------------------------
if [[ ! -f "$HOME/.tmux.conf" && "$DRY_RUN" == false ]]; then
    cat > "$HOME/.tmux.conf" <<'EOF'
# Minimal tmux config for long-running RSC sessions.
set -g mouse on
set -g history-limit 20000
set -g base-index 1
setw -g pane-base-index 1
set -g status-style 'bg=colour236 fg=colour250'
set -g status-right '#[fg=colour245]#H #[fg=colour250]%H:%M'
EOF
    log "Wrote ~/.tmux.conf"
fi

# ════════════════════════════════════════════════════════════════════════════
step "ZRAM compressed swap"
# ════════════════════════════════════════════════════════════════════════════
# Compressed in-RAM swap. Two reasons this matters here: it spares the SD card
# the write amplification of a swapfile, and it buys headroom when Ollama loads
# a model alongside the daemon.

if [[ "$DRY_RUN" == false ]]; then
    sudo tee /etc/default/zramswap >/dev/null <<'EOF'
# Compressed in-RAM swap. Kinder to the SD card than dphys-swapfile.
ALGO=lz4
PERCENT=50
PRIORITY=100
EOF
else
    echo "  [dry-run] would write /etc/default/zramswap"
fi

run sudo modprobe zram 2>/dev/null || warn "zram module not loadable on this kernel."

# The Pi kernel auto-configures /dev/zram0 at module load, after which
# zram-tools fails with "Device or resource busy" trying to rewrite its
# parameters. Reset the device first so the service configures cleanly.
if [[ -e /sys/block/zram0 && "$DRY_RUN" == false ]]; then
    sudo swapoff /dev/zram0 2>/dev/null || true
    echo 1 | sudo tee /sys/block/zram0/reset >/dev/null 2>&1 || true
fi

if [[ "$DRY_RUN" == false ]]; then
    if sudo systemctl restart zramswap 2>/dev/null && sudo systemctl enable zramswap 2>/dev/null; then
        sleep 1
        ZRAM_LINE=$(swapon --show 2>/dev/null | grep -i zram || echo "")
        [[ -n "$ZRAM_LINE" ]] && ok "ZRAM active: ${ZRAM_LINE}" \
                              || warn "zramswap started but no zram device in swapon."
    else
        warn "zramswap failed. Diagnose: systemctl status zramswap; journalctl -u zramswap"
    fi
    sudo systemctl disable --now dphys-swapfile 2>/dev/null || true
fi

# ════════════════════════════════════════════════════════════════════════════
step "Syncthing"
# ════════════════════════════════════════════════════════════════════════════

if [[ "$WITH_SYNCTHING" == false ]]; then
    warn "Skipped (--no-syncthing)"
elif [[ "$DRY_RUN" == true ]]; then
    echo "  [dry-run] would install and enable syncthing@${USER}"
else
    if ! command -v syncthing >/dev/null; then
        log "Adding the Syncthing apt repo…"
        sudo mkdir -p /etc/apt/keyrings
        sudo curl -fsSL -o /etc/apt/keyrings/syncthing-archive-keyring.gpg \
            https://syncthing.net/release-key.gpg
        echo "deb [signed-by=/etc/apt/keyrings/syncthing-archive-keyring.gpg] https://apt.syncthing.net/ syncthing stable" \
            | sudo tee /etc/apt/sources.list.d/syncthing.list >/dev/null
        sudo apt-get update -qq
        sudo apt-get install -y syncthing
    fi
    sudo systemctl enable --now "syncthing@${USER}.service"
    sleep 3
    DEVICE_ID=$(syncthing --device-id 2>/dev/null || echo "unknown")
    ok "Syncthing running — device ID: ${DEVICE_ID}"
    log "GUI over SSH: ssh -L 8384:localhost:8384 ${USER}@${NEW_HOST}.local"
fi

# ════════════════════════════════════════════════════════════════════════════
step "Ollama — local inference"
# ════════════════════════════════════════════════════════════════════════════

if [[ "$WITH_OLLAMA" == false ]]; then
    warn "Skipped (--no-ollama)"
else
    # ---- RAM-gated model selection -----------------------------------------
    # A Pi 4 runs models on four Cortex-A72 cores with no acceleration. These
    # thresholds keep the model plus the daemon plus the page cache inside RAM.
    if [[ -n "$WANT_MODEL" ]]; then
        OLLAMA_MODEL="$WANT_MODEL"
        log "Model overridden on the command line: ${OLLAMA_MODEL}"
    elif [[ "$RAM_MB" -ge 7000 ]]; then
        OLLAMA_MODEL="llama3.2:1b"
    elif [[ "$RAM_MB" -ge 3500 ]]; then
        OLLAMA_MODEL="qwen3:0.6b"
    elif [[ "$RAM_MB" -ge 1800 ]]; then
        OLLAMA_MODEL="qwen3:0.6b"
        warn "${RAM_MB} MB RAM is marginal. Expect swapping when the daemon and model run together."
    else
        OLLAMA_MODEL=""
        warn "${RAM_MB} MB RAM — too little for local inference. Skipping the model pull."
        warn "Point the daemon at a remote endpoint instead."
    fi
    [[ -n "$OLLAMA_MODEL" ]] && log "Selected model: ${OLLAMA_MODEL} (${RAM_MB} MB RAM)"

    # ---- Install ------------------------------------------------------------
    if command -v ollama >/dev/null && [[ -s "$(command -v ollama)" ]]; then
        ok "Ollama present: $(ollama --version 2>/dev/null | head -1)"
    else
        log "Installing Ollama (official ARM64 installer)…"
        if [[ "$DRY_RUN" == true ]]; then
            echo "  [dry-run] would curl -fsSL https://ollama.com/install.sh | sh"
        else
            curl -fsSL https://ollama.com/install.sh | sh
            command -v ollama >/dev/null || fail "Ollama installer ran but no binary appeared."
        fi
    fi

    # ---- systemd override for a small, shared, SD-card-backed machine -------
    if [[ "$DRY_RUN" == false ]]; then
        sudo mkdir -p /etc/systemd/system/ollama.service.d
        sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null <<'EOF'
[Service]
# One model, one request at a time. The daemon needs cores for servos and audio.
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_FLASH_ATTENTION=1"
# Keep the model resident between turns. Reloading from SD costs 30-60 seconds,
# which is unbearable mid-conversation.
Environment="OLLAMA_KEEP_ALIVE=30m"
# Bind to loopback. Nothing off-box should reach the inference endpoint.
Environment="OLLAMA_HOST=127.0.0.1:11434"
# Yield to the daemon under contention: the robot must stay responsive even
# when a generation is in flight.
Nice=5
CPUWeight=50
EOF
        sudo systemctl daemon-reload
        sudo systemctl enable --now ollama
        sudo systemctl restart ollama

        for i in 1 2 3 4 5; do
            curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
            sleep 3
            if [[ "$i" -eq 5 ]]; then
                warn "Ollama API silent after 15s. Recent journal:"
                sudo journalctl -u ollama -n 20 --no-pager | sed 's/^/      /'
            fi
        done
        curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1 \
            && ok "Ollama API responding on 127.0.0.1:11434" \
            || bad "Ollama API not responding."
    fi

    # ---- Pull ---------------------------------------------------------------
    if [[ -n "$OLLAMA_MODEL" && "$DRY_RUN" == false ]]; then
        if ollama list 2>/dev/null | grep -qF "$OLLAMA_MODEL"; then
            ok "${OLLAMA_MODEL} already pulled"
        else
            log "Pulling ${OLLAMA_MODEL} — first pull takes several minutes over SD…"
            ollama pull "$OLLAMA_MODEL" || bad "Pull of ${OLLAMA_MODEL} failed."
        fi
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
step "Smoke test"
# ════════════════════════════════════════════════════════════════════════════

if [[ "$DRY_RUN" == true ]]; then
    warn "Skipped in dry-run mode."
else
    # Thermals — a throttled Pi 4 invalidates every timing number below.
    if command -v vcgencmd >/dev/null; then
        THROTTLED=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
        TEMP=$(vcgencmd measure_temp 2>/dev/null | cut -d= -f2)
        if [[ "$THROTTLED" == "0x0" ]]; then
            ok "No throttling, core at ${TEMP}"
        else
            warn "get_throttled=${THROTTLED} at ${TEMP} — undervoltage or thermal limiting."
            warn "Check the PSU and cooling before trusting any inference timings."
        fi
    fi

    # ZRAM
    swapon --show 2>/dev/null | grep -qi zram \
        && ok "ZRAM swap active" \
        || bad "No ZRAM swap — memory pressure will hit the SD card."

    # mDNS
    systemctl is-active --quiet avahi-daemon \
        && ok "avahi-daemon running — ${NEW_HOST}.local resolvable" \
        || bad "avahi-daemon not running."

    # Ollama: prewarm on a long leash, then measure a real generation.
    if [[ "$WITH_OLLAMA" == true && -n "${OLLAMA_MODEL:-}" ]]; then
        log "Prewarming ${OLLAMA_MODEL} — cold load from SD is slow, allow up to 5 min…"
        timeout 300 ollama run "$OLLAMA_MODEL" "hi" >/dev/null 2>&1 \
            || warn "Prewarm timed out. The model may still work, just slowly."

        log "Measuring generation throughput…"
        GEN_JSON=$(curl -sf --max-time 180 http://127.0.0.1:11434/api/generate \
            -d "{\"model\":\"${OLLAMA_MODEL}\",\"prompt\":\"Say hello in one short sentence.\",\"stream\":false}" \
            2>/dev/null || echo "")
        if [[ -n "$GEN_JSON" ]] && echo "$GEN_JSON" | jq -e '.response' >/dev/null 2>&1; then
            TPS=$(echo "$GEN_JSON" | jq -r '
                if (.eval_count // 0) > 0 and (.eval_duration // 0) > 0
                then (.eval_count / (.eval_duration / 1000000000) | . * 10 | round / 10)
                else "?" end')
            ok "${OLLAMA_MODEL} generated at ~${TPS} tokens/sec"
            if [[ "$TPS" != "?" ]] && awk "BEGIN{exit !($TPS < 3)}"; then
                warn "Under 3 tokens/sec makes conversation painful."
                warn "Consider a remote endpoint with this model kept as an offline fallback."
            fi
        else
            bad "${OLLAMA_MODEL} did not generate within 180s."
        fi
    fi

    # Disk headroom after everything landed
    FREE_GB=$(df -BG --output=avail / | tail -1 | tr -d 'G ')
    [[ "$FREE_GB" -ge 3 ]] && ok "${FREE_GB}G free on /" \
                           || bad "Only ${FREE_GB}G free on / — pull no further models."
fi

# ════════════════════════════════════════════════════════════════════════════
step "Summary"
# ════════════════════════════════════════════════════════════════════════════
echo "  Board      : ${PI_MODEL}"
echo "  Hostname   : ${NEW_HOST}   (mDNS: ${NEW_HOST}.local)"
echo "  RAM        : ${RAM_MB} MB"
echo "  Root fs    : $(df -h / 2>/dev/null | awk 'NR==2{print $2" total, "$4" free"}')"
echo "  Model      : ${OLLAMA_MODEL:-none}"
echo "  Syncthing  : ${DEVICE_ID:-not installed}"
echo "  Smoke test : $([[ "$SMOKE_OK" == true ]] && echo 'PASSED ✓' || echo 'ISSUES — see ✗ lines above')"
echo "  Log        : ${LOG_FILE}"
echo ""

if [[ "$REBOOT_REQUIRED" == true ]]; then
    warn "A reboot is required (hostname and/or filesystem changed)."
fi

# ---- Hand off to the host-stack installer ----------------------------------
INSTALL_SH="$REPO_DIR/scripts/install.sh"
if [[ "$RUN_INSTALL" == true && -f "$INSTALL_SH" && "$DRY_RUN" == false ]]; then
    if [[ "$REBOOT_REQUIRED" == true ]]; then
        warn "Skipping install.sh until after the reboot."
        echo ""
        echo "Next steps:"
        echo "  1. sudo reboot"
        echo "  2. bash ${INSTALL_SH}"
    else
        CONTINUE=$(ask "Continue to install.sh (the host stack) now? [y/N]" "y")
        if [[ "$CONTINUE" =~ ^[Yy]$ ]]; then
            echo ""
            INSTALL_ARGS=()
            [[ "$UNATTENDED" == true ]] && INSTALL_ARGS+=(--unattended)
            [[ "$SKIP_APT"   == true ]] && INSTALL_ARGS+=(--skip-apt)
            exec bash "$INSTALL_SH" ${INSTALL_ARGS[@]+"${INSTALL_ARGS[@]}"}
        fi
        echo ""
        echo "Next step:  bash ${INSTALL_SH}"
    fi
else
    echo "Next step:  bash ${INSTALL_SH}"
fi

echo ""
echo "=== provision-pi.sh finished at $(date -Iseconds) ==="
[[ "$SMOKE_OK" == true ]] || exit 1
