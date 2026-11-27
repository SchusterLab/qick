"""Off-board tests of the board server (qick.boardserver) against the fake board.

The fake replaces only the QickSoc/pynq layer (backend.FakeSoc); the server, job
queue, drain and the real backend's run sequence are the code under test. Nothing
here says anything about behaviour on hardware.

Run:  .venv/bin/python -m pytest slab/tests/test_boardserver.py
"""
import ast
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

import qick
from qick import QickConfig
from qick.asm_v2 import AveragerProgramV2
from qick.boardserver import protocol as P
from qick.boardserver.backend import (FakeBackend, FakeSoc, QickSocBackend, fake_acc_values, fake_dec_values,
                                      fake_image_files, FAKE_VARIANTS)
from qick.boardserver.client import BoardClient, BoardError, IncompleteResult
from qick.boardserver.images import ImageStore
from qick.boardserver.server import BoardServer, acquire_lock_file, board_lock_holder
from qick.streamer import DataStreamer, iter_accumulated

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..'))
TOKEN = b'test-token-5f3a9c'
READ_TOKEN = b'read-only-token-77'


class Prog(AveragerProgramV2):
    def _initialize(self, cfg):
        self.declare_gen(ch=0, nqz=1)
        self.declare_readout(ch=0, length=0.5)
        self.add_readoutconfig(ch=0, name='ro', freq=100, gen_ch=0)
        self.add_pulse(ch=0, name='p', style='const', freq=100, phase=0, gain=0.5, length=0.1)
        if cfg.get('loop'):
            self.add_loop('x', cfg['loop'])

    def _body(self, cfg):
        self.send_readoutconfig(ch=0, name='ro', t=0)
        self.pulse(ch=0, name='p', t=0)
        self.trigger(ros=[0], t=0.2)
        if cfg.get('two_reads'):
            self.trigger(ros=[0], t=1.0)


def make_prog(soccfg, reps=100, loop=4, two_reads=False):
    return Prog(soccfg, reps=reps, final_delay=1.0, cfg={'loop': loop, 'two_reads': two_reads})


# --- fixtures ---------------------------------------------------------------

class Board:
    def __init__(self, tmp_path, cfg=None, load='fake-a', inject=None):
        self.dir = str(tmp_path)
        self.images = ImageStore(os.path.join(self.dir, 'images'))
        for v in FAKE_VARIANTS:
            self.images.add_bytes(v, *fake_image_files(v), added_by='test')
        self.backend = FakeBackend(inject=inject)
        c = {'host': '127.0.0.1', 'port': 0, 'lock_file': os.path.join(self.dir, 'board.lock'),
             'auth_fail_delay_s': 0.0}
        c.update(cfg or {})
        self.srv = BoardServer(self.backend, self.images, c, tokens=[('run', TOKEN), ('read', READ_TOKEN)])
        self.srv.start()
        if load:
            self.srv.load_at_start(load)
        self.port = self.srv.address[1]
        self.clients = []

    def client(self, label='test', token=TOKEN, **kw):
        c = BoardClient('127.0.0.1', self.port, token=token, label=label, **kw)
        self.clients.append(c)
        return c

    def raw(self):
        s = P.connect('127.0.0.1', self.port)
        return s

    def close(self):
        for c in self.clients:
            try:
                c.abort()
            except Exception:
                pass
        self.srv.stop()


@pytest.fixture
def board(tmp_path):
    b = Board(tmp_path)
    yield b
    b.close()


@pytest.fixture
def board_factory(tmp_path):
    made = []

    def make(**kw):
        b = Board(tmp_path / ('b%d' % len(made)), **kw)
        made.append(b)
        return b
    yield make
    for b in made:
        b.close()


def expected_round(rnd, n_points, ch=0):
    return fake_acc_values(ch, rnd, np.arange(n_points))


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    raise AssertionError('condition not met within %s s' % timeout)


# --- framing ------------------------------------------------------------------

def test_frame_roundtrip():
    a, b = socket.socketpair()
    P.prepare_socket(a)
    P.prepare_socket(b)
    arrs = [('x', np.arange(10, dtype=np.int32).reshape(5, 2)),
            ('y', np.linspace(0, 1, 7)),
            ('big_endian', np.arange(4, dtype='>i8')),
            ('empty', np.zeros((0, 2), dtype=np.int16))]
    descs, parts = P.pack_arrays(arrs)
    P.send_frame(a, {'type': 'test', 'id': 3, 'arrays': descs, 'text': 'héllo'}, parts)
    h, body = P.recv_frame(b)
    assert h['type'] == 'test' and h['text'] == 'héllo'
    got = P.unpack_arrays(h['arrays'], body)
    for name, arr in arrs:
        assert np.array_equal(got[name], arr)
        assert got[name].dtype.byteorder in ('<', '=', '|')
    assert all(d['dtype'][0] in '<|' for d in h['arrays'])
    # a prefix is two little-endian uint32
    raw = b''.join(bytes(x) for x in P.encode_frame({'type': 'z'}, b'abc'))
    assert struct.unpack('<II', raw[:8]) == (len(b'{"type":"z"}'), 3)
    a.close()
    b.close()


def test_tree_roundtrip_and_hash(board):
    c = board.client()
    prog = make_prog(c.get_config())
    tree, arrays, h = BoardClient.pack_program(prog)
    json.dumps(tree)  # JSON-safe
    back = P.unpack_tree(tree, dict(arrays))
    tree2, arrays2 = P.pack_tree(back)
    assert P.tree_hash(tree2, arrays2) == h


def test_size_caps():
    with pytest.raises(P.ProtocolError) as e:
        P.encode_frame({'type': 'x', 'pad': 'a' * (P.MAX_HEADER + 1)})
    assert e.value.code == 'frame_too_large'
    with pytest.raises(P.ProtocolError):
        P.encode_frame({'type': 'x'}, [b'\0' * (P.MAX_BODY + 1)])
    for hlen, blen in ((P.MAX_HEADER + 1, 0), (10, P.MAX_BODY + 1)):
        a, b = socket.socketpair()
        P.prepare_socket(b)
        a.sendall(struct.pack('<II', hlen, blen))
        with pytest.raises(P.ProtocolError) as e:
            P.recv_frame(b)
        assert e.value.code == 'frame_too_large'
        a.close()
        b.close()


def test_server_refuses_oversize_frame(board):
    s = board.raw()
    s.sendall(struct.pack('<II', P.MAX_HEADER + 1, 0))
    h, _ = P.recv_frame(s, idle_timeout=5)
    assert h['type'] == 'error' and h['code'] == 'frame_too_large'
    with pytest.raises((EOFError, ConnectionError)):
        P.recv_frame(s, idle_timeout=5)
    s.close()


# --- authentication -----------------------------------------------------------

class Tap:
    """TCP relay that records every byte in both directions."""
    def __init__(self, port):
        self.target = port
        self.ls = socket.socket()
        self.ls.bind(('127.0.0.1', 0))
        self.ls.listen(1)
        self.port = self.ls.getsockname()[1]
        self.data = bytearray()
        threading.Thread(target=self._run, daemon=True).start()

    def _pipe(self, a, b):
        try:
            while True:
                d = a.recv(65536)
                if not d:
                    break
                self.data += d
                b.sendall(d)
        except OSError:
            pass
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _run(self):
        c, _ = self.ls.accept()
        s = socket.create_connection(('127.0.0.1', self.target))
        threading.Thread(target=self._pipe, args=(c, s), daemon=True).start()
        threading.Thread(target=self._pipe, args=(s, c), daemon=True).start()


def test_auth_success_token_never_on_wire(board):
    tap = Tap(board.port)
    c = BoardClient('127.0.0.1', tap.port, token=TOKEN, label='tapped')
    assert c.welcome['type'] == 'welcome' and c.welcome['role'] == 'run'
    assert c.welcome['protocol'] == P.PROTOCOL_VERSION
    job = c.submit(make_prog(c.get_config(), reps=10, loop=2))
    assert job.wait(10)['outcome'] == 'complete'
    c.close()
    time.sleep(0.1)
    assert len(tap.data) > 1000
    assert TOKEN not in bytes(tap.data)
    assert TOKEN.hex().encode() not in bytes(tap.data)


def test_auth_failure_and_roles(board):
    with pytest.raises(BoardError) as e:
        board.client(token=b'wrong')
    assert e.value.code == 'auth_failed'
    # a read token cannot authenticate for the run role
    with pytest.raises(BoardError) as e:
        board.client(token=READ_TOKEN)
    assert e.value.code == 'auth_failed'
    r = board.client(token=READ_TOKEN, role='read')
    assert r.welcome['role'] == 'read'
    r.get_config()
    with pytest.raises(BoardError) as e:
        r.submit(make_prog(r.get_config(), reps=2, loop=0))
    assert e.value.code == 'forbidden'
    with pytest.raises(BoardError) as e:
        r.load_image('fake-b')
    assert e.value.code == 'forbidden'


def test_no_request_before_auth(board):
    s = board.raw()
    P.send_frame(s, {'type': 'get_config', 'id': 1})
    h, _ = P.recv_frame(s, idle_timeout=5)
    assert h['code'] == 'not_authenticated'
    with pytest.raises((EOFError, ConnectionError)):
        P.recv_frame(s, idle_timeout=5)
    s.close()
    # hello, then a request instead of auth
    s = board.raw()
    P.send_frame(s, {'type': 'hello', 'id': 1, 'protocol': 1, 'label': 'x', 'role': 'run'})
    h, _ = P.recv_frame(s, idle_timeout=5)
    assert h['type'] == 'challenge'
    P.send_frame(s, {'type': 'submit', 'id': 2})
    h, _ = P.recv_frame(s, idle_timeout=5)
    assert h['code'] == 'not_authenticated'
    s.close()


def test_unsupported_protocol(board):
    s = board.raw()
    P.send_frame(s, {'type': 'hello', 'id': 1, 'protocol': 99, 'label': 'x'})
    h, _ = P.recv_frame(s, idle_timeout=5)
    assert h['code'] == 'unsupported_protocol' and h['supported'] == [1]
    s.close()


def test_insecure_only_with_fake(tmp_path):
    st = ImageStore(str(tmp_path / 'i'))
    with pytest.raises(ValueError):
        BoardServer(QickSocBackend(), st, {'insecure_fake': True, 'lock_file': None})
    with pytest.raises(ValueError):
        BoardServer(FakeBackend(), st, {'lock_file': None}, tokens=None)


def test_unknown_type_answered(board):
    c = board.client()
    with pytest.raises(BoardError) as e:
        c.request('frobnicate')
    assert e.value.code == 'unknown_type'
    # the connection is still usable
    assert c.get_config()['board'] == 'ZCU216'


# --- jobs ---------------------------------------------------------------------

def test_submit_chunks_done(board):
    c = board.client()
    soccfg = c.get_config()
    prog = make_prog(soccfg, reps=50, loop=8, two_reads=True)   # 400 shots x 2 reads
    job = c.submit(prog, rounds=3, stride=37, ref='my-run-1')
    done = job.wait(20)
    assert done['outcome'] == 'complete', done
    assert done['ref'] == 'my-run-1' and done['outputs_idle'] is True
    lay = job.layout
    assert lay['loop_dims'] == [50, 8] and lay['readouts'][0]['reads_per_shot'] == 2
    seqs = sorted(job.chunks)
    assert seqs == list(range(len(seqs))) and not job.gaps
    rounds = job.raw_rounds()
    for r, rr in enumerate(rounds):
        assert rr['missing'] == []
        assert np.array_equal(rr['acc'][0].reshape(-1, 2), expected_round(r, 800))
    # result() equals the library's own processing of the same raw data
    res = job.result(prog)
    ref = make_prog(soccfg, reps=50, loop=8, two_reads=True)
    ref.acquire_params = {'type': 'accumulated', 'threshold': None, 'angle': None, 'remove_offset': True}
    ref.rounds_buf = [ref._process_accumulated([expected_round(r, 800).reshape(50, 8, 2, 2).astype(np.int64)])
                      for r in range(3)]
    expect = ref._summarize_accumulated(ref.rounds_buf)
    assert res[0].shape == (2, 8, 2)
    assert np.allclose(res[0], expect[0])
    # the same with a program rebuilt from the submitted dictionary
    assert np.allclose(job.result()[0], expect[0])
    # threshold path
    shots = job.result(prog, threshold=0.0, angle=0.3)
    assert shots[0].shape == (2, 8, 2)
    # what the backend did, in order (prepare_round order of the lab fork)
    calls = board.backend.soc.calls
    assert calls.index('clear_tproc_counter') < calls.index('reload_mem') < calls.index('start_tproc')
    assert 'reset_gens' in calls


def test_decimated(board):
    c = board.client()
    prog = make_prog(c.get_config(), reps=1, loop=3)
    job = c.submit(prog, rounds=2, mode='dec')
    assert job.wait(10)['outcome'] == 'complete'
    rr = job.raw_rounds()
    n = prog.ro_chs[0]['length'] * 3
    assert np.array_equal(rr[1]['dec'][0], fake_dec_values(0, 1, n))
    res = job.result(prog)
    ref = make_prog(c.get_config(), reps=1, loop=3)
    ref.acquire_params = {'type': 'decimated', 'remove_offset': True}
    ref.rounds_buf = [ref._process_decimated([fake_dec_values(0, r, n)]) for r in range(2)]
    assert np.allclose(res[0], ref._summarize_decimated(ref.rounds_buf)[0])


def test_program_cache(board):
    c = board.client()
    prog = make_prog(c.get_config(), reps=5, loop=2)
    h = c.put_prog(prog)
    assert h == BoardClient.pack_program(prog)[2]
    assert c.submit(prog).wait(10)['outcome'] == 'complete'
    with pytest.raises(BoardError) as e:
        c.request('submit', kind='qick_prog', prog_ref='0' * 64, run={})
    assert e.value.code == 'unknown_program'
    # another client's image load clears the cache; the client falls back to sending it inline
    d = board.client('other')
    d.load_image('fake-a')
    assert c.submit(prog).wait(10)['outcome'] == 'complete'


def test_bad_program_refused(board):
    c = board.client()
    with pytest.raises(BoardError) as e:
        c.request('submit', kind='qick_prog', prog={'envelopes': []}, arrays=[], run={})
    assert e.value.code == 'program_format' and e.value.header['field']
    with pytest.raises(BoardError) as e:
        c.submit(make_prog(c.get_config(), reps=2, loop=0), rounds=0)
    assert e.value.code == 'bad_request' and e.value.header['field'] == 'run.rounds'
    with pytest.raises(BoardError) as e:
        c.request('submit', kind='qick_prog', prog_ref='x', run={'reduce': 'sum'})
    assert e.value.code == 'unsupported'


def test_reconnect_and_fetch_resumes_without_gap(board):
    c = board.client('first')
    prog = make_prog(c.get_config(), reps=300, loop=10)     # 3000 shots
    job = c.submit(prog, rounds=1, stride=100, fake={'shot_rate': 6000})
    wait_for(lambda: len(job.chunks) >= 3)
    c.abort()                       # the client vanishes; the job keeps running
    got_before = set(job.chunks)
    wait_for(lambda: board.srv.jobs.get(job.id).state == 'done')
    c2 = board.client('second')
    j2 = c2.fetch(job)              # continue the same Job object from its next_seq
    assert j2 is job
    assert j2.wait(10)['outcome'] == 'complete'
    assert set(job.chunks) == set(range(job.done['totals']['chunks']))
    assert not job.gaps and got_before < set(job.chunks)
    rr = job.raw_rounds()[0]
    assert rr['missing'] == []
    assert np.array_equal(rr['acc'][0].reshape(-1, 2), expected_round(0, 3000))
    # acked chunks are freed: fetching from 0 again is refused
    with pytest.raises(BoardError) as e:
        c2.fetch(job.id, from_seq=0)
    assert e.value.code == 'seq_freed'


def test_absent_client_bound_stop(board_factory):
    b = board_factory(cfg={'max_queue_bytes': 8000})
    c = b.client(auto_ack=False)
    prog = make_prog(c.get_config(), reps=500, loop=10)     # 5000 shots, >= 800 B per chunk
    job = c.submit(prog, stride=100, limits={'max_queue_bytes': 8000, 'policy': 'stop'},
                   fake={'shot_rate': 20000})
    c.abort()                        # nobody reads or acks
    sj = b.srv.jobs.get(job.id)
    wait_for(lambda: sj.state == 'done')
    assert sj.outcome == 'incomplete' and sj.reason == 'queue_bound'
    assert sj.overruns == []         # the drain was never stalled by the network
    c2 = b.client('back')
    j = c2.fetch(job.id, from_seq=0)
    done = j.wait(10)
    bs = done['totals']['bound_stop']
    assert bs['bound'] == 8000 and bs['seq'] >= 5
    # exactly the chunks before the stop are there, they cover shots [0, stop shot), within the bound
    assert sorted(j.chunks) == list(range(bs['seq']))
    nbytes = sum(a.nbytes for h, arrs in j.chunks.values() for a in arrs.values())
    assert nbytes == bs['queued_bytes'] <= 8000
    stopped = [e for e in j.events if e['kind'] == 'stopped'][0]
    assert stopped['reason'] == 'queue_bound' and stopped['from_shot'] == bs['shot']
    rr = j.raw_rounds()[0]
    assert rr['missing'] == [[bs['shot'], 5000]]
    assert np.array_equal(rr['acc'][0].reshape(-1, 2)[:bs['shot']], expected_round(0, bs['shot']))
    with pytest.raises(IncompleteResult):
        j.result()


def test_slow_client_drop_oldest_reported_exactly(board_factory):
    b = board_factory(cfg={'max_queue_bytes': 8000})
    c = b.client()
    prog = make_prog(c.get_config(), reps=400, loop=10)     # 4000 shots -> ~40 chunks of >= 800 B

    def on_chunk(h, arrays):
        time.sleep(0.05)             # the client takes 50 ms per chunk: 2 s in all
    job = c.submit(prog, stride=100, limits={'max_queue_bytes': 8000, 'policy': 'drop_oldest'},
                   fake={'shot_rate': 40000}, on_chunk=on_chunk)
    sj = b.srv.jobs.get(job.id)
    wait_for(lambda: sj.state == 'done', 5)
    t_drained = time.monotonic()
    assert job.done is None          # the board finished long before the client did
    assert sj.overruns == []
    done = job.wait(20)
    assert time.monotonic() - t_drained > 0.3
    assert done['outcome'] == 'incomplete' and done['reason'] == 'dropped'
    total = done['totals']['chunks']
    assert total >= 20
    dropped = [d for e in job.events if e['kind'] == 'dropped' for d in e['chunks']]
    dropped_seqs = {d['seq'] for d in dropped}
    ranges = done['totals']['dropped']
    assert dropped_seqs == {s for a, e in ranges for s in range(a, e)}
    received = set(job.chunks)
    assert received | dropped_seqs == set(range(total))
    shots = {}
    for d in dropped:
        # a dropped chunk was received only if the board had already sent it
        assert (d['seq'] in received) == d['sent']
        shots[d['seq']] = d['shots']
    for seq, (h, _) in job.chunks.items():
        shots[seq] = h['shots']
    # in seq order the chunks tile the round: nothing lost without a report
    edges = [shots[k] for k in range(total)]
    assert edges[0][0] == 0 and edges[-1][1] == 4000
    assert all(edges[k][1] == edges[k + 1][0] for k in range(total - 1))
    for seq, (h, arrays) in job.chunks.items():
        a, e = h['shots']
        assert np.array_equal(arrays['acc:0'], fake_acc_values(0, 0, np.arange(a, e)))


def test_firmware_overrun_reported(board_factory):
    b = board_factory(load='fake-b')      # ring of 1024 points
    c = b.client()
    prog = make_prog(c.get_config(), reps=1000, loop=10)    # 10000 shots
    # 100k shots/s and 20 ms per read: 2000 shots arrive during each read
    job = c.submit(prog, stride=200, fake={'shot_rate': 1e5, 'read_delay_s': 0.02})
    done = job.wait(20)
    assert done['outcome'] == 'incomplete' and done['reason'] == 'overrun'
    ov = job.events_of('overrun')
    assert ov and done['totals']['overruns'] == [{'round': 0, 'shots': e['shots']} for e in ov]
    covered = np.zeros(10000, dtype=int)
    for e in ov:
        a, z = e['shots']
        assert a < z
        covered[a:z] += 1
    for seq, (h, arrays) in job.chunks.items():
        a, z = h['shots']
        covered[a:z] += 1
        # nothing overwritten is ever delivered as data
        assert np.array_equal(arrays['acc:0'], fake_acc_values(0, 0, np.arange(a, z)))
    assert (covered == 1).all()       # every shot is either delivered or reported lost, once
    # what the client sees as missing is exactly the reported overruns (merged where adjacent)
    lost = np.zeros(10000, dtype=bool)
    for e in ov:
        lost[e['shots'][0]:e['shots'][1]] = True
    missing = np.zeros(10000, dtype=bool)
    for a, z in job.raw_rounds()[0]['missing']:
        missing[a:z] = True
    assert (lost == missing).all()


def test_queue_flag_error_surfaced(board):
    c = board.client()
    job = c.submit(make_prog(c.get_config(), reps=20, loop=2), rounds=3,
                   fake={'queue_error': {'round': 1, 'queues': ['data']}})
    done = job.wait(10)
    assert done['outcome'] == 'failed'
    err = done['error']
    assert err['code'] == 'tproc_queue_overflow' and err['queues'] == ['data']
    assert 'data' in err['message'] and 'queue' in err['message']
    assert 'TprocQueueError' in err['traceback']
    ev = job.events_of('error')[0]
    assert ev['code'] == 'tproc_queue_overflow' and ev['traceback']
    # round 0 was delivered before the failure
    assert {h['round'] for h, _ in job.chunks.values()} == {0, 1}
    assert done['outputs_idle']


def test_exception_mid_run_surfaced(board):
    c = board.client()
    job = c.submit(make_prog(c.get_config(), reps=300, loop=10), stride=100,
                   fake={'exception_at_shot': 1500, 'shot_rate': 20000})
    done = job.wait(10)
    assert done['outcome'] == 'failed' and done['error']['code'] == 'job_exception'
    assert 'injected exception at shot 1500' in done['error']['traceback']
    assert 'Traceback' in done['error']['traceback']
    st = c.status()
    assert st['last_error']['job'] == job.id


def test_cancel_and_time_limit(board):
    c = board.client()
    prog = make_prog(c.get_config(), reps=1000, loop=10)
    job = c.submit(prog, stride=100, fake={'shot_rate': 2000})
    wait_for(lambda: len(job.chunks) >= 1)
    assert c.cancel(job) == 'running'
    done = job.wait(10)
    assert done['outcome'] == 'cancelled' and done['reason'] == 'client' and done['outputs_idle']
    job = c.submit(prog, stride=100, time_limit_s=0.2, fake={'shot_rate': 2000})
    done = job.wait(10)
    assert done['outcome'] == 'timed_out'
    # cancelling a queued job
    board.srv.jobs.hold(True)
    q = c.submit(prog)
    assert c.cancel(q) == 'queued'
    assert q.wait(5)['outcome'] == 'cancelled'
    board.srv.jobs.hold(False)


def test_back_to_back(board):
    c = board.client()
    p1 = make_prog(c.get_config(), reps=200, loop=10)
    j1 = c.submit(p1, stride=200, fake={'shot_rate': 20000})
    j2 = c.submit(p1, stride=200)
    assert j2.accepted['position'] == 1
    d1, d2 = j1.wait(10), j2.wait(10)
    assert d1['outcome'] == d2['outcome'] == 'complete'
    assert d2['times']['started'] >= d1['times']['finished']
    assert d2['times']['started'] - d1['times']['finished'] < 0.5


def test_subscriber_does_not_slow_owner(board):
    c = board.client('owner')
    w = board.client('watcher', auto_ack=False)
    prog = make_prog(c.get_config(), reps=300, loop=10)
    job = c.submit(prog, stride=100, fake={'shot_rate': 10000})   # 3000 shots, ~0.3 s
    sub = w.subscribe(job.id, newest_only=False)
    sub.on_chunk = lambda h, a: time.sleep(0.1)                  # a slow watcher
    t0 = time.monotonic()
    assert job.wait(10)['outcome'] == 'complete'
    t_owner = time.monotonic() - t0
    assert t_owner < 1.5
    assert sub.done is None                                       # still busy
    assert sub.wait(20)['outcome'] == 'complete'
    assert sub.done['subscriber'] is True
    seqs = [h['seq'] for h, _ in (sub.chunks[k] for k in sorted(sub.chunks))]
    assert seqs == sorted(seqs)
    assert len(job.chunks) == job.done['totals']['chunks'] and not job.gaps
    with pytest.raises(BoardError) as e:
        w.request('ack', job=job.id, seq=0)
    # ack has no reply; a refused ack does
    assert e.value.code == 'not_owner'


def test_subscribe_newest_only(board):
    c = board.client('owner', auto_ack=False)        # keeps every chunk retained
    prog = make_prog(c.get_config(), reps=100, loop=10)
    job = c.submit(prog, stride=100)
    job.wait(10)
    n = job.done['totals']['chunks']
    w = board.client('watcher')
    newest = w.subscribe(job.id, newest_only=True, from_now=False)
    newest.wait(5)
    assert sorted(newest.chunks) == [n - 1]
    w2 = board.client('watcher2')
    every = w2.subscribe(job.id, newest_only=False, from_now=False)
    every.wait(5)
    assert sorted(every.chunks) == list(range(n))


def test_status_and_state(board):
    c = board.client('status-test')
    st = c.status()
    assert any(cl['label'] == 'status-test' for cl in st['clients'])
    assert st['image']['name'] == 'fake-a'
    s = c.get_state()
    assert s['tproc']['queue_flags'] == {'supported': True, 'lost': []}
    assert 'status' in s['tproc'] and 'axi_regs' in s['tproc'] and 'debug' in s['tproc']


# --- images -------------------------------------------------------------------

def _write_fake_image(tmp_path, name, mutate=None):
    bit, hwh = fake_image_files('fake-a')
    doc = json.loads(hwh)
    if mutate:
        mutate(doc)
    bp = str(tmp_path / (name + '.bit'))
    hp = str(tmp_path / (name + '.hwh'))
    with open(bp, 'wb') as f:
        f.write(bit + name.encode())
    with open(hp, 'w') as f:
        json.dump(doc, f)
    return bp, hp


def test_image_put_list_load(board, tmp_path):
    c = board.client('loader')

    def mutate(doc):
        doc['fake_soccfg']['fw_timestamp'] = 'uploaded'
        doc['fake_soccfg']['readouts'][0]['avg_maxlen'] = 4096
    bp, hp = _write_fake_image(tmp_path, 'up1', mutate)
    meta = c.put_image('up1', bp, hp, chunk_bytes=1000)      # several chunks
    assert meta['name'] == 'up1' and len(meta['hash']) == 64
    names = [m['name'] for m in c.list_images()['images']]
    assert names == ['fake-a', 'fake-b', 'fake-rf', 'fake-w', 'up1']
    rec = c.load_image(meta['hash'][:12])
    assert rec['name'] == 'up1' and rec['hash'] == meta['hash']
    assert rec['bit_sha256'] == meta['bit_sha256'] and rec['hwh_sha256'] == meta['hwh_sha256']
    assert rec['requested_by'] == 'loader' and rec['duration_s'] > 0 and rec['t'] > 0
    assert rec['reset'] == {'pl_reconfigured': True, 'clocks_reprogrammed': False, 'converters_restarted': True,
                            'tproc_memories_cleared': True, 'envelopes_cleared': True,
                            'program_cache_cleared': True, 'rf': None}
    assert rec['cancelled_jobs'] == []
    assert rec['soccfg']['readouts'][0]['avg_maxlen'] == 4096
    assert c.get_config()['fw_timestamp'] == 'uploaded'
    # A1: the image loaded before, and a unique load id also in current_image and the welcome
    assert rec['previous']['name'] == 'fake-a' and rec['previous']['hash'] == board.images.resolve('fake-a')['hash']
    assert rec['previous']['load_id'] and rec['load_id'] and rec['load_id'] != rec['previous']['load_id']
    cur = c.current_image()
    assert cur['hash'] == meta['hash'] and cur['requested_by'] == 'loader'
    assert cur['load_id'] == rec['load_id'] and cur['previous'] == rec['previous']
    assert board.client('w').welcome['firmware']['image']['load_id'] == rec['load_id']
    # the soccfg is cached beside the image
    assert [m for m in c.list_images()['images'] if m['name'] == 'up1'][0]['has_soccfg']
    rec_b = c.load_image('fake-b')
    assert rec_b['reset']['clocks_reprogrammed'] is True
    # same name, same content: idempotent; different content: refused
    assert c.put_image('up1', bp, hp)['hash'] == meta['hash']
    bp2, hp2 = _write_fake_image(tmp_path, 'up1x')
    with pytest.raises(BoardError) as e:
        c.put_image('up1', bp2, hp2)
    assert e.value.code == 'name_exists'
    with pytest.raises(BoardError) as e:
        c.load_image('nope')
    assert e.value.code == 'unknown_image'


def test_image_hash_mismatch(board, tmp_path):
    c = board.client()
    bp, hp = _write_fake_image(tmp_path, 'bad')
    with pytest.raises(BoardError) as e:
        c.put_image('bad', bp, hp, bit_sha256='0' * 64)
    assert e.value.code == 'hash_mismatch' and e.value.header['files'] == ['bit']
    assert 'bad' not in [m['name'] for m in c.list_images()['images']]
    assert os.listdir(board.images.upload_root) == []


def test_upload_discarded_when_client_vanishes(board, tmp_path):
    c = board.client()
    bp, hp = _write_fake_image(tmp_path, 'half')
    r = c.request('put_image', phase='begin', name='half', bit_sha256='a' * 64, hwh_sha256='b' * 64,
                  bit_size=10, hwh_size=10)
    assert board.images.uploads
    # vanish in the middle of a frame
    c._sock.sendall(struct.pack('<II', 50, 10) + b'{"type":')
    c.abort()
    wait_for(lambda: not board.images.uploads)
    assert os.listdir(board.images.upload_root) == []
    # the server is fine
    assert board.client('next').get_config()


def test_load_refused_while_job_queued_or_running(board):
    c = board.client('runner')
    other = board.client('operator')
    prog = make_prog(c.get_config(), reps=1000, loop=10)
    running = c.submit(prog, stride=500, fake={'shot_rate': 5000}, ref='long')
    wait_for(lambda: board.srv.jobs.get(running.id).state == 'running')
    for force in (False, True):
        with pytest.raises(BoardError) as e:
            other.load_image('fake-b', force=force)
        assert e.value.code == 'job_running' and e.value.header['job'] == running.id
    c.cancel(running)
    running.wait(10)
    board.srv.jobs.hold(True)
    q1 = c.submit(prog, ref='queued-1')
    q2 = c.submit(prog, ref='queued-2')
    with pytest.raises(BoardError) as e:
        other.load_image('fake-b')
    assert e.value.code == 'jobs_queued'
    assert [(x['job'], x['submitter'], x['ref']) for x in e.value.header['jobs']] == \
        [(q1.id, 'runner', 'queued-1'), (q2.id, 'runner', 'queued-2')]
    rec = other.load_image('fake-b', force=True)
    assert [(x['job'], x['submitter'], x['ref']) for x in rec['cancelled_jobs']] == \
        [(q1.id, 'runner', 'queued-1'), (q2.id, 'runner', 'queued-2')]
    for q in (q1, q2):
        d = q.wait(5)
        assert d['outcome'] == 'cancelled' and d['reason'] == 'image_load'
    board.srv.jobs.hold(False)


def test_job_image_guard_off(board):
    c = board.client()
    hb = board.images.resolve('fake-b')['hash']
    ha = board.images.resolve('fake-a')['hash']
    prog = make_prog(c.get_config(), reps=5, loop=2)
    assert c.submit(prog, image='fake-a').wait(10)['outcome'] == 'complete'
    with pytest.raises(BoardError) as e:
        c.submit(prog, image=hb)
    assert e.value.code == 'image_mismatch'
    assert e.value.header['wanted'] == hb and e.value.header['loaded'] == ha
    assert e.value.header['wanted_name'] == 'fake-b' and e.value.header['loaded_name'] == 'fake-a'
    assert c.current_image()['name'] == 'fake-a'


def test_job_image_guard_on(board_factory):
    b = board_factory(cfg={'allow_job_image_load': True})
    c = b.client('auto')
    prog = make_prog(c.get_config(), reps=100, loop=10)     # 1000 shots
    job = c.submit(prog, image='fake-b', stride=100)
    assert job.accepted['image_load_first'] == b.images.resolve('fake-b')['hash']
    done = job.wait(10)
    assert done['outcome'] == 'complete'
    ev = job.events_of('image_loaded')[0]
    assert ev['image']['name'] == 'fake-b' and ev['image']['requested_by'] == 'auto'
    assert ev['image']['reason'] == 'job %s' % job.id
    assert c.current_image()['name'] == 'fake-b'
    assert c.get_config(refresh=True)['readouts'][0]['avg_maxlen'] == 1024
    assert np.array_equal(job.raw_rounds()[0]['acc'][0].reshape(-1, 2), expected_round(0, 1000))


# --- board ownership ----------------------------------------------------------

def test_lock_file(tmp_path):
    path = str(tmp_path / 'lock')
    assert board_lock_holder(path) is None
    f = acquire_lock_file(path, {'pid': 123})
    with pytest.raises(RuntimeError, match='owned by another process'):
        acquire_lock_file(path, {'pid': 456})
    assert '123' in board_lock_holder(path)
    f.close()
    assert board_lock_holder(path) is None


def test_second_server_refused(board):
    with pytest.raises(RuntimeError, match='owned'):
        BoardServer(FakeBackend(), board.images, {'port': 0, 'lock_file': board.srv.cfg['lock_file']},
                    tokens=[('run', TOKEN)]).start()


# --- the drain on its own -----------------------------------------------------

def _soc(ring=None, **inject):
    with open(os.path.join(REPO, 'qick_lib', 'qick', 'boardserver', 'fake_soccfg.json')) as f:
        cfg = json.load(f)
    if ring:
        cfg['readouts'][0]['avg_maxlen'] = ring
    return FakeSoc(cfg, **inject)


def test_iter_accumulated_detects_overwrite_during_read():
    soc = _soc(ring=1000, shot_rate=1e5, read_delay_s=0.01)
    soc.fake_round_info(0, 20000, [0], [1])
    soc.clear_tproc_counter(1)
    items = list(iter_accumulated(soc, 20000, 1, [0], [1], stride=300))
    kinds = {i[0] for i in items}
    assert 'overrun' in kinds
    whens = {i[3]['when'] for i in items if i[0] == 'overrun'}
    assert whens <= {'before_read', 'during_read'}
    seen = np.zeros(20000, dtype=int)
    for it in items:
        if it[0] == 'data':
            _, a, z, bufs, _ = it
            assert np.array_equal(bufs[0], fake_acc_values(0, 0, np.arange(a, z)))
            seen[a:z] += 1
        elif it[0] == 'overrun':
            seen[it[1]:it[2]] += 1
    assert (seen == 1).all()


def test_datastreamer_still_works_and_fails_on_overrun():
    soc = _soc(shot_rate=1e5)
    soc.fake_round_info(0, 3000, [0], [1])
    soc.clear_tproc_counter(1)
    ds = DataStreamer(soc)
    ds.job_queue.put((3000, 1, [0], [1], None))
    got = []
    n = 0
    while n < 3000:
        k, (d, s) = ds.data_queue.get(timeout=5)
        got.append(d[0])
        n += k
    assert np.array_equal(np.concatenate(got), fake_acc_values(0, 0, np.arange(3000)))
    ds.done_flag.wait(5)
    soc2 = _soc(ring=1000, shot_rate=1e5, read_delay_s=0.02)
    soc2.fake_round_info(0, 20000, [0], [1])
    soc2.clear_tproc_counter(1)
    ds2 = DataStreamer(soc2)
    ds2.job_queue.put((20000, 1, [0], [1], 100))
    err = ds2.error_queue.get(timeout=10)
    assert 'Overflowed the averages buffer' in str(err)


# --- tProc register dumps as data --------------------------------------------

def test_tproc_getters(monkeypatch):
    import types
    overlay = types.ModuleType('pynq.overlay')

    class _MMIO:
        def __init__(self):
            self.array = np.zeros(16, dtype=np.uint32)

    class DefaultIP:
        def __init__(self, description):
            self.mmio = _MMIO()
    overlay.DefaultIP = DefaultIP
    buffer = types.ModuleType('pynq.buffer')
    buffer.allocate = lambda *a, **k: None
    pynq = types.ModuleType('pynq')
    pynq.overlay, pynq.buffer = overlay, buffer
    for name, mod in (('pynq', pynq), ('pynq.overlay', overlay), ('pynq.buffer', buffer)):
        monkeypatch.setitem(sys.modules, name, mod)
    for name in ('qick.ip', 'qick.drivers.tproc'):
        monkeypatch.delitem(sys.modules, name, raising=False)
    from qick.drivers.tproc import Axis_QICK_Proc
    params = {'PMEM_AW': 12, 'DMEM_AW': 14, 'WMEM_AW': 10, 'REG_AW': 4, 'IN_PORT_QTY': 1, 'OUT_TRIG_QTY': 20,
              'OUT_DPORT_QTY': 1, 'OUT_DPORT_DW': 8, 'OUT_WPORT_QTY': 16, 'LFSR': 1, 'DIVIDER': 1, 'ARITH': 1,
              'TIME_READ': 1, 'QCOM': 0, 'CUSTOM_PERIPH': 1, 'IO_CTRL': 1, 'EXT_FLAG': 0, 'QNET': 0,
              'FIFO_DEPTH': 9, 'CALL_DEPTH': 255, 'DEBUG': 0, 'FIFO_GUARD': 8}
    tp = Axis_QICK_Proc({'type': 'QICK:QICK:qick_processor:2.0', 'fullpath': 'qick_processor_0',
                         'parameters': {k: str(v) for k, v in params.items()}})
    tp.mmio.array[14] = 0b101 | (0b100 << 4)     # core C_RUN, time T_RUN
    tp.mmio.array[9] = 0x209
    st = tp.get_status()
    assert st['core_state'] == 'C_RUN' and st['time_state'] == 'T_RUN' and st['raw'] == tp.mmio.array[14]
    regs = tp.get_axi_regs()
    assert regs['queue_flags'] == 0x209 and set(regs) == set(tp.REGISTERS)
    dbg = tp.get_debug()
    assert set(dbg) >= {'div', 'arith', 'fifo', 'memory', 'time', 'port'}
    assert tp.get_queue_overflows() == ['data']
    json.dumps([st, regs, dbg])
    for name in ('qick.ip', 'qick.drivers.tproc'):
        sys.modules.pop(name, None)


# --- board-side Python version ------------------------------------------------

BOARD_SIDE = ['qick_lib/qick/boardserver/protocol.py', 'qick_lib/qick/boardserver/server.py',
              'qick_lib/qick/boardserver/jobs.py', 'qick_lib/qick/boardserver/images.py',
              'qick_lib/qick/boardserver/backend.py', 'qick_lib/qick/boardserver/__main__.py',
              'qick_lib/qick/boardserver/__init__.py', 'qick_lib/qick/boardserver/client.py',
              'qick_lib/qick/boardserver/rf.py',
              'qick_lib/qick/streamer.py', 'qick_lib/qick/drivers/tproc.py', 'qick_lib/qick/helpers.py']


@pytest.mark.parametrize('path', BOARD_SIDE)
def test_python38_syntax(path):
    src = open(os.path.join(REPO, path)).read()
    ast.parse(src, filename=path, feature_version=(3, 8))
    # newer-than-3.8 library calls and annotation forms that ast cannot catch
    for bad in ('.removeprefix(', '.removesuffix(', 'functools.cache(', '@cache\n', 'file_digest(',
                'zoneinfo', 'math.lcm(', '.bit_count(', 'tomllib'):
        assert bad not in src, '%s uses %s (needs Python > 3.8)' % (path, bad)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.arg)) and getattr(node, 'annotation', None) is not None:
            ann = ast.dump(node.annotation)
            assert 'BitOr' not in ann, '%s: X | Y annotation' % path
        if isinstance(node, ast.Match if hasattr(ast, 'Match') else ()):
            raise AssertionError('match statement in %s' % path)


def test_board_side_imports_only_stdlib_and_numpy():
    allowed_third_party = {'numpy'}
    for path in BOARD_SIDE:
        if 'client.py' in path:
            continue
        tree = ast.parse(open(os.path.join(REPO, path)).read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split('.')[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module.split('.')[0]]
            else:
                continue
            for n in names:
                if n in allowed_third_party or n in sys.stdlib_module_names or n == 'qick':
                    continue
                # pynq (tproc.py through qick.ip) and xrfdc (rf.py, the ADC flags) come with the
                # board's PYNQ image; QickSoc itself needs both
                assert n in ('pynq', 'xrfdc'), '%s imports %s' % (path, n)


# --- command line -------------------------------------------------------------

def _run_cli(args, timeout=20):
    return subprocess.run([sys.executable, '-m', 'qick.boardserver'] + args, capture_output=True, text=True,
                          timeout=timeout)


def test_cli_refuses_without_token(tmp_path):
    r = _run_cli(['--image-dir', str(tmp_path / 'i'), '--port', '0', '--lock-file', str(tmp_path / 'l')])
    assert r.returncode != 0 and 'no --token-file' in r.stderr
    r = _run_cli(['--insecure-fake', '--image-dir', str(tmp_path / 'i'), '--port', '0'])
    assert r.returncode != 0 and 'only allowed together with --fake' in r.stderr


def test_cli_fake_server(tmp_path):
    tf = str(tmp_path / 'token')
    r = _run_cli(['new-token', tf])
    assert r.returncode == 0
    assert oct(os.stat(tf).st_mode & 0o777) == '0o600'
    proc = subprocess.Popen([sys.executable, '-m', 'qick.boardserver', '--fake', '--token-file', tf,
                             '--image-dir', str(tmp_path / 'img'), '--port', '0', '--host', '127.0.0.1',
                             '--lock-file', str(tmp_path / 'lock')],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline()
        assert 'ready on port' in line, proc.stderr.read()
        port = int(line.split()[-1])
        c = BoardClient('127.0.0.1', port, token_file=tf, label='cli-test')
        assert c.welcome['fake'] is True
        imgs = c.list_images()
        assert [m['name'] for m in imgs['images']] == ['fake-a', 'fake-b', 'fake-rf', 'fake-w']
        assert c.current_image()['name'] == 'fake-a'
        job = c.submit(make_prog(c.get_config(), reps=10, loop=2))
        assert job.wait(10)['outcome'] == 'complete'
        c.close()
    finally:
        proc.terminate()
        out, err = proc.communicate(timeout=10)
    assert open(tf).read().split()[1] not in err      # the token is never logged


def test_cli_import_image(tmp_path):
    bit, hwh = fake_image_files('fake-a')
    (tmp_path / 'run.bit').write_bytes(bit)
    (tmp_path / 'run.hwh').write_bytes(hwh)
    r = _run_cli(['import-image', '--image-dir', str(tmp_path / 'img'), '--name', 'rollback',
                  '--bit', str(tmp_path / 'run.bit')])
    assert r.returncode == 0, r.stderr
    meta = json.loads(r.stdout)
    assert meta['name'] == 'rollback'
    assert ImageStore(str(tmp_path / 'img')).resolve('rollback')['hash'] == meta['hash']


# --- round 2: idle_after, image changes, decimated shapes -------------------

def test_idle_after(board):
    c = board.client()
    calls = board.backend.soc.calls
    prog = make_prog(c.get_config(), reps=20, loop=2)
    n0 = calls.count('reset_gens')
    done = c.submit(prog, idle_after=False).wait(10)
    assert done['outcome'] == 'complete' and done['outputs_idle'] is False
    assert done['idle']['skipped'] and 'idle_after' in done['outputs_idle_reason']
    assert calls.count('reset_gens') == n0
    assert c.get_state()['last_idle']['skipped']
    # still made idle on error, cancel and time limit
    done = c.submit(prog, rounds=2, idle_after=False, fake={'queue_error': {'round': 0, 'queues': ['data']}}).wait(10)
    assert done['outcome'] == 'failed' and done['outputs_idle'] is True
    assert done['outputs_idle_reason'] == 'error (tproc_queue_overflow)'
    long = make_prog(c.get_config(), reps=1000, loop=10)
    done = c.submit(long, idle_after=False, time_limit_s=0.1, fake={'shot_rate': 2000}).wait(10)
    assert done['outcome'] == 'timed_out' and done['outputs_idle'] is True
    j = c.submit(long, stride=100, idle_after=False, fake={'shot_rate': 2000})
    wait_for(lambda: len(j.chunks) >= 1)
    c.cancel(j)
    assert j.wait(10)['outputs_idle'] is True
    done = c.submit(prog).wait(10)
    assert done['outputs_idle'] is True and done['outputs_idle_reason'] == 'normal end of the job'
    with pytest.raises(BoardError) as e:
        c.request('submit', kind='qick_prog', prog_ref=c.put_prog(prog), run={'idle_after': 'no'})
    assert e.value.code == 'bad_request' and e.value.header['field'] == 'run.idle_after'


def test_queued_job_without_image_not_run_on_another(board_factory):
    b = board_factory(cfg={'allow_job_image_load': True})
    c = b.client('eng')
    prog = make_prog(c.get_config(), reps=10, loop=2)
    b.srv.jobs.hold(True)
    first = c.submit(prog, ref='first')                    # no image: runs on fake-a
    switch = c.submit(prog, image='fake-b', ref='switch')  # loads fake-b
    stale = c.submit(prog, ref='stale')                    # no image, built for fake-a
    pinned = c.submit(prog, image='fake-b', ref='pinned')
    b.srv.jobs.hold(False)
    assert first.wait(10)['outcome'] == 'complete'
    assert switch.wait(10)['outcome'] == 'complete'
    ev = switch.events_of('image_loaded')[0]['image']
    assert ev['cancelled_jobs'] == [{'job': stale.id, 'submitter': 'eng', 'ref': 'stale'}]
    assert ev['previous']['name'] == 'fake-a'
    d = stale.wait(10)
    assert d['outcome'] == 'cancelled' and d['reason'] == 'image_changed'
    assert pinned.wait(10)['outcome'] == 'complete'


class _LibSoc(FakeSoc):
    """FakeSoc driven by the library's own acquire_decimated (which does not call
    fake_round_info): rounds counted at prepare_round."""
    def __init__(self, cfg, total, reads):
        super().__init__(cfg)
        self._lib = (total, reads)
        self._n = -1

    def prepare_round(self):
        self._n += 1
        self.fake_round_info(self._n, self._lib[0], [0], [self._lib[1]])


@pytest.mark.parametrize('reps,loop,two_reads,shape', [
    (1, 0, False, 'L2'),          # single shot, single read: (length, 2)
    (1, 3, False, 'NL2'),         # multi-rep: (n_reps*n_reads, length, 2)
    (1, 0, True, 'NL2'),          # multi-read, one rep: (n_reps*n_reads, length, 2)
    (2, 3, True, 'NRL2'),         # multi-rep and multi-read: (n_reps, n_reads, length, 2)
])
def test_decimated_matches_library_acquire_decimated(board, reps, loop, two_reads, shape):
    c = board.client()
    cfg = c.get_config()
    prog = make_prog(cfg, reps=reps, loop=loop, two_reads=two_reads)
    res = c.submit(prog, rounds=3, mode='dec').result(prog)
    ref_prog = make_prog(cfg, reps=reps, loop=loop, two_reads=two_reads)
    total = int(np.prod(ref_prog.loop_dims))
    nreads = 2 if two_reads else 1
    soc = _LibSoc(cfg.get_cfg(), total, nreads)
    ref = ref_prog.acquire_decimated(soc, rounds=3, progress=False)
    assert soc._n == 2                                     # three rounds, averaged
    assert len(res) == len(ref) == 1
    assert res[0].shape == ref[0].shape and res[0].dtype == ref[0].dtype
    assert np.array_equal(res[0], ref[0])
    L = prog.ro_chs[0]['length']
    want = {'L2': (L, 2), 'NL2': (total * nreads, L, 2), 'NRL2': (total, nreads, L, 2)}[shape]
    assert res[0].shape == want


# --- D: weights over the wire -------------------------------------------------

class WProg(AveragerProgramV2):
    def _initialize(self, cfg):
        self.declare_gen(ch=0, nqz=1)
        self.declare_readout(ch=0, length=0.5, weights=cfg['weights'])
        self.add_readoutconfig(ch=0, name='ro', freq=100, gen_ch=0)
        self.add_pulse(ch=0, name='p', style='const', freq=100, phase=0, gain=0.5, length=0.1)
        self.add_loop('x', 2)

    def _body(self, cfg):
        self.send_readoutconfig(ch=0, name='ro', t=0)
        self.pulse(ch=0, name='p', t=0)
        self.trigger(ros=[0], t=0.2)


def _weight_model():
    import importlib.util
    path = os.path.join(REPO, 'slab', 'sim', 'weighted_buffer', 'model.py')
    spec = importlib.util.spec_from_file_location('wb_model', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_weights_round_trip(board):
    c = board.client()
    c.load_image('fake-w')
    cfg = c.get_config(refresh=True)
    assert cfg['readouts'][0]['has_weights'] and cfg['readouts'][0]['wgt_maxlen'] == 1024
    L = WProg(cfg, reps=1, final_delay=1.0, cfg={'weights': None}).ro_chs[0]['length']
    rng = np.random.default_rng(7)
    w = rng.integers(-30000, 30000, size=(L, 2)).astype(np.int16)
    prog = WProg(cfg, reps=1, final_delay=1.0, cfg={'weights': w})
    tree, arrays, h = BoardClient.pack_program(prog)
    assert any(a.dtype == np.int16 and a.shape == (L, 2) and np.array_equal(a, w) for _, a in arrays)
    job = c.submit(prog, mode='dec')
    assert job.wait(10)['outcome'] == 'complete'
    got = board.backend.soc.weights[0]
    assert got.dtype == np.int16 and np.array_equal(got, w)
    # the fake applies the firmware formula of slab/sim/weighted_buffer/model.py
    model = _weight_model()
    x = fake_dec_values(0, 0, 2 * L)
    want = model.weighted(x, w[np.arange(2 * L) % L]).astype(np.int32)
    assert np.array_equal(job.raw_rounds()[0]['dec'][0], want)
    # the library refuses weights on a readout without a weighted buffer (fake-a)
    with pytest.raises(RuntimeError):
        WProg(QickConfig(json.loads(fake_image_files('fake-a')[1])['fake_soccfg']), reps=1, final_delay=1.0, cfg={'weights': w})


# --- C: RF cards ----------------------------------------------------------------

def rf_board(board_factory, **cfg):
    return board_factory(cfg=cfg, load='fake-rf')


def test_rf_get_state_and_load_record(board_factory):
    b = rf_board(board_factory)
    c = b.client('eng')
    rf = c.rf_get()
    assert rf['cards']['adc'][2]['kind'] == 'rf_in' and rf['cards']['adc'][3]['kind'] == 'dc_in'
    assert rf['ros']['0']['kind'] == 'rf_in' and rf['ros']['1']['kind'] == 'dc_in'
    assert rf['gens']['0']['kind'] == 'rf_out'
    assert rf['adc_att'] == {'20': 27.0, '21': 27.0, '22': 27.0}          # read from the converter
    assert rf['adc_ports']['4']['amp_on'] is False and rf['adc_ports']['6']['amp_on'] is False
    assert rf['adc_ports']['4']['att'] == {'value': 31.75, 'by': 'amplifier off'}
    assert not any(f['over_range'] or f['over_voltage'] for f in rf['flags'].values())
    assert c.get_state()['rf']['adc_att'] == rf['adc_att']
    reset = c.current_image()['reset']['rf']
    assert reset['procedure'] == 'on' and reset['after']['ok'] and reset['after']['source'] == 'built-in default'
    assert reset['found_after_load']['adc_att']['20'] == 0.0              # the converter's state after a load
    assert c.welcome['capabilities']['rf'] is True
    # no RF on a plain image
    c.load_image('fake-a')
    with pytest.raises(BoardError) as e:
        c.rf_get()
    assert e.value.code == 'no_rf_board'


def test_rf_set_safe_order_and_readback(board_factory):
    b = rf_board(board_factory)
    c = b.client('eng')
    hw = b.backend.rf_hardware
    n_ev = len(hw.events)
    # given in an unsafe order; RF-ADC attenuator raised from 0 (as after a load without the procedure)
    hw.dsa['20'] = 0.0
    rec = c.rf_set([{'ro': 0, 'amp': True, 'att': 10},
                    {'gen': 0, 'att': [6, 8]},
                    {'ro': 0, 'filter': {'ftype': 'bandpass', 'fc': 6.0, 'bw': 1.0}},
                    {'gen': 0, 'filter': {'ftype': 'lowpass', 'fc': 8.0}},
                    {'ro': 0, 'adc_att': 20},
                    {'bias': 3, 'volts': 0.5}], settle_s=0.01)
    ops = [(e['phase'], e['op']) for e in rec['order']]
    assert ops == [('reduce', 'adc_att'), ('filters', 'filter'), ('filters', 'filter'), ('increase', 'dac_att'),
                   ('increase', 'adc_rf_on'), ('bias', 'bias')]
    assert rec['order'][1]['target'] == ['adc', 4]                         # readout filter before generator filter
    assert rec['order'][0]['result'] == {'set': 20.0, 'read': 20.0}
    assert rec['order'][4]['result']['amp_on'] is True
    assert rec['ok'] and rec['by'] == 'eng'
    assert not rec['flags_idle']['20']['over_range']
    assert len(hw.events) == n_ev                                          # never over range on the way
    rf = c.rf_get()
    assert rf['adc_att']['20'] == 20.0 and rf['adc_ports']['4']['amp_on'] is True
    assert rf['adc_ports']['4']['att'] == {'value': 10.0, 'by': 'rf_set'}
    assert rf['dac_ports']['0']['att'] == {'value': [6.0, 8.0], 'by': 'rf_set'}
    assert rf['adc_ports']['4']['filter'] != {'WR0_SW': 0xC0, 'WR0_FILTER': 0}
    assert rf['bias']['3'] == 0.5
    # lowering the RF-ADC attenuator with the amplifier on comes after everything else
    rec = c.rf_set([{'ro': 0, 'adc_att': 15}, {'gen': 0, 'att': [10, 10]}, {'ro': 0, 'amp': True, 'att': 12}],
                   settle_s=0)
    assert [(e['phase'], e['op']) for e in rec['order']] == [('reduce', 'dac_att'), ('reduce', 'adc_rf_on'),
                                                            ('increase', 'adc_att')]
    # DC In readout: gain, read back
    rec = c.rf_set([{'ro': 1, 'amp': True, 'gain': 10}], settle_s=0)
    assert rec['order'][0]['result']['gain'] == 10.0 and rec['order'][0]['result']['amp_on'] is True


def test_rf_set_flags_at_idle(board_factory):
    b = rf_board(board_factory)
    c = b.client('eng')
    with pytest.raises(BoardError) as e:
        c.rf_set([{'ro': 0, 'adc_att': 5}, {'ro': 0, 'amp': True, 'att': 10}], settle_s=0.01)
    assert e.value.code == 'adc_flags'
    rec = e.value.header['record']
    assert rec['flags_idle']['20']['over_range'] and not rec['flags_idle']['20']['over_voltage']
    assert rec['actions'] == [] and c.rf_get()['adc_ports']['4']['amp_on'] is True     # left on, reported
    # a job on an ADC in interrupt state is refused by prepare_round, as RFQickSoc216V1 does
    d = c.submit(make_prog(c.get_config(), reps=5, loop=2)).wait(10)
    assert d['outcome'] == 'failed' and d['error']['code'] == 'adc_interrupt'
    # over-voltage: the amplifier is switched off again
    with pytest.raises(BoardError) as e:
        c.rf_set([{'ro': 0, 'adc_att': 0}, {'ro': 0, 'amp': True, 'att': 0}], settle_s=0)
    rec = e.value.header['record']
    assert rec['flags_idle']['20']['over_voltage']
    assert rec['actions'][0]['op'] == 'adc_off' and rec['actions'][0]['amp_on'] is False
    assert c.rf_get()['adc_ports']['4']['amp_on'] is False


def test_rf_set_refusals(board_factory):
    b = rf_board(board_factory)
    c = b.client('eng')
    hw = b.backend.rf_hardware
    before = list(hw.calls)
    bad = [([{'ro': 0, 'att': 10}], 'bad_request'),                  # att without amp
           ([{'ro': 0, 'amp': True, 'att': 10.1}], 'bad_request'),     # not a 0.25 dB step
           ([{'ro': 0, 'adc_att': 28}], 'bad_request'),
           ([{'gen': 0, 'att': 5}], 'bad_request'),
           ([{'ro': 7, 'adc_att': 10}], 'bad_request'),
           ([{'ro': 1, 'amp': True, 'att': 10}], 'no_rf_board'),       # readout 1 is on a DC In card
           ([{'ro': 0, 'adc_att': 20}, {'ro': 0, 'amp': True, 'gain': 3}], 'no_rf_board'),
           ([{'bias': 9, 'volts': 1}], 'bad_request'),
           ([], 'bad_request')]
    for settings, code in bad:
        with pytest.raises(BoardError) as e:
            c.rf_set(settings)
        assert e.value.code == code, settings
    assert hw.calls == before                                         # nothing applied
    r = b.client('viewer', token=READ_TOKEN, role='read')
    assert r.rf_get()['adc_att']
    with pytest.raises(BoardError) as e:
        r.rf_set([{'ro': 0, 'adc_att': 20}])
    assert e.value.code == 'forbidden'
    # another client's running job
    long = make_prog(c.get_config(), reps=1000, loop=10)
    j = c.submit(long, stride=100, fake={'shot_rate': 2000})
    wait_for(lambda: b.srv.jobs.get(j.id).state == 'running')
    other = b.client('other')
    with pytest.raises(BoardError) as e:
        other.rf_set([{'ro': 0, 'adc_att': 20}])
    assert e.value.code == 'job_running' and e.value.header['job'] == j.id
    assert c.rf_set([{'ro': 0, 'adc_att': 26}], settle_s=0)['ok']      # the job's own client may
    c.cancel(j)
    j.wait(10)
    c.load_image('fake-a')
    with pytest.raises(BoardError) as e:
        c.rf_set([{'ro': 0, 'adc_att': 20}])
    assert e.value.code == 'no_rf_board'


def _amp_on(c):
    rec = c.rf_set([{'ro': 0, 'adc_att': 20}, {'ro': 0, 'amp': True, 'att': 10}], settle_s=0)
    assert rec['ok']


def test_rf_load_bad_case_without_procedure(board_factory):
    b = rf_board(board_factory, rf_safe_on_load=False)
    c = b.client('eng')
    assert c.current_image()['reset']['rf']['procedure'] == 'off'
    _amp_on(c)
    hw = b.backend.rf_hardware
    n_ev = len(hw.events)
    rec = c.load_image('fake-rf')
    rf = rec['reset']['rf']
    assert rf['before'] == {'skipped': True, 'why': 'RF procedure off (--no-rf-safe-on-load)'} and rf['after'] is None
    found = rf['found_after_load']
    assert found['adc_att']['20'] == 0.0 and found['adc_ports']['4']['amp_on'] is True
    assert found['flags']['20']['over_range']
    assert any(ev['why'].startswith('PL load') for ev in hw.events[n_ev:])
    d = c.submit(make_prog(c.get_config(), reps=5, loop=2)).wait(10)
    assert d['outcome'] == 'failed' and d['error']['code'] == 'adc_interrupt'


def test_rf_load_with_procedure(board_factory):
    b = rf_board(board_factory)
    c = b.client('eng')
    _amp_on(c)
    hw = b.backend.rf_hardware
    n_ev = len(hw.events)
    rec = c.load_image('fake-rf')
    rf = rec['reset']['rf']
    assert rf['procedure'] == 'on'
    assert {(e['port'], e['amp_on']) for e in rf['before']['done']} == {(4, False), (5, False), (6, False), (7, False)}
    found = rf['found_after_load']
    assert found['adc_att']['20'] == 0.0 and found['adc_ports']['4']['amp_on'] is False
    assert not found['flags']['20']['over_range']
    after = rf['after']
    assert after['ok'] and after['adc_att'] == {'20': 27.0, '21': 27.0, '22': 27.0}
    assert all(e['amp_on'] is False for e in after['amplifiers_off'])
    assert not any(f['over_range'] for f in after['flags_idle'].values())
    assert rf['state']['adc_att']['20'] == 27.0
    assert hw.events[n_ev:] == []                                      # never overdriven
    assert c.submit(make_prog(c.get_config(), reps=5, loop=2)).wait(10)['outcome'] == 'complete'
    # leaving the RF image: the before-half still runs
    _amp_on(c)
    rf = c.load_image('fake-a')['reset']['rf']
    assert rf['before']['ok'] and rf['after'] is None and rf['found_after_load'] is None
    assert hw.amp[4] is False


def test_rf_safe_failed_refuses_load(board_factory):
    b = rf_board(board_factory)
    c = b.client('eng')
    _amp_on(c)
    hw = b.backend.rf_hardware
    loads = hw.pl_loads
    stuck = b.backend.rf.hw
    real_off = stuck.adc_off
    stuck.adc_off = lambda port: None if port == 4 else real_off(port)    # the switch does not respond
    with pytest.raises(BoardError) as e:
        c.load_image('fake-b')
    assert e.value.code == 'rf_safe_failed'
    assert [d for d in e.value.header['record']['done'] if d['port'] == 4][0]['amp_on'] is True
    assert hw.pl_loads == loads and c.current_image()['name'] == 'fake-rf'   # nothing loaded
    rec = c.load_image('fake-b', skip_rf_safe=True)
    assert rec['reset']['rf']['before'] == {'skipped': True, 'why': 'skip_rf_safe in the request'}
    assert c.current_image()['name'] == 'fake-b'


def test_rf_safe_state_file(board_factory):
    b = rf_board(board_factory)
    c = b.client('eng')
    with pytest.raises(BoardError) as e:
        c.put_rf_safe('fake-rf', [{'ro': 0, 'adc_att': 99}])
    assert e.value.code == 'bad_request'
    c.put_rf_safe('fake-rf', [{'ro': 0, 'adc_att': 20}, {'ro': 0, 'filter': {'ftype': 'bandpass', 'fc': 6.0, 'bw': 1.0}},
                              {'gen': 0, 'att': [10, 12]}], settle_s=0.01)
    assert [m for m in c.list_images()['images'] if m['name'] == 'fake-rf'][0]['has_rf_safe']
    after = c.load_image('fake-rf')['reset']['rf']['after']
    assert after['ok'] and after['source'] == 'safe-state file' and after['settle_s'] == 0.01
    assert after['adc_att'] == {'20': 20.0, '21': 27.0, '22': 27.0}
    rf = c.rf_get()
    assert rf['adc_ports']['4']['amp_on'] is False                     # not turned on by the file
    assert rf['dac_ports']['0']['att'] == {'value': [10.0, 12.0], 'by': 'rf_set'}
    # a file that turns the amplifier on: after its attenuator, then flags
    c.put_rf_safe('fake-rf', [{'ro': 0, 'adc_att': 20}, {'ro': 0, 'amp': True, 'att': 15}])
    after = c.load_image('fake-rf')['reset']['rf']['after']
    assert after['ok'] and [o['op'] for o in after['order']][-1] == 'adc_rf_on'
    assert c.rf_get()['adc_ports']['4']['amp_on'] is True
    c.put_rf_safe('fake-rf', None)
    assert c.load_image('fake-rf')['reset']['rf']['after']['source'] == 'built-in default'


def test_real_backend_picks_rf_class(tmp_path):
    bit, hwh = fake_image_files('fake-rf')
    p = tmp_path / 'x.hwh'
    p.write_bytes(hwh)
    assert QickSocBackend.hwh_has_rf_board(str(p))
    p.write_bytes(fake_image_files('fake-a')[1])
    assert not QickSocBackend.hwh_has_rf_board(str(p))
