# QICK board server: protocol, version 1

Start a fake board to develop a client against (no hardware, any PC):

    python -m qick.boardserver new-token /tmp/fake.token
    python -m qick.boardserver --fake --token-file /tmp/fake.token --image-dir /tmp/fake-images --port 8788

It creates four fake images and loads `fake-a`:

- `fake-a`: the QICK testbench configuration (one dynamic readout, accumulated
  ring 8192, decimated buffer 4096);
- `fake-b`: other `fw_timestamp`, accumulated ring 1024, decimated buffer 2048,
  and its load reports `clocks_reprogrammed: true`;
- `fake-rf`: `fake-a` on a QICK box with RF cards (RF Out on DAC slot 0, DC Out
  on slot 1, RF In on ADC slot 2 = ports 4, 5 = ADC blocks 20, 21, DC In on
  slot 3 = ports 6, 7 = blocks 22, 23; readout 0 on ADC 20 = RF In, readout 1
  on ADC 22 = DC In; generator 0 on DAC 00 = RF Out, generator 1 on DAC 10 =
  DC Out). Its fake cards
  model the bad case below (amplifier state survives a load, the RF-ADC
  attenuator returns to 0 dB, over range while an RF In amplifier is on with
  the RF-ADC attenuator under 10 dB);
- `fake-w`: `fake-a` with a weighted readout buffer as the lab firmware has
  it (tProc revision 29 with queue flags, `wgt_maxlen` 8192); `fake-w27`: the
  same as an upstream-style image (revision 27, no queue flags, 1024 weights).
  As in the firmware, both buffers are weighted: the decimated data is the fake
  trace times the loaded weights by the formula of
  `slab/sim/weighted_buffer/model.py`, and each accumulated value is exactly
  the 32-bit sum of its window's weighted samples.

Add `--allow-job-image-load` to try `run.image` loading;
`--insecure-fake` (only together with `--fake`) accepts any client without a token.
Then on the PC:

    from qick.boardserver.client import BoardClient
    bc = BoardClient('localhost', 8788, token_file='/tmp/fake.token', label='me/dev')
    soccfg = bc.get_config()
    job = bc.submit(MyAveragerProgramV2(soccfg, reps=100, final_delay=1.0, cfg={}), rounds=2)
    iq = job.result()            # what acquire(soc, rounds=2) returns

Status (2026-11-26): version 1 is implemented in `qick_lib/qick/boardserver/`
(branch `lab/board-server`) and tested only against the fake board
(`slab/tests/test_boardserver.py`). **It has not run on a board.** The real
backend (`QickSocBackend`) is written from the library code and is unverified;
see `board-server-install.md` for what must be measured on a loopback board
before it goes near a fridge. The second round (same day) added only fields
and requests, so clients of the first round keep working: `load_id` and
`previous` on loads, `loaded_name` in `image_mismatch`, `run.idle_after`, the
`image_changed` rule for jobs that name no image, the RF cards (`rf_get`,
`rf_set`, `put_rf_safe`, the procedure around `load_image`), and the fake
images `fake-rf` and `fake-w`. The RF code on the real path is unverified too.
A third round added: `capabilities` in every reply that follows a load
(`image_loaded`, `config`, `state`), structured ADC flags, `rf_clear_flags`,
decoded filters, per-image RF limits (`rf_limit`), DC Out power-down, and the
fakes made consistent with the firmware.

## Why

Today the PC drives the board's `QickSoc` object method by method through
Pyro4: pickle on the wire, a name server, an unmaintained library, the same
QICK version needed on both ends, and several round trips per program. The
replacement is one small server on the board that takes a whole job, runs it,
and returns arrays as raw bytes.

## Requirements

1. **Draining the firmware buffers must not depend on the network** (Dave).
   A board thread moves data from the firmware's ring buffers into a queue in
   the processor's memory at its own pace. The network sender reads from that
   queue. A slow or absent client makes the queue grow; it never stalls the
   drain.
2. **No silent loss.** The queue has a stated memory bound. Reaching it, or
   the firmware buffer being overwritten before the drain reaches it, is an
   explicit reported outcome on the job.
3. Runs on the board's own Python (3.8 on PYNQ 2.7, 3.10 on PYNQ 3.0) with the
   standard library and numpy only.
4. Token authentication. No pickle. No name server.
5. The board needs no experiment classes: it runs compiled programs.
6. Version mismatch is reported at connect, not discovered as a `KeyError`.
7. Not tied to one kind of payload: a later RISC-Q board should be able to
   speak the same framing with its own job type.
8. The server is the only holder of the `QickSoc`, and an exclusive lock file
   shows that the board is owned.

## How it is built

- `protocol.py`: framing, array packing, HMAC, error codes. Standard library
  only (numpy imported inside the array helpers). Shared by both ends.
- `server.py`: TCP and request handling. **Threads, not asyncio**: the
  hardware calls are blocking (MMIO, DMA waits, `QickSoc` construction) and
  the drain needs its own thread anyway; with threads every part is a plain
  blocking loop. Per connection one reader thread and one sender thread (the
  only writer on that socket).
- `jobs.py`: the queue, the runner thread, chunk stores, the bound, retention.
  The runner thread *is* the drain: it iterates the backend's `run()`
  generator and puts chunks into the job's store; it never touches a socket.
- `backend.py`: the seam to the hardware. `QickSocBackend` (real, untested on
  hardware) and `FakeBackend` (the same class over `FakeSoc`, a simulated
  `QickSoc`: shot counter at a set rate, ring buffers that really get
  overwritten when the drain falls behind, injectable errors).
- `images.py`: the image store. `client.py`: blocking client.
- The drain loop itself is `qick.streamer.iter_accumulated()`, which
  `DataStreamer` now also uses. It fixes the two gaps the sketch noted:
  the queue it feeds is bounded (the job store), and ring overwrite is
  detected (below).

## Framing

One TCP connection. Every message is a frame:

    4 bytes  length of the header (unsigned, little-endian)
    4 bytes  length of the body (unsigned, little-endian)
    header   UTF-8 JSON object: {"type": ..., "id": ..., ...}
    body     raw bytes (may be empty); arrays are described in the header's
             "arrays" list as {"name", "dtype", "shape", "offset", "nbytes"}
             and laid end to end

All lengths and all array data are little-endian (dtype strings like `<i4`,
`<f8`; big-endian input is converted before sending). Allowed dtype kinds:
bool, int, uint, float, complex. A header is at most 1 MiB, a body at most
64 MiB; a frame announcing more is refused with `frame_too_large` before the
oversize part is read, and the connection is closed. A peer that stops in the
middle of a frame for longer than the frame timeout (60 s) is disconnected.
Unknown header fields are ignored; an unknown message type is answered with
`error` code `unknown_type` and the connection stays open.

`welcome.firmware.image` (also `config.image`, `state.image`, `status.image`,
`list_images.current_image`) is null before any load, else exactly
{`state` (`loaded`, `failed` or `unknown`), `name`, `hash`, `bit_sha256`,
`hwh_sha256`, `detail` (only for `unknown`: why), `load_id`}; keys that do
not apply are null. The full record is `current_image`.

Requests carry a client-chosen `id`; the direct reply carries `reply_to` with
that id. Pushed messages (`chunk`, `event`, `done`) have no `reply_to`.

**Program dictionaries** (`dump_prog()`) travel as JSON in the header with
every numpy array (envelopes, readout weights) replaced by
`{"$array": "a<n>"}` and the arrays in the body. The content hash
(`prog_hash`) is defined by `protocol.tree_hash` as SHA-256 over, in order:

1. `json.dumps(tree, sort_keys=True, separators=(',', ':'))` encoded as UTF-8,
   where `tree` is the header's `prog` exactly as received (Python's own JSON
   number formatting);
2. for each entry of the header's `arrays` list, in that order: the ASCII text
   `'%s|%s|%s|' % (name, dtype, list(shape))` (e.g. `a0|<i2|[400, 2]|`), then
   the array's little-endian bytes.

The hash therefore depends on the array names and their order as the client
chose them, and on Python's float formatting. **A client should not compute
it**: send the program once (`put_prog`, or `submit` inline) and use the hash
the server returns (`prog_stored.hash`, `accepted.prog_hash`) as `prog_ref`.
The Python client computes it only to skip a `put_prog` it has already done
on this connection, and falls back to inline on `unknown_program`.

## Authentication

1. Client sends `hello` {`protocol`, `client_version`, `label`, `role`
   (`run` or `read`)}. The board replies `challenge` {`nonce`} (32 random
   bytes, hex).
2. Client sends `auth` {`hmac`}, the hex HMAC-SHA256 with the token as key of
   `"qick-boardserver-auth/1" \0 nonce \0 role \0 label`. The token never
   crosses the wire. The board compares with `hmac.compare_digest` against each
   token allowed for the role.
3. On success: `welcome`. On failure: `error` `auth_failed` after a delay,
   and the connection is closed.

Any other request before a successful `auth` gets `not_authenticated` and the
connection is closed. Roles: a `run` token may do everything; a `read` token
authenticates only for role `read`, which allows `get_config`, `get_state`,
`status`, `subscribe`, `unsubscribe`, `list_images`, `current_image`, `rf_get`,
`bye` (anything else: `forbidden`).

The token file (path from `--token-file` or the config file's `token_file`,
never the token itself on the command line or in the environment) is read at
start; tokens are not logged. The server refuses to start without one unless
`--fake --insecure-fake`. Its format, exactly (`protocol.read_token_file`):
each line is stripped of surrounding whitespace; empty lines and lines starting
with `#` are skipped; a line with one whitespace-separated field is a `run`
token; a line with two fields is `<run|read> <token>`; anything else is an
error. The HMAC key is the token field encoded as UTF-8 bytes. Note that
`new-token` writes the two-field form (`run <token>`), so a client must not
use the whole file content as the key; for a file holding one bare token
the key is the file content stripped of surrounding whitespace.

## Messages

Client to board:

| Type | Purpose | Main fields | Reply |
|---|---|---|---|
| `hello` | start | `protocol`, `client_version`, `label`, `role` | `challenge` |
| `auth` | answer the challenge | `hmac` | `welcome` |
| `get_config` | board configuration | | `config` {`soccfg`, `image`} |
| `get_state` | board state not in the program | | `state` {`state`} |
| `status` | one job (`job`), or the board | `job` optional | `status` |
| `put_prog` | store a program in the cache | `prog`, `arrays` + body | `prog_stored` {`hash`} |
| `submit` | queue a job | see below | `accepted` |
| `fetch` | become the owner of a job and receive from a point | `job`, `from_seq`, `from_event` | `fetching` {`job`, `state` (`queued`/`running`/`done`), `next_seq` (seq the next chunk will get), `first_retained` (lowest seq still held, or `next_seq` if none), `events` (number of events so far)} |
| `ack` | chunks up to `seq` are stored by the client; the board may free them | `job`, `seq` | none (errors only) |
| `subscribe` | watch a job without owning it | `job`, `newest_only` (default true), `from_now` (default true) | `subscribed` |
| `unsubscribe` | stop watching | `job` | `unsubscribed` |
| `cancel` | stop a queued or running job (any `run` client; logged) | `job` | `cancelling` {`job`, `was`: the job's state before (`queued`: finished now; `running`: stop requested, `done` follows; `done`: nothing happened)} |
| `list_images` | the image store | | `images` {`images`, `current`, `current_image`} |
| `put_image` | upload an image in chunks | `phase` = `begin` / `data` / `commit` / `abort` (below) | `upload` or `image_stored` |
| `load_image` | program the FPGA with a store image | `image` (name or hash), `force`, `skip_rf_safe` | `image_loaded` |
| `current_image` | the record of the last load | | `current_image` {`image`} |
| `rf_get` | everything readable about the RF cards | | `rf` {`rf`, `image`} |
| `rf_set` | apply RF settings in the safe order (`run` role, logged) | `settings`, `settle_s` | `rf_applied` |
| `put_rf_safe` | store (or with `settings` and `limits` null remove) an image's RF safe state and limits | `image`, `settings`, `limits`, `settle_s` | `rf_safe_stored` |
| `rf_clear_flags` | read and clear the ADC flags only (`run` role, logged) | `settle_s` (default 0) | `rf_flags_cleared` {`flags_before`, `flags_after`, `settle_s`, `by`} |
| `bye` | close | | `bye` |

Board to client:

| Type | When | Main fields |
|---|---|---|
| `challenge` | reply to `hello` | `nonce`, `protocol` |
| `welcome` | reply to a correct `auth` | `protocol`, `qick_version`, `server_code_hash` (SHA-256 of the server's own modules), `session`, `role`, `insecure`, `fake`, `firmware` {`image`, `fw_timestamp`, `hwh_sha256`}, `board`, `python`, `capabilities` {`job_kinds`, `modes`, `reduce` (false), `image_store`, `allow_job_image_load`, `time_sync_period`, `queue_flags`, `tproc_revision`, `weights` (readout indices with a weighted buffer), `rf` (the loaded image has RF cards), `rf_safe_on_load`, `rf_limits` (the loaded image has a limits table), `idle_after` (true: `run.idle_after` understood)} (the same `capabilities` are in `image_loaded`, `config` and `state`, since they change with the image), `limits` {`max_header`, `max_body`, `max_queue_bytes`, `max_retention_s`}, `wall_time`, `monotonic` |
| `accepted` | reply to `submit` | `job`, `ref`, `position`, `run` and `limits` in effect, `layout`, `prog_hash`, `image_load_first` (hash, if the job will load an image) |
| `chunk` | pushed | `job`, `seq`, `round`, `buffer` (`acc` or `dec`), `shots` [first, last), `arrays`, `queued_bytes` |
| `event` | pushed | `job`, `eseq`, `kind`, `t`, and per kind (below) |
| `done` | pushed after the last chunk | `job`, `ref`, `outcome`, `reason`, `error`, `totals`, `outputs_idle`, `outputs_idle_reason`, `idle`, `times`, `subscriber` |
| `error` | a refused request | `reply_to`, `code`, `message`, `request_type`, and fields per code |

### submit

    {"type": "submit", "kind": "qick_prog",
     "prog_ref": "<hash>"  or  "prog": {...}, "arrays": [...] (+ body),
     "run": {"rounds": 1, "mode": "acc" | "dec", "start_src": "internal" | "external",
             "stride": null, "time_limit_s": null, "image": null, "reduce": null,
             "idle_after": true},
     "limits": {"max_queue_bytes": <= server bound, "policy": "stop" | "drop_oldest",
                "retention_s": 3600},
     "ref": "<client's own reference>"}

- `mode` `acc` is `acquire()`: every shot of every readout from the
  accumulated buffer, streamed while the program runs. `dec` is
  `acquire_decimated()`: per round, the whole decimated trace and the
  accumulated values, in one chunk.
- `stride`: shots per chunk at least (default 10% of the smallest ring, as
  `DataStreamer`); a chunk can be larger when the counter has moved further,
  up to what one transfer may read.
- `prog_ref` of a program not in the cache gives `unknown_program`; the client
  then sends it inline (the client library does this by itself). The cache is
  cleared by every image load.
- `run.fake` (object: `shot_rate`, `read_delay_s`, `queue_error`
  {`round`, `queues`}, `exception_at_shot`) is accepted only by the fake board.
- `run.reduce` is **reserved**: anything but null/`"none"` is refused with
  `unsupported`. Version 1 returns every shot.
- `run.idle_after` (default true): with false, the outputs are **not** made
  idle when the program ran to its end (outcome `complete`, or `incomplete`
  through `overrun`/`dropped`): no `stop_tproc`/`reset_gens`, the generators
  keep what the program left. They are still made idle on an error, a cancel,
  a time limit and a queue-bound stop. `done.outputs_idle` says whether they
  were, `done.outputs_idle_reason` why or why not (`normal end of the job`,
  `error (<code>)`, `cancelled (client)`, `timed_out (time_limit)`,
  `incomplete (queue_bound)`, or the `idle_after` text), and `done.idle` has
  `skipped: true` when they were left.
- **Always send `run.image`** (name or hash of the image the program was
  built for). It is the only thing that protects a queued job from running on
  another image: programs are not rebuilt or re-checked against a new
  configuration. A job that names no image and reaches the head of the queue
  after the image changed is not run: `done` outcome `failed`, code
  `image_changed`. When the change is a `run.image` load by another job, such
  jobs are cancelled at that load (reason `image_changed`) and listed in that
  job's `image_loaded` event as `cancelled_jobs`. An explicit `load_image`
  already refuses (or with `force` cancels) every queued job.

`layout` (in `accepted`, or in the `started` event when the job loads an
image first): `loop_dims`, `avg_level`, `counter_addr`, `shots_per_round`,
`rounds`, `mode`, `readouts` [{`index`, `ch`, `reads_per_shot`, `length`}],
`arrays` (names `acc:<i>`, `dec:<i>`, dtype `<i4`). Array `acc:i` in a chunk
has shape ((last-first) x reads_per_shot, 2): readout i, shot by shot in time
order (shot s is C-order over `loop_dims`), reads within a shot in order. So
`acc_buf[i].reshape(-1, 2)[first*n:last*n] = chunk` rebuilds `get_raw()`.
`Job.result()` does this and then calls the program's own
`_process_accumulated`/`_summarize_accumulated` (or the decimated ones), so it
returns what `acquire()` returns, thresholding included.

### Events

| `kind` | Fields |
|---|---|
| `image_loaded` | `image`: the load record (the job named another image and the server is allowed to load it), with `cancelled_jobs`: queued jobs that named no image, cancelled with reason `image_changed` |
| `started` | `image` (hash), `layout` |
| `overrun` | `round`, `shots` [a, b): shots overwritten in the firmware ring before the drain read them (not delivered), `info` {`when` (`before_read`/`during_read`), `counter`, ...} |
| `dropped` | `chunks` [{`seq`, `round`, `shots`, `sent`}], `seqs`: freed because the queue bound was reached under `drop_oldest`; `sent` says whether the chunk had already been handed to the owner's socket |
| `stopped` | `reason` (`queue_bound`, `cancel`, `image_load`, `time_limit`); for `queue_bound`: `round`, `from_shot`, `next_seq`, `queued_bytes`, `bound` |
| `error` | `code`, `message`, `traceback`, and `queues` for `tproc_queue_overflow` |

Events are numbered `eseq` from 0 per job and are delivered in the order they
happened relative to the chunks.

### done

`outcome`: `complete`, `incomplete`, `cancelled`, `failed`, `timed_out`.
`reason`: for `incomplete` one of `overrun`, `dropped`, `overrun_and_dropped`,
`queue_bound`; for `cancelled` `client`, `image_load`, `image_changed` or
`server_shutdown`;
for `failed` the error code. `error` {`code`, `message`, `traceback`, ...} for
`failed`. `totals` {`chunks`, `shots_stored`, `dropped` (seq ranges),
`overruns`, `events`, `bound_stop`}. `outputs_idle`, `outputs_idle_reason`
and `idle`: whether the board stopped the tProc and ran `reset_gens()` after
the job, and why (what the board commanded, not a measurement at the DAC; see
`run.idle_after`). `times` (board monotonic seconds
since server start): `received`, `queued`, `started`, `loaded` (program and
configuration written), `finished`, `drained` (last chunk stored), `sent`
(owner got `done`).

### Images

- `put_image` `begin` {`name`, `bit_sha256`, `hwh_sha256`, `bit_size`,
  `hwh_size`, `replace`, `note`} -> `upload` {`upload`, `max_chunk`};
  `data` {`upload`, `file` (`bit`/`hwh`), `offset`} + body, in order ->
  `upload` {`written`}; `commit` {`upload`} -> `image_stored` {`image`}, or
  `hash_mismatch` (upload discarded); `abort`. An upload whose connection
  closes is discarded. Same name and same content: returns the existing entry;
  same name, other content: `name_exists` unless `replace`.
- An image's `hash` is SHA-256 of (bit SHA-256 hex + hwh SHA-256 hex);
  `image` arguments accept a name or a hash prefix of 8+ hex digits.
- `load_image` {`image`, `force`, `skip_rf_safe`}: refused with `job_running`
  while a job runs (even with `force`: cancel it first); refused with
  `jobs_queued` while jobs are queued, listing each {`job`, `submitter`, `ref`,
  `state`, `image`}, unless `force`, which cancels them: each gets a `done`
  with outcome `cancelled`, reason `image_load`, and the reply lists them.
  Reply `image_loaded`: `load_id` (unique per load, e.g. `L3-9f2c01ab`; also in
  the server log lines of that load, in `current_image` and in
  `welcome.firmware.image`), `previous` (null, or {`state`, `name`, `hash`,
  `bit_sha256`, `hwh_sha256`, `load_id`, `assumed`} of the image loaded
  before), `name`, `hash`, `bit_sha256`, `hwh_sha256`,
  `soccfg` (new), `duration_s` (wall-clock time of constructing `QickSoc`),
  `requested_by` (label), `reason`, `t` (board monotonic time), `wall_time`,
  `reset` {`pl_reconfigured` (true), `clocks_reprogrammed` (whether
  `QickSoc` re-initialised the clock chips: it does when the RF PLLs are not
  locked or clock options are given), `converters_restarted`,
  `tproc_memories_cleared`, `envelopes_cleared`, `program_cache_cleared`,
  `rf` (null without RF cards before or after; else the RF record, below)},
  `cancelled_jobs` [{`job`, `submitter`, `ref`}]. Every load is logged with
  who asked and its `load_id`. After a load the program cache is empty.
  Queued jobs are **not** recompiled: see `run.image`.
- `current_image`: the same record for the last load; `{"state": "unknown",
  "detail": "loaded before server start", "assumed": {...}}` if the server
  attached to an already configured board (`--attach`); `{"state": "failed",
  ...}` after a failed load; `{"state": "none"}` if nothing was loaded.
- `run.image` (name or hash): if it is not the loaded image, the job is refused
  at `submit` with `image_mismatch` {`wanted`, `wanted_name`, `loaded`,
  `loaded_name`},
  unless the server runs with `allow_job_image_load` (default off); then the
  job loads it when it reaches the head of the queue (event `image_loaded`).

### state

`get_state` returns `image`, `loading` (a load in progress), `outputs_idle_at`,
`last_idle`, `monotonic`, `board`, `fw_timestamp`, `clocks_locked`, and
`tproc` {`type`, `revision`, `status` (decoded TPROC_STATUS), `axi_regs`
(every AXI register), `debug` (what `print_debug` shows), `queue_flags`
{`supported`, `lost`}} on tProc v2, and `rf` (the `rf_get` record, null
without RF cards). DAC settings, mixer/NCO, reference lock detail and
temperatures are listed under `not_implemented` (and the ADC attenuators on
images without RF cards).

`status` without a job: `clients` (labels, roles, sessions), `running`,
`queue` (each job's submitter and `ref`), `recent`, `uptime_s`, `memory`
{`queued_bytes`, `bound`}, `last_error`, `outputs_idle_at`, `image`, `loading`.
With `job`: that job's full record.

### Decimated results and weights

`Job.result()` for `mode: dec` returns exactly what the library's
`acquire_decimated(soc, rounds=R)` returns: per round it passes the round's
`dec:i` arrays to the program's `_process_decimated`, then
`_summarize_decimated` averages the rounds (float). Per readout: (length, 2)
for one shot with one read; (n_shots x n_reads, length, 2) for several shots
or several reads; (n_shots, n_reads, length, 2) for both, where n_shots is the
product of `loop_dims`. The test
`test_decimated_matches_library_acquire_decimated` runs the library's own
`acquire_decimated` on a FakeSoc with the same data and compares the arrays
(shape, dtype, values) for all three shapes.

Readout weights (`declare_readout(..., weights=w)`, int16 (length, 2)) travel
in the program dictionary like envelopes and reach `soc.load_weights` intact
(test `test_weights_round_trip` on `fake-w`). On the fakes the accumulated
value of a weighted readout equals the sum of its window's decimated samples
(`test_weighted_accumulated_is_sum_of_window`, on `fake-w` and `fake-w27`).

### RF cards (QICK box daughter cards, ZCU216)

The first real board will likely be a ZCU216 QICK box with RF cards
(firmware `qick_tprocv2_216_rfbv2`). The real backend constructs
`qick.rfboard.RFQickSoc216V1` instead of `QickSoc` when the image's `.hwh`
contains the RF-board control block `rfb_control` (`--soc-class auto`, the
default; `QickSoc` or `RFQickSoc216V1` force it). The RF logic is in
`qick_lib/qick/boardserver/rf.py`, over a hardware adapter
(`LibraryRfHw` for the library, `FakeRfHw` for the fake). **The real adapter
has not been run on a board**; the RF-ADC attenuator and flag helpers follow
the engine side's `board_side/qick_rf.py` (qubit-measurement repository),
which they ran on such a box.

What can be read (from the library and the converter), and what cannot:

| Part | Readback |
|---|---|
| RF-ADC step attenuator (DSA, 0-27 dB, 1 dB) | read from the converter (`get_adc_attenuator`); not part of the `rfb_*` methods |
| ADC over-range / over-voltage | the converter's interrupt word (`XRFdc_GetIntrStatus`, the bits `clear_interrupts` decodes); not part of `rfb_*` |
| RF In amplifier, DC In power-down | card GPIO register (MCP23S08) |
| DC In gain (LMH6401) | read back |
| Filters (ADMV8818) | `WR0_SW`, `WR0_FILTER` registers read back (raw) and decoded (`filter_setting`) |
| DC Out power-down | card GPIO register |
| Bias outputs (DAC11001) | setpoint read back |
| Step attenuators on RF Out (2 per channel) and RF In (1) (PE43705) | **write-only**: reported as {`value`, `by`} with `by` = `constructor`, `rf_set` or `amplifier off` |
| RF Out 5 V supply | not read; reported as last written |

`rf_get` -> `rf` {`cards` {`adc`, `dac`: per slot null or {`kind`
(`rf_in`, `dc_in`, `balun_in`, `rf_out`, `dc_out`, `balun_out`), `name`,
`ports`}}, `gens`/`ros` (per soccfg index: `dac`/`adc` block, `tile`,
`block`, `port`, `slot`, `kind`; a readout's tile and block are taken from
`rf.adcs[<name>].index`, the converter's own numbering, and from the name's
digits only when the index is missing), `adc_ports` (per ADC port on a card:
`amp_on`, `att`, `filter`, `filter_setting`, `gain`), `dac_ports` (per DAC
port: `att`, `supply_on`, `filter`, `filter_setting`; DC Out: `powered`),
`adc_att` (per RF-ADC block, from the converter), `bias` (8 setpoints),
`flags` (per RF-ADC block {`word`, `word_hex`, `over_range`, `over_voltage`,
`other`}, read without clearing), `flags_list` (the same as a list sorted by
block, each with `block`), `readback` (which fields are read and which are
last written)}. `get_state.rf` is the same record.

**ADC flags as data.** Everywhere a reply has a flag *list* its entries are
{`block`, `over_range`, `over_voltage`, `word`, `word_hex`, `other`} (or
{`block`, `error`}). The older per-block dicts (`flags`, `flags_idle`,
`flags_before_clear`) stay as they were.

`filter_setting`: decoded from the registers with the library's ADMV8818 step
table (`FilterADMV8818.freq2steps`, copied into `rf.py`): {`source`:
`decoded from the registers`, `ftype` (bypass when both sections are
bypassed, lowpass / highpass when one is, bandpass when both are), `fc`, `bw`
(GHz; bandpass: centre and width between the two cutoffs, as `set_filter`
computes them from fc ± bw/2), `hpf`, `lpf` (steps, -1 bypass), `hpf_ghz`,
`lpf_ghz`, `last_request`}. If the registers do not hold a setting
`set_filter_raw` can write: {`source`: `as written`, `request`} (the last
request since the load) or {`source`: `unknown`}. The decoded cutoffs are
the table's nominal values, not a measured response.

`rf_set` {`settings`: [...], `settle_s` (default 0.2, at most 10)}. Each
setting names one target and what to set on it:

    {"ro": 0, "adc_att": 20}                     RF-ADC attenuator of readout 0's ADC
    {"adc": "21", "adc_att": 27}                 by ADC block name
    {"ro": 0, "filter": {"ftype": "bandpass", "fc": 6.0, "bw": 1.0}}   GHz; or {"hpf": n, "lpf": m}
    {"ro": 0, "amp": true, "att": 10}            RF In: amplifier on, card attenuator 10 dB
    {"ro": 0, "amp": false}                      RF In amplifier off (attenuator 31.75), DC In powered down
    {"ro": 1, "amp": true, "gain": 10}           DC In: powered up, gain (-6..26 dB)
    {"gen": 0, "att": [6, 8]}                    RF Out: both attenuators (and its supply on)
    {"gen": 0, "filter": {...}}
    {"gen": 1, "dc": false}                      DC Out: powered down (true: powered up, rfb_set_gen_dc)
    {"bias": 3, "volts": 0.5}

Several keys may share one item (`{"ro": 0, "adc_att": 20, "amp": true,
"att": 10}`). Everything is checked against the loaded image before anything
is applied (`bad_request` with `field`, or `no_rf_board` for a channel not on
a card of the needed kind). The RF In attenuator can only be written together
with switching the amplifier on (the library has no separate call). Refused
for the `read` role (`forbidden`), while another client's job runs
(`job_running`; the caller's own running job does not block it), during a
load (`board_loading`). Logged with the label. DC Out: the library powers a
channel up with `rfb_set_gen_dc`/`rfb_set_dac_dc` but has no `rfb_*` call to
power it down, so the server calls `DacDcChain216.disable()` (PD = 1)
directly; the state is read back from the card GPIO.

**Limits.** An image's safe-state file may carry a `limits` table
(`put_rf_safe` `limits`, or `import-image --rf-safe` with `limits`), checked
on **every** `rf_set` from any client, before anything is applied, and on the
safe-state settings themselves when they are stored:

    "limits": {"bias":        {"3": [-1.0, 1.0]},  V per output; with this section an output
                                                   not listed may only be set to 0 V
               "adc_att_min": {"20": 10},          RF-ADC attenuator floor per ADC block, dB
               "ro_att_min":  {"0": 3},            RF In card attenuator floor per readout, dB
               "gen_att_min": {"0": 6},            RF Out attenuator floor per generator (dB, or [dB1, dB2])
               "dc_gain_max": {"1": 12}}           DC In gain ceiling per readout, dB

A setting outside it is refused with `rf_limit` {`output` (e.g. `bias 3`,
`adc 20`, `ro 0`, `gen 0`), `limit`, `value`}; nothing is applied. Without a
table only the hardware ranges are checked.

The server applies the settings in this order, whatever order they were
given in (each step's phase, op, target, value and readback are in the
reply's `order`):

1. **reduce**: RF-ADC attenuator raised; readout amplifiers off; DC In gain
   lowered; RF In attenuator raised with the amplifier already on; RF Out
   attenuation raised (both stages not lower than last written);
2. **filters**: readout side, then generator side;
3. **increase**: RF Out attenuation lowered, then RF In amplifier on (or its
   attenuator lowered), then DC In gain raised, then RF-ADC attenuator lowered
   last;
4. **bias** outputs;
5. **flags**: every ADC's flags are cleared (the words read while clearing are
   `flags_before_clear`: latched during the change), the server waits
   `settle_s`, reads them again and clears them (`flags_idle`, and as a list
   `flags_after_change`).

Before step 1 (after the settings have been checked) the server reads and
clears every ADC's flags: `flags_latched_before`. So a flag in
`flags_latched_before` was there before this request; a flag only in
`flags_after_change` came with it.

So readout-side attenuation is always raised before gain is added, and
output-reducing changes come before output-increasing ones. In the case the
engine side measured (RF-ADC attenuator at 0 dB after a load) the attenuator
is raised first; when the attenuator is already high (after this server's
own safe state, 27 dB) lowering it to the working value is a gain increase and
comes after the amplifier is on.

Reply `rf_applied` {`order`, `flags_latched_before`, `flags_before_clear`,
`flags_idle`, `flags_after_change`, `flagged` (the entries of
`flags_after_change` with a flag set or an error), `settle_s`, `problems` [],
`actions` [], `ok` true, `by`}. If any ADC is over range or over voltage at
idle the reply is `error` `adc_flags` (one code; the message has the text)
with `flags` (= `flags_after_change`), `flagged`, `flags_latched_before` and
the whole `record`: on **over-voltage** the card amplifier of that ADC is switched off
again (listed in `actions` with its readback); on over-range it is left as
set, for a person to look. A step that raises gives `rf_failed` with the
record up to that step. With an interrupt still set, the next job fails at
`prepare_round` with `adc_interrupt` (that is `RFQickSoc216V1`'s own check).

`rf_clear_flags` {`settle_s`} reads and clears every ADC's flags
(`flags_before`), waits `settle_s`, and reads them again without clearing
(`flags_after`: a persisting condition is back). Same refusals as `rf_set`.

#### Around `load_image`

The bad case designed for: the cards keep their amplifier state across a PL
reload while the RF-ADC attenuator returns to 0 dB, which over-drives the ADC.
From the library code there is a window inside `RFQickSoc216V1.__init__`:
the bitstream is downloaded (attenuator to 0 dB) before `_rfb_config` writes
the card GPIO defaults (amplifiers off), and an amplifier left on by the
previous image is on during that window. So, **on by default** whenever RF
cards are present (`--no-rf-safe-on-load` turns it off):

1. **Before** reprogramming, if the current image has RF cards: every
   readout-side amplifier (RF In and DC In) is switched off and read back. If
   a switch cannot be confirmed off the load is refused with
   `rf_safe_failed` and nothing is loaded, unless the request has
   `skip_rf_safe: true` (logged as a warning).
2. The load.
3. **After**, before replying, if the new image has RF cards: the cards are
   read as found (`found_after_load`, flags not cleared: the measurement of
   the real behaviour); every readout amplifier is switched off (unless the
   image's safe-state file turns it on) and read back; then the safe state is
   applied through the `rf_set` order: every RF-ADC attenuator to the file's
   value or 27 dB (the maximum), the file's filters and card attenuators,
   amplifiers only if the file turns them on; then the flags (cleared, wait,
   read); the RF-ADC attenuators are read back from the converter and compared.

The safe-state file is per image: `put_rf_safe` {`image`, `settings` (the
`rf_set` list, may be empty), `limits`, `settle_s`} or `import-image --rf-safe
FILE`; stored as
`rf_safe.json` beside the image (`list_images` shows `has_rf_safe`). A file
that does not check is ignored (built-in safe state used, `safe_file_problem`
in the record, logged).

`reset.rf` in the `image_loaded` reply: {`procedure` (`on`/`off`), `before`
(null; {`done` [{`port`, `kind`, `amp_on`}], `flags`, `flags_list`, `ok`};
or {`skipped`, `why`}), `constructor` (what the soc constructor does to the
cards, below, with `forced`: the same as data),
`found_after_load` (the `rf_get` record read right after the load, before any
RF call of the server), `after` (null, or {`source` (`built-in default` or
`safe-state file`), `amplifiers_off`, `order`, `flags_latched_before`,
`flags_before_clear`, `flags_idle`, `flags_after_change`, `flagged`,
`settle_s`, `adc_att` (read back), `problems`, `actions`, `ok`}), `state` (the `rf_get` record after the procedure, without flags)}.
`after.ok` false is logged as an error but does not undo the load.

What `RFQickSoc216V1`'s constructor does to the cards (read from
`rfboard.py`/`hardware.py`, not measured; `reset.rf.constructor` carries it):
RF Out: both attenuators of every channel written 31.75 dB, 5 V supplies
written off; RF In: attenuator 31.75 dB, amplifiers written off; DC In: gain
-6 dB, and the GPIO default meant to power the amplifiers down **does not
reach pins 0/1** (`SwitchControl.add_MCP` puts output i's default on pin
3-i while `set_bits` uses pin i), so the DC In amplifiers may be left powered
up; DC Out: powered up; filters: only `ADI_SPI_CONFIG_A` written, the
band/state registers keep what they held; the RF-ADC attenuator is not
written (0 dB after a load, as measured by the engine side); all ADC tiles
restarted and their interrupts cleared.

**Every load sets all eight bias outputs to 0 V.** `_rfb_config` constructs a
`BiasDAC11001` per output, whose constructor writes `CONFIG1_REG` = 0x4e00
and then `set_volt(0)`; after that `rfb_enable_bias()` switches the bias
outputs on. So whatever a bias output held (a flux bias, say) is 0 V after any
load of an RF image, and the outputs are enabled. `reset.rf.constructor.forced`
lists what the constructor forces: `bias_volts` [0 x 8], `bias_enabled` true,
`rf_out_att_db` [31.75, 31.75], `rf_in_att_db` 31.75, `rf_out_supply_on`
false, `rf_in_amp_on` false, `dc_out_powered` true, `dc_in_gain_db` -6,
`dc_in_powered` (intended false, possibly true: the pin bug above); filters
and the RF-ADC attenuator are not forced. A safe-state file can set the bias
back (`{"bias": n, "volts": v}`, within its limits) right after the load.


## Error codes

Every refusal is an `error` with a stable `code` and a human `message`.

| Code | Meaning | Extra fields |
|---|---|---|
| `bad_frame` | frame could not be parsed (connection closed) | |
| `frame_too_large` | header or body above the cap (connection closed) | |
| `not_authenticated` | request before a successful `auth` (connection closed) | |
| `auth_failed` | HMAC did not match a token for the role (connection closed) | |
| `unsupported_protocol` | protocol version not spoken | `supported` |
| `unknown_type` | message type not known | |
| `bad_request` | a field missing or invalid | `field` |
| `forbidden` | the role does not allow this request | |
| `no_image` | no firmware image loaded | |
| `unknown_job` | no such job | `job` |
| `not_owner` | only the owning connection may `ack` | |
| `unknown_program` | `prog_ref` not in the cache | `prog_ref` |
| `program_format` | the program dictionary could not be loaded | `field` |
| `limit_exceeded` | a board limit | `limit` |
| `seq_freed` | `fetch` from chunks already freed | `first_retained` |
| `unsupported` | reserved, not in version 1 (`run.reduce`, other job kinds) | `field` |
| `unknown_image` | no image with that name or hash | `image` |
| `hash_mismatch` | uploaded file's SHA-256 differs from the announced one | `files`, `expected`, `received` |
| `name_exists` | image name taken by other content | `existing_hash` |
| `upload_unknown` | no such upload on this connection | |
| `job_running` | `load_image` while a job runs (even with `force`); `rf_set` while another client's job runs | `job`, `submitter`, `ref` |
| `jobs_queued` | `load_image` without `force` while jobs are queued | `jobs` [{`job`, `submitter`, `ref`, `state`, `image`}] |
| `image_mismatch` | job names an image other than the loaded one | `wanted`, `wanted_name`, `loaded`, `loaded_name` |
| `image_load_failed` | constructing `QickSoc` from the image raised | `traceback`, `image`, `load_id`, `previous` |
| `board_loading` | an image load (or, for `rf_set`/`load_image`, another RF change) is in progress | |
| `no_rf_board` | `rf_get`/`rf_set` on an image without RF cards, or a setting for a channel not on a card of the needed kind | `field` |
| `rf_safe_failed` | `load_image` refused before reprogramming: a readout amplifier could not be confirmed off; nothing loaded | `record`, `load_id`, `image` |
| `adc_flags` | `rf_set` applied, but an ADC is over range / over voltage at idle | `flags`, `flagged`, `flags_latched_before`, `record` |
| `rf_failed` | an `rf_set` step raised; the later steps were not applied | `record` |
| `rf_limit` | an `rf_set` (or safe-state) setting is outside the image's limits table; nothing applied | `output`, `limit`, `value` |
| `internal` | unexpected server error | `traceback` |

In `done.error.code` only: `job_exception` (any Python exception in the job;
`exc_type`, `traceback`), `image_changed` (see `run.image`), `adc_interrupt`
(`RFQickSoc216V1.prepare_round` found an ADC interrupt set, i.e. an ADC over
range, before the round; `exc_type` `ADCInterruptError`) and
`tproc_queue_overflow` (the lab firmware's
lost-push flags were set after a round; `queues` lists `trigger`, `data`,
`wave`; same check as `soc.check_tproc_queues()` in `finish_round`). A job can
also fail with `image_mismatch`, `image_load_failed`, `no_image`,
`program_format` or `limit_exceeded` when it reaches the head of the queue.

## Behaviour

- **Chunks are numbered** from 0 per job and state their round and shots, so a
  gap is visible to the client (the client library records `Job.gaps`).
- **The drain.** The runner thread follows the tProc shot counter and reads
  new shots from each readout's ring (as `DataStreamer` did, 0.5 ms sleep
  between empty polls). The chunk goes into the job's store in memory. Senders
  read the stores; the drain never waits for a socket.
- **Firmware overrun** is checked against the counter before each transfer
  and again after it (the conservative side: a shot is counted as overwritten
  if the counter, plus the shot in progress, has lapped it by the time the
  transfer finished). Overwritten shots are not delivered; they are reported in
  an `overrun` event with the shot range, the drain skips to the oldest intact
  shot, and the outcome becomes `incomplete`. Tested: on the fake ring every
  shot is delivered correctly or reported, exactly once.
- **The bound.** Retained chunk bytes are bounded per job (`limits`) and
  server-wide (`--max-queue-mb`, default 256 MiB; retained chunks of finished
  jobs count too). `stop` (default): the run is stopped at the chunk that does
  not fit; the event says from which round, shot and seq; nothing past it is
  produced. `drop_oldest`: the oldest retained chunks are freed and listed.
- **Results outlive the connection.** A job keeps running and keeps its
  chunks if its client disconnects. `fetch` {`from_seq`, `from_event`} on a
  new connection makes that connection the owner and resumes; the client
  library's `fetch(job)` continues the same `Job` object with no gap. Chunks
  are freed on `ack` (the client acks each chunk when it has stored it), or
  `retention_s` after the job finished.
- **One job at a time**; jobs queue behind it and start as soon as the
  previous one has finished and the outputs have been made idle.
- **Chunks are pushed** on the owner's connection; `done` follows the last.
- **Subscribers** get the job's events and chunks on their own connection;
  with `newest_only` only the newest retained chunk each time. They never ack,
  never hold chunks, and have their own sender thread, so they cannot slow the
  owner or the drain.
- **Times** are seconds on the board's monotonic clock since the server
  started; the wall clock is given once in `welcome`.
- **Outputs made idle** at the end of every job, whatever the outcome
  (`stop_tproc`, `reset_gens`, internal start), unless the job set
  `run.idle_after: false` and ran to its end; and always before an image load.
- **Board ownership.** The server constructs the `QickSoc` itself and exposes
  no object; it holds an exclusive `flock` on the lock file (default
  `/tmp/qick-boardserver.lock`, with pid and port in it). A second server
  refuses to start. `qick.boardserver.server.board_lock_holder(path)` lets
  another program (the Pyro server, if someone adds the check) see it.

## Not in version 1 (reserved)

- `run.reduce` (sums per chunk on the board).
- Compile-on-board job kinds; DDR4 and MR buffers; clock changes; the
  ZCU111 RF board's LO synthesizers; RF settings by DAC/ADC port number for
  ports no generator or readout uses; board-side limits file (max gain,
  durations, bias limits: `rf_set` checks only the hardware ranges).
- TLS (the token is protected by the challenge, the data is not encrypted).
- Tokens stored hashed on the board: with an HMAC challenge the board needs
  the token itself (a stored hash would just be the key).

## Open questions

- **Pyro compatibility front.** Not built. Existing experiment code that calls
  `soc.something()` through Pyro would need either porting to the client or a
  small shim; whether the lab wants one, and for which calls, is open.
- Behaviour of the real backend on hardware: everything in
  `board-server-install.md`'s measurement list.
- tProc v1 programs: the code path exists (`AcquireProgram` + `load_prog`) but
  no v1 configuration is in the tests.
- Programs that override `compile_datamem()` or rely on attributes not in
  `dump_prog()` lose that on the wire (library limitation of `dump_prog`).
- Idling between back-to-back jobs is now optional per job (`run.idle_after`).
  Whether outputs left on should time out on the board is open.
- Identity for `rf_set` during a running job: the server compares the
  submitter **label**, which a client declares itself. Good enough between
  cooperating lab clients; not a security boundary.
- Which non-shared ZCU216 to test on, and when (Dave).
