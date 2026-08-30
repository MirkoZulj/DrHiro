# Public Release Audit — drHiro on TrueForge

**Auditor:** Jeeves (release engineer)
**Date:** 2026-08-30
**Status:** Complete
**Scope:** Audit of the existing private `drHiro` project before producing a clean,
public-safe, installable package for the TrueForge hackathon submission.

---

## 1. Purpose

This document records what was found in the original private drHiro codebase, what
is and is not safe to carry into the public release, and the policy decisions that
govern the new package. It is the source of truth for the public-safety constraints
enforced throughout this release.

## 2. Source materials audited

| Item | Path (private, redacted) | Role |
|------|----------------|------|
| Active drHiro monorepo | `<local>/projects/drh-hiro` | FastAPI core, worker, web, Android bridge, OpenClaw agent, packages |
| Legacy prototype | `<local>/projects/DrHiro` | Older standalone bot (reference only) |
| TrueForge runtime | `/opt/trueforge` (VPS), MIT open source | Agent execution harness |

The **active monorepo** is the primary source of the drHiro-on-TrueForge concept.
The **legacy prototype** is explicitly "reference material only" in the source README
and is not a candidate for release.

## 3. What drHiro is

A privacy-first personal health information agent. It records, stores, and reflects
back an individual's own health metrics (steps, weight, blood pressure, sleep, meals)
via a Telegram conversation, a web/Mini App dashboard, and an Android Health Connect
bridge. A deterministic rule engine produces alerts; an OpenClaw agent (in production,
now being migrated to TrueForge) handles the conversational layer over a consent-scoped
core API. **It does not diagnose, prescribe, triage, or replace a clinician.**

## 4. Findings — what must NOT enter the public package

The following were found in the original codebase and must never be copied, committed,
or referenced in the public release:

| Class | Evidence found | Disposition |
|-------|----------------|-------------|
| Real secrets | `infra/.env.dev` tracked in git (dev passwords); `infra/.env.prod` present on disk (not tracked) | Excluded; new `.env.example` uses placeholders only |
| Secret-shaped git history | 28 token-like patterns matched in `git log --all -p` (bot-token / password patterns) | Original history not carried into the new repo |
| User identity | A real Telegram user ID hardcoded in `openclaw/skills/skill-drhiro/SKILL.md` | Replaced with a config-driven `ALLOWED_USERNAME`; no hardcoded IDs |
| Private infrastructure | VPS IP, a Windows-box IP, a Tailscale hostname, a Docker-gateway IP, and `.sslip.io` domains (values withheld here by design) | Excluded |
| Username / local paths | `/home/<user>/...` paths in scripts, README, `import_usda.py` | Excluded |
| Bot identity | A named production bot handle referenced in tf-shim README | Excluded; new package is bot-token driven |
| Patient / user health data | Real measurements, meals, weight, goals in the production DB; photo-derived values | Never copied; package ships synthetic fixtures only, explicitly labeled |
| Employer/client assets | DrHiro is Kresimir's own personal project; no third-party client data found | n/a |
| Local model files | `nutrition_cache.json`, USDA/CIQUAL data caches, `.gguf` model references | Excluded; package is model-provider agnostic |
| Production config | `infra/.env.prod`, `docker-compose.vps.yml` (hardcoded VPS host), Caddyfile with private domains | Excluded; new compose is generic and self-contained |

## 5. What is safe to reuse (conceptually, re-implemented)

The public package **re-implements** the following concepts from scratch (no source
copying, no private references):

- FastAPI-style structured API, but built fresh as a **MCP tool server** for the four
  submission tools.
- TrueForge as the agent execution harness (open source, MIT) — the design centrepiece.
- Long-polling Telegram transport (no public webhook required).
- Approval-gated write/export actions (TrueForge `tool.approval_required`).
- Structured output validation (TrueForge `response_format: json_schema`).
- Session-context safety (one TrueForge session per conversation, synthetic data only).

All code in the new package is newly written for this release.

## 6. Design decisions (see docs/DECISIONS.md for the full log)

1. **TrueForge manages the agent loop.** Telegram is a pure transport; the model,
   tools, approvals, context, and session state all live in TrueForge.
2. **Synthetic data only.** The tools operate on a bundled, clearly-labelled synthetic
   fixture store. No real health data ships.
3. **No webhook by default.** Long polling is the default; the installer detects an
   existing webhook and requires explicit confirmation before removing it, and never
   runs polling and webhook simultaneously.
4. **Five inputs only.** Telegram bot token, authorized Telegram username,
   OpenAI-compatible backend base URL, API key (or placeholder), and model name.
5. **No medical claims.** The package and docs explicitly state it does not diagnose,
   prescribe, triage, or replace a clinician.

## 7. Remaining verification

- Full secret scan of the new repo before any push (CI gate + manual review).
- Dependency and license review (TrueForge is MIT; app code is MIT).
- Live install-and-test of the package in a clean Docker environment.
- Qodo code review of the feature PR (documented in README).

## 8. Conclusion

The original project is a rich private codebase and must be treated as such. The public
release is a **clean re-implementation** that preserves the drHiro-on-TrueForge concept
and its safety architecture while containing **zero** private data, secrets, identities,
infrastructure references, or production configuration. This audit is filed as
`docs/PUBLIC_RELEASE_AUDIT.md` and will be re-checked before any external publish.
