"""Build the tProc v2 program for the dispatcher-overflow testbench.

One repetition: N readouts, each a readout config, a pulse and a trigger of the
readout and of output pin 0, spaced by SPACING us; then the usual end of shot
(wait_auto, delay_auto, inc_ext_counter, from AveragerProgramV2).
Optional: wait(t=0) after every WAIT_EVERY readouts (the reporter's workaround).

Writes prog.mem (72-bit words), wave.mem (168-bit words), dmem.mem and
params.txt into the output directory.

usage: make_prog.py OUTDIR N REPS [SPACING_US] [WAIT_EVERY]
(WAIT_EVERY = -1: the N readouts as a tProc loop instead of unrolled;
 -2: unrolled, without the triggers, so that the wave queues fill first)
"""
import json, os, sys
import numpy as np
from qick import QickConfig
from qick.asm_v2 import AveragerProgramV2

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, '..', '..', '..', 'firmware', 'testbench', 'qick_testbench', 'soccfg.json')


class ManyReadouts(AveragerProgramV2):
    def _initialize(self, cfg):
        self.declare_gen(ch=0, nqz=1)
        self.declare_readout(ch=0, length=0.05)
        self.add_readoutconfig(ch=0, name='ro', freq=100, gen_ch=0)
        self.add_pulse(ch=0, name='p', style='const', freq=100, phase=0, gain=0.5, length=0.02)

    def _one(self, cfg):
        self.send_readoutconfig(ch=0, name='ro', t=0)
        self.pulse(ch=0, name='p', t=0)
        if not cfg['no_trig']:
            self.trigger(ros=[0], pins=[0], t=0.01)
        self.delay(cfg['spacing'])

    def _body(self, cfg):
        if cfg['loop']:
            # the same readouts as a tProc loop (a jump per readout)
            self.open_loop(cfg['n'], 'ro_loop')
            self._one(cfg)
            self.close_loop()
            # the library counts triggers statically; tell it how many there are
            self.ro_chs[0]['trigs'] = cfg['n']
            return
        for i in range(cfg['n']):
            self._one(cfg)
            if cfg['wait_every'] and (i + 1) % cfg['wait_every'] == 0:
                self.wait(0)


def words(arr, nwords):
    """rows of 32-bit words (little-endian) -> hex strings of nwords*32 bits"""
    out = []
    for row in np.asarray(arr, dtype=np.int64):
        v = 0
        for k in range(nwords):
            v |= (int(row[k]) & 0xffffffff) << (32 * k)
        out.append('%0*x' % (nwords * 8, v))
    return out


def main():
    outdir, n, reps = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    spacing = float(sys.argv[4]) if len(sys.argv) > 4 else 0.3
    wait_every = int(sys.argv[5]) if len(sys.argv) > 5 else 0
    with open(CFG) as f:
        soccfg = QickConfig(json.load(f))
    prog = ManyReadouts(soccfg, reps=reps, final_delay=0.5, initial_delay=1.0,
                        cfg={'n': n, 'spacing': spacing, 'wait_every': max(wait_every, 0), 'loop': wait_every == -1, 'no_trig': wait_every == -2})
    prog.compile()
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, 'prog.mem'), 'w') as f:
        f.write('\n'.join(words(prog.binprog['pmem'], 3)) + '\n')
    with open(os.path.join(outdir, 'wave.mem'), 'w') as f:
        f.write('\n'.join(words(prog.binprog['wmem'], 6)) + '\n')
    dmem = prog.binprog['dmem']
    with open(os.path.join(outdir, 'dmem.mem'), 'w') as f:
        f.write('\n'.join('%08x' % (int(x) & 0xffffffff) for x in (dmem if dmem is not None else [0])) + '\n')
    with open(os.path.join(outdir, 'prog.asm'), 'w') as f:
        f.write(prog.asm())
    ro = soccfg['readouts'][0]
    with open(os.path.join(outdir, 'params.txt'), 'w') as f:
        f.write('n=%d reps=%d spacing_us=%g wait_every=%d ro_trig_port=%d ro_cfg_wport=%d gen_wport=%d pmem_words=%d\n'
                % (n, reps, spacing, wait_every, ro['trigger_port'], ro['tproc_ctrl'],
                   soccfg['gens'][0]['tproc_ch'], len(prog.binprog['pmem'])))
    print(open(os.path.join(outdir, 'params.txt')).read().strip())


if __name__ == '__main__':
    main()
