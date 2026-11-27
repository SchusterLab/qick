"""The board server: TCP, authentication, request handling, delivery.

Threads, not asyncio. The hardware calls (MMIO, DMA waits, QickSoc construction)
are blocking and the drain has to run in its own thread whatever the network side
does; with threads each piece is a plain blocking loop that is easy to read on a
3.8 board, and there are only a handful of clients. Per connection there is a
reader thread (parses requests, enqueues replies) and a sender thread (the only
writer on that socket: replies first, then the chunks and events of the jobs the
connection owns or watches, read from the job stores). A client that vanishes,
even mid-frame, ends its two threads; its jobs keep running and keep their chunks.

The server process is the only holder of the QickSoc (inside the backend), and
takes an exclusive lock file so a second server can see the board is owned.
"""
import errno
import hashlib
import hmac
import json
import logging
import os
import platform
import socket
import sys
import threading
import time
import traceback
from collections import OrderedDict, deque

from .. import get_version
from . import protocol as P
from .protocol import RequestError, ProtocolError
from .jobs import JobManager
from .backend import ProgramError, LimitError

log = logging.getLogger('qick.boardserver')

READ_TYPES = {'get_config', 'get_state', 'status', 'subscribe', 'unsubscribe', 'list_images', 'current_image', 'bye'}

_HERE = os.path.dirname(os.path.abspath(__file__))


def server_code_hash():
    h = hashlib.sha256()
    for fn in sorted(os.listdir(_HERE)):
        if fn.endswith('.py'):
            with open(os.path.join(_HERE, fn), 'rb') as f:
                h.update(fn.encode() + b'\0' + f.read())
    return h.hexdigest()


# --- lock file ---------------------------------------------------------------

def acquire_lock_file(path, info):
    """Take an exclusive flock on `path` and write `info` (JSON) into it.

    Returns the open file, which must stay open for the life of the process; the
    kernel drops the lock when the process dies, so a stale file is harmless.
    Raises RuntimeError naming the holder if another process has it.
    """
    import fcntl
    f = open(path, 'a+')
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            raise
        f.seek(0)
        holder = f.read().strip()
        f.close()
        raise RuntimeError('the board is owned by another process (lock file %s held; it says: %s)' % (path, holder))
    f.seek(0)
    f.truncate()
    f.write(json.dumps(info))
    f.flush()
    return f


def board_lock_holder(path):
    """For other programs (e.g. a Pyro server): None if the board lock is free,
    else the holder's info. Does not keep the lock."""
    import fcntl
    if not os.path.exists(path):
        return None
    with open(path, 'a+') as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.seek(0)
            return f.read().strip() or 'unknown holder'
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return None


# --- configuration -----------------------------------------------------------

DEFAULTS = {
    'host': '0.0.0.0',
    'port': 8788,
    'token_file': None,
    'insecure_fake': False,
    'image_dir': None,
    'lock_file': '/tmp/qick-boardserver.lock',
    'max_queue_bytes': 256 << 20,
    'max_retention_s': 24 * 3600,
    'allow_job_image_load': False,
    'auth_fail_delay_s': 1.0,
    'auth_timeout_s': 30.0,
    'frame_timeout_s': 60.0,
    'send_stall_timeout_s': 600.0,
    'program_cache_size': 64,
    'max_connections': 32,
}


class Connection:
    def __init__(self, server, sock, peer, cid):
        self.server = server
        self.sock = sock
        self.peer = peer
        self.id = cid
        self.label = None
        self.role = None
        self.nonce = None
        self.authed = False
        self.connected_at = server.clock()
        self.closed = threading.Event()
        self.wake = threading.Event()
        self.outq = deque()
        self.outlock = threading.Lock()
        self.attachments = OrderedDict()   # job id -> dict(job, cursor, owner, newest_only)
        self.reader = threading.Thread(target=self._read_loop, name='bs-read-%s' % cid, daemon=True)
        self.sender = threading.Thread(target=self._send_loop, name='bs-send-%s' % cid, daemon=True)

    def start(self):
        self.server.jobs.add_listener(self.wake.set)
        self.reader.start()
        self.sender.start()

    # --- output ---
    def enqueue(self, header, body=b''):
        with self.outlock:
            self.outq.append((header, body))
        self.wake.set()

    def reply(self, req, typ, body=b'', **fields):
        h = {'type': typ, 'reply_to': req.get('id')}
        h.update(fields)
        self.enqueue(h, body)

    def error(self, req, code, message, **fields):
        h = {'type': 'error', 'reply_to': req.get('id') if req else None, 'code': code, 'message': message,
             'request_type': req.get('type') if req else None}
        h.update(fields)
        self.enqueue(h)

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        self.wake.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.server._connection_closed(self)

    def close_after_flush(self):
        self.enqueue({'type': '_close'})

    def _send(self, header, body=b''):
        P.send_frame(self.sock, header, body, stall_timeout=self.server.cfg['send_stall_timeout_s'],
                     should_stop=self.closed.is_set)

    def _send_loop(self):
        jobs = self.server.jobs
        try:
            while not self.closed.is_set():
                self.wake.wait(0.5)
                self.wake.clear()
                progressed = True
                while progressed and not self.closed.is_set():
                    progressed = False
                    while True:
                        with self.outlock:
                            item = self.outq.popleft() if self.outq else None
                        if item is None:
                            break
                        if item[0].get('type') == '_close':
                            self.close()
                            return
                        self._send(*item)
                        progressed = True
                    for jid, att in list(self.attachments.items()):
                        for _ in range(16):
                            nxt = jobs.next_item(att['job'], att['cursor'], att['newest_only'])
                            if nxt is None:
                                break
                            kind, obj = nxt
                            if kind == 'chunk':
                                self._send(obj.header(att['job']), obj.parts)
                            elif kind == 'event':
                                self._send({k: v for k, v in obj.items() if k != 'pos'})
                            else:
                                if att['owner']:
                                    att['job'].times['sent'] = self.server.clock()
                                self._send(dict(obj, subscriber=not att['owner']))
                                self.attachments.pop(jid, None)
                                break
                            progressed = True
                        with self.outlock:
                            if self.outq:
                                break
        except (ConnectionError, OSError) as e:
            log.info('connection %s (%s): send ended: %s', self.id, self.label, e)
        except Exception:
            log.exception('connection %s: sender failed', self.id)
        finally:
            self.close()

    # --- input ---
    def _read_loop(self):
        cfg = self.server.cfg
        try:
            while not self.closed.is_set():
                try:
                    header, body = P.recv_frame(self.sock,
                                                idle_timeout=None if self.authed else cfg['auth_timeout_s'],
                                                frame_timeout=cfg['frame_timeout_s'],
                                                should_stop=self.closed.is_set)
                except ProtocolError as e:
                    self.error(None, e.code, str(e))
                    self.close_after_flush()
                    return
                except socket.timeout:
                    log.info('connection %s: no authentication within %.0f s', self.id, cfg['auth_timeout_s'])
                    return
                self._handle(header, body)
        except EOFError:
            log.info('connection %s (%s): closed by peer', self.id, self.label)
        except (ConnectionError, OSError) as e:
            log.info('connection %s (%s): read ended: %s', self.id, self.label, e)
        except Exception:
            log.exception('connection %s: reader failed', self.id)
        finally:
            if not self.closed.is_set():
                # let the sender flush a final error if one is queued, then close
                time.sleep(0.05)
                self.close()

    def _handle(self, header, body):
        typ = header['type']
        if not self.authed and typ not in ('hello', 'auth'):
            self.error(header, 'not_authenticated', 'authenticate first (hello, auth)')
            self.close_after_flush()
            return
        fn = getattr(self.server, 'h_' + typ, None)
        if fn is None:
            self.error(header, 'unknown_type', 'unknown message type %r' % (typ,))
            return
        if self.authed and self.role != 'run' and typ not in READ_TYPES:
            self.error(header, 'forbidden', 'role %r may not %s' % (self.role, typ))
            return
        try:
            fn(self, header, body)
        except RequestError as e:
            self.error(header, e.code, str(e), **e.fields)
        except Exception as e:
            log.exception('connection %s: %s failed', self.id, typ)
            self.error(header, 'internal', '%s: %s' % (type(e).__name__, e), traceback=traceback.format_exc())


class BoardServer:
    def __init__(self, backend, images, cfg=None, tokens=None):
        self.cfg = dict(DEFAULTS)
        self.cfg.update(cfg or {})
        if tokens is None and not self.cfg['insecure_fake']:
            raise ValueError('no tokens: give a token file (insecure mode is only for the fake backend)')
        if self.cfg['insecure_fake'] and not getattr(backend, 'is_fake', False):
            raise ValueError('insecure_fake is only allowed with the fake backend')
        self.tokens = tokens or []
        self.backend = backend
        self.images = images
        self.t0 = time.monotonic()
        self.wall0 = time.time()
        self.code_hash = server_code_hash()
        if not hasattr(backend, 'generation'):
            backend.generation = 0
        backend.image_hash = None
        self.current_image = None
        self.load_history = deque(maxlen=50)
        self.loading_info = None
        self.prog_cache = OrderedDict()
        self.prog_lock = threading.Lock()
        self.conns = {}
        self.conn_lock = threading.Lock()
        self._ncid = 0
        self.jobs = JobManager(backend, self.clock, max_queue_bytes=self.cfg['max_queue_bytes'],
                               prepare_image=self._prepare_job_image, log=log)
        self.listener = None
        self.lockf = None
        self._accept_thread = None
        self._stopping = threading.Event()

    def clock(self):
        return time.monotonic() - self.t0

    # --- lifecycle ---

    def start(self):
        lf = self.cfg['lock_file']
        if lf:
            self.lockf = acquire_lock_file(lf, {'pid': os.getpid(), 'port': self.cfg['port'],
                                                'program': 'qick.boardserver', 'started': time.time()})
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.cfg['host'], self.cfg['port']))
        s.listen(16)
        s.settimeout(0.5)
        self.listener = s
        self._accept_thread = threading.Thread(target=self._accept_loop, name='bs-accept', daemon=True)
        self._accept_thread.start()
        log.info('board server listening on %s:%d (protocol %d, fake=%s)', self.cfg['host'], self.address[1],
                 P.PROTOCOL_VERSION, getattr(self.backend, 'is_fake', False))

    @property
    def address(self):
        return self.listener.getsockname()

    def stop(self):
        self._stopping.set()
        self.jobs.shutdown()
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
        with self.conn_lock:
            conns = list(self.conns.values())
        for c in conns:
            c.close()
        if self.lockf is not None:
            self.lockf.close()
            self.lockf = None

    def _accept_loop(self):
        while not self._stopping.is_set():
            try:
                sock, peer = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            P.prepare_socket(sock)
            with self.conn_lock:
                if len(self.conns) >= self.cfg['max_connections']:
                    try:
                        P.send_frame(sock, {'type': 'error', 'code': 'limit_exceeded', 'limit': 'max_connections',
                                            'message': 'too many connections'}, stall_timeout=2)
                    except Exception:
                        pass
                    sock.close()
                    continue
                self._ncid += 1
                c = Connection(self, sock, peer, 'c%d' % self._ncid)
                self.conns[c.id] = c
            c.start()

    def _connection_closed(self, conn):
        self.jobs.remove_listener(conn.wake.set)
        with self.conn_lock:
            self.conns.pop(conn.id, None)
        with self.jobs.lock:
            for att in conn.attachments.values():
                if att['owner'] and att['job'].owner_conn is conn:
                    att['job'].owner_conn = None
            conn.attachments.clear()
        dropped = self.images.abort_owner(conn.id) if self.images is not None else []
        if dropped:
            log.info('connection %s closed: discarded %d unfinished upload(s)', conn.id, len(dropped))

    # --- helpers ---

    def _image_summary(self):
        ci = self.current_image
        if ci is None:
            return None
        return {k: ci.get(k) for k in ('state', 'name', 'hash', 'bit_sha256', 'hwh_sha256', 'detail')}

    def _need_image(self):
        if self.backend.soccfg is None:
            raise RequestError('no_image', 'no firmware image is loaded (load_image first)')

    def _cache_prog(self, h, progdict):
        with self.prog_lock:
            self.prog_cache[h] = progdict
            self.prog_cache.move_to_end(h)
            while len(self.prog_cache) > self.cfg['program_cache_size']:
                self.prog_cache.popitem(last=False)

    def _receive_prog(self, header, body):
        tree = header.get('prog')
        if not isinstance(tree, dict):
            raise RequestError('bad_request', '"prog" must be the dump_prog dictionary', field='prog')
        descs = header.get('arrays', [])
        arrays = P.unpack_arrays(descs, body)
        h = P.tree_hash(tree, [(d['name'], arrays[d['name']]) for d in descs])
        progdict = P.unpack_tree(tree, arrays)
        return h, progdict

    def _make_prog(self, progdict):
        try:
            return self.backend.make_program(progdict)
        except ProgramError as e:
            raise RequestError('program_format', str(e), field=e.field)

    # --- handshake ---

    def h_hello(self, conn, req, body):
        if conn.authed:
            raise RequestError('bad_request', 'already authenticated')
        proto = req.get('protocol')
        if proto not in P.SUPPORTED_PROTOCOLS:
            conn.error(req, 'unsupported_protocol', 'protocol %r not supported' % (proto,),
                       supported=P.SUPPORTED_PROTOCOLS)
            conn.close_after_flush()
            return
        role = req.get('role', 'run')
        if role not in ('run', 'read'):
            raise RequestError('bad_request', 'role must be "run" or "read"', field='role')
        label = req.get('label')
        if not isinstance(label, str) or not label or len(label) > 200:
            raise RequestError('bad_request', 'label (who is connecting: engine, setup, person or agent) is required',
                               field='label')
        conn.label, conn.role = label, role
        conn.client_version = req.get('client_version')
        conn.nonce = P.new_nonce()
        conn.reply(req, 'challenge', nonce=conn.nonce, protocol=P.PROTOCOL_VERSION)

    def h_auth(self, conn, req, body):
        if conn.nonce is None or conn.authed:
            raise RequestError('bad_request', 'send hello first')
        mac = req.get('hmac')
        ok = False
        if self.cfg['insecure_fake']:
            ok = True
        elif isinstance(mac, str):
            for role, token in self.tokens:
                if conn.role == 'run' and role != 'run':
                    continue
                expect = P.auth_mac(token, conn.nonce, conn.role, conn.label)
                if hmac.compare_digest(expect, mac):
                    ok = True
        conn.nonce = None
        if not ok:
            log.warning('connection %s (%s, %s): authentication failed', conn.id, conn.label, conn.peer[0])
            time.sleep(self.cfg['auth_fail_delay_s'])
            conn.error(req, 'auth_failed', 'authentication failed')
            conn.close_after_flush()
            return
        conn.authed = True
        log.info('connection %s: %s authenticated as %s', conn.id, conn.label, conn.role)
        cfg = self.backend.soccfg
        tp = cfg['tprocs'][0] if cfg else {}
        conn.reply(req, 'welcome',
                   protocol=P.PROTOCOL_VERSION, qick_version=get_version(), server_code_hash=self.code_hash,
                   session=conn.id, role=conn.role, insecure=bool(self.cfg['insecure_fake']),
                   fake=bool(getattr(self.backend, 'is_fake', False)),
                   firmware={'image': self._image_summary(),
                             'fw_timestamp': cfg.get('fw_timestamp') if cfg else None,
                             'hwh_sha256': (self.current_image or {}).get('hwh_sha256')},
                   board=cfg.get('board') if cfg else None,
                   python=platform.python_version(),
                   capabilities={'job_kinds': ['qick_prog'], 'modes': ['acc', 'dec'], 'reduce': False,
                                 'image_store': self.images is not None,
                                 'allow_job_image_load': bool(self.cfg['allow_job_image_load']),
                                 'time_sync_period': tp.get('time_sync_period', 0),
                                 'queue_flags': bool(tp.get('queue_flags', False))},
                   limits={'max_header': P.MAX_HEADER, 'max_body': P.MAX_BODY,
                           'max_queue_bytes': self.cfg['max_queue_bytes'],
                           'max_retention_s': self.cfg['max_retention_s']},
                   wall_time=time.time(), monotonic=self.clock())

    def h_bye(self, conn, req, body):
        conn.reply(req, 'bye')
        conn.close_after_flush()

    # --- configuration and state ---

    def h_get_config(self, conn, req, body):
        self._need_image()
        conn.reply(req, 'config', soccfg=self.backend.soccfg, image=self._image_summary())

    def h_get_state(self, conn, req, body):
        st = self.backend.get_state() if self.backend.soccfg is not None else {'image_loaded': False}
        st.update({'image': self._image_summary(), 'loading': self.loading_info,
                   'outputs_idle_at': self.jobs.outputs_idle_at, 'last_idle': self.jobs.last_idle,
                   'monotonic': self.clock()})
        conn.reply(req, 'state', state=st)

    def h_status(self, conn, req, body):
        jid = req.get('job')
        if jid is not None:
            job = self.jobs.get(jid)
            conn.reply(req, 'status', job=job.detail())
            return
        with self.conn_lock:
            clients = [{'session': c.id, 'label': c.label, 'role': c.role, 'connected_at': c.connected_at}
                       for c in self.conns.values() if c.authed]
        running, queued = self.jobs.busy()
        with self.jobs.lock:
            recent = [j.summary() for j in list(self.jobs.jobs.values())[-20:]]
        conn.reply(req, 'status', clients=clients,
                   running=running.summary() if running else None,
                   queue=[j.summary() for j in queued],
                   recent=recent,
                   uptime_s=self.clock(),
                   memory={'queued_bytes': self.jobs.total_queued_bytes(), 'bound': self.cfg['max_queue_bytes']},
                   last_error=self.jobs.last_error, outputs_idle_at=self.jobs.outputs_idle_at,
                   image=self._image_summary(), loading=self.loading_info)

    # --- programs and jobs ---

    def h_put_prog(self, conn, req, body):
        self._need_image()
        h, progdict = self._receive_prog(req, body)
        self._make_prog(progdict)
        self._cache_prog(h, progdict)
        conn.reply(req, 'prog_stored', hash=h)

    def _norm_run(self, run):
        if run is None:
            run = {}
        if not isinstance(run, dict):
            raise RequestError('bad_request', '"run" must be an object', field='run')
        out = {}
        rounds = run.get('rounds', 1)
        if not isinstance(rounds, int) or rounds < 1:
            raise RequestError('bad_request', 'run.rounds must be a positive integer', field='run.rounds')
        out['rounds'] = rounds
        mode = run.get('mode', 'acc')
        if mode not in ('acc', 'dec'):
            raise RequestError('bad_request', 'run.mode must be "acc" (accumulated) or "dec" (decimated)',
                               field='run.mode')
        out['mode'] = mode
        ss = run.get('start_src', 'internal')
        if ss not in ('internal', 'external'):
            raise RequestError('bad_request', 'run.start_src must be internal or external', field='run.start_src')
        out['start_src'] = ss
        stride = run.get('stride')
        if stride is not None and (not isinstance(stride, int) or stride < 1):
            raise RequestError('bad_request', 'run.stride must be a positive integer (shots per chunk)',
                               field='run.stride')
        out['stride'] = stride
        tl = run.get('time_limit_s')
        if tl is not None and (not isinstance(tl, (int, float)) or tl <= 0):
            raise RequestError('bad_request', 'run.time_limit_s must be positive', field='run.time_limit_s')
        out['time_limit_s'] = tl
        red = run.get('reduce')
        if red not in (None, 'none'):
            raise RequestError('unsupported', 'run.reduce is reserved; version 1 returns every shot',
                               field='run.reduce')
        out['reduce'] = None
        out['image'] = run.get('image')
        if 'fake' in run:
            if not getattr(self.backend, 'is_fake', False):
                raise RequestError('bad_request', 'run.fake is only accepted by the fake backend', field='run.fake')
            if not isinstance(run['fake'], dict):
                raise RequestError('bad_request', 'run.fake must be an object', field='run.fake')
            out['fake'] = run['fake']
        return out

    def _norm_limits(self, limits):
        limits = limits or {}
        out = {}
        mq = limits.get('max_queue_bytes', self.cfg['max_queue_bytes'])
        if not isinstance(mq, int) or mq <= 0:
            raise RequestError('bad_request', 'limits.max_queue_bytes must be a positive integer',
                               field='limits.max_queue_bytes')
        if mq > self.cfg['max_queue_bytes']:
            raise RequestError('limit_exceeded', 'limits.max_queue_bytes above the server bound %d'
                               % self.cfg['max_queue_bytes'], limit='max_queue_bytes')
        out['max_queue_bytes'] = mq
        pol = limits.get('policy', 'stop')
        if pol not in ('stop', 'drop_oldest'):
            raise RequestError('bad_request', 'limits.policy must be stop or drop_oldest', field='limits.policy')
        out['policy'] = pol
        ret = limits.get('retention_s', 3600)
        if not isinstance(ret, (int, float)) or ret <= 0 or ret > self.cfg['max_retention_s']:
            raise RequestError('bad_request', 'limits.retention_s must be in (0, %d]' % self.cfg['max_retention_s'],
                               field='limits.retention_s')
        out['retention_s'] = ret
        return out

    def _resolve_job_image(self, want):
        """None if the job can run on the loaded image; else the image meta to load first."""
        if not want:
            return None
        if self.images is None:
            raise RequestError('unknown_image', 'this server has no image store', image=want)
        meta = self.images.resolve(want)
        loaded = self.backend.image_hash
        if meta['hash'] == loaded:
            return None
        if not self.cfg['allow_job_image_load']:
            raise RequestError('image_mismatch', 'job wants image %s (%s) but %s is loaded, and this server does not '
                               'load images for jobs (allow_job_image_load is off)'
                               % (meta['name'], meta['hash'][:12], (loaded or 'none')[:12]),
                               wanted=meta['hash'], wanted_name=meta['name'], loaded=loaded)
        return meta

    def h_submit(self, conn, req, body):
        kind = req.get('kind', 'qick_prog')
        if kind != 'qick_prog':
            raise RequestError('unsupported', 'job kind %r not supported (version 1: qick_prog)' % (kind,),
                               field='kind')
        run = self._norm_run(req.get('run'))
        limits = self._norm_limits(req.get('limits'))
        to_load = self._resolve_job_image(run['image'])
        if to_load is None:
            self._need_image()
        if req.get('prog_ref') is not None:
            with self.prog_lock:
                progdict = self.prog_cache.get(req['prog_ref'])
            if progdict is None:
                raise RequestError('unknown_program', 'program %s is not in the cache; send it inline'
                                   % (req['prog_ref'],), prog_ref=req['prog_ref'])
            h = req['prog_ref']
        else:
            h, progdict = self._receive_prog(req, body)
        prog, layout = None, None
        if to_load is None:
            prog = self._make_prog(progdict)
            try:
                layout = self.backend.layout(prog, run)
            except LimitError as e:
                raise RequestError('limit_exceeded', str(e), limit=e.limit)
            self._cache_prog(h, progdict)
        job, position = self.jobs.submit(kind, conn.label, req.get('ref'), h, progdict, prog,
                                         self.backend.generation, run, limits, layout)
        log.info('job %s submitted by %s (ref %r, prog %s, %d round(s), mode %s)', job.id, conn.label,
                 job.ref, h[:12], run['rounds'], run['mode'])
        conn.reply(req, 'accepted', job=job.id, ref=job.ref, position=position, run=run, limits=limits,
                   layout=layout, prog_hash=h, image_load_first=to_load['hash'] if to_load else None)
        with self.jobs.lock:
            cursor = {'seq': 0, 'eseq': 0, 'done_sent': False}
            job.owner_conn = conn
            job.owner_cursor = cursor
            conn.attachments[job.id] = {'job': job, 'cursor': cursor, 'owner': True, 'newest_only': False}
        conn.wake.set()

    def h_subscribe(self, conn, req, body):
        job = self.jobs.get(req.get('job'))
        conn.reply(req, 'subscribed', job=job.id, state=job.state, next_seq=job.next_seq)
        with self.jobs.lock:
            start = job.next_seq if req.get('from_now', True) else 0
            conn.attachments[job.id] = {'job': job, 'cursor': {'seq': start, 'eseq': 0, 'done_sent': False},
                                        'owner': False, 'newest_only': bool(req.get('newest_only', True))}
        conn.wake.set()

    def h_unsubscribe(self, conn, req, body):
        jid = req.get('job')
        att = conn.attachments.get(jid)
        if att is not None and not att['owner']:
            conn.attachments.pop(jid, None)
        conn.reply(req, 'unsubscribed', job=jid)

    def h_fetch(self, conn, req, body):
        job = self.jobs.get(req.get('job'))
        from_seq = req.get('from_seq', 0)
        from_event = req.get('from_event', 0)
        if not isinstance(from_seq, int) or from_seq < 0 or not isinstance(from_event, int) or from_event < 0:
            raise RequestError('bad_request', 'from_seq and from_event must be non-negative integers',
                               field='from_seq')
        with self.jobs.lock:
            first = next(iter(job.chunks)) if job.chunks else job.next_seq
            if from_seq < first and from_seq < job.next_seq:
                missing_ok = all(job.dropped and any(a <= s < b for a, b in job.dropped)
                                 for s in range(from_seq, first))
                if not missing_ok:
                    raise RequestError('seq_freed', 'chunks from seq %d were freed (acked or expired); first '
                                       'retained is %d' % (from_seq, first), first_retained=first)
            old = job.owner_conn
            if old is not None and old is not conn:
                old.attachments.pop(job.id, None)
            job.owner_conn = conn
            conn.reply(req, 'fetching', job=job.id, state=job.state, next_seq=job.next_seq, first_retained=first,
                       events=len(job.events))
            cursor = {'seq': from_seq, 'eseq': from_event, 'done_sent': False}
            job.owner_cursor = cursor
            conn.attachments[job.id] = {'job': job, 'cursor': cursor, 'owner': True, 'newest_only': False}
        conn.wake.set()

    def h_ack(self, conn, req, body):
        job = self.jobs.get(req.get('job'))
        if job.owner_conn is not conn:
            raise RequestError('not_owner', 'only the owning connection may ack job %s' % job.id)
        seq = req.get('seq')
        if not isinstance(seq, int):
            raise RequestError('bad_request', 'seq must be an integer', field='seq')
        self.jobs.ack(job, seq)
        # no reply: acks are frequent

    def h_cancel(self, conn, req, body):
        jid = req.get('job')
        was = self.jobs.cancel(jid)
        log.info('job %s cancelled by %s (was %s)', jid, conn.label, was)
        conn.reply(req, 'cancelling', job=jid, was=was)

    # --- images ---

    def _need_store(self):
        if self.images is None:
            raise RequestError('unsupported', 'this server has no image store (--image-dir)')

    def h_list_images(self, conn, req, body):
        self._need_store()
        conn.reply(req, 'images', images=self.images.list(),
                   current=self.backend.image_hash, current_image=self._image_summary())

    def h_current_image(self, conn, req, body):
        rec = self.current_image
        if rec is None:
            rec = {'state': 'none', 'detail': 'no image loaded since server start'}
        conn.reply(req, 'current_image', image=rec)

    def h_put_image(self, conn, req, body):
        self._need_store()
        phase = req.get('phase')
        if phase == 'begin':
            up = self.images.begin_upload(req.get('name'), req.get('bit_sha256'), req.get('hwh_sha256'),
                                          req.get('bit_size'), req.get('hwh_size'), added_by=conn.label,
                                          owner=conn.id, replace=bool(req.get('replace')), note=req.get('note'))
            conn.reply(req, 'upload', upload=up, max_chunk=P.MAX_BODY)
        elif phase == 'data':
            n = self.images.write_upload(req.get('upload'), conn.id, req.get('file'), req.get('offset'), body)
            conn.reply(req, 'upload', upload=req.get('upload'), file=req.get('file'), written=n)
        elif phase == 'commit':
            meta = self.images.commit_upload(req.get('upload'), conn.id)
            log.info('image %s (%s) stored by %s', meta['name'], meta['hash'][:12], conn.label)
            conn.reply(req, 'image_stored', image=meta)
        elif phase == 'abort':
            self.images.abort_upload(req.get('upload'), conn.id)
            conn.reply(req, 'upload', upload=req.get('upload'), aborted=True)
        else:
            raise RequestError('bad_request', 'phase must be begin, data, commit or abort', field='phase')

    def h_load_image(self, conn, req, body):
        self._need_store()
        meta = self.images.resolve(req.get('image'))
        force = bool(req.get('force'))
        cancelled = self.jobs.begin_exclusive(force)
        try:
            for c in cancelled:
                log.info('load_image by %s: cancelled queued job %s (%s, ref %r)', conn.label, c['job'],
                         c['submitter'], c['ref'])
            rec = self._do_load(meta, conn.label, 'load_image')
        finally:
            self.jobs.end_exclusive()
        rec = dict(rec)
        rec['cancelled_jobs'] = cancelled
        conn.reply(req, 'image_loaded', **rec)

    def _do_load(self, meta, label, reason):
        """Load an image; the caller holds the board exclusively (no job running)."""
        bit, hwh = self.images.paths(meta)
        t_req = self.clock()
        self.loading_info = {'image': meta['name'], 'hash': meta['hash'], 'requested_by': label, 'since': t_req}
        log.info('loading image %s (%s) for %s (%s)', meta['name'], meta['hash'][:12], label, reason)
        try:
            try:
                res = self.backend.load_image(bit, hwh)
            except Exception as e:
                tb = traceback.format_exc()
                self.backend.image_hash = None
                self.backend.generation += 1
                with self.prog_lock:
                    self.prog_cache.clear()
                self.current_image = {'state': 'failed', 'name': meta['name'], 'hash': meta['hash'],
                                      'requested_by': label, 't': t_req, 'error': '%s: %s' % (type(e).__name__, e)}
                self.load_history.append(self.current_image)
                log.error('loading image %s failed: %s', meta['name'], e)
                raise RequestError('image_load_failed', 'loading %s failed: %s: %s' % (meta['name'], type(e).__name__, e),
                                   traceback=tb, image=meta['hash'])
        finally:
            self.loading_info = None
        self.backend.generation += 1
        self.backend.image_hash = meta['hash']
        with self.prog_lock:
            self.prog_cache.clear()
        self.images.save_soccfg(meta['name'], res['soccfg'])
        rec = {'state': 'loaded', 'name': meta['name'], 'hash': meta['hash'], 'bit_sha256': meta['bit_sha256'],
               'hwh_sha256': meta['hwh_sha256'], 'soccfg': res['soccfg'], 'duration_s': res['duration_s'],
               'requested_by': label, 'reason': reason, 't': t_req, 'wall_time': time.time(), 'reset': res['reset']}
        self.current_image = rec
        self.load_history.append({k: v for k, v in rec.items() if k != 'soccfg'})
        log.info('image %s loaded in %.2f s for %s (clocks reprogrammed: %s)', meta['name'], res['duration_s'], label,
                 res['reset'].get('clocks_reprogrammed'))
        return rec

    def _prepare_job_image(self, job):
        """Runner hook: make sure the image a job names is loaded (or refuse)."""
        want = job.run.get('image')
        if not want:
            if self.backend.soccfg is None:
                raise RequestError('no_image', 'no firmware image is loaded')
            return None
        meta = self._resolve_job_image(want)
        if meta is None:
            return None
        rec = self._do_load(meta, job.owner_label, 'job %s' % job.id)
        return rec

    # --- startup helpers (used by __main__) ---

    def load_at_start(self, ref, label='server start'):
        meta = self.images.resolve(ref)
        return self._do_load(meta, label, 'server start')

    def attach_at_start(self, ref):
        """Use whatever the FPGA runs, assuming it is this store image (not checked)."""
        meta = self.images.resolve(ref)
        bit, hwh = self.images.paths(meta)
        res = self.backend.attach(bit, hwh)
        self.backend.generation += 1
        # deliberately not image_hash = meta['hash']: what is on the FPGA is unknown
        self.backend.image_hash = None
        self.current_image = {'state': 'unknown', 'detail': 'loaded before server start',
                              'assumed': {'name': meta['name'], 'hash': meta['hash']},
                              'soccfg': res['soccfg'], 'reset': res['reset'], 't': self.clock(),
                              'requested_by': 'server start'}
        return self.current_image
