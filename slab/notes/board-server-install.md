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
