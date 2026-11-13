#!/usr/bin/env python3
"""Offline tests for the whole `apply` flow against a simulated SMU.

No hardware is touched: SmnDevice is replaced by a fake that models the core
mask, and reboot/disable/systemd are replaced by recorders. These pin the
ordering and side-effect rules around the one hardware write:

  * --dry-run changes nothing at all;
  * the attempt is saved before the SMU is written;
  * a run by hand never burns the unattended reboot budget;
  * an unattended run that exhausts the budget disables itself and never writes.

Run:  python3 -I tests/test_apply_flow.py
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent.parent / "bc250-core-unlock"
loader = SourceFileLoader("bc250_core_unlock_flow", str(MODULE_PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
m = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = m
loader.exec_module(m)

RealSmnDevice = m.SmnDevice  # kept before run() swaps in the fake
failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


class World:
    """One simulated boot: a mask, a thread count, and what the tool did."""

    def __init__(self, mask: int, threads: int, *, write_status: int = 0x01,
                 write_sticks: bool = True, governors: tuple[str, ...] = ()):
        self.mask, self.threads = mask, threads
        self.write_status, self.write_sticks = write_status, write_sticks
        self.governors = governors
        self.writes = 0
        self.reboots = 0
        self.disabled = 0
        self.events: list[str] = []
        self.opened = 0
        self.closed = 0


def run(world: World, state_dir: Path, **flags) -> int:
    args = argparse.Namespace(
        boot=False, force_mask=False, ignore_governor=False, dry_run=False
    )
    for key, value in flags.items():
        setattr(args, key, value)

    class FakeDevice:
        def __init__(self, *a, **k):
            world.opened += 1

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            world.closed += 1
            world.events.append("device-closed")

        def read_mask(self):
            return world.mask

        def write_mask_unlocked(self):
            world.writes += 1
            saved = json.loads((state_dir / "state.json").read_text())
            world.events.append(f"smu-write(saved_attempts={saved['attempts']})")
            if world.write_status == 0x01 and world.write_sticks:
                world.mask = 0xFF
            return world.write_status

    def fake_reboot():
        world.reboots += 1
        world.events.append("reboot")

    def fake_disable():
        world.disabled += 1

    m.STATE_DIR = state_dir
    m.STATE_FILE = state_dir / "state.json"
    m.INSTALL_BIN = state_dir / "bin" / "bc250-core-unlock"  # absent: not the installed copy
    m.SmnDevice = FakeDevice
    m.require_root = lambda: None
    m.is_bc250 = lambda: True
    m.cmdline_disabled = lambda: False
    m.governors_active = lambda: world.governors
    m.threads_present = lambda: world.threads
    m.boot_id = lambda: "test-boot"
    m.reboot_now = fake_reboot
    m.disable_self = fake_disable
    m.time.sleep = lambda _s: None
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return m.cmd_apply(args)


def saved(state_dir: Path) -> dict:
    try:
        return json.loads((state_dir / "state.json").read_text())
    except FileNotFoundError:
        return {}


with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)

    print("cold boot, unattended: write once, save first, then reboot")
    d = base / "a"; d.mkdir()
    w = World(0x77, 12)
    rc = run(w, d, boot=True)
    check("exit 0", rc == 0)
    check("exactly one SMU write", w.writes == 1)
    check("attempt saved BEFORE the SMU write", w.events[0] == "smu-write(saved_attempts=1)")
    check("device closed before reboot", w.events == ["smu-write(saved_attempts=1)", "device-closed", "reboot"])
    check("state: attempts=1, pending-reboot", saved(d).get("attempts") == 1 and saved(d).get("status") == "pending-reboot")

    print("second boot, cores up: nothing to do, budget reset")
    w2 = World(0xFF, 16)
    rc = run(w2, d, boot=True)
    check("exit 0, no write, no reboot", rc == 0 and w2.writes == 0 and w2.reboots == 0)
    check("state: attempts=0, unlocked", saved(d).get("attempts") == 0 and saved(d).get("status") == "unlocked")

    print("board that never keeps the mask: two reboots, then stop for good")
    d = base / "b"; d.mkdir()
    total_writes = total_reboots = 0
    last = None
    for _boot in range(6):  # six cold-looking boots in a row
        last = World(0x77, 12)
        rc = run(last, d, boot=True)
        total_writes += last.writes
        total_reboots += last.reboots
    check("at most 2 writes over 6 boots", total_writes == 2)
    check("at most 2 reboots over 6 boots", total_reboots == 2)
    check("service disabled itself", last.disabled == 1 and saved(d).get("status") == "gave-up")
    check("give-up exits non-zero", rc == 1)

    print("--dry-run: no state, no write, no reboot, no disable")
    d = base / "c"; d.mkdir()
    w = World(0x77, 12)
    rc = run(w, d, boot=True, dry_run=True)
    check("fresh mask: nothing happened", rc == 0 and w.writes == 0 and w.reboots == 0 and saved(d) == {})
    (d / "state.json").write_text(json.dumps({"attempts": 2, "status": "pending-reboot"}))
    before = (d / "state.json").read_text()
    w = World(0x77, 12)
    rc = run(w, d, boot=True, dry_run=True)
    check("at budget: not disabled, state untouched", w.disabled == 0 and (d / "state.json").read_text() == before)
    w = World(0xFF, 12)
    (d / "state.json").write_text(json.dumps({"attempts": 1, "status": "pending-reboot"}))
    before = (d / "state.json").read_text()
    rc = run(w, d, boot=True, dry_run=True)
    check("reboot-pending: no reboot, state untouched", w.reboots == 0 and (d / "state.json").read_text() == before)

    print("run by hand: never limited by, and never burns out, the boot budget")
    d = base / "d"; d.mkdir()
    for n in range(1, 5):  # four manual unlocks on four cold boots, no service
        w = World(0x77, 12)
        rc = run(w, d)
        check(f"manual run {n}: writes and reboots", rc == 0 and w.writes == 1 and w.reboots == 1 and w.disabled == 0)
    check("manual runs leave attempts at 1, not accumulating", saved(d).get("attempts") == 1)

    print("governor active: refuse before opening the device")
    d = base / "e"; d.mkdir()
    w = World(0x77, 12, governors=("cyan-skillfish-governor-smu.service",))
    rc = run(w, d)
    check("refused, device never opened, nothing saved", rc == 1 and w.opened == 0 and w.writes == 0 and saved(d) == {})

    print("SMU refuses the message cleanly: not counted, no reboot")
    d = base / "f"; d.mkdir()
    w = World(0x77, 12, write_status=0xFE)
    rc = run(w, d, boot=True)
    check("exit 1, no reboot", rc == 1 and w.reboots == 0)
    check("attempt rolled back to 0", saved(d).get("attempts") == 0 and saved(d).get("status") == "idle")

    print("write reports OK but mask did not change: not counted, no reboot")
    d = base / "g"; d.mkdir()
    w = World(0x77, 12, write_sticks=False)
    rc = run(w, d, boot=True)
    check("exit 1, no reboot, attempts 0", rc == 1 and w.reboots == 0 and saved(d).get("attempts") == 0)

    print("non-standard mask")
    d = base / "h"; d.mkdir()
    w = World(0xB7, 12)
    rc = run(w, d, boot=True)
    check("unattended: refused, no write, service disabled", rc == 1 and w.writes == 0 and w.disabled == 1)
    w = World(0xB7, 12)
    rc = run(w, d)
    check("by hand: refused, no write, service left alone", rc == 1 and w.writes == 0 and w.disabled == 0)
    w = World(0xB7, 12)
    rc = run(w, d, boot=True, force_mask=True)
    check("--force-mask: writes and reboots", rc == 0 and w.writes == 1 and w.reboots == 1)

    print("state file cannot be saved: nothing is written to the SMU")
    d = base / "i"; d.mkdir()
    (d / "state.json").mkdir()  # a directory where the file must go: save fails
    w = World(0x77, 12)
    try:
        run(w, d, boot=True)
        raised = False
    except OSError:
        raised = True
    check("save failure aborts before any SMU write or reboot", raised and w.writes == 0 and w.reboots == 0)

    print("malformed state files fall back to defaults")
    d = base / "j"; d.mkdir()
    m.STATE_DIR, m.STATE_FILE = d, d / "state.json"
    for bad in ("not json", "[1,2,3]", '{"attempts": "many"}', '{"attempts": true}', '{"attempts": -5}'):
        (d / "state.json").write_text(bad)
        check(f"load({bad!r}) -> attempts 0", m.State.load().attempts == 0)
    (d / "state.json").write_text('{"attempts": 2, "status": "gave-up", "updated": 5}')
    s = m.State.load()
    check("valid file is honoured (int 'updated' accepted)", s.attempts == 2 and s.status == "gave-up" and s.updated == 5.0)

    print("window locking, against a plain file standing in for PCI config space")
    import struct

    d = base / "k"; d.mkdir()
    window = d / "config"
    window.write_bytes(bytes(256))
    m.SMU_RUN_LOCK = str(d / "smu-run.lock")
    other_clients_register = 0x03B10A48
    with open(window, "r+b") as raw:  # another client has selected a register
        raw.seek(m.SMN_INDEX)
        raw.write(struct.pack("<I", other_clients_register))

    def index_now() -> int:
        return struct.unpack("<I", window.read_bytes()[m.SMN_INDEX:m.SMN_INDEX + 4])[0]

    with RealSmnDevice(str(window), exclusive_run=False) as dev:
        dev.read_mask()
        check("while open, the index points at the core mask", index_now() == m.MASK_REG)
        try:
            RealSmnDevice(str(window), timeout=0.1)
            second = "opened"
        except m.SmuBusy:
            second = "busy"
        check("a second client is told the SMU is busy", second == "busy")
    check("on close, the other client's register is selected again", index_now() == other_clients_register)
    with RealSmnDevice(str(window), timeout=0.1):
        check("after close the locks are free again", True)

    held = RealSmnDevice(str(window), exclusive_run=True)
    held_run_fd = held.run_fd
    import fcntl as _fcntl, os as _os
    probe = _os.open(m.SMU_RUN_LOCK, _os.O_RDWR)
    try:
        _fcntl.flock(probe, _fcntl.LOCK_SH | _fcntl.LOCK_NB)
        shared_ok = True
    except OSError:
        shared_ok = False
    check("an unlock run holds the SMU run lock exclusively (samplers stand aside)",
          held_run_fd >= 0 and not shared_ok)
    held.close()
    try:
        _fcntl.flock(probe, _fcntl.LOCK_SH | _fcntl.LOCK_NB)
        shared_ok = True
    except OSError:
        shared_ok = False
    _os.close(probe)
    check("and releases it on close", shared_ok)

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all apply-flow tests passed")
