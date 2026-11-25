# Phase resets on firmware with a time-sync period

## The rule

On firmware built with `TIME_SYNC_PERIOD` (28 tProc cycles, 65.1 ns, on the
lab ZCU216 build), time zero always falls on the same point of the pattern
that the tProc, generator and readout clocks repeat. A phase reset lands on a
reproducible sample only if it is issued at a time, counted from time zero,
that is a multiple of that period (a "legal tick"). The library checks every
phase reset when it compiles a program and refuses one that is not on a legal
tick.

Only the resets have to be on the grid. Pulses, triggers, readout configs
without a reset, and delays can be at any time. The one cost: if you reset in
every shot, each shot has to be a whole number of periods long so the reset
hits the grid every time, and the library pads the end of each shot by less
than one period to make it so.

`soc.time_sync_period_us()` gives the period (0 on firmware without it). On
firmware without a period, everything below still works: resets are issued
when asked, nothing is padded and nothing is checked.

## Reset at the start of every shot

```python
class Prog(AveragerProgramV2):
    def _initialize(self, cfg):
        self.declare_gen(ch=GEN, nqz=1)
        self.declare_readout(ch=RO, length=1.0)
        self.add_readoutconfig(ch=RO, name="ro", freq=f, gen_ch=GEN)
        self.add_pulse(ch=GEN, name="p", style="const", freq=f, phase=0, gain=0.5, length=0.5)

    def _body(self, cfg):
        self.reset_phases()
        self.pulse(ch=GEN, name="p", t=0)
        self.trigger(ros=[RO], t=0.3)

prog = Prog(soccfg, reps=1000, final_delay=10.0)
```

`self.reset_phases()` with no arguments resets every declared generator that
supports it and every declared tProc-configured readout, all on the same legal
tick: the first one at which those channels have finished their previous
pulses and readout windows. It then moves the reference time past the resets,
to the next legal tick, so `t=0` afterwards is free.

To reset only some channels: `self.reset_phases(gens=[0, 2], ros=[0])`. Leaving
out `gens` or `ros` means all of that kind; `gens=[]` or `ros=[]` means none.
A channel that can't be reset (a muxed generator, a generator without a DDS,
a readout not configured by the tProc) is skipped when you don't name it, and
is an error when you do.

`AveragerProgramV2` sees the reset in the shot and, on firmware with a period,
rounds `initial_delay` up to the grid and pads the end-of-shot delay so every
shot is a whole number of periods. Shots without a reset are left alone.
`align=True` forces the padding, `align=False` turns it off (and a per-shot
reset is then refused unless your shot already is a whole number of periods).
If the end-of-shot delay would be swept (for example because a pulse length is
swept), its longest value is used for every shot. With `final_delay=None` the
library can't pad for you: end the shot with `self.delay_auto(..., align=True)`.

### What the reset sends

- A generator gets the built-in "phrst" waveform: zero output for 3 fabric
  cycles.
- A readout gets a copy of its readout config with the reset bit set (named
  `<config>_phrst`, made once), so its frequency and phase stay as you set
  them. If the readout has several readout configs, say which one to copy:
  `self.reset_phases(ros={RO: "ro_long"})`. A config that already has
  `phrst=1` is sent as it is.

The reset zeroes a time counter in the generator or readout; the phase of
every later pulse or readout config is computed from its frequency and that
counter. So pulses and readout configs sent after the reset, without `phrst`,
keep the reset: `send_readoutconfig(ch=RO, name="ro", t=...)` later in the
shot just retunes the readout. Until another config is sent, the readout runs
with the copy, which has the same settings as the original.

## Reset once, before the loops

Put `self.reset_phases()` in `_initialize()` instead. The shots are then not
padded; time zero is on the grid, so the reset is legal.

## Explicit times

`reset_phases(t=0.15)` issues the resets at the first legal tick at or after
0.15 us from the reference time.

`pulse(..., t=...)` and `send_readoutconfig(..., t=...)` with a `phrst=1`
pulse or config are checked as they are. `soc.round_up_to_sync_tick(t)` rounds
a time up to the grid; that is a legal time only if the reference time is on
the grid, which it is at time zero, after `reset_phases()`, and after
`delay(..., align=True)` or `delay_auto(..., align=True)`.

```python
# "p_rst" defined with add_pulse(..., phrst=1)
self.delay_auto(0.2, align=True)
self.pulse(ch=GEN, name="p_rst", t=self.round_up_to_sync_tick(0.15))
```

To see what the checker sees, compile the program and call
`prog.phase_reset_times()`: for each reset, the channel, the time from time
zero in cycles and us (first pass through any loops), and whether it is on
the grid.

## What the check covers

- Delays, `delay_auto`, swept delays and swept reset times (start and step
  must be multiples of the period).
- Loops: a loop that holds a reset and runs more than once must take a whole
  number of periods per pass.
- `resync()` and raw `TIME` instructions other than `inc_ref` with a literal
  make the time unknown; a later reset is refused.
- With labels, jumps, calls or subroutines, every delay in the program must
  be a multiple of the period.
- A reset pulse issued while the previous pulse on that generator is still
  playing is refused.

## What is not checked

- Changes to the tProc time made from the PC (`time_update`, the `time_usr`
  register).
- Waveforms changed while the program runs (`write_wmem` setting the reset
  bit) and raw `WPORT_WR` instructions.
- Periodic-mode pulses that keep a generator busy, and readout configs queued
  behind earlier ones on the same readout.
- The time between issuing a reset and the reset taking effect: it is fixed
  for each generator or readout, and the same at every legal tick.
