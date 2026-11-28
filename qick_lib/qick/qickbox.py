"""Schuster Lab fork: one class for the QICK-box (RF board v2) firmware, in a box or on a bare ZCU216 eval board.

QickSoc(bitfile) returns a QickBoxSoc when the bitfile's .hwh has the RF-board control IPs (rfbox_detect.hwh_has_rfb),
so users keep writing QickSoc(...). Construction runs the ordinary QickSoc initialisation, then
rfbox_detect.detect(): only when that confirms a box are the bias DACs, the bias switch, the card GPIO expanders,
the attenuators and the filters initialised, as RFQickSoc216V1 does (same objects, same order), for the slots that
were found. Without a box the object behaves as a plain QickSoc: no card objects, nothing driven beyond the probe,
prepare_round / cleanup_round are QickSoc's, and the rfb_* methods that would drive the board refuse.

    soc = QickSoc('qick_216_rfbv2.bit')          # or QickSoc(..., rfbox={'require': True}) to fail without a box
    soc['rfbox']                                 # inventory: present, method, gate, cards, slots (plain data)
    soc.rfbox                                    # the lab driver (qick.rfbox.RfBox) when a box is present, else None
    QickSoc(..., rfbox=False)                    # skip detection: a plain QickSoc even on an RF-board image
    QickSoc(..., rfbox={'cards': {'adc': {1: 'dc_in'}}})   # declare a card the probe cannot see (see the note)
    QickSoc(..., rfbox={'names': {'gate': ['my_gate_gpio'], 'gate_bit': 0}})   # firmware block names

RFQickSoc216V1 (qick.rfboard) is unchanged for old scripts. slab/notes/box-detection.md has the details.
"""
import logging

from .qick import QickSoc
from .rfboard import (RFQickSoc216V1Mixin, BoardSelection, DacDcCard216, DacRfCard216, DacBalunCard216,
                      AdcDcCard216, AdcRfCard216, AdcBalunCard216)
from .drivers.hardware import BiasDAC11001, GpioMCP23S08
from . import rfbox_detect
from . import rfbox as _rfbox

logger = logging.getLogger(__name__)

CARD_CLASSES = {'dc_out': DacDcCard216, 'rf_out': DacRfCard216, 'balun_out': DacBalunCard216,
                'dc_in': AdcDcCard216, 'rf_in': AdcRfCard216, 'balun_in': AdcBalunCard216}


def rfbox_options(rfbox):
    """QickSoc's rfbox argument as an options dict: None/True/False or a dict."""
    if rfbox is None:
        return {}
    if isinstance(rfbox, bool):
        return {'require': rfbox, 'skip': not rfbox}
    if isinstance(rfbox, dict):
        return dict(rfbox)
    raise TypeError('rfbox must be None, True, False or a dict of options, not %r' % (rfbox,))


class NoBoxError(RuntimeError):
    pass


class QickBoxSoc(_rfbox.RfBoxSocMixin, RFQickSoc216V1Mixin, QickSoc):
    """QickSoc for the RF-board firmware; detects the box and its cards (see the module docstring)."""
    HAS_LO = False

    def __init__(self, bitfile=None, clk_output=None, rfbox=None, **kwargs):
        self._rfbox_options = rfbox_options(rfbox)
        QickSoc.__init__(self, bitfile=bitfile, clk_output=clk_output, rfbox=rfbox, **kwargs)
        self.adc_chains = []
        self.dac_chains = []
        self.biases = []
        self._rfb_config()

    # ---- detection and set-up --------------------------------------------------------------------------------
    @property
    def rfbox_present(self):
        try:
            return bool(self['rfbox']['present'])
        except (KeyError, TypeError):
            return False

    def _rfb_config(self):
        opts = self._rfbox_options
        self.rfbox = None
        self.dac_cards = [None] * 4
        self.adc_cards = [None] * 4
        if opts.get('skip'):
            inv = rfbox_detect.empty_inventory('detection skipped (rfbox=False)')
        else:
            inv = rfbox_detect.detect(self, opts)
        self['rfbox'] = inv
        if not inv['present']:
            self.dac_chains = [None] * 16
            self.adc_chains = [None] * 8
            self._link_channels()
            self['extra_description'].extend(rfbox_detect.describe(inv))
            if opts.get('require'):
                raise NoBoxError('no QICK box found: %s' % inv['reason'])
            return
        self._box_setup(inv)
        self['extra_description'].extend(rfbox_detect.describe(inv))
        self.rfbox = _rfbox.attach(self)

    def _box_setup(self, inv):
        """RFQickSoc216V1Mixin._rfb_config, for the cards in the inventory only (the box is confirmed)."""
        find = rfbox_detect.find_ip
        self.board_sel = BoardSelection(find(self, 'brd_sel_gpio'))
        self.attn_spi = find(self, 'attn_spi')
        self.filter_spi = find(self, 'filter_spi')
        self.bias_spi = find(self, 'bias_spi')
        self.bias_gpio = find(self, 'bias_gpio')

        self.attn_spi.config(lsb="lsb")
        self.filter_spi.config(lsb="msb")
        self.bias_spi.config(lsb="msb", cpha="invert")

        self.biases = [BiasDAC11001(self.bias_spi, ch_en=ii) for ii in range(8)]
        RFQickSoc216V1Mixin.rfb_enable_bias(self)

        for side, cards, chains, nch, off, cs in (('dac', self.dac_cards, self.dac_chains, 4, 0, 4),
                                                  ('adc', self.adc_cards, self.adc_chains, 2, 4, 2)):
            for slot in range(4):
                kind = rfbox_detect.card_kind(inv, side, slot)
                card = None
                if kind is not None:
                    with self.board_sel.enable_context(board_id=slot + off):
                        gpio = GpioMCP23S08(self.filter_spi, ch_en=cs, dev_addr=0, iodir=0xf0)
                        card = CARD_CLASSES[kind](slot, self, gpio)
                cards[slot] = card
                chains.extend([None] * nch if card is None else card.chains)
        self._link_channels()

        # DC-in ADCs need to be restarted after initial power-up; then clear the ADC interrupts (as the library)
        for tile in self['rf']['tiles']['adc']:
            self.rf.restart_adc_tile(tile)
        self.clear_interrupts(error_on_interrupt=False, error_on_persist=False, warn=False)

    def _link_channels(self):
        """gen.rfb_ch / avg_buf.rfb_ch as RFQickSoc216V1 links them; readouts on ADC tiles 0 and 3 (no box port) get
        None (the library would link tile 0 to the wrong card and fail on tile 3)."""
        for gen in self.gens:
            tile, block = [int(a) for a in gen['dac']]
            card = self.dac_cards[tile] if 0 <= tile < 4 else None
            gen.rfb_ch = card.chains[block] if card is not None else None
        for avg_buf in self.avg_bufs:
            tile, block = [int(a) for a in avg_buf.readout['adc']]
            card = self.adc_cards[2 * (tile - 1) + block // 2] if tile in (1, 2) else None
            avg_buf.rfb_ch = card.chains[block % 2] if card is not None else None

    # ---- behaviour without a box: QickSoc's ------------------------------------------------------------------
    def _need_box(self, what):
        if not self.rfbox_present:
            raise NoBoxError('%s: no QICK box was detected (%s)' % (what, self['rfbox'].get('reason')))

    def rfb_enable_bias(self):
        self._need_box('rfb_enable_bias')
        return RFQickSoc216V1Mixin.rfb_enable_bias(self)

    def rfb_disable_bias(self):
        self._need_box('rfb_disable_bias')
        return RFQickSoc216V1Mixin.rfb_disable_bias(self)

    def rfb_set_bias(self, bias_ch, v):
        self._need_box('rfb_set_bias')
        return RFQickSoc216V1Mixin.rfb_set_bias(self, bias_ch, v)

    def rfb_get_bias(self, bias_ch):
        self._need_box('rfb_get_bias')
        return RFQickSoc216V1Mixin.rfb_get_bias(self, bias_ch)

    def prepare_round(self):
        if self.rfbox_present:
            return RFQickSoc216V1Mixin.prepare_round(self)
        return QickSoc.prepare_round(self)

    def cleanup_round(self):
        if self.rfbox_present:
            return RFQickSoc216V1Mixin.cleanup_round(self)
        return QickSoc.cleanup_round(self)


_rfbox.lock_rfb_methods(QickBoxSoc)
