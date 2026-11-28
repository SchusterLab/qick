"""qick.rfbox over qick.rfbox_fake: ordering, validation, limits, failure midway, readback vs written, layouts.

No hardware: the fake follows the library's object graph and the measured behaviour listed in rfbox_fake's
docstring. Run with `.venv/bin/python -m pytest slab/tests/test_rfbox.py -q`.
"""
import ast
import json
import os
import subprocess
import sys
import threading

import pytest

from qick import rfbox as R
from qick import rfbox_fake as F

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, '..', '..', 'qick_lib', 'qick')


def make(layout=None, library='0.2.450', **kw):
    box = F.FakeQickBox(layout=layout, library=library, **kw)
    soc = box.make_soc()
    rf = R.attach(soc, sleep=lambda s: None)
    return box, soc, rf


@pytest.fixture
def board():
    return make()


def ops(rec):
    return [(e['phase'], e['op'], e['target']) for e in rec['order']]


# ---- deployment ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize('name', ['rfbox.py', 'rfbox_fake.py'])
def test_stdlib_only_and_py38(name):
    src = open(os.path.join(LIB, name)).read()
    tree = ast.parse(src, feature_version=(3, 8))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name.split('.')[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                assert node.module in ('rfbox', None) or name == 'rfbox_fake.py', node.module
                continue
            mods = [node.module.split('.')[0]]
        else:
            continue
        for m in mods:
            assert m in sys.stdlib_module_names or m in ('rfbox', 'xrfdc'), (name, m)
    assert 'boardserver' not in src.replace('board server', '')


def test_standalone_copy_imports_without_pynq(tmp_path):
    for n in ('rfbox.py', 'rfbox_fake.py'):
        (tmp_path / n).write_text(open(os.path.join(LIB, n)).read())
    code = ('import sys, rfbox, rfbox_fake; b = rfbox_fake.FakeQickBox(); r = rfbox.attach(b.make_soc(), '
            'sleep=lambda s: None); r.get(); assert "pynq" not in sys.modules and "qick" not in sys.modules')
    (tmp_path / 'run.py').write_text(code)
    subprocess.check_call([sys.executable, '-s', '-E', str(tmp_path / 'run.py')], cwd=str(tmp_path))


# ---- state and sources ------------------------------------------------------------------------------------------
def test_get_is_plain_and_marks_sources(board):
    box, soc, rf = board
    st = rf.get()
    json.dumps(st)
    a4 = st['adc_ports']['4']
    assert a4['kind'] == 'rf_in' and a4['block'] == '20'
    assert a4['amp']['source'] == 'read'
    assert a4['att']['source'] == 'assumed'          # card channel 0: constructor wrote it with the card selected
    assert st['adc_ports']['5']['att']['source'] == 'unknown'   # channel 1: written while deselected
    assert a4['filter']['source'] == 'read'
    d0 = st['dac_ports']['0']
    assert d0['supply']['source'] == 'read' and d0['supply']['shared_with'] == [2]
    assert st['dac_ports']['1']['supply']['shared_with'] == [3]
    assert st['dac_ports']['8']['powered']['source'] == 'read'
    assert st['adc_att']['20'] == {'value': 0.0, 'source': 'read'}       # measured: DSA 0 dB after a load
    assert st['bias']['0']['source'] == 'read' and st['bias']['0']['value'] == 0.0


def test_get_does_not_clear_flags(board):
    box, soc, rf = board
    box.inject['20'] = R.OVR_RANGE
    box.update('test')
    box.inject.clear()
    assert rf.get()['flags']['20']['over_range']
    assert rf.get()['flags']['20']['over_range']
    rf.clear_flags()
    assert not rf.get()['flags']['20']['over_range']


def test_written_after_set_and_quantisation(board):
    box, soc, rf = board
    rec = rf.set([{'ro': 0, 'adc_att': 27}, {'ro': 0, 'att': 10.25, 'amp': True},
                  {'gen': 0, 'att': [5, 6.5], 'filter': {'ftype': 'lowpass', 'fc': 8}}])
    st = rf.get()
    assert st['adc_ports']['4']['att']['source'] == 'written' and st['adc_ports']['4']['att']['value'] == 10.25
    assert box.att('adc', 4) == 10.25
    assert st['dac_ports']['0']['att']['value'] == [5.0, 6.5] and box.att('dac', 0) == [5.0, 6.5]
    f = [e for e in rec['order'] if e['op'] == 'filter'][0]
    assert f['requested'] == {'ftype': 'lowpass', 'fc': 8.0} and f['applied']['lpf_ghz'] != 8.0
    assert st['dac_ports']['0']['filter']['value']['ftype'] == 'lowpass'


# ---- validation -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize('items', [
    [{'ro': 0, 'att': 10.1, 'amp': True}],              # not a 0.25 dB step
    [{'ro': 0, 'att': 40, 'amp': True}],                # above 31.75
    [{'ro': 0, 'adc_att': 28}],                         # DSA above 27
    [{'gen': 0, 'att': [1]}],                           # two attenuators
    [{'gen': 0, 'filter': {'ftype': 'bandpass', 'fc': 6}}],      # bandpass needs bw
    [{'gen': 0, 'filter': {'ftype': 'lowpass', 'fc': 30}}],      # out of range
    [{'bias': 9, 'volts': 0}],
    [{'bias': 0, 'volts': 11}],
    [{'ro': 0, 'bogus': 1}],
    [{'ro': 0, 'adc_att': 10}, {'ro': 0, 'adc_att': 12}],       # set twice
    [{'ro': 7, 'adc_att': 10}],                         # no such readout
])
def test_validation_touches_nothing(board, items):
    box, soc, rf = board
    n = len(box.calls)
    with pytest.raises(R.RfboxError):
        rf.set(items)
    assert len(box.calls) == n


def test_valid_first_item_not_applied_when_a_later_one_is_bad(board):
    box, soc, rf = board
    n = len(box.calls)
    with pytest.raises(R.RfboxError) as e:
        rf.set([{'ro': 0, 'adc_att': 20}, {'gen': 1, 'att': [3, 3.1]}])
    assert 'att' in str(e.value) and len(box.calls) == n and box.dsa['20'] == 0.0


def test_amp_alone_needs_a_written_attenuator(board):
    box, soc, rf = board
    rf.set([{'ro': 0, 'adc_att': 27}])
    box.calls.clear()
    # readout 0 sits on card channel 0: the constructor's 31.75 dB is only assumed
    with pytest.raises(R.RfboxError) as e:
        rf.set([{'ro': 0, 'amp': True}])
    assert e.value.code == 'unknown_state' and not box.calls
    rf.set([{'ro': 0, 'att': 20}])                      # att alone (amplifier stays off) is allowed
    assert box.amp[4] is False
    rf.set([{'ro': 0, 'amp': True}])
    assert box.amp[4] is True


def test_wrong_card_names_slot_and_card():
    box, soc, rf = make({'adc': [None, None, 'dc_in', None], 'dac': ['dc_out', 'rf_out', None, None]})
    with pytest.raises(R.RfboxError) as e:
        rf.set([{'ro': 0, 'att': 10, 'amp': True}])
    m = str(e.value)
    assert 'slot 2' in m and 'DC In' in m
    with pytest.raises(R.RfboxError) as e:
        rf.set([{'gen': 0, 'att': [10, 10]}])
    assert 'slot 0' in str(e.value) and 'DC Out' in str(e.value)


# ---- ordering ---------------------------------------------------------------------------------------------------
def test_safe_order(board):
    box, soc, rf = board
    rf.set([{'ro': 0, 'adc_att': 10}])
    rec = rf.set([{'ro': 0, 'amp': True, 'att': 10},
                  {'gen': 0, 'att': [6, 8]},
                  {'ro': 0, 'filter': {'ftype': 'bandpass', 'fc': 6.0, 'bw': 1.0}},
                  {'gen': 0, 'filter': {'ftype': 'lowpass', 'fc': 8.0}},
                  {'ro': 0, 'adc_att': 20},
                  {'bias': 3, 'volts': 0.5}], settle_s=0)
    o = ops(rec)
    assert o[0] == ('reduce', 'adc_att', '20')                     # attenuation increases first
    assert [x[1] for x in o[1:3]] == ['filter', 'filter'] and o[1][2] == ['adc', 4]
    assert o.index(('increase', 'dac_att', 0)) < o.index(('increase', 'adc_rf_on', 4))
    assert o[-1][0] == 'bias'
    assert rec['ok'] and not box.events


def test_attenuation_decrease_last(board):
    box, soc, rf = board
    rf.set([{'ro': 0, 'adc_att': 27}, {'ro': 0, 'amp': True, 'att': 20}])
    rec = rf.set([{'ro': 0, 'adc_att': 15}, {'ro': 0, 'att': 25}])
    o = ops(rec)
    assert o.index(('reduce', 'ro_att', 4)) < o.index(('increase', 'adc_att', '20'))
    assert o[-1] == ('increase', 'adc_att', '20')


def test_shared_rf_out_rail_reported(board):
    box, soc, rf = board
    rec = rf.set([{'gen': 0, 'att': [10, 10]}])
    st = [e for e in rec['order'] if e['op'] == 'dac_att'][0]
    assert st['affects'][0]['dac_port'] == 2 and box.supply(2) and box.supply(0)


# ---- limits -----------------------------------------------------------------------------------------------------
def test_limits_table(board):
    box, soc, rf = board
    with pytest.raises(R.RfboxError):
        rf.set_limits({'ro_att_min': {'0': 'x'}})
    with pytest.raises(R.RfboxError):
        rf.set_limits({'bias': {'0': [1, -1]}})
    assert rf.set_limits({'bias': {0: [-1, 1]}, 'adc_att_min': {'20': 10}}) == {'bias': {'0': [-1.0, 1.0]},
                                                                                 'adc_att_min': {'20': 10.0}}
    with pytest.raises(R.RfboxError) as e:
        rf.set([{'ro': 0, 'adc_att': 5}])
    assert e.value.code == 'rf_limit'
    with pytest.raises(R.RfboxError):
        rf.set([{'bias': 1, 'volts': 0.1}])          # not listed: only 0 V
    with pytest.raises(R.RfboxError) as e:
        rf.set([{'bias': 0, 'volts': 2}])
    assert e.value.code == 'rf_limit'
    rf.set([{'bias': 0, 'volts': 0.5}], settle_s=0)
    rf.set_limits(None)
    rf.set([{'bias': 0, 'volts': 2}], settle_s=0)


# ---- failures ---------------------------------------------------------------------------------------------------
def test_failure_midway_returns_reread_state(board):
    box, soc, rf = board
    box.fail('attn', side='dac', port=0)
    with pytest.raises(R.Refused) as e:
        rf.set([{'ro': 0, 'adc_att': 27}, {'gen': 0, 'att': [3, 3]}, {'bias': 1, 'volts': 0.2}])
    rec = e.value.record
    assert e.value.code == 'step_failed'
    assert rec['state']['adc_att']['20']['value'] == 27.0          # the step before it was done
    assert rec['state']['bias']['1']['value'] == 0.0               # the steps after it were not
    assert rec['state']['dac_ports']['0']['att']['source'] in ('unknown', 'error', 'assumed')


def test_readback_disagreement_is_refused(board):
    box, soc, rf = board
    box.stuck.add(('adc', 2))
    rf.set([{'ro': 0, 'adc_att': 27}, {'ro': 0, 'att': 20}])
    with pytest.raises(R.Refused) as e:
        rf.set([{'ro': 0, 'amp': True}])
    assert e.value.code == 'readback' and 'state' in e.value.record


def test_over_range_at_idle(board):
    box, soc, rf = board
    with pytest.raises(R.Refused) as e:
        rf.set([{'ro': 0, 'adc_att': 5}, {'ro': 0, 'amp': True, 'att': 10}])
    assert e.value.code == 'adc_flags'
    assert e.value.record['flags_idle']['20']['over_range']


def test_amp_survives_load_and_safe_state():
    box, soc, rf = make()
    rf.set([{'ro': 0, 'adc_att': 27}, {'ro': 0, 'amp': True, 'att': 10}])
    soc2 = box.make_soc()                                # new image: the amplifier stays on (measured)
    rf2 = R.attach(soc2, sleep=lambda s: None)
    assert rf2.identity['id'] != rf.identity['id']
    st = rf2.get()
    assert st['adc_ports']['4']['amp']['value'] is True and st['adc_att']['20']['value'] == 0.0
    assert st['flags']['20']['over_range']
    rec = rf2.apply_safe()
    assert rec['ok'] and box.amp[4] is False and box.dsa['20'] == 27.0
    assert box.att('dac', 0) == [31.75, 31.75] and box.att('dac', 1) == [31.75, 31.75]
    assert box.bias_volts() == [0.0] * 8
    o = ops(rec)
    assert o[0][1] == 'adc_att'                          # DSA before anything else


def test_before_load_turns_amps_off(board):
    box, soc, rf = board
    rf.set([{'ro': 0, 'adc_att': 27}, {'ro': 0, 'amp': True, 'att': 10}])
    rec = rf.before_load()
    assert rec['ok'] and box.amp[4] is False
    rf.set([{'ro': 0, 'amp': True, 'att': 10}])
    box.stuck.add(('adc', 2))
    with pytest.raises(R.Refused) as e:
        rf.before_load()
    assert e.value.code == 'rf_safe_failed'


# ---- layout -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize('library', ['0.2.360', '0.2.450'])
def test_layout_mixed_population(library):
    lay = {'adc': ['dc_in', 'rf_in', 'balun_in', None], 'dac': ['rf_out', 'dc_out', 'balun_out', None]}
    box, soc, rf = make(lay, library=library)
    L = rf.layout()
    json.dumps(L)
    kinds = [e['kind'] for e in L['adc']], [e['kind'] for e in L['dac']]
    balun = 'balun_in' if library == '0.2.450' else None
    assert kinds[0] == ['dc_in', 'rf_in', balun, None]
    assert kinds[1] == ['rf_out', 'dc_out', 'balun_out' if balun else None, None]
    assert L['adc'][1]['ports'] == [2, 3] and L['dac'][1]['ports'] == [4, 5, 6, 7]
    st = rf.get()
    assert st['adc_ports']['0']['gain']['source'] == 'read'
    rf.set([{'ro': 0, 'adc_att': 27}, {'ro': 0, 'gain': 10, 'amp': True}, {'ro': 1, 'adc_att': 27},
            {'ro': 1, 'amp': True, 'att': 20}], settle_s=0)
    assert rf.get()['adc_ports']['0']['gain']['value'] == 10


def test_absent_cards_and_no_cards():
    box, soc, rf = make({'adc': [None] * 4, 'dac': [None] * 4})
    st = rf.get()
    assert st['adc_ports'] == {} and st['dac_ports'] == {}
    assert all(e['kind'] is None for e in st['layout']['adc'])
    with pytest.raises(R.RfboxError):
        rf.set([{'ro': 0, 'amp': True, 'att': 10}])


def test_library_360_has_no_chains_and_still_works():
    box, soc, rf = make(library='0.2.360')
    assert not hasattr(soc, 'adc_chains')
    rec = rf.set([{'ro': 0, 'adc_att': 27}, {'ro': 0, 'amp': True, 'att': 10},
                  {'gen': 1, 'att': [4, 4], 'filter': {'ftype': 'bypass'}}])
    assert rec['ok'] and box.amp[4] and box.att('dac', 4) == [4.0, 4.0]


# ---- description (qick_rf replacement) -------------------------------------------------------------------------
DESC = {'gens': {0: {'att': [10, 12.5], 'filter': {'ftype': 'lowpass', 'fc': 8.0}}},
        'ros': {'0': {'att': 6, 'filter': {'ftype': 'bandpass', 'fc': 6.0, 'bw': 1.0}}},
        'bias': {3: {'volts': 0.25, 'limits': [-1, 1]}},
        'cards': {'adc': {2: 'rf_in'}, 'dac': {0: 'rf_out', 1: 'rf_out', 2: 'dc_out'}}}


def test_normalize_and_hash_match_qick_rf():
    spec = R.normalize(DESC)
    assert spec['ros'][0]['adc_att'] == 27                      # qick_rf default
    assert R.spec_hash(spec) == 'e46435e582e4'                  # computed with qick_rf.py for the same input
    for bad, where in [({'bias': {0: {'volts': 0.5}}}, 'rf.bias.0'),
                       ({'ros': {0: {'att': 6, 'filter': {'ftype': 'x'}}}}, 'rf.ros.0.filter'),
                       ({'gens': {0: {'att': [1, 2, 3]}}}, 'rf.gens.0')]:
        with pytest.raises(R.RfboxError) as e:
            R.normalize(bad)
        assert where in str(e.value)


def test_apply_and_check(board):
    box, soc, rf = board
    rec = rf.apply(DESC)
    assert rec['ok'] and rec['hash'] == R.spec_hash(R.normalize(DESC))
    o = [e['op'] for e in rec['order']]
    assert o[:3] == ['filter', 'dac_att', 'dac_supply']
    assert o.index('adc_att') < o.index('adc_rf_on') and o[-1] == 'bias'
    assert rf.check(DESC, rec) == [] and rf.check_applied(DESC, rec) is None
    box.dsa['20'] = 3.0
    assert any('20' in p for p in rf.check(DESC, rec))
    json.dumps(rec)


def test_apply_refuses_wrong_layout_and_touches_nothing():
    box, soc, rf = make({'adc': [None, None, 'dc_in', None], 'dac': ['rf_out', None, None, None]})
    n = len(box.calls)
    with pytest.raises(R.Refused) as e:
        rf.apply(DESC)
    assert e.value.code == 'layout' and len(box.calls) == n


def test_apply_over_voltage_switches_amp_off(board):
    box, soc, rf = board
    with pytest.raises(R.Refused) as e:
        rf.apply({'ros': {0: {'att': 0, 'adc_att': 0, 'filter': {'ftype': 'bypass'}}}})
    assert e.value.code == 'adc_flags' and 'OVER-VOLTAGE' in str(e.value)
    assert box.amp[4] is False


def test_apply_over_range_leaves_amp_on(board):
    box, soc, rf = board
    with pytest.raises(R.Refused) as e:
        rf.apply({'ros': {0: {'att': 10, 'adc_att': 5, 'filter': {'ftype': 'bypass'}}}})
    assert 'over range' in str(e.value) and box.amp[4] is True


def test_identity_changes_on_load(board):
    box, soc, rf = board
    rec = rf.apply(DESC)
    rf2 = R.attach(box.make_soc(), sleep=lambda s: None)
    assert any('identity changed' in p for p in rf2.check(DESC, rec))


# ---- helpers and the Pyro mixin --------------------------------------------------------------------------------
def test_module_helpers(board):
    box, soc, rf = board
    assert R.adc_dsa(soc, '20', 15) == 15.0 and box.dsa['20'] == 15.0
    with pytest.raises(R.RfboxError):
        R.adc_dsa(soc, '99', 1)
    box.inject['20'] = R.OVR_RANGE
    box.update('x')
    box.inject.clear()
    assert R.adc_flags(soc, 2, 0) & R.OVR_RANGE
    assert R.adc_flags(soc, 2, 0) == 0
    rf.set([{'ro': 0, 'adc_att': 27}, {'ro': 0, 'amp': True, 'att': 10}])
    assert R.ro_off(soc, 0) is True and box.amp[4] is False and box.att('adc', 4) == 31.75


def test_qick_rf_names(board):
    box, soc, rf = board
    regs = R.filter_registers(soc, 'ro', 0)
    assert (regs['PRODUCT_ID_H'], regs['PRODUCT_ID_L']) == (0x88, 0x18) and regs['WR0_SW'] == 0xC0
    assert R.filter_registers(soc, 'gen', 1)['WR0_FILTER'] == 0
    with pytest.raises(R.RfboxError):
        R.filter_registers(soc, 'gen', 2)              # generator 2 is on the DC Out card
    assert R.has_helpers(R.Local(soc)) and not R.has_helpers(soc)
    assert R.layout(soc.get_cfg())['adc'][2]['kind'] == 'rf_in'      # the constructor's description lines
    assert R.flags(R.OVR_VOLTAGE)['over_voltage']
    for n in ('normalize', 'plain', 'spec_hash', 'layout_text', 'channels', 'problems', 'adc_dsa', 'adc_flags',
              'ro_off', 'Local', 'Refused', 'FTYPES', 'F_MIN', 'F_MAX', 'KINDS', 'NAMES', 'OVR_VOLTAGE',
              'OVR_RANGE', 'SUBADC_RANGE', 'IDLE_WAIT_S', 'ATT_MAX', 'ATT_STEP', 'ADC_ATT_MAX',
              'ADC_ATT_DEFAULT', 'BIAS_MAX', 'FILTER_REGS'):
        assert hasattr(R, n), n


def test_mixin_replies_are_plain_data():
    box = F.FakeQickBox()
    soc = box.make_soc()
    R.attach(soc, sleep=lambda s: None)
    loc = R.Local(soc)
    st = loc.rfbox_get()
    assert st['adc_ports']['4']['kind'] == 'rf_in'
    rep = loc.rfbox_set([{'ro': 0, 'adc_att': 5}, {'ro': 0, 'amp': True, 'att': 10}])
    assert rep['ok'] is False and rep['code'] == 'adc_flags'
    with pytest.raises(R.Refused):
        R.check_reply(rep)
    with pytest.raises(ValueError) as e:
        loc.rfbox_set([{'ro': 0, 'adc_att': 99}])
    assert type(e.value) is ValueError and 'bad_request' in str(e.value)
    assert loc.labdev_adc_dsa('20') == 5.0
    cls = R.soc_class(F.FakeRfSoc)
    assert cls.__name__ == 'FakeRfSoc' and hasattr(cls, 'rfbox_apply')


def test_one_lock_serialises_card_access(board):
    box, soc, rf = board
    errs = []

    def work(g):
        try:
            for _ in range(20):
                rf.set([{'gen': g, 'att': [10, 10]}], settle_s=0)
                rf.get()
        except Exception as e:      # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=work, args=(g,)) for g in (0, 1)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs and box.select_conflicts == 0


# ---- round 2 -------------------------------------------------------------------------------------------------
def test_identity_carries_version_and_file_hash(board):
    import hashlib
    box, soc, rf = board
    sha = hashlib.sha256(open(os.path.join(LIB, 'rfbox.py'), 'rb').read()).hexdigest()
    assert R.RFBOX_VERSION == '0.2' and R.RFBOX_SHA256 == sha
    for d in (rf.identity, rf.get()['identity'], rf.get()):
        assert d['rfbox_version'] == '0.2' and d['rfbox_sha256'] == sha
    assert rf.identity['rfbox_api'] == R.API_VERSION
    v = R.Local(soc).rfbox_version()
    assert v['rfbox_version'] == '0.2' and v['rfbox_sha256'] == sha


def test_helpers_raise_instead_of_returning_failure(board):
    box, soc, rf = board
    loc = R.Local(soc)
    rf.set([{'ro': 0, 'adc_att': 27}, {'ro': 0, 'amp': True, 'att': 10}])
    box.stuck.add(('adc', 2))                      # the amplifier stays on: ro_off cannot confirm it off
    with pytest.raises(RuntimeError) as e:
        loc.labdev_ro_off(0)
    assert type(e.value) is RuntimeError and 'readback' in str(e.value)
    with pytest.raises(ValueError):
        loc.labdev_ro_off(9)
    with pytest.raises(ValueError):
        loc.labdev_adc_dsa('99', 3)
    with pytest.raises(ValueError):
        loc.rfbox_clear_flags(0, ['99'])
    # the record-returning calls keep returning refusals as data
    box.stuck.clear()
    rep = loc.rfbox_set([{'ro': 0, 'adc_att': 5}])
    assert rep['ok'] is False and 'refused' in rep


def test_adc_dsa_rounds_and_reports(board):
    box, soc, rf = board
    assert R.adc_dsa(soc, '20', 10.4) == 10.0
    r = R.adc_dsa(soc, '20', 12.6, record=True)
    assert (r['requested'], r['applied'], r['read']) == (12.6, 13.0, 13.0) and r['note']
    assert R.Local(soc).labdev_adc_dsa('20', 26.9, True)['applied'] == 27.0
    for bad in (-0.1, 27.4, 'x'):
        with pytest.raises(R.RfboxError):
            R.adc_dsa(soc, '20', bad)


def test_clear_flags_some_blocks(board):
    box, soc, rf = board
    for b in ('20', '21'):
        box.inject[b] = R.OVR_RANGE
    box.update('t')
    box.inject.clear()
    rec = rf.clear_flags(blocks=['21'])
    assert rec['cleared'] == ['21'] and rec['blocks'] == ['21']
    st = rf.get()['flags']
    assert st['20']['over_range'] and not st['21']['over_range']
    assert rf.clear_flags()['cleared'] == ['20', '21']
    assert R.Local(soc).rfbox_clear_flags(0, ['20'])['cleared'] == ['20']


@pytest.mark.parametrize('mode', ['zero', 'ones', 'raise'])
def test_filter_no_answer_strict_refuses(board, mode):
    box, soc, rf = board
    box.filter_mode[('adc', 4)] = mode
    with pytest.raises(R.Refused) as e:
        rf.set([{'ro': 0, 'filter': {'ftype': 'lowpass', 'fc': 4}}])
    assert e.value.code == 'readback' and 'filter_verify' in str(e.value)
    with pytest.raises(R.Refused) as e:
        rf.apply({'ros': {0: {'att': 10, 'filter': {'ftype': 'lowpass', 'fc': 4}}}})
    assert e.value.code == 'readback' and box.amp[4] is False     # stopped before the amplifier


@pytest.mark.parametrize('mode', ['zero', 'ones', 'raise'])
def test_filter_no_answer_opt_out_proceeds(board, mode):
    box, soc, rf = board
    box.filter_mode[('adc', 4)] = mode
    rec = rf.set([{'ro': 0, 'filter': {'ftype': 'lowpass', 'fc': 4}, 'filter_verify': False}])
    assert rec['ok'] and len(rec['warnings']) == 1 and 'not verified' in rec['warnings'][0]
    step = rec['order'][0]
    assert step['verified'] is False and step['filter_verify'] is False
    if mode == 'raise':
        assert 'readback_error' in step
    else:
        assert step['readback']['WR0_SW'] in (0, 0xFF)
    assert box.filters[('adc', 4)] == {'WR0_SW': step['written']['WR0_SW'],
                                       'WR0_FILTER': step['written']['WR0_FILTER']}
    f = rf.get()['adc_ports']['4']['filter']                    # never raises: written, not verified
    assert f['source'] == 'written' and f['verified'] is False and f['value']['ftype'] == 'lowpass'
    assert f['readback'] == ('failed' if mode == 'raise' else 'differs')
    desc = {'ros': {0: {'att': 10, 'filter': {'ftype': 'lowpass', 'fc': 4}, 'filter_verify': False}}}
    rec = rf.apply(desc)
    assert rec['ok'] and rec['warnings'] and rec['ros']['0']['filter_verified'] is False and box.amp[4]
    assert rf.check(desc, rec) == []
    assert R.normalize(desc)['ros'][0]['filter_verify'] is False
    with pytest.raises(R.RfboxError):
        rf.set([{'ro': 0, 'filter_verify': False}])                # only next to a filter


def test_filter_verify_true_does_not_change_the_hash():
    d = {'ros': {0: {'att': 10, 'filter': {'ftype': 'bypass'}}}}
    d2 = {'ros': {0: {'att': 10, 'filter': {'ftype': 'bypass'}, 'filter_verify': True}}}
    assert R.spec_hash(R.normalize(d)) == R.spec_hash(R.normalize(d2))


def test_filter_record_states_band_state_and_corners(board):
    box, soc, rf = board
    rec = rf.set([{'gen': 0, 'filter': {'ftype': 'bandpass', 'fc': 6, 'bw': 1}}])
    w = rec['order'][0]['written']
    for k in ('WR0_SW', 'WR0_FILTER', 'hpf_band', 'hpf_state', 'lpf_band', 'lpf_state', 'hpf_ghz', 'lpf_ghz'):
        assert w[k] is not None, k
    assert w['hpf_band'] >= 1 and w['lpf_band'] >= 1 and w['hpf_ghz'] < 6 < w['lpf_ghz']


def test_filter_probe(board):
    box, soc, rf = board
    p = rf.filter_probe('adc', 4, deselected=True)
    assert p['answers'] and p['product_id'] == 0x8818 and p['card_select']['board_id'] == 6
    assert [e['reg'] for e in p['reads']] == list(R.FILTER_REGS)
    assert all(e['value'] == 0xFF for e in p['reads_deselected'])
    box.filter_mode[('adc', 4)] = 'ones'
    p = rf.filter_probe('adc', 4)
    assert not p['answers'] and 'no answer' in p['verdict']
    box.filter_mode[('adc', 4)] = 'raise'
    p = rf.filter_probe('adc', 4)
    assert all('error' in e for e in p['reads']) and p['verdict'] == 'no register could be read'
    with pytest.raises(R.RfboxError):
        rf.filter_probe('dac', 8)                                   # DC Out: no filter
    json.dumps(R.Local(soc).rfbox_filter_probe('dac', 0))


def test_layout_refusal_message_has_board_layout():
    box, soc, rf = make({'adc': [None, None, 'dc_in', None], 'dac': ['rf_out', None, None, None]})
    with pytest.raises(R.Refused) as e:
        rf.apply(DESC)
    assert 'The board has:' in str(e.value) and 'ADC slot 2: DC In' in str(e.value)


def test_fake_does_not_duplicate_card_lines():
    box = F.FakeQickBox()
    soc = box.make_soc()
    cfg = soc.get_cfg()
    n = sum('slot' in str(x) for x in cfg['extra_description'])
    soc2 = box.make_soc(cfg=cfg)
    assert sum('slot' in str(x) for x in soc2.get_cfg()['extra_description']) == n == 8


def _lock_held_elsewhere(lk):
    out = []
    t = threading.Thread(target=lambda: out.append(lk.acquire(blocking=False)))
    t.start()
    t.join()
    if out[0]:
        lk.release()
    return not out[0]


class _LibSoc(F.FakeRfSoc):
    """Stands for RFQickSoc216V1: rfb_* methods that drive the cards behind rfbox's back."""

    def rfb_set_ro_rf(self, ro_ch, att):
        self.seen_lock = _lock_held_elsewhere(R.soc_lock(self))
        chain = self.adc_cards[2].chains[0]
        with self.board_sel.enable_context(chain.card_num):
            chain.attn[0].set_att(att)

    def rfb_get_lo(self):
        return 1.0


def test_soc_class_locks_rfb_methods_and_forgets_writes():
    cls = R.soc_class(_LibSoc)
    box = F.FakeQickBox()
    soc = cls(F.example_cfg())
    box.make_soc(cfg=soc.get_cfg(), target=soc)
    rf = R.attach(soc, sleep=lambda s: None)
    assert R.locked(soc) is rf.lock
    with R.locked(soc):
        with rf.lock:                                   # re-entrant
            rf.get()
    rf.set([{'ro': 0, 'att': 20}])
    assert rf.get()['adc_ports']['4']['att']['source'] == 'written'
    soc.rfb_get_lo()
    assert rf.get()['adc_ports']['4']['att']['source'] == 'written'     # readers do not invalidate
    soc.rfb_set_ro_rf(0, 5)
    assert soc.seen_lock is True and box.att('adc', 4) == 5.0
    a = rf.get()['adc_ports']['4']['att']
    assert a['source'] == 'unknown' and 'rfb_set_ro_rf' in a['why'] and a['previous']['value'] == 20.0
    assert cls.rfb_set_ro_rf._rfbox_locked and not hasattr(_LibSoc.rfb_set_ro_rf, '_rfbox_locked')
