# Contributing to drHiro on TrueForge

Thanks for considering a contribution. Please read and respect the project's core
boundaries before opening an issue or PR.

## Ground rules

1. **No real health data, ever.** This project works with synthetic, clearly-labelled
   fixtures only. Do not add code, tests, fixtures, or docs that introduce real patient
   data, real user identities, or production configuration.
2. **No secrets in the repository.** Never commit a filled `.env`, a token, an API key, a
   password, or a private host/IP. The `.gitignore` protects `.env`; keep it that way.
3. **No medical claims.** The project does not diagnose, prescribe, triage, or replace a
   clinician. Keep disclaimers explicit and honest.
4. **Secret-safe logs.** Logging must never emit the bot token, API keys, or message bodies.

## Development setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e 'services/drhiro-tools[dev]'
./run_tests.sh          # or: python -m pytest tests/ -v
```

## What belongs where

- `services/drhiro-tools/` — the four MCP tools over synthetic data.
- `services/telegram-bridge/` — long-polling Telegram transport to TrueForge.
- `agent/drhiro.agent.json` — the TrueForge agent spec.
- `install.sh` + `scripts/` — Ubuntu installer and operational scripts.
- `tests/` — offline tests using mock Telegram + mock TrueForge (no real bot needed).
- `docs/` — architecture, install, security, validation, demo, decisions.

## Before a PR

1. Run `./run_tests.sh` and confirm the whole suite is green.
2. Run a secret scan on your diff (no tokens, keys, private IPs, usernames, or paths).
3. Verify you have not modified the original private drHiro project — this is a clean,
   self-contained re-implementation.
4. Keep changes on a single feature branch. No history rewriting or backdating.

## Code of conduct

Be constructive and respectful. drHiro is a health-adjacent project; treat contributors
and the subject matter with care.
