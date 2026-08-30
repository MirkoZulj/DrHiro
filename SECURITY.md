# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for a security vulnerability. Report privately by
opening a GitHub Security Advisory (if this repository is public) or by contacting the
maintainer directly. Include a description of the issue, the affected version, and a
minimal reproduction if possible.

## Scope

This project is a reference/demo package for the TrueForge hackathon submission. It
demonstrates a privacy-first health information agent. Because it ships and uses only
synthetic data, the most important security properties are:

- **No real health data** is stored, transmitted, or shipped.
- **No secrets** are committed or logged.
- **Single-user, authorized by username** — only the configured Telegram username reaches
  the agent.
- **Long polling only** — no public webhook surface is exposed or required.

## Security model

See [docs/SECURITY_AND_PRIVACY.md](docs/SECURITY_AND_PRIVACY.md) for the full threat model,
including the approval gate, secret handling, webhook/polling exclusivity, and network
boundaries.

## Supported versions

Only the current `main` branch is supported. This is a demo package; no backports are
provided.

## Our security guarantees

1. The Telegram bot token and AI API key are stored only in a protected `.env` (mode 600),
   never printed, and never committed.
2. Long polling and a Telegram webhook are mutually exclusive; a webhook is only removed
   after explicit operator confirmation.
3. `save_visit_brief` (the only write/export action) requires explicit human approval via
   TrueForge's `tool.approval_required` flow.
4. All tool data is synthetic and labelled as such.

## A note on health safety

drHiro is **not** a medical device and does not diagnose, prescribe, provide emergency
triage, or replace a healthcare professional. If this limitation could put anyone at risk
in your deployment, do not deploy it in that context.
