# Schuster Lab fork of QICK

This is `SchusterLab/qick`, the lab's fork of
[openquantumhardware/qick](https://github.com/openquantumhardware/qick).
Everything in this `slab/` directory, and `CLAUDE.md` at the root, exists only
in the fork. Everything else is upstream code plus whatever lab changes are
listed in [CHANGES.md](CHANGES.md).

## Installing the lab version

    pip install git+https://github.com/SchusterLab/qick

To pin an experiment to an exact version, use a commit hash:

    pip install git+https://github.com/SchusterLab/qick@<commit>

## Versions

`qick.__version__` is upstream's number with the lab release number appended:
`0.2.450.1` is the first lab release on upstream 0.2.450. The lab number is
in `qick_lib/qick/LAB_VERSION`; upstream's `VERSION` is never edited.

- `<upstream>.0` means upstream plus lab changes not yet released.
  `slab/sync-upstream.sh` resets the counter to 0 whenever a merge changes
  upstream's number.
- Between releases, `main` reports the last release's number although it may
  have newer lab commits; pin by tag or commit hash when it matters.
- Each release ships its notes and the matching firmware builds inside the
  package: `qick/LAB_RELEASE.md` and `qick/lab_firmware.json`, read with
  `qick.lab_release.notes()`, `.firmware()`, and `.identify(bitfile)` (which
  finds a build by the sha256 of its `.bit` or `.hwh`).

To make a release:
1. Increase `qick_lib/qick/LAB_VERSION` by one.
2. Add a section `## <version> (<date>)` to `qick_lib/qick/LAB_RELEASE.md`:
   what changed for users, and which firmware goes with it.
3. Add each new firmware build to `qick_lib/qick/lab_firmware.json` with its
   sha256s and whether it has run on a board.
4. Run `slab/tests/`, commit, tag `slab-<version>` (annotated), and push
   `main` and the tag.

The library on the board and the library on the PC have to match. If you
install this fork on a PC, install the same commit on the board.

## Branches

| Branch | What it is | Rule |
|---|---|---|
| `main` | Upstream `main` plus lab changes. This is what the lab installs. | Upstream comes in by merge. Never rewritten or force-pushed. |
| `feat/<topic>` | A change meant to go back upstream. | Branch from `upstream/main`, not from `main`, so the pull request carries only that change. |
| `lab/<topic>` | A change the lab wants that upstream may not. | Branch from `main`, merge back into `main`. |

A change that is useful to everyone goes on a `feat/` branch, is opened as a
pull request against `openquantumhardware/qick`, and is merged into `main` here
in the meantime if the lab needs it before upstream takes it. When upstream
merges it, the next sync absorbs it.

Lab-only files live under `slab/` so that syncs do not conflict. Avoid editing
`qick_lib/qick/VERSION`: upstream rewrites it on every pull request.

## Keeping up with upstream

    slab/sync-upstream.sh          # fetch upstream and merge it into main
    slab/sync-upstream.sh --push   # the same, then push main to the fork

The script stops without changing anything if the working tree is dirty or you
are not on `main`, and it aborts the merge if there are conflicts.

Remotes in a working checkout:

    origin    git@github.com:SchusterLab/qick.git
    upstream  https://github.com/openquantumhardware/qick

## Working on it

    python3 -m venv .venv && .venv/bin/pip install -e .

The library imports without `pynq`, so programs can be built and compiled on
any machine from a saved board configuration (`QickConfig("config.json")`).
Anything that touches the FPGA needs a board.

## Building firmware

The lab machine has Vivado 2024.1 (the newest its licence allows); upstream's
projects are written for 2023.1. The lab project
`firmware/projects/slab_tprocv2_216` is set up for 2024.1:

    cd firmware/projects/slab_tprocv2_216
    python3 make_bd.py                      # only after the stock design changes upstream
    vivado -mode batch -source proj.tcl     # creates the project in ./top

then launch synthesis and implementation (`launch_runs impl_1 -to_step
write_bitstream`). On the lab machine Vivado runs in a container that cannot
see `/home/projects`, so copy `firmware/` (without the other projects) to a
directory under `/tmp` or `/home/slab` and run
`/opt/xilinx/vivado-shell vivado ...` there.

The licence covers the ZCU216 part only, not the ZCU111 or RFSoC4x2.

[NOTEBOOK.md](NOTEBOOK.md) is the running log of what was tried and found.
