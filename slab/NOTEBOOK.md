# Notebook

Running log for the lab fork. Newest entry first. Record dead ends as well as
results, and say where each number came from.

## 2026-11-24: readout phase against frequency (referral from the measurement project)

- Read `ctrl_sg_v6.sv`, `ctrl_dyn_ro_v1.sv`, `axis_cdcsync_v1` and the stock
  ZCU216 tProc v2 block design, and analysed stored labdev runs 306 and 314.
- Findings, evidence and proposed tests are in
  [notes/readout-phase.md](notes/readout-phase.md).
- Simulated the reset path with the real HDL in Vivado 2024.1's simulator
  (`slab/sim/reset_path/`). It reproduces the 28 and 7 offsets exactly and
  puts the reset-path part of the residual at about 15 ns of 151 ns.
- Not done: any measurement on hardware.

## 2026-11-24: phase reset made deterministic (design and first firmware change)

- Tried and dropped: a block that holds reset words until the next legal
  tick. It makes the phase repeatable but delays the reset-carrying pulse by
  0 to 65 ns, differently on each run. Dave prefers hardware that does
  nothing automatically, with software scheduling resets on legal ticks.
- Done: `TIME_SYNC_PERIOD` option in `qproc_ctrl` so time zero always falls
  on the same slot of the 28-cycle clock pattern; simulated in
  `slab/sim/time_sync/` (1 slot with the option, 28 without, 200 starts).
- Options considered for keeping clocks integer-related instead (slower
  tProc, or lower DAC and ADC rates through `dac_sample_rates` /
  `adc_sample_rates`) were set aside: they cost tProc resolution or DAC
  frequency reach. Details in `notes/readout-phase.md`.
- Vivado: the lab machine has 2024.1 only (licence cap). The stock ZCU216
  tProc v2 project creates and validates there with the 2023.1 version check
  bypassed. One block fails to synthesise (`axis_pfb_readout_v4`: its FFT
  code declares `BOOLEAN_VECTOR`, which clashes with VHDL-2008); renaming the
  type fixes that block. Full build result pending.

## 2026-11-24: fork brought up to date

- The fork (`SchusterLab/qick`) was last pushed in August 2024 and its `main`
  was 2227 commits behind upstream and 3 ahead (lab commits from January 2022).
- Merged upstream `main` at `b2abda7b` (version 0.2.450) into `main`. The tree
  was taken from upstream as is; the three old lab commits are obsolete (see
  `CHANGES.md`). No history was rewritten.
- Branch model, install instructions and the sync script are in `README.md`.
- Off-board check: `pip install -e .` in a fresh Python 3.12 venv works and
  `import qick` succeeds without `pynq` (measured here).

### Found while setting up

- `firmware/testbench/qick_testbench/soccfg.json` is stale. It was written by
  library 0.2.367, and `print(QickConfig(path))` with 0.2.450 raises
  `KeyError: 'dac_power'` in `QickConfig.description()` (measured here, after
  the expected version-mismatch warning). Not yet checked whether the
  testbench itself still runs with it.
- The repository has no tests that run without a board. The only CI test is
  `test_zcu111.yml`, which runs the demo notebooks on a self-hosted ZCU111.

### Candidate first projects (not started)

- An off-board test suite: build and compile programs against saved
  configurations for each board and compare with known-good output. This would
  let changes be checked on any machine and in ordinary CI.
- Refresh or regenerate the stale testbench configuration above.
