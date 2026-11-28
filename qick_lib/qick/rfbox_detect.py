"""Schuster Lab fork: decide whether a ZCU216 running a QICK-box (RF board v2) image is inside a QICK box, and which
daughter cards sit in which slots, without driving anything a bare eval board could mind.

Standard library only, Python 3.8, importable without pynq (QickConfig on a PC reads the inventory it produces).
The board side is qick.qickbox.QickBoxSoc, which QickSoc() returns for an RF-board image; slab/notes/box-detection.md
explains every decision and lists what is unverified on hardware.

    detect(soc, options=None) -> inventory     # plain JSON-able dict, stored as soc['rfbox']
    describe(inventory) -> [lines]             # what the board description prints
    hwh_has_rfb(hwh_path) -> bool              # does a hardware description carry the RF-board control IPs
    firmware_identity(soc, bitfile, options)   # fw_id_gpio word (if the firmware has one) and the lab build name

Order of decisions (detect):
  1. firmware has no RF-board control IPs -> 'no rf-board control' (nothing is touched).
  2. firmware has a passive presence input -> read it with the output gate closed and decide from it alone. Absent:
     no box, the gate stays closed, no SPI transaction is made. Present: open the gate and probe the slots (only to
     list the cards).
  3. firmware has an output gate but no presence input -> open the gate, probe; no slot confirmed: close the gate
     again, no box.
  4. firmware without a gate (stock rfbv2 images) -> probe directly (the pins are driven from the bitstream load on).
The slot probe (probe_slot) is read-first: it reads nine MCP23S08 registers twice, with the dummy byte 0x00 and then
0xFF, and accepts the slot only if the answers are independent of the dummy byte and match the expander's register
signature; then it confirms with a write-then-read of DEFVAL (no external effect while GPINTEN = 0, which it has just
read) and restores DEFVAL. Nothing in the probe writes IODIR, GPIO, OLAT, an attenuator, a bias DAC or the bias
switch: those happen in QickBoxSoc only after the box is confirmed.
"""
import copy
import json
import os
import xml.etree.ElementTree as ET

INVENTORY_VERSION = 1

# names of the firmware blocks; the gate / presence / fw_id names are those the parallel firmware task is expected to
# use and can be overridden with options['names'] (QickSoc(..., rfbox={'names': {...}}))
DEFAULT_NAMES = {
    # every one of these must be in the hwh for an image to count as an RF-board image (RFQickSoc216V1Mixin's set)
    'control': ['brd_sel_gpio', 'filter_spi', 'attn_spi', 'bias_spi', 'bias_gpio'],
    'gate': ['rfb_gate_gpio'],          # AXI GPIO: one bit enables the FPGA outputs to the RF board
    'gate_channel': 1,
    'gate_bit': 0,
    'gate_open_level': 1,               # value of the bit that opens the gate
    'presence': ['rfb_presence_gpio'],  # AXI GPIO input, readable with the gate closed (may be the gate IP itself)
    'presence_channel': 1,
    'presence_bit': 0,
    'presence_level': 1,                # value of the bit that means "box present"
    'fw_id': ['fw_id_gpio'],            # AXI GPIO input: 32-bit build ID
    'fw_id_channel': 1,
}

# the 4-bit card IDs (GPIO pins 4-7 of the card's MCP23S08): rfboard.py RFQickSoc216V1Mixin._rfb_config
CARD_IDS = {'dac': {1: 'dc_out', 3: 'rf_out', 6: 'balun_out'}, 'adc': {0: 'dc_in', 2: 'rf_in', 5: 'balun_in'}}
KIND_NAMES = {'rf_out': 'RF Out', 'dc_out': 'DC Out', 'balun_out': 'Balun Out',
              'rf_in': 'RF In', 'dc_in': 'DC In', 'balun_in': 'Balun In'}
SIDES = {'dac': {'board_offset': 0, 'nch': 4, 'mcp_cs': 4}, 'adc': {'board_offset': 4, 'nch': 2, 'mcp_cs': 2}}
FILTER_KINDS = ('rf_out', 'rf_in')

# MCP23S08 (Microchip DS21919): opcode 0100 0 A1 A0 R/W; A1/A0 are ignored while IOCON.HAEN = 0 (reset)
MCP_RD, MCP_WR = 0x41, 0x40
MCP_REGS = (('IODIR', 0x00), ('IPOL', 0x01), ('GPINTEN', 0x02), ('DEFVAL', 0x03), ('INTCON', 0x04),
            ('IOCON', 0x05), ('GPPU', 0x06), ('GPIO', 0x09), ('OLAT', 0x0A))
MCP_DEFVAL = 0x03
ECHO_PATTERNS = (0xA5, 0x5A)
# ADMV8818 (ADI SPI, 16-bit address, bit 15 = read): PRODUCT_ID 0x8818 in registers 0x005 (high) / 0x004 (low)
ADMV_CONFIG_A, ADMV_CHIPTYPE, ADMV_PID_L, ADMV_PID_H = 0x000, 0x003, 0x004, 0x005
ADMV_PRODUCT_ID = 0x8818
ADMV_CONFIG_A_4WIRE = 0x3C          # what the library writes (FilterChain.init_filter): SDO active, mirrored


class DetectError(RuntimeError):
    pass


def merged_names(options=None):
    names = copy.deepcopy(DEFAULT_NAMES)
    for k, v in ((options or {}).get('names') or {}).items():
        if k not in names:
            raise ValueError('unknown rfbox name option %r (known: %s)' % (k, ', '.join(sorted(names))))
        if isinstance(names[k], list):
            names[k] = [v] if isinstance(v, str) else list(v)
        else:
            names[k] = int(v)
    return names


# ---- the hardware description ----------------------------------------------------------------------------------
def hwh_ip_paths(hwh_path):
    """Full paths ('rfb_control/filter_spi') of every MODULE in a .hwh file."""
    out = []
    for _, el in ET.iterparse(hwh_path, events=('start',)):
        if el.tag == 'MODULE':
            fn = el.get('FULLNAME') or el.get('INSTANCE') or ''
            out.append(fn.lstrip('/'))
    return out


def _match(paths, name):
    return [p for p in paths if p == name or p.endswith('/' + name)]


def hwh_has_rfb(hwh_path, options=None):
    """True when every RF-board control IP (DEFAULT_NAMES['control']) is in the hardware description."""
    names = merged_names(options)
    try:
        paths = hwh_ip_paths(hwh_path)
    except (OSError, ET.ParseError):
        return False
    return all(_match(paths, n) for n in names['control'])


def find_ip(soc, name):
    """The driver object of the IP called `name` (last path component or full path), or None."""
    ip_dict = getattr(soc, 'ip_dict', None) or {}
    for path in sorted(_match(list(ip_dict), name), key=len):
        obj = soc
        try:
            for part in path.split('/'):
                obj = getattr(obj, part)
            return obj
        except AttributeError:
            continue
    for holder in (soc, getattr(soc, 'rfb_control', None)):
        if holder is not None:
            obj = getattr(holder, name, None) if name.isidentifier() else None
            if obj is not None:
                return obj
    return None


def _first_ip(soc, names):
    for n in names:
        obj = find_ip(soc, n)
        if obj is not None:
            return n, obj
    return None, None


def _channel(gpio, n):
    return getattr(gpio, 'channel%d' % int(n))


# ---- the bus ---------------------------------------------------------------------------------------------------
class Bus(object):
    """The daughter-card SPI (filter_spi) and the card selection (brd_sel_gpio), at the level the library uses:
    spi.send_receive_m(bytes, ch_en, cs_t) and BoardSelection's 4-bit word (bit 3 enable, bits 2-0 card).
    Every transaction is logged in self.log as (op, board_id, cs, bytes_out, bytes_in)."""

    def __init__(self, spi, sel_gpio):
        self.spi, self.sel = spi, sel_gpio
        self.log = []

    def _select(self, board_id):
        self.sel.channel1.write((1 << 3) + int(board_id), 0xf)

    def _deselect(self):
        self.sel.channel1.write(0, 0xf)

    def xfer(self, board_id, cs, data, op):
        self._select(board_id)
        try:
            res = bytes(self.spi.send_receive_m(bytes(data), cs, ''))
        finally:
            self._deselect()
        self.log.append((op, board_id, cs, bytes(data), res))
        if len(res) != len(data):
            raise DetectError('SPI returned %d bytes for a %d-byte transaction' % (len(res), len(data)))
        return res

    # MCP23S08
    def mcp_read(self, board_id, cs, reg, dummy=0x00):
        return self.xfer(board_id, cs, [MCP_RD, reg, dummy], 'mcp_read')[2]

    def mcp_write(self, board_id, cs, reg, val):
        if reg != MCP_DEFVAL:
            # the probe may only write DEFVAL; anything else would reach pins (IODIR/GPIO/OLAT) or change the protocol
            raise DetectError('the probe writes only DEFVAL, not register 0x%02x' % reg)
        self.xfer(board_id, cs, [MCP_WR, reg, val], 'mcp_write_defval')

    # ADMV8818
    def admv_read(self, board_id, cs, addr):
        return self.xfer(board_id, cs, [0x80 | (addr >> 8), addr & 0xff, 0], 'admv_read')[2]

    def admv_write_config_a(self, board_id, cs):
        self.xfer(board_id, cs, [ADMV_CONFIG_A >> 8, ADMV_CONFIG_A & 0xff, ADMV_CONFIG_A_4WIRE], 'admv_write_config_a')


# ---- the slot probe --------------------------------------------------------------------------------------------
def classify_mcp(read0, read1):
    """Verdict on two register sets {name: value} read with dummy bytes 0x00 and 0xFF. Returns (verdict, why)."""
    vals0 = [read0[n] for n, _ in MCP_REGS]
    vals1 = [read1[n] for n, _ in MCP_REGS]
    if vals0 != vals1:
        return 'unstable', ('answers change with the dummy byte (%s against %s): the data line follows the master '
                            'or is noise, not a register' % (_hex(vals0), _hex(vals1)))
    distinct = set(vals0)
    if distinct == {0x00}:
        return 'floating_low', 'every register reads 0x00: nothing drives the data line (or it is pulled low)'
    if distinct == {0xFF}:
        return 'floating_high', 'every register reads 0xFF: nothing drives the data line (or it is pulled high)'
    if len(distinct) == 1:
        return 'stuck', 'every register reads 0x%02x' % vals0[0]
    r = read0
    bad = []
    if r['IODIR'] & 0xF0 != 0xF0:
        bad.append('IODIR 0x%02x has an ID pin (4-7) as output' % r['IODIR'])
    for n in ('IPOL', 'GPINTEN', 'INTCON'):
        if r[n] != 0:
            bad.append('%s 0x%02x, not its reset value 0x00 (no QICK code writes it)' % (n, r[n]))
    if r['IOCON'] & 0xC1:
        bad.append('IOCON 0x%02x has unimplemented bits (7, 6, 0) set' % r['IOCON'])
    if bad:
        return 'not_mcp', '; '.join(bad)
    return 'mcp_signature', ('IODIR 0x%02x, IPOL/GPINTEN/INTCON 0x00, IOCON 0x%02x: the MCP23S08 reset pattern '
                             '(or the library\'s IODIR 0xf0)' % (r['IODIR'], r['IOCON']))


def _hex(vals):
    return '[%s]' % ' '.join('%02x' % v for v in vals)


def probe_slot(bus, side, slot, echo=True):
    """Probe one slot's GPIO expander. Returns a plain dict:
    {side, slot, board_id, verdict, why, regs, id, confirmed}. verdict is one of
    'card' (expander confirmed, ID read), 'empty' (floating data line), 'unstable', 'stuck', 'not_mcp',
    'no_echo' (signature matched but DEFVAL did not read back what was written), 'error'."""
    s = SIDES[side]
    board_id, cs = s['board_offset'] + slot, s['mcp_cs']
    out = {'side': side, 'slot': slot, 'board_id': board_id, 'verdict': None, 'why': None, 'regs': None,
           'id': None, 'confirmed': False}
    try:
        read0 = dict((n, bus.mcp_read(board_id, cs, a, 0x00)) for n, a in MCP_REGS)
        if len(set(read0.values())) == 1 and set(read0.values()) <= {0x00, 0xFF}:
            read1 = read0      # a constant line: nothing to compare (saves half the traffic on an eval board)
        else:
            read1 = dict((n, bus.mcp_read(board_id, cs, a, 0xFF)) for n, a in MCP_REGS)
    except Exception as e:      # noqa: BLE001
        out.update(verdict='error', why='read failed: %s: %s' % (type(e).__name__, e))
        return out
    out['regs'] = dict((n, read0[n]) for n, _ in MCP_REGS)
    verdict, why = classify_mcp(read0, read1)
    if verdict in ('floating_low', 'floating_high'):
        out.update(verdict='empty', why=why, floating=verdict)
        return out
    if verdict != 'mcp_signature':
        out.update(verdict=verdict, why=why)
        return out
    if echo:
        orig = read0['DEFVAL']
        got = []
        try:
            for pat in ECHO_PATTERNS:
                bus.mcp_write(board_id, cs, MCP_DEFVAL, pat)
                got.append((pat, bus.mcp_read(board_id, cs, MCP_DEFVAL, 0x00),
                            bus.mcp_read(board_id, cs, MCP_DEFVAL, 0xFF)))
        except Exception as e:      # noqa: BLE001
            out.update(verdict='error', why='DEFVAL echo failed: %s: %s' % (type(e).__name__, e))
            return out
        finally:
            try:
                bus.mcp_write(board_id, cs, MCP_DEFVAL, orig)
            except Exception:      # noqa: BLE001
                pass
        out['echo'] = [{'wrote': p, 'read': [a, b]} for p, a, b in got]
        if any(a != p or b != p for p, a, b in got):
            out.update(verdict='no_echo', why='%s, but DEFVAL did not read back what was written (%s)' % (
                why, ', '.join('0x%02x -> 0x%02x/0x%02x' % g for g in got)))
            return out
        why += '; DEFVAL read back 0xa5 and 0x5a and was restored to 0x%02x' % orig
    out['confirmed'] = True
    out['id'] = read0['GPIO'] >> 4
    kind = CARD_IDS[side].get(out['id'])
    out.update(verdict='card', why=why, kind=kind)
    if kind is None:
        out['why'] += '; card ID %d is not a known %s card' % (out['id'], side.upper())
    return out


def check_filters(bus, side, slot, nch):
    """Read PRODUCT_ID of each ADMV8818 of a confirmed RF card; if it does not answer, write ADI_SPI_CONFIG_A = 0x3C
    (what the library's constructor writes: SDO active) and read again. Returns a list of chip checks."""
    board_id = SIDES[side]['board_offset'] + slot
    out = []
    for ch in range(nch):
        rec = {'chip': 'ADMV8818', 'cs': ch, 'expect': '0x%04x' % ADMV_PRODUCT_ID}
        try:
            pid = (bus.admv_read(board_id, ch, ADMV_PID_H) << 8) | bus.admv_read(board_id, ch, ADMV_PID_L)
            rec['first_read'] = '0x%04x' % pid
            if pid != ADMV_PRODUCT_ID:
                bus.admv_write_config_a(board_id, ch)
                rec['wrote_config_a'] = '0x%02x' % ADMV_CONFIG_A_4WIRE
                pid = (bus.admv_read(board_id, ch, ADMV_PID_H) << 8) | bus.admv_read(board_id, ch, ADMV_PID_L)
            rec['product_id'] = '0x%04x' % pid
            rec['ok'] = pid == ADMV_PRODUCT_ID
        except Exception as e:      # noqa: BLE001
            rec.update(ok=False, error='%s: %s' % (type(e).__name__, e))
        out.append(rec)
    return out


# ---- gate and presence -----------------------------------------------------------------------------------------
class Gate(object):
    def __init__(self, gpio, names, ipname):
        self.gpio, self.names, self.name = gpio, names, ipname
        self.ch = _channel(gpio, names['gate_channel'])
        self.mask = 1 << int(names['gate_bit'])
        self.is_open = None

    def set(self, opened):
        lvl = int(names_level(self.names, opened))
        self.ch.write(lvl << int(self.names['gate_bit']), self.mask)
        self.is_open = bool(opened)


def names_level(names, opened):
    return names['gate_open_level'] if opened else 1 - int(names['gate_open_level'])


def read_presence(gpio, names):
    word = int(_channel(gpio, names['presence_channel']).read())
    return ((word >> int(names['presence_bit'])) & 1) == int(names['presence_level']), word


# ---- detection -------------------------------------------------------------------------------------------------
def empty_inventory(reason, **kw):
    inv = {'version': INVENTORY_VERSION, 'present': False, 'reason': reason, 'method': None, 'gate': None,
           'presence_input': None, 'cards': [], 'slots': [], 'forced': {}}
    inv.update(kw)
    return inv


def _ports(side, slot):
    n = SIDES[side]['nch']
    return list(range(n * slot, n * slot + n))


def _forced(options):
    out = {}
    for side, slots in ((options or {}).get('cards') or {}).items():
        if side not in SIDES:
            raise ValueError("rfbox cards: side must be 'dac' or 'adc', not %r" % side)
        for slot, kind in dict(slots).items():
            slot = int(slot)
            if not 0 <= slot < 4:
                raise ValueError('rfbox cards: slot %r out of range 0-3' % slot)
            if kind is not None and kind not in CARD_IDS[side].values():
                raise ValueError('rfbox cards: %r is not a %s card kind (%s)' % (
                    kind, side.upper(), ', '.join(sorted(CARD_IDS[side].values()))))
            out[(side, slot)] = kind
    return out


def detect(soc, options=None):
    """Decide box / no box and list the cards. `soc` needs ip_dict (or attributes) with the RF-board IPs.
    options: {'names': {...DEFAULT_NAMES overrides}, 'cards': {'adc': {slot: kind|None}, 'dac': {...}} (declare a
    slot's card, e.g. a first-version DC In card whose SPI reads do not work; declaring any card asserts the box),
    'echo': bool (DEFVAL write-then-read, default True), 'filters': bool (ADMV8818 PRODUCT_ID check, default True)}.
    Returns the inventory (plain data). The Bus used (with its transaction log) is left at soc._rfbox_bus."""
    options = options or {}
    names = merged_names(options)
    forced = _forced(options)
    ips = dict((n, find_ip(soc, n)) for n in names['control'])
    missing = [n for n, v in ips.items() if v is None]
    if missing:
        return empty_inventory('no rf-board control', missing=missing)
    gate_name, gate_gpio = _first_ip(soc, names['gate'])
    pres_name, pres_gpio = _first_ip(soc, names['presence'])
    gate = Gate(gate_gpio, names, gate_name) if gate_gpio is not None else None
    bus = Bus(ips['filter_spi'], ips['brd_sel_gpio'])
    try:
        object.__setattr__(soc, '_rfbox_bus', bus)
    except Exception:      # noqa: BLE001
        pass
    inv = empty_inventory(None)
    inv['gate'] = None if gate is None else {'ip': gate_name, 'open': None}
    if pres_gpio is not None:
        present, word = read_presence(pres_gpio, names)
        inv['presence_input'] = {'ip': pres_name, 'word': word, 'present': present}

    def close_gate():
        if gate is not None:
            gate.set(False)
            inv['gate']['open'] = False

    def open_gate():
        if gate is not None:
            # idle levels first, so the lines come out of high impedance deselected: card selection off, chip
            # selects high (AXI register writes only; no SPI clock)
            ips['brd_sel_gpio'].channel1.write(0, 0xf)
            for n in ('filter_spi', 'bias_spi'):
                try:
                    ips[n].SPI_SSR = 0xff
                except Exception:      # noqa: BLE001
                    pass
            gate.set(True)
            inv['gate']['open'] = True

    if inv['presence_input'] is not None:
        inv['method'] = 'presence input'
        if not inv['presence_input']['present'] and not forced:
            close_gate()
            inv['reason'] = 'presence input says no box (read with the gate closed; nothing driven)'
            inv['slots'] = []
            return inv
    elif gate is not None:
        inv['method'] = 'gated probe'
    else:
        inv['method'] = 'probe (no gate in this firmware)'

    open_gate()
    # the filter SPI's bit order is part of the AXI configuration (library: lsb="msb"); no transaction
    try:
        ips['filter_spi'].config(lsb='msb')
    except AttributeError:
        pass
    slots = []
    for side in ('dac', 'adc'):
        for slot in range(4):
            if (side, slot) in forced:
                kind = forced[(side, slot)]
                slots.append({'side': side, 'slot': slot, 'board_id': SIDES[side]['board_offset'] + slot,
                              'verdict': 'forced', 'why': 'declared by the caller (options["cards"])',
                              'kind': kind, 'id': None, 'confirmed': False, 'regs': None})
                continue
            slots.append(probe_slot(bus, side, slot, echo=options.get('echo', True)))
    confirmed = [s for s in slots if s['confirmed']]
    present = bool(confirmed) or bool(forced) or bool(inv['presence_input'] and inv['presence_input']['present'])
    inv['present'] = present
    inv['slots'] = slots
    inv['forced'] = dict(('%s %d' % k, v) for k, v in forced.items())
    if not present:
        close_gate()
        inv['reason'] = 'no slot answered as a GPIO expander: no box (or a box with no card that has one)'
        return inv
    inv['reason'] = ('presence input' if inv['presence_input'] and inv['presence_input']['present']
                     else 'GPIO expander confirmed in %d slot(s)' % len(confirmed)) + (
                         '; cards declared by the caller' if forced else '')
    cards = []
    for s in slots:
        kind = s.get('kind')
        if kind is None:
            continue
        c = {'slot': s['slot'], 'side': s['side'], 'kind': kind, 'name': KIND_NAMES[kind], 'id': s['id'],
             'channels': _ports(s['side'], s['slot']), 'source': 'forced' if s['verdict'] == 'forced' else 'probe',
             'chip_checks': [{'chip': 'MCP23S08', 'ok': s['confirmed'], 'why': s['why']}]}
        if kind in FILTER_KINDS and options.get('filters', True) and s['verdict'] != 'forced':
            c['chip_checks'].extend(check_filters(bus, s['side'], s['slot'], SIDES[s['side']]['nch']))
        cards.append(c)
    inv['cards'] = cards
    return inv


def card_kind(inv, side, slot):
    for c in (inv or {}).get('cards') or []:
        if c['side'] == side and c['slot'] == slot:
            return c['kind']
    return None


def plain(inv):
    """The inventory as JSON would give it back (what a Pyro / JSON client sees)."""
    return json.loads(json.dumps(inv))


def describe(inv):
    """Lines for the board description."""
    inv = inv or {}
    if inv.get('reason') == 'no rf-board control':
        return []
    lines = ['\n\tQICK box: %s (%s; %s)' % ('present' if inv.get('present') else 'not present', inv.get('method'),
                                           inv.get('reason'))]
    g = inv.get('gate')
    if g:
        lines.append('\t\tRF-board output gate %s: %s' % (g['ip'], 'open' if g.get('open') else 'closed'))
    if not inv.get('present'):
        return lines
    # the slot lines keep RFQickSoc216V1's wording, which qick.rfbox.cfg_layout parses on the PC
    lines.append("\nQICK box daughter cards detected:")
    order = sorted(inv.get('slots') or [], key=lambda s: (s['side'] != 'adc', s['slot']))
    for s in order:
        if s.get('kind'):
            card = next((c for c in inv['cards'] if c['side'] == s['side'] and c['slot'] == s['slot']), {})
            checks = [c for c in card.get('chip_checks', []) if c.get('chip') == 'ADMV8818']
            extra = '' if not checks else ', filters %s' % ' '.join(
                'ok' if c.get('ok') else 'FAIL(%s)' % c.get('product_id') for c in checks)
            lines.append('\t%s slot %d: %s card has ports %s (%s, ID %s%s)' % (
                s['side'].upper(), s['slot'], KIND_NAMES[s['kind']], _ports(s['side'], s['slot']),
                s['verdict'], s.get('id'), extra))
        else:
            lines.append('\t%s slot %d: No card detected (%s)' % (
                s['side'].upper(), s['slot'], s['verdict'] if s['verdict'] != 'card' else
                'expander answers, unknown ID %s' % s.get('id')))
    return lines


# ---- firmware identity -----------------------------------------------------------------------------------------
def firmware_identity(soc, bitfile=None, options=None):
    """{'fw_id': int|None, 'fw_id_hex', 'fw_id_ip', 'build': manifest name|None, 'matched_by', 'release'}.
    fw_id comes from an AXI GPIO named fw_id_gpio (options['names']['fw_id']) when the firmware has one; the build is
    found in qick.lab_release's manifest by fw_id (entries with a "fw_id" field) or by the sha256 of the .hwh /.bit."""
    names = merged_names(options)
    out = {'fw_id': None, 'fw_id_hex': None, 'fw_id_ip': None, 'build': None, 'matched_by': None, 'release': None}
    ipname, gpio = _first_ip(soc, names['fw_id'])
    if gpio is not None:
        try:
            v = int(_channel(gpio, names['fw_id_channel']).read()) & 0xffffffff
            out.update(fw_id=v, fw_id_hex='0x%08x' % v, fw_id_ip=ipname)
        except Exception as e:      # noqa: BLE001
            out['fw_id_error'] = '%s: %s' % (type(e).__name__, e)
    try:
        from . import lab_release
    except ImportError:      # copied out of the package
        lab_release = None
    build = None
    if lab_release is not None:
        if out['fw_id'] is not None:
            build = lab_release.identify_fw_id(out['fw_id'])
            if build is not None:
                out['matched_by'] = 'fw_id'
        if build is None and bitfile:
            # the .hwh only (small; every manifest entry has its sha256); the .bit only when there is no .hwh
            root, _ = os.path.splitext(str(bitfile))
            hwh = root + '.hwh'
            for path in ([hwh] if os.path.exists(hwh) else [str(bitfile)]):
                if os.path.exists(path):
                    try:
                        build = lab_release.identify(path)
                    except OSError:
                        build = None
                    if build is not None:
                        out['matched_by'] = 'sha256 of %s' % os.path.basename(path)
                        break
    if build is not None:
        out['build'] = build.get('name')
        out['release'] = build.get('release')
    return out


def describe_firmware(ident):
    if not ident or (ident.get('fw_id') is None and ident.get('build') is None):
        return []
    parts = []
    if ident.get('build'):
        parts.append('lab build %s (%s)' % (ident['build'], ident.get('matched_by')))
    if ident.get('fw_id') is not None:
        parts.append('build ID %s' % ident['fw_id_hex'])
    return ['\n\tFirmware: ' + ', '.join(parts)]
