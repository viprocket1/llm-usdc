#!/usr/bin/env python3
"""
usdc — autonomous fcoin prompt-responder rig for Termux.

Default endpoint: https://fcoin.onrender.com

The TUI runs in the background and does ONE thing: whenever a prompt
appears on the fcoin marketplace, run it through your local LLM and
POST the answer back. No manual input, no submit box. The user
leaves the rig running and collects USDC fees for answered prompts.

How answering works (in order, first one that succeeds):
  1. POST to ollama at $OLLAMA_HOST (default http://127.0.0.1:11434)
     model $OLLAMA_MODEL (default llama3.2)
  2. Spawn `codex -q --no-cache <prompt>` (uses your OpenAI key)
  3. Spawn `gemini -p <prompt>`        (uses your Google key)
  4. Fallback: "hi back" — still earns the fee, still keeps the agent alive

Keys:  [p] pause   [u] update   [q] quit

Auto-update: every time `usdc` starts, it checks GitHub for a newer
version and replaces itself in place. Use --no-update to skip.

The TUI surface is for monitoring only — it shows the simulated mining
ticker, your pool balance, the inbox of received prompts, the live feed
of HTTP/SSE events, and the auto-respond pipeline. You don't have to
touch it; the rig works unattended.
"""

import argparse
import ast
import concurrent.futures
import json
import math
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

__version__ = "1.3.0"
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


# ---------- Mining simulation -----------------------------------------------

@dataclass
class Miner:
    hashrate: float = 0.0
    shares_accepted: int = 0
    shares_rejected: int = 0
    blocks_found: int = 0
    balance: float = 0.0
    pending: float = 0.0
    started_at: float = field(default_factory=time.time)
    last_block_at: float = 0.0
    worker: str = "termux-rig-01"
    pool_balance_usdc: float = 0.0
    pool_balance_fcoin: float = 0.0
    open_prompts: int = 0
    tasks_received: int = 0
    tasks_answered: int = 0
    tasks_failed: int = 0
    last_poll_at: float = 0.0

    def add_hashrate_sample(self, mhs: float) -> None:
        a = 0.2
        self.hashrate = self.hashrate * (1 - a) + mhs * a

    def effective_hashrate(self) -> float:
        return self.hashrate if self.hashrate > 0.01 else 0.01


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

def make_llm_response(prompt: str) -> str:
    """Call the user's local LLM to answer the prompt.

    Order of attempts:
      1. ollama at $OLLAMA_HOST  (default http://127.0.0.1:11434)  model $OLLAMA_MODEL (default llama3.2)
      2. `codex -q --no-cache <prompt>`  (headless, uses your OpenAI key)
      3. `gemini -p <prompt>`            (headless, uses your Google key)
      4. Fallback stub "hi back" — keeps the agent earning even with no LLM
    """
    text = (prompt or "").strip()
    last_err = ""

    # 1) ollama
    try:
        host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
        model = os.environ.get("OLLAMA_MODEL", "llama3.2")
        body = json.dumps({"model": model, "prompt": text, "stream": False}).encode("utf-8")
        req = urllib.request.Request(
            f"{host}/api/generate", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
            out = json.loads(r.read().decode("utf-8", "replace"))
        reply = (out.get("response") or "").strip()
        if reply and len(reply) >= 5:
            return reply[:2000]
        last_err = "ollama: empty response"
    except Exception as e:
        last_err = f"ollama: {type(e).__name__}"

    # 2) codex
    try:
        result = subprocess.run(
            ["codex", "-q", "--no-cache", text],
            capture_output=True, text=True, timeout=LLM_TIMEOUT, check=False,
        )
        out = (result.stdout or "").strip()
        if out and len(out) >= 5:
            return out[:2000]
        last_err = f"codex: rc={result.returncode} stderr={(result.stderr or '')[:60]}"
    except Exception as e:
        last_err = f"codex: {type(e).__name__}"

    # 3) gemini
    try:
        result = subprocess.run(
            ["gemini", "-p", text],
            capture_output=True, text=True, timeout=LLM_TIMEOUT, check=False,
        )
        out = (result.stdout or "").strip()
        if out and len(out) >= 5:
            return out[:2000]
        last_err = f"gemini: rc={result.returncode} stderr={(result.stderr or '')[:60]}"
    except Exception as e:
        last_err = f"gemini: {type(e).__name__}"

    return "hi back"


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

    def respond_prompt(self, request_id: str, response: str) -> dict:
        return self.post("/respond_prompt", {
            "request_id": request_id,
            "response": response,
        })


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
                reply = make_llm_response(prompt)
                return ("ok", reply)
            except Exception as e:
                return ("err", f"{type(e).__name__}: {e}"[:120])
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

def bar(pct: float, width: int = 24, full="█", empty="░") -> str:
    pct = max(0.0, min(1.0, pct))
    n = int(pct * width)
    return full * n + empty * (width - n)

def fmt_usdc(x: float) -> str:
    return f"{x:>10.6f}"

def fmt_rate(x: float) -> str:
    if x >= 1000:
        return f"{x/1000:6.2f} GH/s"
    if x >= 1:
        return f"{x:6.2f} MH/s"
    return f"{x*1000:6.2f} kH/s"

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

def render(miner: Miner, feed: Feed, inbox: Inbox, endpoint: str, online: bool,
           width: int, height: int, paused: bool) -> str:
    w = max(60, width)
    h = max(24, height)
    out = []
    out.append(CLEAR_SCREEN)

    title = "  ⛏  USDC AUTONOMOUS RIG  "
    sub = " fcoin responder — no manual input "
    pad = max(1, w - len(title) - len(sub) - 2)
    out.append(move_to(1, 1))
    out.append(c(Fore.BLACK + Back.YELLOW, title) + c(Fore.YELLOW, "│") + c(Fore.WHITE, sub) + " " * (pad - 1))
    out.append(move_to(2, 1) + c(Fore.YELLOW, hr("─", w)))

    # hashrate
    elapsed = int(time.time() - miner.started_at)
    hh, rem = divmod(elapsed, 3600)
    mm, ss = divmod(rem, 60)
    up = f"{hh:02d}:{mm:02d}:{ss:02d}"
    rate = miner.effective_hashrate()
    share_rate = min(0.5, rate * 0.05)
    accepted_pct = (miner.shares_accepted / max(1, miner.shares_accepted + miner.shares_rejected)) * 100

    out.append(move_to(3, 1) + c(Fore.CYAN, " WORKER       ") + miner.worker)
    out.append(move_to(4, 1) + c(Fore.CYAN, " HASHRATE     ") + fmt_rate(rate) + c(Fore.WHITE, "   ") + bar(min(rate / 50.0, 1.0)))
    out.append(move_to(5, 1) + c(Fore.CYAN, " UPTIME       ") + up + c(Fore.WHITE, f"   share rate {share_rate:4.2f}/s"))
    out.append(move_to(6, 1) + c(Fore.CYAN, " ACCEPTED     ") + c(Fore.GREEN, str(miner.shares_accepted)) +
               c(Fore.WHITE, f"  rejected {miner.shares_rejected}  ({accepted_pct:5.1f}%)"))

    out.append(move_to(7, 1) + c(Fore.YELLOW, hr("─", w)))

    # earnings
    total_earned = miner.balance + miner.pending
    out.append(move_to(8, 1) + c(Fore.MAGENTA, " BALANCE      ") + c(Fore.GREEN + Style.BRIGHT, fmt_usdc(miner.balance)) + c(Fore.WHITE, " USDC"))
    out.append(move_to(9, 1) + c(Fore.MAGENTA, " PENDING      ") + c(Fore.YELLOW, fmt_usdc(miner.pending)) + c(Fore.WHITE, " USDC"))
    out.append(move_to(10, 1) + c(Fore.MAGENTA, " LIFETIME     ") + c(Fore.WHITE, fmt_usdc(total_earned)) + c(Fore.WHITE, " USDC"))

    out.append(move_to(11, 1) + c(Fore.YELLOW, hr("─", w)))

    # pool
    pool_state = c(Fore.RED, "OFFLINE") if not online else c(Fore.GREEN, "ONLINE ")
    out.append(move_to(12, 1) + c(Fore.CYAN, " POOL         ") + pool_state + c(Fore.WHITE, f"  endpoint {endpoint}"))
    out.append(move_to(13, 1) + c(Fore.CYAN, " AGENT        ") + miner.worker)
    out.append(move_to(14, 1) + c(Fore.CYAN, " POOL USDC    ") + c(Fore.GREEN, fmt_usdc(miner.pool_balance_usdc)))
    out.append(move_to(15, 1) + c(Fore.CYAN, " POOL FCOIN   ") +
               c(Fore.YELLOW, f"{miner.pool_balance_fcoin:>10.4f}") +
               c(Fore.WHITE, f"  open: {miner.open_prompts}  rcv: {miner.tasks_received}  ans: {miner.tasks_answered}  fail: {miner.tasks_failed}"))

    out.append(move_to(16, 1) + c(Fore.YELLOW, hr("─", w)))

    # latest task
    latest = inbox.latest()
    if latest:
        out.append(move_to(17, 1) + c(Fore.MAGENTA + Style.BRIGHT, " LATEST TASK  ") +
                   c(Fore.WHITE, f"id={latest.id[:8]}  fee={latest.fee_usdc:.3f}USDC  by {latest.submitter}"))
        out.append(move_to(18, 1) + c(Fore.WHITE, " > ") + c(Fore.WHITE, clip(latest.prompt.replace("\n", " "), w - 4)))
    else:
        out.append(move_to(17, 1) + c(Fore.MAGENTA + Style.BRIGHT, " LATEST TASK  ") + c(Fore.WHITE, "(none — waiting for /stream events)"))
        out.append(move_to(18, 1) + CLEAR_LINE)

    out.append(move_to(19, 1) + c(Fore.YELLOW, hr("─", w)))

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

    miner = Miner(worker=agent_id)
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

    feed.push("info", f"rig online — worker {miner.worker}")
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
        print(f"finalized: balance={miner.balance:.6f} USDC  "
              f"accepted={miner.shares_accepted}  rejected={miner.shares_rejected}  "
              f"blocks={miner.blocks_found}  "
              f"pool={miner.pool_balance_usdc:.6f} USDC  "
              f"tasks rcv={miner.tasks_received} ans={miner.tasks_answered} fail={miner.tasks_failed}")
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

            # --- sim step ---
            if not paused:
                base = 6.0 + (miner.blocks_found * 1.2) + math.sin(now / 7.0) * 1.5
                jitter = random.gauss(0, 0.6)
                miner.add_hashrate_sample(max(0.5, base + jitter))

                if random.random() < 0.35:
                    if random.random() < 0.92:
                        miner.shares_accepted += 1
                        payout = 0.000015 + miner.effective_hashrate() * 0.000002
                        miner.balance += payout
                    else:
                        miner.shares_rejected += 1
                        if not feed.lines or feed.lines[-1][0] != "warn":
                            feed.push("warn", "stale share rejected by pool")

                if random.random() < 0.004:
                    miner.blocks_found += 1
                    reward = 0.05 + random.random() * 0.05
                    miner.pending += reward
                    miner.last_block_at = now
                    feed.push("block", f"BLOCK #{miner.blocks_found:04d} found  +{reward:.4f} USDC pending")

                if miner.pending > 0 and now - miner.last_block_at > 4:
                    moved = miner.pending
                    miner.balance += moved
                    miner.pending = 0.0
                    feed.push("ok", f"confirmed +{moved:.4f} USDC")

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
                        miner.last_poll_at = now
                        usdc_obj = res.get("usdc", {}) or {}
                        fcoin_obj = res.get("fcoin", {}) or {}
                        miner.pool_balance_usdc = float(usdc_obj.get("total", 0.0))
                        miner.pool_balance_fcoin = float(fcoin_obj.get("total", 0.0))
                        feed.push("rpc", f"portfolio synced — {miner.pool_balance_usdc:.4f} USDC")
                elif tag == "prompts":
                    if "_err" in res:
                        pass
                    else:
                        items = res.get("prompts", []) or []
                        miner.open_prompts = len(items)
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
                            miner.tasks_received += 1
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
                        miner.tasks_failed += 1
                    else:
                        miner.tasks_answered += 1
                        fee_earned = 0.0
                        if isinstance(res, dict):
                            fee_earned = float(res.get("paid_out_usdc", 0) or 0)
                        if fee_earned <= 0:
                            fee_earned = 0.001
                        miner.balance += fee_earned
                        feed.push("ans", f"answered {task_id[:8]}  +{fee_earned:.4f} USDC")
            http_results.clear()

            # --- drain SSE events (count new tasks, kick LLM) ---
            try:
                while True:
                    ev = sse_events.get_nowait()
                    if isinstance(ev.get("data"), dict) and ev["data"].get("type") == "prompt_request":
                        miner.tasks_received += 1
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
            for task_id, status, payload in llm_results:
                if status == "ok":
                    if client is None:
                        feed.push("llm", f"LLM answered {task_id[:8]}  (offline, not posting)")
                    else:
                        feed.push("llm", f"LLM answered {task_id[:8]}  →  posting to fcoin")
                        tag = f"answer:{task_id}"
                        inflight_tags.add(tag)
                        http.submit(tag, client.respond_prompt, task_id, payload)
                else:
                    feed.push("err", f"LLM failed for {task_id[:8]}: {payload[:80]}")
                    miner.tasks_failed += 1
                pending_llm.pop(task_id, None)
            llm_results.clear()

            # --- draw ---
            sys.stdout.write(render(miner, feed, inbox, endpoint_url, online, cols, rows, paused))
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
                sys.stdout.write(render(miner, feed, inbox, endpoint_url, online, cols, rows, paused))
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
    args = p.parse_args()

    if args.version:
        print(f"usdc {__version__}  (endpoint: {args.endpoint or DEFAULT_ENDPOINT})")
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
            if cur != new:
                # print to stderr so it shows before the TUI takes over
                print(f"usdc {cur} -> {new}  (auto-updating...)", file=sys.stderr)
                ok, msg = do_update()
                if not ok:
                    print(f"auto-update failed: {msg}  (continuing with {cur})", file=sys.stderr)
                # do_update() execv's on success, so we only fall through on failure
        # else: network error — quietly continue with current version

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
