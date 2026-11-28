"""Driver for the daughter cards of a ZCU216 QICK box (the RF front end), over the QICK library's own objects.

One file, Python 3.8, standard library only. It runs on the board inside whatever process holds the soc (the stock
QICK Pyro server, the lab board server, a Jupyter kernel) and imports on a PC without pynq, where the validation, the
filter table, the description format and the fake (qick/rfbox_fake.py) can be used. It imports nothing from qick:
copy it next to a server as rfbox.py and it works there too.

What it needs from the soc is what RFQickSoc216V1 has had since qick 0.2.360 (checked against the library at
0.2.360 and 0.2.450):
    soc.adc_cards, soc.dac_cards      DaughterCard216 objects (or None) per slot, each with .NAME, .card_num,
                                      .switch_control (SwitchControl: net2port, devs[] of GpioMCP23S08) and .chains
    chain.attn[i].set_att             AttenuatorPE43705 (write-only)
    chain.filter.write_reg/read_reg   FilterADMV8818 (WR0_SW, WR0_FILTER)
    chain.gain.set_gain/get_gain      GainLMH6401 (DC In)
    chain.powerup / chain.powerdown   the GPIO net of the chain's amplifier rail / power-down pin
    soc.board_sel.enable_context(id)  BoardSelection
    soc.biases[i].set_volt/get_volt   BiasDAC11001
    soc.rf.set_adc_attenuator/get_adc_attenuator, soc.rf._instance   the RF data converter (and xrfdc for the flags)
    soc.filter_spi                    only to read the ID of a slot the library left empty
It never calls rfb_* methods: several of them clear every ADC's interrupt flags or switch a shared supply rail as a
side effect (slab/notes/rfbox-driver.md lists them). Every SPI and converter access of this module holds one lock per
soc (soc._rfbox_lock); calls made to the soc's rfb_* methods by other code do not take it.

    box = rfbox.attach(soc)           # one RfBox per soc object; a new soc (image load) gets a new one
    box.get()                         # the whole state, plain JSON-able data; reading never clears a flag
    box.layout()                      # cards per slot, card IDs, which generator / readout / ADC block is where
    box.set([{"ro": 0, "adc_att": 27}, {"ro": 0, "att": 10, "amp": True}])   # validated, ordered, verified
    box.apply({"gens": {...}, "ros": {...}, "bias": {...}, "cards": {...}})  # declarative (qick_rf's format)
    box.apply_safe()                  # ADC attenuators 27 dB, amplifiers off, attenuators 31.75 dB; bias untouched
    box.clear_flags(); box.limits / box.set_limits(table); box.check_applied(description, record)
Over Pyro: soc_class(RFQickSoc216V1) adds rfbox_* methods to the served soc (RfBoxSocMixin), which return plain
dicts and turn a refusal into {"ok": False, "refused": ...} (check_reply raises it again on the client).

Every value in get() says where it came from: "read" (read back from the chip now), "written" (the last value this
driver wrote since the soc was constructed; the chip cannot be read), "assumed" (what the library constructor is
believed to have written; not confirmed), "unknown", or "error". Nothing here has been run on a board: see
slab/notes/rfbox-driver.md for the hardware checklist.
"""
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from numbers import Integral, Real

API_VERSION = 1

# ---- nominal datasheet tables -----------------------------------------------------------------------------------------
ATT_MAX, ATT_STEP = 31.75, 0.25          # PE43705 step attenuators (RF Out: two per channel, RF In: one)
DSA_MAX = 27                             # RFSoC Gen 3 RF-ADC digital step attenuator, 1 dB steps
ADC_ATT_MAX = DSA_MAX
ADC_ATT_DEFAULT = DSA_MAX
GAIN_MIN, GAIN_MAX = -6, 26              # LMH6401 (DC In), 1 dB steps
BIAS_MAX = 10.0                          # DAC11001 bias outputs, -10 .. 10 V, 20 bits
BIAS_BITS = 20
FTYPES = ('bypass', 'lowpass', 'highpass', 'bandpass')
F_MIN, F_MAX = 0.5, 20.0                 # GHz: what a filter frequency may be (the library's filter unit is GHz)
F_EDGE_MIN = 0.1                         # GHz: FilterADMV8818.freq2steps refuses an edge below 100 MHz
IDLE_WAIT_S = 0.2
SETTLE_DEFAULT_S = 0.2
SETTLE_MAX_S = 10.0

# ADMV8818 band edges (state 0 and state 15 of each band, MHz), FilterADMV8818.freq2steps (qick >= 0.2.4xx)
FILTER_BANDS = {'LPF': [[2050, 3850], [3350, 7250], [7000, 13000], [12550, 18850]],
                'HPF': [[1750, 3550], [3400, 7250], [6600, 12600], [12500, 19900]]}

DATASHEET = {
    'PE43705': {'what': 'step attenuator, RF Out (att1, att2 per channel) and RF In (one per channel)',
                'range_db': [0.0, ATT_MAX], 'step_db': ATT_STEP, 'readback': False,
                'why': 'serial-in only on the cards (SI/CLK/LE, no data-out); hardware.py AttenuatorPE43705'},
    'ADMV8818': {'what': 'tunable filter, one per RF Out and RF In channel', 'bands_mhz': FILTER_BANDS,
                 'states_per_band': 16, 'readback': True,
                 'why': 'SDO wired on the cards; hardware.py FilterADMV8818.read_reg'},
    'LMH6401': {'what': 'DC In variable-gain amplifier', 'range_db': [GAIN_MIN, GAIN_MAX], 'step_db': 1,
                'readback': True, 'why': 'hardware.py GainLMH6401.get_gain reads GAIN_REG'},
    'DAC11001': {'what': 'main-board bias outputs 0-7', 'range_v': [-BIAS_MAX, BIAS_MAX], 'bits': BIAS_BITS,
                 'readback': True, 'why': 'hardware.py BiasDAC11001.get_volt reads DAC_DATA_REG (the setpoint)'},
    'MCP23S08': {'what': 'GPIO expander per card: amplifier rails, power-downs, card ID (pins 4-7)',
                 'readback': True, 'why': 'GPIO_REG read: the commanded output level of the expander pin, '
                                          'not the supply rail itself'},
    'RF-ADC DSA': {'what': 'RFSoC Gen 3 ADC step attenuator, per ADC block', 'range_db': [0, DSA_MAX], 'step_db': 1,
                   'readback': True, 'why': 'xrfdc block DSA property (RFDC.get_adc_attenuator)'},
}

# the card chain order and supply rails, from the published card schematics (not from the library)
SCHEMATIC = {
    'rf_in': 'SMA - LNA - filter - 2 gain stages - attenuator - 3 gain stages - balun - ADC; one switched 5 V rail per '
             'channel (RFIN5V0CHn_EN) feeds all six amplifiers of the channel; the attenuator protects only the last '
             'three stages and the ADC',
    'rf_out': 'DAC - balun - filter - att1 - amplifier - att2 - amplifier - SMA; two channels share each 5 V rail '
              '(RFOUT5V0_EN0/1)',
}

# ADC interrupt bits (XRFdc_GetIntrStatus; the masks QickSoc.clear_interrupts decodes in qick 0.2.450)
OVR_VOLTAGE = 0x04000000
OVR_RANGE = 0x08000000
SUBADC_RANGE = 0x00FF0000
FLAG_BITS = [(0x0000000F, 'FIFOUSRDAT'), (0x00000FF0, 'DATAPATH'), (0x00FF0000, 'SUBADC_DCDR'),
             (0x04000000, 'OVR_VOLTAGE'), (0x08000000, 'OVR_RANGE'), (0x10000000, 'CMODE_OVR'),
             (0x20000000, 'CMODE_UNDR'), (0x40000000, 'DAT_OVR'), (0x80000000, 'FIFO_OVR')]

KINDS = {'RF Out': 'rf_out', 'DC Out': 'dc_out', 'Balun Out': 'balun_out',
         'RF In': 'rf_in', 'DC In': 'dc_in', 'Balun In': 'balun_in'}
NAMES = dict((v, k) for k, v in KINDS.items())
# the 4-bit card IDs (GPIO pins 4-7 of the card's MCP23S08), rfboard.py RFQickSoc216V1Mixin._rfb_config
CARD_IDS = {'dac': {1: 'dc_out', 3: 'rf_out', 6: 'balun_out'}, 'adc': {0: 'dc_in', 2: 'rf_in', 5: 'balun_in'}}
SIDE_KINDS = {'adc': ('rf_in', 'dc_in', 'balun_in'), 'dac': ('rf_out', 'dc_out', 'balun_out')}

# What RFQickSoc216V1's constructor does to the cards at every soc construction (every image load, every server
# start), from the library code at 0.2.450 (rfboard.py, drivers/hardware.py; 0.2.360 has the same card code without
# balun cards, the tile restart and the interrupt clear). Not measured: slab/notes/rfbox-driver.md has the file:line
# references and the hardware checks.
LIBRARY_CONSTRUCTOR = {
    'source': 'read from qick.rfboard RFQickSoc216V1Mixin._rfb_config and the card/chain constructors; not measured',
    'bias': 'every DAC11001 written CONFIG1 = 0x4e00 and 0 V (BiasDAC11001.__init__), then the bias switch enabled '
            '(rfb_enable_bias): a load steps every bias output to 0 V',
    'card_detect': 'every slot is selected and its GPIO expander written IODIR = 0xf0, then the ID read '
                   '(GPIO_REG >> 4); unknown IDs (and balun IDs before 0.2.4xx) give no card',
    'gpio_defaults': 'SwitchControl.add_MCP writes the defaults MSB-first: output i lands on pin 3-i while set_bits '
                     'uses pin i. RF In / RF Out defaults are all 0 (rails off) so the mirror does not matter; DC In '
                     'PD0/PD1 = 1 is written to pins 3/2, so pins 0/1 (the real PD0/PD1) are written 0: DC In '
                     'amplifiers powered up. DC Out PD0-3 = 0 (powered up). Measured on an rfbv2 box: an RF In '
                     'amplifier left on by the previous image stays on through start-up, so this write may not land',
    'attenuators': 'every PE43705 is written 31.75 dB in its chain constructor, but FilterChain.init_filter opens and '
                   'closes its own card selection inside the card constructor, so from the second chain of a card on '
                   'the attenuator writes are made with the card deselected (chain 0 of each card: written while '
                   'selected; the others: probably not received)',
    'filters': 'ADMV8818: only ADI_SPI_CONFIG_A = 0x3C is written; the band/state registers keep what they held',
    'dc_in_gain': 'LMH6401 written -6 dB (both chains, card selected)',
    'rf_adc_dsa': 'not written by the constructor; the bitstream load leaves it at 0 dB (measured on an rfbv2 box)',
    'adc_tiles': '0.2.4xx: every ADC tile restarted, then every ADC interrupt cleared (clear_interrupts)',
    'forced': {
        'bias_volts': [0.0] * 8, 'bias_enabled': True,
        'rf_out_att_db': {'chain 0 of each card': [ATT_MAX, ATT_MAX], 'chains 1-3': 'written deselected: unknown'},
        'rf_in_att_db': {'chain 0': ATT_MAX, 'chain 1': 'written deselected: unknown'},
        'rf_out_supply_on': False, 'rf_in_amp_on': 'written False, but measured to stay on through start-up',
        'dc_out_powered': True, 'dc_in_powered': 'intended False; pins 0/1 written 0 (powered up)',
        'dc_in_gain_db': -6, 'filters': 'not forced', 'rf_adc_att_db': 'not forced (0 after the bitstream load)',
    },
}

READBACK = {
    'read': ['adc_att (RF-ADC step attenuator, from the converter)', 'flags (ADC interrupt word; get() never clears)',
             'amp / supply / powered (the card GPIO expander output level: the commanded state, not the rail)',
             'gain (LMH6401 GAIN_REG)', 'filter (ADMV8818 WR0_SW / WR0_FILTER)', 'bias (DAC11001 setpoint register)',
             'card id (GPIO pins 4-7)'],
    'not_read': ['att (PE43705, write-only: "written" by this driver since the soc was constructed, "assumed" from '
                 'the constructor, else "unknown")', 'bias_enabled (the AXI GPIO bias switch: last written)',
                 'the supply rails themselves (only the expander pin that switches them)'],
}


# ---- errors ---------------------------------------------------------------------------------------------------------
class RfboxError(ValueError):
    """A request that is wrong for this driver or this board; nothing was touched. code: bad_request (a field),
    no_rf_board (the channel is not on a card of the kind needed; the message names slot and card), rf_limit (outside
    the limits table), unknown_state (needs a state this driver does not know, e.g. an attenuator never written)."""

    def __init__(self, code, message, **info):
        super().__init__(message)
        self.code = code
        self.info = info


class Refused(RuntimeError):
    """The settings were not (completely) applied, and why; nothing that needs them should run. .record holds what
    was done, what was read back, and (.record['state']) the state re-read after the failure. code: adc_flags,
    step_failed, readback, layout, rf_safe_failed."""

    def __init__(self, message, record=None, code='refused'):
        super().__init__(message)
        self.record = record
        self.code = code


def _bad(field, msg):
    return RfboxError('bad_request', '%s: %s' % (field, msg), field=field)


# ---- small helpers ----------------------------------------------------------------------------------------------------
def decode_flags(word):
    w = int(word or 0) & 0xFFFFFFFF
    return {'word': w, 'word_hex': '0x%08x' % w, 'over_voltage': bool(w & OVR_VOLTAGE),
            'over_range': bool(w & (OVR_RANGE | SUBADC_RANGE)),
            'other': w & ~(OVR_VOLTAGE | OVR_RANGE | SUBADC_RANGE) & 0xFFFFFFFF,
            'names': [n for m, n in FLAG_BITS if w & m]}


flags = decode_flags          # qick_rf.flags


def flag_list(fl):
    out = []
    for name in sorted(fl):
        e = {'block': name}
        e.update(fl[name])
        out.append(e)
    return out


def adc_block_of_port(port):
    return '%d%d' % (port // 4 + 1, port % 4)


def adc_port_of_block(name):
    t, b = int(name[0]), int(name[1])
    return (t - 1) * 4 + b if t in (1, 2) else None


def filter_step_freq(section, step):
    """Cutoff (GHz) of filter step 0..63 in the library's table."""
    f0, f15 = FILTER_BANDS[section][step // 16]
    return (f0 + (f15 - f0) * (step % 16) / 15.0) / 1000.0


def filter_step(section, f_ghz):
    """The step whose cutoff is nearest f (GHz), as FilterADMV8818._freq2step (0.2.4xx) picks it."""
    return min(range(64), key=lambda s: abs(filter_step_freq(section, s) - f_ghz))


def filter_steps_for(f):
    """(hpf, lpf) steps for a filter request ({ftype, fc, bw} GHz or {hpf, lpf}); -1 is bypass."""
    if 'hpf' in f or 'lpf' in f:
        return f.get('hpf', -1), f.get('lpf', -1)
    ft = f['ftype']
    if ft == 'lowpass':
        return -1, filter_step('LPF', f['fc'])
    if ft == 'highpass':
        return filter_step('HPF', f['fc']), -1
    if ft == 'bandpass':
        return filter_step('HPF', f['fc'] - f['bw'] / 2), filter_step('LPF', f['fc'] + f['bw'] / 2)
    return -1, -1


def filter_regs(hpf, lpf):
    """WR0_SW / WR0_FILTER for (hpf, lpf) steps, as FilterADMV8818.set_filter_raw computes them."""
    hb, hs = (0, 0) if hpf < 0 else (1 + hpf // 16, hpf % 16)
    lb, ls = (0, 0) if lpf < 0 else (1 + lpf // 16, lpf % 16)
    return {'WR0_SW': 0xC0 + (hb << 3) + lb, 'WR0_FILTER': (hs << 4) + ls}


def decode_filter(regs):
    """WR0_SW / WR0_FILTER -> {ftype, fc, bw (GHz), hpf, lpf, hpf_ghz, lpf_ghz}, or None if the registers do not hold
    a setting set_filter_raw can write."""
    try:
        sw, filt = int(regs['WR0_SW']), int(regs['WR0_FILTER'])
    except (KeyError, TypeError, ValueError):
        return None
    if sw & 0xC0 != 0xC0 or sw > 0xFF:
        return None
    hb, lb = (sw >> 3) & 7, sw & 7
    hs, ls = (filt >> 4) & 15, filt & 15
    if hb > 4 or lb > 4 or (hb == 0 and hs) or (lb == 0 and ls):
        return None
    hpf = -1 if hb == 0 else (hb - 1) * 16 + hs
    lpf = -1 if lb == 0 else (lb - 1) * 16 + ls
    out = {'hpf': hpf, 'lpf': lpf,
           'hpf_ghz': None if hpf < 0 else filter_step_freq('HPF', hpf),
           'lpf_ghz': None if lpf < 0 else filter_step_freq('LPF', lpf)}
    if hpf < 0 and lpf < 0:
        out.update(ftype='bypass', fc=None, bw=None)
    elif hpf < 0:
        out.update(ftype='lowpass', fc=out['lpf_ghz'], bw=None)
    elif lpf < 0:
        out.update(ftype='highpass', fc=out['hpf_ghz'], bw=None)
    else:
        out.update(ftype='bandpass', fc=(out['hpf_ghz'] + out['lpf_ghz']) / 2, bw=out['lpf_ghz'] - out['hpf_ghz'])
    return out


def bias_quantise(v):
    """The voltage BiasDAC11001.set_volt sets for v (20 bits over -10 .. 10 V; the top code rounded down)."""
    d = int(round(2 ** BIAS_BITS * (v + BIAS_MAX) / (2 * BIAS_MAX)))
    d = min(d, 2 ** BIAS_BITS - 1)
    return d * (2 * BIAS_MAX) / 2 ** BIAS_BITS - BIAS_MAX


def _num(v, field):
    if isinstance(v, bool) or not isinstance(v, Real) or v != v or v in (float('inf'), float('-inf')):
        raise _bad(field, '%r is not a number' % (v,))
    return float(v)


def _att(v, field, hi=ATT_MAX, step=ATT_STEP, unit='dB', what='the attenuator step'):
    v = _num(v, field)
    if not 0 <= v <= hi:
        raise _bad(field, '%g %s is outside 0 .. %g %s' % (v, unit, hi, unit))
    if abs(v / step - round(v / step)) > 1e-9:
        raise _bad(field, '%g %s is not a multiple of %g %s (%s)' % (v, unit, step, unit, what))
    return v


def _dsa(v, field):
    v = _num(v, field)
    if not 0 <= v <= DSA_MAX or v != int(v):
        raise _bad(field, '%g dB: the RF-ADC attenuator takes whole dB in 0 .. %d' % (v, DSA_MAX))
    return float(v)


def _filter(f, field, allow_raw=True):
    if not isinstance(f, dict):
        raise _bad(field, 'a filter is {ftype: bypass|lowpass|highpass|bandpass, fc: GHz, bw: GHz}'
                   + (' or {hpf, lpf} (raw steps)' if allow_raw else ''))
    if allow_raw and ('hpf' in f or 'lpf' in f):
        bad = sorted(set(f) - {'hpf', 'lpf'})
        if bad:
            raise _bad(field, 'raw filter has only hpf and lpf, not %s' % ', '.join(bad))
        out = {}
        for k in ('hpf', 'lpf'):
            v = f.get(k, -1)
            if isinstance(v, bool) or not isinstance(v, Integral) or not -1 <= v <= 63:
                raise _bad(field + '.' + k, '%r: -1 (bypass) or 0 .. 63' % (v,))
            out[k] = int(v)
        return out
    bad = sorted(set(f) - {'ftype', 'fc', 'bw'})
    if bad:
        raise _bad(field, 'unknown key %s (a filter has ftype, fc, bw)' % ', '.join(bad))
    ft = f.get('ftype')
    if ft not in FTYPES:
        raise _bad(field + '.ftype', '%r is not one of %s' % (ft, ', '.join(FTYPES)))
    out = {'ftype': ft}
    if ft != 'bypass':
        if f.get('fc') is None:
            raise _bad(field, 'a %s filter needs fc [GHz]' % ft)
        fc = _num(f['fc'], field + '.fc')
        if not F_MIN <= fc <= F_MAX:
            raise _bad(field + '.fc', '%g GHz is outside %g .. %g GHz (filter frequencies are in GHz)'
                       % (fc, F_MIN, F_MAX))
        out['fc'] = fc
    if ft == 'bandpass':
        if f.get('bw') is None:
            raise _bad(field, 'a bandpass filter needs bw [GHz]')
        bw = _num(f['bw'], field + '.bw')
        if not 0 < bw < 2 * out['fc'] or out['fc'] - bw / 2 < F_EDGE_MIN or out['fc'] + bw / 2 > F_MAX:
            raise _bad(field + '.bw', '%g GHz does not fit around fc %g GHz (edges within %g .. %g GHz)'
                       % (bw, out['fc'], F_EDGE_MIN, F_MAX))
        out['bw'] = bw
    return out


def _filter_applied(req):
    """What a filter request becomes: the steps, their cutoffs, and the setting they decode to."""
    h, l = filter_steps_for(req)
    dec = decode_filter(filter_regs(h, l)) or {}
    return dec


# ---- rf_set settings (the board server's vocabulary) ---------------------------------------------------------------------
ITEM_KEYS = {'ro', 'gen', 'adc', 'bias', 'adc_att', 'filter', 'att', 'amp', 'gain', 'volts', 'dc', 'supply'}


def check_items(settings, allow_empty=False):
    """Syntax check of a settings list (no board needed). Returns a normalized copy; raises RfboxError naming the
    field. Items:
        {"ro": R, "adc_att": dB}                 the RF-ADC step attenuator of readout R's ADC block (0-27, whole dB)
        {"adc": "20", "adc_att": dB}             the same by ADC block name
        {"ro": R, "filter": {...}}               RF In filter
        {"ro": R, "att": dB}                     RF In attenuator (0-31.75 in 0.25); the amplifier is not touched
        {"ro": R, "amp": true|false}             RF In amplifier rail / DC In power-down, on its own
        {"ro": R, "amp": true, "att": dB}        RF In: attenuator, then amplifier on (one step)
        {"ro": R, "amp": true, "gain": dB}       DC In: gain, then powered up (one step)
        {"ro": R, "gain": dB}                    DC In gain only
        {"gen": G, "att": [A1, A2]}              RF Out attenuators; switches the channel's 5 V rail on (shared with
                                                 its pair, reported) unless "supply": false
        {"gen": G, "supply": true|false}         RF Out 5 V rail of the channel (and of its pair)
        {"gen": G, "filter": {...}}              RF Out filter
        {"gen": G, "dc": true|false}             DC Out powered up / down
        {"bias": I, "volts": V}                  main-board bias output 0-7
    A filter is {ftype, fc, bw} (GHz) or {hpf, lpf} (raw steps, -1 = bypass)."""
    if not isinstance(settings, list) or (not settings and not allow_empty):
        raise _bad('settings', 'a non-empty list of settings')
    out = []
    for i, it in enumerate(settings):
        w = 'settings[%d]' % i
        if not isinstance(it, dict):
            raise _bad(w, 'each setting is an object')
        bad = sorted(set(it) - ITEM_KEYS)
        if bad:
            raise _bad(w, 'unknown key %s' % ', '.join(bad))
        targets = [k for k in ('ro', 'gen', 'adc', 'bias') if k in it]
        if len(targets) != 1:
            raise _bad(w, 'exactly one of ro (readout index), gen (generator index), adc (ADC block name, for adc_att '
                          'only) or bias (bias output 0-7)')
        t = targets[0]
        n = {t: it[t]}
        if t == 'adc':
            if not isinstance(it['adc'], str) or len(it['adc']) != 2 or not it['adc'].isdigit():
                raise _bad(w + '.adc', 'an ADC block name such as "20" (tile 2, block 0)')
        else:
            v = it[t]
            if isinstance(v, bool) or not isinstance(v, Integral) or v < 0 or (t == 'bias' and v > 7):
                raise _bad(w + '.' + t, '%r is not a channel number' % (v,))
            n[t] = int(v)
        allowed = {'ro': {'adc_att', 'filter', 'att', 'amp', 'gain'}, 'gen': {'filter', 'att', 'dc', 'supply'},
                   'adc': {'adc_att'}, 'bias': {'volts'}}[t]
        keys = set(it) - {t}
        if not keys:
            raise _bad(w, 'nothing to set')
        if keys - allowed:
            raise _bad(w, '%s cannot be set on a %s (allowed: %s)' % (', '.join(sorted(keys - allowed)), t,
                                                                    ', '.join(sorted(allowed))))
        if 'adc_att' in it:
            n['adc_att'] = _dsa(it['adc_att'], w + '.adc_att')
        if 'filter' in it:
            n['filter'] = _filter(it['filter'], w + '.filter')
        if t == 'gen':
            if 'att' in it:
                a = it['att']
                if not isinstance(a, (list, tuple)) or len(a) != 2:
                    raise _bad(w + '.att', 'a generator on an RF Out card takes two attenuations [A1, A2] (dB)')
                n['att'] = [_att(a[0], w + '.att[0]'), _att(a[1], w + '.att[1]')]
            for k in ('supply', 'dc'):
                if k in it and not isinstance(it[k], bool):
                    raise _bad(w + '.' + k, 'true or false')
            if 'dc' in it:
                if set(it) & {'att', 'filter', 'supply'}:
                    raise _bad(w + '.dc', 'dc is for a DC Out card; att, filter and supply are for RF Out')
                n['dc'] = it['dc']
            if 'supply' in it:
                n['supply'] = it['supply']
        if t == 'ro':
            if 'amp' in it and not isinstance(it['amp'], bool):
                raise _bad(w + '.amp', 'true or false')
            if 'att' in it and 'gain' in it:
                raise _bad(w, 'att is for an RF In card, gain for a DC In card: not both')
            if 'att' in it:
                n['att'] = _att(it['att'], w + '.att')
            if 'gain' in it:
                if it.get('amp') is False:
                    raise _bad(w + '.gain', 'gain with amp: false')
                g = _num(it['gain'], w + '.gain')
                if not GAIN_MIN <= g <= GAIN_MAX or g != int(g):
                    raise _bad(w + '.gain', '%g dB: whole dB in %d .. %d' % (g, GAIN_MIN, GAIN_MAX))
                n['gain'] = g
            if 'amp' in it:
                n['amp'] = it['amp']
        if t == 'bias':
            v = _num(it['volts'], w + '.volts')
            if not -BIAS_MAX <= v <= BIAS_MAX:
                raise _bad(w + '.volts', '%g V is outside -%g .. %g V' % (v, BIAS_MAX, BIAS_MAX))
            n['volts'] = v
        out.append(n)
    return out


def check_settle(v):
    if v is None:
        return SETTLE_DEFAULT_S
    if isinstance(v, bool) or not isinstance(v, Real) or not 0 <= v <= SETTLE_MAX_S:
        raise _bad('settle_s', 'seconds in 0 .. %g' % SETTLE_MAX_S)
    return float(v)


LIMIT_KEYS = ('bias', 'adc_att_min', 'ro_att_min', 'gen_att_min', 'dc_gain_max')


def check_limits_table(lim):
    """Syntax check of a limits table:
        bias         {"<output 0-7>": [lo, hi]}  V; with this section, outputs not listed may only be 0 V
        adc_att_min  {"<ADC block, e.g. 20>": dB}  floor of the RF-ADC attenuator
        ro_att_min   {"<readout>": dB}             floor of the RF In card attenuator
        gen_att_min  {"<generator>": dB or [dB1, dB2]}  floor of the RF Out attenuators
        dc_gain_max  {"<readout>": dB}             ceiling of the DC In gain
    Returns a normalized copy (keys as strings), or None for None."""
    if lim is None:
        return None
    if not isinstance(lim, dict):
        raise _bad('limits', 'an object with %s' % ', '.join(LIMIT_KEYS))
    bad = sorted(set(lim) - set(LIMIT_KEYS))
    if bad:
        raise _bad('limits', 'unknown section %s (sections: %s)' % (', '.join(bad), ', '.join(LIMIT_KEYS)))
    out = {}
    for sec, table in lim.items():
        f = 'limits.' + sec
        if not isinstance(table, dict):
            raise _bad(f, 'an object keyed by channel')
        o = {}
        for k, v in table.items():
            fk = '%s.%s' % (f, k)
            if sec == 'bias':
                if str(k) not in [str(i) for i in range(8)]:
                    raise _bad(fk, 'bias outputs are 0-7')
                if not isinstance(v, (list, tuple)) or len(v) != 2:
                    raise _bad(fk, '[lo, hi] in V')
                lo, hi = _num(v[0], fk + '[0]'), _num(v[1], fk + '[1]')
                if not -BIAS_MAX <= lo <= hi <= BIAS_MAX:
                    raise _bad(fk, '[%g, %g] V must be increasing within -%g .. %g V' % (lo, hi, BIAS_MAX, BIAS_MAX))
                o[str(k)] = [lo, hi]
            elif sec == 'gen_att_min':
                vv = list(v) if isinstance(v, (list, tuple)) else [v, v]
                if len(vv) != 2:
                    raise _bad(fk, 'dB or [dB1, dB2]')
                o[str(k)] = [_num(vv[0], fk), _num(vv[1], fk)]
            else:
                o[str(k)] = _num(v, fk)
        out[sec] = o
    return out


def _limit(output, limit, value, msg):
    return RfboxError('rf_limit', '%s: %s' % (output, msg), output=output, limit=limit, value=value)


def check_limits(items, limits, ros):
    """Refuse (RfboxError rf_limit) any item outside a limits table. ros: {readout: {'adc': block, ...}}
    (ro_map), to find a readout's ADC block."""
    if not limits:
        return
    for it in items:
        if 'adc_att' in it:
            block = it['adc'] if 'adc' in it else (ros.get(it['ro']) or {}).get('adc')
            lo = limits.get('adc_att_min', {}).get(str(block))
            if lo is not None and it['adc_att'] < lo:
                raise _limit('adc %s' % block, {'adc_att_min': lo}, it['adc_att'],
                             'RF-ADC attenuator %g dB is below the floor %g dB' % (it['adc_att'], lo))
        if 'ro' in it and 'att' in it:
            lo = limits.get('ro_att_min', {}).get(str(it['ro']))
            if lo is not None and it['att'] < lo:
                raise _limit('ro %d' % it['ro'], {'ro_att_min': lo}, it['att'],
                             'RF In attenuator %g dB is below the floor %g dB' % (it['att'], lo))
        if 'ro' in it and 'gain' in it:
            hi = limits.get('dc_gain_max', {}).get(str(it['ro']))
            if hi is not None and it['gain'] > hi:
                raise _limit('ro %d' % it['ro'], {'dc_gain_max': hi}, it['gain'],
                             'DC In gain %g dB is above the ceiling %g dB' % (it['gain'], hi))
        if 'gen' in it and 'att' in it:
            lo = limits.get('gen_att_min', {}).get(str(it['gen']))
            if lo is not None and (it['att'][0] < lo[0] or it['att'][1] < lo[1]):
                raise _limit('gen %d' % it['gen'], {'gen_att_min': lo}, it['att'],
                             'RF Out attenuators %s dB below the floor %s dB' % (it['att'], lo))
        if 'bias' in it and 'bias' in limits:
            lh = limits['bias'].get(str(it['bias']))
            if lh is None and it['volts'] != 0:
                raise _limit('bias %d' % it['bias'], {'bias': None}, it['volts'],
                             '%g V: the limits table has no range for this output (only 0 V allowed)' % it['volts'])
            if lh is not None and not lh[0] <= it['volts'] <= lh[1]:
                raise _limit('bias %d' % it['bias'], {'bias': lh}, it['volts'],
                             '%g V is outside [%g, %g] V' % (it['volts'], lh[0], lh[1]))


# ---- the description (qick_rf's rf: block) ------------------------------------------------------------------------------
def _chan(k, what, top=64):
    try:
        n = int(k)
    except (TypeError, ValueError):
        raise _bad(what, '%r is not a channel number' % (k,))
    if isinstance(k, bool) or str(n) != str(k).strip() or not 0 <= n < top:
        raise _bad(what, '%r is not a channel number' % (k,))
    return n


def normalize(rf):
    """The description checked and in canonical form (qick_rf.normalize, extended):
        gens: {G: {att: [A1, A2], filter: {...}}}   RF Out generator
              {G: {dc: true|false}}                  DC Out generator powered up / down
        ros:  {R: {att, filter, adc_att (default 27), amp (default true)}}   RF In readout
              {R: {gain, adc_att, amp}}              DC In readout
        bias: {I: {volts, limits: [LO, HI]}}         limits REQUIRED for anything but 0 V
        cards: {adc: {slot: kind|none}, dac: {...}}  the layout the board must report
    RfboxError (a ValueError) names the key: rf.ros.0.att ..."""
    rf = rf or {}
    if not isinstance(rf, dict):
        raise _bad('rf', 'expected a mapping with gens, ros, bias, cards')
    bad = sorted(set(rf) - {'gens', 'ros', 'bias', 'cards'})
    if bad:
        raise _bad('rf', 'unknown key %s (it has gens, ros, bias, cards)' % ', '.join(bad))
    out = {'gens': {}, 'ros': {}, 'bias': {}, 'cards': None}
    for k, g in (rf.get('gens') or {}).items():
        ch, w = _chan(k, 'rf.gens'), 'rf.gens.%s' % k
        if isinstance(g, dict) and 'dc' in g:
            if set(g) - {'dc'} or not isinstance(g['dc'], bool):
                raise _bad(w, 'a DC Out generator has only dc: true|false')
            out['gens'][ch] = {'dc': g['dc']}
            continue
        if not isinstance(g, dict) or sorted(set(g) - {'att', 'filter'}):
            raise _bad(w, 'a generator has att: [A1, A2] and filter: {...} (or dc: true|false on DC Out)')
        a = g.get('att')
        if not isinstance(a, (list, tuple)) or len(a) != 2:
            raise _bad(w + '.att', 'two attenuations [A1, A2] in dB (both stages of the RF Out channel)')
        if 'filter' not in g:
            raise _bad(w, 'no filter (give {ftype: bypass} for none)')
        out['gens'][ch] = {'att': [_att(a[0], w + '.att[0]'), _att(a[1], w + '.att[1]')],
                           'filter': _filter(g['filter'], w + '.filter', allow_raw=False)}
    for k, r in (rf.get('ros') or {}).items():
        ch, w = _chan(k, 'rf.ros'), 'rf.ros.%s' % k
        if not isinstance(r, dict) or sorted(set(r) - {'att', 'filter', 'adc_att', 'amp', 'gain'}):
            raise _bad(w, 'a readout has att, filter, adc_att and amp (RF In) or gain, adc_att, amp (DC In)')
        amp = r.get('amp', True)
        if not isinstance(amp, bool):
            raise _bad(w + '.amp', 'true or false')
        adc_att = _att(ADC_ATT_DEFAULT if r.get('adc_att') is None else r['adc_att'], w + '.adc_att',
                       ADC_ATT_MAX, 1, what='the RF-ADC attenuator takes whole dB')
        if r.get('gain') is not None:
            if set(r) & {'att', 'filter'}:
                raise _bad(w, 'gain is for a DC In readout; att and filter are for RF In')
            g = _num(r['gain'], w + '.gain')
            if not GAIN_MIN <= g <= GAIN_MAX or g != int(g):
                raise _bad(w + '.gain', '%g dB: whole dB in %d .. %d' % (g, GAIN_MIN, GAIN_MAX))
            one = {'gain': g, 'adc_att': adc_att}
        else:
            if r.get('att') is None:
                raise _bad(w, 'no att (the RF In attenuator, dB)')
            if 'filter' not in r:
                raise _bad(w, 'no filter (give {ftype: bypass} for none)')
            one = {'att': _att(r['att'], w + '.att'), 'filter': _filter(r['filter'], w + '.filter', allow_raw=False),
                   'adc_att': adc_att}
        if amp is False:
            one['amp'] = False
        out['ros'][ch] = one
    for k, b in (rf.get('bias') or {}).items():
        ch, w = _chan(k, 'rf.bias', 8), 'rf.bias.%s' % k
        if not isinstance(b, dict) or sorted(set(b) - {'volts', 'limits'}):
            raise _bad(w, 'a bias output has volts and limits: [LO, HI]')
        lim = b.get('limits')
        if lim is not None:
            if not isinstance(lim, (list, tuple)) or len(lim) != 2:
                raise _bad(w + '.limits', '[LO, HI] in V')
            lo, hi = _num(lim[0], w + '.limits[0]'), _num(lim[1], w + '.limits[1]')
            if not -BIAS_MAX <= lo < hi <= BIAS_MAX:
                raise _bad(w + '.limits', '[%g, %g] V must be increasing and within -%g .. %g V'
                           % (lo, hi, BIAS_MAX, BIAS_MAX))
            lim = [lo, hi]
        v = _num(b.get('volts') if b.get('volts') is not None else 0.0, w + '.volts')
        if v != 0 and lim is None:
            raise _bad(w, '%g V needs limits: there are no default limits for a bias output. Add  limits: [LO, HI]  '
                          '(V) to rf.bias.%d, chosen by the setup\'s owner' % (v, ch))
        if lim is not None and not lim[0] <= v <= lim[1]:
            raise _bad(w + '.volts', '%g V is outside the limits %g .. %g V of the setup description'
                       % (v, lim[0], lim[1]))
        out['bias'][ch] = {'volts': v, 'limits': lim}
    if rf.get('cards') is not None:
        c = rf['cards']
        if not isinstance(c, dict) or sorted(set(c) - {'adc', 'dac'}):
            raise _bad('rf.cards', '{adc: {slot: kind}, dac: {slot: kind}}')
        out['cards'] = {'adc': {}, 'dac': {}}
        for side in ('adc', 'dac'):
            for s, kind in ((c.get(side) or {}).items()):
                slot = _chan(s, 'rf.cards.%s' % side, 4)
                if kind in (None, 'none', 'empty'):
                    kind = None
                elif kind not in SIDE_KINDS[side]:
                    raise _bad('rf.cards.%s.%s' % (side, s), '%r is not one of %s, none'
                               % (kind, ', '.join(SIDE_KINDS[side])))
                out['cards'][side][slot] = kind
    return out


def plain(spec):
    """A normalized description back as the block a setup description holds (string keys, JSON-able)."""
    out = {}
    for sec in ('gens', 'ros', 'bias'):
        if spec.get(sec):
            out[sec] = dict((str(k), dict(v)) for k, v in sorted(spec[sec].items()))
            if sec == 'bias':
                out[sec] = dict((k, dict((x, y) for x, y in v.items() if y is not None)) for k, v in out[sec].items())
    if spec.get('cards') is not None:
        out['cards'] = dict((side, dict((str(k), v or 'none') for k, v in sorted(spec['cards'][side].items())))
                            for side in ('adc', 'dac'))
    return out


def spec_hash(spec):
    """The same hash qick_rf.spec_hash gives for a description in its format."""
    return hashlib.sha256(json.dumps(plain(spec), sort_keys=True).encode()).hexdigest()[:12]


_CARD = re.compile(r'(ADC|DAC) slot (\d+): (?:No card detected|(.+?) card has ports \[([\d, ]*)\])')


def cfg_layout(cfg):
    """The cards as soccfg's extra_description lines report them (no board needed; qick_rf.layout):
    {"adc": {slot: {"kind", "name", "ports"}|None}, "dac": {...}}, or None when no QICK box cards are reported."""
    lines = (cfg or {}).get('extra_description') or []
    if isinstance(lines, str):
        lines = lines.splitlines()
    out, seen = {'adc': {}, 'dac': {}}, False
    for line in lines:
        m = _CARD.search(str(line))
        if not m:
            continue
        seen = True
        side, slot, name, ports = m.group(1).lower(), int(m.group(2)), m.group(3), m.group(4)
        out[side][slot] = None if name is None else {'kind': KINDS.get(name, name.lower().replace(' ', '_')),
                                                     'name': name,
                                                     'ports': [int(p) for p in ports.split(',') if p.strip()]}
    return out if seen else None


def layout_text(lay):
    if lay is None:
        return 'no QICK box daughter cards reported'
    parts = []
    for side in ('adc', 'dac'):
        cards = lay[side]
        items = sorted(cards.items()) if isinstance(cards, dict) else list(enumerate(cards))
        for slot, c in items:
            if isinstance(c, dict) and c.get('kind') is None and 'slot' in c:
                c = None
            parts.append('%s slot %d: %s' % (side.upper(), slot, 'none' if c is None else '%s %s' % (
                c.get('name') or NAMES.get(c.get('kind'), c.get('kind')), c.get('ports'))))
    return '; '.join(parts)


def _slot_kind(lay, side, slot):
    cards = (lay or {}).get(side) or {}
    c = cards.get(slot) if isinstance(cards, dict) else (cards[slot] if 0 <= slot < len(cards) else None)
    if isinstance(c, str):
        return c
    return c.get('kind') if isinstance(c, dict) else None


def gen_map(cfg, lay=None):
    """Generator index -> {dac, tile, block, port, slot, chain, kind} as RFQickSoc216V1 links them
    (gen.rfb_ch = dac_cards[tile].chains[block]); port = 4 * tile + block."""
    out = {}
    for i, g in enumerate((cfg or {}).get('gens') or []):
        name = str(g['dac'])
        t, b = int(name[0]), int(name[1])
        port = t * 4 + b
        out[i] = {'dac': name, 'tile': t, 'block': b, 'port': port, 'slot': t, 'chain': b,
                  'kind': _slot_kind(lay, 'dac', t)}
    return out


def ro_map(cfg, lay=None):
    """Readout index -> {adc, tile, block, index, port, slot, chain, kind, warning?}. The port follows the ADC's
    name, as RFQickSoc216V1 links readouts (adc_cards[2 * (tile - 1) + block // 2].chains[block % 2], from
    readout['adc']); `index` is the converter's (tile, block) from rf.adcs, which the flag and attenuator calls use.
    Readouts on ADC tiles 0 and 3 are on no QICK box port (the library links tile 0 to the wrong card and fails on
    tile 3; reported as `warning`)."""
    out = {}
    adcs = ((cfg or {}).get('rf') or {}).get('adcs') or {}
    for i, r in enumerate((cfg or {}).get('readouts') or []):
        name = str(r['adc'])
        t, b = int(name[0]), int(name[1])
        idx = (adcs.get(name) or {}).get('index')
        e = {'adc': name, 'tile': t, 'block': b, 'index': [int(idx[0]), int(idx[1])] if idx else [t, b]}
        if t in (1, 2):
            port = (t - 1) * 4 + b
            e.update(port=port, slot=port // 2, chain=b % 2, kind=_slot_kind(lay, 'adc', port // 2))
        else:
            e.update(port=None, slot=None, chain=None, kind=None,
                     warning='ADC tile %d is not wired to a QICK box port; the library links this readout to %s'
                             % (t, 'ADC slot %d (negative index): the wrong card' % ((2 * (t - 1) + b // 2) % 4)
                                if t == 0 else 'adc_cards[%d], which does not exist' % (2 * (t - 1) + b // 2)))
        out[i] = e
    return out


def channels(cfg, lay):
    """qick_rf.channels: {"gens": gen_map, "ros": ro_map}."""
    return {'gens': gen_map(cfg, lay), 'ros': ro_map(cfg, lay)}


def problems_for(spec, gens, ros, lay):
    """What stops a normalized description from being applied to a board with this layout (empty: nothing).
    lay: {'adc': {slot: kind|None}, 'dac': {...}}."""
    out = []
    if spec['cards'] is not None:
        bad = []
        for side in ('adc', 'dac'):
            for slot in range(4):
                want, got = spec['cards'][side].get(slot), lay[side].get(slot)
                if want != got:
                    bad.append('%s slot %d: the description has %s, the board reports %s'
                               % (side.upper(), slot, NAMES.get(want, want) if want else 'no card',
                                  NAMES.get(got, got) if got else 'no card'))
        if bad:
            out.append('card layout differs: ' + '; '.join(bad))
    for g in sorted(spec['gens']):
        c = gens.get(g)
        need = 'dc_out' if 'dc' in spec['gens'][g] else 'rf_out'
        if c is None:
            out.append('rf.gens.%d: the firmware has no generator %d (it has %d)' % (g, g, len(gens)))
        elif c['kind'] != need:
            out.append('rf.gens.%d: generator %d is on DAC port %d (slot %d, %s): %s'
                       % (g, g, c['port'], c['slot'], NAMES.get(c['kind'], 'no card'),
                          'DC Out power is on DC Out cards' if need == 'dc_out'
                          else 'attenuators and filters are on RF Out cards'))
    for r in sorted(spec['ros']):
        c = ros.get(r)
        need = 'dc_in' if 'gain' in spec['ros'][r] else 'rf_in'
        if c is None:
            out.append('rf.ros.%d: the firmware has no readout %d (it has %d)' % (r, r, len(ros)))
        elif c['kind'] != need:
            out.append('rf.ros.%d: readout %d is on ADC %s (port %s, slot %s, %s): %s'
                       % (r, r, c['adc'], c['port'], c['slot'], NAMES.get(c['kind'], 'no card'),
                          'the gain is on DC In cards' if need == 'dc_in'
                          else 'the amplifier and attenuators are on RF In cards'))
    return out


def problems(spec, cfg):
    """qick_rf.problems, from a soccfg alone (the layout its extra_description reports)."""
    if not (spec['gens'] or spec['ros'] or spec['bias'] or spec['cards']):
        return []
    if (cfg or {}).get('board') not in (None, 'ZCU216'):
        return ['rf: the daughter-card settings are for a ZCU216 QICK box; this board is a %s' % cfg.get('board')]
    lay = cfg_layout(cfg)
    if lay is None:
        return ['rf: the board reports no QICK box daughter cards (firmware without RF board support, or the soc '
                'class is not RFQickSoc216V1)']
    kinds = dict((side, dict((s, (c or {}).get('kind')) for s, c in lay[side].items())) for side in ('adc', 'dac'))
    return problems_for(spec, gen_map(cfg, lay), ro_map(cfg, lay), kinds)


# ---- the lock and the per-soc driver ----------------------------------------------------------------------------------
_GUARD = threading.RLock()        # re-entrant: attach's factory (RfBox) asks for soc_lock under it
_BY_ID = {}


def _soc_attr(soc, name, factory):
    with _GUARD:
        try:
            v = soc.__dict__.get(name)
        except AttributeError:
            v = None
        if v is None:
            v = _BY_ID.get((id(soc), name))
        if v is None:
            v = factory()
            try:
                object.__setattr__(soc, name, v)
            except Exception:      # noqa: BLE001  (an object that takes no attributes)
                _BY_ID[(id(soc), name)] = v
        return v


def soc_lock(soc):
    """The one lock around every hardware access of this module to this soc (SPI and board selection are shared)."""
    return _soc_attr(soc, '_rfbox_lock', threading.RLock)


def attach(soc, **kwargs):
    """The RfBox of this soc (created on first use; a new soc object, as after an image load, gets a new one)."""
    return _soc_attr(soc, '_rfbox', lambda: RfBox(soc, **kwargs))


def _val(value, source, **kw):
    d = {'value': value, 'source': source}
    d.update(kw)
    return d


def _exc(e):
    return '%s: %s' % (type(e).__name__, e)


class _VerifyError(RuntimeError):
    pass


def _library_info(soc):
    v = None
    try:
        v = soc['sw_version']
    except Exception:      # noqa: BLE001
        pass
    if v is None:
        m = sys.modules.get('qick')
        v = getattr(m, '__version__', None)
    rf = getattr(soc, 'rf', None)
    return {'qick_version': v,
            'soc_class': type(soc).__name__,
            'has_port_methods': callable(getattr(type(soc), 'rfb_set_adc_rf', None)),
            'has_adc_chains': isinstance(getattr(soc, '__dict__', {}).get('adc_chains'), list),
            'rf_set_adc_attenuator': rf is not None and callable(getattr(rf, 'set_adc_attenuator', None))}


class RfBox(object):
    """The QICK box front end of one soc (attach(soc) gives the shared one). See the module docstring."""

    def __init__(self, soc, xrfdc=None, sleep=time.sleep, limits=None):
        self.soc = soc
        self.lock = soc_lock(soc)
        self.sleep = sleep
        self._xrfdc = xrfdc
        self.limits = check_limits_table(limits) if limits else None
        self.identity = {'id': uuid.uuid4().hex[:16], 'created': time.time(), 'pid': os.getpid(),
                         'soc': '%x' % id(soc), 'rfbox_api': API_VERSION}
        self.library = _library_info(soc)
        self.identity['qick_version'] = self.library['qick_version']
        self._written = {}           # ('ro_att', port) | ('dac_att', port) | ('filter', side, port) | 'bias_enabled'
        self._assume_constructor()

    # ---- what the constructor left ----------------------------------------------------------------------------
    def _assume_constructor(self):
        """The write-only parts after RFQickSoc216V1's constructor (LIBRARY_CONSTRUCTOR)."""
        why0 = 'written 31.75 dB by the library constructor with the card selected; not confirmed'
        why1 = ('the library constructor wrote 31.75 dB after FilterChain.init_filter had deselected the card: '
                'probably not received')
        for p in self._ports().values():
            if p['kind'] == 'rf_out':
                self._written[('dac_att', p['port'])] = (
                    _val([ATT_MAX, ATT_MAX], 'assumed', by='library constructor', why=why0) if p['card_ch'] == 0
                    else _val(None, 'unknown', why=why1))
            elif p['kind'] == 'rf_in':
                self._written[('ro_att', p['port'])] = (
                    _val(ATT_MAX, 'assumed', by='library constructor', why=why0) if p['card_ch'] == 0
                    else _val(None, 'unknown', why=why1))
        if getattr(self.soc, 'bias_gpio', None) is not None:
            self._written['bias_enabled'] = _val(True, 'assumed', by='library constructor (rfb_enable_bias)')

    # ---- mapping (no hardware access) -------------------------------------------------------------------------
    def _cfg(self, key, default=None):
        try:
            v = self.soc[key]
        except Exception:      # noqa: BLE001
            return default
        return default if v is None else v

    def _cfgdict(self):
        return {'gens': self._cfg('gens', []), 'readouts': self._cfg('readouts', []), 'rf': self._cfg('rf', {})}

    def _cards(self, side):
        cards = getattr(self.soc, side + '_cards', None)
        if not isinstance(cards, (list, tuple)):
            return [None] * 4
        return list(cards) + [None] * (4 - len(cards))

    def _ports(self):
        """(side, port) -> {side, port, slot, board_id, card_ch, kind, name, card, chain}."""
        out = {}
        for side, n, off in (('dac', 4, 0), ('adc', 2, 4)):
            for slot, card in enumerate(self._cards(side)):
                if card is None:
                    continue
                name = getattr(card, 'NAME', '') or type(card).__name__
                kind = KINDS.get(name, name.lower().replace(' ', '_'))
                for ch, chain in enumerate(getattr(card, 'chains', []) or []):
                    port = int(getattr(chain, 'global_ch', chain)) if not isinstance(chain, Integral) else int(chain)
                    out[(side, port)] = {'side': side, 'port': port, 'slot': slot, 'board_id': off + slot,
                                         'card_ch': ch, 'kind': kind, 'name': name, 'card': card,
                                         'chain': None if isinstance(chain, Integral) else chain}
        return out

    def _kinds(self):
        out = {'adc': {}, 'dac': {}}
        for side in ('adc', 'dac'):
            for slot, card in enumerate(self._cards(side)):
                name = getattr(card, 'NAME', None) if card is not None else None
                out[side][slot] = None if card is None else KINDS.get(name, (name or '').lower().replace(' ', '_'))
        return out

    def gen_map(self):
        return gen_map(self._cfgdict(), self._kinds())

    def ro_map(self):
        return ro_map(self._cfgdict(), self._kinds())

    def adc_blocks(self):
        """[(name, tile, block)] of every RF-ADC in the firmware, (tile, block) being the converter's index."""
        out = []
        for name, c in sorted((self._cfg('rf', {}).get('adcs') or {}).items()):
            t, b = c['index']
            out.append((name, int(t), int(b)))
        return out

    def _need(self, side, port, kinds, what, field=None):
        p = self._ports().get((side, port)) if port is not None else None
        if p is None or p['kind'] not in kinds:
            slot = None if port is None else port // (4 if side == 'dac' else 2)
            card = 'no card' if p is None else '%s card' % p['name']
            raise RfboxError('no_rf_board', '%s is on %s port %s (%s slot %s): %s, not %s'
                             % (what, side.upper(), port, side.upper(), slot, card,
                                ' or '.join(NAMES.get(k, k) for k in kinds)), field=field, slot=slot,
                             card=None if p is None else p['kind'])
        return p

    # ---- low-level access (callers hold the lock) --------------------------------------------------------------
    @contextmanager
    def _sel(self, board_id):
        with self.soc.board_sel.enable_context(board_id):
            yield

    def _xr(self):
        if self._xrfdc is not None:
            return self._xrfdc
        x = getattr(self.soc, 'rfbox_xrfdc', None)
        if x is not None and not callable(x):
            return x
        try:
            import xrfdc       # comes with the board's PYNQ image
        except ImportError:
            return None
        return xrfdc

    def _flags_word(self, tile, block, clear):
        xr = self._xr()
        if xr is None:
            raise RuntimeError('ADC flags not available: no xrfdc module in this process')
        arr = xr._ffi.new('unsigned int [1]')
        st = xr._lib.XRFdc_GetIntrStatus(self.soc.rf._instance, xr._lib.XRFDC_ADC_TILE, int(tile), int(block), arr)
        if st != 0:
            raise RuntimeError('XRFdc_GetIntrStatus(%d, %d): status %d' % (tile, block, st))
        word = int(arr[0])
        if clear:
            st = xr._lib.XRFdc_IntrClr(self.soc.rf._instance, xr._lib.XRFDC_ADC_TILE, int(tile), int(block),
                                       0xFFFFFFFF)
            if st != 0:
                raise RuntimeError('XRFdc_IntrClr(%d, %d): status %d' % (tile, block, st))
        return word

    def _flags_all(self, clear=False):
        out = {}
        for name, t, b in self.adc_blocks():
            try:
                e = decode_flags(self._flags_word(t, b, clear))
            except Exception as ex:      # noqa: BLE001
                e = {'error': _exc(ex)}
            e['tile'], e['block_index'] = t, b
            out[name] = e
        return out

    def _adc_block(self, name):
        for n, t, b in self.adc_blocks():
            if n == name:
                return self.soc.rf.adc_tiles[t].blocks[b]
        raise KeyError(name)

    def _dsa_get(self, name):
        rf = self.soc.rf
        if callable(getattr(rf, 'get_adc_attenuator', None)):
            return float(rf.get_adc_attenuator(name))
        return float(self._adc_block(name).DSA['Attenuation'])

    def _dsa_set(self, name, att):
        rf = self.soc.rf
        if callable(getattr(rf, 'set_adc_attenuator', None)):
            rf.set_adc_attenuator(name, float(att))
        else:
            self._adc_block(name).DSA['Attenuation'] = float(round(float(att)))

    def _gpio_reg(self, p, dev=0):
        with self._sel(p['board_id']):
            return int(p['card'].switch_control.devs[dev].read_reg('GPIO_REG'))

    def _net(self, p, attr):
        net = getattr(p['chain'], attr, None) if p['chain'] is not None else None
        if net is None:
            return None, None, None
        dev, bit = p['card'].switch_control.net2port[net]
        return net, dev, bit

    def _net_read(self, p, attr, reg_cache=None):
        """(pin value 0/1, raw GPIO_REG) of the chain's net `attr` (powerup / powerdown)."""
        net, dev, bit = self._net(p, attr)
        if net is None:
            return None, None
        key = (p['board_id'], dev)
        if reg_cache is not None and key in reg_cache:
            reg = reg_cache[key]
        else:
            reg = self._gpio_reg(p, dev)
            if reg_cache is not None:
                reg_cache[key] = reg
        return (reg >> bit) & 1, reg

    def _net_write(self, p, attr, val):
        net, dev, bit = self._net(p, attr)
        with self._sel(p['board_id']):
            p['card'].switch_control[net] = int(val)

    def _amp_read(self, p, reg_cache=None):
        """RF In: rail enable pin; DC In: not power-down. True/False, from the expander."""
        if p['kind'] == 'rf_in':
            v, reg = self._net_read(p, 'powerup', reg_cache)
            return (None if v is None else bool(v)), reg
        if p['kind'] in ('dc_in', 'dc_out'):
            v, reg = self._net_read(p, 'powerdown', reg_cache)
            return (None if v is None else not bool(v)), reg
        return None, None

    def _amp_write(self, p, on):
        if p['kind'] == 'rf_in':
            self._net_write(p, 'powerup', 1 if on else 0)
        else:
            self._net_write(p, 'powerdown', 0 if on else 1)

    def _pair(self, p):
        """Ports on the same RF Out card whose amplifiers hang on the same 5 V rail as p (p excluded)."""
        net = getattr(p['chain'], 'powerup', None)
        return sorted(q['port'] for q in self._ports().values()
                      if q['side'] == 'dac' and q['board_id'] == p['board_id'] and q['port'] != p['port']
                      and getattr(q['chain'], 'powerup', None) == net)

    def _filter_read(self, p):
        with self._sel(p['board_id']):
            f = p['chain'].filter
            return {'WR0_SW': int(f.read_reg('WR0_SW')), 'WR0_FILTER': int(f.read_reg('WR0_FILTER'))}

    def _filter_write(self, p, regs):
        with self._sel(p['board_id']):
            f = p['chain'].filter
            f.write_reg('WR0_SW', int(regs['WR0_SW']))
            f.write_reg('WR0_FILTER', int(regs['WR0_FILTER']))

    def _gain_read(self, p):
        with self._sel(p['board_id']):
            return float(p['chain'].gain.get_gain())

    def _raw_card_id(self, side, slot):
        """The ID of a slot the library left empty, read as the library's detection reads it."""
        spi = getattr(self.soc, 'filter_spi', None)
        if spi is None or not callable(getattr(spi, 'send_receive_m', None)):
            return None
        board_id = slot + (0 if side == 'dac' else 4)
        with self._sel(board_id):
            res = spi.send_receive_m(bytes([0x41, 0x09, 0]), 4 if side == 'dac' else 2, '')
        return int(res[2]) >> 4

    # ---- reading ------------------------------------------------------------------------------------------------
    def _try(self, fn, *a):
        try:
            return fn(*a)
        except Exception as e:      # noqa: BLE001
            return _val(None, 'error', error=_exc(e))

    def _written_rec(self, key):
        r = self._written.get(key)
        return dict(r) if r is not None else _val(None, 'unknown', why='not written by this driver since the soc '
                                                                        'was constructed')

    def _filter_state(self, p):
        try:
            regs = self._filter_read(p)
        except Exception as e:      # noqa: BLE001
            return _val(None, 'error', error=_exc(e))
        dec = decode_filter(regs)
        out = _val(dec, 'read', raw=regs)
        if dec is None:
            out['why'] = 'the registers do not decode to a setting set_filter_raw can write'
        last = self._written.get(('filter', p['side'], p['port']))
        if last is not None:
            out['last_request'] = last['value']
        return out

    def _layout_locked(self, read_ids=True, reg_cache=None):
        ports = self._ports()
        gens, ros = self.gen_map(), self.ro_map()
        out = {'board': self._cfg('board'), 'adc': [], 'dac': [], 'gens': {}, 'ros': {}, 'adc_blocks': [],
               'warnings': [],
               'serial': 'the cards carry no serial number or EEPROM the library can read: the 4-bit ID '
                         '(GPIO pins 4-7) is all the identity a card has'}
        for side, n, off in (('dac', 4, 0), ('adc', 2, 4)):
            for slot, card in enumerate(self._cards(side)):
                e = {'slot': slot, 'board_id': off + slot, 'kind': None, 'name': None, 'library_class': None,
                     'ports': [], 'chains': []}
                if card is not None:
                    e['name'] = getattr(card, 'NAME', None)
                    e['kind'] = KINDS.get(e['name'], (e['name'] or '').lower().replace(' ', '_'))
                    e['library_class'] = type(card).__name__
                    for p in sorted((q for q in ports.values() if q['side'] == side and q['slot'] == slot),
                                    key=lambda q: q['port']):
                        c = {'port': p['port'], 'card_ch': p['card_ch'],
                             'gens': [i for i, g in sorted(gens.items()) if g['port'] == p['port']] if side == 'dac'
                             else [],
                             'ros': [i for i, r in sorted(ros.items()) if r['port'] == p['port']] if side == 'adc'
                             else []}
                        if side == 'adc':
                            c['adc_block'] = adc_block_of_port(p['port'])
                        if p['kind'] in ('rf_out', 'rf_in'):
                            c['supply_net'] = getattr(p['chain'], 'powerup', None)
                        if p['kind'] == 'rf_out':
                            c['supply_shared_with'] = self._pair(p)
                        e['ports'].append(p['port'])
                        e['chains'].append(c)
                if read_ids:
                    try:
                        if card is not None and getattr(card, 'switch_control', None) is not None \
                                and card.switch_control.devs:
                            key = (off + slot, 0)
                            if reg_cache is not None and key in reg_cache:
                                reg = reg_cache[key]
                            else:
                                with self._sel(off + slot):
                                    reg = int(card.switch_control.devs[0].read_reg('GPIO_REG'))
                                if reg_cache is not None:
                                    reg_cache[key] = reg
                            cid = reg >> 4
                        else:
                            cid = self._raw_card_id(side, slot)
                        if cid is None:
                            e['id'] = _val(None, 'unknown', why='this library version gives no way to read it')
                        else:
                            e['id'] = _val(cid, 'read')
                            e['id_kind'] = CARD_IDS[side].get(cid)
                            if card is not None and e['id_kind'] != e['kind']:
                                out['warnings'].append('%s slot %d: the library built a %s card, the ID now reads %d '
                                                       '(%s)' % (side.upper(), slot, e['name'], cid,
                                                                 e['id_kind'] or 'unknown'))
                            if card is None and e['id_kind'] is not None:
                                out['warnings'].append('%s slot %d: ID %d (%s) but the library built no driver for '
                                                       'it (this library version does not know the card)'
                                                       % (side.upper(), slot, cid, NAMES[e['id_kind']]))
                    except Exception as ex:      # noqa: BLE001
                        e['id'] = _val(None, 'error', error=_exc(ex))
                out[side].append(e)
        out['gens'] = dict((str(k), v) for k, v in gens.items())
        out['ros'] = dict((str(k), v) for k, v in ros.items())
        for k, r in sorted(ros.items()):
            if r.get('warning'):
                out['warnings'].append('readout %d: %s' % (k, r['warning']))
            if r['index'] != [r['tile'], r['block']]:
                out['warnings'].append('readout %d: ADC %s has converter index %s; the port follows the name, the '
                                       'converter calls use the index' % (k, r['adc'], r['index']))
        for name, t, b in self.adc_blocks():
            out['adc_blocks'].append({'name': name, 'index': [t, b], 'port': adc_port_of_block(name)})
        return out

    def layout(self, read_ids=True):
        """The cards per slot (kind, library class, ID read from the card now, ports, chains with the generators,
        readouts, ADC block and supply rail of each), the generator and readout maps, the firmware's ADC blocks."""
        with self.lock:
            return self._layout_locked(read_ids=read_ids)

    def _get_locked(self, flags=True):
        cache = {}
        st = {'rfbox': API_VERSION, 'identity': dict(self.identity), 'time': time.time(), 'library': self.library,
              'layout': self._layout_locked(read_ids=True, reg_cache=cache), 'adc_ports': {}, 'dac_ports': {},
              'adc_att': {}, 'bias': {}, 'limits': self.limits, 'readback': READBACK}
        for (side, port), p in sorted(self._ports().items()):
            d = {'kind': p['kind'], 'slot': p['slot'], 'card_ch': p['card_ch']}
            try:
                if side == 'adc':
                    d['block'] = adc_block_of_port(port)
                    if p['kind'] in ('rf_in', 'dc_in'):
                        v, reg = self._amp_read(p, cache)
                        d['amp'] = _val(v, 'read', raw=reg,
                                        note='the expander pin (commanded state), not the rail')
                    if p['kind'] == 'rf_in':
                        d['att'] = self._written_rec(('ro_att', port))
                        d['filter'] = self._filter_state(p)
                    if p['kind'] == 'dc_in':
                        d['gain'] = self._try(lambda: _val(self._gain_read(p), 'read'))
                else:
                    if p['kind'] == 'rf_out':
                        d['att'] = self._written_rec(('dac_att', port))
                        v, reg = self._net_read(p, 'powerup', cache)
                        d['supply'] = _val(None if v is None else bool(v), 'read', raw=reg,
                                           net=getattr(p['chain'], 'powerup', None), shared_with=self._pair(p),
                                           note='the expander pin (commanded state), not the rail')
                        d['filter'] = self._filter_state(p)
                    if p['kind'] == 'dc_out':
                        v, reg = self._amp_read(p, cache)
                        d['powered'] = _val(v, 'read', raw=reg)
            except Exception as e:      # noqa: BLE001
                d['error'] = _exc(e)
            st['%s_ports' % side][str(port)] = d
        for name, t, b in self.adc_blocks():
            st['adc_att'][name] = self._try(lambda: _val(self._dsa_get(name), 'read'))
        biases = getattr(self.soc, 'biases', None) or []
        for i in range(8):
            if i < len(biases):
                st['bias'][str(i)] = self._try(lambda: _val(float(biases[i].get_volt()), 'read'))
            else:
                st['bias'][str(i)] = _val(None, 'unknown', why='no bias DAC object on this soc')
        st['bias_enabled'] = self._written_rec('bias_enabled')
        if flags:
            st['flags'] = self._flags_all(clear=False)
        return st

    def get(self, flags=True):
        """The whole state as plain data: identity, layout, every port's settings with their source, the RF-ADC
        attenuators, the bias setpoints and (flags=True) every ADC's interrupt word. Flags are read, never cleared."""
        with self.lock:
            return self._get_locked(flags=flags)

    # ---- planning -------------------------------------------------------------------------------------------------
    def _known(self, key):
        r = self._written.get(key)
        return r['value'] if r is not None and r['source'] in ('written', 'assumed') else None

    def _plan(self, items):
        gens, ros = self.gen_map(), self.ro_map()
        steps, seen = [], {}
        dsa_now = {}
        att_in_request = set(it['ro'] for it in items if 'ro' in it and 'att' in it)

        def once(key, w):
            if key in seen:
                raise _bad(w, 'sets %s, which %s already sets' % (key[0], seen[key]))
            seen[key] = w

        def step(phase, op, target, value, what, **extra):
            s = {'phase': phase, 'op': op, 'target': target, 'value': value, 'what': what}
            s.update(extra)
            steps.append(s)

        def ro_info(r, w):
            if r not in ros:
                raise _bad(w + '.ro', 'the firmware has no readout %d (it has %d)' % (r, len(ros)))
            return ros[r]

        def amp_now(p):
            try:
                return self._amp_read(p)[0]
            except Exception:      # noqa: BLE001
                return None

        for i, it in enumerate(items):
            w = 'settings[%d]' % i
            if 'adc_att' in it:
                name = it['adc'] if 'adc' in it else ro_info(it['ro'], w)['adc']
                if name not in [b[0] for b in self.adc_blocks()]:
                    raise _bad(w + '.adc', 'the firmware has no RF-ADC block %s' % name)
                once(('adc_att', name), w)
                if name not in dsa_now:
                    try:
                        dsa_now[name] = self._dsa_get(name)
                    except Exception:      # noqa: BLE001
                        dsa_now[name] = None
                cur, new = dsa_now[name], it['adc_att']
                # unknown current: first (never worse than the final state)
                step('reduce' if cur is None or new >= cur else 'increase', 'adc_att', name, new,
                     'RF-ADC %s attenuator %s -> %g dB' % (name, 'unknown' if cur is None else '%g' % cur, new))
            if 'gen' in it:
                g = it['gen']
                if g not in gens:
                    raise _bad(w + '.gen', 'the firmware has no generator %d (it has %d)' % (g, len(gens)))
                c = gens[g]
                what = 'generator %d (DAC %s)' % (g, c['dac'])
                if 'dc' in it:
                    p = self._need('dac', c['port'], ('dc_out',), what, w + '.gen')
                    once(('dc', p['port']), w)
                    step('increase' if it['dc'] else 'reduce', 'dac_dc', p['port'], it['dc'],
                         '%s DC Out %s' % (what, 'powered up' if it['dc'] else 'powered down'))
                    continue
                p = self._need('dac', c['port'], ('rf_out',), what, w + '.gen')
                if 'filter' in it:
                    once(('filter', 'dac', p['port']), w)
                    step('filters', 'filter', ('dac', p['port']), it['filter'], '%s filter' % what)
                # att without supply: the rail goes on in the same step, after the attenuators (as
                # DacRfChain216.enable_rf does, in the safer order)
                implicit = 'att' in it and 'supply' not in it
                supply = it.get('supply')
                if supply is not None or implicit:
                    once(('supply', p['port']), w)
                if 'att' in it:
                    once(('dac_att', p['port']), w)
                    old = self._known(('dac_att', p['port']))
                    up = old is None or (it['att'][0] >= old[0] and it['att'][1] >= old[1])
                    step('reduce' if up else 'increase', 'dac_att', p['port'], it['att'],
                         '%s attenuators %s -> %s dB%s' % (what, old if old is not None else 'unknown', it['att'],
                                                           ', then its 5 V rail on' if implicit else ''),
                         supply=implicit)
                if supply is not None:
                    pair = self._pair(p)
                    step('increase' if supply else 'reduce', 'dac_supply', p['port'], supply,
                         '%s 5 V rail %s (shared with DAC port%s %s)' % (what, 'on' if supply else 'off',
                                                                         's' if len(pair) != 1 else '',
                                                                         ', '.join(map(str, pair)) or 'none'))
            if 'ro' in it and set(it) - {'ro', 'adc_att'}:
                r = it['ro']
                c = ro_info(r, w)
                what = 'readout %d (ADC %s)' % (r, c['adc'])
                if 'filter' in it:
                    p = self._need('adc', c['port'], ('rf_in',), what, w + '.ro')
                    once(('filter', 'adc', p['port']), w)
                    step('filters', 'filter', ('adc', p['port']), it['filter'], '%s filter' % what)
                amp = it.get('amp')
                if amp is not None or 'att' in it or 'gain' in it:
                    kinds = ('rf_in',) if 'att' in it else ('dc_in',) if 'gain' in it else ('rf_in', 'dc_in')
                    p = self._need('adc', c['port'], kinds, what, w + '.ro')
                    port = p['port']
                    if amp is not None:
                        once(('amp', port), w)
                    if 'att' in it:
                        once(('ro_att', port), w)
                    if 'gain' in it:
                        once(('gain', port), w)
                    on = amp_now(p)
                    if amp is False:
                        step('reduce', 'adc_off', port, None, '%s amplifier off' % what)
                        if 'att' in it:
                            step('increase' if it['att'] < ATT_MAX else 'reduce', 'ro_att', port, it['att'],
                                 '%s RF In attenuator %g -> %g dB' % (what, ATT_MAX, it['att']))
                    elif amp is True and 'att' in it:
                        old = self._known(('ro_att', port))
                        up = on is True and old is not None and it['att'] >= old
                        step('reduce' if up else 'increase', 'adc_rf_on', port, it['att'],
                             '%s RF In amplifier on, attenuator %s -> %g dB' % (what, old, it['att']))
                    elif amp is True and 'gain' in it:
                        try:
                            old = self._gain_read(p)
                        except Exception:      # noqa: BLE001
                            old = None
                        up = on is True and old is not None and it['gain'] <= old
                        step('reduce' if up else 'increase', 'adc_dc_on', port, it['gain'],
                             '%s DC In powered up, gain %s -> %g dB' % (what, old, it['gain']))
                    elif amp is True:
                        if p['kind'] == 'rf_in' and self._written.get(('ro_att', port), {}).get('source') != 'written' \
                                and r not in att_in_request:
                            raise RfboxError('unknown_state', '%s: the RF In attenuator of ADC port %d has not been '
                                                              'written by this driver since the soc was constructed '
                                                              '(it is write-only: %s); give att with amp: true'
                                             % (what, port, self._written_rec(('ro_att', port)).get('why')),
                                             field=w + '.amp')
                        step('increase', 'amp_on', port, True, '%s amplifier on' % what)
                    elif 'att' in it:
                        old = self._known(('ro_att', port))
                        up = old is None or it['att'] >= old
                        step('reduce' if up else 'increase', 'ro_att', port, it['att'],
                             '%s RF In attenuator %s -> %g dB' % (what, old, it['att']))
                    elif 'gain' in it:
                        try:
                            old = self._gain_read(p)
                        except Exception:      # noqa: BLE001
                            old = None
                        up = old is None or it['gain'] <= old
                        step('reduce' if up else 'increase', 'dc_gain', port, it['gain'],
                             '%s DC In gain %s -> %g dB' % (what, old, it['gain']))
            if 'bias' in it:
                once(('bias', it['bias']), w)
                biases = getattr(self.soc, 'biases', None) or []
                if it['bias'] >= len(biases):
                    raise RfboxError('no_rf_board', 'bias %d: this soc has %d bias outputs' % (it['bias'], len(biases)),
                                     field=w + '.bias')
                step('bias', 'bias', it['bias'], it['volts'], 'bias %d -> %g V' % (it['bias'], it['volts']))
        # reductions (attenuation added, amplifiers off, gain removed), filters, increases (attenuation removed,
        # amplifiers on; RF-ADC attenuator decreases last), bias
        phase_rank = {'reduce': 0, 'filters': 1, 'increase': 2, 'bias': 3}
        red_rank = {'adc_att': 0, 'ro_att': 1, 'adc_off': 2, 'dc_gain': 3, 'adc_dc_on': 3, 'dac_supply': 4,
                    'dac_dc': 4, 'dac_att': 4, 'adc_rf_on': 5, 'amp_on': 5}
        inc_rank = {'dac_att': 0, 'dac_dc': 0, 'dac_supply': 1, 'adc_rf_on': 2, 'amp_on': 2, 'adc_dc_on': 3,
                    'dc_gain': 4, 'ro_att': 5, 'adc_att': 6}
        keyed = []
        for n, s in enumerate(steps):
            if s['phase'] == 'filters':
                rank = 0 if s['target'][0] == 'adc' else 1
            elif s['phase'] == 'increase':
                rank = inc_rank.get(s['op'], 9)
            else:
                rank = red_rank.get(s['op'], 9)
            keyed.append(((phase_rank[s['phase']], rank, n), s))
        return [s for k, s in sorted(keyed, key=lambda x: x[0])]

    # ---- one step ---------------------------------------------------------------------------------------------
    def _wr(self, key, value, by):
        self._written[key] = _val(value, 'written', by=by, at=time.time())

    def _do(self, s, by):
        """Apply one step and read back what can be read. Returns (result, extra) where extra holds requested /
        applied / affects / verified for the record. Raises on failure or a readback that disagrees."""
        op, t, v = s['op'], s['target'], s['value']
        ports = self._ports()
        extra = {}
        if op == 'adc_att':
            self._dsa_set(t, v)
            got = self._dsa_get(t)
            extra.update(requested=v, applied=float(round(v)), verified=abs(got - v) <= 0.5)
            if not extra['verified']:
                raise _VerifyError('RF-ADC %s attenuator reads %g dB after setting %g dB' % (t, got, v))
            return {'set': float(round(v)), 'read': got}, extra
        if op == 'filter':
            side, port = t
            p = ports[(side, port)]
            h, l = filter_steps_for(v)
            regs = filter_regs(h, l)
            self._filter_write(p, regs)
            self._wr(('filter', side, port), dict(v), by)
            got = self._filter_read(p)
            dec = decode_filter(got)
            setting = dict(dec or {})
            setting['source'] = 'decoded from the registers' if dec else 'registers not decodable'
            setting['last_request'] = dict(v)
            extra.update(requested=dict(v), applied=decode_filter(regs), verified=got == regs)
            if got != regs:
                raise _VerifyError('filter of %s port %d reads %s after writing %s' % (side.upper(), port, got, regs))
            return {'read': got, 'setting': setting, 'steps_requested': [h, l]}, extra
        if op == 'dac_dc':
            p = ports[('dac', t)]
            self._amp_write(p, v)
            got = self._amp_read(p)[0]
            extra.update(requested=v, applied=v, verified=got is v)
            if got is not v:
                raise _VerifyError('DC Out port %d reads powered=%s after setting %s' % (t, got, v))
            return {'powered': got}, extra
        if op == 'dac_att':
            p = ports[('dac', t)]
            with self._sel(p['board_id']):
                a1 = float(p['chain'].attn[0].set_att(v[0]))
                a2 = float(p['chain'].attn[1].set_att(v[1]))
            self._wr(('dac_att', t), [a1, a2], by)
            extra.update(requested=list(v), applied=[a1, a2], verified=None,
                         note='PE43705 is write-only: not read back')
            res = {'set': [a1, a2]}
            if s.get('supply'):
                res['supply_on'] = self._rail(p, True, extra)
            return res, extra
        if op == 'dac_supply':
            p = ports[('dac', t)]
            extra.update(requested=v, applied=v)
            return {'supply_on': self._rail(p, v, extra)}, extra
        if op in ('adc_off', 'adc_rf_on', 'amp_on', 'ro_att'):
            p = ports[('adc', t)]
            if op == 'adc_off':
                self._amp_write(p, False)
                if p['kind'] == 'rf_in':
                    with self._sel(p['board_id']):
                        p['chain'].attn[0].set_att(ATT_MAX)
                    self._wr(('ro_att', t), ATT_MAX, 'amplifier off')
                got = self._amp_read(p)[0]
                extra.update(requested=False, applied=False, verified=got is False)
                if got is not False:
                    raise _VerifyError('ADC port %d amplifier reads on after switching it off' % t)
                return {'amp_on': got}, extra
            if op in ('adc_rf_on', 'ro_att'):
                with self._sel(p['board_id']):
                    a = float(p['chain'].attn[0].set_att(v))
                self._wr(('ro_att', t), a, by)
                if op == 'ro_att':
                    extra.update(requested=v, applied=a, verified=None, note='PE43705 is write-only')
                    return {'set': a}, extra
            self._amp_write(p, True)
            got = self._amp_read(p)[0]
            extra.update(requested=v, applied=v if op == 'amp_on' else a, verified=got is True)
            if got is not True:
                raise _VerifyError('ADC port %d amplifier reads off after switching it on' % t)
            return ({'set': a, 'amp_on': got} if op == 'adc_rf_on' else {'amp_on': got}), extra
        if op in ('adc_dc_on', 'dc_gain'):
            p = ports[('adc', t)]
            with self._sel(p['board_id']):
                g = float(p['chain'].gain.set_gain(v))
            got = self._gain_read(p)
            res = {'set': g, 'gain': got}
            ok = abs(got - g) < 0.5
            if op == 'adc_dc_on':
                self._amp_write(p, True)
                on = self._amp_read(p)[0]
                res['amp_on'] = on
                ok = ok and on is True
            extra.update(requested=v, applied=g, verified=ok)
            if not ok:
                raise _VerifyError('DC In port %d reads gain %s, powered %s after setting %g dB'
                                   % (t, got, res.get('amp_on'), g))
            return res, extra
        if op == 'bias':
            b = self.soc.biases[t]
            got = float(b.set_volt(v))
            read = float(b.get_volt())
            extra.update(requested=v, applied=got, verified=abs(read - got) < 1e-4)
            if not extra['verified']:
                raise _VerifyError('bias %d reads %g V after setting %g V' % (t, read, got))
            return {'set': got, 'read': read}, extra
        raise ValueError(op)

    def _rail(self, p, on, extra):
        """Switch an RF Out channel's 5 V rail; the channels sharing it go into extra['affects']."""
        t = p['port']
        before = self._net_read(p, 'powerup')[0]
        self._net_write(p, 'powerup', 1 if on else 0)
        got = self._net_read(p, 'powerup')[0]
        extra['affects'] = [{'dac_port': q, 'supply_on': None if got is None else bool(got),
                             'was': None if before is None else bool(before),
                             'why': 'shares the 5 V rail %s with DAC port %d' % (p['chain'].powerup, t)}
                            for q in self._pair(p)]
        extra['verified'] = got is not None and bool(got) is bool(on)
        if not extra['verified']:
            raise _VerifyError('RF Out port %d rail pin reads %s after setting %s' % (t, got, on))
        return bool(got)

    def _run(self, rec, s, by):
        """One step into rec['order']; on failure the state is re-read into the record and Refused raised."""
        target = list(s['target']) if isinstance(s['target'], tuple) else s['target']
        entry = {'phase': s['phase'], 'op': s['op'], 'target': target, 'value': s['value'], 'what': s['what']}
        rec['order'].append(entry)
        try:
            entry['result'], extra = self._do(s, by)
            entry.update(extra)
            return entry
        except Exception as e:      # noqa: BLE001
            entry['error'] = _exc(e)
            rec['ok'] = False
            rec['stopped_at'] = s['what']
            rec.setdefault('problems', []).append('%s failed: %s' % (s['what'], entry['error']))
            rec['state'] = self._try(self._get_locked)
            code = 'readback' if isinstance(e, _VerifyError) else 'step_failed'
            raise Refused('%s failed: %s (%d step(s) before it were applied; the state after the failure is in '
                          'the record)' % (s['what'], entry['error'], len(rec['order']) - 1), rec, code)

    # ---- applying a settings list -------------------------------------------------------------------------------
    def _idle_flags(self, settle_s, protect=True):
        out = {'flags_before_clear': self._flags_all(clear=True)}
        if settle_s:
            self.sleep(settle_s)
        idle = self._flags_all(clear=True)
        out['flags_idle'] = idle
        out['flags_after_change'] = flag_list(idle)
        out['flagged'] = [f for f in out['flags_after_change']
                          if f.get('over_range') or f.get('over_voltage') or 'error' in f]
        out['settle_s'] = settle_s
        problems_, actions = [], []
        amps = dict((adc_block_of_port(port), p) for (side, port), p in self._ports().items()
                    if side == 'adc' and p['kind'] in ('rf_in', 'dc_in'))
        for name, f in sorted(idle.items()):
            if 'error' in f:
                problems_.append('ADC %s flags could not be read: %s' % (name, f['error']))
                continue
            if f['over_voltage']:
                problems_.append('ADC %s in OVER-VOLTAGE at idle (0x%08x)' % (name, f['word']))
                if protect and name in amps:
                    p = amps[name]
                    try:
                        self._amp_write(p, False)
                        if p['kind'] == 'rf_in':
                            with self._sel(p['board_id']):
                                p['chain'].attn[0].set_att(ATT_MAX)
                            self._wr(('ro_att', p['port']), ATT_MAX, 'amplifier off')
                        actions.append({'op': 'adc_off', 'port': p['port'], 'why': 'over-voltage at idle',
                                        'amp_on': self._amp_read(p)[0]})
                    except Exception as e:      # noqa: BLE001
                        actions.append({'op': 'adc_off', 'port': p['port'], 'error': _exc(e)})
            elif f['over_range']:
                problems_.append('ADC %s over range at idle (0x%08x)' % (name, f['word']))
        out['problems'] = problems_
        out['actions'] = actions
        out['ok'] = not problems_
        return out

    def _apply_items(self, items, settle_s=SETTLE_DEFAULT_S, protect=True, by='rfbox set', check=True):
        """Plan and apply normalized items under the lock. Returns the record (ok False when a flag is set at idle);
        raises RfboxError before anything is touched, Refused if a step fails (record with the state re-read)."""
        with self.lock:
            if check and self.limits:
                check_limits(items, self.limits, self.ro_map())
            steps = self._plan(items)
            rec = {'order': [], 'actions': [], 'problems': [], 'by': by, 'identity': self.identity['id'],
                   'flags_latched_before': flag_list(self._flags_all(clear=True))}
            for s in steps:
                self._run(rec, s, by)
            rec.update(self._idle_flags(settle_s, protect))
            return rec

    def set(self, settings, settle_s=SETTLE_DEFAULT_S, protect=True, by='rfbox set'):
        """Validate every field (check_items, then the board, then the limits table) before anything is touched;
        apply in the safe order with readback of everything readable; clear the ADC flags, wait settle_s, read
        them. Returns the record; raises RfboxError (nothing touched) or Refused (a step failed or disagreed with
        its readback: record['state'] is the state re-read; or a flag at idle: an over-voltage ADC has had its
        amplifier switched off, record['actions'])."""
        items = check_items(settings)
        rec = self._apply_items(items, check_settle(settle_s), protect, by)
        if not rec['ok']:
            raise Refused('; '.join(rec['problems']), rec, 'adc_flags')
        return rec

    def amp(self, ro, on, att=None, settle_s=SETTLE_DEFAULT_S):
        """A readout's amplifier on or off (with its RF In attenuator if att is given)."""
        it = {'ro': ro, 'amp': bool(on)}
        if att is not None:
            it['att'] = att
        return self.set([it], settle_s=settle_s)

    def ro_off(self, ro):
        """qick_rf.ro_off: the readout's amplifier off and (RF In) its attenuator to 31.75 dB."""
        with self.lock:
            c = self.ro_map().get(int(ro))
            if c is None:
                raise _bad('ro', 'the firmware has no readout %r' % (ro,))
            p = self._need('adc', c['port'], ('rf_in', 'dc_in'), 'readout %d' % ro)
            rec = {'order': []}
            self._run(rec, {'phase': 'reduce', 'op': 'adc_off', 'target': p['port'], 'value': None,
                            'what': 'readout %d amplifier off' % ro}, 'ro_off')
            return True

    # ---- limits ---------------------------------------------------------------------------------------------------
    def set_limits(self, table):
        """Install (or with None remove) the limits table every later set / apply is checked against."""
        self.limits = check_limits_table(table)
        return self.limits

    # ---- flags ----------------------------------------------------------------------------------------------------
    def flags(self, clear=False):
        with self.lock:
            return self._flags_all(clear=clear)

    def clear_flags(self, settle_s=0.0):
        """Read and clear every ADC's flags; after settle_s read them again (not cleared)."""
        with self.lock:
            before = self._flags_all(clear=True)
            if settle_s:
                self.sleep(settle_s)
            after = self._flags_all(clear=False)
            return {'flags_before': flag_list(before), 'flags_after': flag_list(after), 'settle_s': settle_s}

    # ---- safe state ---------------------------------------------------------------------------------------------
    def default_safe_items(self):
        """Every RF-ADC attenuator at 27 dB and every RF Out attenuator at 31.75 dB (rails left as they are); the
        amplifiers are switched off by apply_safe itself. Bias outputs are never part of it."""
        items = [{'adc': name, 'adc_att': float(DSA_MAX)} for name, t, b in self.adc_blocks()]
        for (side, port), p in sorted(self._ports().items()):
            if p['kind'] == 'rf_out':
                for g, c in sorted(self.gen_map().items()):
                    if c['port'] == port:
                        items.append({'gen': g, 'att': [ATT_MAX, ATT_MAX], 'supply': None})
                        break
                else:
                    items.append({'dac_port': port, 'att': [ATT_MAX, ATT_MAX]})
        return items

    def before_load(self):
        """Every readout amplifier (RF In rail, DC In power-down) off, read back. Raises Refused (rf_safe_failed)
        with the record if one cannot be confirmed off."""
        with self.lock:
            rec = {'done': [], 'ok': True}
            for (side, port), p in sorted(self._ports().items()):
                if side != 'adc' or p['kind'] not in ('rf_in', 'dc_in'):
                    continue
                e = {'port': port, 'kind': p['kind']}
                try:
                    self._amp_write(p, False)
                    if p['kind'] == 'rf_in':
                        with self._sel(p['board_id']):
                            p['chain'].attn[0].set_att(ATT_MAX)
                        self._wr(('ro_att', port), ATT_MAX, 'amplifier off')
                    e['amp_on'] = self._amp_read(p)[0]
                except Exception as ex:      # noqa: BLE001
                    e['error'] = _exc(ex)
                if e.get('amp_on') is not False:
                    rec['ok'] = False
                rec['done'].append(e)
            rec['flags'] = self._flags_all(clear=False)
            rec['flags_list'] = flag_list(rec['flags'])
            if not rec['ok']:
                bad = [e for e in rec['done'] if e.get('amp_on') is not False]
                raise Refused('could not confirm the readout amplifiers off: %s' % bad, rec, 'rf_safe_failed')
            return rec

    def _safe(self, items, settle_s, by):
        file_items = list(items or [])
        rm = self.ro_map()
        by_block = {}
        for it in file_items:
            if 'adc_att' in it:
                name = it['adc'] if 'adc' in it else (rm.get(it.get('ro')) or {}).get('adc')
                by_block[name] = it['adc_att']
        gens_in_file = set(it['gen'] for it in file_items if 'gen' in it and 'att' in it)
        defaults = [it for it in self.default_safe_items()
                    if not ('adc' in it and it['adc'] in by_block) and not ('gen' in it and it['gen'] in gens_in_file)]
        on_ports = set(rm[it['ro']]['port'] for it in file_items if it.get('amp') is True and it.get('ro') in rm)
        rec = {'source': 'safe-state file' if file_items else 'built-in default', 'amplifiers_off': [], 'ok': True,
               'bias': 'not touched (never stepped implicitly)'}
        # the RF-ADC attenuators first: they protect the ADC from an amplifier still on from before
        dsa_items = [it for it in defaults if 'adc' in it]
        rest = [it for it in defaults if 'adc' not in it]
        res0 = None
        if dsa_items:
            res0 = self._apply_items(dsa_items, settle_s=0, protect=False, by=by, check=False)
        for (side, port), p in sorted(self._ports().items()):
            if side != 'adc' or p['kind'] not in ('rf_in', 'dc_in') or port in on_ports:
                continue
            e = {'port': port}
            try:
                self._amp_write(p, False)
                if p['kind'] == 'rf_in':
                    with self._sel(p['board_id']):
                        p['chain'].attn[0].set_att(ATT_MAX)
                    self._wr(('ro_att', port), ATT_MAX, 'amplifier off')
                e['amp_on'] = self._amp_read(p)[0]
            except Exception as ex:      # noqa: BLE001
                e['error'] = _exc(ex)
            if e.get('amp_on') is not False:
                rec['ok'] = False
            rec['amplifiers_off'].append(e)
        for it in rest:
            if 'dac_port' in it:
                p = self._ports()[('dac', it['dac_port'])]
                with self._sel(p['board_id']):
                    p['chain'].attn[0].set_att(ATT_MAX)
                    p['chain'].attn[1].set_att(ATT_MAX)
                self._wr(('dac_att', it['dac_port']), [ATT_MAX, ATT_MAX], by)
        rest = [it for it in rest if 'dac_port' not in it]
        res = self._apply_items(rest + file_items, settle_s=settle_s, protect=True, by=by, check=False)
        if res0 is not None:
            res['order'] = res0['order'] + res['order']
            res['flags_latched_before'] = res0['flags_latched_before']
        rec.update(res)
        rec['ok'] = rec['ok'] and res.get('ok', False)
        rec['adc_att'] = {}
        for name, t, b in self.adc_blocks():
            try:
                rec['adc_att'][name] = self._dsa_get(name)
            except Exception as ex:      # noqa: BLE001
                rec['adc_att'][name] = {'error': _exc(ex)}
        want = dict((it['adc'], it['adc_att']) for it in dsa_items)
        want.update(by_block)
        for name, v in want.items():
            got = rec['adc_att'].get(name)
            if not isinstance(got, Real) or abs(got - v) > 0.5:
                rec['ok'] = False
                rec.setdefault('problems', []).append('RF-ADC %s attenuator reads %s after setting %g dB'
                                                      % (name, got, v))
        return rec

    def apply_safe(self, items=None, settle_s=SETTLE_DEFAULT_S, by='apply_safe'):
        """The safe state: every RF-ADC attenuator to 27 dB (read back) before anything else, every readout
        amplifier off (read back) unless `items` turns it on, every RF Out attenuator to 31.75 dB, then `items` (a
        settings list, e.g. an image's safe-state file) in the set order, then the idle flags. Bias outputs and RF
        Out rails are not touched. Returns the record; raises Refused (record attached) if anything is not
        confirmed."""
        its = check_items(items or [], allow_empty=True)
        with self.lock:
            if self.limits:
                check_limits(its, self.limits, self.ro_map())
            try:
                rec = self._safe(its, check_settle(settle_s), by)
            except Refused as e:
                e.code = 'rf_safe_failed'
                raise
        if not rec['ok']:
            raise Refused('safe state not confirmed: %s' % '; '.join(rec.get('problems') or ['see the record']),
                          rec, 'rf_safe_failed')
        return rec

    # ---- the description ----------------------------------------------------------------------------------------
    def problems(self, spec):
        """What stops a normalized description from being applied here (live layout)."""
        if not (spec['gens'] or spec['ros'] or spec['bias'] or spec['cards']):
            return []
        board = self._cfg('board')
        if board not in (None, 'ZCU216'):
            return ['rf: the daughter-card settings are for a ZCU216 QICK box; this board is a %s' % board]
        if not any(self._cards('adc')) and not any(self._cards('dac')) and getattr(self.soc, 'adc_cards', None) is None:
            return ['rf: this soc has no QICK box card drivers (the soc class is not RFQickSoc216V1)']
        out = problems_for(spec, self.gen_map(), self.ro_map(), self._kinds())
        for b in sorted(spec['bias']):
            it = {'bias': b, 'volts': spec['bias'][b]['volts']}
            try:
                check_limits([it], self.limits, {})
            except RfboxError as e:
                out.append('rf.bias.%d: %s' % (b, e))
        return out

    @staticmethod
    def _flat(st):
        out = {}
        for sec in ('adc_ports', 'dac_ports'):
            for port, d in (st.get(sec) or {}).items():
                for k, v in d.items():
                    if isinstance(v, dict) and 'source' in v:
                        out['%s.%s.%s' % (sec, port, k)] = v.get('raw') if k == 'filter' else v.get('value')
        for sec in ('adc_att', 'bias'):
            for k, v in (st.get(sec) or {}).items():
                out['%s.%s' % (sec, k)] = v.get('value') if isinstance(v, dict) else v
        return out

    def apply(self, description, others='leave', by='rfbox apply', log=None):
        """Reach the state a description asks for (normalize's format), in the safe order (qick_rf.apply):
          1. the layout is compared with `cards` and every configured channel must sit on a card of the right kind
             (Refused 'layout', nothing touched);
          2. others='safe': every readout amplifier not in the description off first (others='leave': untouched);
          3. generators: filter, then the two attenuators (and the channel's 5 V rail on; its pair is reported);
             DC Out generators powered up / down;
          4. per readout: the RF-ADC attenuator set and read back (Refused 'readback' if it disagrees: the amplifier
             is not touched), the filter, then the attenuator and the amplifier on (DC In: gain, powered up), then
             the idle ADC flags cleared and read twice IDLE_WAIT_S apart. Over-voltage at idle: the amplifier off
             again, Refused 'adc_flags'; over-range at idle: Refused 'adc_flags', the amplifier left on;
          5. bias outputs, read back.
        Returns the record: what was set and read back per channel, `order` (every step), `warnings`, `changed`
        (each value that differs between the state before and after) and `state`. Refused carries the same record
        with the state re-read."""
        spec = normalize(description)
        if others not in ('leave', 'safe'):
            raise _bad('others', "'leave' or 'safe'")
        with self.lock:
            bad = self.problems(spec)
            lay = self._layout_locked(read_ids=False)
            rec = {'hash': spec_hash(spec), 'identity': dict(self.identity), 'by': by,
                   'cards': layout_text(lay), 'gens': {}, 'ros': {}, 'bias': {}, 'warnings': [], 'order': []}
            if bad:
                rec['state'] = self._try(self._get_locked)
                raise Refused('; '.join(bad), rec, 'layout')
            before = self._get_locked(flags=False)
            gens, ros = self.gen_map(), self.ro_map()

            def refuse(msg, code):
                rec['state'] = self._try(self._get_locked)
                rec['changed'] = self._changed(before, rec['state'])
                raise Refused(msg, rec, code)

            def run(op, target, value, what):
                return self._run(rec, {'phase': 'apply', 'op': op, 'target': target, 'value': value, 'what': what},
                                 by)

            if others == 'safe':
                listed = set(ros[r]['port'] for r in spec['ros'])
                for (side, port), p in sorted(self._ports().items()):
                    if side == 'adc' and p['kind'] in ('rf_in', 'dc_in') and port not in listed:
                        run('adc_off', port, None, 'ADC port %d amplifier off (not in the description)' % port)
            for g in sorted(spec['gens']):
                s, c = spec['gens'][g], gens[g]
                what = 'generator %d (DAC %s)' % (g, c['dac'])
                if 'dc' in s:
                    e = run('dac_dc', c['port'], s['dc'], '%s DC Out %s' % (what, 'on' if s['dc'] else 'off'))
                    rec['gens'][str(g)] = {'dc': s['dc'], 'powered': e['result']['powered'], 'dac': c['dac'],
                                           'port': c['port']}
                    continue
                run('filter', ('dac', c['port']), s['filter'], '%s filter' % what)
                e = run('dac_att', c['port'], s['att'], '%s attenuators' % what)
                e2 = run('dac_supply', c['port'], True, '%s 5 V rail on' % what)
                rec['gens'][str(g)] = {'att': e['result']['set'], 'filter': dict(s['filter']),
                                       'filter_applied': rec['order'][-3].get('applied'), 'dac': c['dac'],
                                       'port': c['port'], 'supply_on': e2['result']['supply_on'],
                                       'supply_shared_with': [a['dac_port'] for a in e2.get('affects', [])]}
            for r in sorted(spec['ros']):
                s, c = spec['ros'][r], ros[r]
                what = 'readout %d (ADC %s)' % (r, c['adc'])
                one = {'adc': c['adc'], 'port': c['port'], 'adc_att': s['adc_att'], 'amp': False}
                rec['ros'][str(r)] = one
                try:
                    e = run('adc_att', c['adc'], s['adc_att'], '%s RF-ADC attenuator' % what)
                except Refused as ex:
                    one['adc_att_read'] = None
                    ex.args = ('readout %d: the ADC attenuator of block %s did not read back %g dB: the amplifier is '
                               'not switched on (%s)' % (r, c['adc'], s['adc_att'], ex),)
                    raise
                one['adc_att_read'] = e['result']['read']
                if 'gain' in s:
                    if s.get('amp', True):
                        e = run('adc_dc_on', c['port'], s['gain'], '%s DC In powered up, gain %g dB' % (what, s['gain']))
                        one.update(gain=e['result']['gain'], amp=True)
                    else:
                        run('adc_off', c['port'], None, '%s DC In powered down' % what)
                        e = run('dc_gain', c['port'], s['gain'], '%s DC In gain' % what)
                        one.update(gain=e['result']['gain'], amp=False)
                        continue
                else:
                    run('filter', ('adc', c['port']), s['filter'], '%s filter' % what)
                    one['filter'] = dict(s['filter'])
                    one['filter_applied'] = rec['order'][-1].get('applied')
                    if not s.get('amp', True):
                        run('adc_off', c['port'], None, '%s amplifier off' % what)
                        e = run('ro_att', c['port'], s['att'], '%s RF In attenuator' % what)
                        one['att'] = e['result']['set']
                        continue
                    e = run('adc_rf_on', c['port'], s['att'], '%s RF In attenuator, amplifier on' % what)
                    one.update(att=e['result']['set'], amp=True)
                tile, block = c['index']
                try:
                    first = decode_flags(self._flags_word(tile, block, True))
                    self.sleep(IDLE_WAIT_S)
                    idle = decode_flags(self._flags_word(tile, block, True))
                except Exception as ex:      # noqa: BLE001
                    refuse('readout %d: the flags of ADC %s cannot be read (%s): the amplifier is left on, nothing '
                           'that needs it should run' % (r, c['adc'], _exc(ex)), 'adc_flags')
                one['flags_switch_on'], one['flags_idle'] = first, idle
                if idle['over_voltage']:
                    run('adc_off', c['port'], None, '%s amplifier off again (over-voltage at idle)' % what)
                    one['amp'] = False
                    refuse('readout %d: ADC %s is in OVER-VOLTAGE with nothing playing (interrupt word 0x%08x) after '
                           'the amplifier was switched on at %s and the ADC attenuator at %g dB: the amplifier was '
                           'switched off again. More attenuation is needed before the ADC (att, adc_att) or the input '
                           'is overdriven.' % (r, c['adc'], idle['word'],
                                               '%g dB' % one['att'] if 'att' in one else 'gain %g dB' % one['gain'],
                                               s['adc_att']), 'adc_flags')
                if idle['over_range']:
                    refuse('readout %d: ADC %s is over range with nothing playing (interrupt word 0x%08x) at ADC '
                           'attenuator %g dB: acquisitions would clip; more attenuation is needed (att, adc_att). The '
                           'amplifier is left on.' % (r, c['adc'], idle['word'], s['adc_att']), 'adc_flags')
                if first['over_voltage'] or first['over_range']:
                    rec['warnings'].append('readout %d: ADC %s flagged %s while the amplifier was switched on '
                                           '(0x%08x); clear at idle' % (r, c['adc'], 'over-voltage'
                                                                        if first['over_voltage'] else 'over-range',
                                                                        first['word']))
            for b in sorted(spec['bias']):
                s = spec['bias'][b]
                e = run('bias', b, s['volts'], 'bias %d -> %g V' % (b, s['volts']))
                rec['bias'][str(b)] = {'volts': s['volts'], 'set': e['result']['set'], 'read': e['result']['read']}
            rec['state'] = self._get_locked(flags=True)
            rec['changed'] = self._changed(before, rec['state'])
            rec['ok'] = True
        for w in rec['warnings']:
            if log is not None:
                log('QICK box: ' + w)
        return rec

    def _changed(self, before, after):
        if not isinstance(after, dict) or 'adc_ports' not in after:
            return None
        a, b = self._flat(before), self._flat(after)
        return [{'what': k, 'before': a.get(k), 'after': b.get(k)} for k in sorted(set(a) | set(b))
                if a.get(k) != b.get(k)]

    def check(self, description, record=None):
        """Every reason the board no longer holds a description (empty list: it does, as far as can be read).
        With the record of the application, a new driver identity (server restart, image load) is one."""
        spec = normalize(description)
        out = []
        if record is not None:
            rid = (record.get('identity') or {}).get('id') if isinstance(record.get('identity'), dict) \
                else record.get('identity')
            if rid != self.identity['id']:
                out.append('the RF driver identity changed (%s -> %s): server restart or image load since the '
                           'description was applied; the cards are back at their power-on state'
                           % (rid, self.identity['id']))
        with self.lock:
            out += self.problems(spec)
            if out and record is None:
                return out
            gens, ros, ports = self.gen_map(), self.ro_map(), self._ports()
            for r in sorted(spec['ros']):
                s, c = spec['ros'][r], ros.get(r)
                if not c or ('adc', c['port']) not in ports:
                    continue
                p = ports[('adc', c['port'])]
                try:
                    got = self._dsa_get(c['adc'])
                    if abs(got - s['adc_att']) > 0.5:
                        out.append('the RF-ADC attenuator of block %s reads %g dB, the description has %g dB'
                                   % (c['adc'], got, s['adc_att']))
                    on = self._amp_read(p)[0]
                    if on is not s.get('amp', True):
                        out.append('readout %d: the amplifier reads %s, the description has %s'
                                   % (r, 'on' if on else 'off', 'on' if s.get('amp', True) else 'off'))
                    if 'filter' in s:
                        want = filter_regs(*filter_steps_for(s['filter']))
                        regs = self._filter_read(p)
                        if regs != want:
                            out.append('readout %d: the filter registers read %s, the description gives %s'
                                       % (r, regs, want))
                    if 'att' in s:
                        w = self._written.get(('ro_att', c['port'])) or {}
                        if w.get('source') != 'written' or w.get('value') != s['att']:
                            out.append('readout %d: the RF In attenuator was not written %g dB by this driver (%s)'
                                       % (r, s['att'], w.get('source', 'unknown')))
                    if 'gain' in s:
                        g = self._gain_read(p)
                        if abs(g - s['gain']) > 0.5:
                            out.append('readout %d: the DC In gain reads %g dB, the description has %g dB'
                                       % (r, g, s['gain']))
                except Exception as e:      # noqa: BLE001
                    out.append('readout %d cannot be read (%s)' % (r, _exc(e)))
            for g in sorted(spec['gens']):
                s, c = spec['gens'][g], gens.get(g)
                if not c or ('dac', c['port']) not in ports:
                    continue
                p = ports[('dac', c['port'])]
                try:
                    if 'dc' in s:
                        if self._amp_read(p)[0] is not s['dc']:
                            out.append('generator %d: DC Out power differs from the description' % g)
                        continue
                    regs = self._filter_read(p)
                    want = filter_regs(*filter_steps_for(s['filter']))
                    if regs != want:
                        out.append('generator %d: the filter registers read %s, the description gives %s'
                                   % (g, regs, want))
                    if self._net_read(p, 'powerup')[0] != 1:
                        out.append('generator %d: the 5 V rail pin of DAC port %d reads off' % (g, c['port']))
                    w = self._written.get(('dac_att', c['port'])) or {}
                    if w.get('source') != 'written' or w.get('value') != s['att']:
                        out.append('generator %d: the attenuators were not written %s dB by this driver (%s)'
                                   % (g, s['att'], w.get('source', 'unknown')))
                except Exception as e:      # noqa: BLE001
                    out.append('generator %d cannot be read (%s)' % (g, _exc(e)))
            biases = getattr(self.soc, 'biases', None) or []
            for b in sorted(spec['bias']):
                try:
                    v = float(biases[b].get_volt())
                    if abs(v - spec['bias'][b]['volts']) > 1e-3:
                        out.append('bias %d reads %g V, the description has %g V' % (b, v, spec['bias'][b]['volts']))
                except Exception as e:      # noqa: BLE001
                    out.append('bias %d cannot be read (%s)' % (b, _exc(e)))
        return out

    def check_applied(self, description, record=None):
        """qick_rf.check_applied: the first reason from check(), or None."""
        r = self.check(description, record)
        return r[0] if r else None


# ---- module-level helpers on a soc (qick_rf compatibility) -----------------------------------------------------------
def adc_dsa(soc, blockname, att=None):
    """Set (att given) and read the RF-ADC step attenuator of ADC block `blockname` ("20": tile 2, block 0)."""
    box = attach(soc)
    blockname = str(blockname)
    with box.lock:
        if blockname not in [b[0] for b in box.adc_blocks()]:
            raise _bad('blockname', 'the firmware has no RF-ADC block %s' % blockname)
        if att is not None:
            box._dsa_set(blockname, _dsa(att, 'att'))
        return box._dsa_get(blockname)


def adc_flags(soc, tile, block, clear=True):
    """The raw interrupt word of one ADC (converter index tile, block), cleared afterwards if `clear`."""
    box = attach(soc)
    with box.lock:
        return box._flags_word(tile, block, clear)


def ro_off(soc, ro_ch):
    """A readout's amplifier off and (RF In) its attenuator to 31.75 dB."""
    return attach(soc).ro_off(int(ro_ch))


FILTER_REGS = ('ADI_SPI_CONFIG_A', 'CHIPTYPE', 'PRODUCT_ID_L', 'PRODUCT_ID_H', 'WR0_SW', 'WR0_FILTER')


def filter_registers(soc, kind, ch):
    """qick_rf.filter_regs: the ADMV8818 registers of generator (kind 'gen') or readout ('ro') `ch`, read from the
    chip: {name: value}. Reads only. PRODUCT_ID 0x8818 when the chip answers (all 0x00 or 0xFF: it does not)."""
    box = attach(soc)
    with box.lock:
        m = box.gen_map() if str(kind) == 'gen' else box.ro_map()
        c = m.get(int(ch))
        if c is None:
            raise _bad('ch', 'the firmware has no %s %r' % ('generator' if str(kind) == 'gen' else 'readout', ch))
        side = 'dac' if str(kind) == 'gen' else 'adc'
        p = box._need(side, c['port'], ('rf_out',) if side == 'dac' else ('rf_in',), '%s %d' % (kind, int(ch)))
        with box._sel(p['board_id']):
            return dict((r, int(p['chain'].filter.read_reg(r))) for r in FILTER_REGS)


def has_helpers(soc):
    """Whether a (remote) soc offers the rfbox_* / labdev_* methods (soc_class, or qick_pyro_server.py's labdev_*)."""
    for n in ('labdev_adc_dsa', 'labdev_adc_flags', 'labdev_ro_off'):
        try:
            if not callable(getattr(soc, n, None)):
                return False
        except Exception:      # noqa: BLE001  (a Pyro proxy that cannot fetch its metadata)
            return False
    return True


layout = cfg_layout           # qick_rf.layout(cfg)


def _plainify(obj):
    return json.loads(json.dumps(obj, default=lambda o: float(o) if isinstance(o, Real) else str(o)))


def _reply(fn, *a, **kw):
    """Run fn for a remote caller: refusals come back as data, bad requests as builtin ValueError."""
    try:
        return _plainify(fn(*a, **kw))
    except Refused as e:
        return _plainify({'ok': False, 'refused': str(e), 'code': e.code, 'record': e.record})
    except RfboxError as e:
        raise ValueError('%s: %s' % (e.code, e))


def check_reply(reply):
    """Client side: raise Refused for a reply that carries a refusal (soc.rfbox_set / rfbox_apply over Pyro)."""
    if isinstance(reply, dict) and reply.get('ok') is False and 'refused' in reply:
        raise Refused(reply['refused'], reply.get('record'), reply.get('code', 'refused'))
    return reply


class RfBoxSocMixin(object):
    """Methods a served soc gains (soc_class): plain data in and out, safe for Pyro's pickle and for JSON."""

    def rfbox_get(self, flags=True):
        return _reply(lambda: attach(self).get(flags=flags))

    def rfbox_layout(self, read_ids=True):
        return _reply(lambda: attach(self).layout(read_ids=read_ids))

    def rfbox_identity(self):
        return _reply(lambda: attach(self).identity)

    def rfbox_set(self, settings, settle_s=SETTLE_DEFAULT_S, protect=True, by='rfbox_set'):
        return _reply(lambda: attach(self).set(settings, settle_s=settle_s, protect=protect, by=by))

    def rfbox_apply(self, description, others='leave', by='rfbox_apply'):
        return _reply(lambda: attach(self).apply(description, others=others, by=by))

    def rfbox_apply_safe(self, items=None, settle_s=SETTLE_DEFAULT_S):
        return _reply(lambda: attach(self).apply_safe(items, settle_s=settle_s))

    def rfbox_clear_flags(self, settle_s=0.0):
        return _reply(lambda: attach(self).clear_flags(settle_s))

    def rfbox_check(self, description, record=None):
        return _reply(lambda: attach(self).check(description, record))

    def rfbox_limits(self, table=None, replace=False):
        """The limits table; with replace=True install `table` (None removes it)."""
        box = attach(self)
        return _reply(lambda: box.set_limits(table) if replace else box.limits)

    # qick_rf / qick_pyro_server.py helpers, same signatures
    def labdev_adc_dsa(self, blockname, att=None):
        return _reply(adc_dsa, self, blockname, att)

    def labdev_adc_flags(self, tile, block, clear=True):
        return _reply(adc_flags, self, tile, block, clear)

    def labdev_ro_off(self, ro_ch):
        return _reply(ro_off, self, ro_ch)

    def labdev_filter_regs(self, kind, ch):
        return _reply(filter_registers, self, kind, ch)


def soc_class(base):
    """base (e.g. qick.rfboard.RFQickSoc216V1) with the rfbox_* and labdev_* methods added, for a Pyro server."""
    class RfBoxSoc(RfBoxSocMixin, base):
        pass
    RfBoxSoc.__name__ = base.__name__
    RfBoxSoc.__qualname__ = getattr(base, '__qualname__', base.__name__)
    return RfBoxSoc


class Local(object):
    """A soc in a Jupyter kernel with the methods a served soc would have (qick_rf.Local)."""

    def __init__(self, soc):
        self._soc = soc

    def __getattr__(self, name):
        if name.startswith(('rfbox_', 'labdev_')):
            return getattr(RfBoxSocMixin, name).__get__(self._soc)
        return getattr(self._soc, name)
