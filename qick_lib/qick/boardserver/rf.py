"""RF front end of a ZCU216 QICK box (the RF daughter cards) for the board server.

This module holds the logic: what can be read, how a list of settings is checked
and put in a safe order, the procedure around an image load, and the records the
server returns. The hardware is reached only through an adapter object:

    LibraryRfHw(soc)   the real one, over qick.rfboard.RFQickSoc216V1 (and xrfdc for
                       the ADC interrupt flags). WRITTEN FROM THE LIBRARY CODE AND FROM
                       board_side/qick_rf.py OF THE ENGINE SIDE'S qubit-measurement
                       REPOSITORY (adc_dsa, adc_flags, ro_off), NOT RUN ON A BOARD
                       FROM THIS CODE. See slab/notes/board-server-install.md for what
                       must be measured before it is trusted.
    FakeRfHw(...)      backend.py's fake RF hardware, for tests.

What the library can and cannot read (qick_lib/qick/rfboard.py, drivers/hardware.py):
  - PE43705 step attenuators (RF Out: two per channel; RF In: one): write-only SPI.
    The server reports the value it (or the constructor) last wrote, marked as such.
  - ADMV8818 filters: the band/state registers WR0_SW, WR0_FILTER are read back.
  - LMH6401 DC In gain: read back (get_gain).
  - DAC11001 bias outputs: setpoint read back (rfb_get_bias).
  - RF In amplifier and DC In power-down switches: MCP23S08 GPIO register read back.
  - RF-ADC step attenuator (DSA, 0-27 dB, Gen 3): read back from the converter
    (QickSoc.get_adc_attenuator). Not part of the rfb_* methods.
  - ADC over-range / over-voltage: the RF data converter's interrupt status word
    (XRFdc_GetIntrStatus, as QickSoc.clear_interrupts reads it). Not part of rfb_*.
  - Not readable: RF Out 5 V supply enables (reported as last written), LO
    synthesizers (ZCU111 only; not handled here).
"""
import time
from numbers import Integral, Real

from .protocol import RequestError

ATT_MAX, ATT_STEP = 31.75, 0.25           # PE43705
DSA_MAX = 27                              # RF-ADC digital step attenuator, 1 dB steps
GAIN_MIN, GAIN_MAX = -6, 26               # LMH6401
BIAS_MAX = 10.0                           # DAC11001
FTYPES = ('bypass', 'lowpass', 'highpass', 'bandpass')
F_MIN, F_MAX = 0.5, 20.0                  # GHz (the library's filter unit)
SETTLE_DEFAULT_S = 0.2
SETTLE_MAX_S = 10.0

# ADC interrupt bits (the masks QickSoc.clear_interrupts decodes)
OVR_VOLTAGE = 0x04000000
OVR_RANGE = 0x08000000
SUBADC_RANGE = 0x00FF0000

KINDS = {'RF Out': 'rf_out', 'DC Out': 'dc_out', 'Balun Out': 'balun_out',
         'RF In': 'rf_in', 'DC In': 'dc_in', 'Balun In': 'balun_in'}

# What RFQickSoc216V1's constructor does to the cards, read from the library code
# (rfboard.py _rfb_config, DaughterCard216/Chain216 constructors, hardware.py).
# Reported in the load_image reset record of an RF image. Not measured.
LIBRARY_CONSTRUCTOR = {
    'source': 'read from qick.rfboard.RFQickSoc216V1Mixin._rfb_config and the card/chain constructors; not measured',
    'rf_out': 'both PE43705 attenuators of every channel written 31.75 dB; card GPIO written with '
              'RFOUT5V0_EN0/1 = 0 (5 V supplies off)',
    'rf_in': 'PE43705 attenuator written 31.75 dB; card GPIO written with RFIN5V0CH0/1_EN = 0 (amplifiers off)',
    'dc_in': 'LMH6401 gain written -6 dB; the card GPIO default is meant to be PD0/PD1 = 1 (powered down), but '
             'SwitchControl.add_MCP writes output i to pin 3-i while set_bits uses pin i, so pins 0/1 are '
             'written 0: the DC In amplifiers may be left powered up (code reading; check on a board)',
    'dc_out': 'card GPIO PD0-3 = 0 (powered up)',
    'filters': 'ADMV8818: only ADI_SPI_CONFIG_A = 0x3C is written; the band/state registers are not, so each '
               'filter keeps what it held (read back in the rf record)',
    'bias': 'DAC11001 CONFIG1 = 0x4e00 and all eight outputs 0 V, then the bias switch enabled (bias_gpio = 1)',
    'rf_adc_dsa': 'not written by the constructor; after a PL load it reads 0 dB (engine-side measurement)',
    'adc_tiles': 'all ADC tiles restarted, then the ADC interrupts cleared without error',
}


def decode_flags(word):
    w = int(word or 0)
    return {'word': w, 'over_voltage': bool(w & OVR_VOLTAGE), 'over_range': bool(w & (OVR_RANGE | SUBADC_RANGE)),
            'other': w & ~(OVR_VOLTAGE | OVR_RANGE | SUBADC_RANGE) & 0xFFFFFFFF}


def adc_block_of_port(port):
    return '%d%d' % (port // 4 + 1, port % 4)


def adc_port_of_block(name):
    t, b = int(name[0]), int(name[1])
    return (t - 1) * 4 + b if t in (1, 2) else None


def _err(field, msg):
    return RequestError('bad_request', '%s: %s' % (field, msg), field=field)


def _num(v, field):
    if isinstance(v, bool) or not isinstance(v, Real) or v != v or v in (float('inf'), float('-inf')):
        raise _err(field, '%r is not a number' % (v,))
    return float(v)


def _att(v, field):
    v = _num(v, field)
    if not 0 <= v <= ATT_MAX:
        raise _err(field, '%g dB is outside 0 .. %g dB' % (v, ATT_MAX))
    if abs(v / ATT_STEP - round(v / ATT_STEP)) > 1e-9:
        raise _err(field, '%g dB is not a multiple of %g dB (the attenuator step)' % (v, ATT_STEP))
    return v


def _dsa(v, field):
    v = _num(v, field)
    if not 0 <= v <= DSA_MAX or v != int(v):
        raise _err(field, '%g dB: the RF-ADC attenuator takes whole dB in 0 .. %d' % (v, DSA_MAX))
    return float(v)


def _filter(f, field):
    if not isinstance(f, dict):
        raise _err(field, 'a filter is {ftype, fc, bw} (GHz) or {hpf, lpf} (raw steps)')
    if 'hpf' in f or 'lpf' in f:
        bad = sorted(set(f) - {'hpf', 'lpf'})
        if bad:
            raise _err(field, 'raw filter has only hpf and lpf, not %s' % ', '.join(bad))
        out = {}
        for k in ('hpf', 'lpf'):
            v = f.get(k, -1)
            if isinstance(v, bool) or not isinstance(v, Integral) or not -1 <= v <= 63:
                raise _err(field + '.' + k, '%r: -1 (bypass) or 0 .. 63' % (v,))
            out[k] = int(v)
        return out
    bad = sorted(set(f) - {'ftype', 'fc', 'bw'})
    if bad:
        raise _err(field, 'unknown key %s (a filter has ftype, fc, bw)' % ', '.join(bad))
    ft = f.get('ftype')
    if ft not in FTYPES:
        raise _err(field + '.ftype', '%r is not one of %s' % (ft, ', '.join(FTYPES)))
    out = {'ftype': ft}
    if ft != 'bypass':
        fc = _num(f.get('fc'), field + '.fc')
        if not F_MIN <= fc <= F_MAX:
            raise _err(field + '.fc', '%g GHz is outside %g .. %g GHz (filter frequencies are in GHz)'
                       % (fc, F_MIN, F_MAX))
        out['fc'] = fc
    if ft == 'bandpass':
        bw = _num(f.get('bw'), field + '.bw')
        if not 0 < bw < 2 * out['fc']:
            raise _err(field + '.bw', '%g GHz does not fit around fc' % bw)
        out['bw'] = bw
    return out


ITEM_KEYS = {'ro', 'gen', 'adc', 'bias', 'adc_att', 'filter', 'att', 'amp', 'gain', 'volts'}


def check_items(settings):
    """Syntax check of an rf_set settings list (no board needed). Returns a normalized
    copy. Raises RequestError('bad_request') naming the field."""
    if not isinstance(settings, list) or not settings:
        raise _err('settings', 'a non-empty list of settings')
    out = []
    for i, it in enumerate(settings):
        w = 'settings[%d]' % i
        if not isinstance(it, dict):
            raise _err(w, 'each setting is an object')
        bad = sorted(set(it) - ITEM_KEYS)
        if bad:
            raise _err(w, 'unknown key %s' % ', '.join(bad))
        targets = [k for k in ('ro', 'gen', 'adc', 'bias') if k in it]
        if len(targets) != 1:
            raise _err(w, 'exactly one of ro (readout index), gen (generator index), adc (ADC block name, '
                          'for adc_att only) or bias (bias output 0-7)')
        t = targets[0]
        n = {t: it[t]}
        if t == 'adc':
            if not isinstance(it['adc'], str) or len(it['adc']) != 2 or not it['adc'].isdigit():
                raise _err(w + '.adc', 'an ADC block name such as "20" (tile 2, block 0)')
        else:
            v = it[t]
            if isinstance(v, bool) or not isinstance(v, Integral) or v < 0 or (t == 'bias' and v > 7):
                raise _err(w + '.' + t, '%r is not a channel number' % (v,))
        allowed = {'ro': {'adc_att', 'filter', 'att', 'amp', 'gain'}, 'gen': {'filter', 'att'},
                   'adc': {'adc_att'}, 'bias': {'volts'}}[t]
        keys = set(it) - {t}
        if not keys:
            raise _err(w, 'nothing to set')
        if keys - allowed:
            raise _err(w, '%s cannot be set on a %s (allowed: %s)' % (', '.join(sorted(keys - allowed)), t,
                                                                    ', '.join(sorted(allowed))))
        if 'adc_att' in it:
            n['adc_att'] = _dsa(it['adc_att'], w + '.adc_att')
        if 'filter' in it:
            n['filter'] = _filter(it['filter'], w + '.filter')
        if t == 'gen' and 'att' in it:
            a = it['att']
            if not isinstance(a, (list, tuple)) or len(a) != 2:
                raise _err(w + '.att', 'a generator on an RF Out card takes two attenuations [A1, A2] (dB)')
            n['att'] = [_att(a[0], w + '.att[0]'), _att(a[1], w + '.att[1]')]
        if t == 'ro':
            if 'amp' in it and not isinstance(it['amp'], bool):
                raise _err(w + '.amp', 'true or false')
            if 'att' in it:
                if it.get('amp') is not True:
                    raise _err(w + '.att', 'the library writes the RF In attenuator only while switching the '
                                           'amplifier on (rfb_set_adc_rf): give amp: true with att')
                n['att'] = _att(it['att'], w + '.att')
            if 'gain' in it:
                if it.get('amp') is False:
                    raise _err(w + '.gain', 'gain with amp: false')
                g = _num(it['gain'], w + '.gain')
                if not GAIN_MIN <= g <= GAIN_MAX or g != int(g):
                    raise _err(w + '.gain', '%g dB: whole dB in %d .. %d' % (g, GAIN_MIN, GAIN_MAX))
                n['gain'] = g
            if it.get('amp') is True and 'att' not in it and 'gain' not in it:
                raise _err(w + '.amp', 'amp: true needs att (RF In) or gain (DC In)')
            if 'amp' in it:
                n['amp'] = it['amp']
        if t == 'bias':
            v = _num(it['volts'], w + '.volts')
            if not -BIAS_MAX <= v <= BIAS_MAX:
                raise _err(w + '.volts', '%g V is outside -%g .. %g V' % (v, BIAS_MAX, BIAS_MAX))
            n['volts'] = v
        out.append(n)
    return out


def check_settle(v):
    if v is None:
        return SETTLE_DEFAULT_S
    if isinstance(v, bool) or not isinstance(v, Real) or not 0 <= v <= SETTLE_MAX_S:
        raise _err('settle_s', 'seconds in 0 .. %g' % SETTLE_MAX_S)
    return float(v)


class RfSafeFailed(Exception):
    def __init__(self, message, record):
        super().__init__(message)
        self.record = record


class RfFrontEnd:
    """The RF cards of the loaded image, over a hardware adapter `hw`:

      hw.layout()                    {'adc': [card|None]*4, 'dac': [card|None]*4}; card = {kind, name, ports}
      hw.adc_blocks()                [(name, tile, block)] of every RF-ADC in the firmware
      hw.dsa_set(name, dB) / dsa_get(name)
      hw.flags(tile, block, clear)   raw interrupt word
      hw.adc_rf_on(port, att) -> att     RF In: amplifier on, attenuator
      hw.adc_dc_on(port, gain) -> gain   DC In: powered up, gain
      hw.adc_off(port)                   RF In: amplifier off, attenuator 31.75; DC In: powered down
      hw.adc_amp_read(port) -> bool|None
      hw.adc_gain_read(port) -> dB|None
      hw.dac_rf(port, a1, a2) -> (a1, a2)
      hw.filter_set(side, port, f)       side 'adc'|'dac'; f {ftype, fc, bw} or {hpf, lpf}
      hw.filter_read(side, port) -> {'WR0_SW', 'WR0_FILTER'}
      hw.bias_set(i, v) -> v / bias_get(i)
      hw.constructor_written()       {'dac': {port: [a1, a2]}, 'adc': {port: att}}: what the soc
                                     constructor wrote to the write-only attenuators
    The cache holds what was last written to things that cannot be read back.
    """

    def __init__(self, hw, soccfg, sleep=time.sleep):
        self.hw = hw
        self.cfg = soccfg
        self.sleep = sleep
        cw = hw.constructor_written()
        self.written = {'dac_att': {int(p): {'value': list(v), 'by': 'constructor'} for p, v in cw.get('dac', {}).items()},
                        'adc_att': {int(p): {'value': v, 'by': 'constructor'} for p, v in cw.get('adc', {}).items()},
                        'dac_supply': {}}

    # --- mapping ----------------------------------------------------------

    def layout(self):
        return self.hw.layout()

    def _card(self, side, port):
        lay = self.layout()
        slot = port // (4 if side == 'dac' else 2)
        cards = lay[side]
        return cards[slot] if 0 <= slot < len(cards) else None

    def gen_map(self):
        out = {}
        for i, g in enumerate(self.cfg.get('gens', [])):
            t, b = int(str(g['dac'])[0]), int(str(g['dac'])[1])
            port = t * 4 + b
            card = self._card('dac', port)
            out[i] = {'dac': str(g['dac']), 'port': port, 'slot': port // 4, 'kind': card['kind'] if card else None}
        return out

    def ro_map(self):
        out = {}
        for i, r in enumerate(self.cfg.get('readouts', [])):
            name = str(r['adc'])
            port = adc_port_of_block(name)
            card = self._card('adc', port) if port is not None else None
            out[i] = {'adc': name, 'tile': int(name[0]), 'block': int(name[1]), 'port': port,
                      'slot': None if port is None else port // 2, 'kind': card['kind'] if card else None}
        return out

    def adc_ports(self):
        """[(port, kind)] of every ADC-side chain on a card."""
        out = []
        for slot, card in enumerate(self.layout()['adc']):
            if card:
                for p in card['ports']:
                    out.append((p, card['kind']))
        return out

    # --- reading ------------------------------------------------------------

    def _try(self, fn, *a):
        try:
            return fn(*a)
        except Exception as e:
            return {'error': '%s: %s' % (type(e).__name__, e)}

    def flags_all(self, clear=False):
        out = {}
        for name, t, b in self.hw.adc_blocks():
            w = self._try(self.hw.flags, t, b, clear)
            out[name] = w if isinstance(w, dict) else decode_flags(w)
        return out

    def dsa_all(self):
        return {name: self._try(self.hw.dsa_get, name) for name, t, b in self.hw.adc_blocks()}

    def read(self, flags=True):
        """Everything readable, plus what was last written to the write-only parts.
        Flags are read without clearing them."""
        lay = self.layout()
        adc = {}
        for p, kind in self.adc_ports():
            d = {'port': p, 'block': adc_block_of_port(p), 'kind': kind}
            if kind in ('rf_in', 'dc_in'):
                d['amp_on'] = self._try(self.hw.adc_amp_read, p)
            if kind == 'rf_in':
                d['att'] = self.written['adc_att'].get(p)
                d['filter'] = self._try(self.hw.filter_read, 'adc', p)
            if kind == 'dc_in':
                d['gain'] = self._try(self.hw.adc_gain_read, p)
            adc[str(p)] = d
        dac = {}
        for slot, card in enumerate(lay['dac']):
            if not card:
                continue
            for p in card['ports']:
                d = {'port': p, 'kind': card['kind']}
                if card['kind'] == 'rf_out':
                    d['att'] = self.written['dac_att'].get(p)
                    d['supply_on'] = self.written['dac_supply'].get(p)
                    d['filter'] = self._try(self.hw.filter_read, 'dac', p)
                dac[str(p)] = d
        rec = {
            'cards': lay,
            'gens': {str(k): v for k, v in self.gen_map().items()},
            'ros': {str(k): v for k, v in self.ro_map().items()},
            'adc_ports': adc,
            'dac_ports': dac,
            'adc_att': self.dsa_all(),
            'bias': {str(i): self._try(self.hw.bias_get, i) for i in range(8)},
            'readback': {'read_from_hardware': ['adc_att (RF-ADC step attenuator, from the converter)',
                                                'flags (ADC interrupt status, not cleared by reading)',
                                                'amp_on (card GPIO register)', 'gain (LMH6401)',
                                                'filter (ADMV8818 WR0_SW / WR0_FILTER)', 'bias (DAC11001)'],
                         'last_written': ['att (PE43705 attenuators are write-only: {value, by})',
                                          'supply_on (RF Out 5 V supply)']},
        }
        if flags:
            rec['flags'] = self.flags_all(clear=False)
        return rec

    # --- applying -------------------------------------------------------------

    def plan(self, items):
        """Check items against this board and order them. Returns a list of steps
        (phase, op, target, value, why). Raises RequestError before anything is applied."""
        gens, ros = self.gen_map(), self.ro_map()
        steps = []
        dsa_now = {}

        def ro_info(r, w, need=None):
            if r not in ros:
                raise _err(w + '.ro', 'the firmware has no readout %d (it has %d)' % (r, len(ros)))
            c = ros[r]
            if need and c['kind'] not in need:
                raise RequestError('no_rf_board', 'readout %d is on ADC %s (port %s): %s, not %s'
                                   % (r, c['adc'], c['port'], c['kind'] or 'no card', ' or '.join(need)),
                                   field=w + '.ro')
            return c

        for i, it in enumerate(items):
            w = 'settings[%d]' % i
            if 'adc' in it or ('ro' in it and 'adc_att' in it):
                name = it['adc'] if 'adc' in it else ro_info(it['ro'], w)['adc']
                if name not in [b[0] for b in self.hw.adc_blocks()]:
                    raise _err(w + '.adc', 'the firmware has no RF-ADC block %s' % name)
                if name not in dsa_now:
                    cur = self._try(self.hw.dsa_get, name)
                    dsa_now[name] = cur if not isinstance(cur, dict) else None
                cur = dsa_now[name]
                new = it['adc_att']
                up = cur is not None and new >= cur
                steps.append(('reduce' if up else 'increase', 'adc_att', name, new,
                              'RF-ADC attenuator %s -> %g dB' % ('unknown' if cur is None else '%g' % cur, new)))
            if 'gen' in it:
                g = it['gen']
                if g not in gens:
                    raise _err(w + '.gen', 'the firmware has no generator %d (it has %d)' % (g, len(gens)))
                c = gens[g]
                if c['kind'] != 'rf_out':
                    raise RequestError('no_rf_board', 'generator %d is on DAC %s (port %d): %s, not an RF Out card'
                                       % (g, c['dac'], c['port'], c['kind'] or 'no card'), field=w + '.gen')
                if 'filter' in it:
                    steps.append(('filters', 'filter', ('dac', c['port']), it['filter'], 'generator %d filter' % g))
                if 'att' in it:
                    old = (self.written['dac_att'].get(c['port']) or {}).get('value')
                    up = old is not None and it['att'][0] >= old[0] and it['att'][1] >= old[1]
                    steps.append(('reduce' if up else 'increase', 'dac_att', c['port'], it['att'],
                                  'generator %d attenuators %s -> %s dB' % (g, old, it['att'])))
            if 'ro' in it and set(it) - {'ro', 'adc_att'}:
                r = it['ro']
                if 'filter' in it:
                    c = ro_info(r, w, ('rf_in',))
                    steps.append(('filters', 'filter', ('adc', c['port']), it['filter'], 'readout %d filter' % r))
                if it.get('amp') is False:
                    c = ro_info(r, w, ('rf_in', 'dc_in'))
                    steps.append(('reduce', 'adc_off', c['port'], None, 'readout %d amplifier off' % r))
                elif 'att' in it:
                    c = ro_info(r, w, ('rf_in',))
                    on = self._try(self.hw.adc_amp_read, c['port'])
                    old = (self.written['adc_att'].get(c['port']) or {}).get('value')
                    up = on is True and old is not None and it['att'] >= old
                    steps.append(('reduce' if up else 'increase', 'adc_rf_on', c['port'], it['att'],
                                  'readout %d RF In amplifier on, attenuator %s -> %g dB' % (r, old, it['att'])))
                elif 'gain' in it:
                    c = ro_info(r, w, ('dc_in',))
                    on = self._try(self.hw.adc_amp_read, c['port'])
                    old = self._try(self.hw.adc_gain_read, c['port'])
                    up = on is True and isinstance(old, Real) and it['gain'] <= old
                    steps.append(('reduce' if up else 'increase', 'adc_dc_on', c['port'], it['gain'],
                                  'readout %d DC In powered up, gain %s -> %g dB' % (r, old, it['gain'])))
            if 'bias' in it:
                steps.append(('bias', 'bias', it['bias'], it['volts'], 'bias %d -> %g V' % (it['bias'], it['volts'])))
        # stable order: reductions (gain removed, attenuation added), filters, increases
        # (attenuation removed, amplifiers on), bias. Within increases: card attenuators,
        # then amplifiers, then RF-ADC attenuator decreases last.
        phase_rank = {'reduce': 0, 'filters': 1, 'increase': 2, 'bias': 3}
        op_rank = {'adc_att': 0, 'adc_off': 1, 'adc_dc_on': 2, 'dac_att': 3, 'adc_rf_on': 4, 'filter': 0, 'bias': 0}
        inc_rank = {'dac_att': 0, 'adc_rf_on': 1, 'adc_dc_on': 2, 'adc_att': 3}
        keyed = []
        for n, s in enumerate(steps):
            rank = inc_rank.get(s[1], 9) if s[0] == 'increase' else op_rank.get(s[1], 9)
            if s[0] == 'filters':
                rank = 0 if s[2][0] == 'adc' else 1
            keyed.append(((phase_rank[s[0]], rank, n), s))
        return [s for k, s in sorted(keyed)]

    def _do(self, step):
        phase, op, target, value, why = step
        hw = self.hw
        if op == 'adc_att':
            got = hw.dsa_set(target, value)
            return {'set': got, 'read': self._try(hw.dsa_get, target)}
        if op == 'filter':
            side, port = target
            hw.filter_set(side, port, value)
            return {'read': self._try(hw.filter_read, side, port)}
        if op == 'dac_att':
            got = hw.dac_rf(target, value[0], value[1])
            got = [float(got[0]), float(got[1])]
            self.written['dac_att'][target] = {'value': got, 'by': 'rf_set'}
            self.written['dac_supply'][target] = True
            return {'set': got}
        if op == 'adc_off':
            hw.adc_off(target)
            if target in self.written['adc_att'] or self._card('adc', target)['kind'] == 'rf_in':
                self.written['adc_att'][target] = {'value': ATT_MAX, 'by': 'amplifier off'}
            return {'amp_on': self._try(hw.adc_amp_read, target)}
        if op == 'adc_rf_on':
            got = float(hw.adc_rf_on(target, value))
            self.written['adc_att'][target] = {'value': got, 'by': 'rf_set'}
            return {'set': got, 'amp_on': self._try(hw.adc_amp_read, target)}
        if op == 'adc_dc_on':
            got = hw.adc_dc_on(target, value)
            return {'set': got, 'gain': self._try(hw.adc_gain_read, target),
                    'amp_on': self._try(hw.adc_amp_read, target)}
        if op == 'bias':
            got = hw.bias_set(target, value)
            return {'set': got, 'read': self._try(hw.bias_get, target)}
        raise ValueError(op)

    def apply(self, items, settle_s=SETTLE_DEFAULT_S, protect=True):
        """Apply checked items in the safe order, then clear the ADC flags, wait
        settle_s and read them. Returns the record; record['ok'] is False when a flag is
        set at idle. With protect, an ADC in over-voltage at idle has its card amplifier
        switched off (record['actions'])."""
        steps = self.plan(items)
        rec = {'order': [], 'actions': [], 'problems': []}
        for s in steps:
            entry = {'phase': s[0], 'op': s[1], 'target': list(s[2]) if isinstance(s[2], tuple) else s[2],
                     'value': s[3], 'what': s[4]}
            rec['order'].append(entry)
            try:
                entry['result'] = self._do(s)
            except Exception as e:
                entry['error'] = '%s: %s' % (type(e).__name__, e)
                rec['problems'].append('%s failed: %s' % (s[4], entry['error']))
                rec['ok'] = False
                rec['stopped_at'] = s[4]
                return rec
        rec.update(self.idle_flags(settle_s, protect))
        return rec

    def idle_flags(self, settle_s, protect=True):
        """Clear the ADC flags (the words read while clearing are returned as
        flags_before_clear), wait settle_s, read them again (and clear)."""
        out = {'flags_before_clear': self.flags_all(clear=True)}
        if settle_s:
            self.sleep(settle_s)
        idle = self.flags_all(clear=True)
        out['flags_idle'] = idle
        out['settle_s'] = settle_s
        problems, actions = [], []
        rf_in = {adc_block_of_port(p): p for p, k in self.adc_ports() if k in ('rf_in', 'dc_in')}
        for name, f in sorted(idle.items()):
            if 'error' in f:
                problems.append('ADC %s flags could not be read: %s' % (name, f['error']))
                continue
            if f['over_voltage']:
                problems.append('ADC %s in OVER-VOLTAGE at idle (0x%08x)' % (name, f['word']))
                if protect and name in rf_in:
                    try:
                        self.hw.adc_off(rf_in[name])
                        actions.append({'op': 'adc_off', 'port': rf_in[name], 'why': 'over-voltage at idle',
                                        'amp_on': self._try(self.hw.adc_amp_read, rf_in[name])})
                    except Exception as e:
                        actions.append({'op': 'adc_off', 'port': rf_in[name], 'error': '%s: %s' % (type(e).__name__, e)})
            elif f['over_range']:
                problems.append('ADC %s over range at idle (0x%08x)' % (name, f['word']))
        out['problems'] = problems
        out['actions'] = actions
        out['ok'] = not problems
        return out

    # --- around an image load ---------------------------------------------------

    def before_load(self):
        """Switch every readout-side amplifier (RF In and DC In) off and read the switch
        back. Raises RfSafeFailed with the record if anything failed or reads on."""
        rec = {'done': [], 'ok': True}
        for p, kind in self.adc_ports():
            if kind not in ('rf_in', 'dc_in'):
                continue
            e = {'port': p, 'kind': kind}
            try:
                self.hw.adc_off(p)
                e['amp_on'] = self.hw.adc_amp_read(p)
            except Exception as ex:
                e['error'] = '%s: %s' % (type(ex).__name__, ex)
            if e.get('amp_on') is not False:
                rec['ok'] = False
            rec['done'].append(e)
        rec['flags'] = self.flags_all(clear=False)
        if not rec['ok']:
            bad = [e for e in rec['done'] if e.get('amp_on') is not False]
            raise RfSafeFailed('could not confirm the readout amplifiers off before the load: %s' % bad, rec)
        return rec

    def default_safe_items(self):
        """Built-in safe state: every RF-ADC attenuator at its maximum, every readout
        amplifier off."""
        items = [{'adc': name, 'adc_att': float(DSA_MAX)} for name, t, b in self.hw.adc_blocks()]
        return items

    def after_load(self, safe_items, settle_s):
        """Apply the safe state after a load: RF-ADC attenuators (the file's values,
        default maximum) with readback, amplifiers off unless the file turns them on,
        then the file's filters and attenuators in the rf_set order, then the idle flags."""
        file_items = list(safe_items or [])
        by_block = {}
        for it in file_items:
            if 'adc_att' in it:
                name = it['adc'] if 'adc' in it else self.ro_map().get(it.get('ro'), {}).get('adc')
                by_block[name] = it['adc_att']
        items = [it for it in self.default_safe_items() if it['adc'] not in by_block]
        on_ports = set()
        rm = self.ro_map()
        for it in file_items:
            if it.get('amp') is True and it.get('ro') in rm:
                on_ports.add(rm[it['ro']]['port'])
        # amplifiers off first (reduce phase), unless the file turns them on later
        off = [p for p, k in self.adc_ports() if k in ('rf_in', 'dc_in') and p not in on_ports]
        rec = {'source': 'safe-state file' if file_items else 'built-in default',
               'amplifiers_off': [], 'ok': True}
        for p in off:
            e = {'port': p}
            try:
                self.hw.adc_off(p)
                if self._card('adc', p)['kind'] == 'rf_in':
                    self.written['adc_att'][p] = {'value': ATT_MAX, 'by': 'amplifier off'}
                e['amp_on'] = self.hw.adc_amp_read(p)
            except Exception as ex:
                e['error'] = '%s: %s' % (type(ex).__name__, ex)
            if e.get('amp_on') is not False:
                rec['ok'] = False
            rec['amplifiers_off'].append(e)
        res = self.apply(items + file_items, settle_s=settle_s, protect=True)
        rec.update(res)
        rec['ok'] = rec['ok'] and res.get('ok', False)
        rec['adc_att'] = self.dsa_all()
        want = dict((it['adc'], it['adc_att']) for it in items)
        want.update(by_block)
        for name, v in want.items():
            got = rec['adc_att'].get(name)
            if not isinstance(got, Real) or abs(got - v) > 0.5:
                rec['ok'] = False
                rec.setdefault('problems', []).append('RF-ADC %s attenuator reads %s after setting %g dB' % (name, got, v))
        return rec


class LibraryRfHw:
    """Adapter over qick.rfboard.RFQickSoc216V1. UNVERIFIED: written from the library
    code; the RF-ADC attenuator and flag helpers follow the engine side's
    qubit-measurement board_side/qick_rf.py (adc_dsa, adc_flags, ro_off), which they
    ran on a ZCU216 with RF cards. Nothing here has been run on a board from this code."""

    def __init__(self, soc):
        self.soc = soc

    @staticmethod
    def present(soc):
        return hasattr(soc, 'adc_cards') and hasattr(soc, 'dac_cards') and hasattr(soc, 'board_sel')

    def layout(self):
        out = {}
        for side, cards in (('adc', self.soc.adc_cards), ('dac', self.soc.dac_cards)):
            out[side] = [None if c is None else
                         {'kind': KINDS.get(c.NAME, c.NAME), 'name': c.NAME,
                          'ports': [int(ch.global_ch) if hasattr(ch, 'global_ch') else int(ch) for ch in c.chains]}
                         for c in cards]
        return out

    def adc_blocks(self):
        out = []
        for name, cfg in sorted(self.soc['rf']['adcs'].items()):
            t, b = cfg['index']
            out.append((name, int(t), int(b)))
        return out

    def constructor_written(self):
        # the chain constructors write 31.75 dB to every PE43705 (AttenuatorPE43705.__init__)
        out = {'dac': {}, 'adc': {}}
        for p, ch in enumerate(self.soc.dac_chains):
            if ch is not None and hasattr(ch, 'attn'):
                out['dac'][p] = [ATT_MAX, ATT_MAX]
        for p, ch in enumerate(self.soc.adc_chains):
            if ch is not None and hasattr(ch, 'attn'):
                out['adc'][p] = ATT_MAX
        return out

    def dsa_set(self, name, att):
        # QickSoc.set_adc_attenuator -> RFDC.set_adc_attenuator (the xrfdc block's DSA)
        return float(self.soc.set_adc_attenuator(name, float(att)))

    def dsa_get(self, name):
        return float(self.soc.get_adc_attenuator(name))

    def flags(self, tile, block, clear):
        # as qubit-measurement board_side/qick_rf.py adc_flags and QickSoc.clear_interrupts
        import xrfdc
        arr = xrfdc._ffi.new("unsigned int [1]")
        st = xrfdc._lib.XRFdc_GetIntrStatus(self.soc.rf._instance, xrfdc._lib.XRFDC_ADC_TILE, int(tile), int(block),
                                            arr)
        if st != 0:
            raise RuntimeError('XRFdc_GetIntrStatus(%d, %d): status %d' % (tile, block, st))
        word = int(arr[0])
        if clear:
            st = xrfdc._lib.XRFdc_IntrClr(self.soc.rf._instance, xrfdc._lib.XRFDC_ADC_TILE, int(tile), int(block),
                                          0xFFFFFFFF)
            if st != 0:
                raise RuntimeError('XRFdc_IntrClr(%d, %d): status %d' % (tile, block, st))
        return word

    def adc_rf_on(self, port, att):
        return self.soc.rfb_set_adc_rf(port, att)

    def adc_dc_on(self, port, gain):
        return self.soc.rfb_set_adc_dc(port, gain)

    def adc_off(self, port):
        # AdcRfChain216.disable: amplifier supply off, attenuator 31.75 dB;
        # AdcDcChain216.disable: PD = 1 (qick_rf.ro_off does the same by readout index)
        self.soc.adc_chains[port].disable()

    def adc_amp_read(self, port):
        ch = self.soc.adc_chains[port]
        net = getattr(ch, 'powerup', None) or getattr(ch, 'powerdown', None)
        if net is None:
            return None
        sc = ch.card.switch_control
        dev, bit = sc.net2port[net]
        with self.soc.board_sel.enable_context(ch.card_num):
            reg = sc.devs[dev].read_reg('GPIO_REG')
        val = (reg >> bit) & 1
        return bool(val) if hasattr(ch, 'powerup') else not bool(val)

    def adc_gain_read(self, port):
        ch = self.soc.adc_chains[port]
        return float(ch.get_gain()) if hasattr(ch, 'get_gain') else None

    def dac_rf(self, port, a1, a2):
        return self.soc.rfb_set_dac_rf(port, a1, a2)

    def filter_set(self, side, port, f):
        if 'hpf' in f:
            fn = self.soc.rfb_set_adc_filter_raw if side == 'adc' else self.soc.rfb_set_dac_filter_raw
            fn(port, hpf=f['hpf'], lpf=f['lpf'])
        else:
            fn = self.soc.rfb_set_adc_filter if side == 'adc' else self.soc.rfb_set_dac_filter
            fn(port, fc=f.get('fc', 0.0), bw=f.get('bw', 1.0), ftype=f['ftype'])

    def filter_read(self, side, port):
        ch = (self.soc.adc_chains if side == 'adc' else self.soc.dac_chains)[port]
        return {'WR0_SW': ch.read_filter('WR0_SW'), 'WR0_FILTER': ch.read_filter('WR0_FILTER')}

    def bias_set(self, i, v):
        return float(self.soc.rfb_set_bias(i, v))

    def bias_get(self, i):
        return float(self.soc.rfb_get_bias(i))
