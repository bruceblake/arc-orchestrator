#!/usr/bin/env bash
# Install Graft (github.com/trailhq/Graft) — the code-graph CLI graft.py
# drives — into the same node prefix dsh lives in, so the orchestrator's
# service (not a login shell) finds it where config.dsh_bin looks.
#
# Why a script and not a README line: the package's tree-sitter grammars ship
# prebuilt N-API binaries for every language except Kotlin, whose install
# script compiles from source and needs make + gcc. This machine (and a fresh
# WSL box) has neither and no passwordless sudo, so the plain install aborts
# — and graft loads every grammar at startup, so a missing Kotlin binding
# breaks `graft build` on a Python repo. The fallback installs with scripts
# skipped and drops in the prebuilt binding from the maintained
# @tree-sitter-grammars fork (same grammar, N-API, no toolchain).
set -euo pipefail
PREFIX="${NODE_PREFIX:-$HOME/.local/opt/node}"
export PATH="$PREFIX/bin:$PATH"
command -v npm >/dev/null || { echo "npm not found under $PREFIX/bin"; exit 1; }

if npm install -g @nanonets/graft >/tmp/graft-install.log 2>&1; then
    echo "installed @nanonets/graft (native build)"
else
    echo "native install failed (no compiler?) — installing with prebuilt bindings"
    npm install -g --ignore-scripts @nanonets/graft
    KOTLIN="$PREFIX/lib/node_modules/@nanonets/graft/node_modules/tree-sitter-kotlin"
    if [ -d "$KOTLIN" ] && ! ls "$KOTLIN"/prebuilds/linux-x64/*.node >/dev/null 2>&1; then
        tmp="$(mktemp -d)"
        npm pack @tree-sitter-grammars/tree-sitter-kotlin --pack-destination "$tmp" >/dev/null
        tar xzf "$tmp"/*.tgz -C "$tmp"
        mkdir -p "$KOTLIN/build/Release"
        cp "$tmp"/package/prebuilds/linux-x64/*.node "$KOTLIN/build/Release/tree_sitter_kotlin_binding.node"
        rm -rf "$tmp"
        echo "dropped in the prebuilt Kotlin binding"
    fi
fi
DO_NOT_TRACK=1 graft version
echo "graft on: $(command -v graft) — main.py doctor reports it; ARC_GRAFT=0 turns the hints off"
