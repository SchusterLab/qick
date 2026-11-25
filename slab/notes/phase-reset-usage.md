# Phase resets on firmware with a time-sync period

## The rule

On firmware built with `TIME_SYNC_PERIOD` (28 tProc cycles, 65.1 ns, on the
lab ZCU216 build), time zero always falls on the same point of the pattern
that the tProc, generator and readout clocks repeat. A phase reset lands on a
reproducible sample only if it is issued at a time, counted from time zero,
that is a multiple of that period (a "legal tick"). The library checks every
phase reset when it compiles a program and refuses one that is not on a legal
tick; it never moves anything unless you ask it to.

`soc.time_sync_period_us()` gives the period (0 on firmware without it). On
firmware without a period, everything below still works: resets are issued at
the times you give and nothing is checked.

## Reset once, before the loops

The simplest case: reset at the start of the program, then run the shots with
any timing. Time zero is on the grid, so a reset at `t=0` is legal.

```python
class Prog(AveragerProgramV2):
    def _initialize(self, cfg):
        self.declare_gen(ch=GEN, nqz=1)
        self.declare_readout(ch=RO, length=1.0)
        self.add_readoutconfig(ch=RO, name="ro_rst", freq=f, gen_ch=GEN, phrst=1)
        self.add_pulse(ch=GEN, name="p", style="const", freq=f, phase=0, gain=0.5, length=0.5)
        self.reset_phases(gens=[GEN], ros={RO: "ro_rst"})

    def _body(self, cfg):
        self.pulse(ch=GEN, name="p", t=0)
        self.trigger(ros=[RO], t=0.3)
```

`reset_phases()` resets every named generator and readout at the same legal
tick, then moves the reference time past the resets to the next legal tick,
so `t=0` afterwards is free and on the grid. Generators get the built-in
"phrst" waveform (zero output for 3 fabric cycles). A readout needs a readout
config defined with `phrst=1`, because a readout config also sets the
frequency and phase; the readout keeps that config afterwards.

## Reset every shot

Pass `align=True` to `AveragerProgramV2`. It rounds `initial_delay` up so the
loops start on the grid, and lengthens the end-of-shot delay (`final_delay`)
so every shot is a whole number of periods. A reset in the shot then lands on
the same tick in every shot.

```python
class Prog(AveragerProgramV2):
    def _initialize(self, cfg):
        ...  # as above, without the reset
    def _body(self, cfg):
        self.reset_phases(gens=[GEN], ros={RO: "ro_rst"})
        self.pulse(ch=GEN, name="p", t=0)
        self.trigger(ros=[RO], t=0.3)

prog = Prog(soccfg, reps=1000, final_delay=10.0, align=True)
```

Each shot can be up to one period (65 ns) longer than without `align`. If the
end-of-shot delay would be swept (for example because a pulse length is
swept), its longest value is used for every shot. With `final_delay=None`,
end the shot yourself with `self.delay_auto(..., align=True)`.

## Explicit times

`pulse(..., t=...)` and `send_readoutconfig(..., t=...)` with a `phrst=1`
pulse or config are checked as they are. Round a time to the grid with
`soc.round_up_to_sync_tick(t)`; this gives a legal absolute time only if the
reference time is on the grid, which it is at time zero, after
`reset_phases()`, and after `delay(..., align=True)` or
`delay_auto(..., align=True)`.

```python
# "p_rst" defined with add_pulse(..., phrst=1)
self.delay_auto(0.2, align=True)
self.pulse(ch=GEN, name="p_rst", t=self.round_up_to_sync_tick(0.15))
```

`reset_phases(..., t=0.15)` does the rounding for you.

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
