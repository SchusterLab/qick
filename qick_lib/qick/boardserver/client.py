"""Blocking client for the QICK board server (PC side, Python 3.8+).

    from qick.boardserver.client import BoardClient
    with BoardClient('board-host', 8788, token_file='~/.qick/board.token', label='me/notebook') as bc:
        soccfg = bc.get_config()                 # QickConfig
        prog = MyProgram(soccfg, reps=1000, final_delay=1.0, cfg=cfg)
        job = bc.submit(prog, rounds=10)
        for chunk in job.iter_chunks():          # optional: watch data arrive
            ...
        iq = job.result(prog)                    # same shape as prog.acquire(soc, rounds=10)

A background thread reads every frame; replies go to the waiting request, chunks
and events into their Job. Chunks are acknowledged as soon as they are stored in
the Job (auto_ack), which lets the board free them.
"""
import hashlib
import os
import threading
import time
from functools import reduce
from operator import mul

import numpy as np

from . import protocol as P
from .protocol import ProtocolError


class BoardError(Exception):
    """The board refused a request. `code` is a stable machine-readable code."""
    def __init__(self, header):
        super().__init__('%s: %s' % (header.get('code'), header.get('message')))
        self.code = header.get('code')
        self.header = header


class IncompleteResult(Exception):
    pass


class Job:
    """A job on the board, as seen from this client."""

    def __init__(self, client, jid, accepted=None, progtree=None, progarrays=None, owner=True):
        self.client = client
        self.id = jid
        self.accepted = accepted or {}
        self.ref = self.accepted.get('ref')
        self.layout = self.accepted.get('layout')
        self.progtree = progtree
        self.progarrays = progarrays
        self.owner = owner
        self.chunks = {}       # seq -> (header, {name: ndarray})
        self.events = []
        self.done = None
        self.next_seq = 0      # next seq expected in order
        self.gaps = []         # [[a, b)] of seqs that were skipped
        self.next_event = 0
        self.cv = threading.Condition()
        self.on_chunk = None
        self._order = []       # arrival order of ('chunk', seq) / ('event', i)

    # --- fed by the reader thread ---
    def _add_chunk(self, header, arrays):
        with self.cv:
            seq = header['seq']
            if seq > self.next_seq:
                self.gaps.append([self.next_seq, seq])
            self.next_seq = max(self.next_seq, seq + 1)
            self.chunks[seq] = (header, arrays)
            self._order.append(('chunk', seq))
            self.cv.notify_all()
        if self.on_chunk is not None:
            self.on_chunk(header, arrays)

    def _add_event(self, header):
        with self.cv:
            self.events.append(header)
            self.next_event = max(self.next_event, header.get('eseq', -1) + 1)
            if header.get('kind') == 'started' and header.get('layout'):
                self.layout = header['layout']
            self._order.append(('event', len(self.events) - 1))
            self.cv.notify_all()

    def _set_done(self, header):
        with self.cv:
            self.done = header
            self.cv.notify_all()

    def _wake(self):
        with self.cv:
            self.cv.notify_all()

    # --- user API ---
    @property
    def outcome(self):
        return None if self.done is None else self.done['outcome']

    def wait(self, timeout=None):
        """Block until `done` arrives; return the done header."""
        end = None if timeout is None else time.monotonic() + timeout
        with self.cv:
            while self.done is None:
                if self.client.closed and self.client.owner_of(self) is self.client:
                    raise ConnectionError('connection to the board lost; resume with BoardClient.fetch(job)')
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    raise TimeoutError('job %s not done after %s s' % (self.id, timeout))
                self.cv.wait(0.2 if left is None else min(0.2, left))
        return self.done

    def iter_chunks(self, timeout=None):
        """Yield (header, arrays) for each chunk in arrival order, until done."""
        i = 0
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            with self.cv:
                while i >= len(self._order) and self.done is None:
                    if self.client.closed:
                        raise ConnectionError('connection to the board lost')
                    if end is not None and time.monotonic() > end:
                        raise TimeoutError('no chunk within %s s' % timeout)
                    self.cv.wait(0.2)
                if i >= len(self._order):
                    return
                kind, k = self._order[i]
                i += 1
                item = self.chunks.get(k) if kind == 'chunk' else None
            if item is not None:
                yield item

    def cancel(self):
        return self.client.cancel(self.id)

    def events_of(self, kind):
        return [e for e in self.events if e.get('kind') == kind]

    def raw_rounds(self):
        """Assemble the received chunks: a list over rounds of dicts
        {'acc': [array (*loop_dims, reads, 2) int64 per readout], 'dec': [...] or None,
        'missing': [[first, last) shots not received]}."""
        lay = self.layout
        if lay is None:
            raise IncompleteResult('no layout yet (the job has not started)')
        dims = lay['loop_dims']
        total = lay['shots_per_round']
        reads = [r['reads_per_shot'] for r in lay['readouts']]
        out = []
        for _ in range(lay['rounds']):
            out.append({'acc': [np.zeros(tuple(dims) + (n, 2), dtype=np.int64) for n in reads],
                        'dec': [None] * len(reads) if lay['mode'] == 'dec' else None,
                        'got': np.zeros(total, dtype=bool)})
        with self.cv:
            items = sorted(self.chunks.items())
        for seq, (h, arrays) in items:
            r = out[h['round']]
            a, b = h['shots']
            for i, n in enumerate(reads):
                acc = arrays.get('acc:%d' % i)
                if acc is not None:
                    r['acc'][i].reshape((-1, 2))[a * n:b * n] = acc
                if h['buffer'] == 'dec':
                    r['dec'][i] = arrays['dec:%d' % i]
            r['got'][a:b] = True
        for r in out:
            got = r.pop('got')
            missing = []
            idx = np.flatnonzero(~got)
            if len(idx):
                starts = [idx[0]] + [idx[k] for k in range(1, len(idx)) if idx[k] != idx[k - 1] + 1]
                ends = [idx[k] + 1 for k in range(len(idx) - 1) if idx[k + 1] != idx[k] + 1] + [idx[-1] + 1]
                missing = [[int(s), int(e)] for s, e in zip(starts, ends)]
            r['missing'] = missing
        return out

    def program(self, soccfg=None):
        """Rebuild a program object from the dictionary that was submitted (for processing)."""
        from ..asm_v2 import AcquireProgramV2
        from ..asm_v1 import AcquireProgram
        if soccfg is None:
            soccfg = self.client.get_config()
        progdict = P.unpack_tree(self.progtree, dict(self.progarrays))
        cls = AcquireProgramV2 if soccfg['tprocs'][0]['type'] == 'qick_processor' else AcquireProgram
        prog = cls(soccfg)
        prog.load_prog(progdict)
        return prog

    def result(self, prog=None, threshold=None, angle=None, remove_offset=True, allow_incomplete=False,
               timeout=None):
        """Wait for the job and return what acquire() (mode 'acc') or acquire_decimated()
        (mode 'dec') would have returned, computed with the program's own processing
        methods. prog: the program object that was submitted (recommended: a subclass
        that overrides the processing then behaves as with acquire); without it a
        program is rebuilt from the submitted dictionary."""
        self.wait(timeout)
        if self.outcome != 'complete' and not allow_incomplete:
            raise IncompleteResult('job %s outcome %s (%s); error: %s' % (
                self.id, self.outcome, self.done.get('reason'), (self.done.get('error') or {}).get('message')))
        if self.gaps and not allow_incomplete:
            raise IncompleteResult('chunks missing on this client: %s' % self.gaps)
        rounds = self.raw_rounds()
        if not allow_incomplete:
            for k, r in enumerate(rounds):
                if r['missing']:
                    raise IncompleteResult('round %d missing shots %s' % (k, r['missing']))
        if prog is None:
            prog = self.program()
        if self.layout['mode'] == 'dec':
            prog.acquire_params = {'type': 'decimated', 'remove_offset': remove_offset}
            prog.acc_buf = [r['acc'] for r in rounds]
            prog.rounds_buf = [prog._process_decimated(r['dec']) for r in rounds]
            return prog._summarize_decimated(prog.rounds_buf)
        prog.acquire_params = {'type': 'accumulated', 'threshold': threshold, 'angle': angle,
                               'remove_offset': remove_offset, 'hidereps': True}
        prog.rounds_buf = [prog._process_accumulated(r['acc']) for r in rounds]
        prog.acc_buf = rounds[-1]['acc'] if rounds else None
        return prog._summarize_accumulated(prog.rounds_buf)


class BoardClient:
    def __init__(self, host, port=8788, token=None, token_file=None, label=None, role='run', timeout=30.0,
                 auto_ack=True, insecure=False):
        if token is None and token_file is not None:
            tokens = P.read_token_file(os.path.expanduser(token_file))
            token = [t for r, t in tokens if r == role or (role == 'read' and r == 'run')][0]
        if token is None and not insecure:
            raise ValueError('give token or token_file (insecure=True only for a fake board started with '
                             '--insecure-fake)')
        self.host, self.port = host, port
        self._token = token if token is not None else b''
        self.label = label or 'qick-client'
        self.role = role
        self.timeout = timeout
        self.auto_ack = auto_ack
        self.welcome = None
        self.closed = False
        self._sock = None
        self._send_lock = threading.Lock()
        self._pending = {}
        self._plock = threading.Lock()
        self._nid = 0
        self._jobs = {}
        self._known_progs = set()
        self._soccfg = None
        self.unsolicited_errors = []
        self._connect()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # --- connection ---
    def _connect(self):
        sock = P.connect(self.host, self.port, timeout=self.timeout)
        self._sock = sock
        P.send_frame(sock, {'type': 'hello', 'id': 0, 'protocol': P.PROTOCOL_VERSION,
                            'client_version': _version(), 'label': self.label, 'role': self.role})
        h, _ = P.recv_frame(sock, idle_timeout=self.timeout)
        if h['type'] == 'error':
            sock.close()
            raise BoardError(h)
        mac = P.auth_mac(self._token, h['nonce'], self.role, self.label)
        P.send_frame(sock, {'type': 'auth', 'id': 1, 'hmac': mac})
        h, _ = P.recv_frame(sock, idle_timeout=self.timeout)
        if h['type'] == 'error':
            sock.close()
            raise BoardError(h)
        self.welcome = h
        self._nid = 2
        self._reader = threading.Thread(target=self._read_loop, name='boardclient-read', daemon=True)
        self._reader.start()

    def close(self):
        if self.closed:
            return
        try:
            self.request('bye', timeout=2.0)
        except Exception:
            pass
        self.abort()

    def abort(self):
        """Drop the connection without saying goodbye (what a crash or network loss looks like)."""
        self.closed = True
        try:
            self._sock.shutdown(2)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._fail_pending(ConnectionError('connection closed'))
        for j in list(self._jobs.values()):
            j._wake()

    def owner_of(self, job):
        return self if self._jobs.get(job.id) is job else None

    def _send(self, header, body=b''):
        with self._send_lock:
            P.send_frame(self._sock, header, body, stall_timeout=self.timeout * 10)

    def _fail_pending(self, exc):
        with self._plock:
            pend = list(self._pending.values())
            self._pending.clear()
        for p in pend:
            p['error'] = exc
            p['event'].set()

    def _read_loop(self):
        err = 'closed'
        try:
            while not self.closed:
                h, body = P.recv_frame(self._sock, idle_timeout=None, frame_timeout=self.timeout * 10,
                                       should_stop=lambda: self.closed)
                self._dispatch(h, body)
        except (EOFError, ConnectionError, OSError, ProtocolError) as e:
            err = e
        except Exception as e:  # pragma: no cover
            err = e
        self.closed = True
        self._fail_pending(ConnectionError('connection to the board lost: %s' % (err,)))
        for j in list(self._jobs.values()):
            j._wake()

    def _dispatch(self, h, body):
        rid = h.get('reply_to')
        if rid is not None:
            with self._plock:
                p = self._pending.pop(rid, None)
            if p is not None:
                if h['type'] != 'error' and p.get('on_reply') is not None:
                    p['on_reply'](h)
                p['reply'] = (h, body)
                p['event'].set()
                return
        t = h['type']
        if t == 'error':
            self.unsolicited_errors.append(h)
            return
        job = self._jobs.get(h.get('job'))
        if job is None:
            return
        if t == 'chunk':
            arrays = P.unpack_arrays(h.get('arrays', []), body)
            job._add_chunk(h, arrays)
            if job.owner and self.auto_ack:
                try:
                    self._send({'type': 'ack', 'job': job.id, 'seq': h['seq']})
                except OSError:
                    pass
        elif t == 'event':
            job._add_event(h)
        elif t == 'done':
            job._set_done(h)

    def request(self, typ, body=b'', timeout=None, on_reply=None, **fields):
        """Send a request and wait for its reply header (raises BoardError on `error`)."""
        if self.closed:
            raise ConnectionError('connection closed')
        with self._plock:
            rid = self._nid
            self._nid += 1
            p = {'event': threading.Event(), 'reply': None, 'error': None, 'on_reply': on_reply}
            self._pending[rid] = p
        h = {'type': typ, 'id': rid}
        h.update(fields)
        self._send(h, body)
        if not p['event'].wait(self.timeout if timeout is None else timeout):
            with self._plock:
                self._pending.pop(rid, None)
            raise TimeoutError('no reply to %s within %s s' % (typ, timeout or self.timeout))
        if p['error'] is not None:
            raise p['error']
        rh, rbody = p['reply']
        if rh['type'] == 'error':
            raise BoardError(rh)
        return rh

    # --- board ---
    def get_config(self, refresh=False):
        from ..qick_asm import QickConfig
        if self._soccfg is None or refresh:
            self._soccfg = QickConfig(self.request('get_config')['soccfg'])
        return self._soccfg

    def get_state(self):
        return self.request('get_state')['state']

    def status(self, job=None):
        h = self.request('status', job=job.id if isinstance(job, Job) else job)
        return {k: v for k, v in h.items() if k not in ('type', 'reply_to')}

    # --- programs and jobs ---
    @staticmethod
    def pack_program(prog):
        progdict = prog.dump_prog() if hasattr(prog, 'dump_prog') else prog
        tree, arrays = P.pack_tree(progdict)
        return tree, arrays, P.tree_hash(tree, arrays)

    def put_prog(self, prog):
        tree, arrays, h = self.pack_program(prog)
        descs, parts = P.pack_arrays(arrays)
        r = self.request('put_prog', body=parts, prog=tree, arrays=descs)
        self._known_progs.add(r['hash'])
        return r['hash']

    def submit(self, prog, rounds=1, mode='acc', start_src='internal', stride=None, time_limit_s=None,
               image=None, ref=None, limits=None, fake=None, use_cache=True, on_chunk=None, idle_after=True):
        """Queue a compiled program. mode 'acc' = acquire(), 'dec' = acquire_decimated().
        Returns a Job; chunks start arriving as soon as it runs."""
        tree, arrays, h = self.pack_program(prog)
        run = {'rounds': rounds, 'mode': mode, 'start_src': start_src, 'stride': stride,
               'time_limit_s': time_limit_s, 'image': image}
        if idle_after is not True:
            run['idle_after'] = idle_after
        if fake is not None:
            run['fake'] = fake
        holder = {}

        def on_reply(rh):
            # runs in the reader thread before any chunk of the job can be dispatched
            if rh['type'] == 'accepted':
                job = Job(self, rh['job'], rh, tree, arrays)
                job.on_chunk = on_chunk
                self._jobs[job.id] = job
                holder['job'] = job

        fields = {'kind': 'qick_prog', 'run': run, 'limits': limits or {}, 'ref': ref}
        if use_cache and h in self._known_progs:
            try:
                self.request('submit', on_reply=on_reply, prog_ref=h, **fields)
                return holder['job']
            except BoardError as e:
                if e.code != 'unknown_program':
                    raise
        descs, parts = P.pack_arrays(arrays)
        self.request('submit', body=parts, on_reply=on_reply, prog=tree, arrays=descs, **fields)
        self._known_progs.add(h)
        return holder['job']

    def fetch(self, job, from_seq=None, from_event=None):
        """Become the owner of a job (e.g. after a reconnect) and receive its chunks
        from from_seq on. Passing the Job object from the lost connection continues it
        in place (from its next_seq and event count, so there is no gap)."""
        if isinstance(job, Job):
            j = job
            j.client = self
            j.owner = True
        else:
            j = Job(self, job)
        if from_seq is None:
            from_seq = j.next_seq
        if from_event is None:
            from_event = j.next_event
        self._jobs[j.id] = j
        try:
            r = self.request('fetch', job=j.id, from_seq=from_seq, from_event=from_event)
        except BoardError:
            self._jobs.pop(j.id, None)
            raise
        if j.layout is None:
            st = self.status(j.id)['job']
            j.ref = st.get('ref')
        return j

    def subscribe(self, job_id, newest_only=True, from_now=True):
        j = Job(self, job_id, owner=False)
        self._jobs[job_id] = j
        self.request('subscribe', job=job_id, newest_only=newest_only, from_now=from_now)
        return j

    def cancel(self, job):
        return self.request('cancel', job=job.id if isinstance(job, Job) else job)['was']

    # --- RF cards ---
    def rf_get(self):
        return self.request('rf_get')['rf']

    def rf_set(self, settings, settle_s=None):
        """Apply RF settings in the server's safe order. Raises BoardError with code
        adc_flags (header['record']) when an ADC is over range at idle afterwards."""
        kw = {} if settle_s is None else {'settle_s': settle_s}
        r = self.request('rf_set', settings=settings, **kw)
        return {k: v for k, v in r.items() if k not in ('type', 'reply_to')}

    def put_rf_safe(self, image, settings, settle_s=None):
        kw = {} if settle_s is None else {'settle_s': settle_s}
        return self.request('put_rf_safe', image=image, settings=settings, **kw)

    # --- images ---
    def list_images(self):
        return self.request('list_images')

    def current_image(self):
        return self.request('current_image')['image']

    def load_image(self, image, force=False, timeout=900.0, skip_rf_safe=False):
        kw = {'skip_rf_safe': True} if skip_rf_safe else {}
        r = self.request('load_image', image=image, force=force, timeout=timeout, **kw)
        self._soccfg = None
        self._known_progs.clear()
        return {k: v for k, v in r.items() if k not in ('type', 'reply_to')}

    def put_image(self, name, bit_path, hwh_path, replace=False, note=None, chunk_bytes=8 << 20,
                  bit_sha256=None, hwh_sha256=None):
        """Upload a bit/hwh pair in chunks. bit_sha256/hwh_sha256 override the announced
        hashes (only useful to test the board's check)."""
        files = {'bit': bit_path, 'hwh': hwh_path}
        sha = {k: _sha256_file(p) for k, p in files.items()}
        if bit_sha256:
            sha['bit'] = bit_sha256
        if hwh_sha256:
            sha['hwh'] = hwh_sha256
        r = self.request('put_image', phase='begin', name=name, replace=replace, note=note,
                         bit_sha256=sha['bit'], hwh_sha256=sha['hwh'],
                         bit_size=os.path.getsize(bit_path), hwh_size=os.path.getsize(hwh_path))
        up = r['upload']
        try:
            for which, path in files.items():
                with open(path, 'rb') as f:
                    off = 0
                    while True:
                        data = f.read(chunk_bytes)
                        if not data:
                            break
                        self.request('put_image', body=data, phase='data', upload=up, file=which, offset=off)
                        off += len(data)
            return self.request('put_image', phase='commit', upload=up)['image']
        except BoardError as e:
            if e.code not in ('hash_mismatch',):
                try:
                    self.request('put_image', phase='abort', upload=up)
                except Exception:
                    pass
            raise


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def _version():
    try:
        from .. import get_version
        return get_version()
    except Exception:
        return None
