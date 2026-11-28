# Notebook

Running log for the lab fork. Newest entry first. Record dead ends as well as
results, and say where each number came from.

## 2026-11-27: rfbox 0.2, from the measurement side's integration against `slab-rfbox-0.1` (branch `lab/rf-driver`)

Against the fake box only. 59 tests in `slab/tests/test_rfbox.py`, 194 in
`slab/tests/` pass; both files parse with the Python 3.8 grammar. The tag
`slab-rfbox-0.1` is left where it was.

- The identity and `get()` carry `rfbox_version` ("0.2") and `rfbox_sha256`
  (of the file as loaded). The mixin has `rfbox_version()`.
- `labdev_*` and every `rfbox_*` call except `set` / `apply` / `apply_safe`
  now raise on failure. In 0.1, `labdev_ro_off` returned the refusal dict,
  which is truthy. The note has the full table of which calls return data and
  which raise.
- `adc_dsa` accepts any value in 0-27 dB and rounds it as qick_rf did;
  `record=True` gives requested, applied and read. `clear_flags` takes a list
  of blocks.
- Filters: on the users' box the filters did not act and may not answer, and
  the strict readback refused at the filter step. Strict stays the default;
  `filter_verify: false` per channel lets the write stand with a warning and
  marks it written-not-verified in `get()`. `filter_probe` gives raw register
  reads with the selects used. Every filter step records the band, state and
  nominal corners written, because older libraries (0.2.360, 0.2.436) choose
  states with different code than rfbox's table.
- `soc_class` (and the board server's RF soc class) wraps every `rfb_*`
  method in the rfbox lock and invalidates what rfbox remembers writing.
- Field numbers from the users (old driver, their setup) recorded in the note
  under "Measured on hardware by the users".

## 2026-11-27: rfbox, a front-end driver for the QICK box (branch `lab/rf-driver`)

Against the fake box only; nothing here has run on a board. 43 tests in
`slab/tests/test_rfbox.py`, 178 in `slab/tests/` pass. Note:
`slab/notes/rfbox-driver.md` (API, readback per chip, upstream hazards with
file:line, loopback checklist, migration table from the measurement side's
`qick_rf.py`).

- `qick/rfbox.py` replaces both the board server's RF code and the
  measurement side's `board_side/qick_rf.py` (same description format and
  hash; every qick_rf function has an rfbox equivalent). It works from
  `adc_cards` / `dac_cards`, chains and chip objects, which 0.2.360 and 0.2.450
  both have; the `rfb_*` port methods, `adc_chains` and the raw filter setter
  are 0.2.4xx only.
- Read from the library, not measured: the card constructor's nested
  `init_filter` selection means only chain 0 of each card gets its 31.75 dB
  attenuator write while selected; `add_MCP` writes the GPIO defaults
  mirrored (DC In power-downs written 0); the RF Out rails are shared by
  ports 0/2 and 1/3 and `enable_rf` switches the rail before the
  attenuators; filter and RF-ADC attenuator `rfb_*` calls clear every ADC's
  interrupts. Full list in the note.
- Corrections to the round-3 board server RF code: the RF Out rail pin is
  readable (same expander as the RF In rails); the constructor does not leave
  every attenuator at 31.75 dB; RF In `att` does not need `amp: true` (that was
  the `rfb_*` call's limitation); a DSA whose current value is unknown is now
  raised first, not last; readout ports follow the ADC name as the library
  does (round 3 used `rf.adcs[name].index`; equal on a ZCU216).
  `test_ro_map_uses_converter_index` became `test_ro_map_port_follows_the_name`,
  `att` alone left the refusal list, the stuck-switch test uses the fake's
  `stuck` set, and bias readbacks compare within the DAC11001 LSB.
- Fake (`qick/rfbox_fake.py`): what it takes from measurement (DSA 0 dB
  after a load, amplifier left on through start-up, DSA reads back integer
  dB) and what is a model (the over-range threshold) is listed in its
  docstring.
- Open for the hardware people: whether the GPIO defaults write lands; whether
  attenuator writes with the card deselected land anywhere; which RF Out
  channels really share a rail; ID of an empty slot (0 or 15); DSA readback
  during over-voltage; whether the AXI bias switch can be read.

## 2026-11-26: board server, third round: from the engine side's client run (branch `lab/board-server`)

All against the fake board; additive on the wire. 78 tests in
`slab/tests/test_boardserver.py` (133 in `slab/tests/`) pass.

- Fake fixed to match the firmware: on a weighted readout the accumulated
  value is now the 32-bit sum of its window's weighted samples (it was
  unweighted); `fake-w` is now tProc revision 29 with 8192 weights, `fake-w27`
  keeps revision 27 / 1024 weights; `fake-rf` gained ADC block 23 (its DC In
  port 7 had no converter block) and a generator on the DC Out card.
- Readouts are mapped to ports through `rf.adcs[name].index`, as
  `RFQickSoc216V1` does, not through the name's digits.
- `capabilities` were only in `welcome`, so a client kept the ones of the image
  loaded when it connected. They are now also in `image_loaded`, `config`
  and `state`.
- ADC flags as per-block lists, with `flags_latched_before` (read before the
  request changes anything) and `flags_after_change`; `rf_clear_flags`.
- Filters decoded with the library's ADMV8818 table (copied: that module needs
  pynq); the request "as written" when the registers do not decode.
- Per-image limits table (bias ranges, attenuation floors, DC In gain
  ceiling) enforced on every `rf_set` (`rf_limit`).
- DC Out: the library can power a channel up (`rfb_set_gen_dc`) but has no
  `rfb_*` call to power it down; the server calls `DacDcChain216.disable()`.
- Confirmed from the library: every load of an RF image writes all eight bias
  outputs to 0 V and enables them (`BiasDAC11001.__init__`, then
  `rfb_enable_bias`). Now stated in the reset record as data.

## 2026-11-26: board server, second round: RF cards, idle_after, image changes (branch `lab/board-server`)

Requested by the engine side, which wrote its own stdlib client, so every
change only adds fields or requests. **Nothing has run on a board; the RF
code on the real path (`rf.LibraryRfHw` over `RFQickSoc216V1`) is unverified.**
68 tests in `slab/tests/test_boardserver.py` (123 in `slab/tests/`) pass on
the lab PC, Python 3.12.

- Loads carry a `load_id` (also in the log lines and `current_image`) and the
  `previous` image; `image_mismatch` names the loaded image; `jobs_queued`
  lists job, submitter, ref.
- `run.idle_after: false` leaves the outputs as the program left them at a
  normal end; errors, cancels, time limits and queue-bound stops still idle.
- The note said queued jobs are rebuilt after a load; the code never did.
  Fixed the other way round: a job that names no image and reaches the head of
  the queue after an image change is not run (`image_changed`), and a
  `run.image` load cancels such queued jobs and lists them. Clients should
  always send `run.image`.
- Checked the client's decimated result against the library: the test runs
  `acquire_decimated` itself on a FakeSoc and compares arrays for the three
  documented shapes. It matched without code changes.
- RF cards. Read from the library: the PE43705 attenuators are write-only (the
  server reports what it last wrote and who wrote it); amplifier switches, DC
  In gain, filter registers and bias setpoints read back. The RF-ADC step
  attenuator and the ADC over-range/over-voltage flags are not in the `rfb_*`
  methods; I added them as the engine side asked (they measured on a ZCU216
  with RF cards that the RF-ADC attenuator is 0 dB after a load and the ADC is
  over range with the RF In amplifier on and nothing playing). Their approach
  (`qubit-measurement` `board_side/qick_rf.py`) is credited in `rf.py`.
- `rf_set` orders the settings itself: attenuation raised and amplifiers off
  first, then filters, then attenuation lowered and amplifiers on, the RF-ADC
  attenuator lowered last, then bias, then the idle flag check. This differs
  from the engine side's fixed order (RF-ADC attenuator first) only when the
  attenuator is lowered; after this server's safe state it starts at 27 dB, so
  lowering it to the working value is a gain increase and now comes last.
- Around every load (on by default, `--no-rf-safe-on-load` turns it off): all
  readout amplifiers off and read back before reprogramming (refused with
  `rf_safe_failed` otherwise), and after the load every RF-ADC attenuator to 27
  dB or the image's safe-state file value, amplifiers off unless the file says
  otherwise, flags cleared and read. Everything read back is in
  `reset.rf`. On the fake (amplifier state survives a load, RF-ADC attenuator
  back to 0) the over-range flag is set after a load without the procedure and
  never with it.
- Found while reading `rfboard.py`/`hardware.py` (not measured):
  `SwitchControl.add_MCP` writes output i's default on pin 3-i while `set_bits`
  uses pin i. For the DC In card the "powered down" default therefore does not
  reach PD0/PD1. Within the window inside `RFQickSoc216V1.__init__` between the
  bitstream download and `_rfb_config`, an amplifier left on by the previous
  image is on while the RF-ADC attenuator is at 0 dB. Both are in the install
  note's measurement list.
- Weights: a fake image with a weighted buffer; a `weights=` array survives
  dump_prog -> server -> `load_weights` bit for bit, and the fake's decimated
  data equals `model.weighted()` of the trace.

## 2026-11-26: board server version 1, against a fake board only (branch `lab/board-server`)

Implemented the server sketched in `slab/notes/board-server-protocol.md`
(now rewritten to match what was built), plus the image store and
`load_image` that the sketch had reserved. **Nothing has run on a board.**
51 tests in `slab/tests/test_boardserver.py` (106 in `slab/tests/` with the
older ones) pass with Python 3.12 on the lab PC; the board-side modules are
only syntax-checked for 3.8 (`ast` `feature_version`), no 3.8 interpreter was
run.

- The fake replaces only the QickSoc/pynq layer (`FakeSoc`): the server, the
  drain, the job store and the real backend's run sequence are the code under
  test. Its ring buffers really get overwritten when the drain lags, so the
  overrun tests check delivered data, not just flags.
- Bug found by that fake in my first drain: when the program finished during a
  transfer, the after-transfer check was skipped and one overwritten shot was
  delivered as data. Fixed (the check now always runs, with the counter capped
  at the last shot); the test that found it checks every shot is delivered
  correctly or reported lost, exactly once.
- `DataStreamer` now uses the same drain loop. Behaviour change for
  `acquire()` users: it also raises when the ring was lapped during a transfer
  (before, that data was returned silently), and it uses each readout's own
  ring length. Checked only on the fake.
- Choices that differ from the sketch: threads rather than asyncio; `ack` has
  no reply; any `run` client may cancel any job (logged); a `read` token only
  authenticates for role `read`; `fetch` takes `from_event` as well as
  `from_seq`; events carry `eseq`; `dropped` entries say whether the chunk had
  already been sent; `run.reduce` is still reserved; tokens are not stored
  hashed (an HMAC challenge needs the key).
- Not measured (list in `slab/notes/board-server-install.md`): the real
  backend at all, DAC outputs during `load_image`, load duration, drain rate
  against shot rate, idle outputs, memory over a long run.

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
