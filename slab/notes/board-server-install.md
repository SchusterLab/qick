# QICK board server: installing on a board

Status: **never run on a board.** Everything below the measurement list is the
intended procedure; nothing in it has been tried on hardware. Protocol and
message reference: `board-server-protocol.md`.

## 1. Install

On the board, as the user that runs QICK (usually `xilinx`, with the PYNQ
environment active, as root for FPGA access):

    pip install git+https://github.com/SchusterLab/qick@<commit>

Install the same commit on the PCs that will connect (the client warns, as
`QickConfig` does, when versions differ). Stop any Pyro server first: both
would construct a `QickSoc` and fight over the FPGA. The board server takes
the lock file `/tmp/qick-boardserver.lock`; the Pyro server does not check it.

## 2. Token file

    python -m qick.boardserver new-token /home/xilinx/.qick/board.token
    python -m qick.boardserver new-token /home/xilinx/.qick/board.token --role read   # optional

The file holds `run <token>` / `read <token>` lines, mode 600; the server
warns if group or others can read it. Copy it to the clients over a trusted
channel (the lab's claim-link convention), into a file only the client user
can read. The token is never given on the command line or in an environment
variable, and the server never logs it. Without a token file the server
refuses to start.

## 3. Image store and the rollback image

The image directory holds named `.bit`/`.hwh` pairs by content hash. Before
anything else, import the image the board runs now, so there is always a known
image to go back to:

    python -m qick.boardserver import-image --image-dir /home/xilinx/qick-images \
        --name rollback --bit /path/to/the/current.bit --note "what the board ran before the server"

(`--hwh` defaults to the `.bit` path with `.hwh`.) For the stock firmware the
pair is in the installed package, e.g. `.../site-packages/qick/qick_216.bit`.
More images can be imported the same way or uploaded by a client with
`BoardClient.put_image()`.

## 4. Start

    python -m qick.boardserver --token-file /home/xilinx/.qick/board.token \
        --image-dir /home/xilinx/qick-images --port 8788 --load rollback \
        --log-file /home/xilinx/qick-boardserver.log

- `--load NAME` programs the FPGA with that image at start (a full
  `QickSoc(bitfile=...)`).
- `--attach NAME` instead constructs `QickSoc(..., download=False)`, assuming
  the FPGA already runs that image; `current_image` then says "unknown, loaded
  before server start" because it cannot be checked.
- With neither, the server starts without a `QickSoc`; clients must
  `load_image` first.
- `--soc-kwargs '{"external_clk": true}'` passes options to `QickSoc`.
- `--allow-job-image-load` lets a job that names another image load it
  (default: refused with `image_mismatch`).
- `--soc-class auto|QickSoc|RFQickSoc216V1` (default `auto`: the RF class when
  the `.hwh` has the `rfb_control` block). `--no-rf-safe-on-load` turns off
  the RF procedure around image loads (section 6); it is on by default.
- `--max-queue-mb` bounds the memory held for unsent and unacknowledged chunks
  (default 256 MiB; the ZCU216 has 4 GiB of PS RAM, part of it used by PYNQ).
- A JSON file given with `--config` may hold the same settings
  (`token_file`, `image_dir`, `port`, `allow_job_image_load`, ...), never the
  token itself.

To keep it running, a systemd unit like `pyro4/qick_pyro.service` with this
command line works the same way.

## 5. Before use on a fridge: measure on a loopback board

None of these has been measured. Do them on a non-shared board with DAC
outputs looped back to ADC inputs (and a scope on a DAC output), and record
the results in `slab/NOTEBOOK.md`.

1. **The real backend at all.** `QickSocBackend` is written from
   `acquire()`/`acquire_decimated()`/`DataStreamer` but has never touched a
   `QickSoc`. Run the same AveragerProgramV2 (loopback readout, several loop
   dimensions, 2 reads per shot, rounds > 1) through `prog.acquire(soc)`
   locally on the board and through the server, and compare raw shots
   (`get_raw()` against `Job.raw_rounds()`) and `acquire()` output bit for bit.
   The same for `acquire_decimated` (`mode='dec'`), for external start, and
   for a tProc v1 firmware if one is still in use.
2. **DAC outputs during `load_image`.** Scope a DAC output that is playing a
   periodic tone, then `load_image`: what the output does while the bitstream
   is replaced and `QickSoc` reconfigures (glitches, full-scale transients,
   how long it is undefined), and with `clocks_reprogrammed` true and false.
   Until this is known, never load an image with a sample connected.
3. **Load duration.** `image_loaded.duration_s` for each image, with and
   without clock re-programming; how long the server is unavailable.
4. **Drain rate against shot rate.** Shots per second the drain sustains for
   1, 2 and 4 readouts and 1-2 reads per shot, with a client reading at full
   speed, with a slow client, and with none (the store filling to the bound);
   at what shot rate `overrun` events start. Check that a 0.5 ms poll sleep
   is a good choice (CPU load of the server, GIL contention with the sender
   threads) and how the drain compares with `DataStreamer` in `acquire()`.
5. **Overrun detection on real hardware.** Deliberately overrun (very short
   `final_delay`, many shots) and check that every delivered shot is correct
   and every lost shot is reported, as the fake test does. The detection
   assumes the ring and counter behave as `DataStreamer` assumes; confirm it.
6. **tProc queue flags.** On the lab firmware with `FIFO_GUARD`, a program
   that overflows a dispatcher queue must end as `failed` with
   `tproc_queue_overflow` naming the queue.
7. **Idle outputs.** After `done` (each outcome, including `cancelled` mid-run
   and a stop at the queue bound), the DAC outputs are quiet (scope), and how
   long `reset_gens` takes between back-to-back jobs.
8. **`get_state` during a run.** Reading the tProc debug registers changes
   `read_sel`; the server serialises it with the drain, but check that a run
   with frequent `get_state` calls delivers correct data.
9. **Memory.** That the server process stays within the bound plus a small
   overhead over a long run (`status.memory`), and that a failed or repeated
   `load_image` does not leak (each `QickSoc` starts a `DataStreamer` thread
   that stays alive).
10. **Network loss.** Pull the cable during a long run: the job continues,
    `fetch` after reconnect resumes with no gap.
11. **`idle_after: false`.** With a scope on a DAC output, a job that ends
    with a tone still playing and `idle_after: false` leaves it playing; the
    next job (or an error, cancel or time limit) makes it idle.

## 6. QICK box with RF cards (ZCU216, `qick_tprocv2_216_rfbv2`)

**Read first**, and write it in the notebook before anything else:

- the PYNQ version (`pip show pynq`) and the Python version
  (`python3 --version`; the server needs 3.8 or later with numpy);
- whether the image's `.hwh` has the RF-board control block:
  `grep -c rfb_control image.hwh` (non-zero: the server will construct
  `RFQickSoc216V1`; zero: plain `QickSoc`, no RF requests).

**Only one process may hold the board.** Stop the Pyro service (and any
Jupyter kernel holding a `QickSoc`) before starting this server, and never run
both: each would construct its own soc, reprogram the FPGA and drive the RF
cards. For a systemd Pyro unit: `systemctl stop qick-pyro` and
`systemctl disable qick-pyro` (or the unit's name on that box). The server's
lock file only stops a second board server; the Pyro server does not check it.

Every load of an RF image sets **all eight bias outputs to 0 V** (the library
constructor does it). Anything biased from them must tolerate that, or the
image's safe-state file must set the bias back (within its `limits`).

The RF code (`qick_lib/qick/boardserver/rf.py`, `LibraryRfHw`) has **not been
run on a board**. It follows the library (`rfboard.py`, `drivers/hardware.py`)
and, for the RF-ADC attenuator and the ADC flags, the engine side's
`board_side/qick_rf.py`, which they ran on such a box. Start the server with
the default `--soc-class auto` (it picks `RFQickSoc216V1` when the `.hwh` has
`rfb_control`); check in the log and in `welcome.capabilities.rf` that it did.
Do the following with no sample connected and a 50 ohm load or a spectrum
analyser on the RF In inputs, and record the results in `slab/NOTEBOOK.md`.

1. **The measurement that settles the load behaviour.** With an RF In
   amplifier switched on (`rf_set [{"ro": 0, "adc_att": 20}, {"ro": 0,
   "amp": true, "att": 10}]`), load an image with the procedure off
   (`--no-rf-safe-on-load`, or `skip_rf_safe: true`) and read
   `reset.rf.found_after_load` in the reply: it is read right after the load,
   before any RF call of the server, flags not cleared. It answers: is the
   amplifier still on (`adc_ports.<port>.amp_on`), is the RF-ADC attenuator
   0 dB (`adc_att`), are over-range / over-voltage flags set (`flags`)? Repeat
   with the procedure on: `before.done` must show every readout amplifier
   off, `found_after_load` must show no flag, `after.adc_att` 27 dB (or the
   safe-state file's values) and `after.flags_idle` clean. Note that
   `RFQickSoc216V1`'s constructor itself writes the amplifier switches off,
   so with a library soc `found_after_load` can show the amplifier off even
   if the window before `_rfb_config` over-drove the ADC; the flags
   (interrupts are latched until cleared, and the constructor clears them at
   its end) may not show it either. To see the window, read the flags with
   `xrfdc` from a second process during the load, or put a power meter on the
   RF In input; say which in the notebook.
2. **Readback matches.** For each RF In and DC In port: `amp_on` from the
   GPIO register is what was set (on, off), including after `adc_off`; the
   DC In `amp_on` is read from `PD` with the opposite sense. The RF-ADC
   attenuator read back equals what was set (0, 13, 27 dB). `filter`
   registers change when a filter is set and decode to the requested steps
   (`FilterADMV8818.set_filter_raw`); `filter_setting` decodes them to the
   requested type and steps (it uses a copy of the library's step table).
   `bias` setpoints read back. DC Out `powered` follows `{"gen": g, "dc":
   false/true}` (the power-down is `DacDcChain216.disable`, called directly).
3. **The DC In power-down default.** `SwitchControl.add_MCP` writes output
   i's default on pin 3-i, while `set_bits` uses pin i: on a DC In card the
   constructor's "powered down" default lands on pins 2/3, not on PD0/PD1.
   Read `adc_ports.<6|7>.amp_on` right after a load: true would confirm the
   DC In amplifiers are left on by the constructor. (Code reading; if
   confirmed this is an upstream bug report, to be raised by a person.)
4. **The ordering is safe.** With the amplifier on and the RF-ADC attenuator
   at 0 dB the engine side saw over-range at idle. Check on the box that the
   server's order never shows a flag: from a load (procedure on), send the
   working settings in the worst order (`amp` first, `adc_att` last) and
   check `flags_before_clear` and `flags_idle` of the reply and a power meter
   on the input; also lower `adc_att` with the amplifier on (applied last).
   Check that `rf_set` with an over-range setting gives `adc_flags`, and that
   over-voltage switches the amplifier off (`actions`).
5. **The settle time.** Whether 0.2 s after clearing is enough for a flag to
   reappear when the input is over range (`settle_s`), and how long a flag
   read takes.
6. **`adc_interrupt`.** A job on an ADC with a latched flag fails at
   `prepare_round` (`RFQickSoc216V1` raises `ADCInterruptError`); check that
   the job error says so and that clearing (`rf_set` with the working
   settings) lets the next job run.
7. **RF Out.** `{"gen": g, "att": [a1, a2]}` switches the card's 5 V supply on
   (both channels of a supply pair) and sets the attenuators; with a tone
   playing, check that the order (attenuation raised before lowered) gives
   no transient above the final level.

An optional per-image safe state (`put_rf_safe`, or `import-image --rf-safe
FILE` with `{"settings": [...], "settle_s": 0.2}`) is applied right after
each load of that image; without one the server sets every RF-ADC attenuator
to 27 dB and leaves the amplifiers off.
