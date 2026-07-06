#!/usr/bin/env bash
# install.sh — full install / lifecycle manager for the harvest autonomous
# rune responder rig.
#
# Idempotent. Re-running is safe. Works on Termux (Android), Linux, macOS.
# No root required.
#
# ONE-COMMAND INSTALL (fresh Termux / Android / Linux):
#   curl -sSL https://raw.githubusercontent.com/viprocket1/harvest-usdc/main/install.sh | bash
# This auto-detects: if git is available it clones the repo; otherwise it
# fetches the files via the GitHub Contents API. Then installs deps,
# links ~/bin/harvest, adds ~/bin to PATH, and prints next-steps.
#
# Subcommands (the first argument decides):
#   (no args)        bootstrap — clone repo if missing, install deps, symlink,
#                                  write ~/.harvest.env, print next-steps.
#   install          same as no-args; idempotent re-link.
#   uninstall        remove symlink + ~/bin PATH line + ~/.harvest.env + state
#                    (does NOT delete the repo clone).
#   update           `git pull` in the repo, then re-run install.
#   status           show install state: version, link, env, repo, deps.
#   doctor           deeper health: deps reachable, endpoint reachable,
#                    agent id set, machine spec uploadable.
#   start            launch the rig in the current shell (foreground).
#   stop             kill any running harvest.py process.
#   restart          stop + start.
#   logs             tail the rig's stdout/stderr log (if --detach was used).
#   agent [ID]       get/set the persistent agent id (~/.harvest.env).
#   endpoint [URL]   get/set the default fcoin endpoint.
#   deps             install Python deps from requirements.txt.
#   backends         alias for `harvest --backends`.
#   bootstrap        force a from-scratch clone (only if repo missing).
#   help             show this summary.
#
# All subcommands also accept these env overrides:
#   REPO_DIR     where the rig lives (default: ~/spider/llm-usdc)
#   INSTALL_BIN  where the harvest symlink goes (default: ~/bin)
#   ENV_FILE     where persistent config lives (default: ~/.harvest.env)
#
# Examples:
#   curl -sSL https://raw.githubusercontent.com/viprocket1/harvest-usdc/main/install.sh | bash
#   ./install.sh                       # full bootstrap
#   ./install.sh agent termux-rig-01   # pin a stable agent id
#   ./install.sh endpoint https://my-fcoin.example.com
#   ./install.sh status                # what's installed where
#   ./install.sh doctor                # everything OK?
#   ./install.sh update                # git pull + re-link
#   ./install.sh uninstall             # remove everything except the repo

set -euo pipefail

SCRIPT_DIR=""
# Detect how we were invoked:
#   - bash ./install.sh        → BASH_SOURCE set, script on disk
#   - curl ... | bash          → BASH_SOURCE empty, content streamed via stdin
#   - source <(curl ...) bash  → BASH_SOURCE may be /dev/stdin or /dev/fd/...
if [[ "${BASH_SOURCE[0]:-}" != "" ]] \
   && [[ "${BASH_SOURCE[0]:-}" != "/dev/stdin" ]] \
   && [[ "${BASH_SOURCE[0]:-}" != "/dev/fd/"* ]] \
   && [[ -f "${BASH_SOURCE[0]:-}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || SCRIPT_DIR=""
fi
REPO_DIR="${REPO_DIR:-}"
if [[ -z "$REPO_DIR" ]]; then
    if [[ -n "$SCRIPT_DIR" ]] && [[ -f "$SCRIPT_DIR/harvest.py" ]]; then
        # Script lives inside the repo (normal local checkout).
        REPO_DIR="$SCRIPT_DIR"
    else
        # Default for fresh installs — we will create this dir if missing.
        REPO_DIR="$HOME/spider/llm-usdc"
    fi
fi
INSTALL_BIN="${INSTALL_BIN:-$HOME/bin}"
INSTALL_LINK="${INSTALL_BIN}/harvest"
ENV_FILE="${ENV_FILE:-$HOME/.harvest.env}"
LOG_FILE="${LOG_FILE:-$HOME/.harvest.log}"
PID_FILE="${PID_FILE:-$HOME/.harvest.pid}"
SOURCE="${REPO_DIR}/harvest.py"
REQUIREMENTS="${REPO_DIR}/requirements.txt"
REPO_URL="${REPO_URL:-https://github.com/viprocket1/harvest-usdc.git}"
SPIDER_PARENT="$(dirname "$REPO_DIR")"

# ─── color helpers (no-op when not a tty) ───────────────────────────────
if [[ -t 1 ]]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
    C_CYN=$'\033[36m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YEL=""; C_CYN=""; C_DIM=""; C_RST=""
fi
say()  { printf "%s==%s %s\n" "$C_CYN" "$C_RST" "$*"; }
ok()   { printf "%s  ok%s  %s\n" "$C_GRN" "$C_RST" "$*"; }
warn() { printf "%s  warn%s %s\n" "$C_YEL" "$C_RST" "$*" >&2; }
err()  { printf "%s  err%s %s\n" "$C_RED" "$C_RST" "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

# ─── env file ───────────────────────────────────────────────────────────
env_load() {
    [[ -f "$ENV_FILE" ]] || return 0
    # shellcheck disable=SC1090
    set -a; . "$ENV_FILE"; set +a
}
env_save() {
    # Args: key=value pairs. Writes to ENV_FILE atomically.
    local tmp
    tmp="$(mktemp "${ENV_FILE}.XXXX")"
    {
        echo "# harvest persistent config — managed by install.sh"
        echo "# edit with: harvest endpoint <url>   /   harvest agent <id>"
        [[ -n "${HARVEST_AGENT_ID:-}"     ]] && echo "HARVEST_AGENT_ID='${HARVEST_AGENT_ID}'"
        [[ -n "${HARVEST_ENDPOINT:-}"     ]] && echo "HARVEST_ENDPOINT='${HARVEST_ENDPOINT}'"
        [[ -n "${HARVEST_NO_UPDATE:-}"    ]] && echo "HARVEST_NO_UPDATE='${HARVEST_NO_UPDATE}'"
        [[ -n "${HARVEST_LLM_FIRST:-}"    ]] && echo "HARVEST_LLM_FIRST='${HARVEST_LLM_FIRST}'"
    } > "$tmp"
    mv "$tmp" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
}

env_load

# ─── subcommands ────────────────────────────────────────────────────────
cmd_install() {
    say "install"
    echo "  repo    : ${REPO_DIR}"
    echo "  source  : ${SOURCE}"
    echo "  link    : ${INSTALL_LINK}"
    echo "  env     : ${ENV_FILE}"

    if [[ ! -f "$SOURCE" ]]; then
        err "harvest.py not found at $SOURCE"
        err "clone the repo first, or run: ${0##*/} bootstrap"
        return 1
    fi

    # Ensure $HOME exists so we can touch rc files inside it. Termux always
    # has $HOME, but containerized/scratch environments may not.
    mkdir -p "$HOME"

    mkdir -p "$INSTALL_BIN"
    chmod +x "$SOURCE"
    # Belt + suspenders: even after the symlink, the file behind it must be +x
    [[ -x "$SOURCE" ]] || chmod +x "$SOURCE"

    if [[ -e "$INSTALL_LINK" ]] && [[ ! -L "$INSTALL_LINK" ]]; then
        # Pre-existing non-symlink file (e.g. an older wrapper). Move it aside.
        warn "$INSTALL_LINK exists and is not a symlink — backing up"
        mv "$INSTALL_LINK" "${INSTALL_LINK}.bak.$(date +%s)"
    fi
    ln -sf "$SOURCE" "$INSTALL_LINK"
    ok "linked ${INSTALL_LINK} -> ${SOURCE}"

    ensure_path_line "${HOME}/.bashrc"
    ensure_path_line "${HOME}/.profile"
    [[ -f "${HOME}/.zshrc" ]] && ensure_path_line "${HOME}/.zshrc" || true

    if have am; then
        ok "'am' found — 'harvest --new-window' will work"
    fi

    # Touch the env file if missing so future `agent`/`endpoint` writes work.
    [[ -f "$ENV_FILE" ]] || : > "$ENV_FILE"
    chmod 600 "$ENV_FILE"

    cat <<EOF

${C_DIM}== next steps ==${C_RST}

  harvest                       # start the rig in this shell
  harvest --new-window          # pop out into a fresh Termux window
  harvest agent termux-rig-01   # pin a stable agent id (recommended)
  harvest endpoint <url>        # override the default fcoin server
  harvest status                # see what's installed
  harvest doctor                # check deps + endpoint reachability
  harvest update                # pull latest + relink
  harvest uninstall             # remove link + env + pid/log

For real LLM answers (instead of the "y" fallback):
  pkg install ollama && ollama serve    # local, no API key
  export OPENAI_API_KEY=sk-...          # enables \`codex\`
  export GOOGLE_API_KEY=...             # enables \`gemini\`
EOF
}

cmd_uninstall() {
    say "uninstall"
    local removed=0
    if [[ -L "$INSTALL_LINK" ]]; then
        rm -f "$INSTALL_LINK"
        ok "removed symlink $INSTALL_LINK"
        removed=$((removed+1))
    elif [[ -e "$INSTALL_LINK" ]]; then
        warn "$INSTALL_LINK exists but is not a symlink — leaving it alone"
    else
        echo "  no symlink at $INSTALL_LINK"
    fi

    # Optionally remove the PATH line we added (only the marker comment + line).
    strip_path_line() {
        local f="$1"
        [[ -f "$f" ]] || return 0
        if grep -qF '# User-installed scripts (~/bin)' "$f"; then
            # remove the comment block + the export line that follows
            local tmp; tmp="$(mktemp)"
            awk '
                /^# User-installed scripts \(~\/bin\)$/ {skip=1; next}
                skip && /^export PATH="\$HOME\/bin:\$PATH"$/ {skip=0; next}
                skip {next}
                {print}
            ' "$f" > "$tmp" && mv "$tmp" "$f"
            ok "removed ~/bin PATH line from $f"
        fi
    }
    strip_path_line "${HOME}/.bashrc"
    strip_path_line "${HOME}/.profile"
    [[ -f "${HOME}/.zshrc" ]] && strip_path_line "${HOME}/.zshrc" || true

    if [[ -f "$ENV_FILE" ]]; then
        rm -f "$ENV_FILE"
        ok "removed $ENV_FILE"
        removed=$((removed+1))
    fi
    if [[ -f "$PID_FILE" ]]; then
        rm -f "$PID_FILE"
        ok "removed $PID_FILE"
        removed=$((removed+1))
    fi
    if [[ -f "$LOG_FILE" ]]; then
        # keep the log by default; user can delete manually
        echo "  kept log at $LOG_FILE (delete manually if desired)"
    fi

    cmd_stop || true
    if [[ $removed -gt 0 ]]; then
        echo "  done — repo at $REPO_DIR is untouched (run \`rm -rf $REPO_DIR\` to remove)"
    fi
}

cmd_update() {
    say "update"
    if [[ -d "$REPO_DIR/.git" ]]; then
        (cd "$REPO_DIR" && git pull --ff-only 2>&1) || {
            warn "git pull failed — keeping current checkout"
        }
        ok "git pull done"
    else
        warn "$REPO_DIR is not a git checkout — skipping git pull"
        warn "(the in-app [u] key can still self-update harvest.py from GitHub)"
    fi
    # Also trigger the rig's self-updater (it knows how to compare + atomic-write).
    if [[ -x "$SOURCE" ]]; then
        "$SOURCE" --check-update 2>&1 || true
    fi
    cmd_install
}

cmd_status() {
    say "status"
    printf "  %-14s " "version"
    if [[ -x "$SOURCE" ]]; then
        "$SOURCE" --version 2>/dev/null | head -1 || echo "?"
    else
        echo "(missing)"
    fi
    printf "  %-14s %s\n" "repo"        "$REPO_DIR"
    printf "  %-14s %s\n" "source"      "$SOURCE"
    printf "  %-14s " "symlink"
    if [[ -L "$INSTALL_LINK" ]]; then
        local tgt; tgt="$(readlink "$INSTALL_LINK")"
        echo "$INSTALL_LINK -> $tgt"
    elif [[ -e "$INSTALL_LINK" ]]; then
        echo "$INSTALL_LINK (not a symlink)"
    else
        echo "(missing — run 'harvest install')"
    fi
    printf "  %-14s %s\n" "~/bin on PATH" "$([[ ":$PATH:" == *":$INSTALL_BIN:"* ]] && echo yes || echo "no — run 'harvest install'")"
    printf "  %-14s %s\n" "env file"   "${ENV_FILE} $([[ -f "$ENV_FILE" ]] && echo '(exists)' || echo '(missing)')"
    printf "  %-14s %s\n" "agent id"   "${HARVEST_AGENT_ID:-<random per-launch>}"
    printf "  %-14s %s\n" "endpoint"   "${HARVEST_ENDPOINT:-https://fcoin.onrender.com}"
    printf "  %-14s " "running"
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
        echo "yes (pid $(cat "$PID_FILE"))"
    else
        echo "no"
    fi
    printf "  %-14s " "python"
    if have python3; then
        python3 --version
    else
        echo "(missing — install python3)"
    fi
    printf "  %-14s " "deps"
    if have python3 && python3 -c "import psutil, colorama, requests" 2>/dev/null; then
        echo "psutil, colorama, requests — installed"
    elif [[ -f "$REQUIREMENTS" ]]; then
        echo "some missing (run 'harvest deps')"
    else
        echo "(no requirements.txt)"
    fi
}

cmd_doctor() {
    say "doctor"
    local fails=0
    check() {
        local what="$1"; shift
        if "$@" >/dev/null 2>&1; then
            ok "$what"
        else
            err "$what — FAILED"
            fails=$((fails+1))
        fi
    }

    check "python3 present"         have python3
    check "harvest.py present"      test -f "$SOURCE"
    check "harvest.py executable"   test -x "$SOURCE"
    check "~/bin on PATH"           test -d "$INSTALL_BIN"
    check "harvest symlink works"   test -x "$INSTALL_LINK"
    check "requirements.txt present" test -f "$REQUIREMENTS"

    if have python3 && [[ -f "$REQUIREMENTS" ]]; then
        check "python deps importable" python3 -c "import psutil, colorama" 2>&1
    fi

    local ep="${HARVEST_ENDPOINT:-https://fcoin.onrender.com}"
    if have curl; then
        if curl -sSf -m 6 "$ep/" >/dev/null 2>&1 \
            || curl -sSf -m 6 "$ep/stats" >/dev/null 2>&1 \
            || curl -sS  -m 6 "$ep/stats" >/dev/null 2>&1; then
            ok "endpoint reachable: $ep"
        else
            warn "endpoint not reachable: $ep (network or DNS issue)"
            fails=$((fails+1))
        fi
    elif have python3; then
        if python3 -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1]+'/stats',timeout=6)" "$ep" 2>/dev/null; then
            ok "endpoint reachable: $ep"
        else
            warn "endpoint not reachable: $ep"
            fails=$((fails+1))
        fi
    fi

    if have am; then
        ok "am available — 'harvest --new-window' supported"
    else
        echo "  - 'am' not found — --new-window will fall back to bare Termux"
    fi

    if [[ $fails -eq 0 ]]; then
        ok "all checks passed"
        return 0
    else
        err "$fails check(s) failed"
        return 1
    fi
}

cmd_start() {
    say "start"
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
        warn "already running (pid $(cat "$PID_FILE"))"
        return 0
    fi
    [[ -x "$INSTALL_LINK" ]] || { err "$INSTALL_LINK not installed — run 'harvest install'"; return 1; }
    local args=()
    [[ -n "${HARVEST_AGENT_ID:-}"  ]] && args+=(--agent "$HARVEST_AGENT_ID")
    [[ -n "${HARVEST_ENDPOINT:-}"  ]] && args+=(--endpoint "$HARVEST_ENDPOINT")
    [[ "${HARVEST_NO_UPDATE:-0}" == "1" ]] && args+=(--no-update)
    if [[ -t 1 ]]; then
        # foreground — takes over the terminal
        "$INSTALL_LINK" "${args[@]}"
    else
        # detached: write log + pid
        nohup "$INSTALL_LINK" "${args[@]}" >"$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        ok "started in background, pid=$(cat "$PID_FILE"), log=$LOG_FILE"
    fi
}

cmd_stop() {
    say "stop"
    if [[ ! -f "$PID_FILE" ]]; then
        echo "  no pid file at $PID_FILE — nothing to stop"
        return 0
    fi
    local pid; pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        # Give it a moment, then SIGKILL if still alive
        for _ in 1 2 3 4 5; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
            warn "had to SIGKILL pid $pid"
        fi
        ok "stopped pid $pid"
    else
        echo "  pid $pid not alive — clearing stale pidfile"
    fi
    rm -f "$PID_FILE"
}

cmd_restart() { cmd_stop; cmd_start; }

cmd_logs() {
    if [[ ! -f "$LOG_FILE" ]]; then
        warn "no log file at $LOG_FILE (start the rig with 'harvest start' in a non-tty shell)"
        return 1
    fi
    if have tail; then
        tail -n 200 -f "$LOG_FILE"
    else
        cat "$LOG_FILE"
    fi
}

cmd_agent() {
    local new_id="${1:-}"
    env_load
    if [[ -z "$new_id" ]]; then
        printf "agent id: %s\n" "${HARVEST_AGENT_ID:-<random per-launch>}"
        printf "  edit with:  harvest agent <id>\n"
        printf "  clear with: harvest agent --clear\n"
        return 0
    fi
    if [[ "$new_id" == "--clear" || "$new_id" == "clear" ]]; then
        unset HARVEST_AGENT_ID
        env_save
        ok "agent id cleared (will be random per-launch)"
        return 0
    fi
    HARVEST_AGENT_ID="$new_id"
    env_save
    ok "agent id set to: $new_id  (saved to $ENV_FILE)"
}

cmd_endpoint() {
    local new_ep="${1:-}"
    env_load
    if [[ -z "$new_ep" ]]; then
        printf "endpoint: %s\n" "${HARVEST_ENDPOINT:-https://fcoin.onrender.com}"
        printf "  edit with:  harvest endpoint <url>\n"
        printf "  clear with: harvest endpoint --clear\n"
        return 0
    fi
    if [[ "$new_ep" == "--clear" || "$new_ep" == "clear" ]]; then
        unset HARVEST_ENDPOINT
        env_save
        ok "endpoint cleared (using default https://fcoin.onrender.com)"
        return 0
    fi
    HARVEST_ENDPOINT="$new_ep"
    env_save
    ok "endpoint set to: $new_ep  (saved to $ENV_FILE)"
}

cmd_deps() {
    say "deps"
    if ! have python3; then
        err "python3 not found — install it first (Termux: pkg install python)"
        return 1
    fi
    if [[ ! -f "$REQUIREMENTS" ]]; then
        warn "no requirements.txt at $REQUIREMENTS"
        return 1
    fi
    # Try pip via python -m (works across Termux, system, venv)
    python3 -m pip install --user --upgrade -r "$REQUIREMENTS"
    ok "deps installed"
}

cmd_backends() {
    [[ -x "$SOURCE" ]] || { err "$SOURCE not executable"; return 1; }
    "$SOURCE" --backends
}

cmd_bootstrap() {
    say "bootstrap"
    if [[ -f "$SOURCE" ]]; then
        ok "repo already present at $REPO_DIR — running install instead"
        cmd_install
        return $?
    fi
    cmd_fetch_repo || return 1
    cmd_install
    cmd_deps || warn "deps install failed — re-run 'harvest deps' after fixing pip"
}

# Fetch the rig into $REPO_DIR. Prefers `git clone`; falls back to a
# direct download via the GitHub Contents API (avoids raw CDN staleness).
cmd_fetch_repo() {
    mkdir -p "$SPIDER_PARENT"
    if have git; then
        say "fetch (git clone)"
        git clone "$REPO_URL" "$REPO_DIR"
        ok "cloned $REPO_URL -> $REPO_DIR"
        return 0
    fi
    warn "git not found — falling back to GitHub Contents API download"
    if ! have curl; then
        err "neither git nor curl is available — install one (Termux: pkg install git)"
        return 1
    fi
    say "fetch (github api)"
    local api="https://api.github.com/repos/viprocket1/harvest-usdc/contents"
    mkdir -p "$REPO_DIR"
    # Fetch the two files we actually need: harvest.py + requirements.txt.
    # The Contents API returns base64-encoded file blobs.
    local files=(harvest.py requirements.txt README.md HOW-IT-WORKS.md)
    for f in "${files[@]}"; do
        # Prefer raw content via Accept header to skip the base64 step.
        local url="$api/$f"
        if ! curl -sSfL -H "Accept: application/vnd.github.raw" "$url" -o "$REPO_DIR/$f"; then
            # README/HOW-IT-WORKS are optional — only error on the must-haves.
            if [[ "$f" == "harvest.py" || "$f" == "requirements.txt" ]]; then
                err "failed to download $f from GitHub"
                return 1
            fi
        else
            ok "downloaded $f"
        fi
    done
    # Bootstrap a tiny .git so 'harvest update' (which runs git pull) degrades
    # cleanly: it'll just print "not a git checkout" instead of erroring.
    ( cd "$REPO_DIR" && git init -q && git add -A && git -c user.email=h@h -c user.name=harvest commit -q -m "downloaded via install.sh" ) 2>/dev/null || true
    ok "rig files in place at $REPO_DIR"
}

cmd_help() {
    sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ─── shared helpers ─────────────────────────────────────────────────────
ensure_path_line() {
    local f="$1"
    [ -f "$f" ] || touch "$f"
    if ! grep -qF 'export PATH="$HOME/bin:$PATH"' "$f"; then
        printf '\n# User-installed scripts (~/bin)\nexport PATH="$HOME/bin:$PATH"\n' >> "$f"
        ok "added ~/bin to PATH in $f"
    fi
}

# ─── dispatcher ─────────────────────────────────────────────────────────
sub="${1:-}"
shift || true

# No-args: be smart.
#   - If harvest.py is in place (either via symlink or local checkout), re-install.
#   - Otherwise this is a fresh machine — bootstrap (clone or fetch).
if [[ -z "$sub" ]]; then
    if [[ -f "$SOURCE" ]]; then
        sub="install"
    else
        sub="bootstrap"
    fi
fi

case "$sub" in
    install)        cmd_install "$@" ;;
    uninstall|remove|purge) cmd_uninstall "$@" ;;
    update|upgrade) cmd_update "$@" ;;
    status|info)    cmd_status "$@" ;;
    doctor|check|health) cmd_doctor "$@" ;;
    start|run)      cmd_start "$@" ;;
    stop|kill)      cmd_stop "$@" ;;
    restart|reload) cmd_restart "$@" ;;
    logs|log|tail)  cmd_logs "$@" ;;
    agent|agent-id|agentid) cmd_agent "$@" ;;
    endpoint|server|host)   cmd_endpoint "$@" ;;
    deps|requirements)      cmd_deps "$@" ;;
    backends)               cmd_backends "$@" ;;
    bootstrap|init)         cmd_bootstrap "$@" ;;
    help|-h|--help|"")      cmd_help ;;
    *)
        err "unknown subcommand: $sub"
        echo "  run '${0##*/} help' for the list"
        exit 2
        ;;
esac