#!/usr/bin/env bash
# bootstrap.sh — Robot Study Companion one-line installer
#
# Published at https://rsc.ee/install.sh
#
#   curl -fsSL https://rsc.ee/install.sh | bash
#   curl -fsSL https://rsc.ee/install.sh | bash -s -- --unattended
#   curl -fsSL https://rsc.ee/install.sh | bash -s -- --branch dev --no-ollama
#
# This script is deliberately short and dumb. Read it before you pipe it to a
# shell — that is the whole point of keeping it under a hundred lines. All it
# does is install git, clone the host repo, and hand over to the real
# provisioner inside that repo, where changes are reviewable in version control.
#
# Environment overrides:
#   RSC_REPO    git URL              (default: the public GitHub repo)
#   RSC_BRANCH  branch or tag        (default: main)
#   RSC_DIR     clone destination    (default: ~/rsc-host)

set -euo pipefail

RSC_REPO="${RSC_REPO:-https://github.com/RobotStudyCompanion/host.git}"
RSC_BRANCH="${RSC_BRANCH:-main}"
RSC_DIR="${RSC_DIR:-$HOME/rsc-host}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${GREEN}[bootstrap]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
fail() { echo -e "${RED}[fail]${NC} $*" >&2; exit 1; }

# Pull out the flags we consume ourselves; pass everything else downstream.
PASSTHROUGH=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch) RSC_BRANCH="$2"; shift ;;
        --repo)   RSC_REPO="$2";   shift ;;
        --dir)    RSC_DIR="$2";    shift ;;
        *)        PASSTHROUGH+=("$1") ;;
    esac
    shift
done

# ---- Guards -----------------------------------------------------------------
[[ $EUID -ne 0 ]] || fail "Run as your regular user, not root. The scripts sudo where needed."
[[ "$(uname -m)" == "aarch64" ]] || fail "Not aarch64 — this targets 64-bit Raspberry Pi OS."
command -v sudo >/dev/null || fail "sudo not found."

MODEL=$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo "unknown")
log "Board: ${MODEL}"
case "$MODEL" in
    *"Raspberry Pi 5"*)
        fail "Pi 5 detected. Mainline pigpio cannot drive the RP1 southbridge, so the
       peripheral stack will not work. Pi 4 only for now." ;;
esac

log "Waiting for network…"
for i in $(seq 1 30); do
    curl -fsS --max-time 3 https://github.com/ >/dev/null 2>&1 && break
    [[ "$i" -eq 30 ]] && fail "No network after 90s. Fix connectivity and re-run."
    sleep 3
done

# ---- git --------------------------------------------------------------------
if ! command -v git >/dev/null; then
    log "Installing git…"
    sudo apt-get update -qq
    sudo apt-get install -y git ca-certificates
fi

# ---- Clone or update --------------------------------------------------------
if [[ -d "$RSC_DIR/.git" ]]; then
    log "Repo already at ${RSC_DIR} — fetching ${RSC_BRANCH}…"
    git -C "$RSC_DIR" fetch --quiet origin "$RSC_BRANCH"
    if [[ -n "$(git -C "$RSC_DIR" status --porcelain)" ]]; then
        warn "Local changes present in ${RSC_DIR} — not touching your working tree."
        warn "Using it as-is. Commit or stash first if you want the latest."
    else
        git -C "$RSC_DIR" checkout --quiet "$RSC_BRANCH"
        git -C "$RSC_DIR" reset --hard --quiet "origin/${RSC_BRANCH}"
        log "Updated to $(git -C "$RSC_DIR" rev-parse --short HEAD)"
    fi
else
    log "Cloning ${RSC_REPO} (${RSC_BRANCH}) → ${RSC_DIR}…"
    git clone --branch "$RSC_BRANCH" --depth 1 "$RSC_REPO" "$RSC_DIR" \
        || fail "Clone failed. If the repo is still private, clone it manually over SSH and run scripts/provision-pi.sh yourself."
fi

PROVISION="$RSC_DIR/scripts/provision-pi.sh"
[[ -f "$PROVISION" ]] || fail "Expected ${PROVISION} — wrong branch, or the repo layout changed."

# When this script arrives through a pipe, stdin is the script itself, so the
# provisioner's prompts would read garbage. Reattach stdin to the terminal.
if [[ ! -t 0 && -e /dev/tty ]]; then
    exec < /dev/tty
fi

log "Handing over to provision-pi.sh"
echo ""
exec bash "$PROVISION" ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}
