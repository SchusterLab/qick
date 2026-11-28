"""The seam between the board server and the hardware.

The server only talks to a backend object with this interface:

    soccfg                     -> dict or None (None: no image loaded)
    load_image(bit, hwh)       -> {'soccfg', 'reset', 'duration_s'}; programs the FPGA
    attach(bit, hwh)           -> the same record; uses whatever the FPGA runs, no download
    make_program(progdict)     -> program object (raises ProgramError on a bad dictionary)
    layout(prog, run)          -> description of the result arrays
    run(prog, run, stop_event, deadline) -> generator of event dicts (see QickSocBackend.run)
    idle()                     -> stops the tProc and drives the generators with zeros
    get_state()                -> dict of board state

QickSocBackend is the real one: it holds the QickSoc and is the only thing in the
server process that touches it. It was written from the library code
(AcquireMixin.acquire/acquire_decimated/prepare_round/finish_round and
DataStreamer) but HAS NOT BEEN RUN ON A BOARD. Everything about it is unverified on
hardware until it has been measured on a loopback board (see
slab/notes/board-server-install.md).

FakeBackend is QickSocBackend driving a FakeSoc instead of a QickSoc: the server,
job and drain logic are the same code; only the QickSoc/pynq layer is simulated.
"""
import json
import os
import threading
import time
import traceback
from functools import reduce
from operator import mul

import numpy as np

from ..helpers import NpEncoder, tproc_queue_overflow_message
from ..qick_asm import QickConfig
from ..streamer import iter_accumulated
from .. import get_version


class ProgramError(Exception):
    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field


class LimitError(Exception):
    def __init__(self, message, limit):
        super().__init__(message)
        self.limit = limit


class TprocQueueError(RuntimeError):
    """The tProc lost a push into a full dispatcher queue (lab firmware flags)."""
    def __init__(self, names):
        super().__init__(tproc_queue_overflow_message(names))
        self.queues = list(names)


def _plain(obj):
    """JSON-safe deep copy (numpy scalars and arrays converted)."""
    return json.loads(json.dumps(obj, cls=NpEncoder))


class QickSocBackend:
    """Backend holding a QickSoc. UNTESTED ON HARDWARE: see the module docstring.

    soc_factory(bitfile, download) returns the soc object; the default constructs
    qick.QickSoc (with soc_kwargs) and records whether QickSoc re-initialised the
    clock chips. poll_interval: sleep between shot-counter reads that found nothing
    new (0 would spin and starve the network threads of the GIL).
    """
    is_fake = False

    def __init__(self, soc_factory=None, soc_kwargs=None, poll_interval=0.0005, soc_class='auto'):
        self._factory = soc_factory or self._qicksoc_factory
        self.soc_kwargs = dict(soc_kwargs or {})
        self.poll_interval = poll_interval
        if soc_class not in ('auto', 'QickSoc', 'RFQickSoc216V1'):
            raise ValueError('soc_class must be auto, QickSoc or RFQickSoc216V1')
        self.soc_class = soc_class
        self.soc = None
        self.rf = None          # RfFrontEnd of the loaded image, None without RF cards
        # serialises every hardware access: the drain holds it around each counter
        # read and transfer, get_state around its register reads
        self.hw_lock = threading.RLock()

    # --- image -----------------------------------------------------------

    @staticmethod
    def hwh_has_rf_board(hwh_path):
        """Whether the firmware has the QICK box RF-board control block (the IPs
        RFQickSoc216V1._rfb_config uses live under 'rfb_control')."""
        with open(hwh_path, 'rb') as f:
            return b'rfb_control' in f.read()

    def _qicksoc_factory(self, bitfile, download):
        from .. import QickSoc  # needs pynq: only importable on the board
        base = QickSoc
        cls = self.soc_class
        if cls == 'auto':
            cls = 'RFQickSoc216V1' if self.hwh_has_rf_board(os.path.splitext(bitfile)[0] + '.hwh') else 'QickSoc'
        if cls == 'RFQickSoc216V1':
            from ..rfboard import RFQickSoc216V1 as base

        class _TracedQickSoc(base):
            # QickSoc.config_clocks calls set_all_clks when it (re)programs the clock chips
            _bs_clocks_reprogrammed = False

            def set_all_clks(self, *args, **kwargs):
                self._bs_clocks_reprogrammed = True
                return super().set_all_clks(*args, **kwargs)

        return _TracedQickSoc(bitfile=bitfile, download=download, **self.soc_kwargs)

    def _make_rf(self, soc):
        # the RF cards through qick.rfbox, the same driver the stock Pyro server can use (one per soc object:
        # a new soc after a load is a new driver identity)
        from .rf import RfFrontEnd
        from .. import rfbox
        if isinstance(getattr(soc, '__dict__', {}).get('adc_cards'), list):
            return RfFrontEnd(rfbox.attach(soc))
        return None

    @property
    def soccfg(self):
        return None if self.soc is None else self.soc.get_cfg()

    def load_image(self, bit_path, hwh_path, rf_policy=None):
        """Program the FPGA with this bit/hwh pair and construct a new QickSoc.
        The .hwh must sit beside the .bit with the same stem (PYNQ finds it that way).

        rf_policy {'enabled', 'skip_before', 'safe_items', 'settle_s'}: the RF procedure
        around the load (rf.py). With enabled and RF cards on the current image, every
        readout amplifier is switched off and read back first (RfSafeFailed if that
        fails, and nothing is loaded, unless skip_before); with RF cards on the new
        image the safe state is applied and read back before returning."""
        from .rf import RfSafeFailed
        if os.path.splitext(bit_path)[0] != os.path.splitext(hwh_path)[0]:
            raise ValueError('the .hwh must be beside the .bit with the same name')
        pol = {'enabled': False, 'skip_before': False, 'safe_items': None, 'settle_s': 0.2}
        pol.update(rf_policy or {})
        rf_rec = {'procedure': 'on' if pol['enabled'] else 'off', 'before': None, 'found_after_load': None,
                  'constructor': None, 'after': None}
        with self.hw_lock:
            if self.soc is not None:
                try:
                    self.idle()
                except Exception:
                    pass
            old_rf = self.rf
            if old_rf is not None and pol['enabled']:
                if pol['skip_before']:
                    rf_rec['before'] = {'skipped': True, 'why': 'skip_rf_safe in the request'}
                else:
                    rf_rec['before'] = old_rf.before_load()     # raises RfSafeFailed: nothing loaded
            elif old_rf is not None:
                rf_rec['before'] = {'skipped': True, 'why': 'RF procedure off (--no-rf-safe-on-load)'}
            # drop the old object first: after the download its IP handles are stale
            self.soc = None
            self.rf = None
            t0 = time.monotonic()
            soc = self._factory(bit_path, True)
            duration = time.monotonic() - t0
            self.soc = soc
            self.rf = self._make_rf(soc)
            if self.rf is not None:
                rf_rec['constructor'] = self._constructor_note()
                # what the cards hold before any RF call of ours (flags not cleared)
                try:
                    rf_rec['found_after_load'] = self.rf.read()
                except Exception as e:
                    rf_rec['found_after_load'] = {'error': '%s: %s' % (type(e).__name__, e),
                                                  'traceback': traceback.format_exc()}
                if pol['enabled']:
                    try:
                        rf_rec['after'] = self.rf.after_load(pol['safe_items'], pol['settle_s'])
                    except Exception as e:
                        rf_rec['after'] = {'ok': False, 'error': '%s: %s' % (type(e).__name__, e),
                                           'traceback': traceback.format_exc()}
                    rf_rec['state'] = self.rf.read(flags=False)
            if old_rf is None and self.rf is None:
                rf_rec = None
        clocks = bool(getattr(soc, '_bs_clocks_reprogrammed', False))
        return {
            'soccfg': _plain(soc.get_cfg()),
            'duration_s': duration,
            'reset': {
                'pl_reconfigured': True,
                'clocks_reprogrammed': clocks,
                # the RF data converter is an IP in the PL: a new bitstream restarts its tiles
                'converters_restarted': True,
                'tproc_memories_cleared': True,
                'envelopes_cleared': True,
                'program_cache_cleared': True,
                'rf': _plain(rf_rec),
            },
        }

    def _constructor_note(self):
        from ..rfbox import LIBRARY_CONSTRUCTOR
        return dict(LIBRARY_CONSTRUCTOR)

    def attach(self, bit_path, hwh_path):
        """Construct QickSoc for a bitstream assumed to be on the FPGA already (no download).
        Whether it really is that bitstream cannot be checked here."""
        with self.hw_lock:
            self.soc = None
            self.rf = None
            t0 = time.monotonic()
            soc = self._factory(bit_path, False)
            duration = time.monotonic() - t0
            self.soc = soc
            self.rf = self._make_rf(soc)
            rf = None if self.rf is None else {'procedure': 'none at attach', 'found_after_load': self.rf.read()}
        return {'soccfg': _plain(soc.get_cfg()), 'duration_s': duration,
                'reset': {'pl_reconfigured': False,
                          'clocks_reprogrammed': bool(getattr(soc, '_bs_clocks_reprogrammed', False)),
                          'converters_restarted': False, 'tproc_memories_cleared': False,
                          'envelopes_cleared': False, 'program_cache_cleared': True, 'rf': _plain(rf)}}

    # --- RF front end ------------------------------------------------------

    def rf_get(self):
        if self.rf is None:
            return None
        with self.hw_lock:
            return _plain(self.rf.read())

    def rf_set(self, items, settle_s):
        with self.hw_lock:
            return _plain(self.rf.apply(items, settle_s=settle_s, protect=True))

    def rf_clear_flags(self, settle_s):
        with self.hw_lock:
            return _plain(self.rf.clear_flags(settle_s))

    # --- programs --------------------------------------------------------

    def make_program(self, progdict):
        if self.soc is None:
            raise ProgramError('no image loaded')
        tproc_type = self.soc['tprocs'][0]['type']
        if tproc_type == 'qick_processor':
            from ..asm_v2 import AcquireProgramV2 as Prog
        else:
            from ..asm_v1 import AcquireProgram as Prog
        prog = Prog(self.soc)
        for key in prog.dump_keys:
            if key not in progdict:
                raise ProgramError('program dictionary has no %r (made for a different tProc or library?)' % key,
                                   field=key)
        try:
            prog.load_prog(progdict)
        except Exception as e:
            raise ProgramError('load_prog failed: %s: %s' % (type(e).__name__, e))
        if prog.counter_addr is None or prog.loop_dims is None:
            raise ProgramError('program has no counter_addr/loop_dims (use an AcquireProgram, e.g. AveragerProgramV2)',
                               field='loop_dims')
        if prog.binprog is None:
            try:
                prog.compile()
            except Exception as e:
                raise ProgramError('compile failed: %s: %s' % (type(e).__name__, e))
        return prog

    def layout(self, prog, run):
        total = int(reduce(mul, prog.loop_dims, 1))
        readouts = []
        for i, (ch, ro) in enumerate(prog.ro_chs.items()):
            readouts.append({'index': i, 'ch': int(ch), 'reads_per_shot': int(ro['trigs']),
                             'length': int(ro['length'])})
            if run['mode'] == 'dec':
                maxlen = self.soc['readouts'][ch]['buf_maxlen']
                if ro['length'] * ro['trigs'] * total > maxlen:
                    raise LimitError('decimated readout %d: %d samples x %d trigs x %d shots exceeds buffer size %d'
                                     % (ch, ro['length'], ro['trigs'], total, maxlen), 'buf_maxlen')
            if ro['trigs'] * total >= self.soc['readouts'][ch]['avg_maxlen'] and run['mode'] == 'dec':
                raise LimitError('readout %d: accumulated buffer too small for a whole round' % ch, 'avg_maxlen')
        arrays = []
        for r in readouts:
            arrays.append({'name': 'acc:%d' % r['index'], 'dtype': '<i4',
                           'shape': ['shots*%d' % r['reads_per_shot'], 2]})
            if run['mode'] == 'dec':
                arrays.append({'name': 'dec:%d' % r['index'], 'dtype': '<i4',
                               'shape': [r['length'] * r['reads_per_shot'] * total, 2]})
        return {
            'loop_dims': [int(x) for x in prog.loop_dims],
            'avg_level': prog.avg_level,
            'counter_addr': prog.counter_addr,
            'shots_per_round': total,
            'rounds': run['rounds'],
            'mode': run['mode'],
            'readouts': readouts,
            'arrays': arrays,
            'order': ('acc:i rows are readout i values in time order: shot s (C order over loop_dims), '
                      'then read 0..reads_per_shot-1; dec:i is one whole round per chunk'),
        }

    # --- running ---------------------------------------------------------

    def run(self, prog, run, stop_event, deadline=None):
        """Run a program for run['rounds'] rounds, yielding events as data arrives:

        {'type': 'loaded'}                         program, envelopes, gens, readouts configured
        {'type': 'round_start', 'round': r}
        {'type': 'data', 'round', 'buffer', 'first', 'last', 'arrays': [(name, ndarray)]}
        {'type': 'overrun', 'round', 'first', 'last', 'info'}   shots lost in the firmware ring
        {'type': 'stopped', 'round', 'shots'}      stop_event or deadline; nothing follows
        {'type': 'round_end', 'round': r}

        A lost dispatcher push raises TprocQueueError at the end of the round, as
        finish_round does via check_tproc_queues. The caller makes the outputs idle.
        The sequence follows AcquireMixin.acquire / acquire_decimated.
        """
        soc = self.soc
        lock = self.hw_lock
        mode = run['mode']
        start_src = run.get('start_src', 'internal')
        total = int(reduce(mul, prog.loop_dims, 1))
        ch_list = list(prog.ro_chs)
        reads = [ro['trigs'] for ro in prog.ro_chs.values()]

        with lock:
            # load the program - data memory is loaded per round by reload_mem()
            prog.config_all(soc, load_envelopes=True, load_mem=False)
        yield {'type': 'loaded'}

        fake_hook = getattr(soc, 'fake_round_info', None) if self.is_fake else None

        for r in range(run['rounds']):
            if stop_event.is_set() or (deadline is not None and time.monotonic() > deadline):
                yield {'type': 'stopped', 'round': r, 'shots': 0}
                return
            with lock:
                # same order as AcquireMixin.prepare_round (lab fork: reset before arming buffers)
                soc.clear_tproc_counter(addr=prog.counter_addr)
                prog.config_bufs(soc, enable_avg=True, enable_buf=(mode == 'dec'))
                soc.reload_mem()
                soc.prepare_round()
                soc.start_src(start_src)
            if fake_hook is not None:
                fake_hook(r, total, ch_list, reads)
            yield {'type': 'round_start', 'round': r}

            if mode == 'acc':
                for item in iter_accumulated(soc, total, prog.counter_addr, ch_list, reads,
                                             stride=run.get('stride'), stop_flag=stop_event,
                                             poll_interval=self.poll_interval, lock=lock, deadline=deadline):
                    if item[0] == 'data':
                        _, first, last, bufs, stats = item
                        yield {'type': 'data', 'round': r, 'buffer': 'acc', 'first': first, 'last': last,
                               'arrays': [('acc:%d' % i, b) for i, b in enumerate(bufs)]}
                    elif item[0] == 'overrun':
                        _, first, last, info = item
                        yield {'type': 'overrun', 'round': r, 'first': first, 'last': last, 'info': info}
                    else:
                        with lock:
                            soc.cleanup_round()
                        yield {'type': 'stopped', 'round': r, 'shots': item[1]}
                        return
            else:
                with lock:
                    soc.start_tproc()
                count = 0
                stopped = False
                while count < total:
                    if stop_event.is_set() or (deadline is not None and time.monotonic() > deadline):
                        stopped = True
                        break
                    with lock:
                        count = soc.get_tproc_counter(addr=prog.counter_addr)
                    if count < total and self.poll_interval:
                        time.sleep(self.poll_interval)
                with lock:
                    soc.start_src('internal')
                if stopped:
                    with lock:
                        soc.cleanup_round()
                    yield {'type': 'stopped', 'round': r, 'shots': count}
                    return
                arrays = []
                with lock:
                    for i, (ch, ro) in enumerate(prog.ro_chs.items()):
                        arrays.append(('dec:%d' % i, soc.get_decimated(ch=ch, address=0,
                                                                       length=ro['length'] * ro['trigs'] * total)))
                        arrays.append(('acc:%d' % i, soc.get_accumulated(ch=ch, address=0,
                                                                         length=ro['trigs'] * total)))
                yield {'type': 'data', 'round': r, 'buffer': 'dec', 'first': 0, 'last': total, 'arrays': arrays}

            with lock:
                soc.cleanup_round()
                if prog.tproccfg.get('queue_flags', False):
                    names = soc.tproc.get_queue_overflows()
                    if names:
                        raise TprocQueueError(names)
            yield {'type': 'round_end', 'round': r}

    def idle(self):
        """Stop the tProc and run the library's all-zeros program on every generator
        (QickSoc.reset_gens). What the DAC outputs do is not measured here."""
        if self.soc is None:
            return {'done': False, 'reason': 'no image loaded'}
        with self.hw_lock:
            self.soc.stop_tproc(lazy=False)
            self.soc.reset_gens()
            self.soc.start_src('internal')
        return {'done': True, 'tproc_stopped': True, 'gens_reset': True}

    # --- state -----------------------------------------------------------

    def get_state(self):
        if self.soc is None:
            return {'image_loaded': False}
        soc = self.soc
        st = {'image_loaded': True, 'board': soc['board'], 'fw_timestamp': soc['fw_timestamp']}
        tp = {'type': soc['tprocs'][0]['type'], 'revision': soc['tprocs'][0].get('revision')}
        tproc = getattr(soc, 'tproc', None)
        with self.hw_lock:
            if tproc is not None and hasattr(tproc, 'get_status'):
                for name, fn in [('status', 'get_status'), ('axi_regs', 'get_axi_regs'), ('debug', 'get_debug')]:
                    try:
                        tp[name] = getattr(tproc, fn)()
                    except Exception as e:
                        tp[name] = {'error': '%s: %s' % (type(e).__name__, e)}
                try:
                    tp['queue_flags'] = {'supported': bool(soc['tprocs'][0].get('queue_flags', False)),
                                         'lost': tproc.get_queue_overflows()}
                except Exception as e:
                    tp['queue_flags'] = {'error': '%s: %s' % (type(e).__name__, e)}
            try:
                st['clocks_locked'] = bool(soc.clocks_locked())
            except Exception as e:
                st['clocks_locked'] = None
            if self.rf is not None:
                try:
                    st['rf'] = self.rf.read()
                except Exception as e:
                    st['rf'] = {'error': '%s: %s' % (type(e).__name__, e)}
            else:
                st['rf'] = None
        st['tproc'] = tp
        st['not_implemented'] = ['dac_settings', 'mixer_nco', 'reference_lock_detail', 'temperatures']
        if self.rf is None:
            st['not_implemented'].insert(0, 'adc_attenuators (only read on images with RF cards, in rf)')
        return _plain(st)


# =========================================================================
# Fake board
# =========================================================================

def fake_acc_values(ch, rnd, points):
    """Deterministic accumulated (I, Q) values a FakeSoc produces for readout ch in
    round rnd at ring point indices `points` (point = shot*reads_per_shot + read)."""
    p = np.asarray(points, dtype=np.int64)
    i = (p * 7 + ch * 1009 + rnd * 100003) % 2000003 - 1000001
    q = -(p * 13 + ch * 31 + rnd * 7919) % 1000003
    return np.stack([i, q], axis=-1).astype(np.int32)


def fake_dec_values(ch, rnd, n, start=0):
    k = np.arange(start, start + n, dtype=np.int64)
    i = ((k * 3 + ch * 17 + rnd * 101) % 4001) - 2000
    q = ((k * 5 + ch * 23 + rnd * 103) % 3001) - 1500
    return np.stack([i, q], axis=-1).astype(np.int32)


class _FakeTproc:
    def __init__(self, soc):
        self.soc = soc
        self.regs = {name: 0 for name in ['tproc_ctrl', 'tproc_cfg', 'mem_addr', 'mem_len', 'mem_dt_i',
                                          'axi_w_dt1', 'axi_w_dt2', 'core_cfg', 'read_sel', 'queue_flags',
                                          'mem_dt_o', 'axi_r_dt1', 'axi_r_dt2', 'time_usr', 'tproc_status',
                                          'tproc_debug']}
        self.regs['queue_flags'] = 9

    def get_queue_overflows(self):
        return list(self.soc.fake_current_queue_error())

    def get_axi_regs(self):
        regs = dict(self.regs)
        regs['axi_r_dt1'] = self.soc.get_tproc_counter(1)
        return regs

    def get_status(self):
        running = self.soc._running and self.soc._count_now() < self.soc._total
        return {'raw': 0, 'core_state': 'C_RUN' if running else 'C_STOP', 'time_state': 'T_RUN',
                'fake': True}

    def get_debug(self):
        return {'fifo': {'raw': 0}, 'fake': True}


class FakeSoc(QickConfig):
    """A stand-in for QickSoc: a QickConfig with the methods the backend calls.

    Shots are produced at `shot_rate` per second from start_tproc(); the
    accumulated buffer of each readout is a ring of avg_maxlen points, and reading
    returns whatever the ring holds at that moment, so a drain that falls behind
    gets overwritten data exactly as on hardware. Values are fake_acc_values().

    Injections (attributes, also settable per job through run.fake):
      shot_rate          shots per second (default 1e5)
      read_delay_s       extra seconds per get_accumulated call (a slow drain)
      queue_error        {'round': r, 'queues': ['data']}: queue flags set after round r
      exception_at_shot  raise RuntimeError in get_accumulated once this shot is reached
    Any other QickSoc method called by the program (load_envelope, set_nyquist, ...) is
    accepted and recorded in self.calls.
    """
    TPROC_VERSION = 2

    def __init__(self, cfg, **inject):
        cfg = json.loads(json.dumps(cfg))
        cfg['sw_version'] = get_version()
        super().__init__(cfg)
        self.calls = []
        self.tproc = _FakeTproc(self)
        self.defaults = {'shot_rate': 1e5, 'read_delay_s': 0.0, 'queue_error': None, 'exception_at_shot': None}
        self.defaults.update(inject)
        self.inject = dict(self.defaults)
        self._start_src = 'internal'
        self._running = False
        self._t0 = 0.0
        self._total = 0
        self._round = 0
        self._reads = {}
        self._queue_error_now = []

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)

        def recorder(*args, **kwargs):
            self.calls.append(name)
        return recorder

    # --- per-job injection (FakeBackend.run applies run.fake) ---
    def fake_set_inject(self, overrides):
        self.inject = dict(self.defaults)
        self.inject.update(overrides or {})

    def fake_round_info(self, rnd, total, ch_list, reads):
        self._round = rnd
        self._total = total
        self._reads = dict(zip(ch_list, reads))
        self._queue_error_now = []

    def fake_current_queue_error(self):
        qe = self.inject.get('queue_error')
        if qe and qe.get('round', 0) == self._round:
            return qe.get('queues', ['trigger'])
        return []

    # --- tProc ---
    def start_src(self, src):
        self.calls.append('start_src')
        self._start_src = src

    def start_tproc(self):
        self.calls.append('start_tproc')
        self._running = True
        self._t0 = time.monotonic()

    def stop_tproc(self, lazy=False):
        self.calls.append('stop_tproc')
        self._running = False

    def clear_tproc_counter(self, addr):
        self.calls.append('clear_tproc_counter')
        self._running = False
        self._t0 = time.monotonic()

    def reset_gens(self):
        self.calls.append('reset_gens')
        self._running = False

    def _count_now(self):
        if not self._running:
            return 0
        n = int((time.monotonic() - self._t0) * float(self.inject['shot_rate']))
        return min(n, self._total)

    def get_tproc_counter(self, addr):
        # like tProc v2: holds its last value until the next reset
        return self._count_now()

    def clocks_locked(self):
        return True

    # --- buffers ---
    def get_accumulated(self, ch, address=0, length=None):
        maxlen = self['readouts'][ch]['avg_maxlen']
        if length >= maxlen:
            raise RuntimeError("length=%d longer than %d" % (length, maxlen))
        delay = float(self.inject.get('read_delay_s') or 0)
        n = self._reads.get(ch, 1)
        written = self._count_now() * n
        exc = self.inject.get('exception_at_shot')
        if exc is not None and written >= exc * n:
            raise RuntimeError('fake board: injected exception at shot %d' % exc)
        slots = (address + np.arange(length)) % maxlen
        # the newest point each slot holds: q = slot + maxlen*k, q < written
        k = (written - 1 - slots) // maxlen
        q = slots + maxlen * k
        w = (self.weights or {}).get(ch)
        if w is not None and self['readouts'][ch].get('has_weights'):
            # weighted buffer: the accumulated value of window q is the sum of that
            # window's weighted (decimated) samples, 32-bit (model.accumulate)
            L = len(w)
            data = np.zeros((length, 2), dtype=np.int32)
            for i, qq in enumerate(q):
                if qq >= 0:
                    y = fake_weighted(fake_dec_values(ch, self._round, L, start=int(qq) * L), w)
                    sm = y.astype(np.int64).sum(axis=0)
                    data[i] = ((sm + 2**31) % 2**32) - 2**31
        else:
            data = fake_acc_values(ch, self._round, q)
        data[q < 0] = 0
        if delay:
            time.sleep(delay)
        return data

    def get_decimated(self, ch, address=0, length=None):
        maxlen = self['readouts'][ch]['buf_maxlen']
        if length >= maxlen:
            raise RuntimeError("requested length=%d longer or equal to decimated buffer size=%d" % (length, maxlen))
        x = fake_dec_values(ch, self._round, length)
        w = (self.weights or {}).get(ch)
        if w is not None and self['readouts'][ch].get('has_weights'):
            # weighted buffer: sample n of each window times weight n (window = readout length)
            n = len(w)
            x = fake_weighted(x, w[np.arange(length) % n]).astype(np.int32)
        return x

    # --- weights ---
    weights = None

    def load_weights(self, ch, data, addr=0):
        self.calls.append('load_weights')
        if self.weights is None:
            self.weights = {}
        self.weights[ch] = np.array(data, dtype=np.int16)

    # --- RF front end (only for images with 'fake_rf') ---
    fake_rf = None

    def prepare_round(self):
        self.calls.append('prepare_round')
        if self.fake_rf is not None:
            # like RFQickSoc216V1.prepare_round: clear_interrupts(error_on_interrupt=True)
            raised = {}
            for name, cfg in self['rf']['adcs'].items():
                w = self.fake_rf.flags(name, clear=True)
                if w:
                    raised[name] = hex(w)
            if raised:
                raise FakeADCInterruptError('fake board: ADC interrupts raised: %s' % raised)


class FakeADCInterruptError(Exception):
    """Stands for qick.helpers.ADCInterruptError (same name for the job error code)."""


FakeADCInterruptError.__name__ = 'ADCInterruptError'


def fake_weighted(x, w):
    """The weighted buffer's product, copied from slab/sim/weighted_buffer/model.py
    weighted() (the bit-exact model of axis_weighted_buffer v1.3):
        y_I = (I*wQ + Q*wI) >> 16,  y_Q = (Q*wQ - I*wI) >> 16  (32-bit wrap, floor)."""
    x = np.asarray(x, dtype=np.int64)
    w = np.asarray(w, dtype=np.int64)
    I, Q = x[..., 0], x[..., 1]
    wI, wQ = w[..., 0], w[..., 1]

    def wrap32(v):
        return ((v + 2**31) % 2**32) - 2**31
    yI = wrap32(Q * wI + I * wQ) >> 16
    yQ = wrap32(Q * wQ - I * wI) >> 16
    return np.stack([yI, yQ], axis=-1).astype(np.int16)


def fake_soc_factory_from_hwh(inject=None, rf_hardware=None):
    """Factory for FakeBackend: a fake image's .hwh file is the soccfg as JSON.
    An image with 'fake_rf' {'layout'} has RF cards: rf_hardware (a qick.rfbox_fake.FakeQickBox,
    shared across loads) gives the FakeSoc the RFQickSoc216V1 attributes, as its constructor would."""
    def factory(bitfile, download):
        hwh = os.path.splitext(bitfile)[0] + '.hwh'
        with open(hwh) as f:
            doc = json.load(f)
        soc = FakeSoc(doc['fake_soccfg'], **(inject or {}))
        soc._bs_clocks_reprogrammed = bool(doc.get('fake_clocks_reprogrammed', False))
        delay = float(doc.get('fake_load_delay_s', 0.05))
        frf = doc.get('fake_rf')
        if rf_hardware is not None:
            blocks = sorted(soc['rf']['adcs'])
            if frf:
                rf_hardware.configure(frf['layout'], blocks)
            if download:
                # the converter restarts whatever the image (the cards are on the box, not in the PL)
                for b in blocks:
                    rf_hardware.dsa.setdefault(b, 0.0)
                    rf_hardware.latched.setdefault(b, 0)
                rf_hardware.pl_load()
        if delay:
            time.sleep(delay)
        if frf and rf_hardware is not None:
            rf_hardware.make_soc(cfg=soc.get_cfg(), target=soc, download=False)
            soc.fake_rf = rf_hardware
        return soc
    return factory


class FakeBackend(QickSocBackend):
    """QickSocBackend over FakeSoc. Fake images: the .hwh is JSON with 'fake_soccfg'
    (and optionally 'fake_clocks_reprogrammed', 'fake_load_delay_s', 'fake_rf'); the
    .bit is any bytes. Per-job injections come from run['fake']. The fake RF cards
    (qick.rfbox_fake.FakeQickBox) are self.rf_hardware and keep their state across loads."""
    is_fake = True

    def __init__(self, inject=None, poll_interval=0.0002, rf_threshold_db=10.0):
        from ..rfbox_fake import FakeQickBox
        self.rf_hardware = FakeQickBox(layout=FAKE_RF_LAYOUT, threshold_db=rf_threshold_db)
        super().__init__(soc_factory=fake_soc_factory_from_hwh(inject, self.rf_hardware), poll_interval=poll_interval)

    def _make_rf(self, soc):
        from .rf import RfFrontEnd
        from .. import rfbox
        if soc.fake_rf is None:
            return None
        return RfFrontEnd(rfbox.attach(soc))

    def _constructor_note(self):
        from ..rfbox import LIBRARY_CONSTRUCTOR
        note = dict(LIBRARY_CONSTRUCTOR)
        note['fake'] = ('fake board (qick.rfbox_fake): the constructor sequence above over fake chips; the GPIO '
                        'defaults write does not land (amplifier state kept across loads, as measured on an rfbv2 '
                        'box) and the attenuator writes made with the card deselected are lost')
        return note

    def run(self, prog, run, stop_event, deadline=None):
        self.soc.fake_set_inject(run.get('fake'))
        return super().run(prog, run, stop_event, deadline)


_HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_SOCCFG_PATH = os.path.join(_HERE, 'fake_soccfg.json')


FAKE_VARIANTS = ('fake-a', 'fake-b', 'fake-rf', 'fake-w', 'fake-w27')

# the fake QICK box: RF Out on DAC slot 0, DC Out on slot 1, RF In on ADC slot 2
# (ports 4, 5: ADC blocks 20, 21), DC In on slot 3 (ports 6, 7: blocks 22, 23)
FAKE_RF_LAYOUT = {
    'dac': [{'kind': 'rf_out', 'name': 'RF Out', 'ports': [0, 1, 2, 3]},
            {'kind': 'dc_out', 'name': 'DC Out', 'ports': [4, 5, 6, 7]}, None, None],
    'adc': [None, None, {'kind': 'rf_in', 'name': 'RF In', 'ports': [4, 5]},
            {'kind': 'dc_in', 'name': 'DC In', 'ports': [6, 7]}],
}


def fake_image_files(variant):
    """(bit bytes, hwh bytes) of a built-in fake image. Variants differ in soccfg:
    'fake-a' is the QICK testbench configuration (one dynamic readout, avg ring 8192);
    'fake-b' has fw_timestamp changed, avg ring 1024 and buf 2048, and reports that
    loading it re-programmed the clock chips;
    'fake-rf' is fake-a on a QICK box with RF cards (FAKE_RF_LAYOUT): readout 0 on
    ADC 20 (RF In), a second readout on ADC 22 (DC In), generator 0 on RF Out;
    'fake-w' is fake-a with a weighted readout buffer as the lab firmware has it (tProc
    revision 29, wgt_maxlen 8192); 'fake-w27' the same at revision 27 with 1024 weights."""
    with open(FAKE_SOCCFG_PATH) as f:
        cfg = json.load(f)
    cfg['tprocs'][0]['queue_flags'] = True
    doc = {'fake_soccfg': cfg, 'fake_clocks_reprogrammed': False}
    if variant == 'fake-b':
        cfg['fw_timestamp'] = 'fake-b ' + cfg['fw_timestamp']
        for ro in cfg['readouts']:
            ro['avg_maxlen'] = 1024
            ro['buf_maxlen'] = 2048
        doc['fake_clocks_reprogrammed'] = True
    elif variant == 'fake-rf':
        cfg['fw_timestamp'] = 'fake-rf ' + cfg['fw_timestamp']
        ro2 = json.loads(json.dumps(cfg['readouts'][0]))
        ro2['adc'] = '22'
        ro2['trigger_bit'] = 1
        ro2['avgbuf_fullpath'] = 'readout_wrapper/axis_avg_buffer_1'
        ro2['ro_fullpath'] = 'readout_wrapper/axis_dyn_readout_v1_1'
        cfg['readouts'].append(ro2)
        # every port of the cards has its converter block: ADC 23 for DC In port 7
        cfg['rf']['adcs']['23'] = dict(cfg['rf']['adcs']['22'], index=[2, 3])
        cfg['rf']['tiles']['adc']['2']['blocks'] = ['20', '21', '22', '23']
        # a generator on the DC Out card (DAC 10 = port 4)
        g2 = json.loads(json.dumps(cfg['gens'][0]))
        g2['dac'] = '10'
        g2['fullpath'] = g2.get('fullpath', 'gen') + '_dc'
        cfg['gens'].append(g2)
        # a real rfbv2 .hwh names the RF-board control block; QickSocBackend's auto
        # class choice looks for it
        doc['fake_rf'] = {'layout': FAKE_RF_LAYOUT, 'constructor_amps_off': False, 'hwh_marker': 'rfb_control'}
    elif variant in ('fake-w', 'fake-w27'):
        # fake-w: like the lab firmware (tProc revision 29 with queue flags, 8192 weights);
        # fake-w27: like an upstream image (revision 27, no queue flags, 1024 weights)
        lab = variant == 'fake-w'
        cfg['fw_timestamp'] = variant + ' ' + cfg['fw_timestamp']
        cfg['tprocs'][0]['revision'] = 29 if lab else 27
        cfg['tprocs'][0]['queue_flags'] = lab
        ro = cfg['readouts'][0]
        ro['has_weights'] = True
        ro['wgt_maxlen'] = 8192 if lab else 1024
        ro['avgbuf_type'] = 'axis_weighted_buffer'
        ro['avgbuf_version'] = '1.3'
    elif variant != 'fake-a':
        raise ValueError('unknown fake image %r' % variant)
    bit = ('FAKE BITSTREAM %s\n' % variant).encode('ascii')
    hwh = json.dumps(doc, indent=1, sort_keys=True).encode('utf-8')
    return bit, hwh
