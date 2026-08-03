#!/bin/bash
# rsc-firstboot.sh — zero-touch provisioning entry point
#
# ─────────────────────────────────────────────────────────────────────────────
# HOW TO USE THIS
#
# 1. Flash Raspberry Pi OS Lite (64-bit) with Imager. Set hostname, username,
#    wifi and SSH key in the advanced options as normal — Imager writes those
#    to custom.toml and the OS applies them itself.
#
# 2. Re-mount the boot partition (it mounts automatically on most desktops as
#    "bootfs") and copy this file to it:
#
#        cp rsc-firstboot.sh /media/$USER/bootfs/rsc-firstboot.sh
#
# 3. Append this to the END of the single line in cmdline.txt on the same
#    partition — it is one line, so do not add a newline:
#
#        systemd.run=/boot/firmware/rsc-firstboot.sh systemd.run_success_action=reboot
#
# 4. Optionally drop a plain-text file named `rsc-firstboot.conf` next to this
#    one to override defaults:
#
#        RSC_BRANCH=dev
#        RSC_FLAGS=--unattended --no-syncthing
#
# 5. Boot the Pi and wait. Provisioning takes 15-30 minutes on a Pi 4, mostly
#    apt and the model pull. Watch it with:
#
#        ssh user@hostname.local
#        journalctl -u rsc-firstboot -f
#
# ─────────────────────────────────────────────────────────────────────────────
# WHY IT WORKS THIS WAY
#
# systemd.run executes during very early boot: the network is not up, the
# user's home directory may not exist yet, and stdout goes nowhere you can
# read afterwards. Doing thirty minutes of apt work there is a recipe for a
# silent brick.
#
# So this script does almost nothing. It writes a systemd oneshot unit ordered
# After=network-online.target, enables it, and exits. The reboot triggered by
# systemd.run_success_action then starts the real provisioning with a working
# network, a real user account, and full journal logging. The unit disables
# itself on success, so it never runs twice.
# ─────────────────────────────────────────────────────────────────────────────

set -eu

BOOT_DIR=/boot/firmware
[ -d "$BOOT_DIR" ] || BOOT_DIR=/boot
MARKER=/var/lib/rsc-firstboot.done
CONF="${BOOT_DIR}/rsc-firstboot.conf"

# Defaults, overridable from rsc-firstboot.conf on the boot partition.
RSC_URL="https://rsc.ee/install.sh"
RSC_BRANCH="main"
RSC_FLAGS="--unattended"

# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"

# Idempotence: never provision an already-provisioned card.
[ -f "$MARKER" ] && exit 0

# ---- Write the deferred provisioning unit ----------------------------------
cat > /etc/systemd/system/rsc-firstboot.service <<EOF
[Unit]
Description=Robot Study Companion first-boot provisioning
After=network-online.target
Wants=network-online.target
ConditionPathExists=!${MARKER}

[Service]
Type=oneshot
RemainAfterExit=yes
# Generous: apt full-upgrade plus a model pull over SD is genuinely slow.
TimeoutStartSec=3600
ExecStart=/usr/bin/env bash /usr/local/sbin/rsc-firstboot-run.sh
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# ---- Write the worker it invokes -------------------------------------------
cat > /usr/local/sbin/rsc-firstboot-run.sh <<EOF
#!/usr/bin/env bash
set -uo pipefail

MARKER="${MARKER}"
RSC_URL="${RSC_URL}"
RSC_BRANCH="${RSC_BRANCH}"
RSC_FLAGS="${RSC_FLAGS}"
EOF

cat >> /usr/local/sbin/rsc-firstboot-run.sh <<'EOF'

log() { echo "[rsc-firstboot] $*"; }

# Identify the human account Imager created: the first non-system user with a
# real home. We cannot know its name at cmdline.txt time, so resolve it now.
TARGET_USER=$(getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 && $6 ~ /^\/home\// {print $1; exit}')
if [ -z "$TARGET_USER" ]; then
    log "FATAL: no regular user account found. Provisioning aborted."
    exit 1
fi
log "Provisioning as ${TARGET_USER}"

# Wait for genuine connectivity, not merely a configured interface.
for i in $(seq 1 60); do
    curl -fsS --max-time 5 https://github.com/ >/dev/null 2>&1 && break
    [ "$i" -eq 60 ] && { log "FATAL: no network after 5 minutes."; exit 1; }
    sleep 5
done
log "Network up"

# Ensure the account can sudo without a password for the duration, because the
# provisioner sudos freely and nobody is here to type anything.
SUDOERS=/etc/sudoers.d/010-rsc-firstboot
echo "${TARGET_USER} ALL=(ALL) NOPASSWD: ALL" > "$SUDOERS"
chmod 0440 "$SUDOERS"

log "Fetching ${RSC_URL} (branch ${RSC_BRANCH})"
RC=0
sudo -u "$TARGET_USER" -H bash -c \
    "curl -fsSL '${RSC_URL}' | RSC_BRANCH='${RSC_BRANCH}' bash -s -- ${RSC_FLAGS}" \
    || RC=$?

# Remove the temporary sudo grant whatever happened.
rm -f "$SUDOERS"

if [ "$RC" -eq 0 ]; then
    log "Provisioning succeeded"
    touch "$MARKER"
    systemctl disable rsc-firstboot.service
else
    log "Provisioning exited ${RC} — leaving the unit enabled so it retries next boot."
    log "Inspect with: journalctl -u rsc-firstboot"
fi
exit "$RC"
EOF

chmod 0755 /usr/local/sbin/rsc-firstboot-run.sh
systemctl enable rsc-firstboot.service

# Strip our own systemd.run arguments from cmdline.txt so this script never
# runs again on subsequent boots.
CMDLINE="${BOOT_DIR}/cmdline.txt"
if [ -f "$CMDLINE" ]; then
    sed -i 's| systemd.run=[^ ]*||g; s| systemd.run_success_action=[^ ]*||g' "$CMDLINE"
fi

exit 0
