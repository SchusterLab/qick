# Lab changes relative to upstream

Every difference between this fork's `main` and `openquantumhardware/qick`
`main` is listed here. Check with `git diff upstream/main main --stat`.

| Change | Files | Upstream status |
|---|---|---|
| Fork documentation and sync script | `slab/`, `CLAUDE.md` | Lab only |
| tProc time reset aligned to the common period of the clocks: parameter `TIME_SYNC_PERIOD` on `qproc_ctrl`, `qick_processor`, `axis_qick_processor` and the IP packaging. Default 0 = unchanged behaviour. | `firmware/ip/qick_processor/` | Not proposed upstream yet |
| PFB readout v4 builds under Vivado 2024.1: type `BOOLEAN_VECTOR` in the generated FFT code renamed `SSR_BOOLEAN_VECTOR` (it clashes with a VHDL-2008 built-in). No functional change. | `firmware/ip/axis_pfb_readout_v4/src/pfb/fft/ssr_fft_8x64/ssr_8x64_entity_declarations.vhd` | Not proposed upstream yet |
| Lab ZCU216 firmware project: stock tProc v2 design with weighted readout buffers, a weight-loading DMA and switch, and `TIME_SYNC_PERIOD` = 28. Block design is generated from the stock one by `make_bd.py`. | `firmware/projects/slab_tprocv2_216/` | Lab only |
| Library (tProc v2): the tProc driver stores `TIME_SYNC_PERIOD` as `tprocs[0]['time_sync_period']` (0 if absent). When it is above 1 the v2 compiler refuses any pulse or readout configuration with a phase reset that is not issued on a multiple of the period, or whose time it cannot establish (class `TimeGrid`). Helpers: `time_sync_period_us()`, `round_up_to_sync_tick()`, `align=` on `delay`/`delay_auto` and on `AveragerProgramV2` (default: pad the shot only if it contains a phase reset), `reset_phases()` (by default resets every declared resettable generator and readout on one legal tick; readouts get a copy of their config with phrst set) and `phase_reset_times()`. Programs without a phase reset, and all programs on firmware without a period, compile as before. Usage: `slab/notes/phase-reset-usage.md`. Tests in `slab/tests/`. | `qick_lib/qick/asm_v2.py`, `qick_lib/qick/qick_asm.py`, `qick_lib/qick/drivers/tproc.py`, `slab/tests/` | Lab only (depends on the firmware option) |
| tProc v2 dispatcher queues: the core pauses while any trigger, data or wave queue has fewer than `FIFO_GUARD` (default 8) free entries, from a registered almost-full flag (upstream pauses on the combinational full flag of all queues). Sticky lost-push flags per queue type in AXI register 9, bits 8/9/10 = trigger/data/wave (bits 3:0 still read 9), cleared by core reset or start. `FIFO_GUARD` = 0 restores upstream's pause. coreRevision 29. | `firmware/ip/qick_processor/` | Not proposed upstream yet |
| Library (tProc v2): with `FIFO_GUARD` present in the firmware (`tprocs[0]['queue_flags']`), every round of `acquire`/`acquire_decimated`/`run_rounds` reads register 9 and raises `RuntimeError` naming the queue type that lost a push (`QickSoc.check_tproc_queues()`). `prepare_round` now resets the tProc before arming the readout buffers instead of after, so a trigger left high by a stopped program cannot be captured as a spurious first measurement. Assembler accepts revisions 28 and 29. Docstring of `TPROC_CFG[11]` corrected (1 = do not pause). | `qick_lib/qick/{qick_asm,qick,asm_v2,helpers}.py`, `qick_lib/qick/drivers/tproc.py`, `slab/tests/test_queue_flags.py` | Reorder could go upstream; flags lab only |
| Simulation of the weighted readout buffer (`axis_weighted_buffer` v1.3) against a bit-exact numpy model, and a note of what it computes. No firmware or library change. | `slab/sim/weighted_buffer/`, `slab/notes/weighted-readout.md` | Lab only |
| Lab version number appended to upstream's (`0.2.450.1`) from `qick_lib/qick/LAB_VERSION`, read by `qick/__init__.py` and `setup.py`; release notes and firmware manifest shipped in the package (`LAB_RELEASE.md`, `lab_firmware.json`, module `qick.lab_release`); sync script resets the counter on a new upstream number. | `qick_lib/qick/{__init__.py,LAB_VERSION,LAB_RELEASE.md,lab_firmware.json,lab_release.py}`, `setup.py`, `slab/sync-upstream.sh`, `slab/tests/test_lab_release.py` | Lab only |

With `TIME_SYNC_PERIOD` at its default the firmware behaves as upstream's.

## Dropped

The fork carried three commits from January 2022. They remain in history but
their changes were dropped in the 2026-11-24 merge:

- `8e1b8e16` replaced `qick.bit` and `qick.hwh` with a build that had a 2**14
  accumulation buffer. Upstream now ships one bitstream per board and those
  files no longer exist.
- `078f8d75` made the `QickSoc` import optional when `pynq` is missing.
  Upstream now imports `QickSoc` only when `pynq` is present.
