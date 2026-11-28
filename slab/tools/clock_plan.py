#!/usr/bin/env python3
"""Clock-plan study for a lab ZCU216 QICK firmware (XCZU49DR-2, CLK104).

Reproduces every table in slab/notes/clock-plan.md:

    python3 slab/tools/clock_plan.py            # all tables, default model
    python3 slab/tools/clock_plan.py --wide 100 --narrow 5

Only the standard library and numpy are used. Frequencies are in MHz and are
kept exact (fractions.Fraction) wherever ratios or periods are computed.
The device limits used are collected in LIMITS with their sources; the
bad-frequency model is described in bad_freqs().
"""
import argparse
import itertools
from fractions import Fraction as F
from math import gcd

import numpy as np

# ---------------------------------------------------------------------------
# Device limits (sources in slab/notes/clock-plan.md, "Constraints")
# ---------------------------------------------------------------------------
LIMITS = dict(
    f_ref=F(24576, 100),          # tile PLL reference on the stock ZCU216 design (bd tcl)
    dac_fs_max=9850,              # DS926, RF-DAC Gen 3 max sample rate
    adc_fs_max=2500,              # DS926, RF-ADC 2.5 GS/s (XCZU49DR)
    dac_hole=(6882, 7863),        # DS926: no DAC PLL output in this range (qick.py:439)
    dac_vco=(7800, 13800),        # qick.py:395 (XRFdc VCO_RANGE constants, Gen 3 DAC)
    adc_vco=(8500, 13200),        # qick.py:398
    fb_div=(13, 160),             # qick.py:392
    dac_m=[1, 2, 3] + list(range(4, 66, 2)),
    adc_m=[2, 3] + list(range(4, 66, 2)),
    pl_if_duc=614,                # DS926 Table 1 (ZU4xDR): ADC, and DAC with DUC (interpolation > 1)
    pl_if_bypass=625,             # DS926 Table 1: DAC with the DUC bypassed (tile 0, x1)
    imr_hp=(0.30, 0.45),          # PG269: usable band of the IMR high-pass mode, x fs
    imr_lp=(0.0, 0.20),           # PG269: IMR low-pass mode
    mmcm_vco=(800, 1600),         # DS926 MMCM F_VCOMIN/F_VCOMAX, -2
    mmcm_pfd=(10, 500),           # DS926 MMCM F_PFDMIN/F_PFDMAX, -2
)
URAM = {'URAM out reg': 600, 'URAM no pipeline': 500, 'BRAM write-first': 738}  # DS926, -2 (0.85 V)

# Timing closure of the lab build (build/slab_tprocv2_216_2026-11-27_plain-large,
# timing_summary.rpt, intra-clock WNS in ns): clock -> (MHz, WNS)
CLOSURE = {
    'G': (F(59904, 100), 0.003),   # RFDAC0_CLK: full-speed generators
    'T': (F(43008, 100), 0.044),   # RFDAC2_CLK: tProc time, int4 gens, mixmux
    'R': (F(3072, 10), 0.045),     # RFADC2_CLK: readouts, buffers
    'C': (F(21504, 100), 0.137),   # clk_core: tProc core
}



def mhz(x):
    return F(x).limit_denominator(10**6)


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------
class Plan:
    """A clock plan. Clocks: G full-speed gen fabric, I interpolated-gen fabric,
    R readout fabric, T tProc time, C tProc core. Converter rates: dac (tile 0,
    full-speed gens), dac_int (tiles 1-3, interpolated gens), adc."""

    def __init__(self, name, dac, dac_int, int_interp, adc, T, C, note=''):
        self.name, self.note = name, note
        self.dac, self.dac_int, self.int_interp, self.adc = map(mhz, (dac, dac_int, int_interp, adc))
        self.G = self.dac / 16                       # axis_signal_gen_v6: 16 samples/clk
        self.I = self.dac_int / self.int_interp / 4  # axis_sg_int4_v2: 4 IQ samples/clk
        self.R = self.adc / 8                        # 8 samples/clk (dyn readout, avg buffers)
        self.T, self.C = mhz(T), mhz(C)

    def reach(self, kind):
        """Usable output bands (MHz): Nyquist zones 1-2 (zone 2 with mix-mode; PG269 says
        higher zones are typically not used).  Above 7 GS/s with interpolation the DAC
        needs an IMR datapath mode; the high-pass one is assumed (0.30-0.45 fs and its
        zone-2 mirror)."""
        fs = float(self.dac if kind == 'dac' else self.dac_int)
        if kind == 'dac_int' and fs > 7000 and self.int_interp > 1:
            a, b = LIMITS['imr_hp']
            return [(a * fs, b * fs), ((1 - b) * fs, (1 - a) * fs)]
        return [(0, fs)]

    def clocks(self):
        return dict(G=self.G, I=self.I, R=self.R, T=self.T, C=self.C)


def baseline():
    return Plan('P0 today', 9584.64, 6881.28, 4, 2457.6, 430.08, 215.04,
                'stock qick_tprocv2_216_standard')


def family(n, tmode="3/2'", int16=False):
    """B = f_ref*n/32 = 7.68*n MHz. DAC fs = 32B on tile 0, ADC fs = 8B, G = 2B, R = I = B.
    Tiles 1-3 (interpolated gens): fs = 32B with x8 interpolation (needs an IMR datapath
    mode above 7 GS/s), or with int16=True fs = 16B with x4 (datapath mode 1).
    tmode chooses the tProc time/core clocks."""
    B = LIMITS['f_ref'] * n / 32
    T, C = {
        'int':   (2 * B, B),          # T = G
        'slow':  (B, B / 2),          # T = R
        '3/2':   (F(3, 2) * B, F(3, 4) * B),
        "3/2'":  (F(3, 2) * B, B / 2),
        '4/3':   (F(4, 3) * B, F(2, 3) * B),
    }[tmode]
    if int16:
        return Plan('H%s N=%d int16' % (tmode, n), 32 * B, 16 * B, 4, 8 * B, T, C)
    return Plan('H%s N=%d' % (tmode, n), 32 * B, 32 * B, 8, 8 * B, T, C)


# ---------------------------------------------------------------------------
# Feasibility: tile PLL, PL interface, MMCM
# ---------------------------------------------------------------------------
def tile_pll(fs, kind):
    """All (N, M, VCO) with fs = f_ref*N/M inside the documented ranges (R = 1)."""
    L = LIMITS
    vco = L['dac_vco'] if kind == 'dac' else L['adc_vco']
    ms = L['dac_m'] if kind == 'dac' else L['adc_m']
    out = []
    for m in ms:
        n = fs * m / L['f_ref']
        if n.denominator == 1 and L['fb_div'][0] <= n <= L['fb_div'][1] and vco[0] <= fs * m <= vco[1]:
            out.append((int(n), m, float(fs * m)))
    return out


def dac_ok(fs):
    h = LIMITS['dac_hole']
    return fs <= LIMITS['dac_fs_max'] and not (h[0] < fs < h[1])


def mmcm(f_in, outs, span=(1, 1)):
    """Find an MMCM setting: VCO = f_in*M/D, outs[0] may use a fractional divider
    (1/8 steps, CLKOUT0), the others integer.  Returns the (D, M, VCO, dividers) with
    the highest VCO, or None."""
    L = LIMITS
    best = None
    for d in range(1, 9):
        if not (L['mmcm_pfd'][0] <= f_in / d <= L['mmcm_pfd'][1]):
            continue
        for m8 in range(16, 1025):
            m = F(m8, 8)
            vco = f_in * m / d
            if not (L['mmcm_vco'][0] <= vco * span[0] and vco * span[1] <= L['mmcm_vco'][1]
                    and f_in * span[1] / d <= L['mmcm_pfd'][1]):
                continue
            divs = []
            for k, fo in enumerate(outs):
                o = vco / fo
                if k == 0:
                    good = (o * 8).denominator == 1 and 2 <= o <= 128
                else:
                    good = o.denominator == 1 and 1 <= o <= 128
                if not good:
                    break
                divs.append(o)
            else:
                if best is None or vco > best[2]:
                    best = (d, m, vco, divs)
    return best


# ---------------------------------------------------------------------------
# Ratios and the common period
# ---------------------------------------------------------------------------
def fgcd(vals):
    """gcd of positive Fractions."""
    num = 0
    den = 1
    for v in vals:
        den = den * v.denominator // gcd(den, v.denominator)
    for v in vals:
        num = gcd(num, int(v * den))
    return F(num, den)


def flcm(vals):
    nums = 1
    dens = 0
    for v in vals:
        nums = nums * v.numerator // gcd(nums, v.numerator)
        dens = gcd(dens, v.denominator)
    return F(nums, dens)


def pattern(p, keys=('G', 'I', 'R', 'T')):
    c = p.clocks()
    u = fgcd([c[k] for k in keys])
    return u, {k: c[k] / u for k in c}


# ---------------------------------------------------------------------------
# Bad frequencies and coverage
# ---------------------------------------------------------------------------
FMAX = 10000.0


def multiples(step, lo=0.0, hi=FMAX):
    step = float(step)
    k0 = int(np.floor(lo / step))
    return [k * step for k in range(max(k0, 0), int(hi / step) + 2)]


def aliases(f0, fs, hi=FMAX):
    """All f in [0, hi] with f = +-f0 mod fs."""
    fs = float(fs)
    out = []
    for k in range(int(hi / fs) + 2):
        out += [k * fs + f0, k * fs - f0]
    return [x for x in out if 0 <= x <= hi]


def bad_freqs(p, kind, args):
    """List of (freq, margin, label) for one converter set of plan p.
    kind: 'dac' (tile 0, full-speed gens), 'dac_int' (tiles 1-3), 'adc'.
    Wide class (margin args.wide): Nyquist-zone edges k*fs/2, where the image
    of the tone is within 2*margin of it.  Narrow class (margin args.narrow):
      - fabric-clock harmonics n*fc <= args.harm_max (as tones on DAC outputs,
        aliased to the same IF on the ADC);
      - ADC: interleaving-spur coincidences k*fs/(2*args.ilv) (a spur at
        k*fs/ilv +- f lands on the tone's own alias);
      - ADC: the image of the full-speed DAC drive (fs_dac - f) aliasing onto
        the readout IF, i.e. f = fs_dac/2 mod fs_adc/2 (and the same for the
        interpolated tiles)."""
    out = []
    fs = {'dac': p.dac, 'dac_int': p.dac_int, 'adc': p.adc}[kind]
    if args.wide > 0:
        out += [(f, args.wide, 'edge') for f in multiples(fs / 2)]
    if args.narrow > 0:
        harm = []
        for fc in set(p.clocks().values()):
            harm += multiples(fc, 0, args.harm_max)[1:]
        if kind == 'adc':
            for h in harm:
                out += [(f, args.narrow, 'clk') for f in aliases(h, fs)]
            if args.ilv > 1:
                out += [(f, args.narrow, 'ilv') for f in multiples(fs / (2 * args.ilv))]
            for fd in (p.dac, p.dac_int):
                out += [(f, args.narrow, 'img') for f in aliases(float(fd) / 2, float(fs) / 2)]
        else:
            out += [(h, args.narrow, 'clk') for h in harm]
    return out


GRID = 0.1  # MHz


def clean_mask(p, kinds, band, args):
    f = np.arange(band[0], band[1] + GRID / 2, GRID)
    ok = np.ones_like(f, dtype=bool)
    for kind in kinds:
        if kind != 'adc':
            r = np.zeros_like(ok)
            for a, b in p.reach(kind):
                r |= (f >= a) & (f <= b)
            ok &= r
        for f0, m, _ in bad_freqs(p, kind, args):
            if f0 + m < band[0] or f0 - m > band[1]:
                continue
            ok &= np.abs(f - f0) > m
    return f, ok


def holes(f, ok, minw=0.0):
    out = []
    bad = ~ok
    if not bad.any():
        return out
    d = np.diff(np.concatenate([[0], bad.astype(int), [0]]))
    for s, e in zip(np.where(d == 1)[0], np.where(d == -1)[0]):
        if f[e - 1] - f[s] > minw:
            out.append((f[s], f[e - 1]))
    return out


BANDS = [
    ('0.05-1 GHz, gen+ro', (50, 1000), ('dac', 'adc')),
    ('3-6 GHz qubits, full-speed gen', (3000, 6000), ('dac',)),
    ('3-6 GHz qubits, int gen', (3000, 6000), ('dac_int',)),
    ('5-8.5 GHz readout, full-speed gen+ADC', (5000, 8500), ('dac', 'adc')),
    ('5-8.5 GHz readout, int gen+ADC', (5000, 8500), ('dac_int', 'adc')),
]


def coverage(plans, band, kinds, args):
    ok = None
    for p in plans:
        f, o = clean_mask(p, kinds, band, args)
        ok = o if ok is None else (ok | o)
    return f, ok


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
def fmt(x, nd=3):
    t = '%.*f' % (nd, float(x))
    return t.rstrip('0').rstrip('.') if '.' in t else t


def table(head, rows):
    print('| ' + ' | '.join(head) + ' |')
    print('|' + '---|' * len(head))
    for r in rows:
        print('| ' + ' | '.join(str(c) for c in r) + ' |')
    print()


def est_slack(key, f):
    f0, wns = CLOSURE[key]
    return 1000 * (wns - (1000 / float(f0) - 1000 / float(f)))  # ps


def candidate_plans():
    return [baseline()] + [family(39, t) for t in ('int', 'slow', '3/2', "3/2'", '4/3')] + \
           [family(39, "3/2'", int16=True), family(38, "3/2'"), family(40, "3/2'")]


def t_candidates(plans):
    print('### Table 1. Candidate plans: frequencies (MHz)\n')
    rows = []
    for p in plans:
        rows.append([p.name, fmt(p.dac, 2), fmt(p.dac_int, 2) + ' /x%d' % p.int_interp, fmt(p.adc, 2),
                     fmt(p.G, 3), fmt(p.I, 3), fmt(p.R, 3), fmt(p.T, 3), fmt(p.C, 3),
                     fmt(1000 / p.T, 3)])
    table(['plan', 'DAC fs tile 0', 'DAC fs tiles 1-3', 'ADC fs', 'G', 'I', 'R', 'T', 'C', 'T period ns'], rows)


def t_ratios(plans):
    print('### Table 2. Ratios and common period\n')
    print('G/R, T/R, G/T are exact ratios. "Align" = common period of G, I, R, T (the grid for\n'
          'phase resets and triggers); "full" adds C. Cycles are of each clock in one period.\n')
    rows = []
    for p in plans:
        c = p.clocks()
        u, cyc = pattern(p)
        uf, cycf = pattern(p, ('G', 'I', 'R', 'T', 'C'))
        rows.append([p.name, c['G'] / c['R'], c['T'] / c['R'], c['G'] / c['T'], c['T'] / c['C'],
                     '%s ns: %s' % (fmt(1000 / u, 2), ' '.join('%s%s' % (k, cyc[k]) for k in 'GIRT')),
                     '%s ns: %s' % (fmt(1000 / uf, 2), ' '.join('%s%s' % (k, cycf[k]) for k in 'GIRTC'))])
    table(['plan', 'G/R', 'T/R', 'G/T', 'T/C', 'align period', 'full period'], rows)


def t_feasible(plans):
    print('### Table 3. Feasibility from the stock 245.76 MHz reference\n')
    rows = []
    for p in plans:
        d0 = tile_pll(p.dac, 'dac')
        d1 = tile_pll(p.dac_int, 'dac')
        a = tile_pll(p.adc, 'adc')
        best = lambda l: ('N%d/M%d VCO %s' % (l[0][0], l[0][1], fmt(l[0][2], 1))) if l else 'none'
        pl_ok = p.G <= LIMITS['pl_if_bypass'] and max(p.I, p.R) <= LIMITS['pl_if_duc']
        # MMCM for T and C, fed by the readout fabric clock R
        span = (F(35, 39), F(40, 39)) if not p.name.startswith('P0') else (1, 1)
        mm = mmcm(p.R, [p.T, p.C], span) if p.T != p.G else None
        mms = ('D%d M%s VCO %s, /%s /%s' % (mm[0], fmt(mm[1]), fmt(mm[2], 1), fmt(mm[3][0]), fmt(mm[3][1]))
               if mm else ('T = G, C by BUFGCE_DIV' if p.T == p.G else 'none'))
        if p.name.startswith('P0'):
            mms = 'T = DAC tile 2 clock; C from T: D2 M5.625 /5.625'
        dp = ('IMR: %s-%s, %s-%s MHz' % tuple(fmt(x * p.dac_int, 0) for x in
                                            (LIMITS['imr_hp'][0], LIMITS['imr_hp'][1],
                                             1 - LIMITS['imr_hp'][1], 1 - LIMITS['imr_hp'][0]))
              if p.dac_int > 7000 and p.int_interp > 1 else 'mode 1, full zones')
        rows.append([p.name, best(d0), best(d1), best(a),
                     'ok' if dac_ok(p.dac) and dac_ok(p.dac_int) and p.adc <= LIMITS['adc_fs_max'] else 'NO',
                     'ok' if pl_ok else 'over',
                     dp, mms])
    table(['plan', 'DAC tile 0 PLL', 'DAC tiles 1-3 PLL', 'ADC PLL', 'fs limits', 'PL i/f (625/614)',
           'tiles 1-3 band (IMR high-pass, zones 1-2)', 'MMCM from R for T, C (in range N 35-40)'], rows)


def t_timing(plans):
    print('### Table 4. First-order timing estimate (ps)\n')
    print('Estimated worst slack = measured slack at today\'s clock minus the period lost,\n'
          'assuming the same logic per domain (the int4 gens move from T to I = R in the H plans).\n')
    rows = []
    for p in plans:
        c = p.clocks()
        r = [p.name]
        for k in ('G', 'R', 'T', 'C'):
            r.append('%s MHz: %+.0f' % (fmt(c[k], 2), est_slack(k, c[k])))
        rows.append(r)
    table(['plan', 'G (gens)', 'R (readout)', 'T (tProc time)', 'C (tProc core)'], rows)


def t_members(top=40, n_min=35):
    print("### Table 1b. Members of the H3/2' family (one bitstream, tile PLL N changed at run time)\n")
    print('C is B/2 (H3/2\'); the last-but-two column gives C = 0.75 B (H3/2) for comparison.\n')
    print('MMCM: one setting (fed by R, chosen to stay in range for N = 35-40) kept for every member;\n'
          'VCO must stay in %s MHz.\n' % (LIMITS['mmcm_vco'],))
    p39 = family(39)
    mm = mmcm(p39.R, [p39.T, p39.C, F(3, 4) * p39.R], (F(35, 39), F(40, 39)))
    rows = []
    for n in range(top, n_min - 1, -1):
        p = family(n)
        q = family(n, '3/2')
        vco = mm[2] * F(n, 39)
        rows.append(['N=%d' % n, fmt(p.R, 2), fmt(p.dac, 2), fmt(p.adc, 2), fmt(p.G, 2), fmt(p.T, 2),
                     fmt(1000 / p.T, 3), fmt(p.C, 2),
                     ' '.join('%s%+.0f' % (k, est_slack(k, v)) for k, v in
                              (('G', p.G), ('R', p.R), ('T', p.T), ('C', p.C))),
                     '%s: %+.0f' % (fmt(q.C, 2), est_slack('C', q.C)),
                     fmt(vco, 1) + ('' if LIMITS['mmcm_vco'][0] <= vco <= LIMITS['mmcm_vco'][1] else ' out'),
                     'ok' if p.G <= min(URAM.values()) or p.G <= URAM['URAM out reg'] else 'G > URAM'])
    table(['N', 'B = R = I', 'DAC fs', 'ADC fs', 'G', 'T', 'T ns', 'C', 'est. slack ps if compiled here',
           'C = 0.75B: slack', 'MMCM VCO', 'URAM on G'], rows)


def t_edges(plans):
    print('### Table 5. Nyquist-zone edges 0-10 GHz (MHz)\n')
    rows = []
    for p in plans:
        rows.append([p.name, ', '.join(fmt(x, 1) for x in multiples(p.dac / 2)[1:] if x <= FMAX),
                     ', '.join(fmt(x, 1) for x in multiples(p.dac_int / 2)[1:] if x <= FMAX),
                     ', '.join(fmt(x, 1) for x in multiples(p.adc / 2)[1:] if x <= FMAX)])
    table(['plan', 'DAC tile 0', 'DAC tiles 1-3', 'ADC'], rows)


def t_freqstep(plans):
    print('### Table 6. DDS frequency steps and generator/readout matching (Hz)\n')
    rows = []
    for p in plans:
        g = p.dac / 2**32                      # axis_signal_gen_v6, f_dds = fs
        i = p.dac_int / p.int_interp / 2**32   # axis_sg_int4_v2, f_dds = fs/interp
        r = p.adc / 2**32                      # dyn readout, decimation 1, f_dds = fs
        rows.append([p.name, fmt(g * 1e6, 4), fmt(i * 1e6, 4), fmt(r * 1e6, 4),
                     fmt(flcm([g, r]) * 1e6, 3), fmt(flcm([i, r]) * 1e6, 3),
                     'yes' if (g / r).denominator == 1 else 'no'])
    table(['plan', 'gen step', 'int gen step', 'readout step', 'gen+ro common step', 'int gen+ro common',
           'every gen freq is a ro freq'], rows)


def t_badlist(plans, args):
    print('### Table 7. Narrow bad frequencies in 0.05-1 GHz (MHz, before margins)\n')
    rows = []
    for p in plans:
        s = sorted(set(round(f, 1) for f, m, l in bad_freqs(p, 'dac', args) + bad_freqs(p, 'adc', args)
                       if l != 'edge' and 50 - m <= f <= 1000 + m))
        rows.append([p.name, len(s), ', '.join(fmt(x, 1) for x in s[:40]) + (' ...' if len(s) > 40 else '')])
    table(['plan', 'count', 'frequencies'], rows)


def members(top, tmode="3/2'", n_min=35):
    return [family(n, tmode) for n in range(top, n_min - 1, -1)]


def t_coverage(args):
    print('### Table 8. Clean fraction per plan (wide %g MHz, narrow %g MHz, ADC interleave %d)\n'
          % (args.wide, args.narrow, args.ilv))
    plans = [baseline()] + members(40) + [family(39, int16=True)]
    rows = []
    for p in plans:
        r = [p.name]
        for name, band, kinds in BANDS:
            f, ok = coverage([p], band, kinds, args)
            r.append('%.1f' % (100 * ok.mean()))
        rows.append(r)
    table(['plan'] + [b[0] for b in BANDS], rows)


def best_sets(pool, size, args):
    """Best combination of `size` plans by mean clean fraction over the bands."""
    res = []
    masks = {}
    for p in pool:
        for bi, (name, band, kinds) in enumerate(BANDS):
            masks[(p.name, bi)] = coverage([p], band, kinds, args)
    for combo in itertools.combinations(pool, size):
        fr = []
        for bi in range(len(BANDS)):
            ok = np.zeros_like(masks[(combo[0].name, bi)][1])
            for p in combo:
                ok |= masks[(p.name, bi)][1]
            fr.append(ok.mean())
        res.append((np.mean([x for x, b in zip(fr, BANDS) if 'dac_int' not in b[2]]), combo, fr))
    res.sort(key=lambda x: -x[0])
    return res, masks


def t_sets(args, top=39, show=3):
    pool = members(top)
    print("### Table 9. Plan sets from one bitstream (H3/2' family, N <= %d), clean in at least one plan (%%)\n" % top)
    rows = []
    allmasks = {}
    for size in (1, 2, 3):
        res, masks = best_sets(pool, size, args)
        allmasks.update(masks)
        for mean, combo, fr in res[:show]:
            rows.append([' + '.join('N%d' % (int(p.name.split('=')[1])) for p in combo)] +
                        ['%.1f' % (100 * x) for x in fr] + ['%.1f' % (100 * mean)])
    table(['set'] + [b[0] for b in BANDS] + ['mean, full-speed rows'], rows)
    return pool


def t_variants(args):
    print('### Table 9b. Effect of the tProc clocks on coverage (clean %, same converter rates)\n')
    rows = []
    for t in ('3/2', "3/2'", '4/3', 'int', 'slow'):
        for combo in ((39,), (39, 37), (39, 38, 36)):
            ps = [family(n, t) for n in combo]
            fr = [coverage(ps, band, kinds, args)[1].mean() for _, band, kinds in BANDS]
            rows.append(['H%s' % t, '+'.join('N%d' % n for n in combo)] + ['%.1f' % (100 * x) for x in fr])
    table(['tProc clocks', 'set'] + [b[0] for b in BANDS], rows)


def t_ranking(args):
    print('### Table 13. Ranking inputs: mean clean % over the full-speed bands for the sets\n'
          'N39, N39+N37, N39+N37+N38, with the common period and the worst estimated slack at N39\n')
    rows = []
    fs_bands = [b for b in BANDS if 'dac_int' not in b[2]]
    for t in ("3/2'", '3/2', '4/3', 'int', 'slow'):
        cov = []
        for combo in ((39,), (39, 37), (39, 37, 38)):
            ps = [family(n, t) for n in combo]
            cov.append(np.mean([coverage(ps, band, kinds, args)[1].mean() for _, band, kinds in fs_bands]))
        p = family(39, t)
        u, cyc = pattern(p)
        uf, cycf = pattern(p, ('G', 'I', 'R', 'T', 'C'))
        worst = min(est_slack(k, v) for k, v in p.clocks().items() if k != 'I')
        rows.append(['H%s' % t, '%s / %s' % (cyc['T'], cycf['T']), ' / '.join('%.1f' % (100 * x) for x in cov),
                     '%+.0f' % worst, fmt(1000 / p.T, 3)])
    p = baseline()
    cov = np.mean([coverage([p], band, kinds, args)[1].mean() for _, band, kinds in fs_bands])
    rows.append(['P0 today', '28 / 28', '%.1f (no family)' % (100 * cov), '+3', fmt(1000 / p.T, 3)])
    table(['plan', 'period, T cycles (G,I,R,T / with C)', 'coverage 1 / 2 / 3 plans', 'worst est. slack ps',
           'T ns'], rows)


def t_holes(args, sets):
    print('### Table 10. Remaining holes (MHz) for selected sets\n')
    rows = []
    for label, plans in sets:
        for name, band, kinds in BANDS:
            f, ok = coverage(plans, band, kinds, args)
            h = holes(f, ok)
            wid = sum(b - a for a, b in h)
            txt = ', '.join('%s-%s' % (fmt(a, 0), fmt(b, 0)) for a, b in h[:8]) + (' ...' if len(h) > 8 else '')
            rows.append([label, name, '%.1f' % (100 * ok.mean()), len(h), fmt(wid, 0), txt or 'none'])
    table(['set', 'band', 'clean %', 'holes', 'total MHz', 'first holes'], rows)


def t_sensitivity(args, sets):
    print('### Table 11. Sensitivity to the keep-out margins (mean clean % over the full-speed-generator bands)\n')
    rows = []
    combos = [(25, 0), (50, 0), (100, 0), (150, 0), (50, 5), (50, 10), (50, 20), (100, 10)]
    for label, plans in sets:
        r = [label]
        for w, n in combos:
            a = argparse.Namespace(**vars(args))
            a.wide, a.narrow = w, n
            fr = [coverage(plans, band, kinds, a)[1].mean() for _, band, kinds in BANDS if 'dac_int' not in kinds]
            r.append('%.1f' % (100 * np.mean(fr)))
        rows.append(r)
    table(['set'] + ['w%d n%d' % c for c in combos], rows)


def t_uram(plans, uram):
    print('### Table 12. Fabric clocks against UltraRAM Fmax (%s)\n' % ', '.join('%s %s MHz' % kv for kv in uram.items()))
    rows = []
    for p in plans:
        c = p.clocks()
        rows.append([p.name] + ['%s %s' % (fmt(c[k], 1), ' '.join(
            ('ok' if c[k] <= v else 'x') for v in uram.values())) for k in ('G', 'R', 'T', 'C')])
    table(['plan', 'G', 'R', 'T', 'C'], rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--wide', type=float, default=50, help='keep-out around Nyquist edges, MHz')
    ap.add_argument('--narrow', type=float, default=10, help='keep-out around narrow spurs, MHz (0: ignore)')
    ap.add_argument('--harm-max', type=float, default=3000, help='highest fabric-clock harmonic counted, MHz')
    ap.add_argument('--ilv', type=int, default=4, help='RF-ADC interleaving factor (4 in a quad tile; 1: ignore)')
    args = ap.parse_args()

    plans = candidate_plans()
    t_candidates(plans)
    t_members()
    t_ratios(plans)
    t_feasible(plans)
    t_timing(plans)
    t_edges([baseline(), family(39), family(39, int16=True), family(38), family(37)])
    t_freqstep([baseline(), family(39), family(38)])
    t_badlist([baseline(), family(39), family(38)], args)
    t_coverage(args)
    t_sets(args)
    t_variants(args)
    t_ranking(args)
    sets = [('P0 today', [baseline()]), ("H3/2' N39", [family(39, "3/2'")]),
            ("H3/2' N39+N37", [family(39, "3/2'"), family(37, "3/2'")]),
            ("H3/2' N39+N37+N38", [family(n, "3/2'") for n in (39, 37, 38)]),
            ('H3/2 N39+N37', [family(39), family(37)])]
    t_holes(args, sets)
    t_sensitivity(args, sets)
    t_uram(plans, URAM)


if __name__ == '__main__':
    main()
