# Demo Script — drHiro on TrueForge

This is the recording script for the demo video (a placeholder link sits in the README).
It is written so a clean install and a full conversation can be recorded end-to-end with
either a real bot (long polling) or the offline mock (no real bot).

> **Privacy rule for the video:** never show a real bot token, API key, or `.env` contents.
> If a real token is used for a live demo, blur it in post-production.

## Scene 0 — Title card (5 s)

- On-screen: **"drHiro — Privacy-first Health Information Agent on TrueForge"**

## Scene 1 — The problem & what drHiro does (20 s)

- Narrate: drHiro helps a person prepare for a visit with their own clinician. It organizes
  what they want to discuss into a care-preparation brief.
- Emphasise: it does **not** diagnose, prescribe, triage, or replace a clinician. All data is
  **synthetic**.

## Scene 2 — Clean install on Ubuntu 22.04 (60–90 s)

- Show a fresh Ubuntu 22.04 terminal.
- `git clone <repo> && cd drhiro-trueforge`
- `sudo ./install.sh`
- Show the five prompts being answered (mask the bot token input — it is not echoed anyway).
- Show the installer validating the token (bot username printed, token not), detecting no
  webhook, validating the AI backend, then building and starting the stack.
- Show `./scripts/health-check.sh` → **ALL CHECKS PASSED**.

## Scene 3 — TrueForge admin UI (20 s)

- Open `http://<host>:8790`.
- Show the `drhiro` agent in the Agent Library; point out the model, the `drhiro-tools` MCP
  server, and the approval gate on `save_visit_brief`.

## Scene 4 — A full conversation (60–90 s)

Using the bot from the **authorized username**:

1. User: *"get me a demo case"* → bot returns the SYNTHETIC demo case.
2. User: *"create a visit brief about sleep and activity"* → bot returns the structured,
   non-diagnostic care-preparation brief (with disclaimer).
3. User: *"save the brief"* → bot shows **Allow / Deny** (TrueForge paused on
   `save_visit_brief`).
4. Narrate the approval gate; click **Allow**.
5. Bot confirms the brief was exported (returns the file path inside the container).
6. Optionally show `get_service_status`.

## Scene 5 — Safety & architecture recap (30 s)

- Show the architecture diagram (docs/ARCHITECTURE.md).
- Recap: long polling only, no webhook; single authorized user; approval-gated write;
  synthetic data only; secret-safe logs.

## Scene 6 — Validation (20 s)

- `./run_tests.sh` → **31 passed**.
- Point to docs/VALIDATION_REPORT.md.

## Scene 7 — Closing card (5 s)

- Repo URL, license (MIT), and the statement: *"Not a medical device."*

## Equipment & notes

- 1080p screen recording; clean shell with a readable font.
- Pre-warm Docker build if recording a live install, or record the build in time-lapse.
- Blur any real token/key that appears; prefer using the mock for a fully offline demo.
- Keep the narration calm and precise; avoid over-claiming (no medical claims, ever).
