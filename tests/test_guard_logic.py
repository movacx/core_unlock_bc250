#!/usr/bin/env python3
"""Offline tests for the anti-reboot-loop guard. No hardware is touched.

These exercise decide_action(), the pure heart of the boot handler. A wrong
decision here risks an infinite reboot loop on a real BC-250, so every branch
is pinned.

Run:  python3 -I tests/test_guard_logic.py
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import asdict
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE.parent / "bc250-core-unlock"


def _load_module():
    # The CLI has no .py suffix, so name the source loader explicitly.
    loader = SourceFileLoader("bc250_core_unlock", str(MODULE_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves cls.__module__ via sys.modules.
    sys.modules[loader.name] = mod
    loader.exec_module(mod)
    return mod


m = _load_module()
S = m.State
BID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "ok  " if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


print("decide_action: steady state")
act, nxt, _ = m.decide_action(0xFF, 16, S(), BID)
check("0xFF + 16 threads -> already-unlocked", act == m.ACT_UNLOCKED)
check("  resets attempts to 0", nxt.attempts == 0)
check("  status is unlocked", nxt.status == "unlocked")

# A unified mask with the low byte 0xFF (e.g. 0x0000_00FF) and 16 threads.
act, _, _ = m.decide_action(0x000000FF, 16, S(attempts=1), BID)
check("high bits ignored, low byte 0xFF -> already-unlocked", act == m.ACT_UNLOCKED)

print("decide_action: fresh cold boot")
act, nxt, _ = m.decide_action(0x77, 12, S(), BID)
check("0x77 + 12 threads -> write-and-reboot", act == m.ACT_WRITE)
check("  increments attempts to 1", nxt.attempts == 1)
check("  records boot id", nxt.last_attempt_boot_id == BID)
check("  status pending-reboot", nxt.status == "pending-reboot")

print("decide_action: mask set but not yet enumerated")
act, nxt, _ = m.decide_action(0xFF, 12, S(attempts=1, status="pending-reboot"), BID)
check("0xFF + 12 threads, 1 prior attempt -> reboot-pending", act == m.ACT_WAIT_REBOOT)
check("  increments attempts to 2", nxt.attempts == 2)

print("decide_action: anti-loop guard (the critical one)")
# After MAX_ATTEMPTS writes with no success, must GIVE UP, never write again.
act, nxt, _ = m.decide_action(0x77, 12, S(attempts=m.MAX_ATTEMPTS), BID)
check("0x77 at attempt budget -> give-up (no more writes)", act == m.ACT_GIVE_UP)
check("  status gave-up", nxt.status == "gave-up")
act2, _, _ = m.decide_action(0xFF, 12, S(attempts=m.MAX_ATTEMPTS), BID)
check("0xFF+12 at attempt budget -> give-up too", act2 == m.ACT_GIVE_UP)
# One below budget still allowed to try.
act3, _, _ = m.decide_action(0x77, 12, S(attempts=m.MAX_ATTEMPTS - 1), BID)
check("0x77 one below budget -> still writes", act3 == m.ACT_WRITE)

print("decide_action: success after attempts resets the budget")
act, nxt, _ = m.decide_action(0xFF, 16, S(attempts=m.MAX_ATTEMPTS), BID)
check("success overrides a spent budget -> already-unlocked", act == m.ACT_UNLOCKED)
check("  attempts reset to 0 so a later cold boot gets a fresh budget", nxt.attempts == 0)

print("decide_action: non-standard mask")
act, nxt, _ = m.decide_action(0xB7, 12, S(), BID)
check("0xB7 without force -> refuse-mask", act == m.ACT_REFUSE)
check("  status refused", nxt.status == "refused")
act, _, _ = m.decide_action(0xB7, 12, S(), BID, force=True)
check("0xB7 with force -> write-and-reboot", act == m.ACT_WRITE)
# Forced path still respects the loop budget.
act, _, _ = m.decide_action(0xB7, 12, S(attempts=m.MAX_ATTEMPTS), BID, force=True)
check("0xB7 forced but at budget -> give-up", act == m.ACT_GIVE_UP)

print("present-thread counting (robust to nosmt)")
check("range '0-15' counts 16", m._count_cpu_range("0-15") == 16)
check("range '0-11' counts 12", m._count_cpu_range("0-11") == 12)
check("list '0-7,8-15' counts 16", m._count_cpu_range("0-7,8-15") == 16)
# A successful unlock under nosmt shows present=16 even if online is lower.
act, _, _ = m.decide_action(0xFF, 16, S(), BID)
check("0xFF + present 16 -> unlocked even if online<16 (nosmt)", act == m.ACT_UNLOCKED)

print("cmdline CPU limiter detection (blocks install)")
check("maxcpus= detected", m._find_cpu_limiter("ro quiet maxcpus=8") == "maxcpus=8")
check("nr_cpus= detected", m._find_cpu_limiter("nr_cpus=4 ro") == "nr_cpus=4")
check("no limiter -> ''", m._find_cpu_limiter("ro quiet splash") == "")

print("State round-trips through JSON cleanly")
s = S(attempts=2, status="pending-reboot", note="x", last_attempt_boot_id=BID)
restored = S(**{k: v for k, v in asdict(s).items()})
check("State survives asdict/round-trip", restored == s)

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all guard-logic tests passed")
