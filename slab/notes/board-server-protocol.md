# QICK board server: protocol, version 1

Start a fake board to develop a client against (no hardware, any PC):

    python -m qick.boardserver new-token /tmp/fake.token
    python -m qick.boardserver --fake --token-file /tmp/fake.token --image-dir /tmp/fake-images --port 8788

It creates two fake images, `fake-a` and `fake-b`, whose configurations differ
(`fake-b`: other `fw_timestamp`, accumulated ring 1024 instead of 8192, decimated
buffer 2048, and its load reports `clocks_reprogrammed: true`), and loads
`fake-a`. Add `--allow-job-image-load` to try `run.image` loading;
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
before it goes near a fridge.

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

Requests carry a client-chosen `id`; the direct reply carries `reply_to` with
that id. Pushed messages (`chunk`, `event`, `done`) have no `reply_to`.

**Program dictionaries** (`dump_prog()`) travel as JSON in the header with
every numpy array (envelopes, readout weights) replaced by
`{"$array": "a<n>"}` and the arrays in the body. The content hash
(`prog_hash`) is SHA-256 over the canonical JSON (sorted keys) and each
array's name, dtype, shape and bytes; both ends compute it the same way
(`protocol.tree_hash`).

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
`status`, `subscribe`, `unsubscribe`, `list_images`, `current_image`, `bye`
(anything else: `forbidden`).

The token file (one `<role> <token>` or bare token per line; path from
`--token-file` or the config file's `token_file`, never the token itself on the
command line or in the environment) is read at start; tokens are not logged.
The server refuses to start without one unless `--fake --insecure-fake`.

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
| `fetch` | become the owner of a job and receive from a point | `job`, `from_seq`, `from_event` | `fetching` |
| `ack` | chunks up to `seq` are stored by the client; the board may free them | `job`, `seq` | none (errors only) |
| `subscribe` | watch a job without owning it | `job`, `newest_only` (default true), `from_now` (default true) | `subscribed` |
| `unsubscribe` | stop watching | `job` | `unsubscribed` |
| `cancel` | stop a queued or running job (any `run` client; logged) | `job` | `cancelling` {`was`} |
| `list_images` | the image store | | `images` {`images`, `current`, `current_image`} |
| `put_image` | upload an image in chunks | `phase` = `begin` / `data` / `commit` / `abort` (below) | `upload` or `image_stored` |
| `load_image` | program the FPGA with a store image | `image` (name or hash), `force` | `image_loaded` |
| `current_image` | the record of the last load | | `current_image` {`image`} |
| `bye` | close | | `bye` |

Board to client:

| Type | When | Main fields |
|---|---|---|
| `challenge` | reply to `hello` | `nonce`, `protocol` |
| `welcome` | reply to a correct `auth` | `protocol`, `qick_version`, `server_code_hash` (SHA-256 of the server's own modules), `session`, `role`, `insecure`, `fake`, `firmware` {`image`, `fw_timestamp`, `hwh_sha256`}, `board`, `python`, `capabilities` {`job_kinds`, `modes`, `reduce` (false), `image_store`, `allow_job_image_load`, `time_sync_period`, `queue_flags`}, `limits` {`max_header`, `max_body`, `max_queue_bytes`, `max_retention_s`}, `wall_time`, `monotonic` |
| `accepted` | reply to `submit` | `job`, `ref`, `position`, `run` and `limits` in effect, `layout`, `prog_hash`, `image_load_first` (hash, if the job will load an image) |
| `chunk` | pushed | `job`, `seq`, `round`, `buffer` (`acc` or `dec`), `shots` [first, last), `arrays`, `queued_bytes` |
| `event` | pushed | `job`, `eseq`, `kind`, `t`, and per kind (below) |
| `done` | pushed after the last chunk | `job`, `ref`, `outcome`, `reason`, `error`, `totals`, `outputs_idle`, `idle`, `times`, `subscriber` |
| `error` | a refused request | `reply_to`, `code`, `message`, `request_type`, and fields per code |

### submit

    {"type": "submit", "kind": "qick_prog",
     "prog_ref": "<hash>"  or  "prog": {...}, "arrays": [...] (+ body),
     "run": {"rounds": 1, "mode": "acc" | "dec", "start_src": "internal" | "external",
             "stride": null, "time_limit_s": null, "image": null, "reduce": null},
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
| `image_loaded` | `image`: the load record (the job named another image and the server is allowed to load it) |
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
`queue_bound`; for `cancelled` `client`, `image_load` or `server_shutdown`;
for `failed` the error code. `error` {`code`, `message`, `traceback`, ...} for
`failed`. `totals` {`chunks`, `shots_stored`, `dropped` (seq ranges),
`overruns`, `events`, `bound_stop`}. `outputs_idle` and `idle`: whether the
board stopped the tProc and ran `reset_gens()` after the job (what the board
commanded, not a measurement at the DAC). `times` (board monotonic seconds
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
- `load_image` {`image`, `force`}: refused with `job_running` while a job runs
  (even with `force`: cancel it first); refused with `jobs_queued` (listing
  them) while jobs are queued, unless `force`, which cancels them: each gets a
  `done` with outcome `cancelled`, reason `image_load`, and the reply lists
  them. Reply `image_loaded`: `name`, `hash`, `bit_sha256`, `hwh_sha256`,
  `soccfg` (new), `duration_s` (wall-clock time of constructing `QickSoc`),
  `requested_by` (label), `reason`, `t` (board monotonic time), `wall_time`,
  `reset` {`pl_reconfigured` (true), `clocks_reprogrammed` (whether
  `QickSoc` re-initialised the clock chips: it does when the RF PLLs are not
  locked or clock options are given), `converters_restarted`,
  `tproc_memories_cleared`, `envelopes_cleared`, `program_cache_cleared`},
  `cancelled_jobs` [{`job`, `submitter`, `ref`}]. Every load is logged with
  who asked. After a load the program cache is empty and programs of queued
  jobs are rebuilt against the new configuration.
- `current_image`: the same record for the last load; `{"state": "unknown",
  "detail": "loaded before server start", "assumed": {...}}` if the server
  attached to an already configured board (`--attach`); `{"state": "failed",
  ...}` after a failed load; `{"state": "none"}` if nothing was loaded.
- `run.image` (name or hash): if it is not the loaded image, the job is refused
  at `submit` with `image_mismatch` {`wanted`, `wanted_name`, `loaded`},
  unless the server runs with `allow_job_image_load` (default off); then the
  job loads it when it reaches the head of the queue (event `image_loaded`).

### state

`get_state` returns `image`, `loading` (a load in progress), `outputs_idle_at`,
`last_idle`, `monotonic`, `board`, `fw_timestamp`, `clocks_locked`, and
`tproc` {`type`, `revision`, `status` (decoded TPROC_STATUS), `axi_regs`
(every AXI register), `debug` (what `print_debug` shows), `queue_flags`
{`supported`, `lost`}} on tProc v2. ADC attenuators, DAC settings,
mixer/NCO, reference lock detail and temperatures are listed under
`not_implemented`.

`status` without a job: `clients` (labels, roles, sessions), `running`,
`queue` (each job's submitter and `ref`), `recent`, `uptime_s`, `memory`
{`queued_bytes`, `bound`}, `last_error`, `outputs_idle_at`, `image`, `loading`.
With `job`: that job's full record.

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
| `job_running` | `load_image` while a job runs (even with `force`) | `job`, `submitter`, `ref` |
| `jobs_queued` | `load_image` without `force` while jobs are queued | `jobs` |
| `image_mismatch` | job names an image other than the loaded one | `wanted`, `wanted_name`, `loaded` |
| `image_load_failed` | constructing `QickSoc` from the image raised | `traceback`, `image` |
| `board_loading` | an image load is in progress | |
| `internal` | unexpected server error | `traceback` |

In `done.error.code` only: `job_exception` (any Python exception in the job;
`exc_type`, `traceback`) and `tproc_queue_overflow` (the lab firmware's
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
  (`stop_tproc`, `reset_gens`, internal start), and before an image load.
- **Board ownership.** The server constructs the `QickSoc` itself and exposes
  no object; it holds an exclusive `flock` on the lock file (default
  `/tmp/qick-boardserver.lock`, with pid and port in it). A second server
  refuses to start. `qick.boardserver.server.board_lock_holder(path)` lets
  another program (the Pyro server, if someone adds the check) see it.

## Not in version 1 (reserved)

- `run.reduce` (sums per chunk on the board).
- Compile-on-board job kinds; DDR4 and MR buffers; `board_change` for
  attenuators and clocks; board-side limits file (max gain, durations).
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
- Whether idling the outputs between back-to-back jobs (one `reset_gens`
  each) is wanted, or should be optional for jobs from the same owner.
- Which non-shared ZCU216 to test on, and when (Dave).
