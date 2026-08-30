# Decisions Log — drHiro on TrueForge

This log records the significant design and release decisions for the public package, with
rationale and any rejected alternatives. It complements `docs/PUBLIC_RELEASE_AUDIT.md`.

## D1 — Re-implement, don't sanitize the original

- **Decision:** Build a clean, self-contained re-implementation in a new repository rather
  than copying and scrubbing the original private drHiro monorepo.
- **Rationale:** The original repo contains a tracked `.env.dev`, real user Telegram IDs,
  private infrastructure IPs/hostnames, a production bot handle, `/home/<user>` paths, and 28
  token-like patterns in git history. Scrubbing is error-prone and can leak through history;
  a clean implementation guarantees zero private content.
- **Rejected:** Forking + `filter-repo` scrubbing (leaves surface area for leaks and
  backdating concerns).

## D2 — TrueForge manages the agent execution loop

- **Decision:** TrueForge (open source, MIT) owns model calls, tool orchestration, approvals,
  context, and session state. The Telegram bridge is a pure transport.
- **Rationale:** This is the submission's core claim — the agent loop lives in TrueForge, not
  in a bespoke bot. It also matches TrueForge's documented architecture.

## D3 — Long polling only; webhook/polling exclusivity

- **Decision:** The bot uses Telegram long polling by default. The installer detects an
  existing webhook and requires explicit `CONFIRM` before `deleteWebhook`; polling never runs
  while a webhook is set.
- **Rationale:** No public domain, HTTPS webhook, DNS, or cert is required — a user needs only
  a token. Running both would cause dropped/inconsistent updates.

## D4 — Exactly five installer inputs

- **Decision:** `install.sh` prompts only for: Telegram bot token, authorized Telegram
  username, AI backend base URL, AI API key (or placeholder), and model name.
- **Rationale:** Matches the spec and keeps the install self-contained. Everything else has a
  sane default or is derived.

## D5 — Synthetic data only

- **Decision:** The four tools operate on bundled synthetic fixtures, each labelled
  `SYNTHETIC`. `save_visit_brief` refuses non-synthetic payloads.
- **Rationale:** No real health data ships or exits the host. This is a privacy-first agent
  by construction, not by convention.

## D6 — Approval gate on the only write/export

- **Decision:** `save_visit_brief` is declared in `require_approval_for_tools`, so TrueForge
  pauses with `tool.approval_required`; the bridge presents Allow/Deny and resumes with
  `user.tool_approval`. A tool-level guard also refuses non-synthetic saves.
- **Rationale:** Defense in depth — the harness gate plus a tool-level guard.

## D7 — MCP server with four narrow tools

- **Decision:** Expose exactly the four required tools via an MCP server (SSE), rather than
  re-implementing drHiro's full production tool set.
- **Rationale:** Keeps the public package small, auditable, and aligned with the submission's
  tool list.

## D8 — Stdlib-only Telegram bridge

- **Decision:** The bridge uses only the Python standard library (urllib + json).
- **Rationale:** Slim container, no third-party surface to patch or audit.

## D9 — Structured output validation

- **Decision:** The agent spec declares `response_format: json_schema`; replies are validated
  against a schema.
- **Rationale:** Demonstrates structured output validation (a required submission element).

## D10 — Offline mock validation

- **Decision:** The test suite runs against mock Telegram + mock TrueForge servers.
- **Rationale:** Installation behaviour is validated deterministically with no real bot, real
  AI backend, or TrueForge instance. Supplements, not replaces, the live demo.

## D11 — Not a medical device

- **Decision:** Agent prompt, tool disclaimers, and docs explicitly state drHiro does not
  diagnose, prescribe, triage, or replace a clinician.
- **Rationale:** Required by the release constraints and ethically correct.

## D12 — Single feature branch + Qodo review

- **Decision:** All release work lands as one substantive feature branch; a PR goes through
  the Qodo review workflow before merge/publication.
- **Rationale:** Matches the phase plan and keeps the review trail clean and factual (no
  backdating or fabricated evidence).

## D13 — No fake Qodo evidence

- **Decision:** The README's "Qodo Code Review Evidence" section is a placeholder until a real
  Qodo-reviewed PR is merged; only then is the actual PR URL and factual summary added.
- **Rationale:** Release constraint: never fabricate review evidence.
