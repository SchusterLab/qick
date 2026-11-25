"""Off-board tests for the dispatcher-queue overflow flags (tProc v2, lab firmware).

Firmware with the qick_processor parameter FIFO_GUARD has sticky lost-push flags in
AXI register 9 (bits 8, 9, 10: trigger, data, wave queue). The library reads them
after every round and raises; on firmware without the parameter nothing changes.

The tProc driver needs pynq, so these tests give it a stand-in register file.

Run:  .venv/bin/python -m pytest slab/tests
"""
import json
import os
import sys
import types

import numpy as np
import pytest
from tqdm.auto import tqdm

import qick  # before the pynq stand-in, so the package imports as on a PC
from qick import QickConfig
from qick.asm_v2 import AveragerProgramV2
from qick.helpers import tproc_queue_overflows, tproc_queue_overflow_message

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, '..', '..', 'firmware', 'testbench', 'qick_testbench', 'soccfg.json')


# --- register decoding ------------------------------------------------------

def test_decode():
    assert tproc_queue_overflows(0x9) == []
    assert tproc_queue_overflows(0x109) == ['trigger']
    assert tproc_queue_overflows(0x209) == ['data']
    assert tproc_queue_overflows(0x409) == ['wave']
    assert tproc_queue_overflows(0x709) == ['trigger', 'data', 'wave']
    msg = tproc_queue_overflow_message(['trigger'])
    assert 'trigger' in msg and 'queue' in msg


# --- the driver, with a stand-in for pynq -----------------------------------

class _MMIO:
    def __init__(self):
        self.array = np.zeros(16, dtype=np.uint32)


@pytest.fixture
def tproc_class(monkeypatch):
    overlay = types.ModuleType('pynq.overlay')

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
    yield Axis_QICK_Proc
    for name in ('qick.ip', 'qick.drivers.tproc'):
        sys.modules.pop(name, None)


def _description(extra=None):
    params = {'PMEM_AW': 12, 'DMEM_AW': 14, 'WMEM_AW': 10, 'REG_AW': 4,
              'IN_PORT_QTY': 1, 'OUT_TRIG_QTY': 20, 'OUT_DPORT_QTY': 1, 'OUT_DPORT_DW': 8,
              'OUT_WPORT_QTY': 16, 'LFSR': 1, 'DIVIDER': 1, 'ARITH': 1, 'TIME_READ': 1,
              'QCOM': 0, 'CUSTOM_PERIPH': 1, 'IO_CTRL': 1, 'EXT_FLAG': 0, 'QNET': 0,
              'FIFO_DEPTH': 9, 'CALL_DEPTH': 255, 'DEBUG': 0}
    params.update(extra or {})
    return {'type': 'QICK:QICK:qick_processor:2.0', 'fullpath': 'qick_processor_0',
            'parameters': {k: str(v) for k, v in params.items()}}


def test_stock_firmware_has_no_flags(tproc_class):
    tp = tproc_class(_description())
    assert tp['queue_flags'] is False
    tp.mmio.array[9] = 0x709          # whatever register 9 holds, it is not read
    assert tp.get_queue_overflows() == []
    tp.check_queue_overflow()


def test_lab_firmware_flags(tproc_class):
    tp = tproc_class(_description({'FIFO_GUARD': 8}))
    assert tp['queue_flags'] is True and tp['fifo_guard'] == 8
    tp.mmio.array[9] = 0x9
    tp.check_queue_overflow()
    tp.mmio.array[9] = 0x109
    with pytest.raises(RuntimeError, match='trigger'):
        tp.check_queue_overflow()
    tp.mmio.array[9] = 0x409
    with pytest.raises(RuntimeError, match='wave'):
        tp.check_queue_overflow()


# --- acquire/run_rounds call the check only when the config says so --------

class Prog(AveragerProgramV2):
    def _initialize(self, cfg):
        self.declare_gen(ch=0, nqz=1)
        self.declare_readout(ch=0, length=0.5)
        self.add_readoutconfig(ch=0, name='ro', freq=100, gen_ch=0)
        self.add_pulse(ch=0, name='p', style='const', freq=100, phase=0, gain=0.5, length=0.1)

    def _body(self, cfg):
        self.send_readoutconfig(ch=0, name='ro', t=0)
        self.pulse(ch=0, name='p', t=0)
        self.trigger(ros=[0], t=0.2)


class FakeSoc:
    """Just what finish_round() needs for a run_rounds() round."""
    def __init__(self, reps, overflow):
        self.reps, self.overflow, self.checked = reps, overflow, 0

    def start_tproc(self): pass
    def start_src(self, src): pass
    def cleanup_round(self): pass
    def get_tproc_counter(self, addr): return self.reps

    def check_tproc_queues(self):
        self.checked += 1
        if self.overflow:
            raise RuntimeError(tproc_queue_overflow_message(['trigger']))


def _round(queue_flags, overflow):
    with open(CFG_PATH) as f:
        cfg = json.load(f)
    if queue_flags is not None:
        cfg['tprocs'][0]['queue_flags'] = queue_flags
    prog = Prog(QickConfig(cfg), reps=5, final_delay=1.0)
    soc = FakeSoc(5, overflow)
    prog.acquire_params = {'type': 'run_rounds', 'soc': soc, 'start_src': 'internal',
                           'rounds_remaining': 1, 'hidereps': True}
    prog.rounds_pbar = tqdm(total=1, disable=True)
    return prog, soc


@pytest.mark.parametrize('queue_flags', [None, False])
def test_round_without_flags_does_not_check(queue_flags):
    prog, soc = _round(queue_flags, overflow=True)
    assert prog.finish_round() is False
    assert soc.checked == 0


def test_round_with_flags_raises():
    prog, soc = _round(True, overflow=True)
    with pytest.raises(RuntimeError, match='trigger port queue'):
        prog.finish_round()
    prog, soc = _round(True, overflow=False)
    assert prog.finish_round() is False
    assert soc.checked == 1


def test_lab_revisions_compile():
    with open(CFG_PATH) as f:
        cfg = json.load(f)
    for rev in (28, 29):
        cfg['tprocs'][0]['revision'] = rev
        Prog(QickConfig(cfg), reps=2, final_delay=1.0).compile()
