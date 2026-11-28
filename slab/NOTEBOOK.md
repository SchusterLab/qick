# Notebook

Running log for the lab fork. Newest entry first. Record dead ends as well as
results, and say where each number came from.

## 2026-11-28: lab version numbers and release notes in the package

At Dave's request, lab releases are numbered `<upstream>.<lab>`: the first is
`0.2.450.1`, the same library code as the earlier tag `slab-0.2.450-1`. The lab
number lives in `qick_lib/qick/LAB_VERSION`, so upstream's `VERSION` stays
untouched and syncs do not conflict; `__init__.py` and `setup.py` append it
(checked: `qick.__version__` and the built wheel both read `0.2.450.1`). Dave
also asked that the library always ship with a description of the firmware it
goes with: release notes (`LAB_RELEASE.md`) and a manifest of lab firmware
builds with their sha256s (`lab_firmware.json`) are package data, read with
`qick.lab_release`; `identify(bitfile)` names the build a file came from.
Counter rule chosen: it restarts at 0 on each new upstream number (the sync
script does this), so `.0` always means unreleased. Not yet done: the firmware
itself does not carry the version (a constant register or the bitstream's
USR_ACCESS word would let the library read it from the loaded image).

## 2026-11-26: first runs of the lab images on a board; larger-memory weighted build

Builds (Vivado 2024.1, same recipe as 11-25, source `main` at the tag
`slab-0.2.450-1`; the size changes were made in the build copy and are not yet
in `make_bd.py`):

- Weighted, larger memories: tProc `PMEM_AW` 14; weighted buffers 0 and 1 with
  `N_BUF` 14 and `N_WGT` 14; buffers 2-9 `N_WGT` 13. Meets timing (setup
  +0.009 ns, hold +0.010 ns), block RAM 93.75 %. Bitstream sha256 `9f3cec75...`.
- Plain buffers (`axis_avg_buffer` 1.2, `N_BUF` 15 on readouts 0 and 1,
  `PMEM_AW` 14, no weights): routed 0.062 ns short on setup (134 endpoints).
  Three polish passes on the routed checkpoint (`place_design
  -post_place_opt`, `phys_opt_design`, `route_design`, `phys_opt_design`)
  went -0.015, then -0.193 and -0.010, then +0.003 ns: meets timing (setup
  +0.003 ns, hold +0.008 ns), block RAM 88.1 %, finished 11-27 02:08.
  Bitstream sha256 `a004d9c4...`. Not run on a board. Placement-preserving
  steps (fanout optimisation, retiming, re-route) did not move the last
  0.010 ns; only another re-placement round did, so each round is a new draw.
- `N_WGT` 14 on all ten readouts (program memory 4096): fits in 94.8 % block
  RAM (my earlier estimate of over 99 % was wrong) and routed 0.028 ns short
  on setup (56 endpoints). Not polished.

Board runs, done by the measurement side on a ZCU216 that normally runs a
2024 custom image (tProc rev 21, qick 0.2.420, Pyro server from a Jupyter
cell). I did not have access to the board; these are their reported results.

1. Library tag `slab-0.2.450-1` on the board (Python 3.8.2) and client with
   the unchanged 2024 image: their reference measurements matched, and the
   board description differed only by new keys. Before this I compared the
   2024 hardware description statically against the tagged drivers (block
   versions, required parameters) and found nothing missing.
2. The 11-25 weighted image (`e20611d4...`): loaded first time by restarting
   the server with the new bitfile, 33-36 s. Default weights measured at
   -88.6 degrees from unit weights (fit +-3 degrees) with per-sample noise
   0.50 of stock, as the simulation predicts (-90 degrees, half scale). A
   single non-zero weight gave a decimated trace non-zero at that sample only.
   A program with 300 readouts per repetition and no hold lost nothing (the
   2024 image lost 14.7 % on the same program).
3. The larger-memory weighted image: sizes read back as built; 16384-sample
   traces and streams of 750 and 2000 readouts per repetition with no stale
   data.
4. Back to the 2024 image: description identical to before (0 differences).

Not checked on the board: window sum against the accumulated value on one
shot, minimum trigger spacing, the lost-push flags read directly
(`soc.check_tproc_queues()`), and the DAC outputs during a load.

Start-up differences of the newer library that were checked on the board
first and were harmless there: `libi2c.so` and `libsensors.so` load at import;
the library reads the DAC_AVTT rail and would switch it if the image's output
current mode asked for it (both images 20 mA mode, rail 2.475 V, no switch);
the reference-clock equality check. `BOARD=ZCU216` comes from the Jupyter
service's environment and is empty in a plain root shell: a service that
starts QickSoc must set it.

## 2026-11-26: weighted readout buffer simulated against a numpy model (branch `lab/weighted-buffer-sim`)

Question from the measurement side: what exactly does `axis_weighted_buffer`
v1.3 compute, so optimal-filter weights can be designed without guessing.
Simulation only, nothing on a board. Summary and usage in
`slab/notes/weighted-readout.md`; bench in `slab/sim/weighted_buffer/`.

- Bench: the whole IP in xsim (AXI-Lite registers, weight stream as
  `load_weights` sends it, both buffers, read-back streams), N_AVG 13, N_BUF
  12, N_WGT 13 and 14. The DSP macro is Xilinx's simulation model generated
  from the `.xci` (upgraded rev 3 to rev 6 in 2024.1; Automatic pipelining
  gives 4 registers A1, A2, M, P, although `matched_filter.vhd` says
  `DSP_LATENCY = 3`; the mismatch only moves which sample opens the window).
  The N_WGT 13 cases were repeated on the post-synthesis netlist (DSP48E2 and
  block RAM primitives); same results.
- Formula, bit-exact on 2304 extreme pairs and random data: with `wI` = column
  0 (lower 16 bits) and `wQ` = column 1, `y_I = (I*wQ + Q*wI) >> 16`,
  `y_Q = (Q*wQ - I*wI) >> 16`, i.e. `y = (I + jQ)(wQ - j wI) / 2**16`,
  floored. Not `x * (wI + j wQ)` as the column names suggest: the library
  default `(32766, 0)` multiplies by -j/2 (90 degree rotation), not by a real
  0.49997. To multiply by `conj(h)` load `(h.imag, h.real)`.
- Window: weight n on window sample n, exactly `length` samples, sample 0 one
  readout clock after the trigger's rising edge at the IP. Each trigger
  restarts at weight 0; past 2**N_WGT the index wraps. After the window the
  decimated buffer sees `w[length..length+2]` and then `w[0]`.
- Triggers need a spacing of at least length + 4 readout cycles; at length + 3
  or less the accumulated buffer silently skips triggers and misweights. 300
  windows at length + 4: 300 identical, correct entries. A trigger held high
  longer than length + 4 cycles restarts the weights.
- No wrap with |w| <= 32766 at full-scale I and Q; only (-32768, -32768) x
  (-32768, -32768) wraps. Floor bias -0.5 LSB per non-zero-weight sample on I
  and Q alike: -768 +- 11 at N = 1536, -4096 +- 25 at N = 8192.
- Reload and partial load at an address work. In RTL simulation they did not
  at first: xsim mis-connects the 13-bit start address to the 32-bit VHDL port
  `DW_ADDR_REG` (a load at 128 landed at 0). Synthesis connects the low bits
  (Synth 8-689) and the netlist loads correctly, so `run.sh` zero-extends the
  port in its simulation copy only.
- With gaps in tvalid the weights misalign (tvalid delayed 7 cycles, data 9).
  The lab's readouts should give continuous tvalid (not simulated).
- Not checked: hardware, DMA/driver path, trigger timing relative to the tProc,
  edge counting, the m2 port.

## 2026-11-25: lab ZCU216 build with the overflow fix meets timing; survey of upstream branches

- Built `slab_tprocv2_216` from `lab/tproc-overflow` (`bad1a662`) in Vivado
  2024.1 with place directive `EarlyBlockPlacement`, `AggressiveExplore` for
  phys-opt and route, and post-route phys-opt. Result without any manual
  step: WNS +0.003 ns, WHS +0.010 ns, 2 h 38 min for implementation. Merged
  the branch into `main`. Not yet run on a board.
- Upstream's open pull request 446 (`fusesoc-hog`) rewrites the firmware tree
  (FuseSoC cores, Hog projects, `firmware/projects` removed) and its tProc
  notes mention a revision 29 with a different meaning (a QP2 flag). Our
  library detects the lab firmware by the `FIFO_GUARD` parameter, not by the
  revision number, so the two cannot be confused, but the number will need
  changing when that branch lands.
- Upstream has no public QICK-II or Versal branch. The Harvey Mudd / HRL work
  that puts the RISC-Q core in front of QICK's generators is public at
  `github.com/juliacygong/HRL_clinic`.

## 2026-11-25: tProc v2 dispatcher queue overflow (branch `lab/tproc-overflow`)

Report: on a ZCU216 with stock tProc v2 firmware and library 0.2.450, a shot with
N = 300 readouts (`send_readoutconfig`, `pulse`, `trigger(ros=[ch], pins=[0])`,
microsecond spacing) put only about 255 readouts per repetition into the
accumulated buffer, contiguous; the next program's data were shifted too; a
`wait(0)` every 32 triggers fixed it.

Queue use per operation (tProc v2, one 512-entry queue per port, usable 511):
a `trigger` takes 2 entries (set and clear) in the queue of each trigger port
it drives, so `ros=[ch], pins=[0]` takes 2 in port 10's queue and 2 in port 0's;
a `pulse` takes 1 entry per waveform in its generator's wave queue (a pulse
built from several waveforms takes several); a `send_readoutconfig` takes 1 in
the readout's wave queue. With N = 300 the two trigger queues need 600 entries
each, the wave queues 300. 511 / 2 = 255.5 triggers is the reported count:
the trigger queue filled and everything pushed after that was dropped until
`wait_auto` let it drain.

Simulation (`slab/sim/fifo_overflow/`, xsim, qick_processor configured like the
stock design: 20 trigger, 16 wave, 1 data port, FIFO_DEPTH 9, t_clk 430.08 MHz,
core 215.04 MHz; N = 300, 2 repetitions, 2 us spacing):

| Sources | TPROC_CFG | readout triggers out | lost pushes | REG9 |
|---|---|---|---|---|
| rev 21 (stock 2024-09 design), before this change | 0 | 600 / 600 | 0 | n/a |
| this fork before this change | 0 | 600 / 600 | 0 | 0x009 |
| this fork before this change | 0x800 (no pause) | 538 / 600 | 244 trigger | 0x009 (no flags) |
| this change | 0 | 600 / 600 | 0 | 0x009 |
| this change | 0x800 (no pause) | 538 / 600 | 244 trigger | 0x109 (trigger flag); 0x009 after the next start |

With this change and the pause on, no queue was ever full (full cycles 0;
26511 before the change) and nothing was lost for N = 500 unrolled (2000
trigger pushes) and N = 2000 in a tProc loop (8000 trigger and 4000 wave
pushes, 2000 / 2000 readout triggers out); the core was paused for 44538 and
328182 core cycles and resumed each time.

The register-transfer logic pauses the core correctly whenever bit 11 of
TPROC_CFG is 0 (the library never sets it), in the rev-21 sources as well as
current ones. So the hardware loss matches a queue that does not pause the core,
but the cause of the missing pause was not reproduced in simulation. Candidates
not checked: timing of the combinational path from 37 full flags to the enable
of the whole core, or a board bitstream that differs from the sources (read
`soc['tprocs'][0]['revision']` on the board). The docstring for TPROC_CFG[11]
had the polarity backwards (1 means do not pause); `tfifo_full` is part of
`some_fifo_full`.

Change: the pause now comes from a registered almost-full flag (`FIFO_GUARD`
free entries, default 8, at least 5 to cover the pushes in flight), which also
takes the long combinational path out; sticky lost-push flags per queue type
are in register 9 and the library raises after each round when one is set.

Next program shifted: the accumulated buffer's write address is reset by the
library (`config_avg` stops the capture FSM and loads the address; `enable_buf`
starts it), not by the tProc. `prepare_round` armed the buffers before resetting
the tProc, and a stopped tProc keeps its trigger outputs and queued entries. A
program whose trigger-clear was dropped leaves the readout trigger high; the
re-armed buffer then captures one window at address 0 at once, and every later
measurement lands one address late. `slab/sim/avg_rearm/` shows it with
`avg.sv`: arm-then-reset gives 6 writes for 5 triggers, reset-then-arm gives 5.
`prepare_round` now resets the tProc first. This is inferred from the sources
and that simulation; it was not seen on hardware. The streamer also reads
`shots x reads_per_shot` entries per shot counted by the tProc, so in a run that
loses triggers it reads past the buffer's write pointer and returns old data;
the new error stops that run instead.

## 2026-11-24: bare `reset_phases()` at the top of the body (branch `lab/phrst-grid`)

- From user feedback: the common case is one call at the start of `_body`.
  `reset_phases()` now defaults to every declared generator that supports a
  reset and every declared tProc-configured readout; channels that can't be
  reset are skipped unless named. `ros` takes a list; a readout's reset is a
  copy of its only readout config with phrst set (`<config>_phrst`), or of
  the one named in the dict form.
- Firmware (read, not simulated): in `ctrl_dyn_ro_v1.sv` and `ctrl_sg_v6.sv`
  a reset zeroes a time counter `cnt_n`, and each config's phase is
  `freq * cnt_n + phase`. So configs sent later without phrst keep the reset.
- `AveragerProgramV2(align=None)` is now the default: pads the initial and
  end-of-shot delays only on firmware with a period and only if the shot or
  a subroutine has a phase reset. The reference programs for "period present,
  no resets" (`slab/tests/data/phrst_grid_golden_period.json`) were compiled
  with the library at `3684d545`, before any of this work; the existing
  no-period reference files regenerated from it identical.
- Kept the round-up of the reference time after `reset_phases()` to the next
  tick: without it a plain loop of a reset and on-grid delays drifts off the
  grid (seen in a test), and `round_up_to_sync_tick()` would stop giving
  legal times after a reset. Cost: at most one period per reset.
- 48 off-board tests pass. Not run on a board.

## 2026-11-24: helpers for phase resets on legal ticks (branch `lab/phrst-grid`)

- `reset_phases(gens=[...], ros={ch: config}, t=None)`: resets the named
  generators (built-in "phrst" waveform) and readouts (a readout config with
  `phrst=1`; a bare reset would also retune the readout) on one legal tick.
  With `t=None` that is the first tick at which the named channels are free,
  according to the library's timestamps; an explicit `t` is rounded up. The
  reference time then moves past the resets, rounded up to the grid. Without
  a period: issued at `t` and the reference moves past the resets.
- `AveragerProgramV2(..., align=True)`: initial delay and end-of-shot delay
  chosen so the loops start and every shot ends on the grid. A swept
  end-of-shot delay becomes its max value. No effect without a period.
- `delay(..., align=True)` now lengthens the delay so the reference time
  after it is on the grid (before: the delay was rounded to a multiple of the
  period, which is the same when the reference was already on the grid).
- `phase_reset_times()`: what the checker sees, also on firmware without a
  period. `TimeGrid` now always runs; without a period it only records and
  never raises, and the reference programs still compile identically.
- Usage note: `notes/phase-reset-usage.md`. The examples in it were compiled
  off-board; not run on a board.

## 2026-11-24: library schedules phase resets on legal ticks (branch `lab/phrst-grid`)

- The tProc v2 driver reads `TIME_SYNC_PERIOD` into
  `tprocs[0]['time_sync_period']`, 0 when the firmware lacks it.
- With a period above 1 the v2 compiler (`TimeGrid` in `asm_v2.py`) follows
  the reference time through the macros, modulo the period, and refuses a
  phase reset (generator pulse or readout configuration) that is not at a
  multiple of the period from time zero. The error gives the time in cycles
  and us and the nearest legal times. Nothing is moved.
- Loops: a loop that runs more than once and holds a reset must move the
  reference time by a multiple of the period per iteration. Swept delays and
  swept reset times must have start and step on the grid. `resync()`, raw
  TIME instructions other than `inc_ref` with a literal, and subroutines,
  labels and jumps (unless every change of the reference time in the program
  is a multiple of the period) make the time unknown, and a later reset is
  refused.
- Not checked: time changes made from the PC (`time_update`, `time_usr`),
  waveforms edited at run time (`write_wmem` setting the reset bit), raw
  `WPORT_WR` instructions, and periodic-mode pulses keeping a generator busy.
- Helpers: `time_sync_period_us()`, `round_up_to_sync_tick(t)`,
  `delay(..., align=True)` and `delay_auto(..., align=True)`.
- `AveragerProgramV2` with its default delays (1 us initial delay) is not on
  the grid; per-shot resets need `initial_delay=round_up_to_sync_tick(...)`
  and the final delay done with `align=True`. A reset sent once before the
  loops needs nothing else.
- Off-board tests: `slab/tests/test_phrst_grid.py`, 29 passing, on the saved
  testbench config. The reference programs for "no period, no change" were
  compiled with the library on `main` before the change. Not run on a board.

## 2026-11-25: lab ZCU216 build meets timing in Vivado 2024.1

Setup slack 0.000 ns, hold 0.000 ns, no failing endpoints, no routing errors
("All user specified timing constraints are met"). Sources: commit 91387476.
Not loaded on a board.

What it took, starting from the default run's -0.075 ns:

| Tried | Worst setup slack |
|---|---|
| Strategies Performance_Explore, ExplorePostRoutePhysOpt, ExtraTimingOpt | -0.33, -0.33, -0.10 |
| Post-route phys_opt on the default run | -0.074 |
| DONT_TOUCH on the FIFO reset registers | -0.565 |
| Four overlapping pblocks, one per converter-tile group | -0.753 |
| place_design directives: AltSpreadLogic low/medium/high, ExtraNetDelay low/high, ExtraPostPlacementOpt, WLDrivenBlockPlacement | -0.07 to -0.27 |
| place_design -directive EarlyBlockPlacement, AggressiveExplore phys_opt and route | -0.018 |
| ... then place_design -post_place_opt, route_design, phys_opt_design twice | 0.000 |

- The failing paths were never one slow piece of logic: about 900 endpoints
  across 174 kinds of path, all short by 0.05-0.075 ns, 85-95% routing delay.
- The naive floorplan pulled the tProc apart (it talks to every generator);
  its 430 MHz and core clocks then failed by 0.6-0.75 ns.
- `place_design -post_place_opt` unroutes what it moves: judge timing only
  after the following `route_design`.
- Margin is zero. Any change to the design will need the same treatment, and
  the result should be the reference for incremental implementation.
- The stock design under the same strategies reached -0.033 ns (not polished).

## 2026-11-24: first firmware builds in Vivado 2024.1

Both builds: default implementation strategy, about 3 hours each, bitstream
written, setup timing missed slightly, hold timing met.

| Design | Worst setup slack | Failing endpoints | Block RAM |
|---|---|---|---|
| Stock `qick_tprocv2_216_standard` (plus the PFB type rename) | -0.100 ns (599.04 MHz generator clock) | 568 | 81% |
| `slab_tprocv2_216` | -0.075 ns (generator and 430.08 MHz tProc clocks) | 903 | 88% |

- The failing paths in the lab build are in the full-speed generators
  (`axis_signal_gen_v6`), the `axis_cdcsync` crossings and the tProc. None are
  in the weighted buffers or the new weight-loading path.
- Whether upstream's 2023.1 build of the stock design meets timing is not
  known: the shipped bitstream comes with no timing report.
- The library's `trace_dma` finds `axi_dma_wgt` / `axis_switch_wgt` port n for
  each of the ten buffers in the lab build's `.hwh`, and reads
  `TIME_SYNC_PERIOD` = 28 and `N_WGT` = 13. Checked off-board with a stand-in
  for `pynq`; not loaded on a board.
- A first attempt hung for two hours: a run left in the "running" state by a
  failed build makes `launch_runs` start nothing. `reset_run` on every run
  that is not complete before launching.
- Next: other implementation strategies to close timing.

## 2026-11-24: readout phase against frequency (referral from the measurement project)

- Read `ctrl_sg_v6.sv`, `ctrl_dyn_ro_v1.sv`, `axis_cdcsync_v1` and the stock
  ZCU216 tProc v2 block design, and analysed stored labdev runs 306 and 314.
- Findings, evidence and proposed tests are in
  [notes/readout-phase.md](notes/readout-phase.md).
- Simulated the reset path with the real HDL in Vivado 2024.1's simulator
  (`slab/sim/reset_path/`). It reproduces the 28 and 7 offsets exactly and
  puts the reset-path part of the residual at about 15 ns of 151 ns.
- Not done: any measurement on hardware.

## 2026-11-24: phase reset made deterministic (design and first firmware change)

- Tried and dropped: a block that holds reset words until the next legal
  tick. It makes the phase repeatable but delays the reset-carrying pulse by
  0 to 65 ns, differently on each run. Dave prefers hardware that does
  nothing automatically, with software scheduling resets on legal ticks.
- Done: `TIME_SYNC_PERIOD` option in `qproc_ctrl` so time zero always falls
  on the same slot of the 28-cycle clock pattern; simulated in
  `slab/sim/time_sync/` (1 slot with the option, 28 without, 200 starts).
- Options considered for keeping clocks integer-related instead (slower
  tProc, or lower DAC and ADC rates through `dac_sample_rates` /
  `adc_sample_rates`) were set aside: they cost tProc resolution or DAC
  frequency reach. Details in `notes/readout-phase.md`.
- Vivado: the lab machine has 2024.1 only (licence cap). The stock ZCU216
  tProc v2 project creates and validates there with the 2023.1 version check
  bypassed. One block fails to synthesise (`axis_pfb_readout_v4`: its FFT
  code declares `BOOLEAN_VECTOR`, which clashes with VHDL-2008); renaming the
  type fixes that block. Full build result pending.

## 2026-11-24: fork brought up to date

- The fork (`SchusterLab/qick`) was last pushed in August 2024 and its `main`
  was 2227 commits behind upstream and 3 ahead (lab commits from January 2022).
- Merged upstream `main` at `b2abda7b` (version 0.2.450) into `main`. The tree
  was taken from upstream as is; the three old lab commits are obsolete (see
  `CHANGES.md`). No history was rewritten.
- Branch model, install instructions and the sync script are in `README.md`.
- Off-board check: `pip install -e .` in a fresh Python 3.12 venv works and
  `import qick` succeeds without `pynq` (measured here).

### Found while setting up

- `firmware/testbench/qick_testbench/soccfg.json` is stale. It was written by
  library 0.2.367, and `print(QickConfig(path))` with 0.2.450 raises
  `KeyError: 'dac_power'` in `QickConfig.description()` (measured here, after
  the expected version-mismatch warning). Not yet checked whether the
  testbench itself still runs with it.
- The repository has no tests that run without a board. The only CI test is
  `test_zcu111.yml`, which runs the demo notebooks on a self-hosted ZCU111.

### Candidate first projects (not started)

- An off-board test suite: build and compile programs against saved
  configurations for each board and compare with known-good output. This would
  let changes be checked on any machine and in ordinary CI.
- Refresh or regenerate the stale testbench configuration above.
