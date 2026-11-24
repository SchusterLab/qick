# Readout phase against frequency: what sets it and what moves it

2026-11-24. Answer to the labdev agent's referral
(`qubit-measurement/notes/qick-readout-phase.md`). Source read at upstream
`b2abda7b` (0.2.450), which is what this fork's `main` contains. Run numbers
are labdev runs on the BFG1 setup. Each statement is marked **source** (read
in the firmware or library), **data** (computed from stored runs) or
**inference** (not checked).

## Summary

1. The reading in the referral is right. The slope of readout phase against
   frequency is the time between the zero points of two sample counters, one
   in the generator and one in the readout, plus the signal path.
2. Nothing in the relevant firmware changed between 0.2.420 and 0.2.450, so
   the reading applies to the version on BFG1. BFG1's tProc and generator
   clocks match the stock design (data); its readout clock is inferred.
3. A reset lands on one of 28 time offsets for the generator but only 7 for
   the readout. The offset is fixed by where the reset falls in a 28-cycle
   pattern of the tProc clock.
4. The one result the model did not explain (partial amplitude with the 10 us
   wait) is consistent with each round landing on a different offset. That is
   an inference; the stored data cannot test it. A test is given below.
5. Per-shot resets cannot be made reliable with this firmware. A one-off
   alignment is the right practice, and it leaves the slope repeatable to a
   few ns but the phase intercept free to take a different value each time.

## The mechanism (source)

`ctrl_sg_v6.sv` (generator) and `ctrl_dyn_ro_v1.sv` (dynamic readout) have the
same structure:

- `cnt_n` adds the number of samples per clock (16 for the generator, 8 for
  the readout) on every clock and never stops (`ctrl_sg_v6.sv:307-310`,
  `ctrl_dyn_ro_v1.sv:224-227`).
- When a waveform or readout configuration is loaded, the counter is latched
  and the oscillator is loaded with phase = frequency word x counter + phase
  word (`ctrl_sg_v6.sv:312-313, 491`; `ctrl_dyn_ro_v1.sv:229-230, 326-327`).
- The counter is zeroed only by the block's reset or by a load with
  `phrst = 1`.
- The counter is 32 bits and wraps about every 0.45 s in the generator. The
  wrap is harmless: frequency word x 2^32 is zero modulo 2^32.

So the generator's phase is 2 pi f (t - t_gen) and the readout's is
2 pi f (t - t_ro), where t_gen and t_ro are the times each counter read zero.
The measured phase is 2 pi f (t_ro - t_gen - path delay). It does not depend
on when the pulse is played or when the readout is triggered, which matches
the referral's observation 1.

## Clocks and the 28-cycle pattern

Stock `qick_tprocv2_216_standard` (source, `bd_2023-1.tcl`):

| Domain | Clock | Source |
|---|---|---|
| tProc timing (`t_clk`) | 430.08 MHz | DAC tile 2 |
| Generator 0 (`axis_signal_gen_v6_0`) | 599.04 MHz, 16 samples per clock | DAC tile 0 |
| Dynamic readout 0 | 307.2 MHz, 8 samples per clock | ADC tile 2 |

BFG1 (data, from the compiled sequence stored with run 306): the trigger time
0.769624 us is exactly 331 cycles of 430.08 MHz, the pulse length 4.999666 us
is exactly 2995 cycles of 599.04 MHz, and the readout frequency offset of
4915.2 MHz is twice an ADC rate of 2457.6 MS/s. The tProc and generator
clocks are therefore the stock ones. The readout clock of 307.2 MHz follows
if the readout takes 8 samples per clock; `soccfg_bfg1.json` would confirm it
(`readouts[0]['f_fabric']`).

The three clocks are 28, 39 and 20 times 15.36 MHz. Their relative phases
repeat every 28 tProc cycles (65.1 ns). A reset issued at a given tProc cycle
takes effect on a later edge of the block's own clock, after an asynchronous
FIFO (`axis_cdcsync_v1`). Relative to the tProc time:

- the generator counter zero falls on one of 28 offsets, 59.6 ps apart;
- the readout counter zero falls on one of 7 offsets, 465 ps apart (not 28,
  because 28 and 20 share a factor of 4).

One generator clock is 0.68 of a cycle of phase at 5.8 GHz and one readout
clock is 0.87 of a cycle (data, from the frequencies of run 306), so these
sub-ns offsets scramble the phase completely.

Consequences:

- Resets every shot give the same phase on every shot only if the shot period
  is a multiple of 28 tProc cycles.
- Each time the tProc is started it begins at an arbitrary point in the
  28-cycle pattern (inference: the start comes from software and is not
  synchronised to the pattern). Two runs of the same program, or two rounds
  of one acquisition, therefore land on different offsets.
- The FIFO crossing can add or drop one destination clock when the write
  falls close to a read edge (inference; not seen in the data).

## What the stored runs show (data)

Run 306, linear fit of unwrapped phase against frequency, 200 shots per
point (20 reps x 10 rounds):

| Variant | Delay (us) | Mean amplitude (ADU) |
|---|---|---|
| No resets | 10.166, 10.161 | 15.8, 16.0 |
| Resets every shot, wait 0 to 3 us | no linear phase | 1.05 to 1.81 |
| Resets every shot, wait 10 us | 10.152, 10.145 | 7.90, 4.98 |

- The short-wait amplitudes equal what 200 shots of random phase give:
  16 / sqrt(200) = 1.13 ADU. The phase is fully scrambled shot to shot.
- The delay of 10.15 us in the no-reset scans is the 10 us wait of the last
  reset that had been issued (4301 tProc cycles = 10.0005 us) plus the
  0.151 us measured after alignment in runs 313 and 314. This explains the
  referral's observation 3.
- With the 10 us wait the phase is linear and the delay is right, but the
  amplitude is 0.49 and 0.31 of full. The mean of 10 unit vectors with random
  phases has typical length 1 / sqrt(10) = 0.32. This fits rounds that are
  each coherent but land on different offsets. Run 306 stored per-round data
  only for the no-reset variant, so this is not tested.
- In the no-reset variant the ten rounds agree in phase to within 0.03 of a
  cycle, as expected when nothing is reset.

## Simulation of the reset path (added 2026-11-24)

`slab/sim/reset_path/` simulates a reset issued on one tProc clock edge
travelling down both paths, using the real source for every block on the way:
`cdcsync` and its XPM FIFO, `sg_translator`, the generator's FIFO and
`ctrl_sg_v6`; and `tmux`, `cdcsync`, `sg_translator`, the readout's FIFO and
`ctrl_dyn_ro_v1`. Clocks are the stock rates with exact ratios. It records the
clock edge at which each sample counter is zeroed, for resets issued on each
of the 28 slots, three passes, four choices of static clock phase.

Results (simulation):

| Quantity | Value |
|---|---|
| Generator: reset to counter zero | 24.7 to 26.3 ns, 28 distinct values 59.6 ps apart |
| Readout: reset to counter zero | 38.6 to 41.8 ns, 7 distinct values 465 ps apart |
| Readout minus generator | 12.4 to 16.9 ns, 28 distinct values, mean 14.5 to 14.9 ns |
| Same slot on a later pass | identical every time |
| Mean phase vector over the 28 slots at 5.798 GHz | 0.04 to 0.07 |

What this settles:

- The 28 and 7 offset counts and their spacings are exactly as derived above.
- A given slot always gives the same result, so the crossing itself adds no
  randomness in simulation. (Real hardware has jitter that simulation does
  not; a slot that sits on a clock edge could still flip.)
- Resets every shot with a period that is not a multiple of 28 cycles average
  to 0.04 to 0.07 of full amplitude, about 0.6 to 1.1 ADU on a 16 ADU signal.
  That is below the 1.13 ADU random floor of 200 shots, as seen in run 306.
- The digital reset paths account for about 15 ns of the 151 ns residual. The
  other 136 ns or so is the oscillator-to-converter pipelines, the converters,
  the cables and cryostat, and the device.
- After a one-off alignment the slope should repeat to within about +-2.2 ns,
  and the phase intercept takes one of 28 values.

Not modelled: the tProc's own output stage (assumed equal for the two ports),
the register slice before the generator (modelled as one register), the
oscillators and converters, and clock jitter.

## A design that stays in sync (added 2026-11-24, proposal)

The scrambling comes from two things: sample counters with independent
origins, and clocks whose relative phase repeats only every 28 tProc cycles.
Ranked by cost:

1. **Integer-related clocks.** Run the tProc and the readout at 299.52 MHz
   (half the generator's 599.04 MHz; ADC at 2396.16 MS/s instead of 2457.6).
   Simulated with the same testbench and unchanged HDL (`TH`, `RH`
   parameters): every reset lands on a single offset, for three choices of
   static clock phase. Per-shot resets then give the same phase every shot
   and every run. Costs: tProc time resolution 3.3 ns instead of 2.3 ns, a
   different ADC rate, a new bitstream. Not checked: that the converter PLLs
   accept these rates on the ZCU216, and timing closure.

   | tProc | Readout | Distinct values of readout minus generator |
   |---|---|---|
   | 430.08 MHz | 307.2 MHz (stock) | 28 |
   | 430.08 MHz | 299.52 MHz | 3, one generator clock apart |
   | 599.04 MHz | 299.52 MHz | 2 |
   | 299.52 MHz | 299.52 MHz | 1 |

2. **One reset release for all counters.** Bring every generator and readout
   out of reset from one synchronised event, so the slope is the true
   electrical delay straight after a bitstream load. This is the one-off
   alignment done by the firmware. With option 1 it is exact; without it, it
   has the same 28-value spread as a software alignment.
3. **Multi-tile synchronisation with SYSREF.** Options 1 and 2 leave the
   static phase between converter tiles, which changes when a tile restarts
   or the board power-cycles. Aligning the tiles to SYSREF and zeroing the
   counters on a SYSREF-derived edge would make the phase reproducible
   across power cycles. More work: firmware and library.
4. **A readable check.** Latch every sample counter on a common strobe into
   registers, so software can confirm the alignment without an RF
   measurement.

## Chosen design: resets scheduled on legal ticks (2026-11-24)

Decision (Dave): the hardware does nothing automatically to a reset word.
Software schedules every phase reset on a legal tick, and the hardware is
fully deterministic. An earlier block that held reset words until the next
legal tick (`axis_phrst_sync_v1`) was dropped for this reason; it is in the
history at the commit "Add axis_phrst_sync_v1".

A legal tick is a tProc time that is a multiple of the common period of the
clocks: 28 tProc cycles (65.1 ns) with the stock ZCU216 rates. The reset-path
simulation shows that resets issued on the same slot of that period always
land on the same offset.

For software to know the slot, time zero has to fall on a fixed slot. That is
the one hardware change:

- `qproc_ctrl` has a new parameter `TIME_SYNC_PERIOD`. When it is 28, a time
  reset (program start, or a time reset issued by the program) waits in a new
  state, with time stopped and nothing dispatched, until a free-running
  28-cycle counter reads zero. The wait is at most 65 ns and happens before
  time zero, so every time in the program is exact. Default 0: unchanged.
- Simulated (`slab/sim/time_sync/`): 200 starts at random times against an
  unrelated core clock. Option on: time zero falls on 1 slot. Option off: on
  all 28.

Still to do:

- Pass the parameter up through `qick_processor` and the IP packaging, and
  expose the value to the library through the board configuration.
- Library: place any pulse or readout configuration with a phase reset on a
  multiple of the period, and keep the time reference on that grid (delays
  that move the reference rounded to multiples of the period); refuse a
  program that breaks the rule.
- A run-time shift of the tProc time (the time-update command) by an amount
  that is not a multiple of the period breaks the alignment. The library has
  to refuse or round it.

## Answers to the five questions

**1. Is the reading right?** Yes for the source. The clocks are confirmed as
above. The model's prediction of full amplitude for the 10 us variant left
out the restart of the tProc at each round.

**2. What sets the remaining 0.15 us?** With both resets issued at the same
tProc time it is the sum of:

- the difference in latency between the two reset paths (tProc port, FIFO
  crossing, translator, block FIFO, control pipeline), a fixed number of
  clocks in each domain plus the offset described above;
- the delay from the generator's oscillator through the DAC, the cables and
  cryostat, and the ADC to the readout's mixer;
- the group delay of whatever is in the path within the scan window
  (filters, mixers, the resonator itself if the fit does not separate it).

It should repeat to within a few ns after each alignment (one clock of each
domain is 1.7 and 3.3 ns). It is not computable from the configuration today:
the library holds no latency model for these paths. A scan with the DAC
cabled directly to the ADC would measure the firmware and converter part;
the rest is the cryostat path. The simulation above gives the reset-path part: about 15 ns.

The phase intercept is a different matter. It changes by a large fraction of
a cycle for each sub-ns step, so it takes a new value at every alignment.

**3. Can the phase be made deterministic by construction?**

- Not with per-shot resets. A shot period that is a multiple of 28 cycles
  gives coherence within one tProc run only, and breaks when any timing in
  the program changes.
- A reset in a section that runs once per program start has the same
  limitation between runs and rounds.
- One alignment per session, then no resets, is sound. The slope is then
  fixed and known to a few ns. This is what `ro_align` does.
- A fix by construction needs firmware: zero both counters on an event that
  is common to all clock domains, for example an edge of the 15.36 MHz common
  period or a SYSREF-derived pulse with multi-tile synchronisation. That
  would belong in QICK. Nothing like it is in upstream now (source: only the
  XCOM documentation mentions multi-tile sync). Whether it is planned is a
  question for the upstream maintainers.

**4. What moves the counter origins?** (source unless marked)

Moves them:

- loading the bitstream, which `QickSoc()` does by default on construction;
  the two blocks come out of reset at unrelated times, which is where a value
  like the original -23.3 us comes from (inference);
- `QickSoc.pl_reset()`;
- restarting converter tiles (`restart_all_tiles`, `restart_adc_tile`), which
  resets the logic on those clocks;
- any program, from any user of the board, that plays a pulse on that
  generator or sends a configuration to that readout with `phrst = 1`.

Does not move them: starting, stopping or resetting the tProc, loading a
program, and the counter wrapping.

To detect a change, measure the slope. Two frequencies a known step apart
give it in one short acquisition; compare with the value recorded at
alignment. A change larger than a few ns means the alignment is gone.

**5. Anything upstream since 0.2.420?** No. No commit between 0.2.420
(`73b4bf34`, 2026-07-11) and 0.2.450 touches `axis_signal_gen_v6`,
`axis_dyn_readout_v1`, `axis_cdcsync_v1` or the stock ZCU216 tProc v2
project. One documentation commit (`5f51e360`) changes the tutorials to
frequency-match every pulse to its readout.

## Tests that would settle the open points

All need the board. None needs more than the readout power already in use.

1. Repeat the 10 us reset variant and store each round separately.
   Prediction: every round has full amplitude (about 16 ADU) and a linear
   phase with the same slope; the rounds differ by constant phase offsets.
2. Same, storing single shots, with a shot period that is not a multiple of
   28 tProc cycles. Prediction: the phase repeats with shot index with period
   28 / gcd(shot period in cycles, 28).
3. Run the alignment 20 times, measuring slope and intercept after each.
   Prediction: slopes within a few ns of each other; intercepts on a discrete
   set of values.
4. One alignment and scan with DAC cabled to ADC, to split the 0.15 us into
   electronics and cryostat.
