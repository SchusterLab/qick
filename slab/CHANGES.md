# Lab changes relative to upstream

Every difference between this fork's `main` and `openquantumhardware/qick`
`main` is listed here. Check with `git diff upstream/main main --stat`.

| Change | Files | Upstream status |
|---|---|---|
| Fork documentation and sync script | `slab/`, `CLAUDE.md` | Lab only |
| tProc time reset aligned to the common period of the clocks: new parameter `TIME_SYNC_PERIOD` on `qproc_ctrl`, default 0 = unchanged behaviour. Not yet passed up through `qick_processor` or the IP packaging, so no build uses it. | `firmware/ip/qick_processor/src/qproc_ctrl.sv` | Not proposed upstream yet |

With `TIME_SYNC_PERIOD` at its default the firmware behaves as upstream's.

## Dropped

The fork carried three commits from January 2022. They remain in history but
their changes were dropped in the 2026-11-24 merge:

- `8e1b8e16` replaced `qick.bit` and `qick.hwh` with a build that had a 2**14
  accumulation buffer. Upstream now ships one bitstream per board and those
  files no longer exist.
- `078f8d75` made the `QickSoc` import optional when `pynq` is missing.
  Upstream now imports `QickSoc` only when `pynq` is present.
