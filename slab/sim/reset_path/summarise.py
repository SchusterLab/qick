import re, glob, numpy as np
tick = 1/15.36e6/43680*1e9  # ns per simulation time unit
f = 5797.975e6
for p in sorted(glob.glob('sim_*.txt')):
    rows=[tuple(map(int,re.findall(r'-?\d+',l))) for l in open(p) if l.startswith('RES')]
    a=np.array(rows); slot,g,r=a[:,1],a[:,2],a[:,3]
    if 'sync1' in p:
        # with the sync block the release time varies; what matters is where the zero lands in the common period
        print(p, 'gen zero: %d distinct positions in the common period; ro zero: %d; ro-gen: %d distinct, %s ns'%(len(set(a[:,6])),len(set(a[:,7])),len(set(r-g)),sorted({round(x*tick,3) for x in r-g})))
        continue
    # repeatability per slot
    rep = all(len(set(g[slot==s]))==1 and len(set(r[slot==s]))==1 for s in set(slot))
    gs=sorted(set(g)); rs=sorted(set(r)); d=(r-g)
    print(p, 'same result every pass:',rep)
    print('  gen: %d distinct, %.3f..%.3f ns (%.2f..%.2f gen clocks), step %.1f ps'%(len(gs),gs[0]*tick,gs[-1]*tick,gs[0]/1120,gs[-1]/1120,np.min(np.diff(gs))*tick*1e3))
    print('  ro : %d distinct, %.3f..%.3f ns (%.2f..%.2f ro clocks), step %.1f ps'%(len(rs),rs[0]*tick,rs[-1]*tick,rs[0]/2184,rs[-1]/2184,np.min(np.diff(rs))*tick*1e3))
    ds=sorted(set(d)); print('  ro-gen: %d distinct, %.3f..%.3f ns, mean %.3f ns'%(len(ds),ds[0]*tick,ds[-1]*tick,d.mean()*tick))
    ph=np.exp(2j*np.pi*f*d*tick*1e-9); u=ph[:28]
    print('  |mean phasor over 28 slots| at 5.798 GHz = %.3f'%abs(u.mean()))
