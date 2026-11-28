# Schuster Lab QICK releases

Versions are `<upstream>.<lab>`: `0.2.450.1` is the first lab release on
upstream 0.2.450. `<upstream>.0` means upstream plus lab changes not yet
released. Install a release with
`pip install git+https://github.com/SchusterLab/qick@slab-<version>`.
The full list of differences from upstream is `slab/CHANGES.md` in the
repository. The firmware builds that go with each release are listed in
`lab_firmware.json` next to this file (`qick.lab_release.firmware()`).

## 0.2.450.1 (2026-11-28)

Base: upstream 0.2.450. Same library code as the earlier tag `slab-0.2.450-1`.

Library:
- tProc v2 phase-reset grid: on firmware built with `TIME_SYNC_PERIOD` above
  1, the compiler refuses phase resets that are off the period and offers
  helpers to align them. Off on firmware without the option.
- tProc v2 queue flags: on firmware with `FIFO_GUARD`, every round checks the
  sticky lost-push flags and raises `RuntimeError` if a trigger, data or wave
  queue lost an entry (`soc.check_tproc_queues()`). Off on firmware without
  the option.
- `prepare_round` resets the tProc before arming the readout buffers, so a
  trigger left high by a stopped program is not captured as a spurious first
  measurement. Applies on all firmware.
- Assembler accepts tProc revisions 28 and 29.
- Lab version number (`qick.__version__`), these notes, and the firmware
  manifest (`qick.lab_release`).

Not included: the QICK box daughter-card driver `rfbox` (branch
`lab/rf-driver`, tags `slab-rfbox-*`).

Firmware (ZCU216, stock tProc v2 standard design with lab changes; none has
RF-board control, so none fits an RF-board box):
- `slab_tprocv2_216_2026-11-25_overflow-fix`: weighted readout buffers,
  `FIFO_GUARD` 8 with lost-push flags, `TIME_SYNC_PERIOD` 28. Ran on a lab
  board.
- `slab_tprocv2_216_2026-11-26_weighted-large`: as above with 16384 program
  words and 16384-sample buffers and weights on readouts 0 and 1. Ran on a lab
  board.
- `slab_tprocv2_216_2026-11-27_plain-large`: stock averaging buffers (no
  weights), 16384 program words, 32768-sample buffers on readouts 0 and 1.
  Not run on a board.

Known on hardware, not changed by this release: on an RF-board ZCU216 the
full-speed generator's output has a floor of about 0.0023 of full scale at
small gains (under investigation); keep gains near 0.1 or above and set
power with the card attenuators.
