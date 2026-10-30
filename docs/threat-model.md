# drHiro — Threat Model (initial)

Scope: self-hosted deployment for a household, single VPS, Docker Compose.
This is the initial pass; a formal review happens in Phase 6 before any
external users.

## Assets

- Health measurements (BP, weight, HR, sleep, nutrition) — sensitive
- Consent grants and household relationships
- Auth tokens (access, refresh, service)
- Food photos
- Audit log

## Trust boundaries

```
Internet
  |  TLS (Caddy) — only 80/443
  v
[Caddy] -> [Web] [API] [OpenClaw]
                  |          |
                  v          v
            [Postgres]  [Redis]  [MinIO]   (private Docker network)
```

## Threats & mitigations

| # | Threat | Mitigation |
|---|--------|-----------|
| T1 | Cross-tenant read (User A reads B) | user_id scoping on every query; acceptance test #1 |
| T2 | Forged Mini App identity | server-side initData HMAC validation; never trust client JS |
| T3 | Forged Android installation | device-code exchange; installation must be linked; batch rejected otherwise |
| T4 | Replay of ingestion batches | batch_id idempotency key; per-record upsert by source id |
| T5 | LLM prompt injection (captions/OCR/URLs) | treat all user content as untrusted data; tools are narrow and signed |
| T6 | LLM fabricates/suppresses alerts | deterministic rule engine only; LLM has no alert-write tool |
| T7 | Token theft | short access TTL (15m), rotating refresh, secret rotation runbook |
| T8 | Secret exposure | .env never committed; Docker secrets/root-readable files; scanning |
| T9 | Photo privacy (EXIF, retention) | EXIF stripping, signed short-expiry URLs, configurable retention |
| T10 | Data loss | daily encrypted backup + monthly restore test |
| T11 | Image supply-chain | pinned versions/digests, no `latest` in prod, dependency scanning |
| T12 | Audit tampering | audit_events append-only in practice; actor recorded for exports/consent |

## Data minimization

- Collect DOB/sex/height only when explicitly supplied (all optional).
- Store Telegram file IDs only as long as required.
- Analytics opt-in separate from core processing.
- Export + deletion workflows implemented (Section 8.6).

## GDPR posture (EU)

- Lawful basis: explicit consent for processing (consent_grants).
- Data access/export/deletion: implemented.
- Processor inventory + breach response: document before public release.
- Legal review required before handling users beyond the household.

## Open items

- Android app signing + tamper resistance
- Photo vision provider (cloud vs self-hosted) decision
- Formal pentest before external users (Phase 6)
