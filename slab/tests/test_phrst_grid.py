"""Off-board tests for scheduling phase resets on legal ticks (tProc v2).

The board configuration is the saved ZCU216 tProc v2 configuration in
firmware/testbench/qick_testbench/soccfg.json (tProc timing clock 430.08 MHz,
one axis_signal_gen_v6, one axis_dyn_readout_v1). It is old but compiles
programs; only its printed description fails.

Run:  .venv/bin/python -m pytest slab/tests
Regenerate the reference programs (only from a library without the check):
      .venv/bin/python slab/tests/test_phrst_grid.py --golden
"""
import copy
import json
import os
import sys

import pytest

from qick import QickConfig
from qick.asm_v2 import AsmV2, QickProgramV2, AveragerProgramV2, QickSweep1D

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, '..', '..', 'firmware', 'testbench', 'qick_testbench', 'soccfg.json')
GOLDEN_PATH = os.path.join(HERE, 'data', 'phrst_grid_golden.json')

P = 28  # common period of the clocks on the lab ZCU216, in tProc cycles


def make_soccfg(period=None):
    """period=None leaves the key out, as in configs saved before this change."""
    with open(CFG_PATH) as f:
        cfg = json.load(f)
    if period is not None:
        cfg['tprocs'][0]['time_sync_period'] = period
    return QickConfig(cfg)


def cyc(soccfg, n):
    """n tProc cycles, in us."""
    return soccfg.cycles2us(n)


def declare(prog, gen_phrst=None, ro_phrst=None):
    prog.declare_gen(ch=0, nqz=1)
    prog.declare_readout(ch=0, length=0.5)
    ro = dict(freq=100, gen_ch=0)
    if ro_phrst is not None:
        ro['phrst'] = ro_phrst
    prog.add_readoutconfig(ch=0, name='ro', **ro)
    g = dict(style='const', freq=100, phase=0, gain=0.5, length=0.1)
    if gen_phrst is not None:
        g['phrst'] = gen_phrst
    prog.add_pulse(ch=0, name='p', **g)


def flat_prog(soccfg, t_pulse, t_ro=0, delay=None, gen_phrst=1, ro_phrst=None):
    prog = QickProgramV2(soccfg)
    declare(prog, gen_phrst=gen_phrst, ro_phrst=ro_phrst)
    if delay is not None:
        prog.delay(delay)
    prog.send_readoutconfig(ch=0, name='ro', t=t_ro)
    prog.pulse(ch=0, name='p', t=t_pulse, tag='the_pulse')
    prog.trigger(ros=[0], t=t_pulse)
    prog.end()
    prog.compile()
    return prog


class ShotProg(AveragerProgramV2):
    """A shot loop with an optional swept delay and an optional phase reset."""
    def _initialize(self, cfg):
        declare(self, gen_phrst=cfg.get('gen_phrst'), ro_phrst=cfg.get('ro_phrst'))
        if 'sweep' in cfg:
            self.add_loop('sweep', cfg['sweep_n'])
        if cfg.get('ro_in_init'):
            self.send_readoutconfig(ch=0, name='ro', t=0)

    def _body(self, cfg):
        if not cfg.get('ro_in_init'):
            self.send_readoutconfig(ch=0, name='ro', t=0)
        self.pulse(ch=0, name='p', t=0)
        if 'sweep' in cfg:
            self.delay(cfg['sweep'], tag='swept')
        self.trigger(ros=[0], t=0.2)
        if 'body_delay' in cfg:
            self.delay(cfg['body_delay'])


class SubProg(AveragerProgramV2):
    """Uses a subroutine (labels and calls)."""
    def _initialize(self, cfg):
        declare(self, gen_phrst=cfg.get('gen_phrst'))
        sub = AsmV2()
        sub.pulse(ch=0, name='p', t=0)
        sub.delay(cfg.get('sub_delay', 1.0))
        self.add_subroutine('sub', sub)

    def _body(self, cfg):
        self.call('sub')
        self.trigger(ros=[0], t=0.2)


def golden_programs(soccfg):
    """Programs compared against the library before this change. They are
    compiled without a period, so whether they would pass the check does not matter."""
    progs = {}
    progs['flat'] = flat_prog(soccfg, t_pulse=0.123, t_ro=0.05, delay=0.37, ro_phrst=1)
    progs['shots'] = ShotProg(soccfg, reps=10, final_delay=1.0, cfg={'gen_phrst': 1})
    progs['swept'] = ShotProg(soccfg, reps=10, final_delay=0.5,
                              cfg={'gen_phrst': 1, 'sweep': QickSweep1D('sweep', 0.1, 1.3), 'sweep_n': 7})
    progs['sub'] = SubProg(soccfg, reps=5, final_delay=0.3, cfg={'gen_phrst': 1})
    # flat-top pulse and a resync
    p = QickProgramV2(soccfg)
    declare(p)
    p.add_gauss(ch=0, name='g', sigma=0.02, length=0.08, even_length=True)
    p.add_pulse(ch=0, name='ft', style='flat_top', envelope='g', freq=50, phase=10, gain=0.3, length=0.2, phrst=1)
    p.pulse(ch=0, name='ft', t=0.3)
    p.resync()
    p.delay_auto(0.1)
    p.pulse(ch=0, name='ft', t='auto')
    p.end()
    p.compile()
    progs['flattop'] = p
    return progs


def dump(prog):
    return json.loads(json.dumps({'pmem': prog.binprog['pmem'], 'wmem': prog.binprog['wmem'],
                                  'asm': prog.asm()}, default=lambda x: x.tolist() if hasattr(x, 'tolist') else int(x)))


# --- no period: no change ---------------------------------------------------

@pytest.mark.parametrize('period', [None, 0])
def test_no_period_identical_to_before(period):
    with open(GOLDEN_PATH) as f:
        golden = json.load(f)
    progs = golden_programs(make_soccfg(period))
    assert sorted(progs) == sorted(golden)
    for name, prog in progs.items():
        assert dump(prog) == golden[name], name


def test_period_absent_reads_as_zero():
    soccfg = make_soccfg()
    assert 'time_sync_period' not in soccfg['tprocs'][0]
    assert soccfg.time_sync_period_us() == 0
    assert soccfg.round_up_to_sync_tick(0.1234) == 0.1234


def _tproc_cfg(monkeypatch, params):
    """Run the tProc v2 driver's _init_config on a fake IP description (no pynq here, so stub it)."""
    import types
    for mod, attrs in {'pynq': {}, 'pynq.buffer': {'allocate': None}, 'pynq.overlay': {'DefaultIP': object}}.items():
        if mod not in sys.modules:
            monkeypatch.setitem(sys.modules, mod, types.SimpleNamespace(**attrs))
    from qick.drivers.tproc import Axis_QICK_Proc
    drv = Axis_QICK_Proc.__new__(Axis_QICK_Proc)
    drv._cfg = {}
    base = {'PMEM_AW': 12, 'DMEM_AW': 14, 'WMEM_AW': 10, 'REG_AW': 4, 'IN_PORT_QTY': 1, 'OUT_TRIG_QTY': 20,
            'OUT_DPORT_QTY': 1, 'OUT_DPORT_DW': 8, 'OUT_WPORT_QTY': 16, 'LFSR': 1, 'DIVIDER': 1, 'ARITH': 1,
            'TIME_READ': 1, 'QCOM': 0, 'CUSTOM_PERIPH': 1, 'IO_CTRL': 1, 'EXT_FLAG': 0, 'QNET': 0,
            'FIFO_DEPTH': 9, 'CALL_DEPTH': 255, 'DEBUG': 3}
    base.update(params)
    drv._init_config({'parameters': {k: str(v) for k, v in base.items()}})
    return drv.cfg


def test_driver_reads_period(monkeypatch):
    assert _tproc_cfg(monkeypatch, {})['time_sync_period'] == 0
    assert _tproc_cfg(monkeypatch, {'TIME_SYNC_PERIOD': 28})['time_sync_period'] == 28


# --- helpers ----------------------------------------------------------------

def test_helpers():
    soccfg = make_soccfg(P)
    assert soccfg.time_sync_period_us() == pytest.approx(P / 430.08)
    for t in [0, 0.001, 0.1, 1.0, cyc(soccfg, 3 * P)]:
        r = soccfg.round_up_to_sync_tick(t)
        n = soccfg.us2cycles(r)
        assert n % P == 0
        assert r >= t - 1e-12
        assert r - t < soccfg.time_sync_period_us() + 1e-9
    assert soccfg.us2cycles(soccfg.round_up_to_sync_tick(cyc(soccfg, 3 * P))) == 3 * P


# --- straight-line programs -------------------------------------------------

def test_aligned_pulse_accepted():
    soccfg = make_soccfg(P)
    prog = flat_prog(soccfg, t_pulse=cyc(soccfg, 5 * P))
    assert prog.binprog is not None


def test_aligned_after_delay_accepted():
    soccfg = make_soccfg(P)
    # delay of 10 cycles, pulse at t = 18 cycles: absolute time 28
    flat_prog(soccfg, t_pulse=cyc(soccfg, 18), delay=cyc(soccfg, 10))


def test_misaligned_pulse_rejected():
    soccfg = make_soccfg(P)
    with pytest.raises(RuntimeError) as e:
        flat_prog(soccfg, t_pulse=cyc(soccfg, 5 * P + 3))
    msg = str(e.value)
    assert "pulse 'p'" in msg and 'gen 0' in msg
    assert '%d cycles' % (5 * P + 3) in msg
    assert '28 cycles' in msg
    # nearest legal times, in us
    assert '%.4f us' % cyc(soccfg, 5 * P) in msg
    assert '%.4f us' % cyc(soccfg, 6 * P) in msg


def test_misaligned_by_delay_rejected():
    soccfg = make_soccfg(P)
    with pytest.raises(RuntimeError, match='not a multiple'):
        flat_prog(soccfg, t_pulse=cyc(soccfg, 2 * P), delay=cyc(soccfg, 5))


def test_no_phrst_not_checked():
    soccfg = make_soccfg(P)
    flat_prog(soccfg, t_pulse=cyc(soccfg, 5 * P + 3), delay=cyc(soccfg, 7), gen_phrst=0)


def test_readout_phrst():
    soccfg = make_soccfg(P)
    flat_prog(soccfg, t_pulse=cyc(soccfg, P), gen_phrst=0, ro_phrst=1, t_ro=cyc(soccfg, 2 * P))
    with pytest.raises(RuntimeError) as e:
        flat_prog(soccfg, t_pulse=cyc(soccfg, P), gen_phrst=0, ro_phrst=1, t_ro=cyc(soccfg, 2 * P + 1))
    assert "readout config 'ro' on readout 0" in str(e.value)


def test_raw_phrst_waveform():
    soccfg = make_soccfg(P)
    prog = QickProgramV2(soccfg)
    declare(prog)
    prog.add_raw_pulse('reset', ['phrst'], gen_ch=0)
    prog.pulse(ch=0, name='reset', t=cyc(soccfg, 11))
    prog.end()
    with pytest.raises(RuntimeError, match="pulse 'reset'"):
        prog.compile()


def test_resync_then_phrst_rejected():
    soccfg = make_soccfg(P)
    prog = QickProgramV2(soccfg)
    declare(prog, gen_phrst=1)
    prog.pulse(ch=0, name='p', t=0)      # fine, before the resync
    prog.resync()
    prog.pulse(ch=0, name='p', t=cyc(soccfg, 5 * P))
    prog.end()
    with pytest.raises(RuntimeError, match='resync'):
        prog.compile()


def test_raw_time_instruction():
    soccfg = make_soccfg(P)
    prog = QickProgramV2(soccfg)
    declare(prog, gen_phrst=1)
    prog.asm_inst({'CMD': 'TIME', 'C_OP': 'inc_ref', 'LIT': '#%d' % (2 * P)})
    prog.pulse(ch=0, name='p', t=0)
    prog.asm_inst({'CMD': 'TIME', 'C_OP': 'updt', 'LIT': '#5'})
    prog.pulse(ch=0, name='p', t=cyc(soccfg, 2 * P))
    prog.end()
    with pytest.raises(RuntimeError, match='updt'):
        prog.compile()


def test_phrst_while_generator_busy_rejected():
    # the 0.1 us pulse is still playing at 1 period (65 ns): the reset would happen when it ends, not at t
    soccfg = make_soccfg(P)
    prog = QickProgramV2(soccfg)
    declare(prog, gen_phrst=1)
    prog.pulse(ch=0, name='p', t=0)
    prog.pulse(ch=0, name='p', t=cyc(soccfg, P))
    prog.end()
    with pytest.raises(RuntimeError, match='before the end of the previous pulse'):
        prog.compile()


def test_delay_align():
    soccfg = make_soccfg(P)
    prog = QickProgramV2(soccfg)
    declare(prog, gen_phrst=1)
    prog.pulse(ch=0, name='p', t=0)
    prog.delay_auto(0.01, align=True)  # end of the pulse + 10 ns, rounded up
    prog.pulse(ch=0, name='p', t=0)
    prog.delay(0.3, align=True)
    prog.pulse(ch=0, name='p', t=0)
    prog.end()
    prog.compile()


# --- loops --------------------------------------------------------------------

def shot_cfg(soccfg, **kw):
    cfg = {'gen_phrst': 1}
    cfg.update(kw)
    return cfg


def test_loop_body_multiple_of_period_accepted():
    soccfg = make_soccfg(P)
    # body length = 0 (pulse) + final_delay; initial_delay and final_delay on the grid
    ShotProg(soccfg, reps=10, initial_delay=cyc(soccfg, 20 * P), final_delay=None,
             cfg=shot_cfg(soccfg, body_delay=cyc(soccfg, 100 * P)))


def test_loop_body_not_multiple_rejected():
    soccfg = make_soccfg(P)
    with pytest.raises(RuntimeError) as e:
        ShotProg(soccfg, reps=10, initial_delay=cyc(soccfg, 20 * P), final_delay=None,
                 cfg=shot_cfg(soccfg, body_delay=cyc(soccfg, 100 * P + 4)))
    msg = str(e.value)
    assert "loop 'reps'" in msg and '4 cycles' in msg and "pulse 'p'" in msg


def test_loop_body_not_multiple_with_one_iteration_accepted():
    soccfg = make_soccfg(P)
    ShotProg(soccfg, reps=1, initial_delay=cyc(soccfg, 20 * P), final_delay=None,
             cfg=shot_cfg(soccfg, body_delay=cyc(soccfg, 100 * P + 4)))


def test_loop_misaligned_but_no_phrst_inside_then_phrst_after():
    soccfg = make_soccfg(P)
    prog = QickProgramV2(soccfg)
    declare(prog, gen_phrst=1)
    prog.open_loop(3, name='i')
    prog.delay(cyc(soccfg, 10))
    prog.close_loop()
    # 3 * 10 = 30 cycles after the loop: legal at t = 26 cycles, not at t = 0
    prog.pulse(ch=0, name='p', t=cyc(soccfg, 26))
    prog.end()
    prog.compile()
    prog2 = QickProgramV2(soccfg)
    declare(prog2, gen_phrst=1)
    prog2.open_loop(3, name='i')
    prog2.delay(cyc(soccfg, 10))
    prog2.close_loop()
    prog2.pulse(ch=0, name='p', t=0)
    prog2.end()
    with pytest.raises(RuntimeError, match='30 cycles'):
        prog2.compile()


def test_default_averager_rejected_and_aligned_version_accepted():
    soccfg = make_soccfg(P)
    # the defaults (initial_delay=1 us, final_delay arbitrary) are not on the grid
    with pytest.raises(RuntimeError):
        ShotProg(soccfg, reps=10, final_delay=1.0, cfg=shot_cfg(soccfg))
    # the same program with its delays rounded to the grid
    class Aligned(ShotProg):
        def _body(self, cfg):
            super()._body(cfg)
            self.delay_auto(1.0, align=True)
    Aligned(soccfg, reps=10, initial_delay=soccfg.round_up_to_sync_tick(1.0), final_delay=None,
            cfg=shot_cfg(soccfg))


def test_phrst_once_before_loops():
    soccfg = make_soccfg(P)
    # phase reset of the readout once, before the loops; the shot timing is free
    ShotProg(soccfg, reps=10, final_delay=1.0, cfg={'ro_phrst': 1, 'ro_in_init': True})


# --- swept delays ---------------------------------------------------------------

def test_swept_delay_on_grid_accepted():
    soccfg = make_soccfg(P)
    n = 5
    start, stop = 4 * P, 4 * P + (n - 1) * 3 * P
    ShotProg(soccfg, reps=4, initial_delay=cyc(soccfg, 20 * P), final_delay=None,
             cfg=shot_cfg(soccfg, sweep=QickSweep1D('sweep', cyc(soccfg, start), cyc(soccfg, stop)), sweep_n=n,
                          body_delay=cyc(soccfg, 50 * P)))


def test_swept_delay_off_grid_step_rejected():
    soccfg = make_soccfg(P)
    n = 5
    start, stop = 4 * P, 4 * P + (n - 1) * 10
    with pytest.raises(RuntimeError) as e:
        ShotProg(soccfg, reps=4, initial_delay=cyc(soccfg, 20 * P), final_delay=None,
                 cfg=shot_cfg(soccfg, sweep=QickSweep1D('sweep', cyc(soccfg, start), cyc(soccfg, stop)), sweep_n=n,
                              body_delay=cyc(soccfg, 50 * P)))
    msg = str(e.value)
    assert "swept" in msg and "'sweep'" in msg and 'step 10 cycles' in msg


def test_swept_delay_off_grid_start_rejected():
    soccfg = make_soccfg(P)
    n = 5
    start, stop = 4 * P + 1, 4 * P + 1 + (n - 1) * 3 * P
    with pytest.raises(RuntimeError):
        ShotProg(soccfg, reps=4, initial_delay=cyc(soccfg, 20 * P), final_delay=None,
                 cfg=shot_cfg(soccfg, sweep=QickSweep1D('sweep', cyc(soccfg, start), cyc(soccfg, stop)), sweep_n=n,
                              body_delay=cyc(soccfg, 50 * P)))


def test_swept_pulse_time():
    soccfg = make_soccfg(P)
    def build(step):
        prog = QickProgramV2(soccfg)
        declare(prog, gen_phrst=1)
        prog.open_loop(4, name='i')
        prog.pulse(ch=0, name='p', t=QickSweep1D('i', cyc(soccfg, P), cyc(soccfg, P + 3 * step)))
        prog.delay(cyc(soccfg, 100 * P))
        prog.close_loop()
        prog.end()
        prog.compile()
    build(2 * P)
    with pytest.raises(RuntimeError, match='step 5 cycles'):
        build(5)


# --- branches ---------------------------------------------------------------------

def test_subroutine_on_grid_accepted():
    soccfg = make_soccfg(P)
    SubProg(soccfg, reps=5, initial_delay=cyc(soccfg, 10 * P), final_delay=None,
            cfg={'gen_phrst': 1, 'sub_delay': cyc(soccfg, 40 * P)})


def test_subroutine_off_grid_rejected():
    soccfg = make_soccfg(P)
    with pytest.raises(RuntimeError, match='jump|call|label'):
        SubProg(soccfg, reps=5, initial_delay=cyc(soccfg, 10 * P), final_delay=None,
                cfg={'gen_phrst': 1, 'sub_delay': cyc(soccfg, 40 * P + 1)})


def test_branch_then_off_grid_delay_with_phrst_before_rejected():
    # the phrst is checked on the first pass only; the backward jump repeats it after a 13-cycle delay
    soccfg = make_soccfg(P)
    prog = QickProgramV2(soccfg)
    declare(prog, gen_phrst=1)
    prog.label('top')
    prog.pulse(ch=0, name='p', t=0)
    prog.delay(cyc(soccfg, 13))
    prog.jump('top')
    with pytest.raises(RuntimeError, match='jump|label'):
        prog.compile()


if __name__ == '__main__':
    if '--golden' in sys.argv:
        progs = golden_programs(make_soccfg())
        os.makedirs(os.path.dirname(GOLDEN_PATH), exist_ok=True)
        with open(GOLDEN_PATH, 'w') as f:
            json.dump({k: dump(v) for k, v in progs.items()}, f, indent=1)
        print('wrote', GOLDEN_PATH, sorted(progs))
