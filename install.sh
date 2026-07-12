#!/bin/bash
# Simple Loop installer
#
# Copies lib/, core/, modules/, and templates/ to ~/.local/share/simple-loop/
# Symlinks bin/loop to ~/.local/bin/loop
# Installs core skills and agents into ~/.claude/{skills,agents}/ (loop-* prefixed)
#
# Flags:
#   --link    Symlink core skills/agents into ~/.claude/ instead of copying.
#             Edits to the repo propagate immediately to all projects.
#             Recommended for the simple-loop maintainer; coworkers should use
#             the default copy mode and `loop update` to refresh.
#   --force   Install even when the source tree is dirty (uncommitted changes).
#             PROVENANCE.json still records source_dirty:true. Use only when you
#             know the installed code will match no commit.

set -euo pipefail

LINK_MODE=false
FORCE=false
for arg in "$@"; do
    case "$arg" in
        --link) LINK_MODE=true ;;
        --force) FORCE=true ;;
        -h|--help)
            sed -n '1,15p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "Unknown flag: $arg" >&2; exit 1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${SIMPLE_LOOP_HOME:-$HOME/.local/share/simple-loop}"
BIN_DIR="$HOME/.local/bin"
CLAUDE_DIR="$HOME/.claude"

# ── Refuse to install from a dirty source tree ──
# A dirty tree means the deployed code matches no commit, so PROVENANCE's
# source_commit is a lie and nobody can reproduce what's running. Uses the same
# `git status --porcelain` detection that write_provenance.py records as
# source_dirty. Not a git repo (tarball/download install) => skip with a note.
if git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    DIRTY_FILES="$(git -C "$SCRIPT_DIR" status --porcelain)"
    if [ -n "$DIRTY_FILES" ]; then
        if [ "$FORCE" = true ]; then
            echo ""
            echo "  ############################################################"
            echo "  # WARNING: --force installing from a DIRTY source tree.     #"
            echo "  # The installed code will match NO commit. PROVENANCE.json  #"
            echo "  # will record source_dirty:true. You are on your own.       #"
            echo "  ############################################################"
            echo ""
            echo "$DIRTY_FILES" | sed 's/^/    /'
            echo ""
        else
            echo "" >&2
            echo "  Dirty source tree — uncommitted changes:" >&2
            echo "$DIRTY_FILES" | sed 's/^/    /' >&2
            echo "" >&2
            echo "  refusing to install from a dirty tree — the installed code would match no commit; commit or stash first, or pass --force" >&2
            exit 1
        fi
    fi
else
    echo "  Note: source is not a git repository — skipping dirty-tree check (provenance will record commit 'unknown')."
fi

echo ""
echo "Installing Simple Loop v0.2..."
echo "  Source:    $SCRIPT_DIR"
echo "  Install:   $INSTALL_DIR"
echo "  Binary:    $BIN_DIR/loop"
echo "  Claude:    $CLAUDE_DIR (workstation skills + agents)"
echo "  Mode:      $([ "$LINK_MODE" = true ] && echo 'symlink (live edits)' || echo 'copy (snapshot)')"
echo ""

# Create directories
mkdir -p "$INSTALL_DIR"/{lib,templates/prompts}
mkdir -p "$INSTALL_DIR"/core/{agents,skills,templates}
mkdir -p "$BIN_DIR"

# Record source provenance for `loop info` / `loop status` / daemon startup log
# (issue #41: version-visibility primitive — PROVENANCE.json is the install
# artifact; nothing here is coordination state). Prints old SHA -> new SHA so
# the install itself is a receipt.
python3 "$SCRIPT_DIR/lib/write_provenance.py" "$SCRIPT_DIR" "$INSTALL_DIR" || echo "  warning: PROVENANCE.json generation failed (non-fatal; 'loop info'/'loop status' will show 'unknown' provenance)"

# Copy lib (daemon runtime)
cp "$SCRIPT_DIR/lib/daemon.sh" "$INSTALL_DIR/lib/"
# All top-level lib modules ship — glob, not a hand-list (a new module missing
# the bundle cost us loop-why + failure_tracker on 2026-07-12; the list is the
# ghost-file bug's inverse). Test files stay source-only; lib/scouts/ subdir
# stays source-only (training watcher, not runtime).
for _libf in "$SCRIPT_DIR"/lib/*.py; do
    case "$(basename "$_libf")" in
        *_test.py|*-test.py|test_*.py) continue ;;
    esac
    cp "$_libf" "$INSTALL_DIR/lib/"
done
cp "$SCRIPT_DIR/lib/lint.py" "$INSTALL_DIR/lib/" 2>/dev/null || true
chmod +x "$INSTALL_DIR/lib/daemon.sh"

# Copy daemon templates (per-project scaffolding for `loop init`)
cp "$SCRIPT_DIR/templates/config.sh" "$INSTALL_DIR/templates/"
cp "$SCRIPT_DIR/templates/brief-template.md" "$INSTALL_DIR/templates/"
cp "$SCRIPT_DIR/templates/prompts/"*.md "$INSTALL_DIR/templates/prompts/"
cp "$SCRIPT_DIR/templates/com.scaviefae.simpleloop.plist" "$INSTALL_DIR/templates/"

# brief-165 presence plane — vendor the intent hooks + the hum plist template +
# the presence-plane services (hum shipper, apiary bus) into the install so the
# harness carries them, not portal. Models the core/skills copy loop above.
# Test files stay source-only (same rule as lib/).
if [ -d "$SCRIPT_DIR/scripts" ]; then
    mkdir -p "$INSTALL_DIR/scripts"
    for _s in "$SCRIPT_DIR"/scripts/intent-journal.py \
              "$SCRIPT_DIR"/scripts/intent-hook-record.py \
              "$SCRIPT_DIR"/scripts/intent-hook-inject.py; do
        [ -f "$_s" ] || continue
        cp "$_s" "$INSTALL_DIR/scripts/"
        echo "  Intent hook: $(basename "$_s")"
    done
fi
if [ -d "$SCRIPT_DIR/hum" ]; then
    mkdir -p "$INSTALL_DIR/hum"
    cp "$SCRIPT_DIR/hum/hum.py" "$INSTALL_DIR/hum/" 2>/dev/null || true
    cp "$SCRIPT_DIR/hum/com.scaviefae.hum.plist" "$INSTALL_DIR/templates/" 2>/dev/null || true
    echo "  Presence: hum shipper + com.scaviefae.hum.plist template"
fi
if [ -d "$SCRIPT_DIR/apiary" ]; then
    mkdir -p "$INSTALL_DIR/apiary"
    cp "$SCRIPT_DIR/apiary/apiary.py" "$INSTALL_DIR/apiary/" 2>/dev/null || true
    echo "  Presence: apiary bus (local-run; deploy is Mattie's gate)"
fi

# Copy docs (conventions, operating docs, templates used by `loop init`)
if [ -d "$SCRIPT_DIR/docs" ]; then
    cp -r "$SCRIPT_DIR/docs" "$INSTALL_DIR/docs"
fi

# Copy v2 core
if [ -d "$SCRIPT_DIR/core" ]; then
    # Core agents
    cp "$SCRIPT_DIR/core/agents/"*.md "$INSTALL_DIR/core/agents/" 2>/dev/null || true

    # Core skills (preserve directory structure)
    if [ -d "$SCRIPT_DIR/core/skills" ]; then
        for skill_dir in "$SCRIPT_DIR/core/skills"/*/; do
            [ -d "$skill_dir" ] || continue
            local_name=$(basename "$skill_dir")
            mkdir -p "$INSTALL_DIR/core/skills/$local_name"
            cp "$skill_dir"* "$INSTALL_DIR/core/skills/$local_name/" 2>/dev/null || true
        done
    fi

    # Core templates
    cp "$SCRIPT_DIR/core/templates/"* "$INSTALL_DIR/core/templates/" 2>/dev/null || true
fi

# ── Workstation install: core skills and agents into ~/.claude/ ──
# Skills land at ~/.claude/skills/loop-<name>/, agents at ~/.claude/agents/loop-<name>.md.
# The loop- prefix namespaces them so they don't collide with the user's own files.
mkdir -p "$CLAUDE_DIR/skills" "$CLAUDE_DIR/agents"

install_skill() {
    local src_dir="$1"
    local name
    name=$(basename "$src_dir")
    local target="$CLAUDE_DIR/skills/loop-${name}"
    rm -rf "$target"
    if [ "$LINK_MODE" = true ]; then
        ln -s "$src_dir" "$target"
    else
        cp -R "$src_dir" "$target"
    fi
    echo "  Skill: /loop-${name}"
}

install_agent() {
    local src_file="$1"
    local name
    name=$(basename "$src_file" .md)
    local target="$CLAUDE_DIR/agents/loop-${name}.md"
    rm -f "$target"
    if [ "$LINK_MODE" = true ]; then
        ln -s "$src_file" "$target"
    else
        cp "$src_file" "$target"
    fi
    echo "  Agent: loop-${name}"
}

if [ -d "$SCRIPT_DIR/core/skills" ]; then
    for skill_dir in "$SCRIPT_DIR/core/skills"/*/; do
        [ -d "$skill_dir" ] || continue
        # Trim trailing slash so basename works
        install_skill "${skill_dir%/}"
    done
fi

if [ -d "$SCRIPT_DIR/core/agents" ]; then
    for agent_file in "$SCRIPT_DIR/core/agents"/*.md; do
        [ -f "$agent_file" ] || continue
        install_agent "$agent_file"
    done
fi

# Copy v2 modules
if [ -d "$SCRIPT_DIR/modules" ]; then
    for module_dir in "$SCRIPT_DIR/modules"/*/; do
        [ -d "$module_dir" ] || continue
        module_name=$(basename "$module_dir")
        echo "  Module: $module_name"

        # Recreate module structure
        mkdir -p "$INSTALL_DIR/modules/$module_name"

        # Copy module.json
        cp "$module_dir/module.json" "$INSTALL_DIR/modules/$module_name/" 2>/dev/null || true

        # Copy agents
        if [ -d "$module_dir/agents" ]; then
            mkdir -p "$INSTALL_DIR/modules/$module_name/agents"
            cp "$module_dir/agents/"*.md "$INSTALL_DIR/modules/$module_name/agents/" 2>/dev/null || true
        fi

        # Copy skills (preserve directory structure)
        if [ -d "$module_dir/skills" ]; then
            for skill_dir in "$module_dir/skills"/*/; do
                [ -d "$skill_dir" ] || continue
                skill_name=$(basename "$skill_dir")
                mkdir -p "$INSTALL_DIR/modules/$module_name/skills/$skill_name"
                cp "$skill_dir"* "$INSTALL_DIR/modules/$module_name/skills/$skill_name/" 2>/dev/null || true
            done
        fi

        # Copy state schema
        if [ -d "$module_dir/state" ]; then
            mkdir -p "$INSTALL_DIR/modules/$module_name/state"
            cp "$module_dir/state/"*.json "$INSTALL_DIR/modules/$module_name/state/" 2>/dev/null || true
        fi

        # Copy claude-instructions
        cp "$module_dir/claude-instructions.md" "$INSTALL_DIR/modules/$module_name/" 2>/dev/null || true
    done
fi

# Copy bin/loop
cp "$SCRIPT_DIR/bin/loop" "$INSTALL_DIR/bin-loop"
chmod +x "$INSTALL_DIR/bin-loop"

# Symlink to PATH
ln -sf "$INSTALL_DIR/bin-loop" "$BIN_DIR/loop"

# ── Build and install hive TUI (requires Rust/cargo) ──
if [ -d "$SCRIPT_DIR/crates/hive" ]; then
    CARGO="${CARGO_HOME:-$HOME/.cargo}/bin/cargo"
    if [ -x "$CARGO" ] || command -v cargo >/dev/null 2>&1; then
        CARGO="${CARGO:-cargo}"
        echo "  Building hive TUI..."
        "$CARGO" build --release --manifest-path "$SCRIPT_DIR/crates/hive/Cargo.toml" --quiet
        cp "$SCRIPT_DIR/target/release/hive" "$BIN_DIR/hive"
        chmod +x "$BIN_DIR/hive"
        # Re-sign after cp: cargo emits a linker-signed adhoc binary, and on
        # macOS 26+ Taskgated rejects the signature once the file is copied
        # (SIGKILL "Code Signature Invalid" on launch). codesign --force --sign -
        # restamps an adhoc signature in place. No-op on non-Darwin.
        if command -v codesign >/dev/null 2>&1; then
            codesign --force --sign - "$BIN_DIR/hive" 2>/dev/null || true
        fi
        echo "  Binary: $BIN_DIR/hive"
    else
        echo "  Warning: cargo not found — hive TUI not installed."
        echo "  Install Rust (https://rustup.rs/) then re-run install.sh."
    fi
fi

echo ""
echo "Installed."
echo ""

# Check PATH
if ! echo "$PATH" | grep -q "$BIN_DIR"; then
    echo "  Note: $BIN_DIR is not in your PATH."
    echo "  Add this to your shell profile:"
    echo ""
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
fi

# Check Agent Teams (experimental, gated by an env var; requires Claude Code v2.1.32+)
if command -v claude >/dev/null 2>&1; then
    settings_file="$CLAUDE_DIR/settings.json"
    if [ ! -f "$settings_file" ] || ! grep -q "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" "$settings_file" 2>/dev/null; then
        echo "  Recommendation: enable Agent Teams for best results (Claude Code v2.1.32+)"
        echo "    Add to ~/.claude/settings.json:"
        echo '      "env": { "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1" }'
        echo ""
    fi
fi

echo "  Run 'loop help' to get started."
echo "  Run 'loop init' in a project directory to set up."
echo ""
