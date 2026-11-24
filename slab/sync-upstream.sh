#!/usr/bin/env bash
# Merge openquantumhardware/qick main into this fork's main.
# Usage: slab/sync-upstream.sh [--push]
set -euo pipefail

UPSTREAM_URL=https://github.com/openquantumhardware/qick
cd "$(git rev-parse --show-toplevel)"

git remote get-url upstream >/dev/null 2>&1 || git remote add upstream "$UPSTREAM_URL"

if [ "$(git symbolic-ref --short HEAD)" != main ]; then
    echo "Not on main. Check out main first." >&2
    exit 1
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "Working tree has uncommitted changes. Commit or stash them first." >&2
    exit 1
fi

git fetch upstream
if git remote get-url origin >/dev/null 2>&1; then
    git fetch origin
    git merge --ff-only origin/main
fi

new=$(git rev-list --count main..upstream/main)
if [ "$new" -eq 0 ]; then
    echo "Already up to date with upstream/main ($(cat qick_lib/qick/VERSION))."
else
    echo "Merging $new upstream commits:"
    git log --oneline --first-parent main..upstream/main | head -30
    if ! git merge --no-edit upstream/main; then
        git merge --abort
        echo "Merge had conflicts and was aborted. Merge upstream/main by hand." >&2
        exit 1
    fi
    echo "Now at upstream $(cat qick_lib/qick/VERSION)."
fi

echo "Lab differences from upstream:"
git diff --stat upstream/main main | tail -n 1

if [ "${1:-}" = --push ]; then
    git push origin main
fi
