#!/usr/bin/env bash
# rsc-doctor — one command that answers "why isn't the robot working?"
#
# Usage:
#   rsc-doctor                  # run every check, human-readable
#   rsc-doctor check            # same
#   rsc-doctor report --json    # machine-readable, for pasting into an issue
#   rsc-doctor report --md      # markdown, for pasting into a chat
#   rsc-doctor --fix            # attempt safe remediations, then re-check
#   rsc-doctor --logs           # dump the last 50 daemon journal lines and exit
#
# Design rule: this script never hardcodes a pin number, a port, or a device
# path. It imports them from rsc_host.config, so bash cannot drift away from
# the daemon. Where a config attribute is absent it falls back to a documented
# default and says so.

set -uo pipefail   # deliberately no -e: a doctor keeps examining after a
                   # finding, rather than dying on the first abnormality.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="${RSC_VENV:-$REPO_DIR/.venv}"
PY="$VENV_DIR/bin/python"
TOKEN_FILE="$HOME/.config/rsc-host/token"
UNIT="rsc-host@${USER}.service"

MODE="check"
FORMAT="human"
DO_FIX=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        check|report) MODE="$1" ;;
        --json)       FORMAT="json" ;;
        --md)         FORMAT="md" ;;
        --fix)        DO_FIX=true ;;
        --logs)       MODE="logs" ;;
        --help|-h)    sed -n '2,17p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *)            echo "Unknown argument: $1  (try --help)"; exit 2 ;;
    esac
    shift
done

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
[[ "$FORMAT" != "human" ]] && { GREEN=''; YELLOW=''; RED=''; BLUE=''; NC=''; }

PASS=0; WARN=0; FAIL=0
RESULTS=()

record() {  # record <status> <name> <detail>
    local status="$1" name="$2" detail="$3"
    RESULTS+=("${status}|${name}|${detail}")
    case "$status" in
        pass) (( PASS++ )); [[ "$FORMAT" == "human" ]] && echo -e "  ${GREEN}✓${NC}  ${name}: ${detail}" ;;
        warn) (( WARN++ )); [[ "$FORMAT" == "human" ]] && echo -e "  ${YELLOW}!${NC}  ${name}: ${detail}" ;;
        fail) (( FAIL++ )); [[ "$FORMAT" == "human" ]] && echo -e "  ${RED}✗${NC}  ${name}: ${detail}" ;;
    esac
    return 0
}
section() { [[ "$FORMAT" == "human" ]] && echo -e "\n${BLUE}── $* ──${NC}"; return 0; }

# ---- --logs shortcut --------------------------------------------------------
if [[ "$MODE" == "logs" ]]; then
    echo "── ${UNIT} (last 50) ──"
    journalctl -u "$UNIT" -n 50 --no-pager
    echo ""
    echo "── pigpiod (last 20) ──"
    journalctl -u pigpiod -n 20 --no-pager
    exit 0
fi

# ════════════════════════════════════════════════════════════════════════════
# Read the truth from Python, not from memory.
# ════════════════════════════════════════════════════════════════════════════
CFG_SOURCE="defaults"
CFG_RAW=""
if [[ -x "$PY" ]]; then
    CFG_RAW=$("$PY" - <<'PYEOF' 2>/dev/null
import json

out = {
    "port": 8765,
    "bind": "0.0.0.0",
    "serial_device": "/dev/serial0",
    "serial_baud": 115200,
    "pin_factory": "pigpio",
    "servo_pins": {},
    "button_pins": [],
    "pwm_pins": [],
}

try:
    from rsc_host import config as C
except Exception:
    print(json.dumps({"_error": "import failed", **out}))
    raise SystemExit(0)

def pick(*names, default=None):
    for n in names:
        v = getattr(C, n, None)
        if v is not None:
            return v
    return default

out["port"]          = pick("PORT", "HOST_PORT", "RSC_HOST_PORT", default=out["port"])
out["bind"]          = pick("BIND", "HOST_BIND", default=out["bind"])
out["serial_device"] = pick("SERIAL_DEVICE", "CYD_DEVICE", "SERIAL_PORT", default=out["serial_device"])
out["serial_baud"]   = pick("SERIAL_BAUD", "CYD_BAUD", default=out["serial_baud"])
out["pin_factory"]   = pick("PIN_FACTORY", default=out["pin_factory"])
out["servo_pins"]    = pick("SERVO_PINS", "SERVOS", default={}) or {}
out["button_pins"]   = pick("BUTTON_PINS", "INPUT_PINS", default=[]) or []
out["pwm_pins"]      = pick("PWM_PINS", "LED_PINS", default=[]) or []

# Normalise: pins may arrive as dicts, tuples, or bare ints.
def as_list(v):
    if isinstance(v, dict):
        return [int(x) for x in v.values()]
    if isinstance(v, (list, tuple, set)):
        return [int(x) for x in v]
    if isinstance(v, int):
        return [v]
    return []

out["button_pins"] = as_list(out["button_pins"])
out["pwm_pins"]    = as_list(out["pwm_pins"])
if isinstance(out["servo_pins"], dict):
    out["servo_pins"] = {str(k): int(v) for k, v in out["servo_pins"].items()}

print(json.dumps(out))
PYEOF
)
fi

if [[ -n "$CFG_RAW" ]] && echo "$CFG_RAW" | jq -e . >/dev/null 2>&1; then
    if echo "$CFG_RAW" | jq -e '._error' >/dev/null 2>&1; then
        CFG_SOURCE="defaults (rsc_host.config would not import)"
    else
        CFG_SOURCE="rsc_host.config"
    fi
else
    CFG_RAW='{"port":8765,"bind":"0.0.0.0","serial_device":"/dev/serial0","serial_baud":115200,"pin_factory":"pigpio","servo_pins":{},"button_pins":[],"pwm_pins":[]}'
fi

cfg() { echo "$CFG_RAW" | jq -r "$1"; }
PORT=$(cfg '.port')
SERIAL_DEV=$(cfg '.serial_device')
PIN_FACTORY=$(cfg '.pin_factory')
mapfile -t BUTTON_PINS < <(cfg '.button_pins[]?')
mapfile -t PWM_PINS    < <(cfg '.pwm_pins[]?')
mapfile -t SERVO_PINS  < <(cfg '.servo_pins | to_entries[]? | "\(.key)=\(.value)"')

[[ "$FORMAT" == "human" ]] && {
    echo ""
    echo "rsc-doctor — $(date '+%Y-%m-%d %H:%M:%S') on $(hostname)"
    echo "config source: ${CFG_SOURCE}"
}

# ════════════════════════════════════════════════════════════════════════════
section "Platform"
# ════════════════════════════════════════════════════════════════════════════
PI_MODEL=$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo "unknown")
case "$PI_MODEL" in
    *"Raspberry Pi 4"*) record pass "board" "$PI_MODEL" ;;
    *"Raspberry Pi 5"*) record fail "board" "$PI_MODEL — pigpio cannot drive RP1" ;;
    *)                  record warn "board" "$PI_MODEL (unrecognised)" ;;
esac

UP=$(uptime -p 2>/dev/null | sed 's/^up //')
record pass "uptime" "${UP:-unknown}"

if command -v vcgencmd >/dev/null; then
    T=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
    TEMP=$(vcgencmd measure_temp 2>/dev/null | cut -d= -f2)
    if [[ "$T" == "0x0" ]]; then
        record pass "thermal" "no throttling, ${TEMP}"
    else
        record warn "thermal" "get_throttled=${T} at ${TEMP} — undervoltage or heat"
    fi
fi

FREE_GB=$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -d 'G ')
MEM_FREE=$(free -m | awk '/^Mem:/{print $7}')
[[ "${FREE_GB:-0}" -ge 2 ]] && record pass "disk" "${FREE_GB}G free" \
                            || record fail "disk" "only ${FREE_GB}G free"
[[ "${MEM_FREE:-0}" -ge 300 ]] && record pass "memory" "${MEM_FREE}MB available" \
                               || record warn "memory" "only ${MEM_FREE}MB available"
swapon --show 2>/dev/null | grep -qi zram \
    && record pass "zram" "compressed swap active" \
    || record warn "zram" "not active — SD card will take the memory pressure"

# ════════════════════════════════════════════════════════════════════════════
section "GPIO layer"
# ════════════════════════════════════════════════════════════════════════════
if systemctl is-active --quiet pigpiod; then
    record pass "pigpiod.service" "active"
else
    record fail "pigpiod.service" "not active — every GPIO peripheral is dead"
fi
systemctl is-enabled --quiet pigpiod 2>/dev/null \
    && record pass "pigpiod boot" "enabled" \
    || record warn "pigpiod boot" "not enabled — will not survive a reboot"

if command -v pigs >/dev/null; then
    if pigs t >/dev/null 2>&1; then
        record pass "pigpio socket" "responds to pigs t"
        # Buttons: pull up, expect a high reading when unpressed.
        for pin in "${BUTTON_PINS[@]}"; do
            pigs m "$pin" r >/dev/null 2>&1
            pigs pud "$pin" u >/dev/null 2>&1
            V=$(pigs r "$pin" 2>/dev/null || echo "?")
            case "$V" in
                1) record pass "button GPIO ${pin}" "reads 1 (pulled up, unpressed)" ;;
                0) record warn "button GPIO ${pin}" "reads 0 — held down, or wiring shorted" ;;
                *) record fail "button GPIO ${pin}" "unreadable" ;;
            esac
        done
        for entry in "${SERVO_PINS[@]}"; do
            name="${entry%%=*}"; pin="${entry##*=}"
            W=$(pigs gpw "$pin" 2>/dev/null || echo "?")
            record pass "servo ${name} (GPIO ${pin})" "pulse width ${W}µs"
        done
        for pin in "${PWM_PINS[@]}"; do
            D=$(pigs gdc "$pin" 2>/dev/null || echo "?")
            record pass "pwm GPIO ${pin}" "duty ${D}"
        done
    else
        record fail "pigpio socket" "pigs t failed — daemon up but socket unreachable"
    fi
else
    record warn "pigs" "not installed; cannot probe pins directly"
fi

# ════════════════════════════════════════════════════════════════════════════
section "Peripherals"
# ════════════════════════════════════════════════════════════════════════════
if [[ -c "$SERIAL_DEV" ]]; then
    if [[ -w "$SERIAL_DEV" ]]; then
        record pass "CYD serial" "${SERIAL_DEV} present and writable"
    else
        record fail "CYD serial" "${SERIAL_DEV} present but not writable — dialout group inactive"
    fi
else
    record fail "CYD serial" "${SERIAL_DEV} missing — UART disabled, or reboot pending"
fi

for grp in gpio spi i2c dialout audio; do
    id -nG "$USER" 2>/dev/null | tr ' ' '\n' | grep -qx "$grp" \
        && record pass "group ${grp}" "member" \
        || record warn "group ${grp}" "not a member (re-login after usermod?)"
done

if [[ -x "$PY" ]]; then
    if "$PY" -c 'import sounddevice; sounddevice.query_devices()' >/dev/null 2>&1; then
        N=$("$PY" -c 'import sounddevice; print(len(sounddevice.query_devices()))' 2>/dev/null)
        record pass "audio" "${N} devices via PortAudio"
    else
        record fail "audio" "sounddevice cannot enumerate — libportaudio2 missing?"
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
section "Python environment"
# ════════════════════════════════════════════════════════════════════════════
if [[ -x "$PY" ]]; then
    record pass "venv" "$($PY --version 2>&1) at ${VENV_DIR}"
    if "$PY" -c 'import rsc_host' 2>/dev/null; then
        VER=$("$PY" -c 'import rsc_host; print(getattr(rsc_host,"__version__","?"))' 2>/dev/null)
        record pass "rsc_host" "imports cleanly (version ${VER})"
    else
        DETAIL=$("$PY" -c 'import rsc_host' 2>&1 | tail -1)
        record fail "rsc_host" "import failed: ${DETAIL}"
    fi
    for mod in websockets pydantic gpiozero pigpio; do
        "$PY" -c "import ${mod}" 2>/dev/null \
            && record pass "module ${mod}" "present" \
            || record fail "module ${mod}" "missing from the venv"
    done
else
    record fail "venv" "no interpreter at ${PY} — run scripts/install.sh"
fi

# ════════════════════════════════════════════════════════════════════════════
section "Daemon"
# ════════════════════════════════════════════════════════════════════════════
if systemctl list-unit-files 2>/dev/null | grep -q '^rsc-host@'; then
    STATE=$(systemctl is-active "$UNIT" 2>/dev/null || echo "inactive")
    if [[ "$STATE" == "active" ]]; then
        SINCE=$(systemctl show "$UNIT" --property=ActiveEnterTimestamp --value 2>/dev/null)
        record pass "${UNIT}" "active since ${SINCE:-unknown}"
    else
        RESULT=$(systemctl show "$UNIT" --property=Result --value 2>/dev/null)
        record fail "${UNIT}" "${STATE} (result: ${RESULT:-none}) — see: rsc-doctor --logs"
    fi
    systemctl is-enabled --quiet "$UNIT" 2>/dev/null \
        && record pass "daemon boot" "enabled" \
        || record warn "daemon boot" "not enabled — will not start on reboot"
else
    record warn "${UNIT}" "unit not installed — run scripts/install-systemd.sh"
fi

if command -v ss >/dev/null && ss -lnt 2>/dev/null | grep -q ":${PORT} "; then
    OWNER=$(ss -lntp 2>/dev/null | grep ":${PORT} " | grep -oP 'users:\(\("\K[^"]+' | head -1)
    record pass "port ${PORT}" "listening (${OWNER:-unknown process})"
else
    record fail "port ${PORT}" "nothing listening"
fi

if [[ -s "$TOKEN_FILE" ]]; then
    PERMS=$(stat -c '%a' "$TOKEN_FILE" 2>/dev/null)
    [[ "$PERMS" == "600" ]] && record pass "token" "present, mode ${PERMS}" \
                            || record warn "token" "present but mode ${PERMS} — should be 600"
else
    record warn "token" "no token at ${TOKEN_FILE}"
fi

# Live handshake — the only check that proves the whole stack end to end.
if [[ -x "$PY" && -s "$TOKEN_FILE" ]] && "$PY" -c 'import websockets' 2>/dev/null; then
    HS=$("$PY" - "$PORT" "$TOKEN_FILE" <<'PYEOF' 2>/dev/null
import asyncio, pathlib, sys
import websockets

port = sys.argv[1]
token = pathlib.Path(sys.argv[2]).read_text().strip()

async def go():
    uri = f"ws://127.0.0.1:{port}/"
    try:
        async with websockets.connect(
            uri, subprotocols=["bearer", token], open_timeout=5
        ) as ws:
            print("ok")
    except Exception as e:
        print(f"fail:{type(e).__name__}")

asyncio.run(go())
PYEOF
)
    case "${HS:-}" in
        ok)      record pass "websocket handshake" "connected and authenticated on 127.0.0.1:${PORT}" ;;
        fail:*)  record fail "websocket handshake" "${HS#fail:} — token mismatch or daemon rejecting" ;;
        *)       record warn "websocket handshake" "inconclusive" ;;
    esac
fi

# ════════════════════════════════════════════════════════════════════════════
section "Network and inference"
# ════════════════════════════════════════════════════════════════════════════
systemctl is-active --quiet avahi-daemon \
    && record pass "mDNS" "$(hostname).local advertised" \
    || record warn "mDNS" "avahi-daemon down — the console cannot discover this Pi"

if command -v ollama >/dev/null; then
    if curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
        NMOD=$(curl -sf http://127.0.0.1:11434/api/tags | jq -r '.models | length' 2>/dev/null)
        record pass "ollama" "API up, ${NMOD:-0} models pulled"
        LOADED=$(curl -sf http://127.0.0.1:11434/api/ps 2>/dev/null | jq -r '.models[0].name // "none"' 2>/dev/null)
        record pass "ollama resident" "${LOADED}"
    else
        record warn "ollama" "installed but API not responding on 127.0.0.1:11434"
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# --fix: only remediations that cannot make things worse.
# ════════════════════════════════════════════════════════════════════════════
if [[ "$DO_FIX" == true && "$FAIL" -gt 0 ]]; then
    section "Attempting safe fixes"
    systemctl is-active --quiet pigpiod || { echo "  → starting pigpiod"; sudo systemctl enable --now pigpiod; }
    systemctl is-active --quiet avahi-daemon || { echo "  → starting avahi-daemon"; sudo systemctl enable --now avahi-daemon; }
    if systemctl list-unit-files 2>/dev/null | grep -q '^rsc-host@'; then
        if ! systemctl is-active --quiet "$UNIT"; then
            echo "  → resetting and restarting ${UNIT}"
            sudo systemctl reset-failed "$UNIT" 2>/dev/null || true
            sudo systemctl restart "$UNIT"
            sleep 4
        fi
    fi
    [[ -s "$TOKEN_FILE" && "$(stat -c '%a' "$TOKEN_FILE")" != "600" ]] && { echo "  → chmod 600 token"; chmod 600 "$TOKEN_FILE"; }
    echo ""
    echo "Fixes applied. Re-running checks…"
    exec "$0" check
fi

# ════════════════════════════════════════════════════════════════════════════
# Output
# ════════════════════════════════════════════════════════════════════════════
case "$FORMAT" in
    json)
        {
            echo "{"
            echo "  \"host\": \"$(hostname)\","
            echo "  \"timestamp\": \"$(date -Iseconds)\","
            echo "  \"config_source\": \"${CFG_SOURCE}\","
            echo "  \"summary\": {\"pass\": ${PASS}, \"warn\": ${WARN}, \"fail\": ${FAIL}},"
            echo "  \"checks\": ["
            for i in "${!RESULTS[@]}"; do
                IFS='|' read -r s n d <<< "${RESULTS[$i]}"
                COMMA=$([[ "$i" -lt $(( ${#RESULTS[@]} - 1 )) ]] && echo "," || echo "")
                printf '    {"status": "%s", "name": %s, "detail": %s}%s\n' \
                    "$s" "$(jq -Rn --arg v "$n" '$v')" "$(jq -Rn --arg v "$d" '$v')" "$COMMA"
            done
            echo "  ]"
            echo "}"
        }
        ;;
    md)
        echo "### rsc-doctor — $(hostname), $(date '+%Y-%m-%d %H:%M')"
        echo ""
        echo "Config read from \`${CFG_SOURCE}\`. **${PASS} pass, ${WARN} warn, ${FAIL} fail.**"
        echo ""
        echo "| | Check | Detail |"
        echo "|---|---|---|"
        for r in "${RESULTS[@]}"; do
            IFS='|' read -r s n d <<< "$r"
            case "$s" in pass) I="✓" ;; warn) I="!" ;; fail) I="✗" ;; esac
            echo "| ${I} | ${n} | ${d} |"
        done
        ;;
    human)
        echo ""
        echo -e "${BLUE}── Summary ──${NC}"
        echo -e "  ${GREEN}${PASS} pass${NC}   ${YELLOW}${WARN} warn${NC}   ${RED}${FAIL} fail${NC}"
        if [[ "$FAIL" -gt 0 ]]; then
            echo ""
            echo "  Try:  rsc-doctor --fix        attempt safe remediations"
            echo "        rsc-doctor --logs       read the daemon journal"
            echo "        rsc-doctor report --md  paste-ready summary"
        fi
        echo ""
        ;;
esac

[[ "$FAIL" -eq 0 ]] || exit 1
exit 0
