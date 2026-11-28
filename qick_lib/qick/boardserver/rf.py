"""RF front end of a ZCU216 QICK box for the board server: a thin layer over qick.rfbox.

All the logic (what is read back, validation, the safe order, the procedure around an image load, the fake) lives
in qick/rfbox.py and qick/rfbox_fake.py, which also run without the board server (in the stock Pyro server, in a
notebook). This module only keeps the board server's wire records (rf_get, rf_set, rf_clear_flags, reset.rf) and
turns rfbox's errors into protocol errors:

    RfFrontEnd(rfbox.attach(soc))   the RF cards of the loaded image
    RfFrontEnd(MapOnly(cfg))        readout -> ADC block map from a soccfg alone (limits of a stored safe state)

Wire compatibility: a value that rfbox reports as {value, source, by} is sent as {value, by} when it was written
(or assumed from the constructor) and null when unknown, as before. rf_set accepts everything it accepted before
and, in addition, att without amp on RF In, amp on its own, gain on its own, and supply on RF Out (rfbox.check_items).
"""
from .. import rfbox as B
from ..rfbox import (ATT_MAX, ATT_STEP, DSA_MAX, GAIN_MIN, GAIN_MAX, BIAS_MAX, FTYPES, F_MIN, F_MAX,  # noqa: F401
                     SETTLE_DEFAULT_S, SETTLE_MAX_S, OVR_VOLTAGE, OVR_RANGE, SUBADC_RANGE, KINDS, FILTER_BANDS,
                     LIMIT_KEYS, ITEM_KEYS, LIBRARY_CONSTRUCTOR, decode_flags, flag_list, filter_step_freq,
                     filter_step, filter_steps_for, decode_filter, adc_block_of_port, adc_port_of_block)
from .protocol import RequestError


def _rq(e):
    return RequestError(e.code, str(e), **dict(e.info))


def check_items(settings, allow_empty=False):
    try:
        return B.check_items(settings, allow_empty)
    except B.RfboxError as e:
        raise _rq(e)


def check_settle(v):
    try:
        return B.check_settle(v)
    except B.RfboxError as e:
        raise _rq(e)


def check_limits_table(lim):
    try:
        return B.check_limits_table(lim)
    except B.RfboxError as e:
        raise _rq(e)


def check_limits(items, limits, fe):
    try:
        B.check_limits(items, limits, fe.ro_map())
    except B.RfboxError as e:
        raise _rq(e)


class RfSafeFailed(Exception):
    def __init__(self, message, record):
        super().__init__(message)
        self.record = record


class MapOnly:
    """No hardware: maps readouts to ADC blocks from a soccfg alone."""

    def __init__(self, cfg):
        self.cfg = cfg


def _old_att(rec):
    if not isinstance(rec, dict) or rec.get('source') not in ('written', 'assumed'):
        return None
    by = rec.get('by')
    return {'value': rec['value'], 'by': 'constructor' if by == 'library constructor' else by}


def _filter_old(f):
    """rfbox filter state -> (raw registers, filter_setting) as rf_get had them."""
    if not isinstance(f, dict):
        return None, {'source': 'unknown'}
    if f.get('source') == 'error':
        return {'error': f.get('error')}, _setting(None, f.get('last_request'))
    if f.get('source') == 'written':
        # the registers do not read back what rfbox wrote (or cannot be read)
        raw = f.get('raw') if f.get('raw') is not None else {'error': f.get('readback_error')}
        return raw, {'source': 'as written', 'request': f.get('last_request'), 'written': f.get('value'),
                     'verified': False, 'why': f.get('why')}
    return f.get('raw'), _setting(f.get('value'), f.get('last_request'))


def _setting(dec, last):
    if dec is not None:
        d = dict(dec)
        d['source'] = 'decoded from the registers'
        if last is not None:
            d['last_request'] = last
        return d
    if last is not None:
        return {'source': 'as written', 'request': last, 'why': 'the registers do not decode to a set_filter_raw setting'}
    return {'source': 'unknown', 'why': 'registers not decodable and nothing written since the load'}


def _v(rec):
    if isinstance(rec, dict) and 'source' in rec:
        return {'error': rec['error']} if rec['source'] == 'error' else rec.get('value')
    return rec


class RfFrontEnd:
    """The board server's view of an rfbox.RfBox (or of a soccfg alone, MapOnly)."""

    def __init__(self, box, soccfg=None):
        if isinstance(box, MapOnly):
            self.box, self.cfg = None, box.cfg
        else:
            self.box, self.cfg = box, soccfg

    def ro_map(self):
        if self.box is not None:
            return self.box.ro_map()
        return B.ro_map(self.cfg, B.cfg_layout(self.cfg))

    def gen_map(self):
        if self.box is not None:
            return self.box.gen_map()
        return B.gen_map(self.cfg, B.cfg_layout(self.cfg))

    def read(self, flags=True):
        st = self.box.get(flags=flags)
        lay = st['layout']
        cards = dict((side, [None if e['kind'] is None else {'kind': e['kind'], 'name': e['name'],
                                                              'ports': list(e['ports'])} for e in lay[side]])
                     for side in ('adc', 'dac'))
        adc, dac = {}, {}
        for p, d in st['adc_ports'].items():
            e = {'port': int(p), 'block': d.get('block'), 'kind': d['kind']}
            if 'amp' in d:
                e['amp_on'] = _v(d['amp'])
            if d['kind'] == 'rf_in':
                e['att'] = _old_att(d.get('att'))
                e['filter'], e['filter_setting'] = _filter_old(d.get('filter'))
            if 'gain' in d:
                e['gain'] = _v(d['gain'])
            if 'error' in d:
                e['error'] = d['error']
            adc[p] = e
        for p, d in st['dac_ports'].items():
            e = {'port': int(p), 'kind': d['kind']}
            if d['kind'] == 'rf_out':
                e['att'] = _old_att(d.get('att'))
                e['supply_on'] = _v(d.get('supply'))
                e['supply_shared_with'] = (d.get('supply') or {}).get('shared_with')
                e['filter'], e['filter_setting'] = _filter_old(d.get('filter'))
            if 'powered' in d:
                e['powered'] = _v(d['powered'])
            if 'error' in d:
                e['error'] = d['error']
            dac[p] = e
        rec = {'cards': cards, 'gens': lay['gens'], 'ros': lay['ros'], 'adc_ports': adc, 'dac_ports': dac,
               'adc_att': dict((k, _v(v)) for k, v in st['adc_att'].items()),
               'bias': dict((k, _v(v)) for k, v in st['bias'].items()),
               'readback': {'read_from_hardware': B.READBACK['read'], 'last_written': B.READBACK['not_read']},
               'identity': st['identity'], 'card_ids': dict((side, [_v(e.get('id')) for e in lay[side]])
                                                            for side in ('adc', 'dac')),
               'warnings': lay['warnings']}
        if flags:
            rec['flags'] = st['flags']
            rec['flags_list'] = flag_list(st['flags'])
        return rec

    def apply(self, items, settle_s=SETTLE_DEFAULT_S, protect=True):
        """rf_set: the record (ok False when a flag is set at idle or a step failed; the server turns that into
        adc_flags / rf_failed). Limits are checked by the server before."""
        try:
            return self.box._apply_items(items, settle_s=settle_s, protect=protect, by='rf_set', check=False)
        except B.Refused as e:
            return e.record
        except B.RfboxError as e:
            raise _rq(e)

    def clear_flags(self, settle_s=0.0):
        return self.box.clear_flags(settle_s)

    def before_load(self):
        try:
            return self.box.before_load()
        except B.Refused as e:
            raise RfSafeFailed(str(e), e.record)

    def after_load(self, safe_items, settle_s):
        try:
            return self.box.apply_safe(safe_items, settle_s=settle_s, by='rf_set')
        except B.Refused as e:
            rec = e.record or {}
            rec['ok'] = False
            if not rec.get('problems'):
                rec['problems'] = [str(e)]
            return rec
