# Provenance / Procedencia

This tool does not invent the unlock. It automates, with a safety guard, the
primitive discovered and documented by **rw-r-r-0644**.

## Upstream

- **Discovery + raw primitive:** https://github.com/rw-r-r-0644/bc250-core-unlock (MIT)
  - Reviewed script: `bc250-unlock-cores.py`
  - Reviewed script SHA-256: `b52bdcc14012c9dd222ebc71b4df11c3e976824a129f01f026bcc38c5482ccc5`
  - Reviewed revision (as pinned by BC-250 Control Center): `fb7e5bceab0e38369006cd1a0e53b768517f6030`
  - This SHA-256 identifies the exact script this tool was **reimplemented
    from** (not run). BC-250 Control Center ships the same payload hash in its
    privileged helper (`bc250-core-unlock-helper`); it is recorded here for
    traceability, not verified at runtime, because this tool performs the write
    itself rather than executing the upstream file.
- **EFI boot shim (alternative permanence path, NOT used here):**
  https://github.com/Hexxeh/bc250-efi-core-unlock (MIT)
- **BIOS DXE mod (the firmware-flash permanence path, NOT used here):**
  https://github.com/RescueMei/BC250-DXEv3-BIOSMOD
- **ACPI table fixes for 8 cores:** https://github.com/e-tho/bc250-acpi-fix

## Register map replicated from the reviewed script

| Name            | Value        | Meaning                                              |
|-----------------|--------------|------------------------------------------------------|
| PCI config      | `0000:00:00.0` | SMN window host device                             |
| SMN index port  | `0xB8`       | SMN address is written here                          |
| SMN data port   | `0xBC`       | SMN data read / written here                         |
| `MASK_REG`      | `0x5A870`    | core presence mask (low byte: `0x77` stock, `0xFF` unlocked) |
| `MSG_WRITE_FF`  | `0x98`       | ungated SMU message that writes `0x00FF` to an SMN addr |
| `Q3_CMD/RSP/ARG`| `0x03B10A20 / 0x03B10A80 / 0x03B10A88` | queue-3 mailbox            |
| `DONE`          | `{0x01,0xFF,0xFE,0xFD,0xFC}` | mailbox completion statuses           |

The SMN mailbox read/write and the `send()` protocol in `bc250-core-unlock`
(class `SmnDevice`) are behaviour-equivalent to the reviewed script. The reviewed
script addresses the mask as `0x5A870`; Hexxeh's EFI shim addresses the same
register as `0x0115A870` (the difference is a bus prefix). This tool uses
`0x5A870`, matching the reviewed Linux payload.

## What this tool adds over the raw script

1. A **boot-time service** (systemd oneshot, or OpenRC) that re-applies the
   write on every cold boot, so the unlock is effectively permanent **without a
   BIOS flash and without any EFI / boot-order change**.
2. An **anti-reboot-loop guard** (`decide_action`): at most `MAX_ATTEMPTS` (2)
   automatic write+reboot cycles, then it disables itself. Each attempt is
   saved and fsynced before the SMU is written. The raw script, run from a boot
   service as is, would loop if a board's reboot did not preserve the mask.
3. Ordering **before** the GPU frequency governors (`Before=`) instead of
   disabling them, so steady-state boots are unaffected.
4. A kernel-command-line escape hatch (`bc250.core_unlock=0`) and a clean
   `uninstall`.

## What this tool deliberately does NOT do

- It does **not** flash or modify the BIOS/UEFI firmware.
- It does **not** install an EFI binary, touch the ESP, or change the UEFI boot
  order (that is what the Hexxeh shim does; it carries a real brick risk and is
  why BC-250 Control Center only *detects* it).
- It does **not** apply Hexxeh's SMU telemetry/SRAM patches. Those address the
  post-unlock GPU-clock-reporting bug but require the complex SMU "secure
  unlock" and are out of scope for a safe first release. The reporting bug is
  pre-existing and shared with the existing temporary unlock.

## The GPU clock reporting bug and the cyan governor

After the unlock, `pp_dpm_sclk` and `freq1_input` report nonsense (~18-60 MHz
instead of 500). Upstream calls this cosmetic. Whether that holds for the cyan
governor depends on its configured `gpu.set-method` (read from its source):

- `smu` (cyan's default; `SmuFreqStrategy` in `src/gpu.rs`): frequency is set
  and read through the SMU. The governor does not consume those sysfs files for
  control, so the bug stays cosmetic.
- `kernel` (`src/gpu/kernel_freq_strategy.rs`): it reads `pp_dpm_sclk` for
  `sclk_min`/`sclk_max` and clamps its target to that range. If the unlock
  corrupts that file's bounds, control is affected. Not verified on hardware.
- `gpu-usage.fix-freq` (`src/gpu_frequency_fix.rs`) overlays `freq1_input` with
  the value read from the SMU, which hides the bad reading from other programs.

A permanent unlock runs the governor on an unlocked board at every boot (the
existing temporary unlock leaves the governor disabled after a manual unlock),
so a board using `set-method = "kernel"` should be checked before relying on it.

## SMU coexistence

Every SMU client on the board shares one non-atomic window (PCI config 0xB8
index, 0xBC data). This tool follows the convention BC-250 Control Center's
helpers and the cyan governor already use:

- `/run/bc250-smu-run.lock`: exclusive for the unlock run, shared for a single
  mask read.
- an exclusive `flock` on `/sys/bus/pci/devices/0000:00:00.0/config` for as long
  as the window is in use, and the index register restored on release (cyan and
  the CPU tuning tool lock each 32-bit access, so one may have selected a
  register and be waiting to touch its data).
- writes are additionally refused while a governor unit is anything but stopped,
  as upstream requires; at boot the unit is ordered before them.
