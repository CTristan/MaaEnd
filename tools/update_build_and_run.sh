#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-v2}"
LOCAL_BRANCH="${LOCAL_BRANCH:-local}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

log() {
    printf '[update-build-run] %s\n' "$*"
}

fail() {
    printf '[update-build-run] ERROR: %s\n' "$*" >&2
    exit 1
}

ensure_ssl_cert_file() {
    if [[ -n "${SSL_CERT_FILE:-}" ]]; then
        return 0
    fi

    local cert_path=""
    if cert_path="$("$PYTHON_BIN" -c 'import certifi; print(certifi.where())' 2>/dev/null)" \
        && [[ -n "$cert_path" ]] \
        && [[ -f "$cert_path" ]]; then
        export SSL_CERT_FILE="$cert_path"
        log "Using certifi CA bundle: $SSL_CERT_FILE"
    fi
}

cd "$ROOT_DIR"

command -v git >/dev/null 2>&1 || fail "git is required but was not found in PATH"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "$PYTHON_BIN is required but was not found in PATH"

ensure_ssl_cert_file

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "must be run inside the MaaEnd git repository"

git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1 || fail "git remote '$UPSTREAM_REMOTE' is not configured"

if [[ -d .git/rebase-merge || -d .git/rebase-apply ]]; then
    fail "a rebase is already in progress — resolve it (git rebase --continue / --abort), then rerun"
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
    fail "working tree has uncommitted changes — commit them to '$LOCAL_BRANCH' first (that's what it's for)"
fi

current_branch="$(git branch --show-current)"
if [[ "$current_branch" != "$LOCAL_BRANCH" ]]; then
    log "Switching to '$LOCAL_BRANCH' (was on '$current_branch')"
    git checkout "$LOCAL_BRANCH"
fi

git show-ref --verify --quiet "refs/heads/$UPSTREAM_BRANCH" \
    || fail "expected local branch '$UPSTREAM_BRANCH' to exist as the upstream mirror"

log "Fetching $UPSTREAM_REMOTE/$UPSTREAM_BRANCH..."
git fetch --prune "$UPSTREAM_REMOTE" "$UPSTREAM_BRANCH"

remote_ref="refs/remotes/$UPSTREAM_REMOTE/$UPSTREAM_BRANCH"
remote_head="$(git rev-parse "$remote_ref")"
v2_head="$(git rev-parse "$UPSTREAM_BRANCH")"

if [[ "$v2_head" != "$remote_head" ]]; then
    log "Fast-forwarding '$UPSTREAM_BRANCH' to $UPSTREAM_REMOTE/$UPSTREAM_BRANCH..."
    git fetch "$UPSTREAM_REMOTE" "$UPSTREAM_BRANCH:$UPSTREAM_BRANCH"
fi

upstream_changed=0
if ! git merge-base --is-ancestor "$remote_ref" HEAD; then
    upstream_changed=1
    log "Rebasing '$LOCAL_BRANCH' onto '$UPSTREAM_BRANCH'..."
    if ! git rebase "$UPSTREAM_BRANCH"; then
        cat <<EOF >&2

[update-build-run] ERROR: rebase conflict.

Resolve the conflicted files, then:
  git add <files>
  git rebase --continue
  ./tools/update_build_and_run.sh

If upstream edited OpenGame.json, keep the 'com.gryphline.endfield.gp' package
line and merge any new OCR patterns alongside the existing English regexes.

To bail out entirely: git rebase --abort
EOF
        exit 1
    fi
else
    log "No upstream changes — '$LOCAL_BRANCH' already contains $UPSTREAM_REMOTE/$UPSTREAM_BRANCH."
fi

if [[ $upstream_changed -eq 1 ]]; then
    log "Running full workspace update/build/install flow..."
    "$PYTHON_BIN" tools/setup_workspace.py --update
else
    log "Skipping setup_workspace.py (no upstream changes). Set REBUILD=1 to force."
    if [[ "${REBUILD:-0}" == "1" ]]; then
        "$PYTHON_BIN" tools/setup_workspace.py --update
    fi
fi

launcher=""
if [[ -x "$ROOT_DIR/install/mxu" ]]; then
    launcher="$ROOT_DIR/install/mxu"
elif [[ -x "$ROOT_DIR/install/mxu.exe" ]]; then
    launcher="$ROOT_DIR/install/mxu.exe"
else
    fail "could not find a runnable MXU binary in install/"
fi

log "Starting $(basename "$launcher")..."
exec "$launcher"
