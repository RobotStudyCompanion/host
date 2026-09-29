"""Shutdown and reboot, without giving the daemon root.

systemd already owns this. ``logind`` exposes ``PowerOff`` and ``Reboot`` on
D-Bus and decides who may call them through polkit, so the daemon asks rather
than acts. No sudo, no shell-out to ``shutdown``, no setuid anything.

Out of the box the answer is ``challenge``: polkit would demand interactive
authentication, which a service user with no session cannot supply. A rule
grants it:

    // /etc/polkit-1/rules.d/50-rsc-power.rules
    polkit.addRule(function(action, subject) {
        if ((action.id == "org.freedesktop.login1.power-off" ||
             action.id == "org.freedesktop.login1.reboot") &&
            subject.user == "rsc") {
            return polkit.Result.YES;
        }
    });

That names two actions for one user. A sudoers entry would instead whitelist a
command line, which is a coarser thing to hand out and easier to get subtly
wrong.

``busctl`` is used rather than a D-Bus binding because it ships with systemd,
so there is no dependency to install on a robot that has to work from a flashed
image.
"""
from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger(__name__)

_BUS = "org.freedesktop.login1"
_PATH = "/org/freedesktop/login1"
_IFACE = "org.freedesktop.login1.Manager"

#: Seconds between answering the caller and the machine going down. The reply
#: has to reach the console before the network does, or the person pressing the
#: button sees a dropped connection and no confirmation — indistinguishable
#: from a crash.
DEFAULT_DELAY = 2.0


async def _busctl(*args: str, timeout: float = 5.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "busctl", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "busctl timed out"
    return proc.returncode, out.decode("utf-8", "replace").strip()


async def _can(action: str) -> str:
    """Return logind's verdict: yes, no, challenge, na, or an error string."""
    if shutil.which("busctl") is None:
        return "unavailable: busctl not found"
    code, out = await _busctl("call", _BUS, _PATH, _IFACE, action)
    if code != 0:
        return f"error: {out}"
    # Replies look like: s "challenge"
    return out.split('"')[1] if '"' in out else out


async def status() -> dict:
    """Whether the daemon may power the machine down, and why not if it may not."""
    can_off, can_reboot = await asyncio.gather(_can("CanPowerOff"), _can("CanReboot"))
    allowed = can_off == "yes" and can_reboot == "yes"
    result = {
        "can_power_off": can_off,
        "can_reboot": can_reboot,
        "allowed": allowed,
    }
    if not allowed:
        # Three different failures, and they want three different answers.
        # Reporting "logind refused" for a bus that is not reachable sends
        # somebody to edit polkit rules that were never the problem.
        answers = (can_off, can_reboot)
        if any(a.startswith("unavailable") for a in answers):
            result["reason"] = "busctl not found; is this a systemd host?"
        elif any(a.startswith("error") for a in answers):
            result["reason"] = f"could not reach logind ({can_off})"
        elif "challenge" in answers:
            result["reason"] = (
                "polkit requires interactive authentication, which a service "
                "user cannot supply"
            )
            result["fix"] = "install /etc/polkit-1/rules.d/50-rsc-power.rules"
        else:
            result["reason"] = f"logind refused (power-off: {can_off}, reboot: {can_reboot})"
    return result


async def _invoke(method: str, delay: float) -> None:
    await asyncio.sleep(delay)
    code, out = await _busctl("call", _BUS, _PATH, _IFACE, method, "b", "false")
    if code != 0:
        # Nothing useful to raise to: the caller was answered seconds ago and
        # the console may already be gone. The journal is the only reader left.
        log.error("%s refused by logind: %s", method, out)
    else:
        log.info("%s accepted by logind", method)


async def request(action: str, delay: float = DEFAULT_DELAY) -> dict:
    """Ask logind to power off or reboot, after ``delay`` seconds.

    Returns as soon as the request is scheduled, so the caller gets an answer
    while the network is still up. The actual call happens in a background task.
    """
    if action not in ("poweroff", "reboot"):
        raise ValueError(f"unknown power action: {action!r}")

    state = await status()
    if not state["allowed"]:
        return {"scheduled": False, **state}

    method = "PowerOff" if action == "poweroff" else "Reboot"
    asyncio.create_task(_invoke(method, delay), name=f"power-{action}")
    log.warning("%s requested; going down in %.1f s", action, delay)
    return {"scheduled": True, "action": action, "delay_s": delay}