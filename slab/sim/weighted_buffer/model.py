"""Bit-exact numpy model of the QICK axis_weighted_buffer v1.3, plus the stimulus
generator and checker for its RTL simulation (run.sh).

For experimenters
-----------------
    import numpy as np, model
    x = ...   # int16 (N, 2): decimated readout samples (I, Q) of the window
    w = ...   # int16 (N, 2): the array passed to declare_readout(weights=w)
    y   = model.weighted(x, w)       # int16 (N, 2): what the decimated buffer stores
    acc = model.accumulate(x, w)     # int64 (2,):   what the accumulated buffer stores (I, Q)

x[n] is the n-th sample of the window: x[0] is the sample one readout clock
cycle after the one on the bus when the trigger rose (see tb.sv), and weight
index n multiplies x[n].

With wI = w[:,0] (lower 16 bits of the weight word) and wQ = w[:,1] (upper):

    y_I = (I*wQ + Q*wI) >> 16      32-bit two's complement, arithmetic shift
    y_Q = (Q*wQ - I*wI) >> 16      (floor division by 65536)

i.e. y = floor( (I + jQ) * (wQ - j wI) / 2**16 ). See slab/notes/weighted-readout.md.

Command line (used by run.sh):
    model.py list                   names of the simulation cases
    model.py gen CASE DIR           write stimulus (data.hex, weights, script.txt) to DIR
    model.py check CASE DIR         compare the simulation's output in DIR with the model
    model.py bias                   truncation-bias study (numpy only)
"""
import sys, os, json
import numpy as np

N_AVG, N_BUF = 13, 12

# ------------------------------------------------------------------ arithmetic

def wrap32(v):
    return ((np.asarray(v, dtype=np.int64) + 2**31) % 2**32) - 2**31

def weighted(x, w):
    """Weighted samples exactly as the matched filter produces them (int16 (...,2))."""
    x = np.asarray(x, dtype=np.int64); w = np.asarray(w, dtype=np.int64)
    I, Q = x[..., 0], x[..., 1]
    wI, wQ = w[..., 0], w[..., 1]
    yI = wrap32(Q*wI + I*wQ) >> 16
    yQ = wrap32(Q*wQ - I*wI) >> 16
    return np.stack([yI, yQ], axis=-1).astype(np.int16)

def accumulate(x, w):
    """Accumulated-buffer value (I, Q) of one window of len(x) samples
    (32-bit accumulator). w must have at least len(x) rows; for a window longer
    than the weight memory pass w[np.arange(len(x)) % 2**N_WGT]."""
    s = weighted(x, w[:len(x)]).astype(np.int64).sum(axis=0)
    return wrap32(s)

def exact_product(x, w):
    """The same product without truncation or wrap, float (for bias studies)."""
    x = np.asarray(x, dtype=np.float64); w = np.asarray(w, dtype=np.float64)
    I, Q, wI, wQ = x[..., 0], x[..., 1], w[..., 0], w[..., 1]
    return np.stack([(Q*wI + I*wQ), (Q*wQ - I*wI)], axis=-1) / 2**16

def truncation_bias(w):
    """Expected offset per output sample (output LSB, the same for I and Q) that
    the floor adds for noise-like input (low 16 bits of the products uniformly
    distributed): -(1 - 2**t/2**16)/2, t = number of trailing zero bits common to
    wI and wQ; 0 where the weight is (0, 0). Sum over the window for the offset
    of the accumulated value; for weights with no common factor of 2 it is
    -0.5 * (number of non-zero weights) to within 1e-5 per sample."""
    w = np.asarray(w, dtype=np.int64)
    g = np.gcd(np.abs(w[..., 0]), np.abs(w[..., 1]))
    t = np.zeros(g.shape, dtype=np.int64); gg = g.copy()
    for _ in range(16):
        m = (gg > 0) & (gg % 2 == 0)
        t[m] += 1; gg[m] //= 2
    b = -(1 - 2.0**np.minimum(t, 16) / 2**16) / 2
    return np.where(g == 0, 0.0, b)

# ------------------------------------------------------------------ cycle model
#
# One readout clock edge per sample (continuous tvalid). Sample s is on s_axis
# after edge s; a trigger "at sample K, width W" is high after edges K..K+W-1.
# Latencies are those of the RTL: 2-flop trigger synchronizer, 4-register data
# delay, BRAM read + register for the weight, DSP macro with 4 registers
# (A1, A2, M, P: Automatic pipelining in the .xci), output register, and a
# 7-register trigger delay to the two buffers. The align case checks them.

class Firmware:
    def __init__(self, n_wgt):
        self.n_wgt = n_wgt
        self.wmem = np.zeros((2**n_wgt, 2), dtype=np.int16)
        self.wdef = np.zeros(2**n_wgt, dtype=bool)
        self.avgmem, self.bufmem = {}, {}
        self.mf_state, self.mf_cnt = "WAIT", 0
        self.avg_state = self.buf_state = "START"
        self.regs = dict(avg_len=1, buf_len=1)
        self.undefined_weight_used = False
        self.cnt_after = {}

    def load_weights(self, addr, w):
        idx = (addr + np.arange(len(w))) % 2**self.n_wgt
        self.wmem[idx] = w; self.wdef[idx] = True

    def arm(self, avg_len, buf_len, avg_addr=0, buf_addr=0):
        self.regs = dict(avg_len=avg_len, buf_len=buf_len)
        self.a_addr, self.a_len = avg_addr, avg_len - 1
        self.b_addr, self.b_len = buf_addr, (buf_len - 1) % 2**N_BUF
        self.avg_state = self.buf_state = "TRIGGER"

    def run(self, x, triggers, width, t_end):
        """Advance over edges around a group of triggers. x(s) returns sample s."""
        ks = sorted(triggers)
        trig = lambda t: any(K <= t < K + width for K in ks)
        t0 = ks[0] - 1
        L = self.regs["avg_len"]
        cnt_after = self.cnt_after = {}
        for t in range(t0 - 20, t_end):
            # matched-filter FSM; trigger_resync seen at edge t is trig(t-3)
            st, c = self.mf_state, self.mf_cnt
            c_new = c + 1 if st == "READ" else 0
            if st == "INIT": st_new = "WAIT"
            elif st == "WAIT": st_new = "READ" if trig(t - 3) else "WAIT"
            else: st_new = "INIT" if c > L else "READ"
            self.mf_state, self.mf_cnt = st_new, c_new
            cnt_after[t] = c_new
        # weighted stream: dout after edge t is sample t-9 times weight address cnt_after[t-7]
        def dout(t):
            s = t - 9
            a = cnt_after.get(s + 2, 0) % 2**self.n_wgt
            if not self.wdef[a]: self.undefined_weight_used = True
            return weighted(x(s), self.wmem[a]).astype(np.int64)
        # buffers: trigger_o seen at edge t is trig(t-10); edge t stores dout(t-1)
        for t in range(t0, t_end):
            tr = trig(t - 10)
            st = self.avg_state
            if st == "TRIGGER":
                if tr: self.avg_state = "AVG"; self.a_cnt = 0; self.acc = np.zeros(2, dtype=np.int64)
            elif st == "AVG":
                self.acc = wrap32(self.acc + dout(t - 1))
                if self.a_cnt == self.a_len: self.avg_state = "QOUT"
                self.a_cnt += 1
            elif st == "QOUT": self.avg_state = "WRITE"
            elif st == "WRITE":
                self.avgmem[self.a_addr % 2**N_AVG] = self.acc.copy()
                self.a_addr += 1; self.avg_state = "WAITT"
            elif st == "WAITT":
                if not tr: self.avg_state = "TRIGGER"
            st = self.buf_state
            if st == "TRIGGER":
                if tr: self.buf_state = "MEMW"; self.b_cnt = 0
            elif st == "MEMW":
                self.bufmem[self.b_addr % 2**N_BUF] = dout(t - 1)
                self.b_addr += 1
                if self.b_cnt == self.b_len: self.buf_state = "WAITT"
                self.b_cnt = (self.b_cnt + 1) % 2**N_BUF
            elif st == "WAITT":
                if not tr: self.buf_state = "TRIGGER"

    def weight_index(self, K, m):
        """Weight address applied to window sample m (sample K+1+m) of the trigger at K."""
        return self.cnt_after.get(K + 3 + m, 0) % 2**self.n_wgt

# ------------------------------------------------------------------ I/O helpers

def pack(iq):
    """int16 (N,2) (I,Q) -> uint32 words, I in the lower 16 bits: the readout's
    format, and the bytes QickSoc.load_weights sends (np.frombuffer(data, int32))."""
    iq = np.ascontiguousarray(iq, dtype=np.int16)
    return np.frombuffer(iq.tobytes(), dtype=np.uint32)

def unpack16(words):
    return np.frombuffer(np.asarray(words, dtype=np.uint32).tobytes(), dtype=np.int16).reshape(-1, 2)

def unpack32(words):
    return np.frombuffer(np.asarray(words, dtype=np.uint64).tobytes(), dtype=np.int32).reshape(-1, 2)

def write_hex(path, words):
    with open(path, "w") as f:
        for v in words: f.write("%08x\n" % int(v))

def read_hex(path, xmask=None):
    """Hex words; lines with X/Z bits (never-written RAM) read as 0 and are flagged in xmask."""
    vals = []
    with open(path) as f:
        for l in f:
            l = l.strip()
            if not l: continue
            bad = any(ch in "xXzZ" for ch in l)
            if xmask is not None: xmask.append(bad)
            vals.append(0 if bad else int(l, 16))
    return vals

def trig_samples(d):
    with open(os.path.join(d, "sim.log")) as f:
        return [int(l.split()[1]) for l in f if l.startswith("TRIG ")]

rng = np.random.default_rng(12345)

def rand_iq(n, lim=32768):
    return rng.integers(-lim, lim, size=(n, 2)).astype(np.int16)

def noise_iq(n, sigma=2000, tone=3000):
    t = np.arange(n)
    z = tone*np.exp(2j*np.pi*0.0137*t) + sigma*(rng.standard_normal(n) + 1j*rng.standard_normal(n))
    return np.clip(np.stack([z.real, z.imag], axis=-1).round(), -32768, 32767).astype(np.int16)

def envelope_weights(n, amp=30000, phase=0.7):
    """Optimal-filter-like weights: Gaussian envelope with a slowly rotating phase."""
    t = np.arange(n)
    env = np.exp(-0.5*((t - 0.4*n)/(0.25*n))**2)
    z = amp*env*np.exp(1j*(phase + 2*np.pi*0.003*t))
    return np.stack([z.real, z.imag], axis=-1).round().astype(np.int16)

# ------------------------------------------------------------------ cases

class Case:
    """Builds the script for tb.sv and the matching op list for the model."""
    def __init__(self, d, n_wgt=13, gap=0):
        self.d, self.n_wgt = d, n_wgt
        self.lines, self.ops, self.nw = [], [], 0
        open(os.path.join(d, "nwgt"), "w").write("%d\n" % n_wgt)
        open(os.path.join(d, "gap"), "w").write("%d\n" % gap)
    def data(self, x):
        write_hex(os.path.join(self.d, "data.hex"), pack(x)); np.save(os.path.join(self.d, "x.npy"), x)
    def wgt(self, w, addr=0):
        f = "w%d" % self.nw; self.nw += 1
        write_hex(os.path.join(self.d, f + ".hex"), pack(w)); np.save(os.path.join(self.d, f + ".npy"), w)
        self.lines.append("wgt %d %d %s.hex" % (addr, len(w), f)); self.ops.append(["wgt", addr, f])
    def arm(self, avg_len, buf_len, avg_addr=0, buf_addr=0):
        # QickSoc.config_avg + config_buf + enable_buf (AxisWeightedBuffer register numbers)
        for r, v in [(0, 0), (1, avg_addr), (2, avg_len), (10, 0), (6, buf_addr), (7, buf_len), (0, 3)]:
            self.lines.append("reg %d %d" % (r, v))
        self.ops.append(["arm", avg_len, buf_len, avg_addr, buf_addr])
    def align(self, p):
        """Next trigger(0, ...) lands so that the window starts at data index 0 mod p."""
        self.lines.append("align %d" % p)
    def trig(self, delay, width, n=1, spacing=0):
        self.lines.append("trig %d %d %d %d" % (delay, width, n, spacing)); self.ops.append(["trig", width, n])
    def wait(self, n): self.lines.append("wait %d" % n)
    def avg(self, addr, n, tag):
        self.lines.append("avg %d %d %s.hex" % (addr, n, tag)); self.ops.append(["avg", addr, n, tag])
    def buf(self, addr, n, tag):
        self.lines.append("buf %d %d %s.hex" % (addr, n, tag)); self.ops.append(["buf", addr, n, tag])
    def note(self, **kw): self.ops.append(["note", kw])
    def save(self):
        with open(os.path.join(self.d, "script.txt"), "w") as f:
            f.write("\n".join(self.lines + ["end"]) + "\n")
        json.dump(self.ops, open(os.path.join(self.d, "ops.json"), "w"))

def notes(d):
    return [o[1] for o in json.load(open(os.path.join(d, "ops.json"))) if o[0] == "note"][0]

def replay(d):
    """Run the model over the case's ops with the trigger samples the simulation
    printed. Returns (model, {tag: (expected, rtl)}, trigger groups, x)."""
    ops = json.load(open(os.path.join(d, "ops.json")))
    n_wgt = int(open(os.path.join(d, "nwgt")).read())
    x = np.load(os.path.join(d, "x.npy")); nx = len(x)
    xs = lambda s: x[s % nx]
    fw = Firmware(n_wgt)
    trigs = trig_samples(d); ti = 0
    out, groups = {}, []
    for op in ops:
        if op[0] == "wgt":
            fw.load_weights(op[1], np.load(os.path.join(d, op[2] + ".npy")))
        elif op[0] == "arm":
            fw.arm(*op[1:])
        elif op[0] == "trig":
            ks = trigs[ti:ti + op[2]]; ti += op[2]
            L = max(fw.regs["avg_len"], fw.regs["buf_len"])
            fw.run(xs, ks, op[1], ks[-1] + L + op[1] + 40)
            groups.append(ks)
        elif op[0] in ("avg", "buf"):
            mem, nb = (fw.avgmem, N_AVG) if op[0] == "avg" else (fw.bufmem, N_BUF)
            exp = np.array([mem.get((op[1] + i) % 2**nb, [np.nan, np.nan]) for i in range(op[2])], dtype=float)
            xm = []
            words = read_hex(os.path.join(d, op[3] + ".hex"), xm)
            got = (unpack32(words) if op[0] == "avg" else unpack16(words)).astype(np.int64)
            out[op[3]] = (exp, got, np.array(xm, dtype=bool))
    return fw, out, groups, x

def compare(out, verbose=True):
    ok = True
    for tag, (exp, got, xm) in out.items():
        if exp.shape != got.shape:
            print("  %s: FAIL, %s entries read, %s expected" % (tag, got.shape, exp.shape)); ok = False; continue
        undef = np.isnan(exp).any(axis=1)
        # an entry agrees if the values match, or if neither model nor RTL wrote it
        # (unwritten RAM reads X in the RTL simulation, 0 in the netlist simulation)
        good = np.where(undef, xm | (got == 0).all(axis=1), (exp == got).all(axis=1) & ~xm)
        n_bad = (~good).sum()
        if verbose or n_bad:
            i = np.argmin(good)
            print("  %s: %d/%d entries agree with the cycle model%s%s" % (tag, good.sum(), len(good),
                  "" if not undef.any() else " (%d never written, in both)" % (undef & xm).sum(),
                  "" if not n_bad else "; first mismatch at %d: model %s rtl %s%s" % (i, exp[i], got[i], " (X)" if xm[i] else "")))
        ok &= n_bad == 0
    return ok

def sum_consistency(out, avg_tag, buf_tag, L, nwin):
    """(e): each window's accumulated value equals the sum of its decimated samples."""
    a = out[avg_tag][1]; b = out[buf_tag][1]
    ok = all((wrap32(b[i*L:(i+1)*L].sum(axis=0)) == a[i]).all() for i in range(nwin))
    print("  sum of the decimated window == accumulated value (%d window%s): %s" %
          (nwin, "s" if nwin > 1 else "", "PASS" if ok else "FAIL"))
    return ok

def window(x, K, L):
    return x[(K + 1 + np.arange(L)) % len(x)]

# --- cases: g_* writes the stimulus, k_* checks and returns True/False ---

def g_align(d, nw=13):
    c = Case(d, nw); c.data(rand_iq(1 << 14)); c.wgt(rand_iq(2**nw))
    c.arm(100, 300); c.trig(200, 7); c.wait(600)
    c.avg(0, 1, "avg"); c.buf(0, 300, "buf"); c.save()

def k_align(d):
    fw, out, groups, x = replay(d)
    ok = compare(out)
    # model-free search: which (sample offset from the trigger sample, weight index) reproduce each decimated sample
    K = groups[0][0]; b = out["buf"][1]; wm = fw.wmem
    tab = []
    for m in range(len(b)):
        hits = []
        for dd in range(-12, 13):
            y = weighted(np.broadcast_to(x[(K + m + dd) % len(x)], wm.shape), wm)
            hits += [(dd, int(j)) for j in np.nonzero((y == b[m]).all(axis=1))[0]]
        tab.append(hits)
    uniq = all(len(h) == 1 for h in tab)
    offs = sorted(set(h[0][0] for h in tab if h))
    idx = [h[0][1] if h else None for h in tab]
    print("  search over offsets -12..12 and all %d weights: unique match for every sample: %s" % (len(wm), uniq))
    print("  decimated sample m = weighted(x[K + m + d], w[j]) with d in %s" % offs)
    print("  j for m = 0..5: %s; m = 97..104: %s; m = 105..299 all %s" % (idx[:6], idx[97:105], sorted(set(idx[105:]))))
    ok &= uniq and offs == [1] and idx == list(range(103)) + [0]*197
    ok &= sum_consistency(out, "avg", "buf", 100, 1)
    return ok

def g_arith(d, nw=13):
    c = Case(d, nw)
    # corners: full-scale input on I and Q times extreme weights, incl. |w| = 32766 (b)
    vx = [-32768, -32767, -1, 0, 1, 32767]
    vw = [-32768, -32767, -32766, -1, 0, 1, 32766, 32767]
    xs = [(a, b) for a in vx for b in vx]; ws = [(a, b) for a in vw for b in vw]
    xc = np.array([p for p in xs for _ in ws], dtype=np.int16)
    wc = np.array([q for _ in xs for q in ws], dtype=np.int16)
    L = 4000
    x = np.concatenate([xc, rand_iq(L - len(xc))]); w = np.concatenate([wc, rand_iq(L - len(wc))])
    c.data(x); c.wgt(w); c.arm(L, L); c.note(nc=len(xc))
    c.align(L); c.trig(0, 7); c.wait(L + 200)
    c.avg(0, 1, "avg"); c.buf(0, L, "buf"); c.save()

def k_arith(d):
    fw, out, groups, x = replay(d)
    ok = compare(out)
    K = groups[0][0]; b = out["buf"][1]; L = len(b)
    xw = window(x, K, L); w = fw.wmem[:L]
    print("  window starts at data index %d" % ((K + 1) % len(x)))
    direct = (weighted(xw, w) == b).all()
    print("  decimated == weighted(x, w) for all %d samples (%d corner pairs + random): %s" % (L, notes(d)["nc"], direct))
    ok &= direct and (accumulate(xw, w) == out["avg"][1][0]).all()
    ex = exact_product(xw, w); wr = (np.floor(ex) != b).any(axis=1)
    print("  samples whose 32-bit sum/difference wrapped: %d" % wr.sum())
    for i in np.nonzero(wr)[0][:4]:
        print("    x=%s w=%s -> %s (untruncated %s)" % (xw[i].tolist(), w[i].tolist(), b[i].tolist(), ex[i].tolist()))
    legal = (np.abs(w.astype(int)) <= 32766).all(axis=1)
    full = (np.abs(xw.astype(int)) >= 32767).all(axis=1)
    print("  with |wI|,|wQ| <= 32766: %d wraps in %d samples (%d of them full-scale on I and Q); max |y| %d" %
          ((wr & legal).sum(), legal.sum(), (legal & full).sum(), np.abs(b[legal].astype(int)).max()))
    ok &= not (wr & legal).any()
    ok &= sum_consistency(out, "avg", "buf", L, 1)
    return ok

def g_default(d, nw=13):
    c = Case(d, nw); L = 1536
    c.data(noise_iq(L))
    w = np.zeros((L, 2), dtype=np.int16); w[:, 0] = 2**15 - 2     # QickProgram.load_envelopes default
    c.wgt(w); c.arm(L, L); c.align(L)
    c.trig(0, 7); c.wait(L + 200); c.avg(0, 1, "avg"); c.buf(0, L, "buf"); c.save()

def k_default(d):
    fw, out, groups, x = replay(d)
    ok = compare(out)
    K = groups[0][0]; b = out["buf"][1].astype(np.int64); L = len(b)
    xw = window(x, K, L).astype(np.int64); a = out["avg"][1][0]
    yI = (xw[:, 1]*32766) >> 16; yQ = (-xw[:, 0]*32766) >> 16
    rot = (b[:, 0] == yI).all() and (b[:, 1] == yQ).all()
    print("  default weights (wI, wQ) = (32766, 0): y_I = floor(Q*32766/2^16), y_Q = floor(-I*32766/2^16): %s" % rot)
    s = xw.sum(axis=0)
    print("  unweighted window sum (I, Q) = %s, accumulated = %s" % (s.tolist(), a.tolist()))
    print("  accumulated I / unweighted Q = %.6f, accumulated Q / unweighted I = %.6f (32766/65536 = %.6f)" %
          (a[0]/s[1], a[1]/s[0], 32766/65536))
    ex = np.array([(xw[:, 1]*32766).sum(), (-xw[:, 0]*32766).sum()]) / 2**16
    print("  truncation offset of the sum: I %.2f, Q %.2f LSB over %d samples (predicted %.2f each)" %
          (a[0]-ex[0], a[1]-ex[1], L, truncation_bias(fw.wmem[:L]).sum()))
    ok &= rot and sum_consistency(out, "avg", "buf", L, 1)
    return ok

def g_realw(d, nw=13):
    c = Case(d, nw); L = 1536
    c.data(noise_iq(L)); c.wgt(envelope_weights(L)); c.arm(L, L); c.align(L)
    c.trig(0, 7); c.wait(L + 200); c.avg(0, 1, "avg"); c.buf(0, L, "buf"); c.save()

def k_realw(d):
    fw, out, groups, x = replay(d)
    ok = compare(out)
    K = groups[0][0]; L = len(out["buf"][1])
    xw = window(x, K, L); a = out["avg"][1][0]; ex = exact_product(xw, fw.wmem[:L]).sum(axis=0)
    print("  envelope weights, noise input: accumulated - untruncated = I %.2f, Q %.2f LSB (predicted %.2f)" %
          (a[0]-ex[0], a[1]-ex[1], truncation_bias(fw.wmem[:L]).sum()))
    return ok and sum_consistency(out, "avg", "buf", L, 1)

def g_short(d, nw=13):
    # (c) 4095 weights loaded; window of 1000 (shorter), then of 4095 (equal)
    c = Case(d, nw); c.data(noise_iq(1 << 14)); c.wgt(envelope_weights(4095))
    c.arm(1000, 1000); c.trig(100, 7); c.wait(1300); c.avg(0, 1, "avg_short"); c.buf(0, 1000, "buf_short")
    c.arm(4095, 4095); c.trig(100, 7); c.wait(4400); c.avg(0, 1, "avg_equal"); c.buf(0, 4095, "buf_equal")
    c.save()

def k_short(d):
    fw, out, groups, x = replay(d)
    ok = compare(out)
    w = np.load(os.path.join(d, "w0.npy"))
    for i, (L, t) in enumerate([(1000, "short"), (4095, "equal")]):
        r = (weighted(window(x, groups[i][0], L), w[:L]) == out["buf_" + t][1]).all()
        print("  window %d with 4095 weights loaded: decimated == weighted(x, w[:%d]): %s" % (L, L, r)); ok &= r
        ok &= sum_consistency(out, "avg_" + t, "buf_" + t, L, 1)
    return ok

def g_spacing(d, nw=13, L=200, g=0, width=7, n=4):
    """n triggers, rising edges L+g samples apart."""
    c = Case(d, nw); c.data(rand_iq(1 << 14)); c.wgt(rand_iq(1024))
    c.arm(L, L); c.trig(200, width, n, L + g); c.wait(L + 100)
    c.avg(0, n, "avg"); c.buf(0, n*L, "buf"); c.note(L=L, n=n, g=g, width=width); c.save()

def k_spacing(d):
    fw, out, groups, x = replay(d)
    ok = compare(out, verbose=False)
    kw = notes(d); L, n = kw["L"], kw["n"]
    a, xa = out["avg"][1], out["avg"][2]
    correct = [accumulate(window(x, K, L), fw.wmem[:L]) for K in groups[0]]
    which = []
    for e in range(n):
        hit = [i for i in range(n) if not xa[e] and (a[e] == correct[i]).all()]
        which.append(("trigger %d" % (hit[0] + 1)) if hit else ("not written" if xa[e] or not a[e].any() else "wrongly weighted"))
    print("  spacing L+%d (L=%d, width %d, %d triggers): RTL == cycle model: %s" % (kw["g"], L, kw["width"], n, ok))
    print("    accumulated entries: %s" % ", ".join(which))
    if n > 1:
        print("    weight index for samples 0..3 after trigger 2: %s" % [fw.weight_index(groups[0][1], m) for m in range(4)])
    return ok

def g_train(d, nw=13, L=100, g=4, n=300):
    # (d) n windows back to back at the minimum spacing, identical input in every window
    S = L + g
    c = Case(d, nw); c.data(np.tile(noise_iq(S), (64, 1)))
    c.wgt(envelope_weights(L)); c.arm(L, L); c.align(S)
    c.trig(0, 7, n, S); c.wait(S + 100)
    c.avg(0, n, "avg"); c.buf(0, 4095, "buf"); c.note(L=L, n=n, S=S); c.save()

def k_train(d):
    fw, out, groups, x = replay(d)
    ok = compare(out)
    kw = notes(d); a = out["avg"][1]; L = kw["L"]
    same = bool((a == a[0]).all())
    exp = accumulate(window(x, groups[0][0], L), fw.wmem[:L])
    print("  %d accumulated entries, all identical: %s, equal to accumulate(x, w): %s; trigger spacing %s" %
          (len(a), same, bool((a[0] == exp).all()), sorted(set(np.diff(groups[0]).tolist()))))
    # the decimated buffer (4096 deep) wraps: check the last windows still intact
    # (not overwritten, and clear of address 4095, which a read of 4095 words misses)
    b = out["buf"][1]; n = kw["n"]
    wins = [i for i in range(n - 4096 // L, n) if 4095 not in (i*L + np.arange(L)) % 4096]
    okb = all((wrap32(b[(i*L + np.arange(L)) % 4096].sum(axis=0)) == a[i]).all() for i in wins)
    print("  decimated buffer (wraps over %d windows): each of the last %d intact windows sums to its accumulated value: %s" % (n, len(wins), okb))
    return ok and same and okb

def g_wrap(d, nw=13):
    L = 2**nw + 808     # window longer than the weight memory
    c = Case(d, nw); c.data(noise_iq(1 << 15)); c.wgt(rand_iq(2**nw)); c.arm(L, 4095)
    c.trig(100, 7); c.wait(L + 300); c.avg(0, 1, "avg"); c.buf(0, 4095, "buf"); c.note(L=L); c.save()

def k_wrap(d):
    fw, out, groups, x = replay(d)
    ok = compare(out)
    K = groups[0][0]; L = notes(d)["L"]; M = 2**fw.n_wgt
    xw = window(x, K, L); a = out["avg"][1][0]
    wraps = bool((accumulate(xw, fw.wmem[np.arange(L) % M]) == a).all())
    print("  L = %d > 2**%d: accumulated == sum with weight index n mod %d: %s" % (L, fw.n_wgt, M, wraps))
    for k, v in {"zero beyond the memory": np.concatenate([fw.wmem, np.zeros((L - M, 2), np.int16)]),
                 "last weight held": np.concatenate([fw.wmem, np.repeat(fw.wmem[-1:], L - M, axis=0)])}.items():
        print("    alternative '%s' would give %s: matches %s" % (k, accumulate(xw, v).tolist(), bool((accumulate(xw, v) == a).all())))
    return ok and wraps

def g_reload(d, nw=13):
    L = 512
    c = Case(d, nw); c.data(noise_iq(1 << 14))
    c.wgt(rand_iq(L)); c.arm(L, L); c.trig(100, 7); c.wait(L + 200); c.avg(0, 1, "avgA"); c.buf(0, L, "bufA")
    c.wgt(rand_iq(L)); c.arm(L, L); c.trig(100, 7); c.wait(L + 200); c.avg(0, 1, "avgB"); c.buf(0, L, "bufB")
    c.wgt(rand_iq(64), addr=128); c.arm(L, L); c.trig(100, 7); c.wait(L + 200); c.avg(0, 1, "avgC"); c.buf(0, L, "bufC")
    c.save()

def k_reload(d):
    fw, out, groups, x = replay(d)
    ok = compare(out)
    wA, wB, wC = [np.load(os.path.join(d, "w%d.npy" % i)) for i in range(3)]
    L = 512; mem = wB.copy(); mem[128:192] = wC
    r = [bool((weighted(window(x, groups[i][0], L), ww) == out[t][1]).all())
         for i, (ww, t) in enumerate([(wA, "bufA"), (wB, "bufB"), (mem, "bufC")])]
    print("  run 1 uses A: %s; after reloading, run 2 uses B: %s; 64 words loaded at address 128 replace B[128:192] only: %s" % tuple(r))
    for t in "ABC": ok &= sum_consistency(out, "avg" + t, "buf" + t, L, 1)
    return ok and all(r)

def g_longtrig(d, nw=13):
    L = 50      # trigger held high longer than the window
    c = Case(d, nw); c.data(rand_iq(1 << 14)); c.wgt(rand_iq(1024))
    c.arm(L, 4*L); c.trig(100, L + 10); c.wait(400); c.avg(0, 1, "avg"); c.buf(0, 4*L, "buf"); c.save()

def k_longtrig(d):
    fw, out, groups, x = replay(d)
    ok = compare(out)
    K = groups[0][0]; idx = [fw.weight_index(K, m) for m in range(200)]
    runs, s = [], 0
    for i in range(1, 201):
        if i == 200 or not (idx[i] == idx[i-1] + 1 or idx[i] == idx[i-1] == 0):
            runs.append("%d..%d: w[%d..%d]" % (s, i-1, idx[s], idx[i-1])); s = i
    print("  L=50, trigger width 60, decimated length 200; weight index by sample: " + ", ".join(runs))
    return ok

def g_gap(d, nw=13):
    # tvalid with gaps (junk on tdata while tvalid is low)
    c = Case(d, nw, gap=5); c.data(rand_iq(1 << 14)); c.wgt(rand_iq(1024))
    c.arm(200, 200); c.trig(100, 7); c.wait(600); c.avg(0, 1, "avg"); c.buf(0, 200, "buf"); c.save()

def k_gap(d):
    # the cycle model assumes continuous tvalid: model-free search only
    x = np.load(os.path.join(d, "x.npy")); w = np.load(os.path.join(d, "w0.npy"))
    K = trig_samples(d)[0]; b = unpack16(read_hex(os.path.join(d, "buf.hex")))
    a = unpack32(read_hex(os.path.join(d, "avg.hex")))[0]
    pairs = []
    for m in range(len(b)):
        hit = None
        for dd in range(-12, 13):
            y = weighted(np.broadcast_to(x[(K + m + dd) % len(x)], w.shape), w)
            j = np.nonzero((y == b[m]).all(axis=1))[0]
            if len(j): hit = (m + dd, int(j[0])); break
        pairs.append(hit)
    good = sum(p is not None and p == (m + 1, m) for m, p in enumerate(pairs))
    print("  tvalid low on ~1/5 of cycles: decimated sample m == weighted(x[K+1+m], w[m]) for %d / %d" % (good, len(pairs)))
    print("  (sample - K, weight index) for m = 0..7: %s" % pairs[:8])
    print("  sum of decimated == accumulated: %s" % bool((wrap32(b.astype(np.int64).sum(axis=0)) == a).all()))
    print("  DIAGNOSTIC (not pass/fail): with tvalid gaps the weighting is not aligned; the lab readouts give continuous tvalid")
    return True

CASES = {
    "align": (g_align, k_align), "arith": (g_arith, k_arith), "default": (g_default, k_default),
    "realw": (g_realw, k_realw), "short": (g_short, k_short), "wrap": (g_wrap, k_wrap),
    "reload": (g_reload, k_reload), "longtrig": (g_longtrig, k_longtrig), "train": (g_train, k_train),
}
DIAGNOSTIC = {"gap": (g_gap, k_gap)}     # run explicitly: run.sh DIR gap
for _g in range(0, 7):
    CASES["spacing%d" % _g] = (lambda d, nw=13, g=_g: g_spacing(d, nw, g=g), k_spacing)
KEY14 = ["align", "arith", "realw", "wrap", "reload", "spacing4", "train"]

def lookup(name):
    base, _, suf = name.partition(".")
    return {**CASES, **DIAGNOSTIC}[base], (14 if suf == "w14" else 13)

# ------------------------------------------------------------------ bias study

def bias_study():
    print("Truncation offset of the accumulated value (output LSB): accumulate() minus the untruncated sum,")
    print("mean +- std over 200 noise traces (tone 3000 + Gaussian noise sigma 2000 on I and Q).")
    for N in (1536, 8192):
        for name, w in [("default (32766, 0)", np.tile(np.array([[32766, 0]], np.int16), (N, 1))),
                        ("envelope, amp 30000", envelope_weights(N)),
                        ("uniform random int16", rand_iq(N, 32767))]:
            dd = np.array([accumulate(x, w) - exact_product(x, w).sum(axis=0)
                           for x in (noise_iq(N) for _ in range(200))])
            print("  N=%5d %-21s I %9.2f +- %5.2f  Q %9.2f +- %5.2f  per sample I %.5f Q %.5f  formula %9.2f" %
                  (N, name, dd[:, 0].mean(), dd[:, 0].std(), dd[:, 1].mean(), dd[:, 1].std(),
                   dd[:, 0].mean()/N, dd[:, 1].mean()/N, truncation_bias(w).sum()))

if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "list":
        print("\n".join(list(CASES) + [k + ".w14" for k in KEY14]))
    elif cmd == "gen":
        (gen, _), nw = lookup(sys.argv[2]); gen(sys.argv[3], nw)
    elif cmd == "check":
        (_, chk), nw = lookup(sys.argv[2])
        print("== %s (N_WGT=%d)" % (sys.argv[2], nw))
        r = chk(sys.argv[3])
        print("   %s" % ("PASS" if r else "FAIL"))
        sys.exit(0 if r else 1)
    elif cmd == "bias":
        bias_study()
