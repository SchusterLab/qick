# Lab changes relative to upstream

Every difference between this fork's `main` and `openquantumhardware/qick`
`main` is listed here. Check with `git diff upstream/main main --stat`.

| Change | Files | Upstream status |
|---|---|---|
| Fork documentation and sync script | `slab/`, `CLAUDE.md` | Lab only |
| tProc time reset aligned to the common period of the clocks: parameter `TIME_SYNC_PERIOD` on `qproc_ctrl`, `qick_processor`, `axis_qick_processor` and the IP packaging. Default 0 = unchanged behaviour. | `firmware/ip/qick_processor/` | Not proposed upstream yet |
| PFB readout v4 builds under Vivado 2024.1: type `BOOLEAN_VECTOR` in the generated FFT code renamed `SSR_BOOLEAN_VECTOR` (it clashes with a VHDL-2008 built-in). No functional change. | `firmware/ip/axis_pfb_readout_v4/src/pfb/fft/ssr_fft_8x64/ssr_8x64_entity_declarations.vhd` | Not proposed upstream yet |
| Lab ZCU216 firmware project: stock tProc v2 design with weighted readout buffers, a weight-loading DMA and switch, and `TIME_SYNC_PERIOD` = 28. Block design is generated from the stock one by `make_bd.py`. | `firmware/projects/slab_tprocv2_216/` | Lab only |

With `TIME_SYNC_PERIOD` at its default the firmware behaves as upstream's.

## Dropped

The fork carried three commits from January 2022. They remain in history but
their changes were dropped in the 2026-11-24 merge:

- `8e1b8e16` replaced `qick.bit` and `qick.hwh` with a build that had a 2**14
  accumulation buffer. Upstream now ships one bitstream per board and those
  files no longer exist.
- `078f8d75` made the `QickSoc` import optional when `pynq` is missing.
  Upstream now imports `QickSoc` only when `pynq` is present.
