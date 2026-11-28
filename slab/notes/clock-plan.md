# Clock plan for a future lab ZCU216 firmware (study, 2026-11-28)

Question (Dave): clocks whose generator, readout, tProc and bus domains are
integer (or small rational) multiples, converters near full speed, and
several software-selectable plans so every frequency is clean in at least
one. A study only: no firmware changed or built. `slab/tools/clock_plan.py`
(stdlib + numpy) prints every table; its defaults are the model used here.

## Answer

One family of plans, made from the stock 245.76 MHz reference by changing
only the tile PLL feedback divider N:

- B = 245.76 N / 32 = 7.68 N MHz
- DAC fs = 32 B, ADC fs = 8 B
- full-speed generators G = 2 B (16 samples per clock, as now)
- interpolated generators I = B
- readouts and buffers R = B
- tProc time clock T = 1.5 B, tProc core C = B / 2 (H3/2' below)

| | Primary N = 39 | Alternate N = 37 | Third N = 38 |
|---|---|---|---|
| B | 299.52 | 284.16 | 291.84 MHz |
| DAC fs | 9584.64 (unchanged) | 9093.12 | 9338.88 MS/s |
| ADC fs | 2396.16 (-2.5 %) | 2273.28 | 2334.72 MS/s |
| G / I = R | 599.04 / 299.52 | 568.32 / 284.16 | 583.68 / 291.84 MHz |
| T (resolution) | 449.28 (2.226 ns) | 426.24 (2.346 ns) | 437.76 (2.284 ns) MHz |
| C | 149.76 | 142.08 | 145.92 MHz |

- One bitstream compiled at N = 39 serves all three; the ratios do not
  depend on N.
- Common period of G, I, R, T: 3 tProc cycles (6.7 ns), against 28
  (65.1 ns) today. C divides every other clock.
- Clean in at least one of N39 and N37, full-speed generators (Tables 9-10):
  - 3-6 GHz qubits: 100 %
  - 0.05-1 GHz: 98 %
  - 5-8.5 GHz readout: 96 %
  - with N38 added, 5-8.5 GHz reaches 98 %.
- Today's plan: 95 %, 67 % and 57 %.

Costs:

1. **tProc time clock**: 4.5 % faster than today, estimated 55 ps short.
   Fallback: compile at N = 38, where every domain estimates >= +3 ps
   (Table 1b).
2. **tProc core**: 150 MHz instead of 215. C = 0.75 B (224.64 MHz) keeps the
   speed but is estimated 62 ps short, doubles the full period and adds
   readout-band spurs (Table 9b). Your choice.
3. **Interpolated generators (tiles 1-3)**: above 7 GS/s the DAC needs an IMR
   mode, which reaches only 2.88-4.31 and 5.27-6.71 GHz; at 16 B (4.79 GS/s)
   they reach 0-4.79 GHz; chosen per tile. Today's 6.88 GS/s (0-6.88 GHz)
   has no family equivalent (Constraints).

## Today (source)

| Domain | Clock | Source |
|---|---|---|
| Full-speed gens (`axis_signal_gen_v6`, DAC tile 0) | 599.04 = 9584.64/16 | `clk_dac0`; `qick_tprocv2_216_standard/bd_2023-1.tcl:949-953, 2468` |
| `axis_sg_int4_v2`, `axis_sg_mixmux8_v1`, tProc `t_clk` | 430.08 = 6881.28/16 (x4 interpolation, tiles 1-3) | `clk_dac2`; `:954-968, 994-1005, 2469` |
| Dyn and PFB readouts, avg buffers, MR buffer, DDR4 writer | 307.2 = 2457.6/8 | `clk_adc2`; `:920-925, 2467` |
| tProc core `c_clk` | 215.04 = 430.08 x 5.625 / 2 / 5.625 | `clk_core` MMCM on `clk_dac2`; `:878-891, 2437` |
| AXI-Lite, DMAs | 100 (PS `pl_clk0`) | `:1571, 2472` |

Tiles, library and timing today:

| Item | Today | Source |
|---|---|---|
| Tile PLLs | 245.76 MHz into every tile (DAC 0, 1, 3 from tile 2); N39/M1 DAC 0, N56/M2 DAC 1-3, N40/M4 ADC. T/R = 7/5 gives the 28-cycle pattern | bd tcl `:921-968` |
| Crossings | all declared asynchronous | `timing.xdc` |
| Other projects | same clocks; lab adds `TIME_SYNC_PERIOD 28` | `slab_tprocv2_216/bd_2024-1.tcl:901-988, 931` |
| Fabric/sample ratios | from the HWH | `qick.py:158-163` |
| `fs`, `fs_mult` (= N), `fs_div`, `f_fabric` | from each tile's `PLLConfig` | `qick.py:203-215` |
| gen `f_dds`, `fdds_div`; readout `f_dds`, `f_output` | derived from the above | `drivers/generator.py:68-72`, `drivers/readout.py:26-33` |
| tProc `f_core`, `f_time` | traced through clocking wizards | `drivers/tproc.py:445-458`, `ip.py:425-490` |
| Frequency matching | integers `fs_mult`, `fdds_div`, and `refclk_freq` | `qick_asm.py:300-348` |
| Rate change | `configure_sample_rates()` at init via `DynamicPLLConfig`; no rate above the compiled one; linked tiles scale together; MMCM input range enforced | `qick.py:481-541, 283-328` |
| Ratio assumptions | only the lab's `time_sync_period` | `drivers/tproc.py:414` |
| Timing, lab build 2026-11-27 | WNS G +3, T +44, R +45, C +137 ps; G is the limit; 0.000 ns needed the 2026-11-25 recipe | `build/*/timing_summary.rpt`, NOTEBOOK |

## Constraints (sources)

DS926 v1.14, PG269 v2.6, UG1437, TI SNAS605AS (LMK04828) and SNAS696C
(LMX2594), embeddedsw `rfdc` driver (`xrfdc_clock.c`, `xrfdc.h`).

| Constraint | Value | Source |
|---|---|---|
| DAC fs, XCZU49DR -2 | 0.5-9.85 GS/s in datapath modes 2-4 (9.70 if the sample clock is forwarded); mode 1 (full DUC) <= 7.0 GS/s | DS926 RF-DAC table 3 |
| ADC fs, quad tile | 0.5-2.5 GS/s | DS926 RF-ADC table 3 |
| Tile PLL reference | 102.4-615 MHz (driver: <= 614.0) | DS926 Clocking; `xrfdc.h` |
| Dividers | R = 1 (only value characterised); N 13-160 (driver searches <= 128); M = 1 (DAC only), 2, 3, even <= 64 | PG269 PLL Parameters |
| VCO | DAC 7.863-13.76 GHz, ADC 8.5-13.2 GHz; no DAC output 6.882-7.863 GHz | PG269; DS926 |
| PL interface | 614 MHz (ADC, DAC with DUC); 625 MHz (DAC, DUC bypassed) | DS926 Table 1 |
| Words per clock | DAC <= 16, ADC <= 12 | PG269; driver |
| IMR modes | high-pass 0.30-0.45 fs, low-pass 0-0.20 fs; usable above 7 GS/s | PG269 |
| MMCM -2 | VCO 800-1600, PFD 10-500 MHz, lock 100 us | DS926 |
| UltraRAM -2 | 600 MHz with output register, 500 without pipeline; BRAM 738 | DS926 (page rendered badly: check the columns against the PDF) |
| `DynamicPLLConfig` | tile goes back to clock-detect and `XRFdc_CustomStartUp` runs: the tile restarts, its fabric clock stops; Gen 3 ADC calibration delay re-set. Lock time, and whether MTS, NCO phase or calibration need redoing: not found | driver `xrfdc_clock.c`; PG269 |
| ADC interleaving | 4 sub-ADCs per quad-tile ADC; offset spurs at k fs/4, skew spurs at ±f + k fs/4; DAC interleaving spurs not found | PG269 |
| CLK104 | LMK04828 sends DAC/ADC_REFCLK to tiles 230/226 (used by QICK); the LMX2594s reach tiles 229/225 as sample clocks. The family changes neither chip | UG1437; Xilinx wiki |

Consequences:
- **Tiles 1-3.** With R = 1 and odd N, the only rates 245.76 N/M that keep
  the fabric of tiles 1-3 an integer multiple of B are 16 B and 32 B.
  20 B needs 4 | N, 18 B needs 8 | N, and 24 B falls in the hole.
- **G : R = 2.** It is the only small ratio that keeps both converters near
  full rate, because DAC fs = 4 x ADC fs. 3/2 puts the DAC in the hole, 5/2
  slows the ADC to 1.97 GS/s, and 15/8 leaves a 30-cycle pattern.
- **MMCM.** Fed by R with D1 M4.5 (VCO 1347.84 MHz), it divides by 3 for T,
  6 for 0.75 B and 9 for B/2, and stays in range for N = 35-40.

## Candidate families

Same converters; only the tProc clocks differ.
- `Hint`: T = G, C = B. Integer; tProc at 599 MHz.
- `Hslow`: T = B, C = B/2. Integer; resolution 3.34 ns.
- `H3/2`: T = 1.5 B, C = 0.75 B.
- `H3/2'`: T = 1.5 B, C = B/2.
- `H4/3`: T = 4B/3, C = 2B/3, resolution 2.50 ns.

### Table 1. Candidate plans: frequencies (MHz)

| plan | DAC fs tile 0 | DAC fs tiles 1-3 | ADC fs | G | I | R | T | C | T period ns |
|---|---|---|---|---|---|---|---|---|---|
| P0 today | 9584.64 | 6881.28 /x4 | 2457.6 | 599.04 | 430.08 | 307.2 | 430.08 | 215.04 | 2.325 |
| Hint N=39 | 9584.64 | 9584.64 /x8 | 2396.16 | 599.04 | 299.52 | 299.52 | 599.04 | 299.52 | 1.669 |
| Hslow N=39 | 9584.64 | 9584.64 /x8 | 2396.16 | 599.04 | 299.52 | 299.52 | 299.52 | 149.76 | 3.339 |
| H3/2 N=39 | 9584.64 | 9584.64 /x8 | 2396.16 | 599.04 | 299.52 | 299.52 | 449.28 | 224.64 | 2.226 |
| H3/2' N=39 | 9584.64 | 9584.64 /x8 | 2396.16 | 599.04 | 299.52 | 299.52 | 449.28 | 149.76 | 2.226 |
| H4/3 N=39 | 9584.64 | 9584.64 /x8 | 2396.16 | 599.04 | 299.52 | 299.52 | 399.36 | 199.68 | 2.504 |
| H3/2' N=39 int16 | 9584.64 | 4792.32 /x4 | 2396.16 | 599.04 | 299.52 | 299.52 | 449.28 | 149.76 | 2.226 |
| H3/2' N=38 | 9338.88 | 9338.88 /x8 | 2334.72 | 583.68 | 291.84 | 291.84 | 437.76 | 145.92 | 2.284 |
| H3/2' N=40 | 9830.4 | 9830.4 /x8 | 2457.6 | 614.4 | 307.2 | 307.2 | 460.8 | 153.6 | 2.17 |

### Table 1b. Members of the H3/2' family (one bitstream, tile PLL N changed at run time)

C is B/2 (H3/2'); the last-but-two column gives C = 0.75 B (H3/2) for comparison.

MMCM: one setting (fed by R, chosen to stay in range for N = 35-40) kept for every member;
VCO must stay in (800, 1600) MHz.

| N | B = R = I | DAC fs | ADC fs | G | T | T ns | C | est. slack ps if compiled here | C = 0.75B: slack | MMCM VCO | URAM on G |
|---|---|---|---|---|---|---|---|---|---|---|---|
| N=40 | 307.2 | 9830.4 | 2457.6 | 614.4 | 460.8 | 2.17 | 153.6 | G-39 R+45 T-111 C+1997 | 230.4: -173 | 1382.4 | G > URAM |
| N=39 | 299.52 | 9584.64 | 2396.16 | 599.04 | 449.28 | 2.226 | 149.76 | G+3 R+128 T-55 C+2164 | 224.64: -62 | 1347.8 | ok |
| N=38 | 291.84 | 9338.88 | 2334.72 | 583.68 | 437.76 | 2.284 | 145.92 | G+47 R+216 T+3 C+2340 | 218.88: +55 | 1313.3 | ok |
| N=37 | 284.16 | 9093.12 | 2273.28 | 568.32 | 426.24 | 2.346 | 142.08 | G+93 R+309 T+65 C+2525 | 213.12: +179 | 1278.7 | ok |
| N=36 | 276.48 | 8847.36 | 2211.84 | 552.96 | 414.72 | 2.411 | 138.24 | G+142 R+407 T+130 C+2720 | 207.36: +309 | 1244.2 | ok |
| N=35 | 268.8 | 8601.6 | 2150.4 | 537.6 | 403.2 | 2.48 | 134.4 | G+194 R+510 T+199 C+2927 | 201.6: +447 | 1209.6 | ok |

### Table 2. Ratios and common period

G/R, T/R, G/T are exact ratios. "Align" = common period of G, I, R, T (the grid for
phase resets and triggers); "full" adds C. Cycles are of each clock in one period.

| plan | G/R | T/R | G/T | T/C | align period | full period |
|---|---|---|---|---|---|---|
| P0 today | 39/20 | 7/5 | 39/28 | 2 | 65.1 ns: G39 I28 R20 T28 | 65.1 ns: G39 I28 R20 T28 C14 |
| Hint N=39 | 2 | 2 | 1 | 2 | 3.34 ns: G2 I1 R1 T2 | 3.34 ns: G2 I1 R1 T2 C1 |
| Hslow N=39 | 2 | 1 | 2 | 2 | 3.34 ns: G2 I1 R1 T1 | 6.68 ns: G4 I2 R2 T2 C1 |
| H3/2 N=39 | 2 | 3/2 | 4/3 | 2 | 6.68 ns: G4 I2 R2 T3 | 13.35 ns: G8 I4 R4 T6 C3 |
| H3/2' N=39 | 2 | 3/2 | 4/3 | 3 | 6.68 ns: G4 I2 R2 T3 | 6.68 ns: G4 I2 R2 T3 C1 |
| H4/3 N=39 | 2 | 4/3 | 3/2 | 2 | 10.02 ns: G6 I3 R3 T4 | 10.02 ns: G6 I3 R3 T4 C2 |
| H3/2' N=39 int16 | 2 | 3/2 | 4/3 | 3 | 6.68 ns: G4 I2 R2 T3 | 6.68 ns: G4 I2 R2 T3 C1 |
| H3/2' N=38 | 2 | 3/2 | 4/3 | 3 | 6.85 ns: G4 I2 R2 T3 | 6.85 ns: G4 I2 R2 T3 C1 |
| H3/2' N=40 | 2 | 3/2 | 4/3 | 3 | 6.51 ns: G4 I2 R2 T3 | 6.51 ns: G4 I2 R2 T3 C1 |

### Table 3. Feasibility from the stock 245.76 MHz reference

| plan | DAC tile 0 PLL | DAC tiles 1-3 PLL | ADC PLL | fs limits | PL i/f (625/614) | tiles 1-3 band (IMR high-pass, zones 1-2) | MMCM from R for T, C (in range N 35-40) |
|---|---|---|---|---|---|---|---|
| P0 today | N39/M1 VCO 9584.6 | N56/M2 VCO 13762.6 | N40/M4 VCO 9830.4 | ok | ok | mode 1, full zones | T = DAC tile 2 clock; C from T: D2 M5.625 /5.625 |
| Hint N=39 | N39/M1 VCO 9584.6 | N39/M1 VCO 9584.6 | N39/M4 VCO 9584.6 | ok | ok | IMR: 2875-4313, 5272-6709 MHz | T = G, C by BUFGCE_DIV |
| Hslow N=39 | N39/M1 VCO 9584.6 | N39/M1 VCO 9584.6 | N39/M4 VCO 9584.6 | ok | ok | IMR: 2875-4313, 5272-6709 MHz | D1 M5 VCO 1497.6, /5 /10 |
| H3/2 N=39 | N39/M1 VCO 9584.6 | N39/M1 VCO 9584.6 | N39/M4 VCO 9584.6 | ok | ok | IMR: 2875-4313, 5272-6709 MHz | D1 M4.5 VCO 1347.8, /3 /6 |
| H3/2' N=39 | N39/M1 VCO 9584.6 | N39/M1 VCO 9584.6 | N39/M4 VCO 9584.6 | ok | ok | IMR: 2875-4313, 5272-6709 MHz | D1 M4.5 VCO 1347.8, /3 /9 |
| H4/3 N=39 | N39/M1 VCO 9584.6 | N39/M1 VCO 9584.6 | N39/M4 VCO 9584.6 | ok | ok | IMR: 2875-4313, 5272-6709 MHz | D3 M14 VCO 1397.8, /3.5 /7 |
| H3/2' N=39 int16 | N39/M1 VCO 9584.6 | N39/M2 VCO 9584.6 | N39/M4 VCO 9584.6 | ok | ok | mode 1, full zones | D1 M4.5 VCO 1347.8, /3 /9 |
| H3/2' N=38 | N38/M1 VCO 9338.9 | N38/M1 VCO 9338.9 | N38/M4 VCO 9338.9 | ok | ok | IMR: 2802-4202, 5136-6537 MHz | D1 M4.5 VCO 1313.3, /3 /9 |
| H3/2' N=40 | N40/M1 VCO 9830.4 | N40/M1 VCO 9830.4 | N40/M4 VCO 9830.4 | ok | ok | IMR: 2949-4424, 5407-6881 MHz | D1 M4.5 VCO 1382.4, /3 /9 |

### Table 4. First-order timing estimate (ps)

Estimated worst slack = measured slack at today's clock minus the period lost,
assuming the same logic per domain (the int4 gens move from T to I = R in the H plans).

| plan | G (gens) | R (readout) | T (tProc time) | C (tProc core) |
|---|---|---|---|---|
| P0 today | 599.04 MHz: +3 | 307.2 MHz: +45 | 430.08 MHz: +44 | 215.04 MHz: +137 |
| Hint N=39 | 599.04 MHz: +3 | 299.52 MHz: +128 | 599.04 MHz: -612 | 299.52 MHz: -1175 |
| Hslow N=39 | 599.04 MHz: +3 | 299.52 MHz: +128 | 299.52 MHz: +1058 | 149.76 MHz: +2164 |
| H3/2 N=39 | 599.04 MHz: +3 | 299.52 MHz: +128 | 449.28 MHz: -55 | 224.64 MHz: -62 |
| H3/2' N=39 | 599.04 MHz: +3 | 299.52 MHz: +128 | 449.28 MHz: -55 | 149.76 MHz: +2164 |
| H4/3 N=39 | 599.04 MHz: +3 | 299.52 MHz: +128 | 399.36 MHz: +223 | 199.68 MHz: +495 |
| H3/2' N=39 int16 | 599.04 MHz: +3 | 299.52 MHz: +128 | 449.28 MHz: -55 | 149.76 MHz: +2164 |
| H3/2' N=38 | 583.68 MHz: +47 | 291.84 MHz: +216 | 437.76 MHz: +3 | 145.92 MHz: +2340 |
| H3/2' N=40 | 614.4 MHz: -39 | 307.2 MHz: +45 | 460.8 MHz: -111 | 153.6 MHz: +1997 |

### Table 5. Nyquist-zone edges 0-10 GHz (MHz)

| plan | DAC tile 0 | DAC tiles 1-3 | ADC |
|---|---|---|---|
| P0 today | 4792.3, 9584.6 | 3440.6, 6881.3 | 1228.8, 2457.6, 3686.4, 4915.2, 6144, 7372.8, 8601.6, 9830.4 |
| H3/2' N=39 | 4792.3, 9584.6 | 4792.3, 9584.6 | 1198.1, 2396.2, 3594.2, 4792.3, 5990.4, 7188.5, 8386.6, 9584.6 |
| H3/2' N=39 int16 | 4792.3, 9584.6 | 2396.2, 4792.3, 7188.5, 9584.6 | 1198.1, 2396.2, 3594.2, 4792.3, 5990.4, 7188.5, 8386.6, 9584.6 |
| H3/2' N=38 | 4669.4, 9338.9 | 4669.4, 9338.9 | 1167.4, 2334.7, 3502.1, 4669.4, 5836.8, 7004.2, 8171.5, 9338.9 |
| H3/2' N=37 | 4546.6, 9093.1 | 4546.6, 9093.1 | 1136.6, 2273.3, 3409.9, 4546.6, 5683.2, 6819.8, 7956.5, 9093.1 |

### Table 6. DDS frequency steps and generator/readout matching (Hz)

| plan | gen step | int gen step | readout step | gen+ro common step | int gen+ro common | every gen freq is a ro freq |
|---|---|---|---|---|---|---|
| P0 today | 2.2316 | 0.4005 | 0.5722 | 22.316 | 4.005 | no |
| H3/2' N=39 | 2.2316 | 0.2789 | 0.5579 | 2.232 | 0.558 | yes |
| H3/2' N=38 | 2.1744 | 0.2718 | 0.5436 | 2.174 | 0.544 | yes |

### Table 7. Narrow bad frequencies in 0.05-1 GHz (MHz, before margins)

| plan | count | frequencies |
|---|---|---|
| P0 today | 16 | 61.4, 92.2, 122.9, 215, 245.8, 307.2, 430.1, 522.2, 599, 614.4, 645.1, 660.5, 737.3, 860.2, 921.6, 952.3 |
| H3/2' N=39 | 6 | 149.8, 299.5, 449.3, 599, 748.8, 898.6 |
| H3/2' N=38 | 6 | 145.9, 291.8, 437.8, 583.7, 729.6, 875.5 |


- **Frequency matching**: the generator step is 4 x the readout step, so
  every generator frequency is a readout frequency (Table 6; today's common
  step is 22.3 Hz).
- **Bad frequencies**: DAC edges are a subset of ADC edges, the DAC image of
  a readout tone aliases onto an ADC edge, and clock harmonics and
  interleaving coincidences share one lattice (B/2 in H3/2', B/4 in H3/2):
  6 narrow bad frequencies below 1 GHz instead of 16.

## Bad frequencies and coverage

Model (`bad_freqs()`):

**Wide, ±50 MHz: Nyquist edges** k fs/2 of each DAC and the ADC. The image
of a tone d from an edge is 2d away; ±50 MHz keeps it >= 100 MHz away, which
a practical 3-8 GHz filter rejects by 20-40 dB.

**Narrow, ±10 MHz** (readout IF bandwidth, qubit sweep):
- fabric-clock harmonics up to 3 GHz, as tones on the DAC outputs and aliased
  (±n fc mod fs) on the ADC;
- ADC interleaving coincidences at k fs/8, where a spur at k fs/4 ± f folds
  onto the tone;
- f = fs_dac/2 mod fs_adc/2, where the DAC image of the drive folds onto the
  readout IF.

**DAC reach**: zones 1-2 (PG269: higher zones "typically not used") or the
IMR band. **Not counted**: fixed 100/333 MHz clocks, sinc roll-off, ADC input
bandwidth.

### Table 8. Clean fraction per plan (wide 50 MHz, narrow 10 MHz, ADC interleave 4)

| plan | 0.05-1 GHz, gen+ro | 3-6 GHz qubits, full-speed gen | 3-6 GHz qubits, int gen | 5-8.5 GHz readout, full-speed gen+ADC | 5-8.5 GHz readout, int gen+ADC |
|---|---|---|---|---|---|
| P0 today | 67.3 | 94.6 | 94.6 | 56.5 | 29.2 |
| H3/2' N=40 | 87.4 | 95.3 | 65.9 | 82.3 | 34.7 |
| H3/2' N=39 | 87.4 | 94.5 | 65.9 | 80.0 | 33.6 |
| H3/2' N=38 | 87.4 | 94.7 | 66.9 | 79.4 | 32.6 |
| H3/2' N=37 | 85.7 | 95.3 | 68.4 | 79.4 | 31.5 |
| H3/2' N=36 | 85.3 | 95.3 | 69.2 | 78.9 | 27.2 |
| H3/2' N=35 | 85.3 | 95.3 | 70.0 | 78.3 | 22.9 |
| H3/2' N=39 int16 | 87.4 | 94.5 | 55.9 | 80.0 | 0.0 |

### Table 9. Plan sets from one bitstream (H3/2' family, N <= 39), clean in at least one plan (%)

| set | 0.05-1 GHz, gen+ro | 3-6 GHz qubits, full-speed gen | 3-6 GHz qubits, int gen | 5-8.5 GHz readout, full-speed gen+ADC | 5-8.5 GHz readout, int gen+ADC | mean, full-speed rows |
|---|---|---|---|---|---|---|
| N39 | 87.4 | 94.5 | 65.9 | 80.0 | 33.6 | 87.3 |
| N38 | 87.4 | 94.7 | 66.9 | 79.4 | 32.6 | 87.2 |
| N37 | 85.7 | 95.3 | 68.4 | 79.4 | 31.5 | 86.8 |
| N39 + N35 | 99.5 | 100.0 | 86.1 | 96.1 | 44.3 | 98.6 |
| N39 + N36 | 99.1 | 100.0 | 81.6 | 96.0 | 44.4 | 98.4 |
| N38 + N35 | 99.1 | 99.8 | 82.2 | 95.4 | 40.8 | 98.1 |
| N39 + N38 + N35 | 99.5 | 100.0 | 86.1 | 99.3 | 46.7 | 99.6 |
| N39 + N37 + N35 | 99.5 | 100.0 | 86.1 | 99.2 | 46.9 | 99.6 |
| N39 + N36 + N35 | 99.5 | 100.0 | 86.1 | 98.7 | 46.0 | 99.4 |

### Table 9b. Effect of the tProc clocks on coverage (clean %, same converter rates)

| tProc clocks | set | 0.05-1 GHz, gen+ro | 3-6 GHz qubits, full-speed gen | 3-6 GHz qubits, int gen | 5-8.5 GHz readout, full-speed gen+ADC | 5-8.5 GHz readout, int gen+ADC |
|---|---|---|---|---|---|---|
| H3/2 | N39 | 81.0 | 94.5 | 65.9 | 68.0 | 29.1 |
| H3/2 | N39+N37 | 97.2 | 100.0 | 77.0 | 89.5 | 40.2 |
| H3/2 | N39+N38+N36 | 98.8 | 100.0 | 81.6 | 97.2 | 43.9 |
| H3/2' | N39 | 87.4 | 94.5 | 65.9 | 80.0 | 33.6 |
| H3/2' | N39+N37 | 98.2 | 100.0 | 77.0 | 95.8 | 44.3 |
| H3/2' | N39+N38+N36 | 99.1 | 100.0 | 81.6 | 98.7 | 46.2 |
| H4/3 | N39 | 86.1 | 94.5 | 65.9 | 79.9 | 33.6 |
| H4/3 | N39+N37 | 98.5 | 100.0 | 77.1 | 95.7 | 45.6 |
| H4/3 | N39+N38+N36 | 99.5 | 100.0 | 81.6 | 99.1 | 47.6 |
| Hint | N39 | 93.7 | 95.2 | 66.5 | 86.3 | 35.9 |
| Hint | N39+N37 | 99.5 | 100.0 | 77.1 | 98.5 | 46.2 |
| Hint | N39+N38+N36 | 100.0 | 100.0 | 81.6 | 100.0 | 47.7 |
| Hslow | N39 | 87.4 | 94.5 | 65.9 | 80.0 | 33.6 |
| Hslow | N39+N37 | 98.2 | 100.0 | 77.0 | 95.8 | 44.3 |
| Hslow | N39+N38+N36 | 99.1 | 100.0 | 81.6 | 98.7 | 46.2 |

### Table 10. Remaining holes (MHz) for selected sets

| set | band | clean % | holes | total MHz | first holes |
|---|---|---|---|---|---|
| P0 today | 0.05-1 GHz, gen+ro | 67.3 | 14 | 309 | 52-71, 82-102, 113-133, 205-225, 236-256, 297-317, 420-440, 512-532 ... |
| P0 today | 3-6 GHz qubits, full-speed gen | 94.6 | 4 | 160 | 3000-3020, 3062-3082, 3584-3604, 4742-4842 |
| P0 today | 3-6 GHz qubits, int gen | 94.6 | 4 | 160 | 3000-3020, 3062-3082, 3391-3491, 3584-3604 |
| P0 today | 5-8.5 GHz readout, full-speed gen+ADC | 56.5 | 60 | 1516 | 5000-5017, 5028-5048, 5120-5140, 5151-5171, 5212-5232, 5243-5263, 5335-5355, 5428-5478 ... |
| P0 today | 5-8.5 GHz readout, int gen+ADC | 29.2 | 32 | 2476 | 5000-5017, 5028-5048, 5120-5140, 5151-5171, 5212-5232, 5243-5263, 5335-5355, 5428-5478 ... |
| H3/2' N39 | 0.05-1 GHz, gen+ro | 87.4 | 6 | 119 | 140-160, 290-310, 439-459, 589-609, 739-759, 889-909 |
| H3/2' N39 | 3-6 GHz qubits, full-speed gen | 94.5 | 5 | 165 | 3000-3005, 3135-3155, 3285-3305, 3584-3604, 4742-4842 |
| H3/2' N39 | 3-6 GHz qubits, int gen | 65.9 | 5 | 1023 | 3000-3005, 3135-3155, 3285-3305, 3584-3604, 4313-5271 |
| H3/2' N39 | 5-8.5 GHz readout, full-speed gen+ADC | 80.0 | 23 | 698 | 5082-5102, 5232-5252, 5381-5401, 5531-5551, 5681-5701, 5831-5851, 5940-6040, 6130-6150 ... |
| H3/2' N39 | 5-8.5 GHz readout, int gen+ADC | 33.6 | 11 | 2321 | 5000-5272, 5381-5401, 5531-5551, 5681-5701, 5831-5851, 5940-6040, 6130-6150, 6280-6300 ... |
| H3/2' N39+N37 | 0.05-1 GHz, gen+ro | 98.2 | 2 | 17 | 140-152, 290-294 |
| H3/2' N39+N37 | 3-6 GHz qubits, full-speed gen | 100.0 | 1 | 1 | 3135-3136 |
| H3/2' N39+N37 | 3-6 GHz qubits, int gen | 77.0 | 2 | 689 | 3135-3136, 4313-5001 |
| H3/2' N39+N37 | 5-8.5 GHz readout, full-speed gen+ADC | 95.8 | 11 | 147 | 5247-5252, 5389-5401, 5531-5551, 5681-5701, 5831-5835, 5957-5977, 7236-7238, 7927-7947 ... |
| H3/2' N39+N37 | 5-8.5 GHz readout, int gen+ADC | 44.3 | 11 | 1948 | 5000-5001, 5105-5125, 5247-5267, 5389-5401, 5531-5551, 5681-5701, 5831-5835, 5957-5977 ... |
| H3/2' N39+N37+N38 | 0.05-1 GHz, gen+ro | 98.2 | 2 | 17 | 140-152, 290-294 |
| H3/2' N39+N37+N38 | 3-6 GHz qubits, full-speed gen | 100.0 | 0 | 0 | none |
| H3/2' N39+N37+N38 | 3-6 GHz qubits, int gen | 77.1 | 1 | 688 | 4313-5001 |
| H3/2' N39+N37+N38 | 5-8.5 GHz readout, full-speed gen+ADC | 98.2 | 6 | 62 | 5247-5252, 5389-5401, 5535-5551, 5681-5701, 5831-5835, 5973-5977 |
| H3/2' N39+N37+N38 | 5-8.5 GHz readout, int gen+ADC | 45.5 | 11 | 1906 | 5000-5001, 5105-5125, 5247-5263, 5389-5401, 5535-5551, 5681-5701, 5831-5835, 5973-5977 ... |
| H3/2 N39+N37 | 0.05-1 GHz, gen+ro | 98.2 | 2 | 17 | 140-152, 290-294 |
| H3/2 N39+N37 | 3-6 GHz qubits, full-speed gen | 100.0 | 1 | 1 | 3135-3136 |
| H3/2 N39+N37 | 3-6 GHz qubits, int gen | 77.0 | 2 | 689 | 3135-3136, 4313-5001 |
| H3/2 N39+N37 | 5-8.5 GHz readout, full-speed gen+ADC | 95.8 | 11 | 147 | 5247-5252, 5389-5401, 5531-5551, 5681-5701, 5831-5835, 5957-5977, 7236-7238, 7927-7947 ... |
| H3/2 N39+N37 | 5-8.5 GHz readout, int gen+ADC | 44.3 | 11 | 1948 | 5000-5001, 5105-5125, 5247-5267, 5389-5401, 5531-5551, 5681-5701, 5831-5835, 5957-5977 ... |

### Table 11. Sensitivity to the keep-out margins (mean clean % over the full-speed-generator bands)

| set | w25 n0 | w50 n0 | w100 n0 | w150 n0 | w50 n5 | w50 n10 | w50 n20 | w100 n10 |
|---|---|---|---|---|---|---|---|---|
| P0 today | 98.5 | 97.0 | 92.1 | 86.4 | 84.5 | 72.8 | 56.3 | 70.8 |
| H3/2' N39 | 98.0 | 96.0 | 90.3 | 84.9 | 91.7 | 87.3 | 78.5 | 81.6 |
| H3/2' N39+N37 | 100.0 | 100.0 | 98.2 | 95.9 | 99.4 | 98.0 | 92.9 | 95.5 |
| H3/2' N39+N37+N38 | 100.0 | 100.0 | 98.2 | 95.9 | 99.7 | 98.8 | 95.0 | 96.7 |
| H3/2 N39+N37 | 100.0 | 100.0 | 98.2 | 95.9 | 99.4 | 98.0 | 92.9 | 95.5 |


- Edges alone cost about 4 %; narrow spurs leave one plan 13-45 % dirty in
  the readout band.
- A step moves lattice point k by k ΔB/2, so points near DC hardly move:
  140-148 MHz stays dirty in every set (choose the IF instead).
- Ranking unchanged for margins 25-150 and 5-20 MHz (Table 11); ignoring
  narrow spurs, one plan covers 96 %, two 100 %.
- Plans two steps apart beat adjacent ones: 98.0 % (N39+N37) against 97.0 %
  (N39+N38).

## Ranking

All N39 plans: DAC 9584.64 MS/s, ADC 97.5 % of today.

### Table 13. Ranking inputs: mean clean % over the full-speed bands for the sets
N39, N39+N37, N39+N37+N38, with the common period and the worst estimated slack at N39

| plan | period, T cycles (G,I,R,T / with C) | coverage 1 / 2 / 3 plans | worst est. slack ps | T ns |
|---|---|---|---|---|
| H3/2' | 3 / 3 | 87.3 / 98.0 / 98.8 | -55 | 2.226 |
| H3/2 | 3 / 6 | 81.2 / 95.6 / 97.5 | -62 | 2.226 |
| H4/3 | 4 / 4 | 86.9 / 98.1 / 99.0 | +3 | 2.504 |
| Hint | 2 / 2 | 91.7 / 99.3 / 99.8 | -1175 | 1.669 |
| Hslow | 1 / 2 | 87.3 / 98.0 / 98.8 | +3 | 3.339 |
| P0 today | 28 / 28 | 72.8 (no family) | +3 | 2.325 |


1. **`H3/2'`** (recommended): 3-cycle grid, C divides all, coverage
   87 / 98 / 99 %; risk only T at 449 MHz.
2. **`H4/3`** if T does not close: same coverage, resolution 8 % worse.
3. **`H3/2`** keeps a 215-class core: ~60 ps short in two domains, more spurs.
4. **Strictly integer**: `Hint` needs the tProc at 599 MHz (~1.2 ns short);
   `Hslow` loses 44 % resolution. Today: 28 cycles, 73 %, no family.

## What a rational ratio costs relative to integer

**(a) Single-clock UltraRAM.** One URAM serves two domains only if the slower
is a clock-enable of the faster: integer ratio, one source. In H3/2'
G : I : R : C = 4 : 2 : 2 : 1 qualifies; T against R or I (3/2) needs a 3:2
gearbox or dual-clock block RAM.
- DMA-loaded memories stay asynchronous unless the DMA side moves to a plan
  clock (B/3 = 99.84 MHz = MMCM VCO/13.5). RFDC AXI-Lite and the PS
  interconnect must stay on `pl_clk0`: plan clocks stop during a switch.
- G at 599.04 MHz is at URAM's 600 MHz limit (Table 12).

### Table 12. Fabric clocks against UltraRAM Fmax (URAM out reg 600 MHz, URAM no pipeline 500 MHz, BRAM write-first 738 MHz)

| plan | G | R | T | C |
|---|---|---|---|---|
| P0 today | 599 ok x ok | 307.2 ok ok ok | 430.1 ok ok ok | 215 ok ok ok |
| Hint N=39 | 599 ok x ok | 299.5 ok ok ok | 599 ok x ok | 299.5 ok ok ok |
| Hslow N=39 | 599 ok x ok | 299.5 ok ok ok | 299.5 ok ok ok | 149.8 ok ok ok |
| H3/2 N=39 | 599 ok x ok | 299.5 ok ok ok | 449.3 ok ok ok | 224.6 ok ok ok |
| H3/2' N=39 | 599 ok x ok | 299.5 ok ok ok | 449.3 ok ok ok | 149.8 ok ok ok |
| H4/3 N=39 | 599 ok x ok | 299.5 ok ok ok | 399.4 ok ok ok | 199.7 ok ok ok |
| H3/2' N=39 int16 | 599 ok x ok | 299.5 ok ok ok | 449.3 ok ok ok | 149.8 ok ok ok |
| H3/2' N=38 | 583.7 ok x ok | 291.8 ok ok ok | 437.8 ok ok ok | 145.9 ok ok ok |
| H3/2' N=40 | 614.4 x x ok | 307.2 ok ok ok | 460.8 ok ok ok | 153.6 ok ok ok |


**(b) Phase-reset/trigger alignment.** Grid of 3 tProc cycles (6.7 ns);
integer 2 (3.3 ns); today 28 (65 ns). `TIME_SYNC_PERIOD` = 3 for every
member; the helpers carry over.

**(c) Emulator/timing model.** A 3-slot phase table per 3/2 crossing; none
for integer. From one MMCM, Vivado times a 3/2 crossing on its worst edge
pair (0.56 ns, G to T), so it keeps a CDC FIFO or gets multicycle
constraints; an integer crossing gets a full fast period.

## Switching plans at run time

`dac_sample_rates`/`adc_sample_rates` at `QickSoc` start, every tile scaled
by N/39 (supported today); reference and CLK104 untouched. Per switch:

1. `DynamicPLLConfig` on every tile: each restarts, its fabric clock stops.
   Tile lock time not documented; MMCM lock 100 us.
2. Reset the MMCM, then the fabric. Today `clk_core` resets only from
   `pl_resetn0` (bd tcl `:2464, 2473`) and `rst_core` waits for `locked`, so
   today's `configure_sample_rates` on DAC tile 2 may leave the core in
   reset: check first.
3. NCO phases restart; ADC calibration and MTS (if used) are expected to need
   redoing (not documented). Programs reload as usual.

Library: a plan argument at start-up; the MMCM/fabric reset; the plan in
`soccfg` (derived frequencies already follow via `_read_freqs()` and clock
tracing); a clean-frequency query to pick a plan. A switch is a
re-initialisation of the board.

## To verify on hardware

1. Every tile locks at N = 35-39 through `DynamicPLLConfig`, measure the
   restart time, and check that the MMCM relocks and the fabric comes back.
   Check this on today's firmware first, then on the family bitstream.
2. ADC noise and spurs at 2.27-2.40 GS/s. Measure where the interleaving
   and clock spurs really are, and how large, so the narrow list can be cut
   to the real spurs.
3. IMR high-pass output band and level on tiles 1-3 at 9.58 GS/s, with the
   x8 interpolation mapping (not found in PG269).
4. Generator/readout phase: one reset offset per run with the 3-cycle grid
   (repeat `slab/sim/time_sync` on the new ratios first).
5. Timing closure of the family at N = 39 (T at 449 MHz), falling back to
   N = 38.
6. NCO phase and ADC calibration after a switch, and whether MTS is needed for
   reproducible phase across switches.
