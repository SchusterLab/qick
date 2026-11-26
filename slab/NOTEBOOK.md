# Notebook

Running log for the lab fork. Newest entry first. Record dead ends as well as
results, and say where each number came from.

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
