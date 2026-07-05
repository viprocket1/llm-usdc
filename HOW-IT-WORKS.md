# How usdc mints USDC using LLMs

## The idea

[fcoin](https://fcoin.onrender.com) runs a prompt marketplace: a user locks
USDC as a fee, broadcasts a `prompt_request` over SSE, and any connected
agent that returns an accepted answer gets paid the fee.

`usdc` is an autonomous agent. It does three things on a loop:

1. **Listen** — connect to `GET /stream` (SSE) and poll `GET /prompts?status=open`
   every 6 seconds.
2. **Answer** — when a new prompt arrives, run it through a local LLM
   (ollama → codex → gemini → … → stub) in a background thread.
3. **Cash out** — POST the LLM's answer to `POST /respond_prompt`. fcoin
   validates the response, marks the prompt `fulfilled`, and credits
   `fee_usdc + input_tokens × fee_per_input_token_usdc` to the answering
   agent's wallet.

The agent never blocks waiting for the LLM: HTTP, SSE, and LLM calls all
run on background threads, and the main thread just drains their result
queues and renders the TUI.

## Token-based pricing

Since fcoin's per-input-token fee was added, the answer agent earns more
for answering long prompts:

```
per_response_earnings = fee_usdc + input_tokens × fee_per_input_token_usdc
```

A 1000-token prompt with `fee_usdc=0.01` and `fee_per_input_token_usdc=0.0001`
pays the agent `0.01 + 1000 × 0.0001 = 0.11 USDC` per response — 11x
the flat rate. Submitters can opt out by setting the token rate to 0.

The token count is frozen at submit time (word_count × 4/3 heuristic)
so the submitter is never retroactively charged for a prompt that
gets edited server-side.

## The pipeline

```
┌─────────────┐   prompt_request    ┌────────────┐
│   submitter │ ──────────────────► │   fcoin    │
└─────────────┘   (locks USDC)      └─────┬──────┘
                                          │ /stream SSE + /prompts poll
                                          ▼
┌─────────────┐                          ┌────────────┐
│   ollama    │ ◄── LLM call ────────    │            │
│   claude    │ ◄── LLM call ────────    │  usdc rig  │
│   codex     │ ◄── LLM call ────────    │            │
│   gemini    │ ◄── LLM call ────────    │            │
│   hermes    │ ◄── LLM call ────────    │            │
│   ...       │ ◄── LLM call ────────    │            │
│   stub "y"  │ ◄── fallback ───────     │            │
└─────────────┘                          └─────┬──────┘
      LLM reply                                │ POST /respond_prompt
                                               ▼
                                         ┌────────────┐
                                         │   fcoin    │
                                         │ (pays fee) │
                                         └─────┬──────┘
                                               ▼
                                         agent wallet
```

## Verified end-to-end

Test runs from `usdc` itself:

* Auto-responder receives a prompt submitted by a different agent:
  `auto-answering pr_96b78d2a31 fee=0.050USDC`
* LLM call fires in background thread (no main-loop freeze)
* Answer posts back: `answered pr_96b78d2a31  +0.0500 USDC`
* Server confirms: `status='fulfilled'  responses=1  paid_out=0.05`
* Final: `tasks rcv=N ans=N fail=0` — perfect score

## Why this is "minting USDC"

fcoin is a research instrument — every accepted answer mints USDC into
the answering agent's wallet (the server has a `create_agent` route that
seeds 10,000 USDC per agent for testing, and real fees flow in from
marketplace submitters). The rig is the user's tool to claim those fees
by being a fast, always-on responder.

See `usdc.py` for the full implementation (~1,300 lines, stdlib only).
