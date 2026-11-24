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

The package version reported by `qick.__version__` is upstream's number
(`major.minor.PR`). It does not tell you which lab changes are present. Use the
commit hash for that.

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

[NOTEBOOK.md](NOTEBOOK.md) is the running log of what was tried and found.
