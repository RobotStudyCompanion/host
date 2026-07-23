"""
tests/uart_cyd.py — UART link to CYD front panel.

Probes the CYD dispatch table by sending a known set of commands and
parsing responses. Also listens passively for host_* messages emitted
by on-screen widget interactions.

Requires: pip install pyserial
"""

import time
import threading

from config import UART as UARTCfg


# ── Check ─────────────────────────────────────────────────────────────────────

def check():
    """Open the UART port and send 'version' — confirm a response arrives."""
    try:
        import serial
    except ImportError:
        return False, "pyserial not installed — pip install pyserial"

    try:
        with serial.Serial(
            UARTCfg.PORT,
            UARTCfg.BAUD,
            timeout=UARTCfg.TIMEOUT_S,
        ) as ser:
            ser.write(b"version\n")
            response = ser.readline().decode(errors="replace").strip()
            if response:
                return True, f"CYD responded: {response!r}"
            return False, f"no response on {UARTCfg.PORT} — is CYD connected and booted?"
    except Exception as e:
        return False, str(e)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _send(ser, cmd):
    """Send a command and return the first non-empty response line."""
    ser.write(f"{cmd}\n".encode())
    deadline = time.time() + UARTCfg.TIMEOUT_S
    while time.time() < deadline:
        line = ser.readline().decode(errors="replace").strip()
        if line:
            return line
    return None


def _listen_for_host_messages(ser, duration_s=10):
    """
    Listen passively for host_* messages for duration_s seconds.
    Prints anything received. Intended to be called while the user
    interacts with the CYD touch UI.
    """
    print(f"listening for host_* messages for {duration_s} s "
          f"— interact with the CYD touch UI now...")
    deadline = time.time() + duration_s
    seen = []
    while time.time() < deadline:
        line = ser.readline().decode(errors="replace").strip()
        if line:
            tag = "host_*" if line.startswith("host_") else "cyd  "
            print(f"  [{tag}] {line}")
            seen.append(line)
    return seen


# ── Run ───────────────────────────────────────────────────────────────────────

def run():
    try:
        import serial
    except ImportError:
        print("pyserial not installed — pip install pyserial")
        return

    print(f"opening {UARTCfg.PORT} at {UARTCfg.BAUD} baud...")

    try:
        ser = serial.Serial(UARTCfg.PORT, UARTCfg.BAUD, timeout=UARTCfg.TIMEOUT_S)
    except Exception as e:
        print(f"failed to open port: {e}")
        return

    with ser:
        # 1. Probe commands
        print("\n── dispatch table probe ──")
        for cmd in UARTCfg.PROBE_CMDS:
            response = _send(ser, cmd)
            if response:
                print(f"  {cmd:<12} → {response}")
            else:
                print(f"  {cmd:<12} → (no response)")

        # 2. Send a test mood command and confirm no error
        print("\n── mood command test ──")
        response = _send(ser, "mood:HAPPY")
        print(f"  mood:HAPPY   → {response!r}")

        # 3. Passive host_* listener
        print()
        try:
            _listen_for_host_messages(ser, duration_s=10)
        except KeyboardInterrupt:
            pass

    print("\ndone.")
