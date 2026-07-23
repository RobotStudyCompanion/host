#!/usr/bin/env python3
"""
rsc_test.py — RSC hardware test entry point.

Usage
-----
  rsc-test                        interactive menu
  rsc-test --check                health-check all subsystems, no interaction needed
  rsc-test button <behaviour>     run a specific button/LED behaviour
  rsc-test servo  <behaviour>     run a specific servo behaviour
  rsc-test ring   <behaviour>     run a specific ring behaviour
  rsc-test respeaker <subtest>    run a ReSpeaker subtest
  rsc-test speaker                play a test tone through the base speaker
  rsc-test scd41                  read CO2 / temp / RH from SCD41
  rsc-test uart                   probe CYD over UART

Run `rsc-test <subsystem> --help` for per-subsystem options.
"""

import argparse
import sys

# ── Subsystem registry ────────────────────────────────────────────────────────
# Each entry: (module_path, run_fn, check_fn, description, subtest_choices)
# Imported lazily so a missing optional dependency in one module doesn't break
# the whole entry point.

SUBSYSTEMS = {
    "button": (
        "tests.button",
        "run",
        "check",
        "Arcade button (GPIO23) + LED PWM (GPIO24)",
        ["print", "solid", "brightness", "tap_hold"],
    ),
    "servo": (
        "tests.servos",
        "run",
        "check",
        "Left + right servo motors via pigpio",
        ["hold", "cycle", "dir"],
    ),
    "ring": (
        "tests.ring",
        "run",
        "check",
        "16× SKC6812 RGBW NeoPixel ring (GPIO12)",
        ["colour", "sweep"],
    ),
    "respeaker": (
        "tests.respeaker",
        "run",
        "check",
        "ReSpeaker 2-Mic HAT — mic record/playback, APA102 LEDs, HAT button",
        ["record", "leds", "hatbutton"],
    ),
    "speaker": (
        "tests.speaker",
        "run",
        "check",
        "Base speaker — ALSA playback test",
        [],
    ),
    "scd41": (
        "tests.scd41",
        "run",
        "check",
        "SCD41 CO2 / temperature / relative humidity over I2C",
        [],
    ),
    "uart": (
        "tests.uart_cyd",
        "run",
        "check",
        "UART link to CYD — probe dispatch table, parse host_* messages",
        [],
    ),
}


# ── Helpers ───────────────────────────────────────────────────────────────────

PASS  = "\033[32m✓\033[0m"
FAIL  = "\033[31m✗\033[0m"
WARN  = "\033[33m!\033[0m"
HEAD  = "\033[1;34m"
RESET = "\033[0m"


def _load(module_path):
    import importlib
    return importlib.import_module(module_path)


def _run_check(name, entry):
    module_path, _, check_fn, description, _ = entry
    print(f"  {HEAD}{name:<12}{RESET} {description}")
    try:
        mod = _load(module_path)
        result = getattr(mod, check_fn)()   # returns (ok: bool, detail: str)
        ok, detail = result
        icon = PASS if ok else FAIL
        print(f"             {icon}  {detail}")
        return ok
    except ImportError as e:
        print(f"             {WARN}  import error — {e}")
        return False
    except Exception as e:
        print(f"             {FAIL}  {e}")
        return False


# ── --check mode ──────────────────────────────────────────────────────────────

def cmd_check(_args):
    """Probe every subsystem and print a health summary. No hardware interaction."""
    print(f"\n{HEAD}RSC subsystem health check{RESET}\n")
    results = {}
    for name, entry in SUBSYSTEMS.items():
        results[name] = _run_check(name, entry)
    passed = sum(results.values())
    total  = len(results)
    colour = "\033[32m" if passed == total else "\033[31m"
    print(f"\n{colour}{passed}/{total} subsystems OK{RESET}\n")
    sys.exit(0 if passed == total else 1)


# ── Interactive menu ──────────────────────────────────────────────────────────

def cmd_menu(_args):
    """Present an interactive prompt to pick subsystem and subtest."""
    names = list(SUBSYSTEMS.keys())

    print(f"\n{HEAD}RSC test rig{RESET}\n")
    for i, name in enumerate(names, 1):
        _, _, _, description, _ = SUBSYSTEMS[name]
        print(f"  {i:>2}.  {name:<12}  {description}")
    print(f"   c.  {'check':<12}  Health-check all subsystems")
    print(f"   q.  quit\n")

    choice = input("Select subsystem: ").strip().lower()

    if choice in ("q", "quit"):
        sys.exit(0)

    if choice in ("c", "check"):
        cmd_check(None)
        return

    # Accept number or name
    if choice.isdigit():
        idx = int(choice) - 1
        if not (0 <= idx < len(names)):
            print("Invalid choice.")
            sys.exit(1)
        name = names[idx]
    elif choice in SUBSYSTEMS:
        name = choice
    else:
        print(f"Unknown subsystem '{choice}'.")
        sys.exit(1)

    module_path, run_fn, _, _, subtests = SUBSYSTEMS[name]

    if subtests:
        print(f"\n{HEAD}{name} subtests{RESET}\n")
        for i, s in enumerate(subtests, 1):
            print(f"  {i:>2}.  {s}")
        print()
        sub = input("Select subtest (or Enter for default): ").strip().lower()
        if sub.isdigit():
            idx = int(sub) - 1
            subtest = subtests[idx] if 0 <= idx < len(subtests) else subtests[0]
        elif sub in subtests:
            subtest = sub
        elif sub == "":
            subtest = subtests[0]
        else:
            print(f"Unknown subtest '{sub}'.")
            sys.exit(1)
    else:
        subtest = None

    _invoke(module_path, run_fn, subtest)


# ── Direct invocation ─────────────────────────────────────────────────────────

def cmd_direct(args):
    """Called when a subsystem name is given directly on the command line."""
    name = args.subsystem
    if name not in SUBSYSTEMS:
        print(f"Unknown subsystem '{name}'. Run rsc-test --help.")
        sys.exit(1)
    module_path, run_fn, _, _, _ = SUBSYSTEMS[name]
    _invoke(module_path, run_fn, getattr(args, "subtest", None))


def _invoke(module_path, run_fn, subtest):
    try:
        mod = _load(module_path)
        fn  = getattr(mod, run_fn)
        if subtest:
            fn(subtest)
        else:
            fn()
    except ImportError as e:
        print(f"Import error loading {module_path}: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nstopped.")
        sys.exit(0)


# ── Argument parser ───────────────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(
        prog="rsc-test",
        description="RSC hardware test rig.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="health-check all subsystems without interactive prompts",
    )

    sub = parser.add_subparsers(dest="subsystem", metavar="SUBSYSTEM")

    for name, (_, _, _, description, subtests) in SUBSYSTEMS.items():
        p = sub.add_parser(name, help=description)
        if subtests:
            p.add_argument(
                "subtest",
                nargs="?",
                default=subtests[0],
                choices=subtests,
                help=f"subtest to run (default: {subtests[0]})",
            )

    return parser


# ── Entry ─────────────────────────────────────────────────────────────────────

def main():
    parser = build_parser()
    args   = parser.parse_args()

    if args.check:
        cmd_check(args)
    elif args.subsystem:
        cmd_direct(args)
    else:
        cmd_menu(args)


if __name__ == "__main__":
    main()
