# QICK board server: protocol sketch (draft, nothing built)

Status: a first sketch for discussion with the measurement-platform side. No
code exists. Message names and fields are proposals.

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

## What exists in the library already

- `QickProgram.dump_prog()` / `load_prog()`: a plain dictionary holding the
  compiled program, envelopes, channel declarations and loop shape. This is
  the job payload.
- `DataStreamer` (`qick/streamer.py`): a board thread that polls the tProc
  shot counter, reads new shots from the accumulated buffers, and puts them on
  a Python queue. So the drain is already separate from the transport. What
  it lacks: the queue is unbounded, and it does not detect the firmware ring
  buffer being overwritten when the drain falls more than one buffer length
  behind.
- Firmware buffers per readout: accumulated (one I/Q value per shot, a ring),
  decimated (a trace), and on some builds the DDR4 and MR buffers for long raw
  captures. The lab build adds integration weights.

## Framing

One TCP connection. Every message is a frame:

    4 bytes  length of the header
    4 bytes  length of the body
    header   UTF-8 JSON object: {"type": ..., "id": ..., ...}
    body     raw bytes (may be empty); arrays are described in the header as
             {"name", "dtype", "shape", "offset", "nbytes"} and laid end to end

All lengths and all array data are little-endian. A header is at most 1 MiB.
A body is at most 64 MiB: larger data (DDR4 captures) is always split into
chunks. Unknown header fields are ignored and an unknown message type is
answered with `error`, so the two ends can differ in version.

JSON for the header keeps the board dependency-free and easy to debug; the
body carries the bulk without encoding. (MessagePack would also work, but is
not in the standard library.)

## Messages

Client to board:

| Type | Purpose | Main fields |
|---|---|---|
| `hello` | Start; the board replies with a `challenge` (a nonce) | `protocol` version, client library version, `label` (engine, setup, person or agent), role wanted (`run` or `read`) |
| `auth` | Answer the challenge | HMAC-SHA256 of the nonce with the token; the token never crosses the wire |
| `get_config` | Fetch the board configuration (`soccfg`) | |
| `put_prog` | Store a program in the board's cache | the `dump_prog` dictionary (envelopes and weights in the body); the board replies with its content hash |
| `submit` | Queue a job | `kind` (`"qick_prog"`), `prog_ref` (content hash; `error` "unknown program" if evicted) or the program inline, `run` (rounds, start source, thresholds, which buffers to return, chunk size, `reduce`, time limit), `limits` (queue memory bound, policy when reached), `ref` (the client's own job reference) |
| `subscribe` | Watch a running job without owning it | `job`, delivery `newest_only`; no `ack` rights, never slows the owner |
| `get_state` | Board settings that are not in the program | |
| `board_change` | Reserved: anything that changes the board for everyone (bitstream, attenuators, clocks) | refused while another client's job is queued or running unless `force`; logged with who asked. Not in version 1 except attenuators if needed |
| `status` | State of one job, or with no job: connected clients (labels), the queue (each job's submitter and `ref`), server uptime, memory in use against the bound, last error, and when the outputs were last made idle | `job` |
| `fetch` | Resume a job's chunks from a sequence number after a reconnect (not needed otherwise: chunks are pushed) | `job`, `from_seq` |
| `ack` | Client has safely stored chunks up to a sequence number; the board may free them | `job`, `seq` |
| `cancel` | Stop a job | `job` |
| `bye` | Close | |

Board to client:

| Type | Purpose | Main fields |
|---|---|---|
| `challenge` | Reply to `hello` | nonce |
| `welcome` | Reply to a correct `auth` | protocol version, QICK library version, hash of the server's own code, firmware identity (name, timestamp, hash of the hardware description), board, Python version, `time_sync_period` and other capabilities, the board's wall-clock time and its monotonic time now |
| `config` | Reply to `get_config` | the `soccfg` JSON |
| `accepted` | Reply to `submit` | `job`, position in queue, the run parameters in effect after defaults, and the result layout: order of loop dimensions, readouts and rounds, and values per shot per readout, so a chunk can be written straight into the sweep array |
| `chunk` | A piece of results | `job`, `seq`, `round`, `shots` = [first, last) covered, arrays in the body, `queued_bytes` |
| `event` | Something the client must know | `job`, `kind`: `started`, `overrun` (firmware buffer overwritten: which shots), `dropped` (queue bound reached: which `seq`), `stopped`, `error` (with the Python traceback text) |
| `state` | Reply to `get_state` | ADC attenuators, DAC settings where readable, mixer/NCO settings, reference and lock state, temperatures if cheap |
| `done` | Job finished | `job`, outcome (`complete`, `incomplete`, `cancelled`, `failed`, `timed_out`), totals, whether outputs were made idle, times of: received, queued, loaded, started, finished, drained, sent |
| `error` | Request refused | reason (authentication failed, unsupported protocol, unknown job, unknown program, program format not understood: which field, board limit exceeded: which) |

## Behaviour to agree

- **Chunks are numbered** from 0 per job and state the shots they cover, so a
  gap is detectable by the client and can be recorded on the run.
- **Results outlive the connection.** A job keeps running and its chunks stay
  queued if the client disconnects; `fetch` with `from_seq` resumes. Chunks
  are freed on `ack`, or when the job's retention limit is reached.
- **When the queue bound is reached**, per-job policy: `stop` the run and
  report it (default), or `drop_oldest` and report which chunks.
- **Firmware overrun** is detected by comparing the tProc shot counter with
  the drain position against the ring length. It is reported with the affected
  shot range; the job outcome becomes `incomplete`.
- **One job runs at a time**; `submit` while one runs queues behind it.
  Several `submit`s sent back to back run with no gap between them.
- **Chunks are pushed** on the submitting connection as they are produced;
  there is no polling. `done` follows the last chunk.
- **Program cache** by content hash, so a repeated program is neither sent
  nor loaded again. (A program whose parameters changed is a different
  compiled program; avoiding a reload for that needs the board to diff the
  program and waveform memories, which is possible later.)
- **Times** are seconds on the board's monotonic clock since the server
  started. The board's wall clock is given once in `welcome` and is not
  trusted.
- **Outputs made idle.** At the end of every job, on cancel, on error, on a
  time limit and on a stop caused by the queue bound, the board stops the
  tProc and resets the generators, and records when it did. This is what the
  board commanded, not a measurement at the DAC.
- **Board-side limits** (optional): a file an administrator writes on the
  board with a maximum gain per generator and maximum pulse and run
  durations. Checked at `submit` against the compiled program's waveform
  table, including the extremes of sweeps, and refused with a reason. It
  cannot see values a program computes at run time, so it is a guard against
  mistakes, not a guarantee.
- **Tokens** reach the board by the lab's claim-link convention into a file
  only the server can read, are stored hashed and never logged. Two roles:
  `run` and `read`.
- **Averaging on the board** is optional per job (`run.reduce`): return every
  shot, or return running sums per chunk so a long average does not move
  every shot over the network.

## Open points

- Version 1 leaves out bitstream loading, compile-on-board jobs and TLS; the
  request types are reserved.

- Which non-shared ZCU216 to test on, and when to start (Dave).
- Whether the generic JSON-configured program is compiled on the PC and sent
  as a compiled program, or also offered as a compile-on-board job kind.
- Loading bitstreams through the server (RISC-Q does this with "bundles").
- TLS, or token over a trusted lab network only.
- Decimated and DDR4/MR captures: same chunk mechanism, sizes to be checked.
