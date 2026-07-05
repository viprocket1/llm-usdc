#!/usr/bin/env bash
# install.sh — install the usdc autonomous fcoin responder rig.
#
# Idempotent. Re-running is safe. Works on Termux (Android), Linux, macOS.
# No root required.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_BIN="${HOME}/bin"
INSTALL_LINK="${INSTALL_BIN}/usdc"
SOURCE="${REPO_DIR}/usdc.py"

echo "== usdc install =="
echo "source  : ${SOURCE}"
echo "link    : ${INSTALL_LINK}"

# 1) make sure ~/bin exists and is on PATH
mkdir -p "${INSTALL_BIN}"

# 2) symlink the CLI so plain `usdc` works
chmod +x "${SOURCE}"
# chmod follows symlinks, so this ensures the script (not the symlink) is +x
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
#    about `usdc --new-window` for users who want a separate window.
if command -v am >/dev/null 2>&1; then
    echo "  + 'am' found — 'usdc --new-window' is supported"
fi

# 5) optional: tell the user about ollama / codex / gemini
cat <<'EOF'

== next steps ==

  usdc                # start the rig in this shell
  usdc --new-window   # pop out into a fresh Termux window

For real LLM answers (instead of the "hi back" stub), start one of:
  ollama serve                         # local, no API key
  export OPENAI_API_KEY=sk-...         # enables `codex`
  export GOOGLE_API_KEY=...            # enables `gemini`

Re-run this script any time to refresh the install.
EOF
