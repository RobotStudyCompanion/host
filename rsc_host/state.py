"""Durable state the daemon owns outright.

Settings a user changes from the console have to survive a reboot, and the
console is the only surface some users have — no shell, no editor, no way to
run a command. So persistence cannot depend on writing a file somebody else
owns: ``/var/lib/alsa/asound.state`` is root's, and granting the daemon a root
write for one button would weaken the posture that keeps the rest of it
unprivileged.

The answer is a directory the daemon owns from the start. ``StateDirectory=``
in the systemd unit creates ``/var/lib/rsc-host`` owned by the service user
before the process launches, and exports its path as ``STATE_DIRECTORY``.

Layering, which is also how a shipped default coexists with a user's changes:

1. Values baked into the code ship in the image and are known-good on first
   boot.
2. Files here hold what the user changed, and win over the defaults.
3. Environment variables override both, for a developer who wants them to.

A fresh robot has no files here, so layer 1 runs alone and works untouched.
The user stores something and layer 2 appears. Resetting deletes the file and
returns them to the shipped default. Every step is reachable from the console.

Everything here fails soft. A daemon that cannot write its state directory
still runs — it just cannot remember, and it says so rather than pretending.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

#: Where state goes when systemd has not said otherwise.
DEFAULT_DIRECTORY = "/var/lib/rsc-host"


def _candidate_directories() -> list[Path]:
    """Where to look, best first.

    ``RSC_HOST_STATE_DIR`` is the explicit override. ``STATE_DIRECTORY`` is set
    by systemd from ``StateDirectory=`` and is the normal production answer.
    The home-relative path is for running the daemon by hand on a laptop, where
    ``/var/lib`` is neither writable nor appropriate.
    """
    out: list[Path] = []
    for var in ("RSC_HOST_STATE_DIR", "STATE_DIRECTORY"):
        value = os.environ.get(var, "").strip()
        if value:
            # systemd passes a colon-separated list when several are declared.
            out.append(Path(value.split(":")[0]))
    out.append(Path(DEFAULT_DIRECTORY))
    out.append(Path.home() / ".local" / "state" / "rsc-host")
    return out


class StateStore:
    """Small JSON store in a directory the daemon owns.

    Keys are flat names without slashes; each becomes ``<name>.json``. Writes
    are atomic — a temporary file in the same directory, then ``os.replace`` —
    so a power cut during a write leaves the previous contents intact rather
    than a truncated file. On this robot the power cut is a realistic event:
    pulling the plug is how most people turn it off.
    """

    def __init__(self, directory: str | os.PathLike[str] | None = None) -> None:
        self._dir: Path | None = None
        self._reason: str | None = None

        candidates = [Path(directory)] if directory else _candidate_directories()
        for candidate in candidates:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                probe = candidate / ".writable"
                probe.write_text("")
                probe.unlink()
            except OSError as exc:
                self._reason = f"{candidate}: {exc}"
                continue
            self._dir = candidate
            self._reason = None
            break

        if self._dir is None:
            log.warning(
                "no writable state directory (%s); settings will not survive a "
                "restart. Add StateDirectory=rsc-host to the systemd unit.",
                self._reason,
            )
        else:
            log.info("state directory: %s", self._dir)

    @property
    def available(self) -> bool:
        """False when nothing here can be written. Callers should degrade, not
        raise — losing persistence is not worth refusing to start over."""
        return self._dir is not None

    @property
    def directory(self) -> Path | None:
        return self._dir

    @property
    def reason(self) -> str | None:
        """Why the store is unavailable, for reporting to the user."""
        return self._reason

    def path(self, name: str) -> Path | None:
        if self._dir is None:
            return None
        if "/" in name or "\\" in name or name.startswith("."):
            raise ValueError(f"bad state key: {name!r}")
        return self._dir / f"{name}.json"

    def read(self, name: str, default: dict | None = None) -> dict:
        """Return the stored object, or ``default`` if absent or unreadable.

        Corruption is treated as absence and logged. A user who cannot open a
        shell cannot repair a bad file, so refusing to start would strand them
        with a robot that boots to nothing; falling back to the shipped default
        at least leaves a working machine.
        """
        fallback = {} if default is None else dict(default)
        target = self.path(name)
        if target is None or not target.exists():
            return fallback
        try:
            loaded = json.loads(target.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("state %s unreadable (%s); using defaults", target, exc)
            return fallback
        if not isinstance(loaded, dict):
            log.warning("state %s is not an object; using defaults", target)
            return fallback
        return loaded

    def write(self, name: str, data: dict) -> Path | None:
        """Persist ``data``. Returns the path, or None when unavailable."""
        target = self.path(name)
        if target is None:
            return None
        payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        return target

    def delete(self, name: str) -> bool:
        """Remove the stored object. True if a file went away."""
        target = self.path(name)
        if target is None or not target.exists():
            return False
        target.unlink()
        return True

    def status(self) -> dict:
        """Snapshot for the ``peripherals.status`` verb and the console."""
        return {
            "available": self.available,
            "directory": str(self._dir) if self._dir else None,
            "reason": self._reason,
            "keys": sorted(p.stem for p in self._dir.glob("*.json"))
            if self._dir
            else [],
        }