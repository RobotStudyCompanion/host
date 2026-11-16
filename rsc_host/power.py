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
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

_BUS = "org.freedesktop.login1"
_PATH = "/org/freedesktop/login1"
_IFACE = "org.freedesktop.login1.Manager"

#: Seconds between answering the caller and the machine going down. The reply
#: has to reach the console before the network does, or the person pressing the
#: button sees a dropped connection and no confirmation — indistinguishable
#: from a crash.
DEFAULT_DELAY = 2.0

#: Called with a reason when logind refuses a request the caller was already
#: told had been scheduled.
OnFailure = Callable[[str], Awaitable[None]]


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


async def _invoke(
    method: str, delay: float, on_failure: OnFailure | None = None
) -> None:
    await asyncio.sleep(delay)
    try:
        code, out = await _busctl("call", _BUS, _PATH, _IFACE, method, "b", "false")
    except Exception as exc:
        code, out = 1, f"{type(exc).__name__}: {exc}"
    if code == 0:
        log.info("%s accepted by logind", method)
        return
    # The caller was answered seconds ago and the robot is staying up, with the
    # hardware already made safe for a shutdown that isn't coming.
    log.error("%s refused by logind: %s", method, out)
    if on_failure is not None:
        try:
            await on_failure(f"logind refused {method}: {out}")
        except Exception:
            log.exception("handling the failed %s failed too", method)


_pending: set[asyncio.Task[None]] = set()


async def request(
    action: str, delay: float = DEFAULT_DELAY, on_failure: OnFailure | None = None
) -> dict:
    """Ask logind to power off or reboot, after ``delay`` seconds.

    Returns as soon as the request is scheduled, so the caller gets an answer
    while the network is still up. The actual call happens in a background
    task; if logind refuses it then, ``on_failure`` is awaited with a reason.
    """
    if action not in ("poweroff", "reboot"):
        raise ValueError(f"unknown power action: {action!r}")

    state = await status()
    if not state["allowed"]:
        return {"scheduled": False, **state}

    method = "PowerOff" if action == "poweroff" else "Reboot"
    task = asyncio.create_task(
        _invoke(method, delay, on_failure), name=f"power-{action}"
    )
    # Held until done: the event loop keeps only weak references to tasks.
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    log.warning("%s requested; going down in %.1f s", action, delay)
    return {"scheduled": True, "action": action, "delay_s": delay}

# ---- Backends ----
#
# setup() picks one by backend, as it does for the hardware. A laptop running
# the fake backend must never ask logind to power off the laptop: with a
# permissive polkit setup it would.


class LogindPower:
    """The real thing: asks logind through ``busctl``."""

    async def status(self) -> dict:
        return await status()

    async def request(
        self,
        action: str,
        delay: float = DEFAULT_DELAY,
        on_failure: OnFailure | None = None,
    ) -> dict:
        return await request(action, delay, on_failure)


class FakePower:
    """Always allowed, never acts. Logs what it would have done.

    Gives the same replies as :class:`LogindPower` when logind says yes, so a
    console can run the whole shutdown flow against a laptop.
    """

    async def status(self) -> dict:
        return {"can_power_off": "yes", "can_reboot": "yes", "allowed": True}

    async def request(
        self,
        action: str,
        delay: float = DEFAULT_DELAY,
        on_failure: OnFailure | None = None,
    ) -> dict:
        """``on_failure`` is accepted and never called: the fake never fails."""
        if action not in ("poweroff", "reboot"):
            raise ValueError(f"unknown power action: {action!r}")
        log.warning(
            "fake backend: would %s in %.1f s",
            "power off" if action == "poweroff" else "reboot",
            delay,
        )
        return {"scheduled": True, "action": action, "delay_s": delay}
