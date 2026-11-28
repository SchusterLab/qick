"""A fake ZCU216 QICK box for qick.rfbox: the cards as physical state behind objects shaped like the library's.

Standard library only, Python 3.8. Stable import path: qick.rfbox_fake (or rfbox_fake next to rfbox.py). Another
repository's tests pin this module; the interface below is kept steady:

    box = FakeQickBox(layout=None, threshold_db=10.0, gpio_defaults_land=False, library='0.2.450')
    soc = box.make_soc(cfg=None)        # a "bitstream load + RFQickSoc216V1 construction": a new soc object
    rf = rfbox.attach(soc)              # the real driver over the fake objects
    box.load_image()                    # the same as make_soc(): returns the new soc

    box.dsa[block]                      dB of each RF-ADC step attenuator (assignable)
    box.amp[port]                       {adc port: amplifier on} from the expander pins (read-only view)
    box.att(side, port)                 the physical PE43705 values (the driver cannot read these)
    box.filters[(side, port)]           {'WR0_SW', 'WR0_FILTER'} (assignable)
    box.bias_volts()                    the eight bias outputs, V
    box.latched[block], box.inject[block] |= word   latched interrupt words; a persistent injected condition
    box.events                          every new interrupt latch: {block, word, why, t}
    box.calls                           every write that reached a chip: (chip, side, port, ...)
    box.lost                            every write made while its card was not selected
    box.fail(chip, side=None, port=None, times=1, exc=None)   the next matching write raises
    box.stuck.add((side, slot))         that card's GPIO expander ignores writes
    box.filter_mode[(side, port)] = 'zero' | 'ones' | 'raise'   that filter chip reads 0x00 / 0xFF / fails to read
    box.pl_loads

What it models, and from where:
  - the library's object graph (rfboard.py 0.2.360 / 0.2.450): adc_cards / dac_cards, chains with attn, filter,
    gain, powerup / powerdown nets, SwitchControl with net2port and the MCP23S08 devices, BoardSelection,
    BiasDAC11001 objects, the RFDC's set/get_adc_attenuator, and the xrfdc interrupt calls (soc.rfbox_xrfdc);
  - card selection: a chip ignores writes and reads 0xFF while its card is not selected, so the constructor's
    nested FilterChain.init_filter selection loses the attenuator writes of the later chains, as the library code
    implies (not measured);
  - the constructor sequence of RFQickSoc216V1Mixin._rfb_config, including SwitchControl.add_MCP's mirrored GPIO
    defaults when gpio_defaults_land=True. The default (False) follows the measurement on an rfbv2 box: an amplifier
    left on by the previous image stays on through start-up;
  - measured on an rfbv2 box: a load puts every RF-ADC step attenuator at 0 dB; it reads back what was set
    (integer dB); an RF In amplifier on with the ADC attenuator below threshold_db latches over-range, and with the
    ADC attenuator at 0 dB and the card attenuator at 0 dB also over-voltage (the threshold is a model, not a
    measurement);
  - from the card schematics: RF Out channels share a 5 V rail per pair (the library's net assignment
    RFOUT5V0_EN((ch + 1) % 2): channels 0 and 2 on EN1, 1 and 3 on EN0); PE43705 has no readback.
"""
import json
import time
from contextlib import contextmanager

try:
    from . import rfbox as R
except ImportError:      # copied next to rfbox.py
    import rfbox as R

ATT_MAX = R.ATT_MAX

# the example population from the measurement side: ADC slot 2 RF In (readout 0 on ADC port 4, block 20); DAC slots
# 0 and 1 RF Out (generators 0 and 1), DAC slot 2 DC Out; other slots empty
EXAMPLE_LAYOUT = {'adc': [None, None, 'rf_in', None], 'dac': ['rf_out', 'rf_out', 'dc_out', None]}

ID_OF = {'dac': {'dc_out': 1, 'rf_out': 3, 'balun_out': 6}, 'adc': {'dc_in': 0, 'rf_in': 2, 'balun_in': 5}}
EMPTY_ID = 15                       # an empty slot reads all ones (assumed pull-ups; question for the hardware)


def example_cfg(layout=None, library='0.2.450'):
    """A minimal soccfg for a layout: a generator on block 0 of every DAC slot with a card, a readout on block 0 of
    every ADC slot with a card (ADC blocks of both ports of each card in rf.adcs)."""
    lay = _norm_layout(layout or EXAMPLE_LAYOUT)
    gens = [{'dac': '%d0' % slot} for slot, k in enumerate(lay['dac']) if k]
    ros, adcs = [], {}
    for slot, k in enumerate(lay['adc']):
        if not k:
            continue
        for ch in range(2):
            port = 2 * slot + ch
            name = R.adc_block_of_port(port)
            adcs[name] = {'index': [int(name[0]), int(name[1])]}
        ros.append({'adc': R.adc_block_of_port(2 * slot)})
    return {'board': 'ZCU216', 'sw_version': library, 'gens': gens, 'readouts': ros,
            'rf': {'adcs': adcs}, 'extra_description': []}


def _norm_layout(layout):
    out = {}
    for side in ('adc', 'dac'):
        slots = list((layout or {}).get(side) or [])
        slots += [None] * (4 - len(slots))
        out[side] = [(s.get('kind') if isinstance(s, dict) else s) for s in slots[:4]]
    return out


def _version(v):
    try:
        return tuple(int(x) for x in str(v).split('.')[:3])
    except ValueError:
        return (0, 2, 450)


# ---- chips ------------------------------------------------------------------------------------------------------
class _Chip(object):
    def __init__(self, box, side, slot):
        self.box, self.side, self.slot = box, side, slot
        self.board_id = slot + (0 if side == 'dac' else 4)

    def _on(self):
        return self.box.selected == self.board_id

    def _write(self, what, fn, *info):
        self.box._fault(what, self.side, info[0] if info else None)
        if self._on():
            fn()
            self.box.calls.append((what, self.side) + tuple(info))
        else:
            self.box.lost.append((what, self.side) + tuple(info))


class FakeMCP23S08(_Chip):
    """GpioMCP23S08 of one card: pins 0-3 outputs (IODIR 0xf0), pins 4-7 the card ID."""

    def __init__(self, box, side, slot, iodir=0xf0):
        super().__init__(box, side, slot)
        self.outputs = [i for i in range(8) if (1 << i) & iodir == 0]
        self.write_reg('IODIR_REG', iodir)

    def read_reg(self, reg):
        if not self._on():
            return 0xFF
        self.box._fault('gpio_read', self.side, self.slot)
        s = self.box.slots[(self.side, self.slot)]
        if reg in ('GPIO_REG', 'OLAT_REG'):
            return ((s['id'] << 4) | (s['gpio'] & 0x0F)) if reg == 'GPIO_REG' else s['gpio']
        if reg == 'IODIR_REG':
            return s['iodir']
        return 0

    def write_reg(self, reg, val):
        s = self.box.slots[(self.side, self.slot)]
        if reg in ('GPIO_REG', 'OLAT_REG') and (self.side, self.slot) in self.box.stuck:
            self.box.lost.append(('gpio stuck', self.side, self.slot, val))
            return

        def do():
            if reg in ('GPIO_REG', 'OLAT_REG'):
                s['gpio'] = val & 0x0F
                self.box.update('gpio write')
            elif reg == 'IODIR_REG':
                s['iodir'] = val
        self._write('gpio', do, self.slot, reg, val)

    def set_bits(self, bits, val):          # GpioMCP23S08.set_bits
        if val not in [0, 1]:
            raise RuntimeError('invalid value:', val)
        reg = self.read_reg('GPIO_REG')
        for bit in bits:
            if bit not in self.outputs:
                raise RuntimeError('tried to set output %d, but only pins %s are configured as outputs'
                                   % (bit, self.outputs))
            if val == 1:
                reg |= (1 << bit)
            else:
                reg &= ~(1 << bit)
        self.write_reg('GPIO_REG', reg)


class FakeSwitchControl(object):
    """SwitchControl, with add_MCP's MSB-first defaults as in hardware.py."""

    def __init__(self, box):
        self.box = box
        self.devs = []
        self.net2port = {}

    def add_MCP(self, gpio, outputs):
        defaults = 0
        for i, output in enumerate(outputs):
            defaults <<= 1
            if output is not None:
                net, dv = output
                self.net2port[net] = (len(self.devs), i)
                defaults += dv
        if self.box.gpio_defaults_land:
            gpio.write_reg('GPIO_REG', defaults)
        else:
            self.box.lost.append(('gpio defaults (not landing, as measured)', gpio.side, gpio.slot, defaults))
        self.devs.append(gpio)

    def __setitem__(self, net, val):
        dev, bit = self.net2port[net]
        self.devs[dev].set_bits(bits=[bit], val=val)

    def __contains__(self, key):
        return key in self.net2port


class FakePE43705(_Chip):
    def __init__(self, box, side, slot, ch, stage):
        super().__init__(box, side, slot)
        self.ch, self.stage = ch, stage
        self.set_att(ATT_MAX)

    def set_att(self, db):
        if db < 0 or db > ATT_MAX:
            raise RuntimeError('attenuation value %f out of range [%f, %f]' % (db, 0, ATT_MAX))
        reg = int(round(db / 0.25))
        port = self.box.port(self.side, self.slot, self.ch)

        def do():
            self.box._att[(self.side, port, self.stage)] = reg * 0.25
            self.box.update('attenuator')
        self._write('attn', do, port, self.stage, reg * 0.25)
        return reg * 0.25


class FakeADMV8818(_Chip):
    def __init__(self, box, side, slot, ch):
        super().__init__(box, side, slot)
        self.ch = ch

    def _regs(self):
        return self.box._filters[(self.side, self.box.port(self.side, self.slot, self.ch))]

    def write_reg(self, reg, value):
        port = self.box.port(self.side, self.slot, self.ch)
        self._write('filter', lambda: self._regs().__setitem__(reg, int(value)), port, reg, value)

    def read_reg(self, reg):
        if not self._on():
            return 0xFF
        mode = self.box.filter_mode.get((self.side, self.box.port(self.side, self.slot, self.ch)))
        if mode == 'zero':
            return 0x00
        if mode == 'ones':
            return 0xFF
        if mode == 'raise':
            raise RuntimeError('fake board: filter SPI read failed')
        fixed = {'PRODUCT_ID_L': 0x18, 'PRODUCT_ID_H': 0x88, 'CHIPTYPE': 0x01}
        return int(fixed[reg] if reg in fixed else self._regs().get(reg, 0))


class FakeLMH6401(_Chip):
    def __init__(self, box, side, slot, ch):
        super().__init__(box, side, slot)
        self.ch = ch
        self.set_gain(-6)

    def set_gain(self, db):
        if db < R.GAIN_MIN or db > R.GAIN_MAX:
            raise RuntimeError('gain %f out of limits' % db)
        regval = int(round(R.GAIN_MAX - db))
        port = self.box.port(self.side, self.slot, self.ch)
        self._write('gain', lambda: self.box._gain.__setitem__(port, regval), port, R.GAIN_MAX - regval)
        return R.GAIN_MAX - regval

    def get_gain(self):
        if not self._on():
            return R.GAIN_MAX - 0xFF
        return R.GAIN_MAX - self.box._gain.get(self.box.port(self.side, self.slot, self.ch), 0)


class FakeDAC11001(object):
    def __init__(self, box, i):
        self.box, self.i = box, i
        self.box.calls.append(('bias config', 'bias', i, 0x4e00))
        self.set_volt(0)

    def set_volt(self, volt):
        if volt < -10 or volt > 10:
            raise RuntimeError('BiasDAC11001: %d V out of range' % volt)
        d = int(round(2 ** 20 * (volt + 10) / 20.0))
        if d == 2 ** 20:
            d -= 1
        self.box._fault('bias', 'bias', self.i)
        self.box._bias[self.i] = d << 4
        self.box.calls.append(('bias', 'bias', self.i, volt))
        return d * 20.0 / 2 ** 20 - 10

    def get_volt(self):
        return (self.box._bias[self.i] >> 4) * 20.0 / 2 ** 20 - 10


class FakeBoardSelection(object):
    def __init__(self, box):
        self.box = box

    def enable(self, board_id=0):
        if self.box.selected is not None and self.box.selected != board_id:
            # another card is selected: two threads interleaving on the shared SPI / selection GPIO
            self.box.select_conflicts += 1
        self.box.selected = board_id

    def disable(self):
        self.box.selected = None

    @contextmanager
    def enable_context(self, board_id):
        try:
            self.enable(board_id)
            yield None
        finally:
            self.disable()


class FakeSpi(object):
    """filter_spi, only for the MCP23S08 ID read of an empty slot."""

    def __init__(self, box):
        self.box = box

    def send_receive_m(self, data, ch_en, cs_t='pulse'):
        b = self.box
        if b.selected is None or len(data) != 3 or data[0] != 0x41 or data[1] != 0x09:
            return bytes(len(data))
        side, slot = ('dac', b.selected) if b.selected < 4 else ('adc', b.selected - 4)
        if ch_en != (4 if side == 'dac' else 2):
            return bytes([0, 0, 0xFF])
        s = b.slots.get((side, slot))
        return bytes([0, 0, (s['id'] << 4) | (s['gpio'] & 0x0F)])


class _Channel(object):
    def __init__(self, box):
        self.box = box

    def write(self, val, mask):
        self.box.bias_enabled = bool(val & mask & 1)
        self.box.calls.append(('bias_gpio', 'bias', val, mask))


class FakeAxiGpio(object):
    def __init__(self, box):
        self.channel1 = _Channel(box)


class _Ffi(object):
    @staticmethod
    def new(spec):
        return [0]


class _Lib(object):
    XRFDC_ADC_TILE = 0

    def __init__(self, box):
        self.box = box

    def XRFdc_GetIntrStatus(self, inst, typ, tile, block, arr):
        name = self.box._block_of_index.get((tile, block))
        if name is None:
            return 1
        self.box.update('flags read')
        arr[0] = self.box.latched.get(name, 0)
        return 0

    def XRFdc_IntrClr(self, inst, typ, tile, block, mask):
        name = self.box._block_of_index.get((tile, block))
        if name is None:
            return 1
        self.box.latched[name] &= ~mask & 0xFFFFFFFF
        return 0


class FakeXrfdc(object):
    """The part of the xrfdc module rfbox uses."""

    def __init__(self, box):
        self._ffi = _Ffi()
        self._lib = _Lib(box)


class FakeRfdc(object):
    def __init__(self, box, library):
        self.box = box
        self._instance = object()
        self._library = library

    def set_adc_attenuator(self, blockname, attenuation):
        self.box._fault('dsa', 'adc', blockname)
        if blockname not in self.box.dsa:
            raise KeyError(blockname)
        self.box.dsa[blockname] = float(round(attenuation))
        self.box.calls.append(('dsa', 'adc', blockname, float(round(attenuation))))
        self.box.update('dsa_set')
        return None if self._library < (0, 2, 400) else float(round(attenuation))

    def get_adc_attenuator(self, blockname):
        return self.box.dsa[blockname]


# ---- chains and cards (rfboard.py shapes) ----------------------------------------------------------------------
class FakeChain(object):
    def __init__(self, soc, card, global_ch, card_num, card_ch):
        self.soc, self.card, self.global_ch, self.card_num, self.card_ch = soc, card, global_ch, card_num, card_ch


class FakeFilterChain(FakeChain):
    def init_filter(self):
        with self.soc.board_sel.enable_context(self.card_num):
            self.filter.write_reg('ADI_SPI_CONFIG_A', 0x3C)

    def read_filter(self, reg=''):
        with self.soc.board_sel.enable_context(self.card_num):
            return self.filter.read_reg(reg)


class FakeDacRfChain(FakeFilterChain):
    def __init__(self, soc, card, global_ch, card_num, card_ch):
        super().__init__(soc, card, global_ch, card_num, card_ch)
        box = card.box
        self.powerup = 'RFOUT5V0_EN%d' % ((card_ch + 1) % 2)
        self.attn = [FakePE43705(box, 'dac', card.card_num, card_ch, i) for i in range(2)]
        self.filter = FakeADMV8818(box, 'dac', card.card_num, card_ch)
        self.init_filter()

    def enable_rf(self, att1, att2):
        with self.soc.board_sel.enable_context(self.card_num):
            self.card.switch_control[self.powerup] = 1
            return self.attn[0].set_att(att1), self.attn[1].set_att(att2)

    def disable(self):
        with self.soc.board_sel.enable_context(self.card_num):
            self.attn[0].set_att(31.75)
            self.attn[1].set_att(31.75)


class FakeAdcRfChain(FakeFilterChain):
    def __init__(self, soc, card, global_ch, card_num, card_ch):
        super().__init__(soc, card, global_ch, card_num, card_ch)
        box = card.box
        self.powerup = 'RFIN5V0CH%d_EN' % card_ch
        self.attn = [FakePE43705(box, 'adc', card.card_num, card_ch, 0)]
        self.filter = FakeADMV8818(box, 'adc', card.card_num, card_ch)
        self.init_filter()

    def enable_rf(self, att):
        with self.soc.board_sel.enable_context(self.card_num):
            self.card.switch_control[self.powerup] = 1
            return self.attn[0].set_att(att)

    def disable(self):
        with self.soc.board_sel.enable_context(self.card_num):
            self.card.switch_control[self.powerup] = 0
            self.attn[0].set_att(31.75)


class FakeDacDcChain(FakeChain):
    def __init__(self, soc, card, global_ch, card_num, card_ch):
        super().__init__(soc, card, global_ch, card_num, card_ch)
        self.powerdown = 'PD%d' % card_ch

    def enable_dc(self):
        with self.soc.board_sel.enable_context(self.card_num):
            self.card.switch_control[self.powerdown] = 0

    def disable(self):
        with self.soc.board_sel.enable_context(self.card_num):
            self.card.switch_control[self.powerdown] = 1


class FakeAdcDcChain(FakeChain):
    def __init__(self, soc, card, global_ch, card_num, card_ch):
        super().__init__(soc, card, global_ch, card_num, card_ch)
        self.powerdown = 'PD%d' % card_ch
        self.gain = FakeLMH6401(card.box, 'adc', card.card_num, card_ch)

    def enable_dc(self, gain):
        with self.soc.board_sel.enable_context(self.card_num):
            self.card.switch_control[self.powerdown] = 0
            return self.gain.set_gain(gain)

    def disable(self):
        with self.soc.board_sel.enable_context(self.card_num):
            self.card.switch_control[self.powerdown] = 1

    def get_gain(self):
        with self.soc.board_sel.enable_context(self.card_num):
            return self.gain.get_gain()


class FakeBalunChain(FakeChain):
    pass


CARD_CLASSES = {
    'rf_out': ('RF Out', 4, 0, FakeDacRfChain, [('RFOUT5V0_EN%d' % i, 0) for i in range(2)] + [None] * 2),
    'dc_out': ('DC Out', 4, 0, FakeDacDcChain, [('PD%d' % i, 0) for i in range(4)]),
    'balun_out': ('Balun Out', 4, 0, FakeBalunChain, [None] * 4),
    'rf_in': ('RF In', 2, 4, FakeAdcRfChain, [('RFIN5V0CH%d_EN' % i, 0) for i in range(2)] + [None] * 2),
    'dc_in': ('DC In', 2, 4, FakeAdcDcChain, [('PD%d' % i, 1) for i in range(2)] + [None] * 2),
    'balun_in': ('Balun In', 2, 4, FakeBalunChain, [None] * 4),
}


class FakeCard(object):
    def __init__(self, box, kind, card_num, soc, gpio):
        self.box = box
        self.kind = kind
        self.NAME, self.NCH, self.CARDNUM_OFFSET, chain_cls, self.GPIO_OUTPUTS = CARD_CLASSES[kind]
        self.card_num = card_num
        self.soc = soc
        self.switch_control = FakeSwitchControl(box)
        self.switch_control.add_MCP(gpio, self.GPIO_OUTPUTS)
        self.chains = []
        for ch in range(self.NCH):
            self.chains.append(chain_cls(soc=soc, card=self, global_ch=self.NCH * card_num + ch,
                                         card_num=self.CARDNUM_OFFSET + card_num, card_ch=ch))


class FakeRfSoc(object):
    """A stand-in for an RFQickSoc216V1 when no other fake soc is used: the cfg as items, the RF attributes."""

    def __init__(self, cfg):
        self._cfg = json.loads(json.dumps(cfg))

    def __getitem__(self, k):
        return self._cfg[k]

    def __setitem__(self, k, v):
        self._cfg[k] = v

    def __contains__(self, k):
        return k in self._cfg

    def get(self, k, default=None):
        return self._cfg.get(k, default)

    def get_cfg(self):
        return self._cfg


# ---- views for tests --------------------------------------------------------------------------------------------
class _Filters(object):
    def __init__(self, box):
        self.box = box

    def __getitem__(self, key):
        r = self.box._filters[key]
        return {'WR0_SW': r.get('WR0_SW', 0), 'WR0_FILTER': r.get('WR0_FILTER', 0)}

    def __setitem__(self, key, regs):
        self.box._filters[key].update(regs)

    def __contains__(self, key):
        return key in self.box._filters

    def keys(self):
        return self.box._filters.keys()


# ---- the box ----------------------------------------------------------------------------------------------------
class FakeQickBox(object):
    def __init__(self, layout=None, threshold_db=10.0, gpio_defaults_land=False, library='0.2.450', ids=None):
        self.threshold_db = float(threshold_db)
        self.gpio_defaults_land = bool(gpio_defaults_land)
        self.library = library
        self.ids = dict(ids or {})
        self.selected = None
        self.select_conflicts = 0
        self.slots = {}
        self._att, self._filters, self._gain = {}, {}, {}
        self._bias = [0] * 8
        self.bias_enabled = False
        self.dsa, self.latched, self.inject = {}, {}, {}
        self._block_of_index = {}
        self.events, self.calls, self.lost = [], [], []
        self.faults = []
        self.stuck = set()
        self.filter_mode = {}       # (side, port) -> 'zero' | 'ones' | 'raise': an ADMV8818 that does not answer
        self.pl_loads = 0
        self.soc = None
        self.configure(layout or EXAMPLE_LAYOUT)

    # -- physical layout --
    @staticmethod
    def port(side, slot, ch):
        return (4 if side == 'dac' else 2) * slot + ch

    def configure(self, layout, blocks=None):
        """Install cards (kinds per slot; {'kind': ...} dicts accepted). Physical state of cards already present is
        kept; a new card starts with its rails off and its attenuators at 31.75 dB."""
        lay = _norm_layout(layout)
        self.layout = lay
        for side in ('adc', 'dac'):
            for slot, kind in enumerate(lay[side]):
                old = self.slots.get((side, slot))
                cid = self.ids.get((side, slot), ID_OF[side].get(kind, EMPTY_ID) if kind else EMPTY_ID)
                if old is None or old['kind'] != kind:
                    self.slots[(side, slot)] = {'kind': kind, 'id': cid, 'gpio': 0, 'iodir': 0xFF}
                    n = 4 if side == 'dac' else 2
                    for ch in range(n):
                        p = self.port(side, slot, ch)
                        for st in (0, 1):
                            self._att[(side, p, st)] = ATT_MAX
                        self._filters[(side, p)] = {'WR0_SW': 0xC0, 'WR0_FILTER': 0}
                        self._gain[p] = 32 if side == 'adc' else 0
                else:
                    old['id'] = cid
        for b in blocks or []:
            self.dsa.setdefault(b, 0.0)
            self.latched.setdefault(b, 0)

    def kind(self, side, port):
        return self.layout[side][port // (4 if side == 'dac' else 2)]

    def att(self, side, port, stage=None):
        if side == 'dac':
            return [self._att[('dac', port, 0)], self._att[('dac', port, 1)]] if stage is None \
                else self._att[('dac', port, stage)]
        return self._att[('adc', port, 0)]

    @property
    def filters(self):
        return _Filters(self)

    @property
    def amp(self):
        out = {}
        for slot, kind in enumerate(self.layout['adc']):
            if kind not in ('rf_in', 'dc_in'):
                continue
            g = self.slots[('adc', slot)]['gpio']
            for ch in range(2):
                bit = bool((g >> ch) & 1)
                out[self.port('adc', slot, ch)] = bit if kind == 'rf_in' else not bit
        return out

    def supply(self, port):
        """RF Out: the expander pin of the rail of DAC port `port`."""
        slot, ch = port // 4, port % 4
        return bool((self.slots[('dac', slot)]['gpio'] >> ((ch + 1) % 2)) & 1)

    def dc_powered(self, port):
        slot, ch = port // 4, port % 4
        return not bool((self.slots[('dac', slot)]['gpio'] >> ch) & 1)

    def bias_volts(self):
        return [(r >> 4) * 20.0 / 2 ** 20 - 10 for r in self._bias]

    # -- faults --
    def fail(self, chip, side=None, port=None, times=1, exc=None):
        """The next `times` writes to `chip` ('attn', 'filter', 'gpio', 'gain', 'bias', 'dsa'), optionally on one
        side / port (gpio: slot), raise exc (default RuntimeError)."""
        self.faults.append({'chip': chip, 'side': side, 'port': port, 'times': times,
                            'exc': exc or RuntimeError('fake board: injected %s failure' % chip)})

    def _fault(self, chip, side, port):
        for f in self.faults:
            if f['chip'] == chip and f['times'] > 0 and (f['side'] in (None, side)) and (f['port'] in (None, port)):
                f['times'] -= 1
                raise f['exc']

    # -- physics --
    def condition(self, block):
        p = R.adc_port_of_block(block)
        w = self.inject.get(block, 0)
        if p is None or self.kind('adc', p) != 'rf_in' or not self.amp.get(p):
            return w
        dsa = self.dsa.get(block, 0.0)
        if dsa < self.threshold_db:
            w |= R.OVR_RANGE
            if dsa == 0 and self._att[('adc', p, 0)] == 0:
                w |= R.OVR_VOLTAGE
        return w

    def update(self, why):
        for b in self.dsa:
            w = self.condition(b)
            if w & ~self.latched.get(b, 0):
                self.events.append({'block': b, 'word': w, 'why': why, 't': time.monotonic()})
            self.latched[b] = self.latched.get(b, 0) | w

    def flags(self, block, clear):
        """The latched word of a block (and clear it): what FakeSoc.prepare_round and tests use."""
        self.update('flags read')
        w = self.latched.get(block, 0)
        if clear:
            self.latched[block] = 0
        return w

    def pl_load(self):
        """A bitstream download: every RF-ADC step attenuator to 0 dB; the cards are not touched."""
        self.pl_loads += 1
        for b in self.dsa:
            self.dsa[b] = 0.0
        self.update('PL load %d' % self.pl_loads)

    # -- a soc --
    def make_soc(self, cfg=None, target=None, download=True):
        """A bitstream load (download) and an RFQickSoc216V1 construction over the cards: returns the new soc
        (target: an existing fake soc object, e.g. the board server's FakeSoc, to give the RF attributes to)."""
        cfg = cfg if cfg is not None else example_cfg(self.layout, self.library)
        soc = target if target is not None else FakeRfSoc(cfg)
        adcs = (cfg.get('rf') or {}).get('adcs') or {}
        self._block_of_index = dict(((int(c['index'][0]), int(c['index'][1])), n) for n, c in adcs.items())
        for n in adcs:
            self.dsa.setdefault(n, 0.0)
            self.latched.setdefault(n, 0)
        if download:
            self.pl_load()
        self._construct(soc)
        self.soc = soc
        return soc

    load_image = make_soc

    def _construct(self, soc):
        lib = _version(self.library)
        soc.rfbox_xrfdc = FakeXrfdc(self)
        soc.rf = FakeRfdc(self, lib)
        soc.board_sel = FakeBoardSelection(self)
        soc.filter_spi = FakeSpi(self)
        soc.bias_gpio = FakeAxiGpio(self)
        soc.biases = [FakeDAC11001(self, i) for i in range(8)]
        soc.bias_gpio.channel1.write(1, 0x1)                          # rfb_enable_bias
        known = {'dac': {1: 'dc_out', 3: 'rf_out'}, 'adc': {0: 'dc_in', 2: 'rf_in'}}
        if lib >= (0, 2, 400):
            known['dac'][6], known['adc'][5] = 'balun_out', 'balun_in'
        for side, attr, ch_en in (('dac', 'dac_cards', 4), ('adc', 'adc_cards', 2)):
            cards = []
            for slot in range(4):
                bid = slot + (0 if side == 'dac' else 4)
                with soc.board_sel.enable_context(bid):
                    gpio = FakeMCP23S08(self, side, slot)
                    cid = gpio.read_reg('GPIO_REG') >> 4
                    kind = known[side].get(cid)
                    card = FakeCard(self, kind, slot, soc, gpio) if kind else None
                cards.append(card)
            setattr(soc, attr, cards)
        if lib >= (0, 2, 400):
            soc.dac_chains = [c for card in soc.dac_cards for c in (card.chains if card else [None] * 4)]
            soc.adc_chains = [c for card in soc.adc_cards for c in (card.chains if card else [None] * 2)]
            for b in self.latched:                                    # restart tiles, clear_interrupts
                self.latched[b] = 0
            self.update('constructor')
        try:
            desc = soc['extra_description']
        except Exception:      # noqa: BLE001
            desc = None
        # card lines only if the cfg does not have them yet (a cfg saved from a real or earlier fake soc does)
        if isinstance(desc, list) and not any(R._CARD.search(str(x)) for x in desc):
            desc.append('\nQICK box daughter cards detected:')
            for side, label in (('adc', 'ADC'), ('dac', 'DAC')):
                for slot, card in enumerate(getattr(soc, side + '_cards')):
                    if card is None:
                        desc.append('\t%s slot %d: No card detected' % (label, slot))
                    else:
                        desc.append('\t%s slot %d: %s card has ports %s'
                                    % (label, slot, card.NAME, [c.global_ch for c in card.chains]))
