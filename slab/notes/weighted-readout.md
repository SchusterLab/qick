# Weighted readout (axis_weighted_buffer v1.3): what it computes

2026-11-26. Established by simulation only (`slab/sim/weighted_buffer/`):
xsim RTL simulation of the whole IP (registers, weight DMA stream, matched
filter, both buffers, read-back streams) with Xilinx's own model of the DSP
macro generated from its `.xci`, plus the post-synthesis netlist of the IP
(Vivado 2024.1, xczu49dr) for the N_WGT = 13 cases. Every check compares
against the numpy model `model.py` bit for bit. **Nothing here has been run on
a board.** Parameters as in the lab ZCU216 build: N_AVG = 13, N_BUF = 12,
N_WGT = 13 (the key cases also at N_WGT = 14), B = 16.

## The formula

The weight array is what you pass to `declare_readout(weights=w)`: int16 of
shape (N, 2). `load_weights` sends `w[n, 0]` in the lower 16 bits of the word
and `w[n, 1]` in the upper 16 bits. Call them `wI = w[:, 0]` and
`wQ = w[:, 1]`, as the library's comment does. The readout's sample has I in
the lower and Q in the upper 16 bits. The firmware computes, for each sample,

    y_I = (I*wQ + Q*wI) >> 16
    y_Q = (Q*wQ - I*wI) >> 16

with 32-bit two's-complement products and sums and an arithmetic shift, i.e.
`floor(. / 65536)`. As complex numbers:

    y = floor( (I + jQ) * (wQ - j*wI) / 2**16 )

This is **not** `(I + jQ) * (wI + j*wQ)`. It equals `-j * x * (wI + j*wQ)`.

What to load:

| You want each sample multiplied by | load `w[:, 0]` | load `w[:, 1]` |
|---|---|---|
| complex `h` (`y = x * h`) | `-h.imag` | `h.real` |
| `conj(h)` (matched filter, `y = x * conj(h)`) | `h.imag` | `h.real` |
| a real gain `g` | `0` | `g` |

The library default (`load_envelopes` fills `w = (32766, 0)` when no weights
are declared, described in the docstring as "the maximum positive real
value") is therefore multiplication by **-j** * 32766/65536: the decimated and
accumulated I is +0.49997 x input Q and Q is -0.49997 x input I, a -90 degree
rotation. A real gain of 0.49997 is `(0, 32766)`.

The accumulated buffer stores the 32-bit sum of the `y` of the window
(`get_accumulated`: lower 32 bits I, upper Q); the decimated buffer stores `y`
itself (`get_decimated`: lower 16 bits I, upper Q). The sum of a window's
decimated samples equals its accumulated value exactly.

## Alignment and length

- Weight index `n` multiplies window sample `n`, for `n = 0 .. length-1`; the
  accumulated value is the sum of exactly `length` samples (the length
  register, no extra or missing sample).
- Window sample 0 is the sample one readout clock after the one on the bus in
  the cycle the trigger rose at the IP's input (trigger driven synchronously
  to the readout clock in the testbench).
- After the window the decimated buffer, if longer, sees samples `length`,
  `length+1`, `length+2` weighted by `w[length .. length+2]` (whatever the
  memory holds there) and every later sample weighted by `w[0]`. QICK
  programs give both buffers the same length, so this does not arise there.
- Each trigger restarts the weight address at 0.
- A window longer than the weight memory (2**N_WGT samples) wraps: sample `n`
  uses `w[n mod 2**N_WGT]` (checked at 9000 samples for N_WGT = 13 and 17192
  for 14).
- `load_weights(data, addr)` writes memory `[addr, addr + len(data))` and
  leaves the rest; the window always starts reading at address 0. Reloaded
  weights take effect on the next window.

## Triggers

- Minimum spacing between trigger rising edges: **length + 4 readout clock
  cycles**. At length + 3 or less nothing reports an error: the accumulated
  buffer skips triggers, and the windows it does record are weighted from the
  wrong weight address. 300 windows back to back at length + 4 all gave
  identical, correct entries.
- The trigger must not stay high longer than length + 4 cycles: while it is
  high the weight address restarts every length + 4 cycles. The default
  width (10 tProc cycles, about 7 readout cycles) is fine for length >= 3.

## Range and truncation

- With |wI|, |wQ| <= 32766 (the library's limit) nothing wraps, even with
  full-scale input on I and Q (|y| <= 32766). The only wrap found over all
  extreme combinations is input (-32768, -32768) with weight (-32768, -32768),
  which gives y_I = -32768 instead of +32768.
- The floor adds a negative offset to every sample whose weight is non-zero:
  for noise-like input -0.5 output LSB per sample on I and on Q alike (sum and
  difference behave the same), with a spread of sqrt(N/12). For the
  accumulated value of N samples: -768 +- 11 at N = 1536 and -4096 +- 25 at
  N = 8192 on each of I and Q (numpy, 200 noise traces; the simulated windows
  agree). To remove it, add `0.5 * (number of non-zero weights)` to both I and
  Q of the accumulated value, or exactly `-model.truncation_bias(w).sum()`
  (weights with common factors of 2, such as 32766, give
  `(1 - 2**t/65536)/2` per sample; a weight of (0, 0) gives none).

## Example

```python
import numpy as np

n = prog.us2cycles(ro_ch=ro_ch, us=ro_len_us)  # decimated samples in the window
h = template[:n]                               # complex: e.g. mean |e> trace - mean |g> trace
scale = 32766 / np.abs(np.concatenate([h.real, h.imag])).max()
w = np.round(np.stack([h.imag, h.real], axis=-1) * scale).astype(np.int16)  # y = x * conj(h)
prog.declare_readout(ch=ro_ch, length=ro_len_us, weights=w)
# accumulated value: I + jQ = sum(floor(x * conj(h) * scale / 2**16)), about -0.5*n per component from the floor
```

`len(w)` must equal the readout length in cycles exactly (the library checks).
`slab/sim/weighted_buffer/model.py` gives `weighted(x, w)` and
`accumulate(x, w)` to predict the decimated trace and the accumulated value
bit for bit from a recorded trace.

## Not checked

- Anything on hardware; the DMA, switch and driver code (the testbench reads
  the buffers' streams directly; register numbers and unpacking were read from
  `readout.py`).
- Where window sample 0 falls relative to the tProc's trigger time: that
  depends on paths outside this IP, and in hardware the trigger crosses from
  the tProc clock, so it may vary by a sample.
- Input with gaps in tvalid. The matched filter delays tvalid by 7 cycles and
  the data by 9, so with gaps the weights misalign (diagnostic case `gap`:
  142 of 200 samples right). The dynamic readout's decimating FIR takes 8
  samples per clock and decimates by 8, so its output should be continuous;
  that and the PFB readout's output were not simulated.
- Edge-counting mode and the tProc read-back port (m2).
- xsim mis-connects the 13-bit weight start address to the 32-bit VHDL port
  (a load at address 128 lands at 0); `run.sh` zero-extends it in its copy of
  the source. Synthesis connects it correctly (warning Synth 8-689), and the
  netlist simulation of the same case passes.
