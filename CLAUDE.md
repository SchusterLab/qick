# Schuster Lab fork of QICK: notes for agents

This file and `slab/` exist only in the fork (`SchusterLab/qick`). Read
`slab/README.md` first: it has the branch model and the sync procedure.

- At the start of a work session run `slab/sync-upstream.sh` and report what
  came in.
- `main` is upstream plus lab changes. Never force-push or rewrite it. Bring
  upstream in by merge.
- A change meant for upstream goes on `feat/<topic>` branched from
  `upstream/main`. A lab-only change goes on `lab/<topic>` branched from `main`.
- Record every difference from upstream in `slab/CHANGES.md`, and keep the
  running log in `slab/NOTEBOOK.md`.
- Do not edit `qick_lib/qick/VERSION`; upstream rewrites it on every pull
  request and an edit will conflict at every sync.
- Opening a pull request or issue on `openquantumhardware/qick` reaches the
  upstream maintainers. Ask the user before doing it.
- This repository is public. Keep hostnames, tokens and unpublished results
  out of it.
- Dev environment: `python3 -m venv .venv && .venv/bin/pip install -e .`
  (`.venv/` is ignored locally through `.git/info/exclude`).
