#!/usr/bin/env bash
# install.sh — install the harvest autonomous rune responder rig.
#
# Idempotent. Re-running is safe. Works on Termux (Android), Linux, macOS.
# No root required.
#
# Assumes you've cloned the rig at $REPO_DIR (default: ~/spider/llm-usdc).
# On Termux the install script also supports being run from a copy
# outside ~/spider/ — it will use the script's own directory.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Default to the spider/llm-usdc layout; fall back to the script's own dir.
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd || echo "$SCRIPT_DIR")}"
# If we were actually invoked from a llm-usdc directory, use that.
if [[ ! -f "$REPO_DIR/harvest.py" ]] && [[ -f "$SCRIPT_DIR/harvest.py" ]]; then
    REPO_DIR="$SCRIPT_DIR"
fi

INSTALL_BIN="${HOME}/bin"
INSTALL_LINK="${INSTALL_BIN}/harvest"
SOURCE="${REPO_DIR}/harvest.py"

echo "== harvest install =="
echo "repo    : ${REPO_DIR}"
echo "source  : ${SOURCE}"
echo "link    : ${INSTALL_LINK}"

# 1) make sure ~/bin exists and is on PATH
mkdir -p "${INSTALL_BIN}"

# 2) symlink the CLI so plain `harvest` works
chmod +x "${SOURCE}"
ln -sf "${SOURCE}" "${INSTALL_LINK}"
# Belt and suspenders: ensure the script is +x even if the symlink got
# followed to a non-executable target during a copy/edit
[ -x "${SOURCE}" ] || chmod +x "${SOURCE}"

# 3) ensure ~/.bashrc and ~/.profile put ~/bin on PATH
ensure_path_line() {
    local f="$1"
    [ -f "$f" ] || touch "$f"
    if ! grep -qF 'export PATH="$HOME/bin:$PATH"' "$f"; then
        # insert after any existing 'export PATH' lines, or at end
        printf '\n# User-installed scripts (~/bin)\nexport PATH="$HOME/bin:$PATH"\n' >> "$f"
        echo "  + added ~/bin to PATH in $f"
    fi
}
ensure_path_line "${HOME}/.bashrc"
ensure_path_line "${HOME}/.profile"

# 4) Termux-specific: ensure `am` (activity manager) is available
#    (it's bundled with com.termux, no install needed). Just print a hint
#    about `harvest --new-window` for users who want a separate window.
if command -v am >/dev/null 2>&1; then
    echo "  + 'am' found — 'harvest --new-window' is supported"
fi

# 5) optional: tell the user about ollama / codex / gemini
cat <<'EOF'

== next steps ==

  harvest              # start the rig in this shell
  harvest --new-window # pop out into a fresh Termux window
  harvest --backends   # show which LLM CLIs are detected on this machine

For real LLM answers (instead of the "y" fallback), start one of:
  ollama serve                         # local, no API key
  export OPENAI_API_KEY=sk-...         # enables `codex`
  export GOOGLE_API_KEY=...            # enables `gemini`

Repo layout: ~/spider/ contains both the rig (llm-usdc) and the
optional rune server source (fcoin) side-by-side, each its own git repo.

Re-run this script any time to refresh the install.
EOF
