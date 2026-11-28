"""QICK box detection (qick.rfbox_detect) and the one-class entry point (qick.qickbox.QickBoxSoc) over the bus-level
fake in qick.rfbox_fake. No hardware. Run with `.venv/bin/python -m pytest slab/tests/test_box_detect.py -q`.

The QickBoxSoc tests import the real qick.qick / qick.rfboard / drivers with a stand-in for pynq (restored after the
module), so the card set-up that runs on a confirmed box is the library's own code talking to the fake chips.
"""
import ast
import json
import os
import pickle
import sys
import types
import warnings

import pytest

import qick
from qick import QickConfig
from qick import rfbox as R
from qick import rfbox_detect as D
from qick import rfbox_fake as F
from qick import lab_release

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, '..', '..', 'qick_lib', 'qick')

LAYOUTS = {
    'example': {'adc': [None, None, 'rf_in', None], 'dac': ['rf_out', 'rf_out', 'dc_out', None]},
    'full': {'adc': ['dc_in', 'rf_in', 'rf_in', 'balun_in'], 'dac': ['rf_out', 'dc_out', 'balun_out', 'rf_out']},
    'one_dc_in': {'adc': ['dc_in', None, None, None], 'dac': [None, None, None, None]},
    'dac_only': {'adc': [None, None, None, None], 'dac': [None, None, None, 'rf_out']},
}


def run(layout=None, options=None, **kw):
    hw = F.FakeRfBoardHW(layout, **kw)
    soc = hw.make_holder()
    inv = D.detect(soc, options)
    return hw, soc, inv


def kinds(inv):
    out = {'adc': [None] * 4, 'dac': [None] * 4}
    for c in inv['cards']:
        out[c['side']][c['slot']] = c['kind']
    return out


def mcp_writes(hw):
    return [e for e in hw.spi_log('filter_spi') if e['out'][0] == 0x40]


# ---- module hygiene ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize('name', ['rfbox_detect.py', 'qickbox.py', 'rfbox_fake.py'])
def test_py38_syntax(name):
    ast.parse(open(os.path.join(LIB, name)).read(), feature_version=(3, 8))


def test_detect_is_stdlib_only():
    tree = ast.parse(open(os.path.join(LIB, 'rfbox_detect.py')).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split('.')[0] in sys.stdlib_module_names
        elif isinstance(node, ast.ImportFrom) and not node.level:
            assert node.module.split('.')[0] in sys.stdlib_module_names


# ---- bare eval board ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize('floating', ['low', 'high', 'echo', 'noise'])
def test_bare_eval_board_is_no_box(floating):
    hw, soc, inv = run(None, floating=floating)
    assert inv['present'] is False and inv['cards'] == []
    assert all(s['verdict'] in ('empty', 'unstable') for s in inv['slots'])
    # never mistaken for a DC In card (ID 0) or anything else, and nothing but reads went out
    assert hw.power_writes() == []
    assert mcp_writes(hw) == []
    assert hw.spi_log('attn_spi') == [] and hw.spi_log('bias_spi') == []
    assert all(e['ip'] == 'brd_sel_gpio' for e in hw.log if e['op'] == 'gpio_write')
    assert D.plain(inv) == inv


def test_floating_low_reads_like_id_0_but_is_rejected():
    """The library's own detection reads GPIO >> 4 = 0 here and builds a DC In card."""
    hw, soc, inv = run(None, floating='low')
    s = [x for x in inv['slots'] if x['side'] == 'adc'][0]
    assert s['regs']['GPIO'] >> 4 == 0
    assert s['verdict'] == 'empty' and s['floating'] == 'floating_low' and not s['confirmed']


def test_constant_line_needs_only_one_pass():
    hw, soc, inv = run(None, floating='high')
    assert len(hw.spi_log('filter_spi')) == 8 * len(D.MCP_REGS)


def test_echo_line_is_caught_by_the_dummy_byte():
    # a line that follows the master reads the dummy byte back: all 0x00 with dummy 0x00 (a constant line, empty)
    hw, soc, inv = run(None, floating='echo')
    assert all(s['verdict'] == 'empty' for s in inv['slots'])
    # and if the first pass is not constant, the second pass (dummy 0xFF) differs
    bus = D.Bus(hw.spis['filter_spi'], hw.brd_sel_gpio)
    r0 = dict((n, a + 1) for n, a in D.MCP_REGS)
    r1 = dict((n, 0xFF) for n, a in D.MCP_REGS)
    assert D.classify_mcp(r0, r1)[0] == 'unstable'
    del bus


def test_signature_rejections():
    good = {'IODIR': 0xF0, 'IPOL': 0, 'GPINTEN': 0, 'DEFVAL': 0, 'INTCON': 0, 'IOCON': 0, 'GPPU': 0, 'GPIO': 0x20,
            'OLAT': 0}
    assert D.classify_mcp(good, good)[0] == 'mcp_signature'
    for k, v in (('IODIR', 0x0F), ('IPOL', 1), ('GPINTEN', 4), ('INTCON', 2), ('IOCON', 0x80)):
        bad = dict(good, **{k: v})
        assert D.classify_mcp(bad, bad)[0] == 'not_mcp', k
    stuck = dict((k, 0x5A) for k in good)
    assert D.classify_mcp(stuck, stuck)[0] == 'stuck'


# ---- boxes -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize('name', sorted(LAYOUTS))
@pytest.mark.parametrize('box_floating', ['low', 'high'])
def test_box_cards_found(name, box_floating):
    lay = LAYOUTS[name]
    hw, soc, inv = run(lay, box_floating=box_floating)
    assert inv['present'] is True
    assert kinds(inv) == lay
    for c in inv['cards']:
        n = 4 if c['side'] == 'dac' else 2
        assert c['channels'] == list(range(n * c['slot'], n * c['slot'] + n))
        assert c['chip_checks'][0]['chip'] == 'MCP23S08' and c['chip_checks'][0]['ok']
        if c['kind'] in ('rf_in', 'rf_out'):
            f = [x for x in c['chip_checks'] if x['chip'] == 'ADMV8818']
            assert len(f) == n and all(x['ok'] and x['product_id'] == '0x8818' for x in f)
    for s in inv['slots']:
        if lay[s['side']][s['slot']] is None:
            assert s['verdict'] == 'empty'
    assert hw.power_writes() == []


def test_empty_adc_slot_with_low_line_is_not_dc_in():
    hw, soc, inv = run(LAYOUTS['dac_only'], box_floating='low')
    assert kinds(inv)['adc'] == [None] * 4


def test_probe_restores_defval_and_writes_nothing_else():
    hw, soc, inv = run(LAYOUTS['full'])
    for (b, card) in hw.cards.items():
        assert card.mcp.regs[0x03] == 0x00                     # DEFVAL back to its reset value
        assert card.mcp.regs[0x00] == 0xFF                     # IODIR never written: every pin still an input
        assert card.mcp.regs[0x0A] == 0x00
    regs = set(e['out'][1] for e in mcp_writes(hw))
    assert regs == {0x03}


def test_previous_session_state_is_accepted():
    """After an earlier library start the expanders have IODIR 0xf0 and the filters' SDO is on."""
    hw, soc, inv = run(LAYOUTS['example'], previous_init=True)
    assert kinds(inv) == LAYOUTS['example']
    f = [x for c in inv['cards'] for x in c['chip_checks'] if x['chip'] == 'ADMV8818']
    assert f and all('wrote_config_a' not in x for x in f)


def test_fresh_filters_get_config_a_then_answer():
    hw, soc, inv = run(LAYOUTS['example'])
    f = [x for c in inv['cards'] for x in c['chip_checks'] if x['chip'] == 'ADMV8818']
    assert all(x['first_read'] != '0x8818' and x['wrote_config_a'] == '0x3c' and x['ok'] for x in f)


def test_unknown_id_card_listed_but_not_configured():
    hw, soc, inv = run({'adc': [None, 'rf_in', None, None], 'dac': [None] * 4}, ids={('adc', 1): 15})
    s = [x for x in inv['slots'] if x['side'] == 'adc' and x['slot'] == 1][0]
    assert s['verdict'] == 'card' and s['confirmed'] and s['id'] == 15 and s['kind'] is None
    assert inv['present'] is True and inv['cards'] == []
    assert 'unknown ID 15' in '\n'.join(D.describe(inv))


def test_dc_in_v1_without_reads_needs_declaring():
    lay = {'adc': [None, 'dc_in', 'rf_in', None], 'dac': [None] * 4}
    hw, soc, inv = run(lay, broken=[('adc', 1)])
    assert kinds(inv)['adc'] == [None, None, 'rf_in', None]
    hw, soc, inv = run(lay, broken=[('adc', 1)], options={'cards': {'adc': {1: 'dc_in'}}})
    assert kinds(inv)['adc'] == [None, 'dc_in', 'rf_in', None]
    c = [c for c in inv['cards'] if c['slot'] == 1 and c['side'] == 'adc'][0]
    assert c['source'] == 'forced'
    with pytest.raises(ValueError):
        D.detect(soc, {'cards': {'adc': {1: 'rf_out'}}})


def test_no_rf_board_control_touches_nothing():
    inv = D.detect(F.FakeBusHolder())
    assert inv['present'] is False and inv['reason'] == 'no rf-board control'
    assert D.describe(inv) == []


# ---- gate and presence input ------------------------------------------------------------------------------------
def gate_writes(hw):
    return [e for e in hw.log if e['op'] == 'gpio_write' and e['ip'] == hw.gate_name]


def test_presence_input_absent_decides_alone():
    hw, soc, inv = run(None, gate=True, presence=True)
    assert inv['present'] is False and inv['method'] == 'presence input'
    assert hw.spi_log() == [] and not hw.gate_open()
    assert inv['presence_input']['present'] is False


def test_presence_input_trusted_even_if_cards_would_answer():
    hw, soc, inv = run(LAYOUTS['example'], gate=True, presence=False)
    assert inv['present'] is False and hw.spi_log() == [] and not hw.gate_open()


def test_presence_input_present_then_probe():
    hw, soc, inv = run(LAYOUTS['example'], gate=True, presence=True)
    assert inv['present'] is True and inv['method'] == 'presence input'
    assert kinds(inv) == LAYOUTS['example'] and hw.gate_open() and inv['gate']['open'] is True
    assert hw.power_writes() == []


def test_presence_input_on_gate_channel_2_by_name():
    hw = F.FakeRfBoardHW(LAYOUTS['example'], gate=True, presence=True, presence_on_gate_ch2=True)
    inv = D.detect(hw.make_holder(), {'names': {'presence': 'rfb_gate_gpio', 'presence_channel': 2}})
    assert inv['present'] and inv['presence_input']['ip'] == 'rfb_gate_gpio'


def test_gate_without_presence_no_box_closes_again():
    hw, soc, inv = run(None, gate=True, floating='high')
    assert inv['present'] is False and inv['method'] == 'gated probe'
    w = [e['value'] & 1 for e in gate_writes(hw)]
    assert w == [1, 0] and not hw.gate_open()
    assert all(e['gate_open'] for e in hw.spi_log())
    # idle levels set before the gate opened
    i_open = next(i for i, e in enumerate(hw.log) if e['op'] == 'gpio_write' and e['ip'] == hw.gate_name)
    assert any(e['op'] == 'gpio_write' and e['ip'] == 'brd_sel_gpio' and e['value'] == 0 for e in hw.log[:i_open])
    assert hw.power_writes() == []


def test_gate_without_presence_box_found():
    hw, soc, inv = run(LAYOUTS['full'], gate=True)
    assert inv['present'] and kinds(inv) == LAYOUTS['full'] and hw.gate_open()


def test_gate_name_from_options():
    hw = F.FakeRfBoardHW(LAYOUTS['example'], gate=True, gate_name='my_gate')
    inv = D.detect(hw.make_holder())                 # default name not found: the probe sees a closed gate
    assert inv['present'] is False and inv['gate'] is None
    hw = F.FakeRfBoardHW(LAYOUTS['example'], gate=True, gate_name='my_gate')
    inv = D.detect(hw.make_holder(), {'names': {'gate': ['my_gate']}})
    assert inv['present'] and inv['gate']['ip'] == 'my_gate'
    with pytest.raises(ValueError):
        D.merged_names({'names': {'gat': 'x'}})


def test_stock_firmware_without_gate_falls_back_to_probe():
    hw, soc, inv = run(LAYOUTS['example'])
    assert inv['method'] == 'probe (no gate in this firmware)' and inv['gate'] is None and inv['present']


# ---- hardware description, identity -----------------------------------------------------------------------------
HWH = '''<?xml version="1.0"?><EDKSYSTEM><MODULES>
%s
</MODULES></EDKSYSTEM>'''


def write_hwh(path, fullnames):
    path.write_text(HWH % '\n'.join('<MODULE FULLNAME="/%s" INSTANCE="%s"/>' % (n, n.replace('/', '_'))
                                    for n in fullnames))
    return str(path)


RFB = ['rfb_control/brd_sel_gpio', 'rfb_control/filter_spi', 'rfb_control/attn_spi', 'rfb_control/bias_spi',
       'rfb_control/bias_gpio', 'usp_rf_data_converter_0']


def test_hwh_has_rfb(tmp_path):
    assert D.hwh_has_rfb(write_hwh(tmp_path / 'a.hwh', RFB))
    assert not D.hwh_has_rfb(write_hwh(tmp_path / 'b.hwh', RFB[1:]))
    assert not D.hwh_has_rfb(str(tmp_path / 'missing.hwh'))


def test_firmware_identity_by_fw_id_and_by_hash(tmp_path, monkeypatch):
    hwh = write_hwh(tmp_path / 'img.hwh', RFB)
    (tmp_path / 'img.bit').write_bytes(b'bits')
    import hashlib
    h = hashlib.sha256(open(hwh, 'rb').read()).hexdigest()
    builds = [{'name': 'by-id', 'release': '9', 'fw_id': '0x5ab00001'}, {'name': 'by-hash', 'hwh_sha256': h}]
    monkeypatch.setattr(lab_release, 'firmware', lambda: builds)
    hw = F.FakeRfBoardHW(None, fw_id=0x5AB00001)
    ident = D.firmware_identity(hw.make_holder(), str(tmp_path / 'img.bit'))
    assert ident['fw_id'] == 0x5AB00001 and ident['build'] == 'by-id' and ident['matched_by'] == 'fw_id'
    ident = D.firmware_identity(F.FakeRfBoardHW(None).make_holder(), str(tmp_path / 'img.bit'))
    assert ident['fw_id'] is None and ident['build'] == 'by-hash' and 'img.hwh' in ident['matched_by']
    ident = D.firmware_identity(F.FakeRfBoardHW(None).make_holder(), None)
    assert ident['build'] is None and D.describe_firmware(ident) == []
    assert lab_release.identify_fw_id(0x1234) is None


# ---- description round trip through QickConfig -----------------------------------------------------------------
def base_cfg():
    with open(os.path.join(LIB, 'boardserver', 'fake_soccfg.json')) as f:
        cfg = json.load(f)
    cfg['rf'].setdefault('dac_power', '20mA')
    cfg['sw_version'] = qick.__version__
    return cfg


@pytest.mark.parametrize('layout', [None, 'example', 'full'])
def test_inventory_round_trip(layout):
    hw, soc, inv = run(LAYOUTS.get(layout))
    cfg = base_cfg()
    cfg['rfbox'] = inv
    cfg['extra_description'] = D.describe(inv)
    text = json.dumps(cfg)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        soccfg = QickConfig(json.loads(text))
    assert soccfg['rfbox'] == inv
    assert pickle.loads(pickle.dumps(soccfg['rfbox'])) == inv
    desc = str(soccfg)
    assert ('QICK box: present' in desc) == (layout is not None)
    lay = R.cfg_layout(soccfg.get_cfg())
    if layout is None:
        assert lay is None
    else:
        for side in ('adc', 'dac'):
            for slot, kind in enumerate(LAYOUTS[layout][side]):
                got = lay[side].get(slot)
                assert (got['kind'] if got else None) == kind


# ---- the soc class, with a stand-in for pynq --------------------------------------------------------------------
class _Stub(types.ModuleType):
    def __getattr__(self, k):
        if k.startswith('__'):
            raise AttributeError(k)
        cls = type(k, (object,), {'__init__': lambda self, *a, **kw: None})
        setattr(self, k, cls)
        return cls


@pytest.fixture(scope='module')
def qb():
    saved = dict(sys.modules)
    for name in ('pynq', 'pynq.overlay', 'pynq.buffer', 'pynq.lib', 'xrfclk', 'xrfdc', 'cffi', 'qick.board_utils'):
        sys.modules[name] = _Stub(name)
    try:
        import qick.qickbox as qbm
        import qick.qick as qq
        yield types.SimpleNamespace(qb=qbm, QickSoc=qq.QickSoc)
    finally:
        for k in list(sys.modules):
            if k not in saved:
                del sys.modules[k]
        sys.modules.update(saved)
        for attr in ('qick', 'qickbox', 'rfboard', 'ip', 'drivers'):
            if 'qick.' + attr not in saved and hasattr(qick, attr):
                delattr(qick, attr)


class Gen(dict):
    pass


class AvgBuf(object):
    def __init__(self, adc):
        self.readout = {'adc': adc}


def make_soc(qb, layout, options=None, gens=('00', '10', '20', '30'), ros=('20', '22', '00'), **kw):
    hw = F.FakeRfBoardHW(layout, **kw)
    soc = object.__new__(qb.qb.QickBoxSoc)
    soc._cfg = {'extra_description': [], 'rf': {'tiles': {'adc': [0, 1, 2, 3]}}, 'board': 'ZCU216'}
    soc.gens = [Gen(dac=g) for g in gens]
    soc.avg_bufs = [AvgBuf(a) for a in ros]
    soc.restarted = []
    soc.cleared = []
    soc.rf = types.SimpleNamespace(restart_adc_tile=soc.restarted.append)
    soc.clear_interrupts = lambda **k: soc.cleared.append(k)
    hw.make_holder(soc)
    soc._rfbox_options = qb.qb.rfbox_options(options)
    soc.adc_chains, soc.dac_chains, soc.biases = [], [], []
    real = D.detect

    def detect(*a, **k):
        inv = real(*a, **k)
        hw.detect_end = len(hw.log)          # everything after this is the set-up of a confirmed box
        return inv
    D.detect = detect
    try:
        soc._rfb_config()
    finally:
        D.detect = real
    return hw, soc


def test_soc_on_eval_board_is_plain(qb):
    hw, soc = make_soc(qb, None)
    assert soc['rfbox']['present'] is False and soc.rfbox is None
    assert soc.dac_cards == [None] * 4 and soc.adc_cards == [None] * 4
    assert all(g.rfb_ch is None for g in soc.gens) and all(a.rfb_ch is None for a in soc.avg_bufs)
    assert hw.power_writes() == [] and soc.restarted == [] and soc.cleared == []
    soc.prepare_round()
    soc.cleanup_round()
    assert soc.cleared == []                      # QickSoc's (no ADC interrupt check)
    for fn, a in ((soc.rfb_enable_bias, ()), (soc.rfb_disable_bias, ()), (soc.rfb_set_bias, (0, 1.0)),
                  (soc.rfb_get_bias, (0,))):
        with pytest.raises(qb.qb.NoBoxError):
            fn(*a)
    with pytest.raises(RuntimeError):
        soc.rfb_set_gen_rf(0, 10, 10)
    assert hw.power_writes() == []
    with pytest.raises(qb.qb.NoBoxError):
        make_soc(qb, None, options={'require': True})


def test_soc_skip_detection(qb):
    hw, soc = make_soc(qb, LAYOUTS['example'], options=False)
    assert soc['rfbox']['present'] is False and hw.log == []


@pytest.mark.parametrize('name', sorted(LAYOUTS))
def test_soc_in_box_sets_up_cards_after_confirmation(qb, name):
    lay = LAYOUTS[name]
    hw, soc = make_soc(qb, lay)
    assert soc['rfbox']['present'] is True
    for side in ('adc', 'dac'):
        for slot, kind in enumerate(lay[side]):
            card = getattr(soc, side + '_cards')[slot]
            assert (card is None) == (kind is None)
            if card is not None:
                assert card.NAME == D.KIND_NAMES[kind]
    # every write that could reach power or bias comes after detection has finished (box confirmed)
    assert hw.power_writes() and min(hw.power_writes()) >= hw.detect_end
    assert any(e['op'] == 'gpio_write' and e['ip'] == 'bias_gpio' for e in hw.log[hw.detect_end:])
    # the expanders of empty slots never see a write
    for e in hw.spi_log('filter_spi'):
        if e['out'][0] == 0x40 and e['board'] is not None:
            side, slot = ('dac', e['board']) if e['board'] < 4 else ('adc', e['board'] - 4)
            assert lay[side][slot] is not None
    # library set-up as RFQickSoc216V1: IODIR 0xf0 on the confirmed expanders, bias DACs configured, bias on
    for b, card in hw.cards.items():
        assert card.mcp.regs[0x00] == 0xF0
    assert set(hw.bias) == set(range(8)) and hw.bias_gpio.channel1.value == 1
    assert soc.restarted == [0, 1, 2, 3] and soc.cleared
    # gens / readouts linked: gen on DAC tile t -> dac_cards[t]; readout '20' -> ADC slot 2, '22' -> slot 3,
    # readout on tile 0 -> no port
    for g in soc.gens:
        t, blk = int(g['dac'][0]), int(g['dac'][1])
        card = soc.dac_cards[t]
        assert g.rfb_ch is (card.chains[blk] if card else None)
    assert soc.avg_bufs[2].rfb_ch is None
    for a, slot in ((soc.avg_bufs[0], 2), (soc.avg_bufs[1], 3)):
        card = soc.adc_cards[slot]
        assert a.rfb_ch is (card.chains[0] if card else None)
    # the lab driver is attached and agrees with the inventory
    assert isinstance(soc.rfbox, R.RfBox)
    lay2 = soc.rfbox.layout(read_ids=False)
    assert json.dumps(lay2)
    assert any('QICK box daughter cards detected:' in x for x in soc['extra_description'])
    soc.prepare_round()
    assert soc.cleared[-1].get('error_on_interrupt') is True


def test_soc_declared_dc_in_v1(qb):
    lay = {'adc': [None, 'dc_in', 'rf_in', None], 'dac': [None] * 4}
    hw, soc = make_soc(qb, lay, options={'cards': {'adc': {1: 'dc_in'}}}, broken=[('adc', 1)])
    assert soc.adc_cards[1].NAME == 'DC In' and soc.adc_cards[2].NAME == 'RF In'


def test_soc_gated_no_box_leaves_gate_closed(qb):
    hw, soc = make_soc(qb, None, gate=True)
    assert not hw.gate_open() and soc['rfbox']['gate']['open'] is False and hw.power_writes() == []


def test_soc_presence_absent_no_spi(qb):
    hw, soc = make_soc(qb, None, gate=True, presence=True)
    assert hw.spi_log() == [] and soc['rfbox']['present'] is False


def test_soc_gated_box(qb):
    hw, soc = make_soc(qb, LAYOUTS['example'], gate=True, presence=True)
    assert soc['rfbox']['present'] and hw.gate_open() and soc.dac_cards[0].NAME == 'RF Out'


def test_quicksoc_new_dispatches_on_the_hwh(qb, tmp_path):
    rfb = write_hwh(tmp_path / 'rfb.hwh', RFB)
    ev = write_hwh(tmp_path / 'eval.hwh', ['usp_rf_data_converter_0', 'axi_dma_avg'])
    QS, QB = qb.QickSoc, qb.qb.QickBoxSoc
    assert type(QS.__new__(QS, rfb[:-4] + '.bit')) is QB
    assert type(QS.__new__(QS, bitfile=rfb[:-4] + '.bit')) is QB
    assert type(QS.__new__(QS, rfb[:-4] + '.bit', rfbox=False)) is QS
    assert type(QS.__new__(QS, ev[:-4] + '.bit')) is QS
    assert type(QS.__new__(QS)) is QS
    assert issubclass(QB, QS)
    from qick.rfboard import RFQickSoc216V1
    assert type(RFQickSoc216V1.__new__(RFQickSoc216V1, rfb[:-4] + '.bit')) is RFQickSoc216V1


def test_rfbox_driver_reads_the_real_cards(qb):
    hw, soc = make_soc(qb, LAYOUTS['example'])
    lay = soc.rfbox.layout(read_ids=True)
    st = soc.rfbox.get(flags=False)
    json.dumps(st)
    a4 = st['adc_ports']['4']
    assert a4['kind'] == 'rf_in' and a4['amp']['source'] == 'read' and a4['amp']['value'] is False
    assert lay is not None
