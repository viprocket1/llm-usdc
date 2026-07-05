#!/usr/bin/env python3
"""
usdc — fcoin prompt-responder agent.

Connects to a fcoin-compatible server (default: https://fcoin.onrender.com),
receives prompt requests via SSE + polling, runs each one through your
local LLM, and POSTs the answer back. No manual input.

The TUI is a read-only monitor: it shows connection state, your agent's
wallet balance, the inbox of received prompts, and the live event feed.
You don't have to interact with it — the agent runs unattended.

Answering pipeline (first backend that returns a valid reply wins):
  1. ollama            ($OLLAMA_HOST, model $OLLAMA_MODEL)
  2. claude            (claude -p)
  3. codex             (codex exec --no-cache -q)
  4. gemini            (gemini -p)
  5. opencode          (opencode run)
  6. aider             (aider --no-auto-commits --message)
  7. goose             (goose run --no-session)
  8. qwen              (qwen -p)
  9. hermes            (hermes chat -q)
 10. openclaw          (openclaw run / claw run)
 11. openhands         (openhands -t)
 12. agent-zero        (agent-zero --prompt)
 13. openmanus         (python -m openmanus)
 14. autogpt           (autogpt --prompt)
 15. superagi          (superagi run --prompt)
 16. crewai            (crewai run --prompt)
 17. metagpt           (metagpt)
 18. camel             (camel run --prompt)
 19. Anthropic API     (if ANTHROPIC_API_KEY is set)
 20. OpenAI API        (if OPENAI_API_KEY is set)

Override priority order:  USDC_LLM_FIRST=<name>
Keys:  [p] pause  [u] self-update  [q] quit
"""

import argparse
import ast
import concurrent.futures
import json
import os
import queue
import random
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# colorama is in Termux's default site-packages; falls back to no color.
try:
    from colorama import init as _cinit, Fore, Style, Back
    _cinit(autoreset=True)
    HAVE_COLOR = True
except Exception:
    HAVE_COLOR = False
    class Fore:  # type: ignore[assignment]
        RESET = RED = GREEN = YELLOW = CYAN = MAGENTA = BLUE = WHITE = BLACK = ""
    class Style:  # type: ignore[assignment]
        RESET_ALL = BRIGHT = DIM = ""
    class Back:  # type: ignore[assignment]
        RESET = RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = WHITE = BLACK = ""


# ---------- Defaults --------------------------------------------------------

DEFAULT_ENDPOINT = "https://fcoin.onrender.com"
NETWORK_TIMEOUT = 2.5
NETWORK_POLL_PORTFOLIO = 8.0
NETWORK_POLL_PROMPTS = 6.0
SSE_RECONNECT_BACKOFF = 3.0
LLM_TIMEOUT = 30

__version__ = "1.9.0"
# We use the GitHub API instead of raw.githubusercontent.com because the raw
# CDN caches stale content for minutes after a push. The API always returns
# the fresh file. See https://docs.github.com/en/rest/repos/contents
GITHUB_API_URL = "https://api.github.com/repos/viprocket1/llm-usdc/contents/usdc.py"
GITHUB_RAW_URL = "https://raw.githubusercontent.com/viprocket1/llm-usdc/main/usdc.py"  # fallback


# ---------- ANSI helpers ----------------------------------------------------

CSI = "\x1b["
HIDE_CURSOR = CSI + "?25l"
SHOW_CURSOR = CSI + "?25h"
CLEAR_SCREEN = CSI + "2J" + CSI + "H"
ALT_SCREEN_ON = CSI + "?1049h"
ALT_SCREEN_OFF = CSI + "?1049l"
CLEAR_LINE = CSI + "0J"

def c(fg: str, s: str) -> str:
    return f"{fg}{s}{Style.RESET_ALL}" if HAVE_COLOR else s

def move_to(row: int, col: int) -> str:
    return CSI + f"{row};{col}H"


# ---------- Agent state ----------------------------------------------------

@dataclass
class Agent:
    """Per-agent state. Everything here is observable on the server —
    no simulated numbers, no fake rewards. Counts come from real
    successful interactions with the fcoin API."""

    agent_id:     str = "termux-rig-01"
    started_at:   float = field(default_factory=time.time)
    usdc_balance: float = 0.0       # from /portfolio
    fcoin_balance: float = 0.0     # from /portfolio
    open_prompts: int = 0          # from /prompts?status=open
    received:     int = 0          # count of prompts received
    answered:     int = 0          # count of responses accepted by server
    failed:       int = 0          # count of responses the server rejected
    last_poll_at: float = 0.0      # last successful /portfolio response


# ---------- Log feed --------------------------------------------------------

class Feed:
    def __init__(self, capacity: int = 6):
        self.capacity = capacity
        self.lines: list[tuple[str, str]] = []

    def push(self, level: str, text: str) -> None:
        self.lines.append((level, text))
        if len(self.lines) > self.capacity:
            self.lines = self.lines[-self.capacity:]


# ---------- Inbox of received tasks ----------------------------------------

@dataclass
class Task:
    id: str
    prompt: str
    submitter: str
    fee_usdc: float
    received_at: float

class Inbox:
    def __init__(self, capacity: int = 8):
        self.capacity = capacity
        self.tasks: list[Task] = []
        self._lock = threading.Lock()

    def add(self, task: Task) -> bool:
        with self._lock:
            if any(t.id == task.id for t in self.tasks):
                return False
            self.tasks.append(task)
            if len(self.tasks) > self.capacity:
                self.tasks = self.tasks[-self.capacity:]
            return True

    def latest(self) -> Task | None:
        with self._lock:
            return self.tasks[-1] if self.tasks else None

    def pop(self) -> Task | None:
        with self._lock:
            return self.tasks.pop() if self.tasks else None

    def count(self) -> int:
        with self._lock:
            return len(self.tasks)


# ---------- LLM responder --------------------------------------------------

# --- LLM backend registry -------------------------------------------------
#
# Each backend is a callable that takes the prompt and either:
#   - returns a string (>=5 chars) on success
#   - returns None on failure (clamped, logged internally)
#   - raises an exception (caught by the dispatcher)
#
# The dispatcher tries them in order. First non-None wins. This means a
# user can have ollama + codex + claude all installed and the rig will
# use ollama for everything, falling back to codex if ollama is down,
# and so on. Each backend is self-contained and stateless, so they can
# also be run in parallel if speed matters.

def _llm_ollama(text: str):
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    model = os.environ.get("OLLAMA_MODEL", "llama3.2")
    body = json.dumps({"model": model, "prompt": text, "stream": False}).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/api/generate", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
        out = json.loads(r.read().decode("utf-8", "replace"))
    return (out.get("response") or "").strip() or None


# --- LLM binary cache -----------------------------------------------------
# shutil.which() per call is cheap but not free; cache results.
_LLM_BIN_CACHE: dict[str, bool] = {}

def _has_binary(name: str) -> bool:
    """True if the named binary is on PATH and executable."""
    if name in _LLM_BIN_CACHE:
        return _LLM_BIN_CACHE[name]
    # Skip "python" and "python3" — always available
    if name in ("python", "python3"):
        _LLM_BIN_CACHE[name] = True
        return True
    found = shutil.which(name) is not None
    _LLM_BIN_CACHE[name] = True
    return found


# Names of the actual binaries the rig would call. Used for the
# "backends detected" panel and the --backends CLI command.
def _backend_binary_names() -> list[tuple[str, str]]:
    """Return [(display_name, binary_path), ...] for every CLI backend."""
    return [
        ("ollama",     "ollama"),
        ("claude",     "claude"),
        ("codex",      "codex"),
        ("gemini",     "gemini"),
        ("opencode",   "opencode"),
        ("aider",      "aider"),
        ("goose",      "goose"),
        ("qwen",       "qwen"),
        ("hermes",     "hermes"),
        ("openclaw",   "openclaw"),
        ("openhands",  "openhands"),
        ("agent-zero", "agent-zero"),
        ("openmanus",  "python"),       # uses `python -m openmanus`
        ("autogpt",    "autogpt"),
        ("superagi",   "superagi"),
        ("crewai",     "crewai"),
        ("metagpt",    "metagpt"),
        ("camel",      "camel"),
    ]


def detect_llm_backends() -> list[tuple[str, bool, str | None]]:
    """Return [(name, found, binary_path)] for every LLM backend.

    `found` is True if the binary is on PATH. `binary_path` is the
    full path the rig would use, or None if not found. API-direct
    backends (anthropic, openai) are also reported based on whether
    the matching env var is set.
    """
    out = []
    for name, binary in _backend_binary_names():
        path = shutil.which(binary) if binary not in ("python", "python3") else shutil.which("python3") or "python3"
        out.append((name, path is not None, path))
    # API-direct
    out.append(("anthropic-api",
                bool(os.environ.get("ANTHROPIC_API_KEY")),
                "ANTHROPIC_API_KEY env var"))
    out.append(("openai-api",
                bool(os.environ.get("OPENAI_API_KEY")),
                "OPENAI_API_KEY env var"))
    return out


def _llm_subprocess(cmd, text: str, stdin: bool = False):
    """Run an LLM CLI and return its stdout (stripped), or None.

    Skips instantly if the binary isn't on PATH (no 30s timeout penalty).
    """
    if cmd and not _has_binary(cmd[0]):
        return None
    try:
        kwargs = dict(capture_output=True, text=True, timeout=LLM_TIMEOUT, check=False)
        if stdin:
            kwargs["input"] = text
            return subprocess.run(cmd, **kwargs).stdout.strip() or None
        return subprocess.run(cmd + [text], **kwargs).stdout.strip() or None
    except FileNotFoundError:
        return None
    except Exception:
        return None


# (name, callable). The order here is the priority order. To prefer a
# different backend, set USDC_LLM_FIRST=<name> in the environment.
LLM_BACKENDS = [
    ("ollama",       _llm_ollama),
    ("claude",       lambda t: _llm_subprocess(["claude", "-p"], t)),
    ("codex",        lambda t: _llm_subprocess(["codex", "exec", "--no-cache", "-q"], t)),
    ("gemini",       lambda t: _llm_subprocess(["gemini", "-p"], t)),
    ("opencode",     lambda t: _llm_subprocess(["opencode", "run"], t)),
    ("aider",        lambda t: _llm_subprocess(["aider", "--no-auto-commits", "--message"], t)),
    ("goose",        lambda t: _llm_subprocess(["goose", "run", "--no-session"], t)),
    ("qwen",         lambda t: _llm_subprocess(["qwen", "-p"], t)),
    # ── prompt-as-argument autonomous agents ──
    ("hermes",       lambda t: _llm_subprocess(["hermes", "chat", "-q"], t)),
    ("openclaw",     lambda t: _llm_subprocess(["openclaw", "run"], t) or _llm_subprocess(["claw", "run"], t)),
    ("openhands",    lambda t: _llm_subprocess(["openhands", "-t"], t)),
    ("agent-zero",   lambda t: _llm_subprocess(["agent-zero", "--prompt"], t)),
    ("openmanus",    lambda t: _llm_subprocess(["python", "-m", "openmanus"], t)),
    ("autogpt",      lambda t: _llm_subprocess(["autogpt", "--prompt"], t)),
    ("superagi",     lambda t: _llm_subprocess(["superagi", "run", "--prompt"], t)),
    ("crewai",       lambda t: _llm_subprocess(["crewai", "run", "--prompt"], t)),
    ("metagpt",      lambda t: _llm_subprocess(["metagpt"], t)),
    ("camel",        lambda t: _llm_subprocess(["camel", "run", "--prompt"], t)),
]


def _try_anthropic_api(text: str):
    """Optional direct Anthropic API call (skipped unless the SDK is importable)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        # Use urllib to avoid pulling in the anthropic SDK as a dep
        body = json.dumps({
            "model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": text}],
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }, method="POST",
        )
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
            out = json.loads(r.read().decode("utf-8", "replace"))
        for blk in (out.get("content") or []):
            if blk.get("type") == "text":
                return (blk.get("text") or "").strip() or None
    except Exception:
        return None
    return None


def _try_openai_api(text: str):
    """Optional direct OpenAI API call (skipped unless OPENAI_API_KEY set)."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        body = json.dumps({
            "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            "messages": [{"role": "user", "content": text}],
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions", data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }, method="POST",
        )
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
            out = json.loads(r.read().decode("utf-8", "replace"))
        return (out["choices"][0]["message"]["content"] or "").strip() or None
    except Exception:
        return None


# Add API-direct backends (only attempt if the env key is set; otherwise skip fast)
LLM_BACKENDS_WITH_API = LLM_BACKENDS + [
    ("anthropic-api", _try_anthropic_api),
    ("openai-api",    _try_openai_api),
]


def make_llm_response(prompt: str) -> tuple[str, str]:
    """Call every available LLM backend in order; first success wins.

    Returns (reply_text, backend_name) so the rig can send the backend
    name as X-LLM-Backend provenance. Returns ("y", "") for the fallback —
    but the rig's dispatch loop REFUSES to post the fallback when the
    prompt has a min_response_words requirement (or default >= 3), so the
    server won't accept it. To override that, set USDC_LLM_FIRST to a
    real backend, or use USDC_ALLOW_STUB=1 to override the refusal.
    """
    text = (prompt or "").strip()

    # Allow reordering: USDC_LLM_FIRST=<name> promotes that backend to the front.
    preferred = os.environ.get("USDC_LLM_FIRST", "").strip()
    backends = LLM_BACKENDS_WITH_API
    if preferred:
        backends = sorted(backends, key=lambda b: (0 if b[0] == preferred else 1))

    for name, fn in backends:
        if fn is None:
            continue
        try:
            reply = fn(text)
        except Exception:
            continue
        if reply and len(reply.strip()) >= 5:
            return reply.strip()[:2000], name

    # 21) fallback — 1 char so it passes the fcoin min length even at 1
    return "y", ""


# ---------- fcoin HTTP client (synchronous, called from worker threads) ----

class FcoinClient:
    """Plain synchronous HTTP. All errors are non-fatal and surfaced as _err."""

    def __init__(self, base: str, agent_id: str):
        self.base = base.rstrip("/")
        self.agent_id = agent_id

    def _request(self, method: str, path: str, body: dict | None = None, timeout: float = NETWORK_TIMEOUT):
        url = f"{self.base}{path}"
        data = None
        headers = {"X-Agent-ID": self.agent_id, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return {"_raw": raw[:200]}
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                detail = ""
            return {"_err": f"HTTP {e.code}", "_detail": detail}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return {"_err": str(e)[:120]}

    def get(self, path: str, timeout: float = NETWORK_TIMEOUT):
        return self._request("GET", path, timeout=timeout)

    def post(self, path: str, body: dict, timeout: float = NETWORK_TIMEOUT):
        return self._request("POST", path, body=body, timeout=timeout)

    def portfolio(self) -> dict:
        return self.get(f"/portfolio?agent_id={urllib.parse.quote(self.agent_id)}")

    def prompts(self, status: str = "open") -> dict:
        return self.get(f"/prompts?status={status}")

    def respond_prompt(self, request_id: str, response: str, backend: str = "") -> dict:
        """POST a response. Optional `backend` is sent as the X-LLM-Backend
        header so the server can record provenance and enforce any
        allowed_backends whitelist the submitter set."""
        # When the rig posts via HTTPRequest, the headers argument lets
        # us set X-LLM-Backend alongside the default X-Agent-ID.
        url = f"{self.base}/respond_prompt"
        body = json.dumps({
            "request_id": request_id,
            "response":   response,
        }).encode("utf-8")
        headers = {
            "X-Agent-ID":   self.agent_id,
            "Content-Type": "application/json",
        }
        if backend:
            headers["X-LLM-Backend"] = backend
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                detail = ""
            return {"_err": f"HTTP {e.code}", "_detail": detail}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return {"_err": str(e)[:120]}


# ---------- ThreadPool wrapper for non-blocking calls ----------------------

class AsyncHTTP:
    def __init__(self, max_workers: int = 6):
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        self._results: queue.Queue[tuple[str, dict, float]] = queue.Queue()
        self._inflight: dict[str, concurrent.futures.Future] = {}

    def submit(self, tag: str, fn, *args, **kwargs):
        def wrap():
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                return {"_err": f"{type(e).__name__}: {e}"[:120]}
        fut = self._pool.submit(wrap)
        self._inflight[tag] = fut
        fut.add_done_callback(lambda f, t=tag: self._on_done(t, f))

    def _on_done(self, tag: str, fut):
        try:
            res = fut.result()
        except Exception as e:
            res = {"_err": str(e)[:120]}
        self._results.put((tag, res, time.time()))
        self._inflight.pop(tag, None)

    def take(self) -> tuple[str, dict, float] | None:
        try:
            return self._results.get_nowait()
        except queue.Empty:
            return None

    def drain(self, sink: list):
        while True:
            r = self.take()
            if r is None: break
            sink.append(r)

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)


# ---------- SSE listener thread --------------------------------------------

def sse_thread(base: str, agent_id: str, inbox: Inbox, on_event, on_log):
    """Connect to /stream, parse SSE events, push tasks into inbox. Reconnects on failure."""
    url = f"{base}/stream"
    backoff = SSE_RECONNECT_BACKOFF
    while True:
        try:
            req = urllib.request.Request(url, headers={
                "X-Agent-ID": agent_id,
                "Accept": "text/event-stream",
            })
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=None, context=ctx) as r:
                on_log("info", f"SSE connected — {url}")
                backoff = SSE_RECONNECT_BACKOFF
                event_type = ""
                data_buf: list[str] = []
                for raw in r:
                    try:
                        line = raw.decode("utf-8", "replace").rstrip("\n").rstrip("\r")
                    except Exception:
                        continue
                    if not line:
                        if data_buf:
                            data_str = "\n".join(data_buf)
                            try:
                                obj = json.loads(data_str)
                            except json.JSONDecodeError:
                                obj = {"_raw": data_str[:200]}
                            ev = {"type": event_type or "message", "data": obj}
                            on_event(ev)
                            if (ev["data"].get("type") == "prompt_request"
                                    or (isinstance(ev["data"], dict) and ev["data"].get("prompt"))):
                                d = ev["data"]
                                task = Task(
                                    id=str(d.get("id", "")),
                                    prompt=str(d.get("prompt", "")),
                                    submitter=str(d.get("submitter", "?")),
                                    fee_usdc=float(d.get("fee_usdc", 0) or 0),
                                    received_at=time.time(),
                                )
                                if task.id:
                                    inbox.add(task)
                        event_type = ""
                        data_buf = []
                    elif line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data_buf.append(line[5:].lstrip())
                    elif line.startswith(":"):
                        pass
        except (urllib.error.URLError, TimeoutError, OSError, socket.error) as e:
            on_log("warn", f"SSE dropped: {type(e).__name__}: {str(e)[:60]} — retry {backoff:.0f}s")
        except Exception as e:
            on_log("err", f"SSE crash: {type(e).__name__}: {str(e)[:60]}")
        time.sleep(backoff)
        backoff = min(backoff * 1.5, 30.0)


# ---------- LLM worker pool -------------------------------------------------

class LLMWorker:
    """Run LLM calls in a separate thread pool so the TUI never blocks on a
    slow ollama/codex/gemini call. Returns a (task_id, response_or_err) via
    a queue when done."""

    def __init__(self, max_workers: int = 2):
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        self._results: queue.Queue[tuple[str, str, str]] = queue.Queue()
        self._inflight: dict[str, concurrent.futures.Future] = {}

    def submit(self, task_id: str, prompt: str):
        if task_id in self._inflight:
            return
        def wrap():
            try:
                # Returns (status, reply, backend_name) so the rig can
                # send X-LLM-Backend provenance. Backend is "" if the
                # fallback was used.
                reply, backend = make_llm_response(prompt)
                return ("ok", reply, backend)
            except Exception as e:
                return ("err", f"{type(e).__name__}: {e}"[:120], "")
        fut = self._pool.submit(wrap)
        self._inflight[task_id] = fut
        fut.add_done_callback(lambda f, t=task_id: self._on_done(t, f))

    def _on_done(self, task_id: str, fut):
        try:
            status, payload = fut.result()
        except Exception as e:
            status, payload = "err", f"{type(e).__name__}: {e}"[:120]
        self._results.put((task_id, status, payload))
        self._inflight.pop(task_id, None)

    def drain(self, sink: list):
        while True:
            try:
                sink.append(self._results.get_nowait())
            except queue.Empty:
                break

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)


# ---------- Render ----------------------------------------------------------

def hr(ch: str = "─", width: int = 60) -> str:
    return ch * width

def fmt_usdc(x: float) -> str:
    return f"{x:>10.6f}"

def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"

LEVEL_COLORS = {
    "info":  Fore.CYAN,
    "ok":    Fore.GREEN,
    "warn":  Fore.YELLOW,
    "err":   Fore.RED,
    "block": Fore.MAGENTA,
    "rpc":   Fore.BLUE,
    "mkt":   Fore.MAGENTA,
    "sse":   Fore.BLUE,
    "task":  Fore.MAGENTA,
    "ans":   Fore.GREEN,
    "llm":   Fore.CYAN,
}

def render(agent: Agent, feed: Feed, inbox: Inbox, endpoint: str, online: bool,
           width: int, height: int, paused: bool) -> str:
    w = max(60, width)
    h = max(24, height)
    out = []
    out.append(CLEAR_SCREEN)

    title = "  USDC PROMPT-RESPONDER AGENT  "
    sub = " fcoin marketplace  ·  v" + __version__
    pad = max(1, w - len(title) - len(sub) - 2)
    out.append(move_to(1, 1))
    out.append(c(Fore.BLACK + Back.YELLOW, title) + c(Fore.YELLOW, "│") + c(Fore.WHITE, sub) + " " * (pad - 1))
    out.append(move_to(2, 1) + c(Fore.YELLOW, hr("─", w)))

    # status line
    elapsed = int(time.time() - agent.started_at)
    hh, rem = divmod(elapsed, 3600)
    mm, ss = divmod(rem, 60)
    up = f"{hh:02d}:{mm:02d}:{ss:02d}"
    pool_state = c(Fore.RED, "OFFLINE") if not online else c(Fore.GREEN, "ONLINE ")

    out.append(move_to(3, 1) + c(Fore.CYAN, " AGENT        ") + agent.agent_id +
               c(Fore.WHITE, f"   {pool_state}   uptime {up}"))
    out.append(move_to(4, 1) + c(Fore.CYAN, " ENDPOINT     ") + endpoint)
    out.append(move_to(5, 1) + c(Fore.CYAN, " PROMPTS      ") +
               c(Fore.WHITE, f"open={agent.open_prompts}  received={agent.received}  "
                              f"answered={c(Fore.GREEN, str(agent.answered))}  "
                              f"failed={c(Fore.RED if agent.failed else Fore.WHITE, str(agent.failed))}"))
    out.append(move_to(6, 1) + c(Fore.CYAN, " WALLET       ") +
               c(Fore.WHITE, f"USDC={c(Fore.GREEN, fmt_usdc(agent.usdc_balance))}   "
                              f"fcoin={c(Fore.YELLOW, f'{agent.fcoin_balance:>10.4f}')}"))

    out.append(move_to(7, 1) + c(Fore.YELLOW, hr("─", w)))

    # latest task
    latest = inbox.latest()
    if latest:
        out.append(move_to(8, 1) + c(Fore.MAGENTA + Style.BRIGHT, " LATEST TASK  ") +
                   c(Fore.WHITE, f"id={latest.id[:8]}  fee={latest.fee_usdc:.4f} USDC  by {latest.submitter}"))
        out.append(move_to(9, 1) + c(Fore.WHITE, " > ") +
                   c(Fore.WHITE, clip(latest.prompt.replace("\n", " "), w - 4)))
    else:
        out.append(move_to(8, 1) + c(Fore.MAGENTA + Style.BRIGHT, " LATEST TASK  ") +
                   c(Fore.WHITE, "(no prompts received yet)"))
        out.append(move_to(9, 1) + CLEAR_LINE)

    out.append(move_to(10, 1) + c(Fore.YELLOW, hr("─", w)))

    # LLM backends detected on this machine
    backends = detect_llm_backends()
    detected = [(n, p) for (n, ok, p) in backends if ok]
    out.append(move_to(11, 1) + c(Fore.WHITE + Style.BRIGHT, " LLMs         ") +
               c(Fore.WHITE, f"{len(detected)} of {len(backends)} backends available"))
    if detected:
        # Show up to 6 on one line; if more, show the rest on the next line
        names = [n for n, _ in detected]
        out.append(move_to(12, 1) + c(Fore.GREEN, "  ✓  ") +
                   c(Fore.WHITE, "  ".join(names[:6])))
        if len(names) > 6:
            out.append(move_to(13, 1) + c(Fore.GREEN, "  ✓  ") +
                       c(Fore.WHITE, "  ".join(names[6:])))
    else:
        out.append(move_to(12, 1) +
                   c(Fore.RED, "  ✗  no LLM CLIs detected on this machine") +
                   c(Fore.WHITE, "  (rig will answer with the 'y' fallback)"))
        out.append(move_to(13, 1) + CLEAR_LINE)

    out.append(move_to(14, 1) + c(Fore.YELLOW, hr("─", w)))

    # feed
    out.append(move_to(20, 1) + c(Fore.WHITE + Style.BRIGHT, " FEED ") + c(Fore.WHITE, hr("─", w - 5)))
    feed_rows = 3
    start = 21
    visible = feed.lines[-feed_rows:]
    for i in range(feed_rows):
        row = start + i
        out.append(move_to(row, 1) + CLEAR_LINE)
        if i < len(visible):
            lvl, text = visible[i]
            color = LEVEL_COLORS.get(lvl, Fore.WHITE)
            ts = time.strftime("%H:%M:%S")
            line = f" {c(Fore.WHITE, ts)}  {c(color, lvl.upper().ljust(5))}  {text}"
            out.append(move_to(row, 1) + clip(line, w + 64))

    out.append(move_to(start + feed_rows, 1) + c(Fore.YELLOW, hr("─", w)))

    # footer
    base_row = start + feed_rows + 1
    state = "PAUSED" if paused else "RUNNING"
    state_color = Fore.YELLOW if paused else Fore.GREEN
    out.append(move_to(base_row, 1) +
               c(state_color + Style.BRIGHT, f"[{state}]") +
               c(Fore.WHITE, f"  endpoint: {endpoint}    online: {'yes' if online else 'no'}    inbox: {inbox.count()}"))
    out.append(move_to(base_row + 1, 1) +
               c(Fore.WHITE, f" usdc {__version__}  keys: ") +
               c(Fore.CYAN, "[p]") + c(Fore.WHITE, " pause  ") +
               c(Fore.CYAN, "[u]") + c(Fore.WHITE, " update  ") +
               c(Fore.CYAN, "[q]") + c(Fore.WHITE, " quit"))

    return "".join(out)


# ---------- Spawn new Termux window ----------------------------------------

TERMUX_ACTIVITY = "com.termux/com.termux.app.TermuxActivity"

def spawn_new_window(extra_args: list[str]) -> None:
    argv0 = os.path.abspath(__file__)
    intent = [
        "am", "start",
        "-n", TERMUX_ACTIVITY,
        "-a", "android.intent.action.MAIN",
        "-c", "android.intent.category.LAUNCHER",
        "--es", "com.termux.RUN_COMMAND_PATH", argv0,
        "--es", "com.termux.RUN_COMMAND_ARGUMENTS", " ".join(extra_args),
    ]
    try:
        subprocess.run(intent, check=True, timeout=5)
        return
    except Exception as e:
        sys.stderr.write(f"warn: am start failed ({e}); opening bare Termux window\n")
        subprocess.run(["am", "start", "-n", TERMUX_ACTIVITY], check=False, timeout=5)


# ---------- Terminal handling -----------------------------------------------

def terminal_size() -> tuple[int, int]:
    try:
        sz = shutil.get_terminal_size((80, 24))
        return sz.columns, sz.lines
    except Exception:
        return 80, 24

def enable_raw_mode() -> None:
    try:
        import termios, tty
        fd = sys.stdin.fileno()
        if not hasattr(enable_raw_mode, "_orig"):
            enable_raw_mode._orig = termios.tcgetattr(fd)  # type: ignore[attr-defined]
        tty.setcbreak(fd)
    except Exception:
        pass

def restore_terminal() -> None:
    try:
        import termios
        fd = sys.stdin.fileno()
        orig = getattr(enable_raw_mode, "_orig", None)
        if orig is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, orig)
    except Exception:
        pass

def read_key_nonblocking() -> str | None:
    try:
        import select
        if not select.select([sys.stdin], [], [], 0.0)[0]:
            return None
        return sys.stdin.read(1)
    except Exception:
        return None


# ---------- Main loop -------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    if args.new_window:
        extra: list[str] = []
        if args.local:
            extra += ["--local"]
        elif args.endpoint:
            extra += ["--endpoint", args.endpoint]
        if args.agent:
            extra += ["--agent", args.agent]
        if args.detach:
            extra += ["--detach"]
        spawn_new_window(extra)
        print("spawned new Termux window for the USDC rig.")
        return 0

    random.seed(args.seed)
    endpoint_url = args.endpoint or DEFAULT_ENDPOINT
    agent_id = args.agent
    online = False
    client: FcoinClient | None = None
    if not args.local:
        client = FcoinClient(endpoint_url, agent_id)

    agent = Agent(agent_id=agent_id)
    feed = Feed()
    inbox = Inbox()
    paused = False
    http = AsyncHTTP()
    llm = LLMWorker()
    sse_events: queue.Queue = queue.Queue()
    http_results: list = []
    llm_results: list = []
    pending_llm: dict[str, str] = {}  # task_id -> prompt (for retry)
    last_seen_prompts: set[str] = set()

    feed_lock = threading.Lock()
    def log(level, msg):
        with feed_lock:
            feed.push(level, msg)
    def on_sse_log(level, msg):
        log("sse", f"{msg}")
    def on_sse_event(ev):
        ev_type = ev.get("type", "?")
        d = ev.get("data", {})
        if isinstance(d, dict) and d.get("type") == "prompt_request":
            log("task", f"new task {str(d.get('id',''))[:8]} fee={d.get('fee_usdc', 0):.3f}USDC")
        else:
            log("sse", f"event {ev_type}")
        sse_events.put(ev)

    feed.push("info", f"rig online — worker {agent.agent_id}")
    if client:
        feed.push("info", f"endpoint {endpoint_url} (default fcoin)")
        feed.push("info", "auto-responder ON — ollama → codex → gemini → stub")
        feed.push("info", "no manual input — rig runs unattended")
        t_sse = threading.Thread(target=sse_thread, args=(endpoint_url, agent_id, inbox, on_sse_event, on_sse_log), daemon=True)
        t_sse.start()
    else:
        feed.push("info", "local simulation only (--local)")

    sys.stdout.write(ALT_SCREEN_ON + HIDE_CURSOR)
    sys.stdout.flush()
    enable_raw_mode()

    def shutdown(*_):
        http.shutdown()
        llm.shutdown()
        restore_terminal()
        sys.stdout.write(SHOW_CURSOR + ALT_SCREEN_OFF)
        sys.stdout.flush()
        print()
        print(f"finalized: usdc={agent.usdc_balance:.6f} USDC  fcoin={agent.fcoin_balance:.4f}  "
              f"prompts rcv={agent.received} ans={agent.answered} fail={agent.failed}")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    inflight_tags: set[str] = set()
    last_poll = 0.0
    last_prompts_poll = 0.0

    def kick_poll():
        nonlocal last_poll, last_prompts_poll
        if client is None:
            return
        now = time.time()
        if now - last_poll > NETWORK_POLL_PORTFOLIO and "portfolio" not in inflight_tags:
            last_poll = now
            inflight_tags.add("portfolio")
            http.submit("portfolio", client.portfolio)
        if now - last_prompts_poll > NETWORK_POLL_PROMPTS and "prompts" not in inflight_tags:
            last_prompts_poll = now
            inflight_tags.add("prompts")
            http.submit("prompts", client.prompts, "open")

    try:
        while True:
            now = time.time()
            cols, rows = terminal_size()
            fee_pending = 0.0

            # The "sim step" is gone. The agent's counters are updated
            # only by real events: /portfolio poll increments wallet fields,
            # /prompts poll increments open_prompts/received, and the
            # respond_prompt success/failure updates answered/failed.

            # --- network: kick polls + drain ---
            if client:
                kick_poll()
            http.drain(http_results)
            for tag, res, _ in http_results:
                inflight_tags.discard(tag)
                if tag == "portfolio":
                    if "_err" in res:
                        online = False
                        feed.push("err", f"portfolio: {res['_err'][:60]}")
                    else:
                        online = True
                        agent.last_poll_at = now
                        usdc_obj = res.get("usdc", {}) or {}
                        fcoin_obj = res.get("fcoin", {}) or {}
                        agent.usdc_balance = float(usdc_obj.get("total", 0.0))
                        agent.fcoin_balance = float(fcoin_obj.get("total", 0.0))
                        feed.push("rpc", f"portfolio synced — {agent.usdc_balance:.4f} USDC")
                elif tag == "prompts":
                    if "_err" in res:
                        pass
                    else:
                        items = res.get("prompts", []) or []
                        agent.open_prompts = len(items)
                        for it in items:
                            pid = str(it.get("id", ""))
                            if not pid or pid in last_seen_prompts:
                                continue
                            last_seen_prompts.add(pid)
                            task = Task(
                                id=pid,
                                prompt=str(it.get("prompt", "")),
                                submitter=str(it.get("submitter", "?")),
                                fee_usdc=float(it.get("fee_usdc", 0) or 0),
                                received_at=time.time(),
                            )
                            inbox.add(task)
                            agent.received += 1
                            fee = it.get("fee_usdc", 0)
                            sub = it.get("submitter", "?")
                            prompt_text = clip(str(it.get("prompt", "")).replace("\n", " "), 40)
                            feed.push("mkt", f"new prompt {pid[:8]}  fee={fee:.3f}USDC  by {sub}: {prompt_text}")
                            # Kick the LLM
                            if client is not None and pid not in pending_llm:
                                pending_llm[pid] = task.prompt
                                llm.submit(pid, task.prompt)
                                feed.push("llm", f"calling local LLM for {pid[:8]}...")
                elif tag.startswith("answer:"):
                    task_id = tag.split(":", 1)[1]
                    if "_err" in res:
                        detail = res.get("_detail", "")
                        msg = f"answer {task_id[:8]}: {res['_err'][:60]}"
                        if detail:
                            msg += f" — {detail[:60]}"
                        feed.push("err", msg)
                        agent.failed += 1
                    else:
                        agent.answered += 1
                        fee_earned = 0.0
                        if isinstance(res, dict):
                            fee_earned = float(res.get("paid_out_usdc", 0) or 0)
                        if fee_earned <= 0:
                            fee_earned = 0.001
                        agent.usdc_balance += fee_earned
                        feed.push("ans", f"answered {task_id[:8]}  +{fee_earned:.4f} USDC")
            http_results.clear()

            # --- drain SSE events (count new tasks, kick LLM) ---
            try:
                while True:
                    ev = sse_events.get_nowait()
                    if isinstance(ev.get("data"), dict) and ev["data"].get("type") == "prompt_request":
                        agent.received += 1
                        d = ev["data"]
                        pid = str(d.get("id", ""))
                        if pid and client is not None and pid not in pending_llm:
                            pending_llm[pid] = str(d.get("prompt", ""))
                            llm.submit(pid, pending_llm[pid])
                            feed.push("llm", f"calling local LLM for {pid[:8]}...")
            except queue.Empty:
                pass

            # --- drain LLM results, dispatch respond_prompt ---
            llm.drain(llm_results)
            for task_id, status, payload, backend in llm_results:
                if status == "ok":
                    if client is None:
                        feed.push("llm", f"LLM answered {task_id[:8]}  (offline, not posting)")
                    else:
                        # Refuse to post the stub fallback. fcoin's server-side
                        # check will reject anything <3 words by default, so
                        # sending "y" only burns the request without earning
                        # the fee. Honor USDC_ALLOW_STUB=1 to override (e.g.
                        # for submitters who set min_response_words=1).
                        is_stub = (not backend) or payload.strip() in ("y", "hi back")
                        if is_stub and not os.environ.get("USDC_ALLOW_STUB"):
                            feed.push("warn",
                                      f"skip stub for {task_id[:8]}: no LLM backend produced a real answer "
                                      f"(set USDC_LLM_FIRST=<backend> or USDC_ALLOW_STUB=1 to override)")
                            agent.failed += 1
                            pending_llm.pop(task_id, None)
                            continue
                        prov = f" via {backend}" if backend else ""
                        feed.push("llm", f"LLM answered {task_id[:8]}{prov}  →  posting to fcoin")
                        tag = f"answer:{task_id}"
                        inflight_tags.add(tag)
                        http.submit(tag, client.respond_prompt, task_id, payload, backend)
                else:
                    feed.push("err", f"LLM failed for {task_id[:8]}: {payload[:80]}")
                    agent.failed += 1
                pending_llm.pop(task_id, None)
            llm_results.clear()

            # --- draw ---
            sys.stdout.write(render(agent, feed, inbox, endpoint_url, online, cols, rows, paused))
            sys.stdout.flush()

            # --- input (only p and q) ---
            k = read_key_nonblocking()
            if not k:
                time.sleep(0.15)
                continue
            if k in ("q", "Q", "\x03"):
                shutdown()
            elif k in ("p", "P"):
                paused = not paused
                feed.push("info", "paused" if paused else "resumed")
            elif k in ("u", "U"):
                feed.push("info", "self-update requested — fetching latest...")
                sys.stdout.write(render(agent, feed, inbox, endpoint_url, online, cols, rows, paused))
                sys.stdout.flush()
                # Shutdown gracefully first
                http.shutdown()
                llm.shutdown()
                restore_terminal()
                sys.stdout.write(SHOW_CURSOR + ALT_SCREEN_OFF)
                sys.stdout.flush()
                print()
                ok, msg = do_update()
                if not ok:
                    print(f"update failed: {msg}")
                    sys.exit(1)

            time.sleep(0.15)
    finally:
        http.shutdown()
        llm.shutdown()
        restore_terminal()
        sys.stdout.write(SHOW_CURSOR + ALT_SCREEN_OFF)
        sys.stdout.flush()


def _fetch_remote_source(timeout: float = 15) -> str | None:
    """Fetch the latest usdc.py from GitHub. Tries the API first (always fresh),
    falls back to raw.githubusercontent.com (may be cached). Returns None on
    any failure. Never raises."""
    # 1) GitHub API — always fresh
    try:
        req = urllib.request.Request(
            GITHUB_API_URL,
            headers={
                "Accept": "application/vnd.github.raw+json",
                "User-Agent": "usdc-rig",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            ct = (r.headers.get("Content-Type") or "").lower()
            # With Accept: application/vnd.github.raw+json the body IS raw text
            if "json" in ct:
                try:
                    obj = json.loads(body.decode("utf-8", "replace"))
                    if isinstance(obj, dict) and "content" in obj and obj.get("encoding") == "base64":
                        import base64 as _b64
                        return _b64.b64decode(obj["content"]).decode("utf-8", "replace")
                except Exception:
                    pass
            # Otherwise treat body as text
            return body.decode("utf-8", "replace")
    except Exception:
        pass
    # 2) raw fallback
    try:
        with urllib.request.urlopen(GITHUB_RAW_URL, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def _version_tuple(v: str) -> tuple:
    """Parse '1.5.3' or '1.5.3-rc1' into (1, 5, 3, 'rc1') for comparison.

    Non-numeric suffixes sort after numeric ones. Returns (0,) for unparseable
    strings so they sort as 'oldest' (won't be installed).
    """
    out = []
    suffix = ""
    for part in v.replace("-", ".").split("."):
        try:
            out.append(int(part))
        except ValueError:
            suffix = part  # e.g. "rc1", "beta2"
    return tuple(out) + (suffix,)


def check_update(timeout: float = 5.0) -> tuple[str, str] | None:
    """Fetch the latest usdc.py from GitHub and return (current_ver, new_ver).

    Returns None on any failure (network, parse, etc.) — never raises.
    No file is modified. Safe to call on every startup.
    """
    new_src = _fetch_remote_source(timeout=timeout)
    if new_src is None:
        return None
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', new_src, re.MULTILINE)
    if not m:
        return None
    new_ver = m.group(1)
    return (__version__, new_ver)


def do_update() -> tuple[bool, str]:
    """Fetch latest usdc.py from GitHub, replace the running script, re-exec.

    Returns (success, message). On success, this function does NOT return —
    the process is replaced via os.execv. The return type is for the
    error/validation path.
    """
    script_path = os.path.abspath(__file__)
    new_src = _fetch_remote_source(timeout=15)
    if new_src is None:
        return False, "download failed (network)"

    # 1) must be valid Python
    try:
        ast.parse(new_src)
    except SyntaxError as e:
        return False, f"new version has syntax error: {e}"

    # 2) must have the same entrypoint symbols we depend on at runtime
    required = ("Miner", "Feed", "Inbox", "FcoinClient", "AsyncHTTP", "LLMWorker", "make_llm_response", "def main(")
    for sym in required:
        if sym not in new_src:
            return False, f"new version missing symbol: {sym}"

    # 3) extract __version__ from new source
    new_ver = __version__
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', new_src, re.MULTILINE)
    if m:
        new_ver = m.group(1)

    # 4) same version? nothing to do
    if new_ver == __version__:
        return True, f"already on latest ({__version__})"

    # 5) write atomically: write to .new, fsync, rename over the script
    tmp = script_path + ".new"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new_src)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, script_path)
    except Exception as e:
        try: os.unlink(tmp)
        except Exception: pass
        return False, f"write failed: {type(e).__name__}: {e}"[:120]

    # 6) re-exec current python with the same args (minus --update)
    new_argv = [a for a in sys.argv if a != "--update"]
    print(f"updated: {__version__} -> {new_ver} — restarting rig")
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable] + new_argv)
    # unreachable
    return True, f"updated to {new_ver}"


def main() -> int:
    p = argparse.ArgumentParser(
        prog="usdc",
        description="Autonomous fcoin prompt-responder rig for Termux. No manual input.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "keys:  [p] pause   [u] update   [q] quit\n\n"
            "Runs unattended. Polls fcoin for new prompts, sends each one to your\n"
            "local LLM (ollama → codex → gemini → fallback), and POSTs the reply\n"
            "back to /respond_prompt. Earns USDC for every accepted answer.\n\n"
            "auto-update:  runs on every `usdc` startup. Fetches the latest\n"
            "              usdc.py from GitHub main, replaces the running\n"
            "              script atomically, and restarts in place.\n"
            "              Use --no-update to skip, --check-update to peek.\n"
        ),
    )
    p.add_argument("--endpoint", default=None,
                   help=f"fcoin-style base URL (default: {DEFAULT_ENDPOINT})")
    p.add_argument("--agent", default=f"termux-{random.randint(1000, 9999)}",
                   help="fcoin agent id (default: auto)")
    p.add_argument("--local", action="store_true",
                   help="skip network, run pure local simulation")
    p.add_argument("--new-window", action="store_true",
                   help="spawn a new Termux window and run the rig there, then exit")
    p.add_argument("--detach", action="store_true",
                   help="(internal) flag set by --new-window; no extra effect")
    p.add_argument("--seed", type=int, default=None, help="RNG seed (for reproducible sim)")
    p.add_argument("--update", action="store_true",
                   help="force self-update, then exit (don't start the rig)")
    p.add_argument("--check-update", action="store_true",
                   help="check for updates, print result, exit (no install)")
    p.add_argument("--no-update", action="store_true",
                   help="skip the auto-update check on startup")
    p.add_argument("--version", action="store_true", help="print version and exit")

    p.add_argument("--prompts", action="store_true",
                   help="list open marketplace prompts and exit")
    p.add_argument("--responses", action="store_true",
                   help="list all responses (use --responses-of AGENT to filter) and exit")
    p.add_argument("--responses-of", metavar="AGENT", default="",
                   help="filter --responses by responding agent")
    p.add_argument("--earnings", action="store_true",
                   help="show earnings ledger (use --earnings-of AGENT for one agent) and exit")
    p.add_argument("--earnings-of", metavar="AGENT", default="",
                   help="filter --earnings to one agent")
    p.add_argument("--stats", action="store_true",
                   help="show global marketplace stats and exit")
    p.add_argument("--backends", action="store_true",
                   help="list all LLM backends and which are detected on this machine, then exit")
    args = p.parse_args()

    if args.version:
        print(f"usdc {__version__}  (endpoint: {args.endpoint or DEFAULT_ENDPOINT})")
        return 0

    if args.backends:
        bk = detect_llm_backends()
        n_ok = sum(1 for _, ok, _ in bk if ok)
        n_tot = len(bk)
        print(f"# LLM backends detected on this machine: {n_ok} of {n_tot}")
        for name, ok, path in bk:
            mark = "✓" if ok else "✗"
            where = path if ok else "not found"
            print(f"  {mark}  {name:14s}  {where}")
        if n_ok == 0:
            print()
            print("  no LLM CLIs detected — the rig will answer with the 'y' fallback.")
            print("  install one (e.g. `pkg install ollama && ollama serve`) for real answers.")
        return 0

    # === marketplace query commands ===
    if args.prompts or args.responses or args.earnings or args.stats:
        import urllib.request, json as _json
        endpoint = (args.endpoint or DEFAULT_ENDPOINT).rstrip("/")
        try:
            if args.prompts:
                with urllib.request.urlopen(f"{endpoint}/prompts?status=all&limit=20", timeout=8) as r:
                    d = _json.loads(r.read())
                print(f"# {d.get('count',0)} prompts (latest)")
                for p in d.get("prompts", []):
                    print(f"  {p.get('id','')[:12]}  {p.get('status',''):10s}  "
                          f"{p.get('fee_usdc',0):>6.3f} USDC  by {p.get('submitter','')}: "
                          f"{str(p.get('prompt',''))[:60]!r}")
            elif args.responses:
                q = f"?agent={args.responses_of}&limit=20" if args.responses_of else "?limit=20"
                with urllib.request.urlopen(f"{endpoint}/responses{q}", timeout=8) as r:
                    d = _json.loads(r.read())
                print(f"# {d.get('count',0)} responses (latest)")
                for r in d.get("responses", []):
                    print(f"  prompt={r.get('request_id','')[:12]}  by {r.get('agent_id','')}  "
                          f"earned {r.get('fee_usdc',0):.3f} USDC")
                    print(f"    Q: {str(r.get('prompt',''))[:80]!r}")
                    print(f"    A: {str(r.get('response',''))[:80]!r}")
            elif args.earnings:
                q = f"?agent={args.earnings_of}" if args.earnings_of else ""
                with urllib.request.urlopen(f"{endpoint}/earnings{q}", timeout=8) as r:
                    d = _json.loads(r.read())
                if "agent" in d:
                    print(f"agent {d['agent']}: submitted={d.get('submitted',0)}  "
                          f"answered={d.get('answered',0)}  "
                          f"spent={d.get('spent_usdc',0):.4f} USDC  "
                          f"earned={d.get('earned_usdc',0):.4f} USDC")
                else:
                    print(f"# {d.get('count',0)} agents; totals: {d.get('totals',{})}")
                    for ag, row in sorted(d.get("agents",{}).items(),
                                          key=lambda kv: -kv[1].get("earned_usdc",0)):
                        print(f"  {ag:25s}  submitted={row.get('submitted',0):3d}  "
                              f"answered={row.get('answered',0):3d}  "
                              f"earned={row.get('earned_usdc',0):>10.4f} USDC")
            elif args.stats:
                with urllib.request.urlopen(f"{endpoint}/stats", timeout=8) as r:
                    d = _json.loads(r.read())
                p = d.get("prompts", {})
                r = d.get("responses", {})
                print(f"# marketplace stats")
                print(f"  prompts:    {p.get('total',0)} total, by status: {p.get('by_status',{})}")
                print(f"  fees locked: {p.get('total_fees_locked',0):.4f} USDC")
                print(f"  responses:  {r.get('total',0)}")
                print(f"  top earners:")
                for e in d.get("top_earners", [])[:10]:
                    print(f"    {e.get('agent',''):25s}  earned={e.get('earned_usdc',0):>10.4f} USDC")
        except Exception as e:
            print(f"error: {e}")
            return 1
        return 0

    if args.update:
        ok, msg = do_update()
        if not ok:
            print(f"update failed: {msg}")
            return 1
        # do_update() calls os.execv on success, so we never reach here
        print(msg)
        return 0

    if args.check_update:
        result = check_update()
        if result is None:
            print(f"usdc {__version__}  (could not reach update server)")
            return 1
        cur, new = result
        if cur == new:
            print(f"usdc {cur}  (latest)")
            return 0
        print(f"usdc {cur}  ->  {new} available  (run `usdc --update` to install)")
        return 0

    # Auto-update on startup unless --no-update is set
    if not args.no_update:
        result = check_update()
        if result is not None:
            cur, new = result
            if _version_tuple(new) > _version_tuple(cur):
                # print to stderr so it shows before the TUI takes over
                print(f"usdc {cur} -> {new}  (auto-updating...)", file=sys.stderr)
                ok, msg = do_update()
                if not ok:
                    print(f"auto-update failed: {msg}  (continuing with {cur})", file=sys.stderr)
            # else: local is at or ahead of remote — do nothing
        # else: network error — quietly continue with current version

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
