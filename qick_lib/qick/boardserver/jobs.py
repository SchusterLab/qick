"""Job queue, the drain, and chunk retention.

One runner thread takes jobs from the queue one at a time and iterates the
backend's run() generator. That loop is the drain: it moves data from the firmware
buffers into each job's chunk store in memory and never touches the network. The
per-connection sender threads read from the stores (server.py). A slow or absent
client therefore makes the store grow; it never stalls the drain.

The store has a byte bound (per job, and server-wide). Reaching it is handled by
the job's policy, `stop` (stop the run, outcome `incomplete`) or `drop_oldest`
(free the oldest chunks, report exactly which), and is reported as an event.
Chunks are kept until the owner acks them, or until the retention time after the
job finished.
"""
import threading
import time
import traceback
from collections import OrderedDict, deque

from .backend import TprocQueueError, ProgramError, LimitError
from .protocol import pack_arrays, RequestError

MAX_EVENTS = 1000


class Chunk:
    __slots__ = ('seq', 'pos', 'round', 'buffer', 'first', 'last', 'descs', 'parts', 'nbytes')

    def __init__(self, seq, pos, rnd, buffer, first, last, descs, parts, nbytes):
        self.seq, self.pos, self.round, self.buffer = seq, pos, rnd, buffer
        self.first, self.last, self.descs, self.parts, self.nbytes = first, last, descs, parts, nbytes

    def header(self, job):
        return {'type': 'chunk', 'job': job.id, 'seq': self.seq, 'round': self.round, 'buffer': self.buffer,
                'shots': [self.first, self.last], 'arrays': self.descs, 'queued_bytes': job.queued_bytes}


def _merge_range(ranges, a, b):
    if ranges and ranges[-1][1] == a:
        ranges[-1][1] = b
    else:
        ranges.append([a, b])


class Job:
    def __init__(self, jid, kind, owner_label, ref, prog_hash, progdict, prog, prog_gen, run, limits, layout, now):
        self.id = jid
        self.kind = kind
        self.owner_label = owner_label
        self.ref = ref
        self.prog_hash = prog_hash
        self.progdict = progdict
        self.prog = prog
        self.prog_gen = prog_gen
        self.run = run
        self.limits = limits
        self.layout = layout
        self.state = 'queued'
        self.outcome = None
        self.reason = None
        self.error = None
        self.idle = None
        self.times = {'received': now, 'queued': now, 'loaded': None, 'started': None, 'finished': None,
                      'drained': None, 'sent': None}
        self.stop_event = threading.Event()
        self.stop_reason = None         # 'cancel', 'queue_bound', 'image_load'
        self.chunks = OrderedDict()     # seq -> Chunk (retained)
        self.next_seq = 0
        self.next_pos = 0
        self.queued_bytes = 0
        self.events = []                # each has 'eseq' and 'pos'
        self.events_truncated = 0
        self.dropped = []               # [[seq_start, seq_end), ...]
        self.overruns = []              # [{'round', 'shots': [a, b)}]
        self.timed_out = False
        self.bound_stop = None
        self.shots_stored = 0
        self.acked = -1
        self.owner_conn = None          # the connection currently receiving as owner
        self.owner_cursor = None        # its delivery cursor (seqs below cursor['seq'] were sent)
        self.done_header = None
        self.freed = False

    def summary(self):
        return {'job': self.id, 'state': self.state, 'submitter': self.owner_label, 'ref': self.ref,
                'outcome': self.outcome}

    def detail(self):
        d = self.summary()
        d.update({'kind': self.kind, 'prog_hash': self.prog_hash, 'run': self.run, 'limits': self.limits,
                  'times': self.times, 'next_seq': self.next_seq,
                  'retained': [min(self.chunks), max(self.chunks) + 1] if self.chunks else None,
                  'queued_bytes': self.queued_bytes, 'acked': self.acked, 'dropped': self.dropped,
                  'overruns': self.overruns, 'events': len(self.events), 'reason': self.reason,
                  'error': self.error, 'owner_connected': self.owner_conn is not None, 'freed': self.freed})
        return d


class JobManager:
    """Queue, runner (drain) thread, chunk stores.

    backend: the backend object; clock(): seconds since server start;
    prepare_image(job): called by the runner before a job starts, may load an
    image or raise RequestError (server.py supplies it);
    max_queue_bytes: server-wide bound on retained chunk bytes.
    """

    def __init__(self, backend, clock, max_queue_bytes=256 << 20, max_queued_jobs=64, history=200,
                 prepare_image=None, log=None):
        self.backend = backend
        self.clock = clock
        self.max_queue_bytes = int(max_queue_bytes)
        self.max_queued_jobs = max_queued_jobs
        self.history = history
        self.prepare_image = prepare_image
        self.log = log
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.queue = deque()
        self.jobs = OrderedDict()
        self.running = None
        self.loading = False
        self.held = False                   # True: start no new job (operator pause; tests)
        self.exec_lock = threading.Lock()   # held while a job runs or an image loads
        self._n = 0
        self._listeners = []
        self.last_error = None
        self.outputs_idle_at = None
        self.last_idle = None
        self._shutdown = False
        self.runner = threading.Thread(target=self._runner, name='boardserver-runner', daemon=True)
        self.runner.start()

    # --- listeners (connection senders) ----------------------------------

    def add_listener(self, fn):
        with self.lock:
            self._listeners.append(fn)

    def remove_listener(self, fn):
        with self.lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def _notify(self):
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                pass

    # --- submit / cancel -------------------------------------------------

    def submit(self, kind, owner_label, ref, prog_hash, progdict, prog, prog_gen, run, limits, layout):
        with self.lock:
            if len(self.queue) >= self.max_queued_jobs:
                raise RequestError('limit_exceeded', 'queue holds %d jobs already' % len(self.queue),
                                   limit='max_queued_jobs')
            self._n += 1
            jid = 'j%d' % self._n
            job = Job(jid, kind, owner_label, ref, prog_hash, progdict, prog, prog_gen, run, limits, layout,
                      self.clock())
            self.jobs[jid] = job
            self.queue.append(job)
            position = len(self.queue) - 1 + (1 if self.running is not None else 0)
            self._trim_history()
            self.cond.notify_all()
        self._notify()
        return job, position

    def get(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
        if job is None:
            raise RequestError('unknown_job', 'no job %r' % (jid,), job=jid)
        return job

    def cancel(self, jid, reason='client'):
        """Cancel a queued or running job. Returns the job's state before."""
        with self.lock:
            job = self.get(jid)
            if job.state == 'queued':
                self.queue.remove(job)
                self._finish(job, 'cancelled', reason)
                was = 'queued'
            elif job.state == 'running':
                job.stop_reason = job.stop_reason or ('image_load' if reason == 'image_load' else 'cancel')
                job.stop_event.set()
                was = 'running'
            else:
                was = job.state
        self._notify()
        return was

    def hold(self, held):
        with self.lock:
            self.held = bool(held)
            self.cond.notify_all()

    def queued_jobs(self):
        with self.lock:
            return list(self.queue)

    def busy(self):
        with self.lock:
            return self.running is not None, list(self.queue)

    # --- exclusive board access (image loads) ----------------------------

    def begin_exclusive(self, force, cancel_reason='image_load'):
        """For load_image: refuse while a job runs; refuse while jobs are queued
        unless force, in which case cancel them (each gets done/cancelled/image_load).
        On success the runner is held off until end_exclusive(). Returns cancelled jobs."""
        with self.lock:
            if self.loading:
                raise RequestError('board_loading', 'an image load is already in progress')
            if self.running is not None:
                j = self.running
                raise RequestError('job_running', 'job %s (%s, ref %r) is running; cancel it first' %
                                   (j.id, j.owner_label, j.ref), job=j.id, submitter=j.owner_label, ref=j.ref)
            if self.queue and not force:
                listed = [{'job': j.id, 'submitter': j.owner_label, 'ref': j.ref, 'state': j.state,
                           'image': j.run.get('image')} for j in self.queue]
                raise RequestError('jobs_queued', '%d job(s) queued (%s); pass force to cancel them' % (
                    len(self.queue), ', '.join('%s by %s, ref %r' % (x['job'], x['submitter'], x['ref'])
                                               for x in listed)), jobs=listed)
            cancelled = []
            while self.queue:
                j = self.queue.popleft()
                self._finish(j, 'cancelled', 'image_load')
                cancelled.append({'job': j.id, 'submitter': j.owner_label, 'ref': j.ref})
            self.loading = True
        self._notify()
        # the runner releases exec_lock between jobs; it cannot start one while loading
        self.exec_lock.acquire()
        return cancelled

    def cancel_unpinned(self, generation, reason='image_changed'):
        """After a load started by a job (run.image): cancel the queued jobs that do not
        name an image and were submitted against an earlier image. Returns them."""
        out = []
        with self.lock:
            for j in list(self.queue):
                if not j.run.get('image') and j.prog_gen != generation:
                    self.queue.remove(j)
                    self._finish(j, 'cancelled', reason)
                    out.append({'job': j.id, 'submitter': j.owner_label, 'ref': j.ref})
        if out:
            self._notify()
        return out

    def end_exclusive(self):
        self.exec_lock.release()
        with self.lock:
            self.loading = False
            self.cond.notify_all()
        self._notify()

    # --- the runner (drain) ----------------------------------------------

    def shutdown(self):
        with self.lock:
            self._shutdown = True
            for j in list(self.queue):
                self.queue.remove(j)
                self._finish(j, 'cancelled', 'server_shutdown')
            if self.running is not None:
                self.running.stop_reason = 'cancel'
                self.running.stop_event.set()
            self.cond.notify_all()

    def _runner(self):
        while True:
            with self.lock:
                while not self._shutdown and (not self.queue or self.loading or self.held):
                    self.cond.wait(1.0)
                    self._expire()
                if self._shutdown:
                    return
                job = self.queue.popleft()
                self.running = job
                job.state = 'running'
                job.times['started'] = self.clock()
            self._notify()
            with self.exec_lock:
                try:
                    self._run_job(job)
                except Exception:
                    # never let the runner die
                    self.last_error = traceback.format_exc()
                    if self.log:
                        self.log.exception('runner: unexpected error')
            with self.lock:
                self.running = None
                self.cond.notify_all()
            self._notify()

    def _event(self, job, kind, **fields):
        with self.lock:
            if len(job.events) >= MAX_EVENTS:
                job.events_truncated += 1
                return
            ev = {'type': 'event', 'job': job.id, 'kind': kind, 'eseq': len(job.events), 'pos': job.next_pos,
                  't': self.clock()}
            ev.update(fields)
            job.next_pos += 1
            job.events.append(ev)
        self._notify()

    def _retained_elsewhere(self, job):
        return sum(j.queued_bytes for j in self.jobs.values() if j is not job)

    def _store(self, job, ev):
        """Put one data event into the job's store, applying the bound. Drain thread only."""
        descs, parts = pack_arrays(ev['arrays'])
        nbytes = sum(d['nbytes'] for d in descs)
        dropped_now = []
        with self.lock:
            bound = min(int(job.limits['max_queue_bytes']), self.max_queue_bytes - self._retained_elsewhere(job))
            if job.queued_bytes + nbytes > bound:
                if job.limits['policy'] == 'stop':
                    job.bound_stop = {'round': ev['round'], 'shot': ev['first'], 'seq': job.next_seq,
                                      'queued_bytes': job.queued_bytes, 'bound': bound}
                    job.stop_reason = job.stop_reason or 'queue_bound'
                    job.stop_event.set()
                    stopped = True
                else:
                    while job.chunks and job.queued_bytes + nbytes > bound:
                        seq, c = job.chunks.popitem(last=False)
                        job.queued_bytes -= c.nbytes
                        sent = job.owner_cursor is not None and seq < job.owner_cursor['seq']
                        dropped_now.append({'seq': seq, 'round': c.round, 'shots': [c.first, c.last],
                                            'sent': sent})
                        _merge_range(job.dropped, seq, seq + 1)
                    stopped = False
            else:
                stopped = False
            if stopped:
                pass
            else:
                seq = job.next_seq
                job.next_seq += 1
                if nbytes > bound:
                    # does not fit even in an empty store: dropped itself
                    dropped_now.append({'seq': seq, 'round': ev['round'], 'shots': [ev['first'], ev['last']],
                                        'sent': False})
                    _merge_range(job.dropped, seq, seq + 1)
                else:
                    c = Chunk(seq, job.next_pos, ev['round'], ev['buffer'], ev['first'], ev['last'], descs, parts,
                              nbytes)
                    job.next_pos += 1
                    job.chunks[seq] = c
                    job.queued_bytes += nbytes
                    job.shots_stored += ev['last'] - ev['first']
                job.times['drained'] = self.clock()
        if stopped:
            b = job.bound_stop
            self._event(job, 'stopped', reason='queue_bound', round=b['round'], from_shot=b['shot'],
                        next_seq=b['seq'], queued_bytes=b['queued_bytes'], bound=b['bound'],
                        message='queue bound of %d bytes reached; run stopped at round %d shot %d, chunks from seq %d '
                                'not produced' % (b['bound'], b['round'], b['shot'], b['seq']))
        elif dropped_now:
            self._event(job, 'dropped', chunks=dropped_now, seqs=[d['seq'] for d in dropped_now],
                        message='queue bound reached: dropped %d chunk(s)' % len(dropped_now))
        else:
            self._notify()

    def _run_job(self, job):
        be = self.backend
        try:
            if self.prepare_image is not None:
                rec = self.prepare_image(job)
                if rec is not None:
                    self._event(job, 'image_loaded', image=rec)
            if not job.run.get('image') and job.prog_gen != getattr(be, 'generation', 0):
                # submitted against another image and does not name one: do not run it on this one
                raise RequestError('image_changed', 'the image changed after job %s was submitted and the job does '
                                   'not name an image (run.image); it is not run on %s'
                                   % (job.id, getattr(be, 'image_hash', None)),
                                   loaded=getattr(be, 'image_hash', None))
            if job.prog is None or job.prog_gen != getattr(be, 'generation', 0):
                job.prog = be.make_program(job.progdict)
                job.prog_gen = getattr(be, 'generation', 0)
                job.layout = be.layout(job.prog, job.run)
            self._event(job, 'started', image=getattr(be, 'image_hash', None), layout=job.layout)
            tl = job.run.get('time_limit_s')
            deadline = time.monotonic() + float(tl) if tl else None
            for ev in be.run(job.prog, job.run, job.stop_event, deadline):
                t = ev['type']
                if t == 'data':
                    if job.bound_stop is None:
                        self._store(job, ev)
                elif t == 'overrun':
                    with self.lock:
                        job.overruns.append({'round': ev['round'], 'shots': [ev['first'], ev['last']]})
                    self._event(job, 'overrun', round=ev['round'], shots=[ev['first'], ev['last']],
                                info=ev.get('info'),
                                message='firmware ring overwritten before it was read: round %d shots [%d, %d)'
                                        % (ev['round'], ev['first'], ev['last']))
                elif t == 'loaded':
                    job.times['loaded'] = self.clock()
                elif t == 'stopped':
                    # nobody asked it to stop: the time limit did
                    if job.stop_reason is None:
                        job.timed_out = True
                    if job.stop_reason != 'queue_bound':
                        self._event(job, 'stopped', reason='time_limit' if job.timed_out else job.stop_reason,
                                    round=ev['round'], shots_in_round=ev['shots'])
            outcome, reason = self._outcome(job)
            error = None
        except TprocQueueError as e:
            outcome, reason = 'failed', 'tproc_queue_overflow'
            error = {'code': 'tproc_queue_overflow', 'queues': e.queues, 'message': str(e),
                     'traceback': traceback.format_exc()}
        except RequestError as e:
            outcome, reason = 'failed', e.code
            error = dict(e.fields)
            error.update({'code': e.code, 'message': str(e), 'traceback': None})
        except ProgramError as e:
            outcome, reason = 'failed', 'program_format'
            error = {'code': 'program_format', 'field': e.field, 'message': str(e),
                     'traceback': traceback.format_exc()}
        except LimitError as e:
            outcome, reason = 'failed', 'limit_exceeded'
            error = {'code': 'limit_exceeded', 'limit': e.limit, 'message': str(e), 'traceback': None}
        except Exception as e:
            # RFQickSoc216V1.prepare_round raises ADCInterruptError when an ADC is over range
            code = 'adc_interrupt' if type(e).__name__ == 'ADCInterruptError' else 'job_exception'
            outcome, reason = 'failed', code
            error = {'code': code, 'exc_type': type(e).__name__, 'message': str(e),
                     'traceback': traceback.format_exc()}
        if error is not None:
            self.last_error = {'job': job.id, 'code': error['code'], 'message': error['message'], 't': self.clock()}
            self._event(job, 'error', **error)
        # outputs idle, unless the program ended normally and the job asked to keep them
        normal_end = error is None and (outcome == 'complete' or reason in ('overrun', 'dropped',
                                                                             'overrun_and_dropped'))
        if normal_end and job.run.get('idle_after', True) is False:
            idle = {'done': False, 'skipped': True,
                    'reason': 'run.idle_after is false and the program ran to its end: outputs left as the '
                              'program left them'}
        else:
            if error is not None:
                why = 'error (%s)' % error['code']
            elif normal_end:
                why = 'normal end of the job'
            else:
                why = '%s (%s)' % (outcome, reason)
            try:
                idle = be.idle()
                idle['reason'] = why
            except Exception as e:
                idle = {'done': False, 'reason': why, 'error': '%s: %s' % (type(e).__name__, e),
                        'traceback': traceback.format_exc()}
        idle['t'] = self.clock()
        with self.lock:
            job.idle = idle
            self.last_idle = idle
            if idle.get('done'):
                self.outputs_idle_at = idle['t']
            job.error = error
            self._finish(job, outcome, reason)
        self._notify()

    def _outcome(self, job):
        if job.stop_reason == 'cancel':
            return 'cancelled', 'client'
        if job.stop_reason == 'image_load':
            return 'cancelled', 'image_load'
        if job.timed_out:
            return 'timed_out', 'time_limit'
        if job.bound_stop is not None:
            return 'incomplete', 'queue_bound'
        if job.overruns and job.dropped:
            return 'incomplete', 'overrun_and_dropped'
        if job.overruns:
            return 'incomplete', 'overrun'
        if job.dropped:
            return 'incomplete', 'dropped'
        return 'complete', None

    def _finish(self, job, outcome, reason):
        """Mark finished and build the done header. Caller holds the lock."""
        job.state = 'done'
        job.outcome = outcome
        job.reason = reason
        job.times['finished'] = self.clock()
        job.done_header = {
            'type': 'done', 'job': job.id, 'ref': job.ref, 'outcome': outcome, 'reason': reason,
            'error': job.error,
            'totals': {'chunks': job.next_seq, 'shots_stored': job.shots_stored, 'dropped': job.dropped,
                       'overruns': job.overruns, 'events': len(job.events),
                       'events_truncated': job.events_truncated, 'bound_stop': job.bound_stop},
            'outputs_idle': bool(job.idle and job.idle.get('done')), 'idle': job.idle,
            'outputs_idle_reason': (job.idle or {}).get('reason', 'the job never ran'),
            'times': job.times, 'events_total': len(job.events),
        }
        self.cond.notify_all()

    # --- acks, retention --------------------------------------------------

    def ack(self, job, seq):
        with self.lock:
            for s in list(job.chunks):
                if s > seq:
                    break
                c = job.chunks.pop(s)
                job.queued_bytes -= c.nbytes
            job.acked = max(job.acked, seq)
            self._maybe_free(job)
        self._notify()

    def _maybe_free(self, job):
        if job.state == 'done' and not job.chunks and job.acked >= job.next_seq - 1:
            job.freed = True

    def _expire(self):
        now = self.clock()
        for job in list(self.jobs.values()):
            if job.state == 'done' and job.chunks:
                keep = float(job.limits.get('retention_s', 3600))
                if job.times['finished'] is not None and now - job.times['finished'] > keep:
                    job.chunks.clear()
                    job.queued_bytes = 0
                    job.freed = True

    def _trim_history(self):
        while len(self.jobs) > self.history:
            for jid, j in self.jobs.items():
                if j.state == 'done' and (j.freed or not j.chunks):
                    del self.jobs[jid]
                    break
            else:
                return

    def total_queued_bytes(self):
        with self.lock:
            return sum(j.queued_bytes for j in self.jobs.values())

    # --- delivery: what a sender should send next ----------------------------

    def next_item(self, job, cursor, newest_only=False):
        """For a sender: the next ('event', ev) / ('chunk', Chunk) / ('done', hdr) / None.

        cursor is a dict {'seq', 'eseq', 'done_sent'} that this call advances.
        Events and chunks come out in the order they were produced.
        """
        with self.lock:
            ev = job.events[cursor['eseq']] if cursor['eseq'] < len(job.events) else None
            ch = None
            if job.chunks and cursor['seq'] < job.next_seq:
                if newest_only:
                    last = next(reversed(job.chunks))
                    if last >= cursor['seq']:
                        ch = job.chunks[last]
                elif cursor['seq'] in job.chunks:
                    ch = job.chunks[cursor['seq']]
                else:
                    first = next(iter(job.chunks))
                    if first > cursor['seq']:
                        ch = job.chunks[first]
            if ev is not None and (ch is None or ev['pos'] < ch.pos):
                cursor['eseq'] += 1
                return ('event', ev)
            if ch is not None:
                cursor['seq'] = ch.seq + 1
                return ('chunk', ch)
            if job.state == 'done' and not cursor['done_sent'] and cursor['eseq'] >= len(job.events):
                # all retained chunks at or past the cursor have gone out
                if not job.chunks or cursor['seq'] > next(reversed(job.chunks)) or newest_only:
                    cursor['done_sent'] = True
                    return ('done', job.done_header)
            return None
