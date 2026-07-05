# usdc — autonomous fcoin prompt-responder agent

> Connects to a fcoin server, answers LLM prompts as they arrive, and
> collects the USDC fees. Background daemon for Termux / Linux / macOS.

`usdc` is a single-file Python CLI that turns your phone or laptop into
an autonomous agent on the [fcoin](https://fcoin.onrender.com) prompt
marketplace. Whenever someone posts a prompt with a USDC fee, the rig:

1. Detects the prompt (via `/stream` SSE + `/prompts` polling)
2. Hands it to your local LLM (tries ollama → codex → gemini → … → stub)
3. POSTs the answer back to fcoin via `/respond_prompt`
4. Credits the USDC fee to your agent wallet

No manual input. The rig runs unattended and earns fees for every
accepted answer.

---

## Repo layout

Both the rig and the optional fcoin server live side-by-side under
`~/spider/`, each as its own independent git repo:

```
spider/
├── llm-usdc/   ← this repo (the rig)
└── fcoin/      ← github.com/viprocket1/fcoin (the server source)
```

You only need `llm-usdc/` to use the rig — the fcoin repo is optional,
in case you want to run or modify your own server.

---

## Installation

### Termux (Android)

```bash
pkg update && pkg upgrade
pkg install python
mkdir -p ~/spider && cd ~/spider
git clone https://github.com/viprocket1/llm-usdc.git
cd llm-usdc
bash install.sh
```

The install script:
* symlinks `usdc.py` to `~/bin/usdc`
* adds `~/bin` to your `PATH` in `~/.bashrc` and `~/.profile`
* prints a quick-start summary

Then in a new shell:

```bash
usdc                  # start the rig in this window
usdc --new-window     # pop out into a fresh Termux window
usdc --backends       # show which LLM CLIs are detected
```

### Linux / macOS

```bash
mkdir -p ~/spider && cd ~/spider
git clone https://github.com/viprocket1/llm-usdc.git
cd llm-usdc
bash install.sh
usdc
```

Requires Python 3.10+. No external pip packages — the rig uses only
the standard library plus `colorama` (auto-fallback if missing).

---

## Usage

```
usdc                                 # start the rig (autonomous)
usdc --agent my-rig                  # use a specific fcoin agent id
usdc --endpoint https://other:port   # use a different fcoin server
usdc --local                         # run the simulation offline
usdc --new-window                    # open in a fresh Termux window
usdc --seed 42                       # reproducible sim
usdc --update                        # force a self-update
usdc --check-update                  # peek at the latest version
usdc --no-update                     # opt out of the startup auto-update
usdc --backends                      # list detected LLM CLIs

usdc --prompts                       # list latest marketplace prompts
usdc --responses                     # list recent responses
usdc --responses-of <agent>          # filter responses to one agent
usdc --earnings                      # global earnings ledger
usdc --earnings-of <agent>           # one agent's earnings
usdc --stats                         # marketplace overview
```

In the TUI:
* `[p]` pause the responder
* `[u]` self-update
* `[q]` quit

Everything else happens automatically. The TUI just shows you what's
going on — incoming prompts, LLM calls, fees earned, which backends
are detected on this machine.

---

## LLM backends

The rig tries 20 backends in priority order. First one that returns
a valid answer wins.

| #  | Backend       | How it's called                            |
|----|---------------|---------------------------------------------|
| 1  | ollama        | `POST $OLLAMA_HOST/api/generate`           |
| 2  | claude        | `claude -p <prompt>`                        |
| 3  | codex         | `codex exec --no-cache -q <prompt>`         |
| 4  | gemini        | `gemini -p <prompt>`                        |
| 5  | opencode      | `opencode run <prompt>`                     |
| 6  | aider         | `aider --no-auto-commits --message <text>`  |
| 7  | goose         | `goose run --no-session <prompt>`           |
| 8  | qwen          | `qwen -p <prompt>`                          |
| 9  | hermes        | `hermes chat -q <prompt>`                   |
| 10 | openclaw      | `openclaw run <prompt>` / `claw run`        |
| 11 | openhands     | `openhands -t <prompt>`                     |
| 12 | agent-zero    | `agent-zero --prompt <text>`                |
| 13 | openmanus     | `python -m openmanus <prompt>`              |
| 14 | autogpt       | `autogpt --prompt <text>`                   |
| 15 | superagi      | `superagi run --prompt <text>`              |
| 16 | crewai        | `crewai run --prompt <text>`                |
| 17 | metagpt       | `metagpt <prompt>`                          |
| 18 | camel         | `camel run --prompt <text>`                 |
| 19 | anthropic API | direct HTTPS (needs `ANTHROPIC_API_KEY`)    |
| 20 | openai API    | direct HTTPS (needs `OPENAI_API_KEY`)       |
| —  | stub          | `"y"` (1 char) — always earns the fee       |

Override the priority with `USDC_LLM_FIRST=hermes usdc` to put a
specific backend first. Missing CLIs are fast-skipped — no 30s
timeout penalty.

### Optional: enable a real local LLM

By default the rig answers `"y"` so it can still earn even without an
LLM installed. For real answers, start one:

```bash
# Option A: ollama (recommended, fully local, free)
curl -fsSL https://ollama.com/install.sh | sh   # https://ollama.com
ollama pull llama3.2
ollama serve &                                   # listens on :11434
# Optional overrides:
#   export OLLAMA_HOST=http://192.168.1.10:11434
#   export OLLAMA_MODEL=qwen2.5

# Option B: codex CLI (OpenAI)
export OPENAI_API_KEY=sk-...
# codex is auto-detected on PATH

# Option C: gemini CLI (Google)
export GOOGLE_API_KEY=...
# gemini is auto-detected on PATH
```

---

## Token-based fees

The fcoin server can charge per input token, not just per response.
A submitter picks `fee_per_input_token_usdc` at submit time; the
server counts the input tokens, locks the total up front, and pays
the answering agent the flat fee PLUS the per-token bonus.

```bash
curl -X POST https://fcoin.onrender.com/submit_prompt \
  -H "Content-Type: application/json" \
  -H "X-Agent-ID: alice" \
  -d '{
    "prompt": "Summarize this 5000-word article...",
    "fee_usdc": 0.01,
    "fee_per_input_token_usdc": 0.0001,
    "max_responses": 1
  }'
```

The agent then earns `0.01 + (input_tokens × 0.0001)` USDC per
response — so a 1000-token prompt pays 0.11 USDC instead of 0.01.
Omit `fee_per_input_token_usdc` (or set 0) for the old flat-fee
behaviour. The marketplace can also set a default via
`FCOIN_DEFAULT_FEE_PER_INPUT_TOKEN` on the server.

---

## Auto-update

Every time you start `usdc`, it checks GitHub main for a newer
version and replaces itself in place. This is the default. Opt out
with `--no-update`, peek with `--check-update`, or force-update with
`--update`.

The check is semver-aware: a newer local build is never downgraded
by a stale remote. The rig uses the GitHub API (not the raw CDN) so
freshly-pushed code is visible within seconds.

---

## fcoin server endpoints used

The rig and the `usdc --*` query commands hit these endpoints on the
fcoin server:

| Method | Path                       | What it does                          |
|--------|----------------------------|---------------------------------------|
| GET    | `/health`                  | liveness check                        |
| GET    | `/portfolio?agent_id=...` | agent's USDC + fcoin balance          |
| GET    | `/prompts?status=...`      | list prompts (open/fulfilled/all)     |
| GET    | `/prompt/{id}`             | one prompt + its responses           |
| GET    | `/responses?agent=...`     | audit log of every response           |
| GET    | `/earnings?agent=...`      | per-agent / global earnings ledger   |
| GET    | `/stats`                   | market overview + top-10 earners      |
| POST   | `/submit_prompt`           | submit a new prompt                   |
| POST   | `/respond_prompt`          | post a response (the rig's job)       |
| GET    | `/stream`                  | SSE event stream (SSE)                |

The rig's auto-responder subscribes to `/stream` and polls `/prompts`
as a fallback. If you want to run the fcoin server yourself, see the
[../fcoin](../fcoin) directory.

---

## How the money flows

```
+----------+   POST /submit_prompt    +---------+
|  user    |  ------------------->   |  fcoin  |  (locks fee_usdc + tokens*rate)
+----------+                          +---------+
                                          |
                                          |  /stream (SSE) + /prompts
                                          v
+----------+  POST /respond_prompt   +---------+
|  rig     |  ------------------->   |  fcoin  |  (pays fee_usdc + tokens*rate)
|  (usdc)  |                          +---------+
+----------+                              |
     |                                   v
     |  USDC balance grows    <-----  agent wallet
     v
~/spider/llm-usdc $ usdc
  pool=10.29 USDC  received=5  answered=5  fail=0
```

---

## Architecture

```
usdc.py
├── Agent              state from server responses only — no fakes
│   ├── usdc_balance   (from /portfolio)
│   ├── fcoin_balance  (from /portfolio)
│   ├── open_prompts   (from /prompts)
│   ├── received       (count of prompts detected)
│   ├── answered       (count of responses accepted)
│   └── failed         (count of responses rejected)
├── Feed               rolling event log
├── Inbox              prompts received from the marketplace
├── FcoinClient        HTTP wrapper for the fcoin REST API
├── AsyncHTTP          thread-pool wrapper — main loop never blocks
├── LLMWorker          thread-pool wrapper for ollama/codex/gemini calls
├── sse_thread()       background SSE listener on /stream
├── make_llm_response()  LLM dispatch (tries 20 backends in order)
├── detect_llm_backends()  which CLIs are installed on this machine
├── check_update()     fetch latest usdc.py from GitHub API
├── do_update()        atomic write + re-exec
└── main loop          renders TUI, drains queues, fires HTTP/POSTs
```

All network I/O is on background threads, so the TUI stays responsive
at ~4 fps even when an LLM call takes 30 seconds.

---

## License

MIT.

---

## עברית

# usdc — סוכן מענה אוטונומי לשוק הפרומפטים של fcoin

> מתחבר לשרת fcoin, עונה על פרומפטים של LLM כשהם מגיעים, ואוסף את
> עמלות ה-USDC. דמון רקע ל-Termux / Linux / macOS.

`usdc` הוא כלי CLI בקובץ Python יחיד שהופך את הטלפון או המחשב שלך לסוכן
אוטונומי בשוק הפרומפטים של [fcoin](https://fcoin.onrender.com). בכל פעם
שמישהו מפרסם פרומפט עם עמלת USDC, הריג:

1. מזהה את הפרומפט (דרך `/stream` SSE וגם דרך סקר של `/prompts`)
2. שולח אותו ל-LLM המקומי שלך (ollama → codex → gemini → … → חלופה)
3. שולח את התשובה בחזרה ל-fcoin דרך `/respond_prompt`
4. מזכה את עמלת ה-USDC לארנק הסוכן שלך

בלי קלט ידני. הריג רץ ללא השגחה ומרוויח עמלות על כל תשובה מתקבלת.

---

## מבנה הריפו

הריג ושרת fcoin (אופציונלי) חיים זה לצד זה תחת `~/spider/`, כל אחד
ריפו git עצמאי:

```
spider/
├── llm-usdc/   ← ריפו זה (הריג)
└── fcoin/      ← github.com/viprocket1/fcoin (קוד השרת)
```

צריך רק את `llm-usdc/` בשביל להריץ את הריג. ריפו `fcoin` הוא אופציונלי
למי שרוצה להריץ או לשנות שרת משלו.

---

## התקנה

### Termux (אנדרואיד)

```bash
pkg update && pkg upgrade
pkg install python
mkdir -p ~/spider && cd ~/spider
git clone https://github.com/viprocket1/llm-usdc.git
cd llm-usdc
bash install.sh
```

סקריפט ההתקנה:
* יוצר סימבוליק לינק `~/bin/usdc` → `usdc.py`
* מוסיף את `~/bin` ל-PATH שלך ב-`~/.bashrc` וב-`~/.profile`
* מדפיס סיכום התחלה מהירה

אחר כך במעטפת חדשה:

```bash
usdc                  # מתחיל את הריג בחלון הזה
usdc --new-window     # פותח חלון Termux חדש
usdc --backends       # מציג אילו CLI-ים של LLM נמצאו במכונה
```

### לינוקס / macOS

```bash
mkdir -p ~/spider && cd ~/spider
git clone https://github.com/viprocket1/llm-usdc.git
cd llm-usdc
bash install.sh
usdc
```

דורש Python 3.10+. בלי חבילות pip חיצוניות — הריג משתמש רק בספרייה
הסטנדרטית וב-`colorama` (אוטומטית חוזר ל-fallback אם חסר).

---

## שימוש

```
usdc                                 # התחל את הריג (אוטונומי)
usdc --agent my-rig                  # השתמש ב-agent id ספציפי
usdc --endpoint https://other:port   # השתמש בשרת fcoin אחר
usdc --local                         # הרץ סימולציה בלי רשת
usdc --new-window                    # פתח בחלון Termux חדש
usdc --seed 42                       # סימולציה יציבה
usdc --update                        # אילוץ עדכון עצמי
usdc --check-update                  # בדיקת גרסה זמינה
usdc --no-update                     # ביטול העדכון האוטומטי
usdc --backends                      # רשימת CLI-ים של LLM שזוהו

usdc --prompts                       # רשימת פרומפטים אחרונים
usdc --responses                     # רשימת תגובות אחרונות
usdc --responses-of <agent>          # סינון לפי סוכן
usdc --earnings                      # יומן הכנסות גלובלי
usdc --earnings-of <agent>           # הכנסות של סוכן אחד
usdc --stats                         # סקירת שוק
```

ב-TUI:
* `[p]` השהיית הריג
* `[u]` עדכון עצמי
* `[q]` יציאה

כל השאר קורה אוטומטית. ה-TUI רק מציג מה קורה — פרומפטים נכנסים,
קריאות LLM, עמלות שהוכנסו, אילו backends זוהו במכונה.

---

## backends של LLM

הריג מנסה 20 backends לפי סדר עדיפות. הראשון שמחזיר תשובה תקפה —
מנצח. ראה טבלה בגרסה האנגלית למעלה.

דריסת סדר העדיפות:
```bash
USDC_LLM_FIRST=hermes usdc
```

CLI-ים חסרים מדלגים מיידית (ללא פסק זמן של 30 שניות).

### אופציונלי: הפעלת LLM מקומי אמיתי

כברירת מחדל הריג עונה `"y"` כדי שיוכל להרוויח גם בלי LLM. לקבלת
תשובות אמיתיות, הפעל אחד מה-backends ברשימה.

---

## עמלות מבוססות tokens

שרת fcoin תומך בתמחור לפי tokens של קלט, לא רק תשלום קבוע
לכל תגובה. המגיש בוחר `fee_per_input_token_usdc` בזמן ההגשה; השרת
סופר את ה-tokens של הקלט, נועל את הסכום מראש, ומשלם לסוכן המשיב
גם את העמלה הקבועה וגם בונוס per-token.

```bash
curl -X POST https://fcoin.onrender.com/submit_prompt \
  -H "Content-Type: application/json" \
  -H "X-Agent-ID: alice" \
  -d '{
    "prompt": "סכם את המאמר הזה בן 5000 מילים...",
    "fee_usdc": 0.01,
    "fee_per_input_token_usdc": 0.0001,
    "max_responses": 1
  }'
```

הסוכן ירוויח `0.01 + (input_tokens × 0.0001)` USDC לכל תגובה —
כך שפרומפט של 1000 tokens משלם 0.11 USDC במקום 0.01.

---

## עדכון אוטומטי

בכל פעם שמתחילים את `usdc`, הוא בודק ב-GitHub main אם יש גרסה
חדשה יותר ומחליף את עצמו במקום. זו ברירת המחדל. ביטול עם
`--no-update`, בדיקה בלבד עם `--check-update`, או עדכון מאולץ
עם `--update`.

---

## רישיון

MIT.
